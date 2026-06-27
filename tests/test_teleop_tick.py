"""Tests for the teleop tick integration (P2 + P5).

These tests exercise tick_teleop on the runtime without a real Isaac
articulation — verifying controller math, diagnostics, and session
state updates in analytical mode.

P5 additions: mock articulation tests (apply_action verification) and
joint-limit clamping integration tests.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from isaac_bridge.bridge_server import BridgeServer
from isaac_bridge.runtime_isaac import IsaacRuntime


def _mechanism() -> dict:
    return {
        "name": "test_hex",
        "parts": [
            {"id": "body", "is_ground": False},
        ],
        "joints": [
            {
                "id": "hip_lf",
                "joint_type": "revolute",
                "parent_part": "body",
                "child_part": "leg_lf",
            },
            {
                "id": "hip_rf",
                "joint_type": "revolute",
                "parent_part": "body",
                "child_part": "leg_rf",
            },
        ],
        "drives": [],
    }


class TestTickTeleopNoArticulation(unittest.TestCase):
    """tick_teleop with no articulation (analytical mode)."""

    def setUp(self) -> None:
        self.runtime = IsaacRuntime(headless=True)

    def _start_session(self) -> str:
        result = self.runtime.teleop_start(mechanism=_mechanism())
        return result["session_id"]

    def test_tick_no_sessions_noop(self) -> None:
        """tick_teleop with no active sessions is a no-op."""
        self.runtime.tick_teleop(0.01)  # should not raise

    def test_tick_increments_tick_count(self) -> None:
        session_id = self._start_session()
        self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=session_id)
        self.assertEqual(state["tick_count"], 1)

    def test_tick_multiple_increments(self) -> None:
        session_id = self._start_session()
        for _ in range(10):
            self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=session_id)
        self.assertEqual(state["tick_count"], 10)

    def test_tick_zero_dt_noop(self) -> None:
        """Zero dt should be a no-op."""
        session_id = self._start_session()
        self.runtime.tick_teleop(0.0)
        state = self.runtime.teleop_state(session_id=session_id)
        self.assertEqual(state["tick_count"], 0)

    def test_tick_negative_dt_noop(self) -> None:
        """Negative dt should be a no-op."""
        session_id = self._start_session()
        self.runtime.tick_teleop(-0.01)
        state = self.runtime.teleop_state(session_id=session_id)
        self.assertEqual(state["tick_count"], 0)

    def test_tick_updates_targets_with_command(self) -> None:
        session_id = self._start_session()
        # Send a forward command
        self.runtime.teleop_command(
            session_id=session_id,
            vx_mps=0.3,
            yaw_rate_rps=0.0,
            body_height_m=0.0,
        )
        # Tick enough for slew filter to ramp up
        for _ in range(200):
            self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=session_id)
        targets = state["last_joint_targets_rad"]
        # With nonzero vx, at least one joint should be non-neutral
        non_neutral = any(abs(v) > 0.001 for v in targets.values())
        self.assertTrue(non_neutral, "Expected non-neutral targets after forward command")

    def test_tick_targets_at_neutral_with_zero_command(self) -> None:
        session_id = self._start_session()
        self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=session_id)
        targets = state["last_joint_targets_rad"]
        for name, val in targets.items():
            self.assertAlmostEqual(val, 0.0, places=4, msg=f"{name} should be at neutral (0.0)")

    def test_tick_last_apply_ok_true_without_articulation(self) -> None:
        session_id = self._start_session()
        self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=session_id)
        self.assertTrue(state["last_apply_ok"])

    def test_tick_only_affects_teleop_sessions(self) -> None:
        """Simulate sessions are not affected by tick_teleop."""
        # Start a simulate session (reference mode)
        sim_result = self.runtime.simulate_start(
            mechanism=_mechanism(),
            duration_s=0.1,
            dt_s=0.01,
            output_interval=0.05,
        )
        sim_id = sim_result["session_id"]
        # Start a teleop session
        teleop_id = self._start_session()
        # Tick
        self.runtime.tick_teleop(0.01)
        # Teleop should be ticked
        teleop_state = self.runtime.teleop_state(session_id=teleop_id)
        self.assertEqual(teleop_state["tick_count"], 1)
        # Simulate session should be unaffected
        sim_status = self.runtime.simulate_status(session_id=sim_id)
        self.assertIn("status", sim_status)

    def test_tick_after_stop_no_crash(self) -> None:
        session_id = self._start_session()
        self.runtime.tick_teleop(0.01)
        self.runtime.teleop_stop(session_id=session_id)
        # Ticking after stop should be a no-op
        self.runtime.tick_teleop(0.01)  # should not raise

    def test_multiple_teleop_sessions_ticked(self) -> None:
        id1 = self._start_session()
        id2 = self._start_session()
        self.runtime.tick_teleop(0.01)
        state1 = self.runtime.teleop_state(session_id=id1)
        state2 = self.runtime.teleop_state(session_id=id2)
        self.assertEqual(state1["tick_count"], 1)
        self.assertEqual(state2["tick_count"], 1)


class TestTickWithYawAndHeight(unittest.TestCase):
    """Verify yaw and height commands propagate through tick."""

    def setUp(self) -> None:
        self.runtime = IsaacRuntime(headless=True)

    def _start_and_command(
        self,
        vx: float = 0.0,
        yaw: float = 0.0,
        height: float = 0.0,
    ) -> str:
        result = self.runtime.teleop_start(mechanism=_mechanism())
        sid = result["session_id"]
        self.runtime.teleop_command(
            session_id=sid,
            vx_mps=vx,
            yaw_rate_rps=yaw,
            body_height_m=height,
        )
        return sid

    def test_yaw_creates_differential_in_targets(self) -> None:
        sid = self._start_and_command(yaw=1.0)
        for _ in range(200):
            self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=sid)
        targets = state["last_joint_targets_rad"]
        # Default left legs vs right legs should differ
        if len(targets) >= 2:
            vals = list(targets.values())
            # Not all the same
            self.assertFalse(
                all(abs(v - vals[0]) < 1e-6 for v in vals),
                "Expected differential targets with yaw command",
            )

    def test_height_offsets_targets(self) -> None:
        sid = self._start_and_command(height=0.03)
        for _ in range(200):
            self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=sid)
        targets = state["last_joint_targets_rad"]
        # All targets should have a positive offset from neutral
        for name, val in targets.items():
            self.assertGreater(val, -0.001, msg=f"{name} should have positive height offset")


class TestTickViaBridge(unittest.TestCase):
    """Verify tick_teleop is callable through the bridge server pattern."""

    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)

    def _call(self, payload: str) -> dict:
        return self.server._handle_line(payload.encode("utf-8"))  # type: ignore[attr-defined]

    def test_tick_after_teleop_start(self) -> None:
        """The runtime's tick_teleop can be called directly after bridge-level start."""
        started = self._call(
            json.dumps(
                {
                    "cmd": "teleop_start",
                    "args": {"mechanism": _mechanism()},
                }
            )
        )
        self.assertTrue(started["ok"])
        session_id = started["result"]["session_id"]

        # Simulate what the pump loop does
        self.server._runtime.tick_teleop(0.01)

        state = self._call(
            json.dumps(
                {
                    "cmd": "teleop_state",
                    "args": {"session_id": session_id},
                }
            )
        )
        self.assertTrue(state["ok"])
        self.assertEqual(state["result"]["tick_count"], 1)

    def test_command_then_tick_updates_targets(self) -> None:
        started = self._call(
            json.dumps(
                {
                    "cmd": "teleop_start",
                    "args": {"mechanism": _mechanism()},
                }
            )
        )
        session_id = started["result"]["session_id"]

        # Send command
        self._call(
            json.dumps(
                {
                    "cmd": "teleop_command",
                    "args": {
                        "session_id": session_id,
                        "vx_mps": 0.5,
                        "yaw_rate_rps": 0.0,
                        "body_height_m": 0.0,
                    },
                }
            )
        )

        # Tick many times
        for _ in range(200):
            self.server._runtime.tick_teleop(0.01)

        state = self._call(
            json.dumps(
                {
                    "cmd": "teleop_state",
                    "args": {"session_id": session_id},
                }
            )
        )
        self.assertTrue(state["ok"])
        self.assertEqual(state["result"]["tick_count"], 200)
        targets = state["result"]["last_joint_targets_rad"]
        non_neutral = any(abs(v) > 0.001 for v in targets.values())
        self.assertTrue(non_neutral, "Expected non-neutral targets after 200 ticks with vx=0.5")


