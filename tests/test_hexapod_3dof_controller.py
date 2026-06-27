"""Tests for the Hexapod3DOFController (3-DOF IK-based gait)."""
from __future__ import annotations

import math
import unittest

from isaac_bridge.controllers import Hexapod3DOFController
from isaac_bridge.hexapod_ik import LegAngles, LegGeometry, forward_kinematics
from isaac_bridge.models import Controller, TeleopConfig, TeleopState


def _3dof_config(**overrides: object) -> TeleopConfig:
    base = {"controller_type": "hexapod_3dof_tripod"}
    base.update(overrides)
    return TeleopConfig.from_profile(base)


def _state(vx: float = 0.0, yaw: float = 0.0, height: float = 0.0) -> TeleopState:
    return TeleopState(vx_mps=vx, yaw_rate_rps=yaw, body_height_m=height)


class TestProtocolCompliance(unittest.TestCase):
    def test_implements_controller(self) -> None:
        ctrl = Hexapod3DOFController()
        self.assertIsInstance(ctrl, Controller)

    def test_returns_correct_types(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        targets, new_phase = ctrl.compute_targets(_state(vx=0.3), 0.01, cfg, 0.0)
        self.assertIsInstance(targets, dict)
        self.assertIsInstance(new_phase, float)


class TestJointCount(unittest.TestCase):
    def test_returns_18_joints(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        targets, _ = ctrl.compute_targets(_state(vx=0.3), 0.01, cfg, 0.0)
        self.assertEqual(len(targets), 18)
        self.assertEqual(set(targets.keys()), set(cfg.leg_joint_names))

    def test_all_values_finite(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        targets, _ = ctrl.compute_targets(_state(vx=0.3), 0.01, cfg, 0.0)
        for name, val in targets.items():
            self.assertTrue(math.isfinite(val), f"{name} is not finite: {val}")


class TestZeroCommand(unittest.TestCase):
    def test_neutral_stance_phase_frozen(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        _, phase = ctrl.compute_targets(_state(), 0.01, cfg, 1.0)
        self.assertAlmostEqual(phase, 1.0, places=6)

    def test_zero_dt_returns_neutral(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        targets, phase = ctrl.compute_targets(_state(vx=0.5), 0.0, cfg, 0.5)
        self.assertAlmostEqual(phase, 0.5, places=6)
        self.assertEqual(len(targets), 18)

    def test_neutral_stance_consistent(self) -> None:
        """Two calls with zero vx should produce identical targets."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        t1, _ = ctrl.compute_targets(_state(), 0.01, cfg, 0.0)
        t2, _ = ctrl.compute_targets(_state(), 0.01, cfg, 0.0)
        for name in t1:
            self.assertAlmostEqual(t1[name], t2[name], places=8,
                                   msg=f"{name} inconsistent at neutral")


class TestForwardCommand(unittest.TestCase):
    def test_joints_move_from_neutral(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        # Get neutral reference
        neutral, _ = ctrl.compute_targets(_state(), 0.01, cfg, 0.0)

        # New controller for forward motion
        ctrl2 = Hexapod3DOFController()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(100):
            targets, phase = ctrl2.compute_targets(state, 0.01, cfg, phase)

        moved = any(abs(targets[n] - neutral[n]) > 0.001 for n in targets)
        self.assertTrue(moved, "Expected joints to move from neutral with nonzero vx")

    def test_phase_advances(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(200):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        self.assertGreater(phase, 0.1)


class TestTripodPhaseGroups(unittest.TestCase):
    """Tripod groups (A: LF/LR/RM, B: LM/RF/RR) should alternate."""

    def test_groups_differ(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        # Group A (offset=0.0): legs 0, 2, 4 (LF, LR, RM)
        # Group B (offset=0.5): legs 1, 3, 5 (LM, RF, RR)
        # Compare femur angles between groups
        group_a_femurs = [targets[cfg.leg_joint_names[i * 3 + 1]] for i in [0, 2, 4]]
        group_b_femurs = [targets[cfg.leg_joint_names[i * 3 + 1]] for i in [1, 3, 5]]

        avg_a = sum(group_a_femurs) / len(group_a_femurs)
        avg_b = sum(group_b_femurs) / len(group_b_femurs)

        # They should generally differ (one group in swing, other in stance)
        # unless we happen to be at a transition point
        if abs(avg_a) > 0.01 or abs(avg_b) > 0.01:
            self.assertNotAlmostEqual(avg_a, avg_b, places=2,
                                      msg="Tripod groups should differ")


class TestYawDifferential(unittest.TestCase):
    def test_yaw_creates_asymmetry(self) -> None:
        """With yaw, left/right femur angles should differ due to stride differential."""
        ctrl_yaw = Hexapod3DOFController()
        ctrl_straight = Hexapod3DOFController()
        cfg = _3dof_config()

        state_yaw = _state(vx=cfg.vx_max_mps, yaw=cfg.yaw_max_rps)
        state_straight = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(200):
            t_yaw, phase = ctrl_yaw.compute_targets(state_yaw, 0.01, cfg, phase)
        phase = 0.0
        for _ in range(200):
            t_straight, phase = ctrl_straight.compute_targets(state_straight, 0.01, cfg, phase)

        # With yaw, targets should differ from straight walking
        diff = sum(abs(t_yaw[n] - t_straight[n]) for n in t_yaw)
        self.assertGreater(diff, 0.01, "Yaw should change targets vs straight walking")


class TestHeightCommand(unittest.TestCase):
    def test_height_shifts_body(self) -> None:
        ctrl_flat = Hexapod3DOFController()
        ctrl_up = Hexapod3DOFController()
        cfg = _3dof_config()

        phase = 0.0
        for _ in range(200):
            t_flat, phase = ctrl_flat.compute_targets(_state(), 0.01, cfg, phase)
        phase = 0.0
        for _ in range(200):
            t_up, phase = ctrl_up.compute_targets(
                _state(height=cfg.height_max_m), 0.01, cfg, phase,
            )

        # At least one joint should differ
        changed = any(
            abs(t_flat[n] - t_up[n]) > 0.001 for n in t_flat
        )
        self.assertTrue(changed, "Height command should change joint targets")


class TestSlewFiltering(unittest.TestCase):
    def test_slew_limits_step_response(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        ctrl.compute_targets(state, 0.01, cfg, 0.0)
        self.assertLess(ctrl.filtered_vx, 1.0)
        self.assertGreater(ctrl.filtered_vx, 0.0)

    def test_slew_converges(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(1000):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        self.assertAlmostEqual(ctrl.filtered_vx, 1.0, places=2)


class TestPhaseWrapping(unittest.TestCase):
    def test_phase_stays_in_range(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(10000):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        self.assertGreaterEqual(phase, 0.0)
        self.assertLess(phase, 2.0 * math.pi)


class TestJointLimits(unittest.TestCase):
    """All outputs should be within typical joint limits."""

    def test_outputs_within_limits(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps, yaw=cfg.yaw_max_rps * 0.5)
        phase = 0.0
        for _ in range(500):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        for name, val in targets.items():
            self.assertGreater(val, -math.pi, f"{name} below -π")
            self.assertLess(val, math.pi, f"{name} above π")


class TestFKConsistency(unittest.TestCase):
    """Controller outputs should produce reachable foot positions."""

    def test_fk_positions_reachable(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        geom = LegGeometry(l_coxa=cfg.l_coxa, l_femur=cfg.l_femur, l_tibia=cfg.l_tibia)
        max_reach = geom.l_coxa + geom.l_femur + geom.l_tibia
        n_legs = len(cfg.leg_joint_names) // 3

        for leg_idx in range(n_legs):
            base = leg_idx * 3
            angles = LegAngles(
                coxa=targets[cfg.leg_joint_names[base]],
                femur=targets[cfg.leg_joint_names[base + 1]],
                tibia=targets[cfg.leg_joint_names[base + 2]],
            )
            px, py, pz = forward_kinematics(angles, geom)
            dist = math.sqrt(px * px + py * py + pz * pz)
            self.assertLessEqual(
                dist, max_reach + 1e-6,
                f"Leg {leg_idx} foot at distance {dist:.4f} exceeds max reach {max_reach:.4f}",
            )


class TestBodyFrameFootTrajectory(unittest.TestCase):
    """Tests for body-frame foot trajectory (replaces world-frame tests)."""

    def test_stance_targets_change_smoothly(self) -> None:
        """Joint targets should change smoothly between ticks (no jumps)."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        # Warm up
        for _ in range(100):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        prev_targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        max_jump = 0.0
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            for j in cfg.leg_joint_names:
                jump = abs(targets[j] - prev_targets[j])
                max_jump = max(max_jump, jump)
            prev_targets = dict(targets)

        # Max inter-tick change should be < 10 degrees (no wild jumps)
        self.assertLess(
            max_jump, math.radians(10),
            f"Max inter-tick joint jump is {math.degrees(max_jump):.1f}° — too large",
        )

    def test_walking_produces_coxa_oscillation(self) -> None:
        """When walking forward, coxa joints should oscillate (not stay at zero)."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        # Warm up
        for _ in range(100):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        coxa_range = 0.0
        coxa_min = float("inf")
        coxa_max = float("-inf")
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            val = targets[cfg.leg_joint_names[0]]  # LF coxa
            coxa_min = min(coxa_min, val)
            coxa_max = max(coxa_max, val)

        coxa_range = coxa_max - coxa_min
        self.assertGreater(
            coxa_range, math.radians(2),
            f"Coxa range is only {math.degrees(coxa_range):.1f}° — gait not producing motion",
        )

    def test_joint_ranges_reasonable(self) -> None:
        """Joint ranges during walking should be moderate, not hitting limits."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(100):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        ranges: dict[str, tuple[float, float]] = {
            j: (float("inf"), float("-inf")) for j in cfg.leg_joint_names
        }
        for _ in range(200):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            for j, v in targets.items():
                lo, hi = ranges[j]
                ranges[j] = (min(lo, v), max(hi, v))

        # No joint should exceed ±45° from neutral (which is 0 after bias subtraction)
        for j, (lo, hi) in ranges.items():
            self.assertGreater(lo, math.radians(-45),
                               f"{j} goes below -45° ({math.degrees(lo):.1f}°)")
            self.assertLess(hi, math.radians(45),
                            f"{j} goes above 45° ({math.degrees(hi):.1f}°)")


class TestStaticStability(unittest.TestCase):
    """Stance foot count check — with duty_factor >= 0.5 and tripod
    gait, at least 3 legs should be in stance at any time."""

    def test_at_least_3_stance_feet_during_walk(self) -> None:
        """With tripod gait and duty_factor >= 0.5, there should always
        be at least 3 feet in stance (the support triangle)."""
        cfg = _3dof_config()
        phase = 0.0
        # Just check the phase math — no controller internals needed
        min_stance = 6
        for _tick in range(500):
            phase_norm = (phase / (2.0 * math.pi))
            stance_count = 0
            for leg_idx in range(6):
                offset = cfg.leg_phase_offsets[leg_idx]
                leg_phase = (phase_norm + offset) % 1.0
                if leg_phase < cfg.duty_factor:
                    stance_count += 1
            min_stance = min(min_stance, stance_count)
            # Advance phase as the controller would at full speed
            phase = (phase + cfg.stride_hz * 2.0 * math.pi * 1.0 * 0.01) % (2.0 * math.pi)

        self.assertGreaterEqual(
            min_stance, 3,
            f"Minimum stance feet was {min_stance} — need at least 3 for stability",
        )


class TestJointNameLengthValidation(unittest.TestCase):
    """Validate that mismatched leg_joint_names length raises early."""

    def test_wrong_length_raises_value_error(self) -> None:
        ctrl = Hexapod3DOFController()
        # 17 joints instead of 18 (not a multiple of 3)
        bad_names = [f"joint_{i}" for i in range(17)]
        cfg = TeleopConfig(
            controller_type="hexapod_3dof_tripod",
            leg_joint_names=tuple(bad_names),
            dofs_per_leg=3,
        )
        with self.assertRaises(ValueError) as ctx:
            ctrl.compute_targets(_state(vx=0.3), 0.01, cfg, 0.0)
        self.assertIn("leg_joint_names", str(ctx.exception))

    def test_correct_length_does_not_raise(self) -> None:
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        # Should not raise
        targets, _ = ctrl.compute_targets(_state(vx=0.3), 0.01, cfg, 0.0)
        self.assertEqual(len(targets), 18)


class TestBodyWidthYawGain(unittest.TestCase):
    """Verify that body_width is used instead of hardcoded 0.075."""

    def test_wider_body_amplifies_yaw(self) -> None:
        ctrl_narrow = Hexapod3DOFController()
        ctrl_wide = Hexapod3DOFController()
        cfg_narrow = _3dof_config(body_width=0.10)
        cfg_wide = _3dof_config(body_width=0.30)

        state = _state(vx=0.3, yaw=1.0)
        phase = 0.0
        for _ in range(200):
            t_narrow, phase = ctrl_narrow.compute_targets(state, 0.01, cfg_narrow, phase)
        phase = 0.0
        for _ in range(200):
            t_wide, phase = ctrl_wide.compute_targets(state, 0.01, cfg_wide, phase)

        # Wider body should produce different yaw-affected targets
        diff = sum(abs(t_narrow[n] - t_wide[n]) for n in t_narrow)
        self.assertGreater(diff, 0.001,
                           "Different body_width should change yaw behavior")


if __name__ == "__main__":
    unittest.main()
