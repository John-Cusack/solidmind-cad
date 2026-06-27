"""Tier 1: Full pipeline through real GazeboBridgeServer + StubGazeboRuntime.

No mocks — exercises real bridge code to catch protocol drift.
"""
from __future__ import annotations

import json
import socket
import unittest

from tests.conftest import GazeboStubBridge, mechanism_factory, unused_tcp_port


def _send_command(host: str, port: int, cmd: str, args: dict | None = None) -> dict:
    """Send a single command to the bridge and return the parsed response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.connect((host, port))
    try:
        msg = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
        sock.sendall(msg.encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode().strip())
    finally:
        sock.close()


class TestPingThroughRealBridge(unittest.TestCase):
    """Client -> TCP -> bridge_server dispatch -> runtime.handle_ping()."""

    def test_ping(self):
        port = unused_tcp_port()
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "ping")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["pong"])


class TestSimulateReturnTimeSeries(unittest.TestCase):
    """motion_simulate path through real bridge."""

    def test_simulate_returns_time_series(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("time_series", result)
        self.assertIn("summary", result)
        ts = result["time_series"]
        self.assertGreater(len(ts), 0)
        # Every entry has 't'
        for entry in ts:
            self.assertIn("t", entry)
            self.assertIsInstance(entry["t"], (int, float))
            self.assertGreaterEqual(entry["t"], 0)
        # Summary has simulation_time_s
        self.assertIn("simulation_time_s", result["summary"])
        # Joint efforts present since mechanism has joints
        self.assertIn("joint_efforts", ts[-1])
        # Peak joint forces in summary
        self.assertIn("peak_joint_forces", result["summary"])


class TestTeleopFullLifecycle(unittest.TestCase):
    """teleop_start -> teleop_command (3 ticks) -> teleop_state -> teleop_stop."""

    def test_teleop_lifecycle(self):
        port = unused_tcp_port()
        with GazeboStubBridge(port) as bridge:
            # Start
            start_resp = _send_command(bridge.host, bridge.port, "teleop_start", {
                "profile": {"controller_type": "multirotor_direct"},
            })
            self.assertTrue(start_resp["ok"], start_resp)
            session_id = start_resp["result"]["session_id"]
            self.assertIsInstance(session_id, str)
            self.assertEqual(start_resp["result"]["status"], "started")

            # 3 command ticks
            for i in range(3):
                cmd_resp = _send_command(bridge.host, bridge.port, "teleop_command", {
                    "session_id": session_id,
                    "vx_mps": 1.0,
                    "dt_s": 0.02,
                })
                self.assertTrue(cmd_resp["ok"], cmd_resp)
                self.assertTrue(cmd_resp["result"]["applied"])
                self.assertEqual(cmd_resp["result"]["tick_count"], i + 1)

            # State
            state_resp = _send_command(bridge.host, bridge.port, "teleop_state", {
                "session_id": session_id,
            })
            self.assertTrue(state_resp["ok"], state_resp)
            self.assertIn("state", state_resp["result"])

            # Stop
            stop_resp = _send_command(bridge.host, bridge.port, "teleop_stop", {
                "session_id": session_id,
            })
            self.assertTrue(stop_resp["ok"], stop_resp)
            self.assertTrue(stop_resp["result"]["stopped"])
            self.assertEqual(stop_resp["result"]["tick_count"], 3)


class TestSpawnModel(unittest.TestCase):
    """spawn_model with dummy path through stub."""

    def test_spawn_model_with_existing_path(self):
        import os
        import tempfile

        port = unused_tcp_port()
        # Create a temp SDF file
        with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as f:
            f.write(b"<sdf></sdf>")
            sdf_path = f.name

        try:
            with GazeboStubBridge(port) as bridge:
                resp = _send_command(bridge.host, bridge.port, "spawn_model", {
                    "sdf_path": sdf_path,
                    "model_name": "test_model",
                })
            self.assertTrue(resp["ok"], resp)
            self.assertTrue(resp["result"]["spawned"])
            self.assertIsInstance(resp["result"]["entity_id"], int)
            self.assertEqual(resp["result"]["model_name"], "test_model")
        finally:
            os.unlink(sdf_path)


class TestDiagnoseReturnsStubMode(unittest.TestCase):
    """diagnose -> response includes runtime_mode: 'stub'."""

    def test_diagnose(self):
        port = unused_tcp_port()
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "diagnose", {})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["runtime_mode"], "stub")
        self.assertTrue(resp["result"]["connected"])


class TestConcurrentSessions(unittest.TestCase):
    """Two teleop sessions on same bridge, independent state."""

    def test_concurrent_sessions(self):
        port = unused_tcp_port()
        with GazeboStubBridge(port) as bridge:
            # Start two sessions
            s1 = _send_command(bridge.host, bridge.port, "teleop_start", {
                "profile": {"controller_type": "multirotor_direct"},
            })
            s2 = _send_command(bridge.host, bridge.port, "teleop_start", {
                "profile": {"controller_type": "multirotor_direct"},
            })
            self.assertTrue(s1["ok"])
            self.assertTrue(s2["ok"])
            sid1 = s1["result"]["session_id"]
            sid2 = s2["result"]["session_id"]
            self.assertNotEqual(sid1, sid2)

            # Command only session 1
            _send_command(bridge.host, bridge.port, "teleop_command", {
                "session_id": sid1,
                "vx_mps": 2.0,
                "dt_s": 0.1,
            })

            # Session 2 should still be at tick 0
            state2 = _send_command(bridge.host, bridge.port, "teleop_state", {
                "session_id": sid2,
            })
            self.assertTrue(state2["ok"])
            self.assertEqual(state2["result"]["tick_count"], 0)

            # Session 1 should be at tick 1
            state1 = _send_command(bridge.host, bridge.port, "teleop_state", {
                "session_id": sid1,
            })
            self.assertTrue(state1["ok"])
            self.assertEqual(state1["result"]["tick_count"], 1)

            # Cleanup
            _send_command(bridge.host, bridge.port, "teleop_stop", {"session_id": sid1})
            _send_command(bridge.host, bridge.port, "teleop_stop", {"session_id": sid2})


if __name__ == "__main__":
    unittest.main()
