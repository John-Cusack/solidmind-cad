"""Integration tests for motion tools with the Gazebo backend.

These tests run the motion tool surface through ``server.main._call_tool``
against a lightweight socket bridge that emulates the Gazebo sidecar protocol.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import unittest

from server import gazebo_client
from server import isaac_client
from server import main as mcp_main
from server import motion_store
from server.tools_motion import _active_sessions


class _FakeGazeboBridge:
    """Small in-process TCP server that emulates the Gazebo bridge protocol."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = 0
        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._sessions: dict[str, dict[str, float]] = {}
        self.last_sim_profile: dict | None = None
        self.last_teleop_profile: dict | None = None
        self.last_teleop_command_args: dict | None = None

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.settimeout(0.2)
        srv.bind((self.host, 0))
        srv.listen(1)

        self._srv = srv
        self.port = int(srv.getsockname()[1])

        def run() -> None:
            self._ready.set()
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                with conn:
                    conn.settimeout(0.2)
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            data = conn.recv(65536)
                        except socket.timeout:
                            continue
                        except OSError:
                            break
                        if not data:
                            break
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if not line:
                                continue
                            cmd = json.loads(line)
                            resp = self._handle(cmd)
                            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _handle(self, cmd: dict) -> dict:
        name = cmd.get("cmd")
        args = cmd.get("args", {})

        if name == "ping":
            return {"ok": True, "result": {"pong": True}}

        if name == "simulate":
            duration = float(args.get("duration_s", 1.0))
            mech = args.get("mechanism", {})
            self.last_sim_profile = args.get("profile", {})
            part_ids = [p.get("id") for p in mech.get("parts", []) if p.get("id")]
            return {
                "ok": True,
                "result": {
                    "time_series": [
                        {"t": 0.0, "parts": {pid: {"omega_rpm": 0.0} for pid in part_ids}},
                        {"t": duration, "parts": {pid: {"omega_rpm": 99.9} for pid in part_ids}},
                    ],
                    "summary": {
                        "simulation_time_s": duration,
                        "steady_state_speeds": {pid: 99.9 for pid in part_ids},
                    },
                },
            }

        if name == "teleop_start":
            self.last_teleop_profile = args.get("profile", {})
            session_id = f"gz_sess_{len(self._sessions) + 1}"
            self._sessions[session_id] = {
                "vx_mps": 0.0,
                "yaw_rate_rps": 0.0,
                "body_height_m": 0.0,
                "vy_mps": 0.0,
                "vz_mps": 0.0,
            }
            return {
                "ok": True,
                "result": {
                    "session_id": session_id,
                    "status": "started",
                },
            }

        if name == "teleop_command":
            session_id = str(args.get("session_id", ""))
            self.last_teleop_command_args = dict(args)
            if session_id not in self._sessions:
                return {"ok": False, "error": f"unknown session {session_id}"}
            self._sessions[session_id] = {
                "vx_mps": float(args.get("vx_mps", 0.0)),
                "yaw_rate_rps": float(args.get("yaw_rate_rps", 0.0)),
                "body_height_m": float(args.get("body_height_m", 0.0)),
                "vy_mps": float(args.get("vy_mps", 0.0)),
                "vz_mps": float(args.get("vz_mps", 0.0)),
            }
            return {"ok": True, "result": {"applied": True}}

        if name == "teleop_state":
            session_id = str(args.get("session_id", ""))
            if session_id not in self._sessions:
                return {"ok": False, "error": f"unknown session {session_id}"}
            return {"ok": True, "result": {"state": dict(self._sessions[session_id])}}

        if name == "teleop_stop":
            session_id = str(args.get("session_id", ""))
            if session_id in self._sessions:
                del self._sessions[session_id]
            return {"ok": True, "result": {"stopped": True}}

        return {"ok": False, "error": f"unknown cmd: {name}"}