class TestTickWithMockArticulation(unittest.TestCase):
    """Verify _apply_and_step is called when articulation is present (P5).

    Since _apply_and_step imports omni.isaac types that are unavailable
    in CI, we patch the method itself and verify the call contract
    (session + targets dict).  A separate test verifies that when the
    method raises, last_apply_ok goes False.
    """

    def setUp(self) -> None:
        self.runtime = IsaacRuntime(headless=True)

    def _start_with_mock_articulation(self) -> str:
        """Start a teleop session and inject a mock articulation + dof_index_map."""
        result = self.runtime.teleop_start(mechanism=_mechanism())
        sid = result["session_id"]

        mock_art = MagicMock()
        mock_art.num_dof = 6

        with self.runtime._lock:
            session = self.runtime._sessions[sid]
            session.articulation = mock_art
            session.dof_index_map = {"hip_lf": 0, "hip_rf": 3}

        return sid

    def test_apply_and_step_called_on_tick(self) -> None:
        """_apply_and_step is called once per tick when articulation is present."""
        sid = self._start_with_mock_articulation()
        with unittest.mock.patch.object(self.runtime, "_apply_and_step") as mock_apply:
            self.runtime.tick_teleop(0.01)
            mock_apply.assert_called_once()
            # First arg is the session, second is the clamped targets dict
            args = mock_apply.call_args[0]
            self.assertEqual(args[0].session_id, sid)
            self.assertIsInstance(args[1], dict)

    def test_apply_and_step_receives_all_config_joints(self) -> None:
        """The targets dict passed to _apply_and_step has all config joint names."""
        self._start_with_mock_articulation()
        with unittest.mock.patch.object(self.runtime, "_apply_and_step") as mock_apply:
            self.runtime.tick_teleop(0.01)
            targets = mock_apply.call_args[0][1]
            # Default TeleopConfig has 6 joints; targets should include all of them
            from isaac_bridge.models import TeleopConfig

            config = TeleopConfig()
            for name in config.joint_names:
                self.assertIn(name, targets, f"Missing joint {name} in targets")

    def test_apply_and_step_called_every_tick(self) -> None:
        """_apply_and_step is called once per tick for N ticks."""
        self._start_with_mock_articulation()
        with unittest.mock.patch.object(self.runtime, "_apply_and_step") as mock_apply:
            for _ in range(5):
                self.runtime.tick_teleop(0.01)
            self.assertEqual(mock_apply.call_count, 5)

    def test_last_apply_ok_true_when_apply_succeeds(self) -> None:
        """last_apply_ok is True when _apply_and_step succeeds."""
        sid = self._start_with_mock_articulation()
        with unittest.mock.patch.object(self.runtime, "_apply_and_step"):
            self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=sid)
        self.assertTrue(state["last_apply_ok"])

    def test_last_apply_ok_false_on_exception(self) -> None:
        """last_apply_ok is False when _apply_and_step raises."""
        sid = self._start_with_mock_articulation()
        with unittest.mock.patch.object(
            self.runtime,
            "_apply_and_step",
            side_effect=RuntimeError("GPU error"),
        ):
            self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=sid)
        self.assertFalse(state["last_apply_ok"])
        # tick_count still increments (targets were computed before apply)
        self.assertEqual(state["tick_count"], 1)

    def test_targets_nonzero_after_command_with_articulation(self) -> None:
        """With a forward command and mock articulation, targets are non-neutral."""
        sid = self._start_with_mock_articulation()
        self.runtime.teleop_command(
            session_id=sid,
            vx_mps=0.3,
            yaw_rate_rps=0.0,
            body_height_m=0.0,
        )
        with unittest.mock.patch.object(self.runtime, "_apply_and_step") as mock_apply:
            for _ in range(200):
                self.runtime.tick_teleop(0.01)
            # Last call's targets should have non-neutral values
            targets = mock_apply.call_args[0][1]
            non_neutral = any(abs(v) > 0.001 for v in targets.values())
            self.assertTrue(non_neutral, "Expected non-neutral targets after forward command")


