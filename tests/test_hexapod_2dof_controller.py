"""Tests for the Hexapod2DOFController."""
from __future__ import annotations

import math
import unittest

from isaac_bridge.controllers import Hexapod2DOFController
from isaac_bridge.models import Controller, TeleopConfig, TeleopState

_DEG2RAD = math.pi / 180.0

# 2-DOF config: 6 legs × 2 joints = 12 joint names
# Rectangular body: 3 left, 3 right, legs point straight outward
_2DOF_JOINT_NAMES = [
    "hip_yaw_L1", "hip_pitch_L1",
    "hip_yaw_L2", "hip_pitch_L2",
    "hip_yaw_L3", "hip_pitch_L3",
    "hip_yaw_R1", "hip_pitch_R1",
    "hip_yaw_R2", "hip_pitch_R2",
    "hip_yaw_R3", "hip_pitch_R3",
]

_PHASE_OFFSETS = [0.0, 0.5, 0.0, 0.5, 0.0, 0.5]

_LEFT_LEGS = ["hip_yaw_L1", "hip_yaw_L2", "hip_yaw_L3"]
_RIGHT_LEGS = ["hip_yaw_R1", "hip_yaw_R2", "hip_yaw_R3"]
_TRIPOD_A = ["hip_yaw_L1", "hip_yaw_L3", "hip_yaw_R2"]
_TRIPOD_B = ["hip_yaw_L2", "hip_yaw_R1", "hip_yaw_R3"]


def _2dof_config(**overrides) -> TeleopConfig:
    profile = {
        "controller_type": "hexapod_2dof_tripod",
        "leg_joint_names": _2DOF_JOINT_NAMES,
        "leg_phase_offsets": _PHASE_OFFSETS,
        "left_legs": _LEFT_LEGS,
        "right_legs": _RIGHT_LEGS,
        "tripod_a": _TRIPOD_A,
        "tripod_b": _TRIPOD_B,
        "dofs_per_leg": 2,
        "amplitude_deg": 18.0,
        "lift_deg": 15.0,
        "stride_hz": 2.0,
        "duty_factor": 0.5,
        **overrides,
    }
    return TeleopConfig.from_profile(profile)


def _state(vx: float = 0.0, yaw: float = 0.0, height: float = 0.0) -> TeleopState:
    return TeleopState(vx_mps=vx, yaw_rate_rps=yaw, body_height_m=height)


class TestProtocol(unittest.TestCase):
    def test_implements_controller(self) -> None:
        ctrl = Hexapod2DOFController()
        self.assertIsInstance(ctrl, Controller)

    def test_returns_correct_types(self) -> None:
        ctrl = Hexapod2DOFController()
        targets, new_phase = ctrl.compute_targets(
            _state(vx=0.2), 0.02, _2dof_config(), 0.0,
        )
        self.assertIsInstance(targets, dict)
        self.assertIsInstance(new_phase, float)

    def test_returns_all_12_joints(self) -> None:
        ctrl = Hexapod2DOFController()
        cfg = _2dof_config()
        targets, _ = ctrl.compute_targets(_state(vx=0.2), 0.02, cfg, 0.0)
        self.assertEqual(set(targets.keys()), set(_2DOF_JOINT_NAMES))


class TestZeroCommand(unittest.TestCase):
    def test_zero_vx_all_neutral(self) -> None:
        ctrl = Hexapod2DOFController()
        cfg = _2dof_config()
        targets, _ = ctrl.compute_targets(_state(), 0.02, cfg, 0.0)
        for name, val in targets.items():
            self.assertAlmostEqual(val, 0.0, places=6, msg=f"{name} should be 0")

    def test_zero_vx_phase_frozen(self) -> None:
        ctrl = Hexapod2DOFController()
        _, phase = ctrl.compute_targets(_state(), 0.02, _2dof_config(), 1.0)
        self.assertAlmostEqual(phase, 1.0, places=6)


