"""Tests for server.sim_engine_manager — state machine, thread safety, health checks."""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from server.sim_engine_manager import (
    VALID_BACKENDS,
    EngineState,
    EngineStatus,
    _engines,
    _error,
    _get_host,
    _get_port,
    _health_check,
    _lock,
    _tcp_ping,
    engine_status,
    shutdown_all,
    start_engine,
    start_monitor,
    stop_engine,
    stop_monitor,
)


class TestEngineStatus(unittest.TestCase):
    """Test EngineStatus enum values."""

    def test_enum_values(self):
        self.assertEqual(EngineStatus.STOPPED.value, "stopped")
        self.assertEqual(EngineStatus.INIT.value, "init")
        self.assertEqual(EngineStatus.READY.value, "ready")
        self.assertEqual(EngineStatus.RUNNING.value, "running")
        self.assertEqual(EngineStatus.DRAINING.value, "draining")
        self.assertEqual(EngineStatus.FAILED.value, "failed")

    def test_str_enum(self):
        self.assertIsInstance(EngineStatus.READY, str)
        self.assertEqual(EngineStatus.READY.value, "ready")


class TestEngineState(unittest.TestCase):
    """Test EngineState dataclass."""

    def test_defaults(self):
        state = EngineState(backend="chrono")
        self.assertEqual(state.backend, "chrono")
        self.assertEqual(state.status, EngineStatus.STOPPED)
        self.assertEqual(state.port, 0)
        self.assertIsNone(state.pid)
        self.assertIsNone(state.process)
        self.assertEqual(state.started_at, 0.0)
        self.assertEqual(state.error, "")

    def test_slots(self):
        state = EngineState(backend="gazebo")
        with self.assertRaises(AttributeError):
            state.nonexistent = True  # type: ignore[attr-defined]


class TestConfiguration(unittest.TestCase):
    """Test port/host configuration from env vars."""

    def test_default_host(self):
        with patch.dict("os.environ", {}, clear=False):
            # Remove SOLIDMIND_SIM_HOST if present
            import os
            os.environ.pop("SOLIDMIND_SIM_HOST", None)
            self.assertEqual(_get_host(), "127.0.0.1")

    def test_custom_host(self):
        with patch.dict("os.environ", {"SOLIDMIND_SIM_HOST": "192.168.1.10"}):
            self.assertEqual(_get_host(), "192.168.1.10")

    def test_default_ports(self):
        import os
        for key in ["SOLIDMIND_CHRONO_PORT", "SOLIDMIND_GAZEBO_PORT", "SOLIDMIND_ISAAC_PORT"]:
            os.environ.pop(key, None)
        self.assertEqual(_get_port("chrono"), 9877)
        self.assertEqual(_get_port("gazebo"), 9879)
        self.assertEqual(_get_port("isaac"), 9878)

    def test_custom_port(self):
        with patch.dict("os.environ", {"SOLIDMIND_CHRONO_PORT": "19877"}):
            self.assertEqual(_get_port("chrono"), 19877)

    def test_invalid_port_env(self):
        with patch.dict("os.environ", {"SOLIDMIND_CHRONO_PORT": "not_a_number"}):
            self.assertEqual(_get_port("chrono"), 9877)  # falls back to default


class TestErrorHelper(unittest.TestCase):
    def test_error_format(self):
        result = _error("TEST_CODE", "test message")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TEST_CODE")
        self.assertEqual(result["error"]["message"], "test message")


