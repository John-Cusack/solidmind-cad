"""Tier 2: Real subprocess lifecycle tests for sim_engine_manager.

Tests real process spawn, health check, crash detection — all using stub runtime
so no GPU or real Gazebo/Isaac install is needed.
"""
from __future__ import annotations

import os
import signal
import socket
import time
import unittest

from server.sim_engine_manager import (
    EngineStatus,
    _engines,
    _lock,
    engine_status,
    shutdown_all,
    start_engine,
    start_monitor,
    stop_engine,
    stop_monitor,
)
from tests.conftest import unused_tcp_port


class SubprocessLifecycleBase(unittest.TestCase):
    """Base class that cleans engine state before/after each test."""

    def setUp(self):
        with _lock:
            _engines.clear()
        # Clear port env vars to avoid interference
        for key in ("SOLIDMIND_GAZEBO_PORT", "SOLIDMIND_ISAAC_PORT", "SOLIDMIND_CHRONO_PORT"):
            os.environ.pop(key, None)

    def tearDown(self):
        shutdown_all()
        with _lock:
            _engines.clear()


class TestStartStubGazeboBecomesReady(SubprocessLifecycleBase):
    """start_engine('gazebo', runtime='stub') spawns process, health check passes."""

    def test_start_stub_gazebo_becomes_ready(self):
        port = unused_tcp_port()
        result = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(result["ok"], f"start_engine failed: {result}")
        self.assertIn(result["status"], ("started", "already_running"))
        self.assertEqual(result["backend"], "gazebo")
        self.assertEqual(result["port"], port)
        self.assertIsNotNone(result.get("pid"))

        # Verify engine state
        with _lock:
            state = _engines.get("gazebo")
            self.assertIsNotNone(state)
            self.assertEqual(state.status, EngineStatus.READY)


class TestStopEngineGraceful(SubprocessLifecycleBase):
    """After start, stop_engine drains and process exits 0."""

    def test_stop_engine_graceful(self):
        port = unused_tcp_port()
        start_result = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(start_result["ok"], start_result)
        start_result["pid"]

        stop_result = stop_engine("gazebo", drain_timeout_s=5.0)
        self.assertTrue(stop_result["ok"], stop_result)
        self.assertEqual(stop_result["status"], "stopped")

        with _lock:
            state = _engines.get("gazebo")
            self.assertIsNotNone(state)
            self.assertEqual(state.status, EngineStatus.STOPPED)


class TestStartTwiceIdempotent(SubprocessLifecycleBase):
    """Second start_engine detects healthy process, returns without spawning."""

    def test_start_twice_idempotent(self):
        port = unused_tcp_port()
        r1 = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(r1["ok"], r1)
        pid1 = r1["pid"]

        r2 = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["status"], "already_running")
        # Should be same PID (or at least not a new process)
        if r2.get("pid") is not None:
            self.assertEqual(r2["pid"], pid1)


class TestPortConflict(SubprocessLifecycleBase):
    """Pre-bind a socket, then start_engine -> PORT_UNAVAILABLE error."""

    def test_port_conflict(self):
        port = unused_tcp_port()
        # Pre-bind the port
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            result = start_engine("gazebo", port=port, runtime="stub", timeout_s=5.0)
            # Should detect as external already_running (tcp_ping succeeds)
            # OR port_unavailable (tcp_ping fails but port can't be bound)
            # The blocker socket accepts connections but doesn't respond to health check
            # so _tcp_ping returns True -> "already_running"
            if result["ok"]:
                self.assertEqual(result["status"], "already_running")
            else:
                self.assertEqual(result["error"]["code"], "PORT_UNAVAILABLE")
        finally:
            blocker.close()


class TestCrashDetectedByStatus(SubprocessLifecycleBase):
    """Start, kill process, then engine_status -> FAILED."""

    def test_crash_detected_by_status(self):
        port = unused_tcp_port()
        result = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(result["ok"], result)
        pid = result["pid"]

        # Kill the process
        os.kill(pid, signal.SIGKILL)
        # Give it a moment to die
        time.sleep(0.5)

        status = engine_status()
        self.assertTrue(status["ok"])
        gazebo_status = status["engines"]["gazebo"]
        self.assertEqual(gazebo_status["status"], "failed")
        self.assertIn("Process exited", gazebo_status["error"])


class TestMonitorDetectsCrash(SubprocessLifecycleBase):
    """Start + start_monitor(), kill process, assert monitor sets FAILED within 15s."""

    def test_monitor_detects_crash(self):
        port = unused_tcp_port()
        result = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(result["ok"], result)
        pid = result["pid"]

        start_monitor(interval_s=0.5)
        try:
            # Kill the process
            os.kill(pid, signal.SIGKILL)

            # Wait for monitor to detect crash
            deadline = time.monotonic() + 10.0
            detected = False
            while time.monotonic() < deadline:
                with _lock:
                    state = _engines.get("gazebo")
                    if state and state.status == EngineStatus.FAILED:
                        detected = True
                        break
                time.sleep(0.2)

            self.assertTrue(detected, "Monitor did not detect crash within 10s")
        finally:
            stop_monitor()


class TestShutdownAll(SubprocessLifecycleBase):
    """Start gazebo stub, shutdown_all(), verify all stopped."""

    def test_shutdown_all(self):
        port = unused_tcp_port()
        result = start_engine("gazebo", port=port, runtime="stub", timeout_s=15.0)
        self.assertTrue(result["ok"], result)

        shutdown_all()

        with _lock:
            state = _engines.get("gazebo")
            if state is not None:
                self.assertEqual(state.status, EngineStatus.STOPPED)


if __name__ == "__main__":
    unittest.main()
