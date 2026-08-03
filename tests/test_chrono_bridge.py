"""Tests for the Chrono bridge shim — contract face in front of the daemon.

Two layers:

* protocol behaviour with a fake daemon (always runs),
* one end-to-end pass through the real C++ daemon, skipped unless it is built.

The shim is where mechanism → Chrono-spec translation lives after the dialect
inversion, so these also pin that core never has to speak Chrono.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from chrono_bridge.bridge_server import ChronoBridgeServer
from chrono_bridge.mechanism import JointType, as_mechanism, detect_planetary_sets

_DAEMON_PATH = Path(__file__).resolve().parents[1] / "chrono_daemon" / "build" / "chrono_daemon"
_CHRONO_LIB_DIR = os.environ.get("CHRONO_LIB_DIR", "/usr/local/lib")


def _daemon_available() -> bool:
    return _DAEMON_PATH.is_file() and os.access(_DAEMON_PATH, os.X_OK)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_listening(host: str, port: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _gear_pair_mechanism() -> dict[str, Any]:
    """A driven gear pair — the simplest thing with real Chrono content."""
    return {
        "name": "gear_pair",
        "parts": [
            {"id": "frame", "is_ground": True},
            {"id": "gear_a", "mass_kg": 0.1},
            {"id": "gear_b", "mass_kg": 0.2},
        ],
        "joints": [
            {
                "id": "rev_a",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "gear_a",
            },
            {
                "id": "rev_b",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "gear_b",
            },
            {
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "gear_a",
                "child_part": "gear_b",
                "teeth_parent": 20,
                "teeth_child": 40,
                "gear_ratio": 0.5,
            },
        ],
        "drives": [{"joint_id": "rev_a", "speed_rpm": 600.0, "torque_nm": 1.0}],
    }


class _FakeDaemon:
    """Stands in for the C++ daemon: records what the shim forwards."""

    def __init__(self, *, fail: bool = False) -> None:
        self.port = _unused_port()
        self.received: list[dict[str, Any]] = []
        self._fail = fail
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _FakeDaemon:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.settimeout(0.2)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(4)
        self._srv = srv
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(2.0)
                buf = b""
                try:
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            return
                        buf += chunk
                    msg = json.loads(buf.split(b"\n", 1)[0])
                    self.received.append(msg)
                    conn.sendall((json.dumps(self._reply(msg)) + "\n").encode())
                except (OSError, json.JSONDecodeError):
                    continue

    def _reply(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = msg.get("cmd")
        if cmd == "ping":
            return {"ok": True, "result": {"pong": True}}
        if cmd == "hello":
            return {"ok": True, "result": {"engine_version": "fake-9.9"}}
        if cmd == "shutdown":
            return {"ok": True, "result": {"message": "Shutting down"}}
        if cmd == "simulate":
            if self._fail:
                return {
                    "ok": False,
                    "error": {"code": "ENGINE_ERROR", "message": "solver diverged"},
                }
            return {
                "ok": True,
                "result": {
                    "time_series": [{"t": 0.0, "parts": {}}, {"t": 1.0, "parts": {}}],
                    "summary": {"steady_state_speeds": {"gear_a": 600.0, "gear_b": 300.0}},
                },
            }
        return {"ok": False, "error": {"code": "UNSUPPORTED_COMMAND", "message": str(cmd)}}


class _Shim:
    """Run a ChronoBridgeServer in a thread, pointed at *daemon_port*."""

    def __init__(self, daemon_port: int) -> None:
        self.port = _unused_port()
        self._server = ChronoBridgeServer(port=self.port, daemon_port=daemon_port)
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Shim:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if not _wait_for_listening("127.0.0.1", self.port):
            raise RuntimeError("chrono bridge did not start")
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10.0) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])


class TestMechanismView(unittest.TestCase):
    """The wire format parses back into the shape the spec builder reads."""

    def test_parses_a_mechanism_dict(self) -> None:
        mech = as_mechanism(_gear_pair_mechanism())
        self.assertEqual(mech.name, "gear_pair")
        self.assertEqual([p.id for p in mech.parts], ["frame", "gear_a", "gear_b"])
        self.assertTrue(mech.parts[0].is_ground)
        self.assertEqual(mech.get_joint("mesh").joint_type, JointType.GEAR_MESH)
        self.assertEqual(mech.get_joint("mesh").teeth_child, 40)
        self.assertEqual(mech.drives[0].speed_rpm, 600.0)

    def test_passes_through_attribute_objects(self) -> None:
        """In-process callers can hand over their own model objects."""
        from server.motion_models import Mechanism, PartNode

        mech = Mechanism(name="m", parts=(PartNode(id="a"),), joints=(), drives=())
        self.assertIs(as_mechanism(mech), mech)

    def test_unknown_joint_type_degrades_to_fixed(self) -> None:
        mech = as_mechanism(
            {
                "name": "m",
                "parts": [{"id": "a"}],
                "joints": [
                    {"id": "j", "joint_type": "warp_drive", "parent_part": "a", "child_part": "a"}
                ],
            }
        )
        self.assertEqual(mech.joints[0].joint_type, JointType.FIXED)

    def test_planetary_detection_matches_cores(self) -> None:
        """The engine's port must agree with core on what a planetary set is."""
        from server.motion_models import Mechanism
        from server.motion_planetary import detect_planetary_sets as core_detect
        from tests.conftest import mechanism_factory

        data = mechanism_factory("planetary")
        core_sets = core_detect(Mechanism.from_dict(data))
        bridge_sets = detect_planetary_sets(as_mechanism(data))

        self.assertEqual(len(bridge_sets), len(core_sets))
        for bridge, core in zip(bridge_sets, core_sets, strict=True):
            self.assertEqual(bridge.carrier, core.carrier)
            self.assertEqual(bridge.sun, core.sun)
            self.assertEqual(bridge.ring, core.ring)
            self.assertEqual(sorted(bridge.planets), sorted(core.planets))
            self.assertAlmostEqual(bridge.t0, core.t0)