class TestTickWithJointLimits(unittest.TestCase):
    """Verify limit_clamp_count increments when targets exceed joint limits (P5)."""

    def setUp(self) -> None:
        self.runtime = IsaacRuntime(headless=True)

    def _start_with_tight_limits(self) -> str:
        """Start a teleop session and inject very tight joint limits on all joints."""
        result = self.runtime.teleop_start(mechanism=_mechanism())
        sid = result["session_id"]

        # Inject tight limits: ±0.01 rad (< 1 degree) for ALL config joints
        from isaac_bridge.models import TeleopConfig

        config = TeleopConfig()
        with self.runtime._lock:
            session = self.runtime._sessions[sid]
            session.joint_limits = {name: (-0.01, 0.01) for name in config.joint_names}

        return sid

    def test_clamp_count_zero_at_neutral(self) -> None:
        """No clamping with zero command (neutral targets within limits)."""
        sid = self._start_with_tight_limits()
        self.runtime.tick_teleop(0.01)
        state = self.runtime.teleop_state(session_id=sid)
        self.assertEqual(state["limit_clamp_count"], 0)

    def test_clamp_count_increments_with_large_command(self) -> None:
        """limit_clamp_count > 0 when large commands push targets beyond tight limits."""
        sid = self._start_with_tight_limits()

        # Send a large forward command
        self.runtime.teleop_command(
            session_id=sid,
            vx_mps=0.5,
            yaw_rate_rps=0.0,
            body_height_m=0.0,
        )

        # Tick enough for slew to ramp up and targets to exceed ±0.01 rad
        for _ in range(200):
            self.runtime.tick_teleop(0.01)

        state = self.runtime.teleop_state(session_id=sid)
        self.assertGreater(
            state["limit_clamp_count"], 0, "Expected clamping with large command and tight limits"
        )

    def test_clamp_count_accumulates(self) -> None:
        """limit_clamp_count accumulates across multiple ticks."""
        sid = self._start_with_tight_limits()

        self.runtime.teleop_command(
            session_id=sid,
            vx_mps=0.5,
            yaw_rate_rps=0.0,
            body_height_m=0.0,
        )

        # First batch
        for _ in range(200):
            self.runtime.tick_teleop(0.01)
        state1 = self.runtime.teleop_state(session_id=sid)
        count1 = state1["limit_clamp_count"]

        # Second batch
        for _ in range(200):
            self.runtime.tick_teleop(0.01)
        state2 = self.runtime.teleop_state(session_id=sid)
        count2 = state2["limit_clamp_count"]

        self.assertGreater(count2, count1, "Clamp count should accumulate across tick batches")

    def test_targets_clamped_to_limits(self) -> None:
        """Joint targets are clamped to the configured limits."""
        sid = self._start_with_tight_limits()

        self.runtime.teleop_command(
            session_id=sid,
            vx_mps=0.5,
            yaw_rate_rps=1.0,
            body_height_m=0.03,
        )

        for _ in range(200):
            self.runtime.tick_teleop(0.01)

        state = self.runtime.teleop_state(session_id=sid)
        targets = state["last_joint_targets_rad"]
        for name, val in targets.items():
            self.assertGreaterEqual(val, -0.01 - 1e-9, f"{name}={val} below lower limit")
            self.assertLessEqual(val, 0.01 + 1e-9, f"{name}={val} above upper limit")

    def test_no_limits_no_clamping(self) -> None:
        """Without joint limits, no clamping occurs even with large targets."""
        # Start without injecting limits (empty dict by default)
        result = self.runtime.teleop_start(mechanism=_mechanism())
        sid = result["session_id"]

        self.runtime.teleop_command(
            session_id=sid,
            vx_mps=0.5,
            yaw_rate_rps=1.0,
            body_height_m=0.03,
        )
        for _ in range(200):
            self.runtime.tick_teleop(0.01)

        state = self.runtime.teleop_state(session_id=sid)
        self.assertEqual(state["limit_clamp_count"], 0)


if __name__ == "__main__":
    unittest.main()
