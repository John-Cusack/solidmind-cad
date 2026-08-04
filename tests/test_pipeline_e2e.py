"""The pipeline end to end, across the split layout.

Mechanism → canonical package → courtesy URDF → RL env config, with every
boundary crossed the way it is crossed in production: core writes only neutral
artifacts, the engine compiles its own dialect from them, and the RL pipeline
runs as a subprocess on its own interpreter.

Engine-side legs live with their engines now: package→SDF in
solidmind-engine-gazebo, URDF import in solidmind-engine-isaac.  What core
verifies here is its own half — that the artifacts it writes are neutral,
schema-valid and readable by a contract engine.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from server.motion_models import JointEdge, JointType, Mechanism, PartNode
from server.sim_export import build_sim_model, write_urdf
from server.sim_package_manifest import build_manifest, write_manifest


def _rl_pipeline_installed() -> bool:
    """Is solidmind-rl on this machine?

    After the split it is a separate repository on Isaac's interpreter, so a
    core checkout usually does not have it — and the tools are expected to say
    so rather than fail.
    """
    import subprocess

    from server.tools_rl import _rl_python

    proc = subprocess.run(
        [_rl_python(), "-m", "rl_training.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


_STL = """solid leg
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 40 0 0
      vertex 0 20 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 0 0 20
      vertex 40 0 20
      vertex 0 20 20
    endloop
  endfacet
endsolid leg
"""


def _walker_mechanism() -> Mechanism:
    """A two-legged walker — enough joints to exercise the whole chain."""
    return Mechanism(
        name="pipeline_walker",
        parts=(
            PartNode(id="chassis", body_name="Body_Chassis", is_ground=True, mass_kg=1.0),
            PartNode(id="leg_l", body_name="Body_LegL", mass_kg=0.2),
            PartNode(id="leg_r", body_name="Body_LegR", mass_kg=0.2),
        ),
        joints=(
            JointEdge(
                id="hip_l",
                joint_type=JointType.REVOLUTE,
                parent_part="chassis",
                child_part="leg_l",
                origin=(50.0, 30.0, 0.0),
                axis=(0.0, 1.0, 0.0),
                min_angle_deg=-45.0,
                max_angle_deg=45.0,
            ),
            JointEdge(
                id="hip_r",
                joint_type=JointType.REVOLUTE,
                parent_part="chassis",
                child_part="leg_r",
                origin=(50.0, -30.0, 0.0),
                axis=(0.0, 1.0, 0.0),
                min_angle_deg=-45.0,
                max_angle_deg=45.0,
            ),
        ),
        drives=(),
    )


def _body_manifest(tmp: Path) -> list[dict[str, Any]]:
    bodies = []
    for name, position in (
        ("Body_Chassis", [0.0, 0.0, 0.0]),
        ("Body_LegL", [50.0, 30.0, 0.0]),
        ("Body_LegR", [50.0, -30.0, 0.0]),
    ):
        mesh = tmp / f"{name}.stl"
        mesh.write_text(_STL)
        bodies.append(
            {
                "name": name,
                "label": name,
                "mesh_path": str(mesh),
                "placement": {"position": position, "rotation_quat": [1.0, 0.0, 0.0, 0.0]},
                "bbox_mm": [40.0, 20.0, 20.0],
                "bbox_min_mm": [0.0, 0.0, 0.0],
                "volume_mm3": 8000.0,
            }
        )
    return bodies


class TestPipelineAcrossTheSplit(unittest.TestCase):
    """Every artifact that crosses a repository boundary, in order."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.pkg = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        bodies = _body_manifest(self.pkg)
        self.model = build_sim_model(_walker_mechanism(), bodies)
        manifest = build_manifest(
            name=self.model.name,
            output_dir=str(self.pkg),
            body_manifest=bodies,
            sim_model=self.model,
        )
        self.manifest_path = Path(write_manifest(manifest, str(self.pkg)))
        self.urdf_path = Path(
            write_urdf(
                self.model, str(self.pkg / f"{self.model.name}.urdf"), base_dir=str(self.pkg)
            )
        )

    # -- what core produces ---------------------------------------------

    def test_core_writes_only_neutral_artifacts(self) -> None:
        """Manifest, meshes, URDF — and nothing vendor-specific."""
        written = sorted(p.name for p in self.pkg.iterdir())
        self.assertIn("manifest.json", written)
        self.assertIn("pipeline_walker.urdf", written)
        self.assertEqual([p for p in written if p.endswith(".sdf")], [])
        self.assertEqual([p for p in written if p.startswith("5")], [])  # no PX4 airframe

    def test_manifest_validates_against_the_published_schema(self) -> None:
        import jsonschema

        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "sim_package.schema.json"
        with open(schema_path) as fh:
            schema = json.load(fh)
        with open(self.manifest_path) as fh:
            jsonschema.validate(json.load(fh), schema)

    # -- what an engine makes of it -------------------------------------

    # The Gazebo leg — package in, SDF out — moved to solidmind-engine-gazebo
    # with the bridge.  What core can still assert is that any contract engine
    # reads the package, which the reference engine does below.

    def test_the_reference_engine_ingests_the_package(self) -> None:
        """Any contract engine can read what core wrote."""
        from reference_engine.runtime import ReferenceRuntime

        result = ReferenceRuntime().simulate(
            {"package_path": str(self.pkg), "duration_s": 0.2, "dt_s": 0.01}
        )
        self.assertEqual(result["summary"]["link_count"], 3)
        self.assertEqual(result["summary"]["joint_count"], 2)
        self.assertGreater(len(result["time_series"]), 1)

    # -- what the RL pipeline makes of it -------------------------------

    @unittest.skipUnless(_rl_pipeline_installed(), "solidmind-rl is not installed")
    def test_rl_configures_an_environment_from_the_urdf(self) -> None:
        """The CLI parity path: core shells out, the pipeline answers JSON.

        This is the boundary that used to be an in-process import of a package
        that lives on Isaac's interpreter.
        """
        from server.tools_rl import rl_configure_environment

        result = rl_configure_environment(
            urdf_path=str(self.urdf_path),
            output_path=str(self.pkg / "env_config.py"),
            num_envs=16,
        )
        self.assertTrue(result["ok"], result.get("error"))

        analysis = result["analysis"]
        self.assertEqual(analysis["robot_name"], "pipeline_walker")
        self.assertEqual(sorted(analysis["actuated_joints"]), ["hip_l", "hip_r"])
        self.assertGreater(analysis["total_mass_kg"], 0.0)

        config = Path(result["config_path"])
        self.assertTrue(config.is_file())
        self.assertIn("hip_l", config.read_text())

    def test_rl_reports_a_missing_urdf_rather_than_raising(self) -> None:
        from server.tools_rl import rl_configure_environment

        result = rl_configure_environment(urdf_path="/nonexistent/robot.urdf")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")

    @unittest.skipIf(_rl_pipeline_installed(), "solidmind-rl is installed here")
    def test_absent_rl_pipeline_degrades_to_a_clear_error(self) -> None:
        """Core without solidmind-rl says so — it does not crash on an import."""
        from server.tools_rl import rl_configure_environment

        result = rl_configure_environment(urdf_path=str(self.urdf_path))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "RL_PIPELINE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
