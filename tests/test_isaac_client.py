"""Tests for Isaac bridge client.

Uses an echo server (no Isaac runtime needed).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import unittest
from unittest.mock import patch

from server.isaac_client import IsaacClient, IsaacCommandError, IsaacConnectionError


def _make_isaac_echo_server(host: str, port: int) -> tuple[socket.socket, threading.Event]:
    """Create a simple echo server that simulates the Isaac bridge."""
    ready = threading.Event()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(5.0)
    srv.bind((host, port))
    srv.listen(1)

    def run() -> None:
        ready.set()
        try:
            conn, _ = srv.accept()
            conn.settimeout(5.0)
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = json.loads(line)
                    if cmd["cmd"] == "ping":
                        resp = {"ok": True, "result": {"pong": True}}
                    elif cmd["cmd"] == "fail":
                        resp = {"ok": False, "error": "intentional failure"}
                    elif cmd["cmd"] == "fail_structured":
                        resp = {
                            "ok": False,
                            "error": {
                                "code": "UNSUPPORTED_JOINT_TYPE",
                                "message": "Unsupported joints present",
                            },
                        }
                    elif cmd["cmd"] == "import_urdf":
                        args = cmd.get("args", {})
                        resp = {
                            "ok": True,
                            "result": {
                                "prim_path": "/World/robot",
                                "joint_count": 6,
                                "link_count": 7,
                            },
                        }
                    elif cmd["cmd"] == "simulate":
                        args = cmd.get("args", {})
                        duration = args.get("duration_s", 1.0)
                        summary: dict = {"simulation_time_s": duration}
                        if "urdf_path" in args:
                            summary["urdf_path"] = args["urdf_path"]
                        resp = {
                            "ok": True,
                            "result": {
                                "time_series": [
                                    {"t": 0.0, "parts": {}},
                                    {"t": duration, "parts": {}},
                                ],
                                "summary": summary,
                            },
                        }
                    elif cmd["cmd"] == "simulate_start":
                        args = cmd.get("args", {})
                        duration = args.get("duration_s", 1.0)
                        resp = {
                            "ok": True,
                            "result": {
                                "session_id": "sim_echo",
                                "status": "complete",
                                "target_steps": 100,
                                "steady_state_speeds": {},
                                "profile_used": args.get("profile", {}),
                            },
                        }
                    elif cmd["cmd"] == "simulate_status":
                        resp = {
                            "ok": True,
                            "result": {
                                "status": "complete",
                                "completed_steps": 100,
                                "target_steps": 100,
                                "samples_count": 2,
                            },
                        }
                    elif cmd["cmd"] == "simulate_stop":
                        resp = {
                            "ok": True,
                            "result": {
                                "stopped": True,
                                "completed_steps": 100,
                                "target_steps": 100,
                                "samples": [
                                    {"t": 0.0, "parts": {}},
                                    {"t": 0.5, "parts": {}},
                                ],
                            },
                        }
                    elif cmd["cmd"] == "teleop_start":
                        resp = {
                            "ok": True,
                            "result": {
                                "session_id": "sess_echo",
                                "status": "started",
                            },
                        }
                    elif cmd["cmd"] == "teleop_command":
                        resp = {"ok": True, "result": {"applied": True}}
                    elif cmd["cmd"] == "teleop_state":
                        resp = {"ok": True, "result": {"state": {"vx_mps": 0.2}}}
                    elif cmd["cmd"] == "teleop_stop":
                        resp = {"ok": True, "result": {"stopped": True}}
                    else:
                        resp = {"ok": True, "result": {"echo": cmd}}
                    conn.sendall((json.dumps(resp) + "\n").encode())
            conn.close()
        except (TimeoutError, OSError):
            pass
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(timeout=2.0)
    return srv, ready


class TestIsaacClientConnection(unittest.TestCase):
    def test_connection_refused(self) -> None:
        client = IsaacClient(host="127.0.0.1", port=29989)
        with self.assertRaises(IsaacConnectionError):
            client.connect(timeout=0.5)

    def test_connect_and_ping(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29886)
        try:
            client = IsaacClient(host="127.0.0.1", port=29886)
            client.connect(timeout=2.0)
            self.assertTrue(client.is_connected)
            self.assertTrue(client.ping())
            client.disconnect()
            self.assertFalse(client.is_connected)
        finally:
            srv.close()

    def test_send_command_failure(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29887)
        try:
            client = IsaacClient(host="127.0.0.1", port=29887)
            client.connect(timeout=2.0)
            with self.assertRaises(IsaacCommandError):
                client.send_command("fail")
            client.disconnect()
        finally:
            srv.close()

    def test_socket_creation_permission_error_wrapped(self) -> None:
        client = IsaacClient(host="127.0.0.1", port=29890)
        with patch("server.isaac_client.socket.socket", side_effect=PermissionError("denied")):
            with self.assertRaises(IsaacConnectionError):
                client.connect(timeout=0.5)

    def test_structured_error_preserves_code(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29891)
        try:
            client = IsaacClient(host="127.0.0.1", port=29891)
            client.connect(timeout=2.0)
            with self.assertRaises(IsaacCommandError) as ctx:
                client.send_command("fail_structured")
            self.assertEqual(ctx.exception.code, "UNSUPPORTED_JOINT_TYPE")
            self.assertIn("Unsupported joints", str(ctx.exception))
            client.disconnect()
        finally:
            srv.close()

    def test_env_overrides_default_host_port_and_timeouts(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SOLIDMIND_ISAAC_HOST": "10.1.2.3",
                "SOLIDMIND_ISAAC_PORT": "29900",
                "SOLIDMIND_ISAAC_CONNECT_TIMEOUT_S": "7.5",
                "SOLIDMIND_ISAAC_READ_TIMEOUT_S": "42.0",
            },
            clear=False,
        ):
            client = IsaacClient()
            self.assertEqual(client._host, "10.1.2.3")
            self.assertEqual(client._port, 29900)
            self.assertAlmostEqual(client._connect_timeout, 7.5)
            self.assertAlmostEqual(client._read_timeout, 42.0)


class TestIsaacClientImportURDF(unittest.TestCase):
    def test_import_urdf(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29892)
        try:
            client = IsaacClient(host="127.0.0.1", port=29892)
            client.connect(timeout=2.0)
            result = client.import_urdf("/tmp/robot.urdf", import_config={"fix_base": True})
            self.assertEqual(result["prim_path"], "/World/robot")
            self.assertEqual(result["joint_count"], 6)
            self.assertEqual(result["link_count"], 7)
            client.disconnect()
        finally:
            srv.close()

    def test_import_urdf_no_config(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29893)
        try:
            client = IsaacClient(host="127.0.0.1", port=29893)
            client.connect(timeout=2.0)
            result = client.import_urdf("/tmp/robot.urdf")
            self.assertEqual(result["prim_path"], "/World/robot")
            client.disconnect()
        finally:
            srv.close()


class TestIsaacClientSimulation(unittest.TestCase):
    def test_simulate(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29888)
        try:
            client = IsaacClient(host="127.0.0.1", port=29888)
            client.connect(timeout=2.0)
            result = client.simulate(
                mechanism={"name": "test", "parts": [], "joints": [], "drives": []},
                duration_s=0.5,
                dt_s=0.001,
            )
            self.assertIn("time_series", result)
            self.assertIn("summary", result)
            self.assertEqual(len(result["time_series"]), 2)
            self.assertAlmostEqual(result["summary"]["simulation_time_s"], 0.5)
            client.disconnect()
        finally:
            srv.close()

    def test_simulate_with_urdf_path(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29894)
        try:
            client = IsaacClient(host="127.0.0.1", port=29894)
            client.connect(timeout=2.0)
            result = client.simulate(
                mechanism={"name": "test", "parts": [], "joints": [], "drives": []},
                duration_s=0.5,
                dt_s=0.001,
                urdf_path="/tmp/robot.urdf",
                import_config={"fix_base": True},
            )
            self.assertIn("summary", result)
            self.assertEqual(result["summary"]["urdf_path"], "/tmp/robot.urdf")
            client.disconnect()
        finally:
            srv.close()


class TestIsaacClientSimulateSession(unittest.TestCase):
    def test_simulate_lifecycle(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29895)
        try:
            client = IsaacClient(host="127.0.0.1", port=29895)
            client.connect(timeout=2.0)

            started = client.simulate_start(
                mechanism={"name": "test", "parts": [], "joints": [], "drives": []},
                duration_s=0.5,
            )
            self.assertEqual(started["session_id"], "sim_echo")

            status = client.simulate_status("sim_echo")
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["completed_steps"], 100)

            stopped = client.simulate_stop("sim_echo")
            self.assertTrue(stopped["stopped"])
            self.assertIn("samples", stopped)
            self.assertEqual(len(stopped["samples"]), 2)

            client.disconnect()
        finally:
            srv.close()


class TestIsaacClientTeleop(unittest.TestCase):
    def test_teleop_flow(self) -> None:
        srv, _ = _make_isaac_echo_server("127.0.0.1", 29889)
        try:
            client = IsaacClient(host="127.0.0.1", port=29889)
            client.connect(timeout=2.0)

            started = client.teleop_start(mechanism={"name": "hex"}, profile={})
            self.assertEqual(started["session_id"], "sess_echo")

            command = client.teleop_command(
                "sess_echo", vx_mps=0.2, yaw_rate_rps=0.1, body_height_m=0.0
            )
            self.assertTrue(command["applied"])

            state = client.teleop_state("sess_echo")
            self.assertAlmostEqual(state["state"]["vx_mps"], 0.2)

            stopped = client.teleop_stop("sess_echo")
            self.assertTrue(stopped["stopped"])

            client.disconnect()
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
