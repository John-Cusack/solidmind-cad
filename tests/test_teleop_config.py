"""Tests for TeleopConfig validation and Controller protocol (P0)."""
from __future__ import annotations

import json
import unittest

from isaac_bridge.models import (
    Controller,
    SimulationSession,
    TeleopConfig,
    TeleopConfigError,
    TeleopState,
)


class TestTeleopConfigDefaults(unittest.TestCase):
    """TeleopConfig defaults match the backlog specification."""

    def test_default_construction(self) -> None:
        cfg = TeleopConfig()
        self.assertEqual(cfg.controller_type, "hexapod_1dof_tripod")
        self.assertEqual(len(cfg.joint_names), 6)
        self.assertAlmostEqual(cfg.amplitude_deg, 18.0)
        self.assertAlmostEqual(cfg.stride_hz, 2.0)
        self.assertAlmostEqual(cfg.vx_max_mps, 0.3)
        self.assertAlmostEqual(cfg.yaw_max_rps, 1.0)

    def test_from_profile_none(self) -> None:
        cfg = TeleopConfig.from_profile(None)
        self.assertEqual(cfg.controller_type, "hexapod_1dof_tripod")
        self.assertEqual(cfg.joint_names, (
            "hip_lf", "hip_lm", "hip_lr", "hip_rf", "hip_rm", "hip_rr",
        ))

    def test_from_profile_empty_dict(self) -> None:
        cfg = TeleopConfig.from_profile({})
        self.assertEqual(cfg, TeleopConfig())

    def test_from_profile_preserves_unknown_keys(self) -> None:
        """Unknown keys in profile are silently ignored."""
        cfg = TeleopConfig.from_profile({"speed": 1, "unknown_key": "val"})
        self.assertEqual(cfg.controller_type, "hexapod_1dof_tripod")


class TestTeleopConfigOverrides(unittest.TestCase):
    """Profile values override defaults."""

    def test_override_amplitude(self) -> None:
        cfg = TeleopConfig.from_profile({"amplitude_deg": 25.0})
        self.assertAlmostEqual(cfg.amplitude_deg, 25.0)

    def test_override_stride_hz(self) -> None:
        cfg = TeleopConfig.from_profile({"stride_hz": 2.0})
        self.assertAlmostEqual(cfg.stride_hz, 2.0)

    def test_override_controller_type(self) -> None:
        cfg = TeleopConfig.from_profile({"controller_type": "wheeled_diff"})
        self.assertEqual(cfg.controller_type, "wheeled_diff")

    def test_override_joint_names_and_tripods(self) -> None:
        cfg = TeleopConfig.from_profile({
            "joint_names": ["j1", "j2", "j3", "j4"],
            "tripod_a": ["j1", "j3"],
            "tripod_b": ["j2", "j4"],
        })
        self.assertEqual(cfg.joint_names, ("j1", "j2", "j3", "j4"))
        self.assertEqual(cfg.tripod_a, ("j1", "j3"))
        self.assertEqual(cfg.tripod_b, ("j2", "j4"))

    def test_override_slew_rates(self) -> None:
        cfg = TeleopConfig.from_profile({
            "slew_vx_mps2": 3.0,
            "slew_yaw_rps2": 5.0,
            "slew_height_mps2": 0.1,
        })
        self.assertAlmostEqual(cfg.slew_vx_mps2, 3.0)
        self.assertAlmostEqual(cfg.slew_yaw_rps2, 5.0)
        self.assertAlmostEqual(cfg.slew_height_mps2, 0.1)