class TestHealthCheck(unittest.TestCase):
    """Test protocol-level health check."""

    def test_health_check_against_mock_bridge(self):
        """Start a mock bridge that responds to ping, verify health check."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        port = server_sock.getsockname()[1]
        server_sock.listen(1)

        def mock_bridge():
            conn, _ = server_sock.accept()
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.decode().strip())
            if req.get("cmd") == "ping":
                resp = json.dumps({"ok": True, "backend": "test"}) + "\n"
                conn.sendall(resp.encode())
            conn.close()

        t = threading.Thread(target=mock_bridge, daemon=True)
        t.start()

        try:
            healthy, resp = _health_check("127.0.0.1", port, timeout=2.0)
            self.assertTrue(healthy)
            self.assertTrue(resp.get("ok"))
        finally:
            server_sock.close()
            t.join(timeout=2)

    def test_health_check_connection_refused(self):
        healthy, resp = _health_check("127.0.0.1", 1, timeout=0.5)
        self.assertFalse(healthy)
        self.assertIn("error", resp)

    def test_tcp_ping_connection_refused(self):
        self.assertFalse(_tcp_ping("127.0.0.1", 1, timeout=0.5))


class TestStartEngine(unittest.TestCase):
    """Test start_engine with mocked subprocess."""

    def setUp(self):
        # Clean state between tests
        with _lock:
            _engines.clear()

    def test_unknown_backend(self):
        result = start_engine("unknown_thing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_BACKEND")

    @patch("server.sim_engine_manager._tcp_ping", return_value=True)
    def test_already_running_external(self, mock_ping):
        """If something is already listening, return already_running."""
        result = start_engine("gazebo")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "already_running")

    @patch("server.sim_engine_manager._port_available", return_value=False)
    @patch("server.sim_engine_manager._tcp_ping", return_value=False)
    def test_port_unavailable(self, mock_ping, mock_port):
        result = start_engine("gazebo")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PORT_UNAVAILABLE")


class TestStopEngine(unittest.TestCase):
    """Test stop_engine with state transitions."""

    def setUp(self):
        with _lock:
            _engines.clear()

    def test_unknown_backend(self):
        result = stop_engine("unknown")
        self.assertFalse(result["ok"])

    def test_stop_not_running(self):
        result = stop_engine("chrono")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "not_running")

    def test_stop_transitions_to_stopped(self):
        """Mock a running process and verify draining → stopped."""
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None  # process alive
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0  # exits cleanly on wait

        with _lock:
            state = EngineState(
                backend="gazebo",
                status=EngineStatus.READY,
                port=9879,
                pid=12345,
                process=mock_proc,
                started_at=time.monotonic(),
            )
            _engines["gazebo"] = state

        with patch("server.sim_engine_manager._send_shutdown"):
            result = stop_engine("gazebo")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["pid"], 12345)

        with _lock:
            self.assertEqual(_engines["gazebo"].status, EngineStatus.STOPPED)


class TestEngineStatusReport(unittest.TestCase):
    """Test engine_status reporting."""

    def setUp(self):
        with _lock:
            _engines.clear()

    @patch("server.sim_engine_manager._health_check", return_value=(False, {"error": "refused"}))
    def test_all_stopped(self, mock_hc):
        result = engine_status()
        self.assertTrue(result["ok"])
        self.assertIn("engines", result)
        for backend in VALID_BACKENDS:
            eng = result["engines"][backend]
            self.assertEqual(eng["status"], "stopped")
            self.assertFalse(eng["managed"])

    def test_crash_detection_in_status(self):
        """A crashed process should be detected during status check."""
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 1  # process exited
        mock_proc.returncode = 1

        with _lock:
            _engines["chrono"] = EngineState(
                backend="chrono",
                status=EngineStatus.READY,
                port=9877,
                pid=99999,
                process=mock_proc,
                started_at=time.monotonic(),
            )

        result = engine_status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["engines"]["chrono"]["status"], "failed")
        self.assertIn("Process exited", result["engines"]["chrono"]["error"])


class TestThreadSafety(unittest.TestCase):
    """Test concurrent access doesn't crash."""

    def setUp(self):
        with _lock:
            _engines.clear()

    @patch("server.sim_engine_manager._tcp_ping", return_value=False)
    @patch("server.sim_engine_manager._port_available", return_value=False)
    def test_concurrent_start_stop(self, mock_port, mock_ping):
        """Multiple threads calling start/stop shouldn't raise."""
        errors: list[Exception] = []

        def worker(backend: str):
            try:
                for _ in range(5):
                    start_engine(backend)
                    stop_engine(backend)
                    engine_status()
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(b,))
            for b in VALID_BACKENDS
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Thread safety errors: {errors}")


class TestMonitor(unittest.TestCase):
    """Test monitor start/stop."""

    def setUp(self):
        with _lock:
            _engines.clear()
        stop_monitor()

    def tearDown(self):
        stop_monitor()

    def test_start_stop_monitor(self):
        start_monitor(interval_s=0.1)
        time.sleep(0.3)
        stop_monitor()
        # Should not raise

    def test_crash_detection_by_monitor(self):
        """Monitor should detect a crashed process."""
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 137  # killed
        mock_proc.returncode = 137

        with _lock:
            _engines["gazebo"] = EngineState(
                backend="gazebo",
                status=EngineStatus.READY,
                port=9879,
                pid=55555,
                process=mock_proc,
                started_at=time.monotonic(),
            )

        start_monitor(interval_s=0.1)
        time.sleep(0.5)
        stop_monitor()

        with _lock:
            self.assertEqual(_engines["gazebo"].status, EngineStatus.FAILED)
            self.assertIn("137", _engines["gazebo"].error)


class TestShutdownAll(unittest.TestCase):
    def setUp(self):
        with _lock:
            _engines.clear()

    def test_shutdown_all_empty(self):
        shutdown_all()  # should not raise


if __name__ == "__main__":
    unittest.main()
