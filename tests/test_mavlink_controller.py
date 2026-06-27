"""Unit tests for ``server.mavlink_controller.MavlinkController``.

Tests stub the pymavlink connection via the ``connect_factory`` injection
seam so they run without a real PX4.  E2E tests against a live PX4
process live in ``tests/test_gazebo_px4_real_runtime.py`` (Phase 2's
real-runtime gate).
"""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from server.mavlink_controller import (
    MavlinkController,
    MavlinkError,
)


@dataclass
class _FakeMessage:
    """Lightweight stand-in for a pymavlink message object."""

    msg_type: str
    fields: dict[str, Any] = field(default_factory=dict)

    def get_type(self) -> str:
        return self.msg_type

    def __getattr__(self, item: str) -> Any:
        if item in self.fields:
            return self.fields[item]
        raise AttributeError(item)


class _FakeConnection:
    """Minimal in-memory replacement for ``mavutil.mavlink_connection``.

    Records sent commands so tests can assert on them, and lets tests
    inject inbound messages (HEARTBEAT, COMMAND_ACK, LOCAL_POSITION_NED)
    via ``inject_message``.
    """

    def __init__(self, target_system: int = 1, target_component: int = 1) -> None:
        self.target_system = target_system
        self.target_component = target_component

        self.mav = MagicMock()
        self._inbox: list[_FakeMessage] = []
        self._inbox_cv = threading.Condition()
        self._closed = False

    def inject_message(self, msg: _FakeMessage) -> None:
        with self._inbox_cv:
            self._inbox.append(msg)
            self._inbox_cv.notify_all()

    def wait_heartbeat(self, timeout: float | None = None) -> _FakeMessage | None:
        deadline = time.monotonic() + (timeout or 0.0)
        with self._inbox_cv:
            while True:
                for i, m in enumerate(self._inbox):
                    if m.msg_type == "HEARTBEAT":
                        self._inbox.pop(i)
                        return m
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._inbox_cv.wait(timeout=remaining)

    def recv_match(
        self,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> _FakeMessage | None:
        deadline = time.monotonic() + (timeout or 0.0)
        with self._inbox_cv:
            while True:
                if self._inbox:
                    return self._inbox.pop(0)
                if not blocking:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._inbox_cv.wait(timeout=min(0.1, remaining))

    def close(self) -> None:
        self._closed = True


def _factory_for(conn: _FakeConnection):
    """Return a ``connect_factory`` callable that yields ``conn``."""

    def factory(url: str, ss: int, sc: int) -> _FakeConnection:
        return conn

    return factory


class TestMavlinkControllerConnect(unittest.TestCase):
    def test_connect_waits_for_heartbeat_and_starts_rx(self) -> None:
        fake = _FakeConnection(target_system=1, target_component=1)
        fake.inject_message(
            _FakeMessage(
                "HEARTBEAT",
                {"base_mode": 0, "custom_mode": 0},
            )
        )
        ctrl = MavlinkController(connect_factory=_factory_for(fake))
        ctrl.connect(timeout_s=1.0)
        try:
            self.assertTrue(ctrl.is_connected())
            tel = ctrl.get_telemetry()
            self.assertGreater(tel.last_heartbeat_ts, 0)
            self.assertFalse(tel.armed)
        finally:
            ctrl.disconnect()

    def test_connect_raises_on_heartbeat_timeout(self) -> None:
        fake = _FakeConnection()  # no heartbeat injected
        ctrl = MavlinkController(connect_factory=_factory_for(fake))
        with self.assertRaises(MavlinkError) as cm:
            ctrl.connect(timeout_s=0.1)
        self.assertEqual(cm.exception.code, "HEARTBEAT_TIMEOUT")

    def test_disconnect_is_safe_when_not_connected(self) -> None:
        ctrl = MavlinkController()
        # Should not raise.
        ctrl.disconnect()


class TestMavlinkControllerCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _FakeConnection(target_system=1, target_component=1)
        self.fake.inject_message(_FakeMessage("HEARTBEAT", {"base_mode": 0}))
        self.ctrl = MavlinkController(connect_factory=_factory_for(self.fake))
        self.ctrl.connect(timeout_s=1.0)

    def tearDown(self) -> None:
        self.ctrl.disconnect()

    def _ack(self, command: int, result: int = 0) -> None:
        """Inject a COMMAND_ACK after a tiny delay so the rx loop sees it."""

        def deliver() -> None:
            time.sleep(0.05)
            self.fake.inject_message(
                _FakeMessage(
                    "COMMAND_ACK",
                    {"command": command, "result": result},
                )
            )

        threading.Thread(target=deliver, daemon=True).start()

    def test_arm_sends_command_long_and_waits_for_ack(self) -> None:
        self._ack(400)
        self.ctrl.arm(timeout_s=2.0)
        self.fake.mav.command_long_send.assert_called()
        args = self.fake.mav.command_long_send.call_args.args
        # Args: tgt_sys, tgt_comp, command, confirmation, p1..p7
        self.assertEqual(args[0], 1)  # target_system
        self.assertEqual(args[1], 1)  # target_component
        self.assertEqual(args[2], 400)  # MAV_CMD_COMPONENT_ARM_DISARM
        self.assertEqual(args[4], 1.0)  # param1 = 1 (arm)
        # PX4 v1.17 SITL requires force-arm (param2=21196) by default.
        self.assertEqual(args[5], 21196.0)

    def test_arm_without_force_sends_zero_param2(self) -> None:
        self._ack(400)
        self.ctrl.arm(timeout_s=2.0, force=False)
        args = self.fake.mav.command_long_send.call_args.args
        self.assertEqual(args[5], 0.0)  # param2 = 0 (no force)

    def test_takeoff_via_mode_switches_to_auto_takeoff(self) -> None:
        self._ack(176)
        self.ctrl.takeoff_via_mode(timeout_s=2.0)
        args = self.fake.mav.command_long_send.call_args.args
        self.assertEqual(args[2], 176)  # MAV_CMD_DO_SET_MODE
        self.assertEqual(args[4], 1.0)  # CUSTOM_MODE_ENABLED
        self.assertEqual(args[5], 4.0)  # AUTO main mode
        self.assertEqual(args[6], 2.0)  # AUTO_TAKEOFF sub mode

    def test_land_via_mode_switches_to_auto_land(self) -> None:
        self._ack(176)
        self.ctrl.land_via_mode(timeout_s=2.0)
        args = self.fake.mav.command_long_send.call_args.args
        self.assertEqual(args[2], 176)
        self.assertEqual(args[5], 4.0)  # AUTO main
        self.assertEqual(args[6], 6.0)  # AUTO_LAND sub

    def test_disarm_sets_param1_to_zero(self) -> None:
        self._ack(400)
        self.ctrl.disarm(timeout_s=2.0)
        args = self.fake.mav.command_long_send.call_args.args
        self.assertEqual(args[2], 400)
        self.assertEqual(args[4], 0.0)

    def test_takeoff_passes_alt_in_param7(self) -> None:
        self._ack(22)
        self.ctrl.takeoff(7.5, timeout_s=2.0)
        args = self.fake.mav.command_long_send.call_args.args
        self.assertEqual(args[2], 22)  # MAV_CMD_NAV_TAKEOFF
        self.assertEqual(args[10], 7.5)  # param7

    def test_land_sends_correct_command(self) -> None:
        self._ack(21)
        self.ctrl.land(timeout_s=2.0)
        args = self.fake.mav.command_long_send.call_args.args
        self.assertEqual(args[2], 21)  # MAV_CMD_NAV_LAND

    def test_command_rejected_raises(self) -> None:
        self._ack(400, result=4)  # MAV_RESULT_FAILED
        with self.assertRaises(MavlinkError) as cm:
            self.ctrl.arm(timeout_s=2.0)
        self.assertEqual(cm.exception.code, "COMMAND_REJECTED")

    def test_command_ack_timeout_raises(self) -> None:
        # No ACK injected.
        with self.assertRaises(MavlinkError) as cm:
            self.ctrl.arm(timeout_s=0.1)
        self.assertEqual(cm.exception.code, "ACK_TIMEOUT")


class TestMavlinkControllerSetpointStream(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _FakeConnection()
        self.fake.inject_message(_FakeMessage("HEARTBEAT", {}))
        self.ctrl = MavlinkController(connect_factory=_factory_for(self.fake))
        # Bump rate so tests are quick.
        self.ctrl._SETPOINT_RATE_HZ = 100.0  # noqa: SLF001
        self.ctrl.connect(timeout_s=1.0)

    def tearDown(self) -> None:
        self.ctrl.stop_setpoint_stream()
        self.ctrl.disconnect()

    def test_setpoint_stream_publishes_velocity_setpoints(self) -> None:
        self.ctrl.set_velocity(0.5, -0.25, 0.1, 0.2)
        self.ctrl.start_setpoint_stream()
        # Wait for at least 3 ticks at 100 Hz.
        time.sleep(0.2)
        self.assertGreater(
            self.fake.mav.set_position_target_local_ned_send.call_count,
            2,
        )
        # Validate the velocity fields of the most recent send.
        args = self.fake.mav.set_position_target_local_ned_send.call_args.args
        # Args layout (from pymavlink):
        # time_boot_ms, tgt_sys, tgt_comp, frame, type_mask,
        # x, y, z, vx, vy, vz, afx, afy, afz, yaw, yaw_rate
        self.assertAlmostEqual(args[8], 0.5)
        self.assertAlmostEqual(args[9], -0.25)
        self.assertAlmostEqual(args[10], 0.1)
        self.assertAlmostEqual(args[15], 0.2)

    def test_set_velocity_updates_active_stream(self) -> None:
        self.ctrl.set_velocity(0.0, 0.0, 0.0, 0.0)
        self.ctrl.start_setpoint_stream()
        time.sleep(0.1)
        first_call_args = self.fake.mav.set_position_target_local_ned_send.call_args.args
        self.assertAlmostEqual(first_call_args[8], 0.0)

        # Update setpoint mid-stream.
        self.ctrl.set_velocity(2.0, 0.0, 0.0, 0.0)
        time.sleep(0.1)
        latest_args = self.fake.mav.set_position_target_local_ned_send.call_args.args
        self.assertAlmostEqual(latest_args[8], 2.0)

    def test_start_setpoint_stream_requires_connection(self) -> None:
        ctrl = MavlinkController(connect_factory=_factory_for(self.fake))
        # Not connected yet.
        with self.assertRaises(MavlinkError) as cm:
            ctrl.start_setpoint_stream()
        self.assertEqual(cm.exception.code, "NOT_CONNECTED")


class TestMavlinkControllerTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _FakeConnection()
        self.fake.inject_message(_FakeMessage("HEARTBEAT", {}))
        self.ctrl = MavlinkController(connect_factory=_factory_for(self.fake))
        self.ctrl.connect(timeout_s=1.0)

    def tearDown(self) -> None:
        self.ctrl.disconnect()

    def test_local_position_ned_message_updates_telemetry(self) -> None:
        self.fake.inject_message(
            _FakeMessage(
                "LOCAL_POSITION_NED",
                {"x": 1.0, "y": 2.0, "z": -3.0, "vx": 0.1, "vy": 0.2, "vz": 0.3},
            )
        )
        # Allow rx loop to drain the message.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            tel = self.ctrl.get_telemetry()
            if tel.local_position is not None:
                break
            time.sleep(0.01)

        tel = self.ctrl.get_telemetry()
        self.assertEqual(tel.local_position, (1.0, 2.0, -3.0))
        self.assertEqual(tel.local_velocity, (0.1, 0.2, 0.3))

    def test_armed_flag_reflects_heartbeat_base_mode(self) -> None:
        # MAV_MODE_FLAG_SAFETY_ARMED == 128
        self.fake.inject_message(_FakeMessage("HEARTBEAT", {"base_mode": 128}))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.ctrl.get_telemetry().armed:
                break
            time.sleep(0.01)
        self.assertTrue(self.ctrl.get_telemetry().armed)


if __name__ == "__main__":
    unittest.main()
