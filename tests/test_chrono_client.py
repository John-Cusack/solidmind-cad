"""Tests for the Chrono daemon client.

Uses an echo server (no Chrono binary needed) — same pattern as test_freecad_client.py.
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

from server.chrono_client import ChronoClient, ChronoCommandError, ChronoConnectionError

_DAEMON_PATH = Path(__file__).resolve().parents[1] / "chrono_daemon" / "build" / "chrono_daemon"
_CHRONO_LIB_DIR = os.environ.get("CHRONO_LIB_DIR", "/usr/local/lib")


def _daemon_available() -> bool:
    return _DAEMON_PATH.is_file() and os.access(_DAEMON_PATH, os.X_OK)


def _wait_for_listening(host: str, port: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


def _make_chrono_echo_server(host: str, port: int) -> tuple[socket.socket, threading.Event]:
    """Create a simple echo server that simulates the Chrono daemon."""
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
                    elif cmd["cmd"] == "simulate":
                        # Return a minimal simulation result
                        args = cmd.get("args", {})
                        duration = args.get("duration_s", 1.0)
                        resp = {
                            "ok": True,
                            "result": {
                                "time_series": [
                                    {"t": 0.0, "parts": {}},
                                    {"t": duration, "parts": {}},
                                ],
                                "summary": {
                                    "steady_state_speeds": {"sun": 1000, "carrier": 333.3},
                                    "peak_torques": {"sun": 5.2, "carrier": 15.1},
                                    "overall_efficiency": 0.97,
                                    "simulation_time_s": duration,
                                    "time_steps": int(duration / args.get("dt_s", 0.001)),
                                    "output_samples": 2,
                                },
                            },
                        }
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


class TestChronoClientConnection(unittest.TestCase):
    def test_connection_refused(self) -> None:
        client = ChronoClient(host="127.0.0.1", port=29999)
        with self.assertRaises(ChronoConnectionError):
            client.connect(timeout=0.5)

    def test_connect_and_ping(self) -> None:
        srv, _ = _make_chrono_echo_server("127.0.0.1", 29876)
        try:
            client = ChronoClient(host="127.0.0.1", port=29876)
            client.connect(timeout=2.0)
            self.assertTrue(client.is_connected)
            self.assertTrue(client.ping())
            client.disconnect()
            self.assertFalse(client.is_connected)
        finally:
            srv.close()

    def test_send_command_success(self) -> None:
        srv, _ = _make_chrono_echo_server("127.0.0.1", 29877)
        try:
            client = ChronoClient(host="127.0.0.1", port=29877)
            client.connect(timeout=2.0)
            result = client.send_command("test_cmd", x=1, y=2)
            self.assertIn("echo", result)
            self.assertEqual(result["echo"]["cmd"], "test_cmd")
            client.disconnect()
        finally:
            srv.close()

    def test_send_command_failure(self) -> None:
        srv, _ = _make_chrono_echo_server("127.0.0.1", 29878)
        try:
            client = ChronoClient(host="127.0.0.1", port=29878)
            client.connect(timeout=2.0)
            with self.assertRaises(ChronoCommandError):
                client.send_command("fail")
            client.disconnect()
        finally:
            srv.close()


class TestChronoClientRetry(unittest.TestCase):
    def test_connect_with_retry_fails(self) -> None:
        client = ChronoClient(host="127.0.0.1", port=29999)
        with self.assertRaises(ChronoConnectionError):
            client.connect_with_retry(max_retries=2, retry_delay=0.1)


class TestChronoClientSimulate(unittest.TestCase):
    def test_simulate(self) -> None:
        srv, _ = _make_chrono_echo_server("127.0.0.1", 29879)
        try:
            client = ChronoClient(host="127.0.0.1", port=29879)
            client.connect(timeout=2.0)
            result = client.simulate(
                mechanism={"name": "test", "parts": [], "joints": [], "drives": []},
                duration_s=0.5,
                dt_s=0.001,
            )
            self.assertIn("time_series", result)
            self.assertIn("summary", result)
            self.assertEqual(len(result["time_series"]), 2)
            self.assertAlmostEqual(result["summary"]["overall_efficiency"], 0.97)
            self.assertAlmostEqual(result["summary"]["steady_state_speeds"]["carrier"], 333.3)
            client.disconnect()
        finally:
            srv.close()


# Real-backend test (pytest marker: requires_chrono_real).  The repo runs
# unittest, so the gate is skipUnless — same convention as
# tests/test_chrono_spring_e2e.py.
@unittest.skipUnless(_daemon_available(), f"chrono_daemon binary not built at {_DAEMON_PATH}")
class TestChronoContractHandshake(unittest.TestCase):
    """Engine Integration Contract v1 against the real C++ daemon.

    Build it with::

        cd chrono_daemon && mkdir -p build && cd build && cmake .. && make
    """

    _PORT = 29881

    @classmethod
    def setUpClass(cls) -> None:
        cls._proc = subprocess.Popen(  # noqa: S603 — local test binary
            [str(_DAEMON_PATH), "--port", str(cls._PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LD_LIBRARY_PATH": _CHRONO_LIB_DIR},
        )
        if not _wait_for_listening("127.0.0.1", cls._PORT):
            cls._proc.kill()
            raise unittest.SkipTest("chrono_daemon did not start listening")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._proc.terminate()
        try:
            cls._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls._proc.kill()

    def _call(self, payload: dict) -> dict:
        with socket.create_connection(("127.0.0.1", self._PORT), timeout=5.0) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.split(b"\n")[0].decode())

    def test_hello(self) -> None:
        resp = self._call({"cmd": "hello", "args": {}})
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertEqual(result["protocol_version"], "1.0.0")
        self.assertEqual(result["contract_versions_supported"], ["1"])
        self.assertEqual(result["engine"], "chrono")
        self.assertEqual(result["runtime_mode"], "real")

        caps = result["capabilities"]
        self.assertEqual(caps["modes"], ["batch"])
        self.assertEqual(caps["formats"], ["chrono_spec", "mechanism"])
        self.assertEqual(caps["features"], [])
        self.assertEqual(caps["fields"], {"emits": [], "accepts": []})

    def test_request_id_round_trip(self) -> None:
        self.assertEqual(self._call({"cmd": "hello", "request_id": "rid-1"})["request_id"], "rid-1")
        self.assertNotIn("request_id", self._call({"cmd": "hello"}))

    def test_unknown_command(self) -> None:
        resp = self._call({"cmd": "definitely_not_a_verb", "request_id": "rid-2"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "UNSUPPORTED_COMMAND")
        self.assertEqual(resp["request_id"], "rid-2")


if __name__ == "__main__":
    unittest.main()
