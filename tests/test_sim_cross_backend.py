"""Tier 3: Cross-backend response contract validation.

Verify all backends produce responses matching canonical schemas.
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


# Expected response shapes
SIMULATE_RESULT_REQUIRED_KEYS = {"time_series", "summary"}
SUMMARY_REQUIRED_KEYS = {"simulation_time_s", "dt_s", "engine_mode"}
TELEOP_START_REQUIRED_KEYS = {"session_id", "status", "controller_type"}


class TestSimulateResponseShapeGazeboStub(unittest.TestCase):
    """Gazebo stub response has all required keys, correct types."""

    def test_simulate_response_shape(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 1.0,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        for key in SIMULATE_RESULT_REQUIRED_KEYS:
            self.assertIn(key, result, f"Missing key: {key}")
        self.assertIsInstance(result["time_series"], list)
        self.assertIsInstance(result["summary"], dict)
        for key in SUMMARY_REQUIRED_KEYS:
            self.assertIn(key, result["summary"], f"Missing summary key: {key}")


class TestTimeSeriesEntriesHaveT(unittest.TestCase):
    """Every entry in time_series has float t >= 0."""

    def test_time_series_t_values(self):
        port = unused_tcp_port()
        mech = mechanism_factory("four_bar")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.05,
            })
        self.assertTrue(resp["ok"], resp)
        ts = resp["result"]["time_series"]
        self.assertGreater(len(ts), 0)
        prev_t = -1.0
        for entry in ts:
            self.assertIn("t", entry)
            t = entry["t"]
            self.assertIsInstance(t, (int, float))
            self.assertGreaterEqual(t, 0.0)
            self.assertGreaterEqual(t, prev_t)  # monotonically increasing
            prev_t = t


class TestSummaryHasSimulationTime(unittest.TestCase):
    """summary.simulation_time_s is a positive float."""

    def test_summary_simulation_time(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 2.0,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"], resp)
        sim_time = resp["result"]["summary"]["simulation_time_s"]
        self.assertIsInstance(sim_time, (int, float))
        self.assertGreater(sim_time, 0.0)


class TestTeleopStartShape(unittest.TestCase):
    """teleop_start response has session_id (str) + status."""

    def test_teleop_start_shape(self):
        port = unused_tcp_port()
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "teleop_start", {
                "profile": {"controller_type": "multirotor_direct"},
            })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        for key in TELEOP_START_REQUIRED_KEYS:
            self.assertIn(key, result, f"Missing key: {key}")
        self.assertIsInstance(result["session_id"], str)
        self.assertIsInstance(result["status"], str)
        self.assertIsInstance(result["controller_type"], str)


class TestPeakJointForcesWhenJointsExist(unittest.TestCase):
    """If mechanism has joints, summary.peak_joint_forces is a dict."""

    def test_peak_joint_forces(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 1.0,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"], resp)
        summary = resp["result"]["summary"]
        self.assertIn("peak_joint_forces", summary)
        pjf = summary["peak_joint_forces"]
        self.assertIsInstance(pjf, dict)
        # Should have entries for the joints
        self.assertGreater(len(pjf), 0)
        # All values should be numeric
        for _k, v in pjf.items():
            self.assertIsInstance(v, (int, float))
            self.assertGreaterEqual(v, 0.0)


class TestSimulateWithDifferentMechanisms(unittest.TestCase):
    """Verify simulate works across different mechanism types."""

    def _run_simulate(self, kind: str) -> dict:
        port = unused_tcp_port()
        mech = mechanism_factory(kind)
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.2,
                "dt_s": 0.01,
                "output_interval": 0.05,
            })
        self.assertTrue(resp["ok"], resp)
        return resp["result"]

    def test_gear_pair(self):
        result = self._run_simulate("gear_pair")
        self.assertIn("time_series", result)

    def test_four_bar(self):
        result = self._run_simulate("four_bar")
        self.assertIn("time_series", result)

    def test_planetary(self):
        result = self._run_simulate("planetary")
        self.assertIn("time_series", result)

    def test_hexapod(self):
        result = self._run_simulate("hexapod")
        self.assertIn("time_series", result)


if __name__ == "__main__":
    unittest.main()
