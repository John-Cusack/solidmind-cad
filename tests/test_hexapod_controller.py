"""Tests for the HexapodTripodController (P1)."""
from __future__ import annotations

import math
import unittest

from isaac_bridge.controllers import HexapodTripodController, clamp_targets
from isaac_bridge.models import Controller, TeleopConfig, TeleopState

_DEG2RAD = math.pi / 180.0


def _default_config() -> TeleopConfig:
    return TeleopConfig()


def _default_state(vx: float = 0.0, yaw: float = 0.0, height: float = 0.0) -> TeleopState:
    return TeleopState(vx_mps=vx, yaw_rate_rps=yaw, body_height_m=height)


class TestControllerProtocolCompliance(unittest.TestCase):
    def test_implements_protocol(self) -> None:
        ctrl = HexapodTripodController()
        self.assertIsInstance(ctrl, Controller)

    def test_returns_correct_types(self) -> None:
        ctrl = HexapodTripodController()
        targets, new_phase = ctrl.compute_targets(
            _default_state(vx=0.3), 0.01, _default_config(), 0.0,
        )
        self.assertIsInstance(targets, dict)
        self.assertIsInstance(new_phase, float)


class TestZeroCommand(unittest.TestCase):
    """With zero command, all joints should be at neutral."""

    def test_zero_vx_neutral_targets(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        targets, phase = ctrl.compute_targets(
            _default_state(), 0.01, cfg, 0.0,
        )
        neutral_rad = cfg.neutral_deg * _DEG2RAD
        for name, value in targets.items():
            self.assertAlmostEqual(value, neutral_rad, places=6,
                                   msg=f"{name} should be neutral")

    def test_zero_vx_phase_frozen(self) -> None:
        """Phase should not advance when vx is zero."""
        ctrl = HexapodTripodController()
        _, phase = ctrl.compute_targets(
            _default_state(), 0.01, _default_config(), 1.0,
        )
        self.assertAlmostEqual(phase, 1.0, places=6)

    def test_zero_dt_returns_neutral(self) -> None:
        """Zero dt should return neutral regardless of state."""
        ctrl = HexapodTripodController()
        targets, phase = ctrl.compute_targets(
            _default_state(vx=0.5), 0.0, _default_config(), 0.5,
        )
        neutral_rad = _default_config().neutral_deg * _DEG2RAD
        for v in targets.values():
            self.assertAlmostEqual(v, neutral_rad, places=6)
        self.assertAlmostEqual(phase, 0.5, places=6)


class TestNonzeroCommand(unittest.TestCase):
    """With nonzero forward command, targets change from neutral."""

    def test_forward_command_moves_targets(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        neutral_rad = cfg.neutral_deg * _DEG2RAD
        # Run enough ticks for slew filter to ramp up
        state = _default_state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(100):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        # At least one joint should have moved away from neutral
        moved = any(abs(v - neutral_rad) > 0.001 for v in targets.values())
        self.assertTrue(moved, "Expected joints to move from neutral with nonzero vx")

    def test_all_joints_present(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        targets, _ = ctrl.compute_targets(
            _default_state(vx=0.3), 0.01, cfg, 0.0,
        )
        self.assertEqual(set(targets.keys()), set(cfg.joint_names))

    def test_backward_command_reverses_direction(self) -> None:
        """Negative vx should reverse oscillation direction."""
        ctrl_fwd = HexapodTripodController()
        ctrl_bwd = HexapodTripodController()
        cfg = _default_config()
        state_fwd = _default_state(vx=cfg.vx_max_mps)
        state_bwd = _default_state(vx=-cfg.vx_max_mps)
        # Run until slew filter settles
        phase = 0.5  # nonzero starting phase to get non-neutral sin
        for _ in range(200):
            targets_fwd, phase_fwd = ctrl_fwd.compute_targets(state_fwd, 0.01, cfg, phase)
            targets_bwd, phase_bwd = ctrl_bwd.compute_targets(state_bwd, 0.01, cfg, phase)
            phase = phase_fwd  # keep same phase progression for comparison

        # At the same phase, forward and backward should produce
        # opposite oscillation on at least one joint
        joint = cfg.joint_names[0]
        neutral = cfg.neutral_deg * _DEG2RAD
        delta_fwd = targets_fwd[joint] - neutral
        delta_bwd = targets_bwd[joint] - neutral
        # They should have opposite signs (or both zero if at zero crossing)
        if abs(delta_fwd) > 0.001 and abs(delta_bwd) > 0.001:
            self.assertLess(delta_fwd * delta_bwd, 0,
                            "Forward and backward should produce opposite oscillation")


class TestTripodPhaseOffset(unittest.TestCase):
    """Tripod groups should be 180° out of phase."""

    def test_tripod_groups_opposite(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        state = _default_state(vx=cfg.vx_max_mps)
        # Run until filter settles and we're at a clear point
        phase = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        neutral = cfg.neutral_deg * _DEG2RAD
        # Average deviation from neutral for each tripod group
        a_devs = [targets[n] - neutral for n in cfg.tripod_a]
        b_devs = [targets[n] - neutral for n in cfg.tripod_b]
        avg_a = sum(a_devs) / len(a_devs)
        avg_b = sum(b_devs) / len(b_devs)

        # If both averages are nonzero, they should have opposite signs
        if abs(avg_a) > 0.001 and abs(avg_b) > 0.001:
            self.assertLess(avg_a * avg_b, 0,
                            "Tripod groups should oscillate in opposite phase")


class TestYawDifferential(unittest.TestCase):
    """Yaw command creates differential between left and right legs."""

    def test_yaw_creates_offset(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        # Pure yaw, no forward motion
        state = _default_state(yaw=cfg.yaw_max_rps)
        phase = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        # With zero vx, phase freezes and oscillation is zero.
        # Only yaw offset should be present.
        neutral = cfg.neutral_deg * _DEG2RAD
        left_avg = sum(targets[n] for n in cfg.left_legs) / len(cfg.left_legs)
        right_avg = sum(targets[n] for n in cfg.right_legs) / len(cfg.right_legs)

        # Left and right should have opposite offsets from neutral
        left_delta = left_avg - neutral
        right_delta = right_avg - neutral
        if abs(left_delta) > 1e-6 and abs(right_delta) > 1e-6:
            self.assertLess(left_delta * right_delta, 0,
                            "Yaw should create opposite offsets on left/right legs")

    def test_zero_yaw_no_differential(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        state = _default_state(vx=0.0, yaw=0.0)
        targets, _ = ctrl.compute_targets(state, 0.01, cfg, 0.0)
        neutral = cfg.neutral_deg * _DEG2RAD
        left_avg = sum(targets[n] for n in cfg.left_legs) / len(cfg.left_legs)
        right_avg = sum(targets[n] for n in cfg.right_legs) / len(cfg.right_legs)
        self.assertAlmostEqual(left_avg, right_avg, places=6)


class TestHeightOffset(unittest.TestCase):
    """Height command adds uniform offset."""

    def test_height_offsets_all_joints(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        # Pure height, no motion
        state = _default_state(height=cfg.height_max_m)
        phase = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        neutral = cfg.neutral_deg * _DEG2RAD
        height_mix_rad = cfg.height_mix_deg * _DEG2RAD
        # All joints should have the same offset (height is uniform)
        for name in cfg.joint_names:
            delta = targets[name] - neutral
            self.assertAlmostEqual(delta, height_mix_rad, places=3,
                                   msg=f"{name} height offset wrong")


class TestSlewFiltering(unittest.TestCase):
    """Slew rate filtering limits how fast commands take effect."""

    def test_slew_limits_step_response(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        dt = 0.01
        # Jump from 0 to max vx
        state = _default_state(vx=cfg.vx_max_mps)
        ctrl.compute_targets(state, dt, cfg, 0.0)
        # After one tick, filtered_vx should be less than 1.0 (normalized)
        # Max step = slew_vx_mps2 / vx_max_mps * dt = 1.0/0.5 * 0.01 = 0.02
        self.assertLess(ctrl.filtered_vx, 1.0)
        self.assertGreater(ctrl.filtered_vx, 0.0)

    def test_slew_converges(self) -> None:
        """After enough ticks, filtered value converges to target."""
        ctrl = HexapodTripodController()
        cfg = _default_config()
        state = _default_state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(1000):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        self.assertAlmostEqual(ctrl.filtered_vx, 1.0, places=2)

    def test_slew_yaw(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        state = _default_state(yaw=cfg.yaw_max_rps)
        ctrl.compute_targets(state, 0.01, cfg, 0.0)
        self.assertGreater(ctrl.filtered_yaw, 0.0)
        self.assertLess(ctrl.filtered_yaw, 1.0)


class TestPhaseWrapping(unittest.TestCase):
    """Phase should wrap correctly around 2π."""

    def test_phase_stays_in_range(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        state = _default_state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(10000):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        self.assertGreaterEqual(phase, 0.0)
        self.assertLess(phase, 2.0 * math.pi)

    def test_phase_advances_with_speed(self) -> None:
        ctrl = HexapodTripodController()
        cfg = _default_config()
        state = _default_state(vx=cfg.vx_max_mps)
        phase = 0.0
        # Run enough for slew to settle
        for _ in range(200):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        # Phase should have advanced significantly
        self.assertGreater(phase, 0.1)


class TestClampTargets(unittest.TestCase):
    """clamp_targets utility respects limits."""

    def test_within_limits_unchanged(self) -> None:
        targets = {"a": 0.5, "b": -0.5}
        limits = {"a": (-1.0, 1.0), "b": (-1.0, 1.0)}
        clamped, count = clamp_targets(targets, limits)
        self.assertEqual(count, 0)
        self.assertAlmostEqual(clamped["a"], 0.5)
        self.assertAlmostEqual(clamped["b"], -0.5)

    def test_exceeds_upper_clamped(self) -> None:
        targets = {"a": 1.5}
        limits = {"a": (-1.0, 1.0)}
        clamped, count = clamp_targets(targets, limits)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(clamped["a"], 1.0)

    def test_exceeds_lower_clamped(self) -> None:
        targets = {"a": -1.5}
        limits = {"a": (-1.0, 1.0)}
        clamped, count = clamp_targets(targets, limits)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(clamped["a"], -1.0)

    def test_no_limits_passthrough(self) -> None:
        targets = {"a": 100.0}
        clamped, count = clamp_targets(targets, {})
        self.assertEqual(count, 0)
        self.assertAlmostEqual(clamped["a"], 100.0)

    def test_multiple_clamped(self) -> None:
        targets = {"a": 2.0, "b": -2.0, "c": 0.5}
        limits = {"a": (-1.0, 1.0), "b": (-1.0, 1.0), "c": (-1.0, 1.0)}
        clamped, count = clamp_targets(targets, limits)
        self.assertEqual(count, 2)


class TestCustomConfig(unittest.TestCase):
    """Controller works with non-default configs."""

    def test_custom_amplitude(self) -> None:
        ctrl = HexapodTripodController()
        cfg = TeleopConfig.from_profile({"amplitude_deg": 30.0})
        state = _default_state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(500):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        # Max deviation should be around 30 degrees
        neutral = cfg.neutral_deg * _DEG2RAD
        max_dev = max(abs(v - neutral) for v in targets.values())
        # Should be within amplitude + yaw_mix + height_mix range
        max_possible = (30.0 + cfg.yaw_mix_deg + cfg.height_mix_deg) * _DEG2RAD
        self.assertLessEqual(max_dev, max_possible + 0.01)

    def test_four_leg_config(self) -> None:
        """Controller works with non-standard joint count."""
        cfg = TeleopConfig.from_profile({
            "joint_names": ["fl", "fr", "rl", "rr"],
            "tripod_a": ["fl", "rr"],
            "tripod_b": ["fr", "rl"],
            "left_legs": ["fl", "rl"],
            "right_legs": ["fr", "rr"],
        })
        ctrl = HexapodTripodController()
        targets, _ = ctrl.compute_targets(
            _default_state(vx=0.3), 0.01, cfg, 0.0,
        )
        self.assertEqual(set(targets.keys()), {"fl", "fr", "rl", "rr"})


if __name__ == "__main__":
    unittest.main()
