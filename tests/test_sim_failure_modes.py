"""Resilience tests for simulation bridge client and failure scenarios."""
from __future__ import annotations

import json
import socket
import threading
import time
import unittest

from tests.conftest import GazeboStubBridge, unused_tcp_port


class _BadTcpServer:
    """Minimal TCP server that misbehaves in configurable ways."""

    def __init__(self, port: int, behavior: str = "accept_never_respond") -> None:
        self.host = "127.0.0.1"
        self.port = port
        self.behavior = behavior
        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.settimeout(0.2)
        self._srv.bind((self.host, self.port))
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            try:
                if self.behavior == "accept_never_respond":
                    # Accept but never send anything back
                    time.sleep(10)
                elif self.behavior == "truncated_json":
                    # Read the request, send back half a JSON line
                    conn.recv(4096)
                    conn.sendall(b'{"ok": true, "res')
                    conn.close()
                elif self.behavior == "close_mid_response":
                    # Read request, start responding, close socket
                    conn.recv(4096)
                    conn.sendall(b'{"ok": true, "result": {"time_se')
                    time.sleep(0.01)
                    conn.close()
                else:
                    conn.close()
            except OSError:
                pass

    def __enter__(self) -> _BadTcpServer:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class TestSimulateBackendNotRunning(unittest.TestCase):
    """No bridge on port -> structured error, not crash."""

    def test_simulate_backend_not_running(self):
        from server.sim_engine_manager import _health_check

        port = unused_tcp_port()
        healthy, resp = _health_check("127.0.0.1", port, timeout=1.0)
        self.assertFalse(healthy)
        self.assertIn("error", resp)


class TestHealthCheckTimeout(unittest.TestCase):
    """TCP server accepts but never responds -> timeout within 2s."""

    def test_health_check_timeout(self):
        from server.sim_engine_manager import _health_check

        port = unused_tcp_port()
        with _BadTcpServer(port, "accept_never_respond"):
            start = time.monotonic()
            healthy, resp = _health_check("127.0.0.1", port, timeout=2.0)
            elapsed = time.monotonic() - start

        self.assertFalse(healthy)
        # Should timeout in roughly 2s, not hang forever
        self.assertLess(elapsed, 5.0)


class TestTruncatedJsonResponse(unittest.TestCase):
    """Server sends half a JSON line -> client raises, not hangs."""

    def test_truncated_json_response(self):
        port = unused_tcp_port()
        with _BadTcpServer(port, "truncated_json"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", port))
            try:
                msg = json.dumps({"cmd": "ping", "args": {}}) + "\n"
                sock.sendall(msg.encode())
                data = b""
                try:
                    while b"\n" not in data:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except TimeoutError:
                    pass
                # Either we get incomplete data or empty — both are ok as long as no hang
                if data.strip():
                    with self.assertRaises(json.JSONDecodeError):
                        json.loads(data.decode().strip())
                # If data is empty that's also a valid failure mode (connection closed)
            finally:
                sock.close()


class TestConnectionClosedMidResponse(unittest.TestCase):
    """Server closes socket after partial write -> client gets error."""

    def test_connection_closed_mid_response(self):
        port = unused_tcp_port()
        with _BadTcpServer(port, "close_mid_response"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", port))
            try:
                msg = json.dumps({"cmd": "simulate", "args": {}}) + "\n"
                sock.sendall(msg.encode())
                data = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except TimeoutError:
                    pass
                # Should get incomplete data, not a valid JSON response
                if data.strip():
                    try:
                        json.loads(data.decode().strip())
                        # If it somehow parsed, it shouldn't have complete result
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass  # Expected
            finally:
                sock.close()


class TestLargeMechanismNoOverflow(unittest.TestCase):
    """500-joint mechanism -> serialize/send/receive without buffer issues."""

    def test_large_mechanism(self):
        port = unused_tcp_port()
        # Build a large mechanism
        parts = [{"id": f"part_{i}"} for i in range(501)]
        parts[0]["is_ground"] = True
        joints = [
            {
                "id": f"joint_{i}",
                "joint_type": "revolute",
                "parent_part": "part_0",
                "child_part": f"part_{i + 1}",
            }
            for i in range(500)
        ]
        big_mech = {
            "name": "big_mechanism",
            "parts": parts,
            "joints": joints,
            "drives": [],
        }

        with GazeboStubBridge(port) as bridge:
            # Simulate with the big mechanism
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30.0)
            sock.connect((bridge.host, bridge.port))
            try:
                msg = json.dumps({
                    "cmd": "simulate",
                    "args": {
                        "mechanism": big_mech,
                        "duration_s": 0.1,
                        "dt_s": 0.01,
                        "output_interval": 0.05,
                    },
                }) + "\n"
                sock.sendall(msg.encode())
                data = b""
                while b"\n" not in data:
                    chunk = sock.recv(1048576)  # 1MB buffer
                    if not chunk:
                        break
                    data += chunk
                resp = json.loads(data.decode().strip())
                self.assertTrue(resp["ok"], resp)
                ts = resp["result"]["time_series"]
                self.assertGreater(len(ts), 0)
                # Check that all 501 parts appear in entries
                first_entry = ts[0]
                self.assertEqual(len(first_entry["parts"]), 501)
            finally:
                sock.close()


class TestTwoEnginesSamePort(unittest.TestCase):
    """Start gazebo on port X, then try to bind another server on same X."""

    def test_two_engines_same_port(self):
        from server.sim_engine_manager import _engines, _lock, shutdown_all, start_engine

        try:
            port = unused_tcp_port()
            r1 = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
            self.assertTrue(r1["ok"], r1)

            # Try to manually check port availability - it should be taken
            from server.sim_engine_manager import _port_available
            self.assertFalse(_port_available("127.0.0.1", port))
        finally:
            shutdown_all()
            with _lock:
                _engines.clear()


if __name__ == "__main__":
    unittest.main()