class TestFemurLift(unittest.TestCase):
    """Femur should lift during swing and stay neutral during stance."""

    def test_femur_lifts_during_swing(self) -> None:
        ctrl = Hexapod2DOFController()
        cfg = _2dof_config()
        # Warm up slew filter with several ticks at full speed
        phase = 0.0
        for _ in range(100):
            _, phase = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)

        # Collect femur targets over a full gait cycle
        max_femur = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)
            for name in _2DOF_JOINT_NAMES:
                if "pitch" in name:
                    max_femur = max(max_femur, targets[name])

        # Femur should lift significantly (at least half of lift_deg)
        half_lift = cfg.lift_deg * _DEG2RAD * 0.5
        self.assertGreater(max_femur, half_lift,
                           "Femur should lift during swing phase")

    def test_femur_never_negative(self) -> None:
        """Femur lift should always be >= 0 (lift up, never push down)."""
        ctrl = Hexapod2DOFController()
        cfg = _2dof_config()
        phase = 0.0
        for _ in range(100):
            _, phase = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)

        for _ in range(200):
            targets, phase = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)
            for name in _2DOF_JOINT_NAMES:
                if "pitch" in name:
                    self.assertGreaterEqual(targets[name], -1e-9,
                                            f"{name} should never be negative")


class TestTripodAlternation(unittest.TestCase):
    """Tripod A and B should have opposite coxa phases."""

    def test_coxa_phase_opposition(self) -> None:
        ctrl = Hexapod2DOFController()
        cfg = _2dof_config()
        # Warm up
        phase = 0.0
        for _ in range(100):
            _, phase = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)

        targets, _ = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)

        # L1 (offset=0.0) and L2 (offset=0.5) should have opposite coxa signs
        l1_coxa = targets["hip_yaw_L1"]
        l2_coxa = targets["hip_yaw_L2"]
        # They should generally have opposite signs (or one near zero)
        if abs(l1_coxa) > 0.01 and abs(l2_coxa) > 0.01:
            self.assertLess(l1_coxa * l2_coxa, 0.0,
                            "Tripod A and B coxa should oppose")


class TestLeftRightMirror(unittest.TestCase):
    """Left and right coxa should mirror via explicit config lists."""

    def test_same_phase_legs_have_negated_oscillation(self) -> None:
        """L1 (offset=0.0) and R1 (offset=0.5) have different phases,
        so compare legs with the SAME phase offset: L1 (0.0) vs R2 (0.5)
        won't match either.  Instead, run with all-zero phase offsets
        so left and right at the same phase should produce negated coxa."""
        ctrl = Hexapod2DOFController()
        # All legs at same phase — isolates the left/right negation
        cfg = _2dof_config(leg_phase_offsets=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        phase = 0.0
        for _ in range(100):
            _, phase = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)

        targets, _ = ctrl.compute_targets(_state(vx=0.3), 0.02, cfg, phase)

        # With same phase, left and right coxa should be negated
        for i in range(1, 4):
            l_val = targets[f"hip_yaw_L{i}"]
            r_val = targets[f"hip_yaw_R{i}"]
            if abs(l_val) > 0.01:
                self.assertAlmostEqual(
                    l_val, -r_val, places=6,
                    msg=f"L{i} and R{i} coxa should be negated",
                )


class TestYawDifferential(unittest.TestCase):
    def test_yaw_creates_left_right_difference(self) -> None:
        ctrl = Hexapod2DOFController()
        cfg = _2dof_config()
        # Warm up with yaw command
        phase = 0.0
        for _ in range(100):
            _, phase = ctrl.compute_targets(_state(vx=0.1, yaw=0.5), 0.02, cfg, phase)

        targets, _ = ctrl.compute_targets(_state(vx=0.1, yaw=0.5), 0.02, cfg, phase)

        # Sum of left coxa vs right coxa should differ
        left_sum = sum(targets[f"hip_yaw_L{i}"] for i in range(1, 4))
        right_sum = sum(targets[f"hip_yaw_R{i}"] for i in range(1, 4))
        self.assertNotAlmostEqual(left_sum, right_sum, places=3)


class TestDtZero(unittest.TestCase):
    def test_dt_zero_returns_neutral(self) -> None:
        ctrl = Hexapod2DOFController()
        targets, phase = ctrl.compute_targets(_state(vx=0.3), 0.0, _2dof_config(), 1.5)
        for val in targets.values():
            self.assertAlmostEqual(val, 0.0, places=6)
        self.assertAlmostEqual(phase, 1.5, places=6)


if __name__ == "__main__":
    unittest.main()