class TestTeleopConfigValidation(unittest.TestCase):
    """Invalid profile values raise TeleopConfigError."""

    def test_negative_amplitude(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"amplitude_deg": -1.0})

    def test_zero_stride_hz(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"stride_hz": 0.0})

    def test_nan_vx_max(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"vx_max_mps": float("nan")})

    def test_inf_yaw_max(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"yaw_max_rps": float("inf")})

    def test_string_amplitude(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"amplitude_deg": "fast"})

    def test_empty_controller_type(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"controller_type": ""})

    def test_non_string_controller_type(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"controller_type": 42})

    def test_joint_names_not_list(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"joint_names": "hip_lf"})

    def test_joint_names_empty(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"joint_names": []})

    def test_joint_names_non_string_elements(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"joint_names": [1, 2, 3]})

    def test_neutral_deg_allows_negative(self) -> None:
        """neutral_deg is the only numeric that can be negative."""
        cfg = TeleopConfig.from_profile({"neutral_deg": -5.0})
        self.assertAlmostEqual(cfg.neutral_deg, -5.0)

    def test_yaw_mix_deg_allows_zero(self) -> None:
        """yaw_mix_deg is non-negative, so zero is valid."""
        cfg = TeleopConfig.from_profile({"yaw_mix_deg": 0.0})
        self.assertAlmostEqual(cfg.yaw_mix_deg, 0.0)

    def test_negative_yaw_mix(self) -> None:
        with self.assertRaises(TeleopConfigError):
            TeleopConfig.from_profile({"yaw_mix_deg": -1.0})


class TestTripodConsistency(unittest.TestCase):
    """Tripod set-consistency checks."""

    def test_overlapping_tripods(self) -> None:
        with self.assertRaises(TeleopConfigError) as ctx:
            TeleopConfig.from_profile({
                "joint_names": ["j1", "j2", "j3", "j4"],
                "tripod_a": ["j1", "j2"],
                "tripod_b": ["j2", "j3"],
            })
        self.assertIn("overlap", ctx.exception.message)

    def test_tripods_missing_joint(self) -> None:
        with self.assertRaises(TeleopConfigError) as ctx:
            TeleopConfig.from_profile({
                "joint_names": ["j1", "j2", "j3", "j4"],
                "tripod_a": ["j1"],
                "tripod_b": ["j2"],
            })
        self.assertIn("missing from tripods", ctx.exception.message)

    def test_tripods_extra_joint(self) -> None:
        with self.assertRaises(TeleopConfigError) as ctx:
            TeleopConfig.from_profile({
                "joint_names": ["j1", "j2"],
                "tripod_a": ["j1", "j3"],
                "tripod_b": ["j2"],
            })
        self.assertIn("not in joint_names", ctx.exception.message)

    def test_single_joint_rejected(self) -> None:
        with self.assertRaises(TeleopConfigError) as ctx:
            TeleopConfig.from_profile({
                "joint_names": ["j1"],
                "tripod_a": ["j1"],
                "tripod_b": [],
            })
        # Empty tripod_b triggers "must not be empty"
        self.assertIn("must not be empty", ctx.exception.message)

    def test_default_tripods_are_consistent(self) -> None:
        """Default config passes consistency checks."""
        cfg = TeleopConfig()
        joints = set(cfg.joint_names)
        self.assertEqual(set(cfg.tripod_a) | set(cfg.tripod_b), joints)
        self.assertEqual(set(cfg.tripod_a) & set(cfg.tripod_b), set())


class TestTeleopConfigToDict(unittest.TestCase):
    def test_round_trip(self) -> None:
        cfg = TeleopConfig()
        d = cfg.to_dict()
        cfg2 = TeleopConfig.from_profile(d)
        self.assertEqual(cfg, cfg2)

    def test_json_serializable(self) -> None:
        cfg = TeleopConfig()
        s = json.dumps(cfg.to_dict())
        self.assertIsInstance(s, str)


class TestControllerProtocol(unittest.TestCase):
    """Controller protocol can be implemented."""

    def test_protocol_is_runtime_checkable(self) -> None:
        class DummyController:
            def compute_targets(
                self,
                state: TeleopState,
                dt_s: float,
                config: TeleopConfig,
                phase: float,
            ) -> tuple[dict[str, float], float]:
                return {}, 0.0

        ctrl = DummyController()
        self.assertIsInstance(ctrl, Controller)


