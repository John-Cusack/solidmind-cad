"""Unit tests for ``server.px4_airframe_generator``.

Pure-math + file-format tests.  E2E tests that actually fly a SolidMind
airframe under PX4 live in ``tests.test_px4_solidmind_drone_e2e``
(Phase 4 verification, gated on PX4 build).
"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from server.px4_airframe_generator import (
    _AUTOSTART_BASE,
    _AUTOSTART_RANGE,
    _X500_ARM_LENGTH_M,
    _X500_MC_ROLLRATE_P,
    AirframeGeneratorError,
    airframe_filename,
    compute_arm_length,
    compute_hover_throttle,
    compute_sys_autostart,
    compute_total_mass,
    extract_rotors,
    format_airframe_init_script,
    generate_airframe_params,
    register_airframe,
    seed_pid_gains,
)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@dataclass
class _FakeLink:
    name: str
    position: tuple[float, float, float]  # mm
    mass_kg: float | None = None


@dataclass
class _FakeJoint:
    name: str
    parent: str
    child: str


@dataclass
class _FakeSimModel:
    links: list[_FakeLink] = field(default_factory=list)
    joints: list[_FakeJoint] = field(default_factory=list)


def _x500_like_drone_config() -> dict:
    """drone_config matching the stock X500 layout (4 rotors).

    Positions are in **Gazebo FLU body frame** (X forward, Y left,
    Z up), matching the SDF ``<pose>`` convention.  ``extract_rotors``
    converts these to PX4 FRD (negates Y, Z).  Stock x500 has rotor 0
    at FRD (+0.13, +0.22), which is FLU (+0.13, -0.22).
    """
    return {
        "rotors": [
            {
                "index": 0,
                "joint": "rotor_0_joint",
                "direction": "ccw",
                "position_m": (0.13, -0.22, 0.0),
            },
            {
                "index": 1,
                "joint": "rotor_1_joint",
                "direction": "ccw",
                "position_m": (-0.13, 0.20, 0.0),
            },
            {
                "index": 2,
                "joint": "rotor_2_joint",
                "direction": "cw",
                "position_m": (0.13, 0.22, 0.0),
            },
            {
                "index": 3,
                "joint": "rotor_3_joint",
                "direction": "cw",
                "position_m": (-0.13, -0.20, 0.0),
            },
        ],
    }


# ----------------------------------------------------------------------
# SYS_AUTOSTART
# ----------------------------------------------------------------------


class TestComputeSysAutostart(unittest.TestCase):
    def test_returns_value_in_solidmind_range(self) -> None:
        for name in ("camera_drone", "racer_250", "delivery_hex_v3"):
            with self.subTest(name=name):
                v = compute_sys_autostart(name)
                self.assertGreaterEqual(v, _AUTOSTART_BASE)
                self.assertLess(v, _AUTOSTART_BASE + _AUTOSTART_RANGE)

    def test_stable_across_calls(self) -> None:
        a = compute_sys_autostart("camera_drone_v2")
        b = compute_sys_autostart("camera_drone_v2")
        self.assertEqual(a, b)

    def test_distinct_names_differ_with_high_probability(self) -> None:
        names = [f"drone_{i}" for i in range(50)]
        ids = {compute_sys_autostart(n) for n in names}
        # 50 random names in 1000 buckets: collisions are possible but
        # rare; require at least 45 unique to catch a degenerate hash.
        self.assertGreaterEqual(len(ids), 45)

    def test_empty_name_raises(self) -> None:
        with self.assertRaises(AirframeGeneratorError) as cm:
            compute_sys_autostart("")
        self.assertEqual(cm.exception.code, "INVALID_MODEL_NAME")


# ----------------------------------------------------------------------
# Rotor extraction
# ----------------------------------------------------------------------


class TestExtractRotors(unittest.TestCase):
    def test_uses_explicit_position_m(self) -> None:
        # FLU input (0.13, -0.22, 0) → FRD output (0.13, +0.22, 0).
        cfg = _x500_like_drone_config()
        rotors = extract_rotors(cfg)
        self.assertEqual(len(rotors), 4)
        self.assertAlmostEqual(rotors[0].px_m, 0.13)
        self.assertAlmostEqual(rotors[0].py_m, 0.22)

    def test_negates_y_and_z_for_flu_to_frd(self) -> None:
        """FLU input is converted to FRD: Y and Z negated, X unchanged."""
        cfg = {
            "rotors": [
                {"joint": "j", "direction": "ccw", "position_m": (0.10, 0.20, 0.05)},
            ],
        }
        rotors = extract_rotors(cfg)
        self.assertAlmostEqual(rotors[0].px_m, +0.10)
        self.assertAlmostEqual(rotors[0].py_m, -0.20)
        self.assertAlmostEqual(rotors[0].pz_m, -0.05)

    def test_normalizes_direction_to_plus_minus_one(self) -> None:
        cfg = {
            "rotors": [
                {"joint": "j0", "direction": "ccw", "position_m": (0.1, 0.1)},
                {"joint": "j1", "direction": "cw", "position_m": (-0.1, 0.1)},
                {"joint": "j2", "direction": 1, "position_m": (0.1, -0.1)},
                {"joint": "j3", "direction": -1, "position_m": (-0.1, -0.1)},
            ],
        }
        rotors = extract_rotors(cfg)
        self.assertEqual([r.direction for r in rotors], [1, -1, 1, -1])

    def test_falls_back_to_sim_model_link_position(self) -> None:
        # SimLink positions are FLU mm; extract_rotors converts to FRD m.
        sim = _FakeSimModel(
            links=[
                _FakeLink("base", (0.0, 0.0, 0.0)),
                _FakeLink("rotor_fl", (130.0, -220.0, 0.0)),  # FLU mm
            ],
            joints=[_FakeJoint("rotor_fl_joint", "base", "rotor_fl")],
        )
        cfg = {
            "rotors": [{"joint": "rotor_fl_joint", "direction": "ccw"}],
        }
        rotors = extract_rotors(cfg, sim_model=sim)
        self.assertEqual(len(rotors), 1)
        # FLU (130, -220, 0) mm → FRD (0.130, +0.220, 0) m.
        self.assertAlmostEqual(rotors[0].px_m, 0.130)
        self.assertAlmostEqual(rotors[0].py_m, 0.220)

    def test_missing_position_raises(self) -> None:
        cfg = {"rotors": [{"joint": "j", "direction": "ccw"}]}
        with self.assertRaises(AirframeGeneratorError) as cm:
            extract_rotors(cfg)
        self.assertEqual(cm.exception.code, "ROTOR_POSITION_MISSING")

    def test_empty_rotor_list_raises(self) -> None:
        with self.assertRaises(AirframeGeneratorError) as cm:
            extract_rotors({"rotors": []})
        self.assertEqual(cm.exception.code, "NO_ROTORS")

    def test_per_rotor_motor_overrides_propagate(self) -> None:
        cfg = {
            "rotors": [
                {
                    "joint": "j",
                    "direction": "ccw",
                    "position_m": (0.1, 0.1),
                    "motor_constant": 1.5e-5,
                    "max_rot_velocity": 1500.0,
                    "moment_constant": 0.07,
                }
            ],
        }
        rotors = extract_rotors(cfg)
        self.assertAlmostEqual(rotors[0].motor_constant, 1.5e-5)
        self.assertAlmostEqual(rotors[0].max_rot_velocity, 1500.0)
        self.assertAlmostEqual(rotors[0].moment_constant, 0.07)


# ----------------------------------------------------------------------
# Arm length, mass, hover throttle, PID seeds
# ----------------------------------------------------------------------


class TestGeometryDerived(unittest.TestCase):
    def test_arm_length_x500_matches_reference(self) -> None:
        rotors = extract_rotors(_x500_like_drone_config())
        arm = compute_arm_length(rotors)
        self.assertAlmostEqual(arm, _X500_ARM_LENGTH_M, places=2)

    def test_total_mass_sums_links(self) -> None:
        sim = _FakeSimModel(
            links=[
                _FakeLink("a", (0, 0, 0), mass_kg=0.5),
                _FakeLink("b", (0, 0, 0), mass_kg=1.5),
                _FakeLink("c", (0, 0, 0)),  # no mass
            ]
        )
        self.assertAlmostEqual(compute_total_mass(sim), 2.0)

    def test_total_mass_zero_raises(self) -> None:
        sim = _FakeSimModel(links=[_FakeLink("a", (0, 0, 0))])
        with self.assertRaises(AirframeGeneratorError) as cm:
            compute_total_mass(sim)
        self.assertEqual(cm.exception.code, "NO_MASS")

    def test_hover_throttle_x500_close_to_reference(self) -> None:
        rotors = extract_rotors(_x500_like_drone_config())
        # Stock X500 mass ~ 2 kg, hover ~ 0.6.  Our formula is more
        # principled than X500's pre-tuned value, so tolerance is loose.
        h = compute_hover_throttle(2.0, rotors)
        self.assertGreater(h, 0.5)
        self.assertLess(h, 0.8)

    def test_hover_throttle_rejects_infeasible_specs(self) -> None:
        """Out-of-range hover ω is a spec error, not silently clamped.

        The legacy implementation clipped hover throttle to [0.3, 0.8]
        when the drone was too heavy or too light for its rotors.  That
        masked motor / mass mismatches that should be caller-visible.
        The corrected implementation raises so the operator can pick
        appropriate motors instead of getting a wrong-but-plausible
        MPC_THR_HOVER baked into the airframe.
        """
        from server.px4_airframe_generator import AirframeGeneratorError

        rotors = extract_rotors(_x500_like_drone_config())
        # 100 kg on x500-class motors → ω_hover ~5400 rad/s, max 1000.
        with self.assertRaises(AirframeGeneratorError) as ctx:
            compute_hover_throttle(100.0, rotors)
        self.assertEqual(ctx.exception.code, "HOVER_INFEASIBLE")
        # 10 g on x500-class motors → ω_hover ~54 rad/s, min 150.
        with self.assertRaises(AirframeGeneratorError) as ctx:
            compute_hover_throttle(0.01, rotors)
        self.assertEqual(ctx.exception.code, "HOVER_BELOW_MIN_VELOCITY")

    def test_pid_seeds_scale_inversely_with_arm_length(self) -> None:
        # Bigger drones need LOWER P gains (more inertia per torque).
        # We use sqrt-inverse scaling — gentler than 1/L because the
        # heuristic is approximate; PX4's auto-tune refines per drone.
        small = seed_pid_gains(mass_kg=1.0, arm_length_m=0.13, rotor_count=4)
        big = seed_pid_gains(mass_kg=4.0, arm_length_m=0.52, rotor_count=4)
        # Big drone has 4× the arm length; rate gains should be √4 = 2× lower.
        self.assertAlmostEqual(
            small["mc_rollrate_p"] / big["mc_rollrate_p"],
            2.0,
            places=3,
        )

    def test_pid_seeds_x500_match_reference(self) -> None:
        seeds = seed_pid_gains(
            mass_kg=2.0,
            arm_length_m=_X500_ARM_LENGTH_M,
            rotor_count=4,
        )
        self.assertAlmostEqual(seeds["mc_rollrate_p"], _X500_MC_ROLLRATE_P)


# ----------------------------------------------------------------------
# generate_airframe_params (integration)
# ----------------------------------------------------------------------


class TestGenerateAirframeParams(unittest.TestCase):
    def test_x500_clone_produces_close_to_reference_params(self) -> None:
        cfg = _x500_like_drone_config()
        params = generate_airframe_params(
            model_name="x500_clone",
            drone_config=cfg,
            mass_kg_override=2.0,
        )
        self.assertEqual(params.rotor_count, 4)
        self.assertAlmostEqual(params.arm_length_m, _X500_ARM_LENGTH_M, places=2)
        self.assertGreater(params.hover_throttle, 0.5)
        self.assertGreaterEqual(params.sys_autostart, _AUTOSTART_BASE)

    def test_uses_sim_model_mass_when_no_override(self) -> None:
        sim = _FakeSimModel(
            links=[
                _FakeLink("base", (0, 0, 0), mass_kg=1.5),
                _FakeLink("rotor_0", (130, 220, 0), mass_kg=0.05),
                _FakeLink("rotor_1", (-130, -200, 0), mass_kg=0.05),
                _FakeLink("rotor_2", (130, -220, 0), mass_kg=0.05),
                _FakeLink("rotor_3", (-130, 200, 0), mass_kg=0.05),
            ],
            joints=[
                _FakeJoint("rotor_0_joint", "base", "rotor_0"),
                _FakeJoint("rotor_1_joint", "base", "rotor_1"),
                _FakeJoint("rotor_2_joint", "base", "rotor_2"),
                _FakeJoint("rotor_3_joint", "base", "rotor_3"),
            ],
        )
        cfg = {
            "rotors": [
                {"joint": "rotor_0_joint", "direction": "ccw"},
                {"joint": "rotor_1_joint", "direction": "ccw"},
                {"joint": "rotor_2_joint", "direction": "cw"},
                {"joint": "rotor_3_joint", "direction": "cw"},
            ],
        }
        params = generate_airframe_params(
            model_name="auto_mass_drone",
            sim_model=sim,
            drone_config=cfg,
        )
        self.assertAlmostEqual(params.mass_kg, 1.7)  # 1.5 + 4*0.05

    def test_no_mass_source_raises(self) -> None:
        with self.assertRaises(AirframeGeneratorError) as cm:
            generate_airframe_params(
                model_name="no_mass",
                drone_config=_x500_like_drone_config(),
            )
        self.assertEqual(cm.exception.code, "NO_MASS_SOURCE")


# ----------------------------------------------------------------------
# Format
# ----------------------------------------------------------------------


class TestFormatAirframeInitScript(unittest.TestCase):
    def setUp(self) -> None:
        self.params = generate_airframe_params(
            model_name="test_quad",
            drone_config=_x500_like_drone_config(),
            mass_kg_override=2.0,
        )

    def test_starts_with_shebang_and_metadata(self) -> None:
        script = format_airframe_init_script(self.params)
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("@name SolidMind test_quad", script)
        self.assertIn("@type Multirotor", script)
        self.assertIn(f"SYS_AUTOSTART = {self.params.sys_autostart}", script)

    def test_sources_mc_defaults(self) -> None:
        script = format_airframe_init_script(self.params)
        self.assertIn(". ${R}etc/init.d/rc.mc_defaults", script)

    def test_includes_ca_rotor_params_for_each_rotor(self) -> None:
        script = format_airframe_init_script(self.params)
        for idx in range(4):
            self.assertIn(f"CA_ROTOR{idx}_PX", script)
            self.assertIn(f"CA_ROTOR{idx}_PY", script)
            self.assertIn(f"CA_ROTOR{idx}_KM", script)
        self.assertIn("CA_ROTOR_COUNT 4", script)
        self.assertIn("CA_AIRFRAME 0", script)  # 0 = multicopter

    def test_includes_sim_gz_motor_function_assignments(self) -> None:
        script = format_airframe_init_script(self.params)
        for idx in range(1, 5):
            self.assertIn(f"SIM_GZ_EC_FUNC{idx} {100 + idx}", script)
            self.assertIn(f"SIM_GZ_EC_MIN{idx} 150", script)
            self.assertIn(f"SIM_GZ_EC_MAX{idx} 1000", script)

    def test_includes_mpc_thr_hover(self) -> None:
        script = format_airframe_init_script(self.params)
        self.assertIn("MPC_THR_HOVER", script)
        # The exact hover value is geometry-derived; check it's present
        # as a numeric value formatted to 3 decimals.
        self.assertRegex(script, r"MPC_THR_HOVER 0\.\d{3}")

    def test_includes_pid_seeds(self) -> None:
        script = format_airframe_init_script(self.params)
        self.assertIn("MC_ROLLRATE_P", script)
        self.assertIn("MC_PITCHRATE_P", script)
        self.assertIn("MC_YAWRATE_P", script)

    def test_pz_omitted_for_zero_offset(self) -> None:
        # With pz_m = 0 (default), the script should not emit the line
        # to keep the file clean.
        script = format_airframe_init_script(self.params)
        for idx in range(4):
            self.assertNotIn(f"CA_ROTOR{idx}_PZ", script)

    def test_pz_emitted_for_nonzero_offset(self) -> None:
        # Single rotor + 1 kg pushes ω_hover just above 1000 rad/s with
        # default motor constants; use a tiny coaxial-class mass so the
        # quadratic-correct hover formula stays feasible.
        # FLU input pz=0.05 → FRD output pz=-0.05 (negated by extract_rotors).
        cfg = {
            "rotors": [
                {
                    "joint": "j",
                    "direction": "ccw",
                    "position_m": (0.1, 0.1, 0.05),  # non-zero PZ in FLU
                }
            ],
        }
        params = generate_airframe_params(
            model_name="lifted",
            drone_config=cfg,
            mass_kg_override=0.5,
        )
        script = format_airframe_init_script(params)
        self.assertIn("CA_ROTOR0_PZ -0.05", script)

    def test_km_sign_matches_direction(self) -> None:
        script = format_airframe_init_script(self.params)
        # X500 layout: rotors 0,3 ccw (+km), rotors 1,2 cw (-km).
        # Wait — actually our config has 0,1 ccw and 2,3 cw. Verify.
        self.assertRegex(script, r"CA_ROTOR0_KM\s+0\.05")  # ccw, positive
        self.assertRegex(script, r"CA_ROTOR2_KM\s+-0\.05")  # cw, negative

    def test_yaw_p_uses_sqrt_scaling(self) -> None:
        # Sanity: for X500-sized arm, yaw P ≈ 0.20.
        script = format_airframe_init_script(self.params)
        match = re.search(r"MC_YAWRATE_P\s+(\d+\.\d+)", script)
        self.assertIsNotNone(match)
        # X500 reference is 0.20; allow 5% tolerance on derived value.
        self.assertAlmostEqual(float(match.group(1)), 0.2, delta=0.02)


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


class TestRegisterAirframe(unittest.TestCase):
    def setUp(self) -> None:
        self.params = generate_airframe_params(
            model_name="reg_test_drone",
            drone_config=_x500_like_drone_config(),
            mass_kg_override=2.0,
        )
        self.tmp = tempfile.TemporaryDirectory()
        # Mimic the PX4 install layout that register_airframe expects.
        self.airframes_dir = (
            Path(self.tmp.name) / "ROMFS" / "px4fmu_common" / "init.d-posix" / "airframes"
        )
        self.airframes_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_writes_file_with_canonical_name(self) -> None:
        path = register_airframe(self.params, install_path=self.tmp.name)
        self.assertEqual(path.parent, self.airframes_dir)
        # Filename: <sys_autostart>_<sanitized_name>
        expected = airframe_filename(self.params)
        self.assertEqual(path.name, expected)
        self.assertTrue(path.exists())

    def test_written_file_is_executable(self) -> None:
        path = register_airframe(self.params, install_path=self.tmp.name)
        # Owner execute bit must be set so PX4 can run it on boot.
        self.assertTrue(path.stat().st_mode & 0o100)

    def test_written_file_contents_match_format(self) -> None:
        path = register_airframe(self.params, install_path=self.tmp.name)
        content = path.read_text()
        self.assertIn(f"SYS_AUTOSTART = {self.params.sys_autostart}", content)
        self.assertIn("CA_ROTOR_COUNT 4", content)

    def test_overwrite_default_replaces_existing(self) -> None:
        register_airframe(self.params, install_path=self.tmp.name)
        # Second call with same params — should not raise.
        path = register_airframe(self.params, install_path=self.tmp.name)
        self.assertTrue(path.exists())

    def test_overwrite_false_raises_when_file_exists(self) -> None:
        register_airframe(self.params, install_path=self.tmp.name)
        with self.assertRaises(AirframeGeneratorError) as cm:
            register_airframe(
                self.params,
                install_path=self.tmp.name,
                overwrite=False,
            )
        self.assertEqual(cm.exception.code, "AIRFRAME_EXISTS")

    def test_missing_install_path_raises(self) -> None:
        with self.assertRaises(AirframeGeneratorError) as cm:
            register_airframe(self.params, install_path="/no/such/path")
        self.assertEqual(cm.exception.code, "PX4_INSTALL_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