class TestShimProtocol(unittest.TestCase):
    def test_hello_reports_daemon_version(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "hello", "args": {}, "request_id": "rid-1"})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["request_id"], "rid-1")
        result = resp["result"]
        self.assertEqual(result["engine"], "chrono")
        self.assertEqual(result["protocol_version"], "1.0.0")
        self.assertEqual(result["engine_version"], "fake-9.9")
        self.assertEqual(result["capabilities"]["modes"], ["batch"])
        self.assertEqual(result["capabilities"]["formats"], ["mechanism"])

    def test_ping_reports_daemon_reachability(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "ping"})
        self.assertTrue(resp["result"]["pong"])
        self.assertTrue(resp["result"]["daemon_reachable"])

    def test_ping_survives_a_dead_daemon(self) -> None:
        with _Shim(_unused_port()) as shim:
            resp = shim.call({"cmd": "ping"})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["result"]["daemon_reachable"])

    def test_unknown_command(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "nope", "request_id": "rid-2"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "UNSUPPORTED_COMMAND")
        self.assertEqual(resp["request_id"], "rid-2")

    def test_request_id_omitted_when_absent(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            self.assertNotIn("request_id", shim.call({"cmd": "ping"}))

    def test_malformed_json(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            with socket.create_connection(("127.0.0.1", shim.port), timeout=5.0) as sock:
                sock.sendall(b"{not json}\n")
                buf = b""
                while b"\n" not in buf:
                    buf += sock.recv(65536)
        resp = json.loads(buf.split(b"\n", 1)[0])
        self.assertEqual(resp["error"]["code"], "INVALID_JSON")

    def test_simulate_translates_mechanism_to_chrono_spec(self) -> None:
        """The whole point: core sends a mechanism, the shim sends a spec."""
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call(
                {
                    "cmd": "simulate",
                    "args": {
                        "mechanism": _gear_pair_mechanism(),
                        "duration_s": 1.0,
                        "dt_s": 0.001,
                    },
                }
            )
            forwarded = [m for m in daemon.received if m.get("cmd") == "simulate"]

        self.assertTrue(resp["ok"], resp)
        self.assertEqual(len(forwarded), 1)
        args = forwarded[0]["args"]
        self.assertIn("simulation_spec", args)
        self.assertNotIn("mechanism", args)
        self.assertTrue(any(o["type"] == "shaft" for o in args["simulation_spec"]["objects"]))

        summary = resp["result"]["summary"]
        self.assertEqual(summary["engine_mode"], "chrono")
        self.assertAlmostEqual(summary["dt_s"], 0.001)

    def test_simulate_requires_a_mechanism(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "simulate", "args": {"duration_s": 1.0}})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "INVALID_REQUEST")

    def test_undriven_mechanism_fails_preflight(self) -> None:
        mech = _gear_pair_mechanism()
        mech["drives"] = []
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "simulate", "args": {"mechanism": mech}})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "PACKAGE_INVALID")

    def test_daemon_error_surfaces(self) -> None:
        with _FakeDaemon(fail=True) as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "simulate", "args": {"mechanism": _gear_pair_mechanism()}})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "ENGINE_ERROR")
        self.assertIn("diverged", resp["error"]["message"])

    def test_shutdown_drains_the_daemon(self) -> None:
        with _FakeDaemon() as daemon, _Shim(daemon.port) as shim:
            resp = shim.call({"cmd": "shutdown"})
            self.assertTrue(resp["result"]["daemon_stopped"])
            self.assertIn("shutdown", [m.get("cmd") for m in daemon.received])


# Real-backend test (pytest marker: requires_chrono_real).  The repo runs
# unittest, so the gate is skipUnless — same convention as the other e2e tests.
@unittest.skipUnless(_daemon_available(), f"chrono_daemon binary not built at {_DAEMON_PATH}")
class TestShimAgainstRealDaemon(unittest.TestCase):
    """One full pass: mechanism → shim → C++ daemon → contract result."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._daemon_port = _unused_port()
        cls._proc = subprocess.Popen(  # noqa: S603 — local test binary
            [str(_DAEMON_PATH), "--port", str(cls._daemon_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LD_LIBRARY_PATH": _CHRONO_LIB_DIR},
        )
        if not _wait_for_listening("127.0.0.1", cls._daemon_port):
            cls._proc.kill()
            raise unittest.SkipTest("chrono_daemon did not start listening")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._proc.terminate()
        try:
            cls._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls._proc.kill()

    def test_gear_pair_runs_and_respects_the_ratio(self) -> None:
        with _Shim(self._daemon_port) as shim:
            hello = shim.call({"cmd": "hello"})["result"]
            self.assertEqual(hello["engine"], "chrono")
            # The daemon's own version, read through its handshake.
            self.assertEqual(hello["engine_version"], "1.0.0")

            resp = shim.call(
                {
                    "cmd": "simulate",
                    "args": {
                        "mechanism": _gear_pair_mechanism(),
                        "duration_s": 0.5,
                        "dt_s": 0.001,
                        "output_interval": 0.05,
                    },
                }
            )

        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertGreater(len(result["time_series"]), 1)
        speeds = result["summary"]["steady_state_speeds"]
        # 20:40 teeth → the driven gear turns at half the driver's rate.
        self.assertAlmostEqual(abs(speeds["gear_b"]), abs(speeds["gear_a"]) / 2.0, delta=5.0)


if __name__ == "__main__":
    unittest.main()
