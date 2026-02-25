"""Tests for Isaac bridge protocol parsing and command dispatch."""
from __future__ import annotations

import json
import unittest

from isaac_bridge.bridge_server import BridgeServer
from isaac_bridge.protocol import ProtocolError, parse_request_line


def _supported_mechanism() -> dict:
    return {
        "name": "bridge_test",
        "parts": [
            {"id": "frame", "is_ground": True},
            {"id": "link_a"},
        ],
        "joints": [
            {
                "id": "joint_a",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "link_a",
            },
        ],
        "drives": [{"joint_id": "joint_a", "speed_rpm": 120.0}],
    }


def _unsupported_mechanism() -> dict:
    mechanism = _supported_mechanism()
    mechanism["joints"] = [
        {
            "id": "mesh_1",
            "joint_type": "gear_mesh",
            "parent_part": "frame",
            "child_part": "link_a",
        }
    ]
    return mechanism


class TestProtocolParsing(unittest.TestCase):
    def test_parse_request_line_success(self) -> None:
        cmd, args = parse_request_line(b'{"cmd":"ping","args":{}}\n')
        self.assertEqual(cmd, "ping")
        self.assertEqual(args, {})

    def test_parse_request_line_invalid_json(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request_line(b"{bad json}\n")
        self.assertEqual(ctx.exception.code, "INVALID_JSON")

    def test_parse_request_line_missing_cmd(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request_line(b'{"args":{}}\n')
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")


class TestBridgeDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_ping(self) -> None:
        result = self._call('{"cmd":"ping","args":{}}')
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["pong"])
        self.assertIn("import_urdf", result["result"]["capabilities"]["commands"])

    def test_unknown_command(self) -> None:
        result = self._call('{"cmd":"nope","args":{}}')
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_COMMAND")

    def test_unsupported_joint_type(self) -> None:
        result = self._call(
            json.dumps(
                {
                    "cmd": "simulate",
                    "args": {
                        "mechanism": _unsupported_mechanism(),
                        "duration_s": 0.2,
                        "dt_s": 0.01,
                        "output_interval": 0.05,
                    },
                }
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_JOINT_TYPE")
        self.assertIn("unsupported_joints", result["error"]["details"])

    def test_teleop_lifecycle(self) -> None:
        started = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {"mechanism": _supported_mechanism(), "profile": {"speed": 1}},
        }))
        self.assertTrue(started["ok"])
        session_id = started["result"]["session_id"]

        applied = self._call(json.dumps({
            "cmd": "teleop_command",
            "args": {
                "session_id": session_id,
                "vx_mps": 0.3,
                "yaw_rate_rps": 0.1,
                "body_height_m": 0.02,
            },
        }))
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["result"]["applied"])

        state = self._call(json.dumps({"cmd": "teleop_state", "args": {"session_id": session_id}}))
        self.assertTrue(state["ok"])
        self.assertAlmostEqual(state["result"]["state"]["vx_mps"], 0.3)

        stopped = self._call(json.dumps({"cmd": "teleop_stop", "args": {"session_id": session_id}}))
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["result"]["stopped"])

        missing = self._call(json.dumps({"cmd": "teleop_state", "args": {"session_id": session_id}}))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "ISAAC_UNKNOWN_SESSION")


class TestDiagnoseCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_diagnose_without_isaac(self) -> None:
        """diagnose returns an error when Isaac is not available."""
        result = self._call(json.dumps({"cmd": "diagnose", "args": {}}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ISAAC_NOT_AVAILABLE")

    def test_diagnose_with_prim_path(self) -> None:
        """diagnose accepts a prim_path argument."""
        result = self._call(json.dumps({
            "cmd": "diagnose",
            "args": {"prim_path": "/some/path"},
        }))
        # Should fail (no Isaac) but with ISAAC_NOT_AVAILABLE, not INVALID_ARGS
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ISAAC_NOT_AVAILABLE")


class TestReloadCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_reload_returns_ok(self) -> None:
        """reload succeeds even without Isaac (reloads module, recreates runtime)."""
        result = self._call(json.dumps({"cmd": "reload", "args": {}}))
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["reloaded"])
        self.assertIn("isaac_available", result["result"])

    def test_reload_preserves_ping(self) -> None:
        """After reload, ping still works with the new runtime."""
        self._call(json.dumps({"cmd": "reload", "args": {}}))
        result = self._call('{"cmd":"ping","args":{}}')
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["pong"])

    def test_ping_includes_new_commands_diagnose_reload(self) -> None:
        """ping capabilities list includes diagnose and reload."""
        result = self._call('{"cmd":"ping","args":{}}')
        self.assertTrue(result["ok"])
        commands = result["result"]["capabilities"]["commands"]
        self.assertIn("diagnose", commands)
        self.assertIn("reload", commands)


class TestImportURDFCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_import_urdf_missing_path(self) -> None:
        result = self._call(json.dumps({"cmd": "import_urdf", "args": {}}))
        self.assertFalse(result["ok"])

    def test_import_urdf_file_not_found(self) -> None:
        result = self._call(json.dumps({
            "cmd": "import_urdf",
            "args": {"urdf_path": "/nonexistent/robot.urdf"},
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")

    def test_import_urdf_empty_string(self) -> None:
        result = self._call(json.dumps({
            "cmd": "import_urdf",
            "args": {"urdf_path": ""},
        }))
        self.assertFalse(result["ok"])

    def test_import_urdf_non_string_type(self) -> None:
        result = self._call(json.dumps({
            "cmd": "import_urdf",
            "args": {"urdf_path": 42},
        }))
        self.assertFalse(result["ok"])

    def test_simulate_with_urdf_path_not_found(self) -> None:
        result = self._call(json.dumps({
            "cmd": "simulate",
            "args": {
                "mechanism": _supported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
                "urdf_path": "/nonexistent/robot.urdf",
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")

    def test_simulate_without_urdf_still_works(self) -> None:
        """Backwards compatibility: simulate without urdf_path works as before."""
        result = self._call(json.dumps({
            "cmd": "simulate",
            "args": {
                "mechanism": _supported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertTrue(result["ok"])
        self.assertIn("time_series", result["result"])

    def test_teleop_start_with_urdf_path_not_found(self) -> None:
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {
                "mechanism": _supported_mechanism(),
                "urdf_path": "/nonexistent/robot.urdf",
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")

    def test_teleop_start_without_urdf_still_works(self) -> None:
        """Backwards compatibility: teleop_start without urdf_path works as before."""
        result = self._call(json.dumps({
            "cmd": "teleop_start",
            "args": {"mechanism": _supported_mechanism()},
        }))
        self.assertTrue(result["ok"])
        self.assertIn("session_id", result["result"])


class TestSimulateSessionLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_simulate_start_returns_session_id(self) -> None:
        result = self._call(json.dumps({
            "cmd": "simulate_start",
            "args": {
                "mechanism": _supported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertTrue(result["ok"])
        self.assertIn("session_id", result["result"])
        self.assertIn("status", result["result"])
        self.assertIn("steady_state_speeds", result["result"])

    def test_simulate_lifecycle(self) -> None:
        # Start
        started = self._call(json.dumps({
            "cmd": "simulate_start",
            "args": {
                "mechanism": _supported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertTrue(started["ok"])
        session_id = started["result"]["session_id"]

        # Status
        status = self._call(json.dumps({
            "cmd": "simulate_status",
            "args": {"session_id": session_id},
        }))
        self.assertTrue(status["ok"])
        self.assertIn("status", status["result"])
        self.assertIn("completed_steps", status["result"])

        # Stop
        stopped = self._call(json.dumps({
            "cmd": "simulate_stop",
            "args": {"session_id": session_id},
        }))
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["result"]["stopped"])
        self.assertIn("samples", stopped["result"])

    def test_simulate_status_unknown_session(self) -> None:
        result = self._call(json.dumps({
            "cmd": "simulate_status",
            "args": {"session_id": "nonexistent"},
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ISAAC_UNKNOWN_SESSION")

    def test_simulate_stop_unknown_session_idempotent(self) -> None:
        result = self._call(json.dumps({
            "cmd": "simulate_stop",
            "args": {"session_id": "nonexistent"},
        }))
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["already_stopped"])

    def test_simulate_start_unsupported_joint(self) -> None:
        result = self._call(json.dumps({
            "cmd": "simulate_start",
            "args": {
                "mechanism": _unsupported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_JOINT_TYPE")

    def test_backward_compat_simulate_still_works(self) -> None:
        """The old simulate command still returns time_series as before."""
        result = self._call(json.dumps({
            "cmd": "simulate",
            "args": {
                "mechanism": _supported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertTrue(result["ok"])
        self.assertIn("time_series", result["result"])
        self.assertIn("summary", result["result"])

    def test_ping_includes_new_commands(self) -> None:
        result = self._call('{"cmd":"ping","args":{}}')
        self.assertTrue(result["ok"])
        commands = result["result"]["capabilities"]["commands"]
        self.assertIn("simulate_start", commands)
        self.assertIn("simulate_status", commands)
        self.assertIn("simulate_stop", commands)


class TestScreenshotCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_screenshot_without_isaac(self) -> None:
        """screenshot returns an error when Isaac is not available."""
        result = self._call(json.dumps({"cmd": "screenshot", "args": {}}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ISAAC_NOT_AVAILABLE")

    def test_screenshot_with_custom_size(self) -> None:
        """screenshot accepts width/height arguments (still fails without Isaac)."""
        result = self._call(json.dumps({
            "cmd": "screenshot",
            "args": {"width": 640, "height": 480},
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ISAAC_NOT_AVAILABLE")

    def test_screenshot_with_camera(self) -> None:
        """screenshot accepts camera_position and camera_target."""
        result = self._call(json.dumps({
            "cmd": "screenshot",
            "args": {
                "camera_position": [2.0, 2.0, 1.0],
                "camera_target": [0.0, 0.0, 0.0],
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ISAAC_NOT_AVAILABLE")

    def test_ping_includes_screenshot(self) -> None:
        """ping capabilities list includes screenshot."""
        result = self._call('{"cmd":"ping","args":{}}')
        self.assertTrue(result["ok"])
        commands = result["result"]["capabilities"]["commands"]
        self.assertIn("screenshot", commands)


class TestMobileRobotDefaults(unittest.TestCase):
    """Milestone 1: robot_type='mobile' applies research-validated defaults."""

    def test_mobile_defaults_applied(self) -> None:
        from isaac_bridge.models import URDFImportConfig
        cfg = URDFImportConfig.from_dict({"robot_type": "mobile"})
        self.assertFalse(cfg.fix_base)
        self.assertTrue(cfg.merge_fixed_joints)
        self.assertAlmostEqual(cfg.default_drive_stiffness, 400.0)
        self.assertAlmostEqual(cfg.default_drive_damping, 30.0)
        self.assertEqual(cfg.robot_type, "mobile")

    def test_mobile_explicit_override_wins(self) -> None:
        from isaac_bridge.models import URDFImportConfig
        cfg = URDFImportConfig.from_dict({
            "robot_type": "mobile",
            "fix_base": True,
            "default_drive_stiffness": 50.0,
        })
        # Explicit overrides must win over mobile defaults
        self.assertTrue(cfg.fix_base)
        self.assertAlmostEqual(cfg.default_drive_stiffness, 50.0)
        # Non-overridden mobile defaults still apply
        self.assertTrue(cfg.merge_fixed_joints)
        self.assertAlmostEqual(cfg.default_drive_damping, 30.0)

    def test_manipulator_defaults_unchanged(self) -> None:
        from isaac_bridge.models import URDFImportConfig
        cfg = URDFImportConfig.from_dict({"robot_type": "manipulator"})
        self.assertTrue(cfg.fix_base)
        self.assertFalse(cfg.merge_fixed_joints)
        self.assertAlmostEqual(cfg.default_drive_stiffness, 1000.0)
        self.assertAlmostEqual(cfg.default_drive_damping, 100.0)

    def test_default_is_manipulator(self) -> None:
        from isaac_bridge.models import URDFImportConfig
        cfg = URDFImportConfig.from_dict({})
        self.assertEqual(cfg.robot_type, "manipulator")
        self.assertTrue(cfg.fix_base)

    def test_none_dict_returns_manipulator(self) -> None:
        from isaac_bridge.models import URDFImportConfig
        cfg = URDFImportConfig.from_dict(None)
        self.assertEqual(cfg.robot_type, "manipulator")
        self.assertTrue(cfg.fix_base)


class TestURDFOnlySimulation(unittest.TestCase):
    """Milestone 3: simulate without mechanism (URDF-only)."""

    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_simulate_with_urdf_only_no_mechanism(self) -> None:
        """simulate with urdf_path but no mechanism synthesizes a minimal mech."""
        result = self._call(json.dumps({
            "cmd": "simulate",
            "args": {
                "urdf_path": "/nonexistent/robot.urdf",
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        # Should fail with URDF_NOT_FOUND (not INVALID_ARGS about mechanism)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")

    def test_simulate_start_with_urdf_only_no_mechanism(self) -> None:
        """simulate_start with urdf_path but no mechanism synthesizes a minimal mech."""
        result = self._call(json.dumps({
            "cmd": "simulate_start",
            "args": {
                "urdf_path": "/nonexistent/robot.urdf",
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")

    def test_simulate_no_mechanism_no_urdf_errors(self) -> None:
        """simulate with neither mechanism nor urdf_path gives INVALID_INPUT."""
        result = self._call(json.dumps({
            "cmd": "simulate",
            "args": {
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_INPUT", result["error"]["code"])

    def test_backward_compat_mechanism_still_works(self) -> None:
        """simulate with mechanism but no urdf_path still works (reference mode)."""
        result = self._call(json.dumps({
            "cmd": "simulate",
            "args": {
                "mechanism": _supported_mechanism(),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        }))
        self.assertTrue(result["ok"])
        self.assertIn("time_series", result["result"])


if __name__ == "__main__":
    unittest.main()
