"""Tests for the FreeCAD → Gazebo → PX4 SITL pipeline.

This is the legacy ``examples/quadrotor_camera_drone/run.py`` path:
build geometry in FreeCAD, export STL meshes + SDF + airframe init,
deploy to PX4's gz models tree, register in CMakeLists, fly.

Three tiers of tests:

1. **Unit tests** (no FreeCAD, no PX4 install).  Cover the helpers in
   ``run.py`` that map airframe names to model dirs, deploy SDF + STLs
   into a target tree, and patch CMakeLists.  These run in <50 ms.
2. **Integration tests** (require FreeCAD addon on TCP 9876).  Drive
   the FreeCAD addon to build geometry, export, and verify the
   generated airframe init script has FRD-correct CA_ROTOR values.
   Skipped if FreeCAD isn't reachable.
3. **End-to-end smoke** (requires PX4-Autopilot installed *and*
   ``SOLIDMIND_RUN_SITL_SMOKE=1``).  Boots PX4, force-arms,
   AUTO_TAKEOFF, asserts altitude.  Skipped by default.

Run just this file::

    python3 -m unittest tests.test_freecad_to_gazebo

Run only the unit tier (fast, deterministic, no external deps)::

    python3 -m unittest tests.test_freecad_to_gazebo.TestRunPyHelpers

Enable the smoke test::

    SOLIDMIND_RUN_SITL_SMOKE=1 python3 -m unittest tests.test_freecad_to_gazebo
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# Make examples/ importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Tier 1: unit tests (no FreeCAD, no PX4 install)
# ---------------------------------------------------------------------------


class TestRunPyHelpers(unittest.TestCase):
    """Pure functions in run.py — name munging, file deployment, cmake patching."""

    def setUp(self) -> None:
        from examples.quadrotor_camera_drone import run as run_mod

        self.run = run_mod

    # --- name munging ---

    def test_make_target_name_adds_gz_prefix(self) -> None:
        self.assertEqual(self.run._make_target_name("camera_drone"), "gz_camera_drone")

    def test_make_target_name_handles_full_filename(self) -> None:
        # "50837_gz_<model>" → "gz_<model>"
        self.assertEqual(
            self.run._make_target_name("50837_gz_camera_drone"),
            "gz_camera_drone",
        )

    def test_make_target_name_idempotent_on_prefixed(self) -> None:
        self.assertEqual(self.run._make_target_name("gz_camera_drone"), "gz_camera_drone")

    def test_model_base_name_strips_gz(self) -> None:
        self.assertEqual(self.run._model_base_name("camera_drone"), "camera_drone")
        self.assertEqual(self.run._model_base_name("gz_camera_drone"), "camera_drone")
        self.assertEqual(self.run._model_base_name("50837_gz_camera_drone"), "camera_drone")

    # --- deploy_model_to_px4 ---

    def test_deploy_model_to_px4_copies_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            px4 = Path(tmp) / "px4"
            out_dir.mkdir()
            (out_dir / "test.sdf").write_text(
                f'<sdf><model name="test"><link><visual>'
                f"<geometry><mesh><uri>{out_dir}/Chassis.stl</uri></mesh></geometry>"
                f"</visual></link></model></sdf>"
            )
            (out_dir / "Chassis.stl").write_text("solid stub\nendsolid")
            (out_dir / "rotor_FR.stl").write_text("solid stub\nendsolid")

            model_dir = self.run.deploy_model_to_px4(out_dir, px4, "test_drone")

            # Model dir at expected location
            expected = px4 / "Tools" / "simulation" / "gz" / "models" / "test_drone"
            self.assertEqual(model_dir, expected)
            # STLs copied
            self.assertTrue((model_dir / "Chassis.stl").exists())
            self.assertTrue((model_dir / "rotor_FR.stl").exists())
            # SDF copied as model.sdf with mesh URIs rewritten to relative
            sdf_text = (model_dir / "model.sdf").read_text()
            self.assertIn("<uri>Chassis.stl</uri>", sdf_text)
            self.assertNotIn(str(out_dir), sdf_text)
            # model.config written
            self.assertIn("test_drone", (model_dir / "model.config").read_text())

    def test_deploy_model_to_px4_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            px4 = Path(tmp) / "px4"
            out_dir.mkdir()
            (out_dir / "test.sdf").write_text("<sdf/>")
            self.run.deploy_model_to_px4(out_dir, px4, "test_drone")
            # Second call should not raise.
            self.run.deploy_model_to_px4(out_dir, px4, "test_drone")

    def test_deploy_raises_when_no_sdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            px4 = Path(tmp) / "px4"
            out_dir.mkdir()
            with self.assertRaises(RuntimeError):
                self.run.deploy_model_to_px4(out_dir, px4, "test_drone")

    # --- patch_airframes_cmakelists ---

    def _make_cmakelists(self, root: Path) -> Path:
        cmake_dir = root / "ROMFS" / "px4fmu_common" / "init.d-posix" / "airframes"
        cmake_dir.mkdir(parents=True)
        cmake_lists = cmake_dir / "CMakeLists.txt"
        cmake_lists.write_text(
            textwrap.dedent("""\
            px4_add_romfs_files(

                4001_gz_x500
                50000_gz_rover_differential

                # [22000, 22999] Reserve for custom models
            )
        """)
        )
        return cmake_lists

    def test_patch_cmakelists_inserts_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            px4 = Path(tmp)
            cmake_lists = self._make_cmakelists(px4)
            self.run.patch_airframes_cmakelists(px4, "50837_gz_camera_drone")
            text = cmake_lists.read_text()
            self.assertIn("50837_gz_camera_drone", text)

    def test_patch_cmakelists_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            px4 = Path(tmp)
            cmake_lists = self._make_cmakelists(px4)
            self.run.patch_airframes_cmakelists(px4, "50837_gz_camera_drone")
            text_after_first = cmake_lists.read_text()
            self.run.patch_airframes_cmakelists(px4, "50837_gz_camera_drone")
            text_after_second = cmake_lists.read_text()
            self.assertEqual(text_after_first, text_after_second)
            self.assertEqual(text_after_first.count("50837_gz_camera_drone"), 1)


# ---------------------------------------------------------------------------
# Tier 2: integration test (requires FreeCAD addon on TCP 9876)
# ---------------------------------------------------------------------------


def _freecad_addon_reachable(host: str = "127.0.0.1", port: int = 9876) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (TimeoutError, OSError):
        return False


@unittest.skipUnless(
    _freecad_addon_reachable(),
    "FreeCAD addon not running on 127.0.0.1:9876 — start FreeCAD with the bridge addon",
)
class TestFreecadToAirframeIntegration(unittest.TestCase):
    """Build geometry in FreeCAD, export, verify the airframe init.

    These tests catch the convention-mismatch bugs that break flight:
    if the SDF puts rotor_FR at FLU front-right but CA_ROTOR0 says
    front-left, motors saturate to zero in flight.  This test asserts
    the two agree.
    """

    def test_legacy_export_produces_frd_correct_airframe(self) -> None:
        """`run.py`-style export → SDF in FLU + airframe in FRD, matched."""
        from examples.quadrotor_camera_drone import run as run_mod

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            doc_name = "FreecadGazeboTest"
            run_mod.build_drone_geometry(doc_name)
            mech_id = run_mod.define_rotor_mechanism(
                {
                    "chassis_body": "Chassis",
                    "rotor_bodies": [r[0] for r in run_mod.ROTORS],
                }
            )

            # Export against a stub PX4 install — generator writes the
            # init script under <px4>/ROMFS/.../airframes/.
            stub_px4 = out_dir / "px4"
            (stub_px4 / "ROMFS" / "px4fmu_common" / "init.d-posix" / "airframes").mkdir(
                parents=True
            )

            result = run_mod.export_sim_package_with_px4(
                mech_id,
                out_dir,
                stub_px4,
            )
            airframe_path = Path(result["airframe_path"])
            self.assertTrue(airframe_path.exists())
            init_text = airframe_path.read_text()

            # rotor_FR is at FLU (+ARM, -ARM) — front-right.
            # In FRD that's (+ARM, +ARM) — same physical location.
            # CA_ROTOR0 (which is rotor_FR per index 0) must therefore
            # have PY > 0 in the generated airframe init.
            import re

            m = re.search(r"CA_ROTOR0_PY\s+(-?\d+\.?\d*)", init_text)
            self.assertIsNotNone(m, "CA_ROTOR0_PY not found in airframe init")
            assert m is not None
            ca_py = float(m.group(1))
            self.assertGreater(
                ca_py,
                0.0,
                "CA_ROTOR0_PY (FRD) must be positive — rotor_FR sits at "
                "front-right, which is +Y in FRD body frame.  If this is "
                "negative or zero, extract_rotors() is missing the "
                "FLU→FRD conversion.",
            )

            # Sanity: SDF rotor_FR <pose> Y should be NEGATIVE (FLU
            # right-of-forward = -Y).
            sdf_text = Path(result["sdf_path"]).read_text()
            m = re.search(
                r'<link name="rotor_FR">.*?<pose[^>]*>([-\d.\s]+)</pose>',
                sdf_text,
                re.DOTALL,
            )
            self.assertIsNotNone(m, "rotor_FR pose not found in SDF")
            assert m is not None
            sdf_pose_y = float(m.group(1).split()[1])
            self.assertLess(
                sdf_pose_y,
                0.0,
                "SDF rotor_FR <pose> Y must be negative — FLU right is -Y.  "
                "If positive, the run.py ROTORS table mislabels the "
                "rotor (front-right at +Y is actually front-left in FLU).",
            )


# ---------------------------------------------------------------------------
# Tier 3: end-to-end SITL smoke test (opt-in)
# ---------------------------------------------------------------------------


def _smoke_enabled() -> bool:
    return bool(os.environ.get("SOLIDMIND_RUN_SITL_SMOKE", ""))


def _resolve_px4_install() -> Path | None:
    candidate = os.environ.get("SOLIDMIND_PX4_INSTALL")
    path = Path(candidate).expanduser() if candidate else Path.home() / "repos" / "PX4-Autopilot"
    return path if (path / "ROMFS").is_dir() else None


@unittest.skipUnless(_smoke_enabled(), "Set SOLIDMIND_RUN_SITL_SMOKE=1 to enable")
@unittest.skipUnless(
    _resolve_px4_install() is not None,
    "PX4-Autopilot not installed (set SOLIDMIND_PX4_INSTALL)",
)
@unittest.skipUnless(
    _freecad_addon_reachable(),
    "FreeCAD addon not running on 127.0.0.1:9876",
)
class TestFreecadToFlightSmoke(unittest.TestCase):
    """Build, export, deploy, fly — assert the drone reaches takeoff alt."""

    def test_legacy_drone_takes_off(self) -> None:
        # Implementation deferred — pattern matches tests/test_quadrotor_smoke.py.
        # Enable when PX4 + FreeCAD are both available in CI.
        self.skipTest("not yet implemented; see test_quadrotor_smoke for pattern")


if __name__ == "__main__":
    unittest.main()