class TestMotionGazeboIntegration(unittest.TestCase):
    def setUp(self) -> None:
        motion_store.clear()
        _active_sessions.clear()
        gazebo_client.reset_client()
        # Also ensure Isaac client is disconnected so it doesn't interfere
        isaac_client.reset_client()
        self.bridge = _FakeGazeboBridge()
        self.bridge.start()
        gazebo_client._client = gazebo_client.GazeboClient(  # type: ignore[attr-defined]
            host="127.0.0.1", port=self.bridge.port,
        )

    def tearDown(self) -> None:
        gazebo_client.reset_client()
        isaac_client.reset_client()
        _active_sessions.clear()
        motion_store.clear()
        self.bridge.stop()

    @staticmethod
    def _mechanism_dict() -> dict:
        return {
            "name": "hexapod_min",
            "parts": [
                {"id": "base"},
                {"id": "leg_1"},
                {"id": "frame", "is_ground": True},
            ],
            "joints": [
                {
                    "id": "base_rev",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "base",
                },
                {
                    "id": "leg_rev",
                    "joint_type": "revolute",
                    "parent_part": "base",
                    "child_part": "leg_1",
                },
            ],
            "drives": [{"joint_id": "base_rev", "speed_rpm": 100.0}],
        }

    def test_batch_simulate_gazebo_routes_correctly(self) -> None:
        defined = mcp_main._call_tool(
            "motion.define_mechanism",
            {"mechanism": self._mechanism_dict()},
        )
        self.assertTrue(defined["ok"])

        result = mcp_main._call_tool(
            "motion.simulate",
            {
                "mechanism_id": defined["mechanism_id"],
                "backend": "gazebo",
                "duration_s": 0.5,
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend_used"], "gazebo")
        self.assertEqual(result["mode_used"], "batch")
        self.assertIn("summary", result)
        self.assertAlmostEqual(result["summary"]["simulation_time_s"], 0.5)

    def test_gazebo_teleop_lifecycle_via_main_dispatch(self) -> None:
        defined = mcp_main._call_tool(
            "motion.define_mechanism",
            {"mechanism": self._mechanism_dict()},
        )
        mechanism_id = defined["mechanism_id"]

        started = mcp_main._call_tool(
            "motion.teleop_start",
            {"mechanism_id": mechanism_id, "backend": "gazebo"},
        )
        self.assertTrue(started["ok"])
        self.assertEqual(started["backend_used"], "gazebo")
        session_id = started["session_id"]

        applied = mcp_main._call_tool(
            "motion.teleop_command",
            {
                "session_id": session_id,
                "vx_mps": 0.3,
                "yaw_rate_rps": 0.5,
                "body_height_m": 0.02,
                "vy_mps": 0.1,
                "vz_mps": 0.05,
            },
        )
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["backend_used"], "gazebo")

        state = mcp_main._call_tool(
            "motion.teleop_state",
            {"session_id": session_id},
        )
        self.assertTrue(state["ok"])
        self.assertAlmostEqual(state["state"]["vy_mps"], 0.1)
        self.assertAlmostEqual(state["state"]["vz_mps"], 0.05)

        stopped = mcp_main._call_tool(
            "motion.teleop_stop",
            {"session_id": session_id},
        )
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["stopped"])

    def test_gazebo_teleop_command_forwards_vy_vz(self) -> None:
        defined = mcp_main._call_tool(
            "motion.define_mechanism",
            {"mechanism": self._mechanism_dict()},
        )
        mechanism_id = defined["mechanism_id"]

        started = mcp_main._call_tool(
            "motion.teleop_start",
            {"mechanism_id": mechanism_id, "backend": "gazebo"},
        )
        session_id = started["session_id"]

        mcp_main._call_tool(
            "motion.teleop_command",
            {
                "session_id": session_id,
                "vx_mps": 0.0,
                "vy_mps": 0.7,
                "vz_mps": 0.3,
            },
        )

        # Verify the bridge actually received vy_mps and vz_mps
        self.assertIsNotNone(self.bridge.last_teleop_command_args)
        self.assertAlmostEqual(self.bridge.last_teleop_command_args["vy_mps"], 0.7)
        self.assertAlmostEqual(self.bridge.last_teleop_command_args["vz_mps"], 0.3)

    def test_isaac_default_backend_unaffected(self) -> None:
        """Adding Gazebo should not break the Isaac default path."""
        defined = mcp_main._call_tool(
            "motion.define_mechanism",
            {"mechanism": self._mechanism_dict()},
        )

        result = mcp_main._call_tool(
            "motion.simulate",
            {"mechanism_id": defined["mechanism_id"], "backend": "isaac"},
        )

        if result["ok"]:
            # Isaac bridge is running — verify it still uses isaac backend
            self.assertEqual(result["backend_used"], "isaac")
        else:
            # Isaac is unavailable — verify the choice contract includes all 3 backends
            self.assertEqual(result["error"]["code"], "BACKEND_UNAVAILABLE_CHOOSE")
            self.assertEqual(result["backend_requested"], "isaac")
            choice_backends = {entry["backend"] for entry in result.get("choices", [])}
            self.assertIn("isaac", choice_backends)
            self.assertIn("chrono", choice_backends)
            self.assertIn("gazebo", choice_backends)


if __name__ == "__main__":
    unittest.main()
