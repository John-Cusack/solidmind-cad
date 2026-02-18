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


if __name__ == "__main__":
    unittest.main()