class TestSimulationSessionTeleopFields(unittest.TestCase):
    """SimulationSession teleop fields are properly initialized."""

    def test_teleop_session_has_config(self) -> None:
        cfg = TeleopConfig()
        session = SimulationSession(
            session_id="test",
            session_type="teleop",
            mechanism={},
            profile={},
            started_at_s=0.0,
            teleop_config=cfg,
        )
        self.assertIsNotNone(session.teleop_config)
        self.assertEqual(session.tick_count, 0)
        self.assertEqual(session.limit_clamp_count, 0)
        self.assertTrue(session.last_apply_ok)
        self.assertEqual(session.dof_index_map, {})
        self.assertEqual(session.last_joint_targets_rad, {})
        self.assertAlmostEqual(session.gait_phase, 0.0)

    def test_simulate_session_no_teleop_fields(self) -> None:
        session = SimulationSession(
            session_id="test",
            session_type="simulate",
            mechanism={},
            profile={},
            started_at_s=0.0,
        )
        self.assertIsNone(session.teleop_config)

    def test_summary_includes_teleop_telemetry(self) -> None:
        cfg = TeleopConfig()
        session = SimulationSession(
            session_id="test",
            session_type="teleop",
            mechanism={},
            profile={},
            started_at_s=0.0,
            teleop_config=cfg,
        )
        session.tick_count = 42
        session.limit_clamp_count = 3
        s = session.summary()
        self.assertEqual(s["controller_type"], "hexapod_1dof_tripod")
        self.assertEqual(s["tick_count"], 42)
        self.assertEqual(s["limit_clamp_count"], 3)

    def test_summary_simulate_no_teleop_keys(self) -> None:
        session = SimulationSession(
            session_id="test",
            session_type="simulate",
            mechanism={},
            profile={},
            started_at_s=0.0,
        )
        s = session.summary()
        self.assertNotIn("controller_type", s)
        self.assertNotIn("tick_count", s)


class TestBridgeTeleopConfigIntegration(unittest.TestCase):
    """Integration: teleop_start through the bridge dispatches config validation."""

    def setUp(self) -> None:
        from isaac_bridge.bridge_server import BridgeServer
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def _mechanism(self) -> dict:
        return {
            "name": "test",
            "parts": [{"id": "frame", "is_ground": True}, {"id": "link"}],
            "joints": [{"id": "j", "joint_type": "revolute", "parent_part": "frame", "child_part": "link"}],
            "drives": [],
        }

    def test_teleop_start_default_profile(self) -> None:
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {"mechanism": self._mechanism()},
        }))
        self.assertTrue(result["ok"])
        self.assertIn("session_id", result["result"])
        self.assertEqual(result["result"]["controller_type"], "hexapod_1dof_tripod")
        # profile_used now contains full resolved config
        self.assertIn("amplitude_deg", result["result"]["profile_used"])

    def test_teleop_start_custom_profile(self) -> None:
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {
                "mechanism": self._mechanism(),
                "profile": {"amplitude_deg": 25.0, "stride_hz": 2.0},
            },
        }))
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["result"]["profile_used"]["amplitude_deg"], 25.0)
        self.assertAlmostEqual(result["result"]["profile_used"]["stride_hz"], 2.0)

    def test_teleop_start_invalid_profile_returns_error(self) -> None:
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {
                "mechanism": self._mechanism(),
                "profile": {"amplitude_deg": -5.0},
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_teleop_start_invalid_tripod_returns_error(self) -> None:
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {
                "mechanism": self._mechanism(),
                "profile": {
                    "joint_names": ["j1", "j2"],
                    "tripod_a": ["j1", "j2"],
                    "tripod_b": ["j1"],
                },
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_teleop_start_old_profile_still_works(self) -> None:
        """Backward compat: old-style profile with arbitrary keys doesn't break."""
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {
                "mechanism": self._mechanism(),
                "profile": {"speed": 1, "custom_key": "value"},
            },
        }))
        self.assertTrue(result["ok"])

    def test_teleop_state_includes_telemetry(self) -> None:
        started = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {"mechanism": self._mechanism()},
        }))
        session_id = started["result"]["session_id"]
        state = self._call(json.dumps({
            "cmd": "teleop_state",
            "args": {"session_id": session_id},
        }))
        self.assertTrue(state["ok"])
        r = state["result"]
        self.assertIn("state", r)
        self.assertIn("controller_type", r)
        self.assertIn("joint_names", r)
        self.assertIn("tick_count", r)
        self.assertIn("limit_clamp_count", r)
        self.assertIn("last_apply_ok", r)
        self.assertEqual(r["tick_count"], 0)


if __name__ == "__main__":
    unittest.main()
