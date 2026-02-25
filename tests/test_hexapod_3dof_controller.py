"""Tests for the Hexapod3DOFController (3-DOF IK-based gait)."""
from __future__ import annotations

import math
import unittest

from isaac_bridge.controllers import Hexapod3DOFController
from isaac_bridge.hexapod_ik import LegGeometry, forward_kinematics, LegAngles
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


class TestWorldFrameFootPlanting(unittest.TestCase):
    """Tests for the world-frame foot planting fix."""

    def test_stance_foot_within_stride_of_default(self) -> None:
        """When walking forward, a stance foot in body frame should be
        within one stride length of its default position (Raibert placement
        puts feet slightly ahead at start-of-stance, slightly behind at
        end-of-stance)."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        # Run enough ticks for slew to converge and gait to cycle
        for _ in range(300):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        assert ctrl._default_feet is not None
        assert ctrl._foot_plant_world is not None
        from isaac_bridge.controllers import _world_to_body
        found_stance = False
        for leg_idx in range(6):
            offset = cfg.leg_phase_offsets[leg_idx]
            leg_phase = (phase / (2.0 * math.pi) + offset) % 1.0
            if leg_phase < cfg.duty_factor:
                plant_w = ctrl._foot_plant_world[leg_idx]
                plant_b = _world_to_body(
                    plant_w[0], plant_w[1], plant_w[2],
                    ctrl._body_x, ctrl._body_y, ctrl._body_yaw,
                )
                default = ctrl._default_feet[leg_idx]
                # Foot should be within one stride of default position
                dx = abs(plant_b[0] - default[0])
                self.assertLess(
                    dx, cfg.stride_length + 0.01,
                    f"Leg {leg_idx} stance foot too far from default "
                    f"(offset={dx:.4f}m, max={cfg.stride_length}m)",
                )
                found_stance = True
        self.assertTrue(found_stance, "Expected at least one leg in stance")

    def test_dead_reckoning_accumulates(self) -> None:
        """Walking forward for 1 second should advance _body_x > 0."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        for _ in range(100):  # 100 ticks * 0.01s = 1 second
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
        self.assertGreater(ctrl._body_x, 0.0, "Body should have moved forward")

    def test_foot_plant_constant_during_stance(self) -> None:
        """_foot_plant_world[i] should not change between ticks while
        leg i remains in stance."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0
        # Warm up
        for _ in range(200):
            _, phase = ctrl.compute_targets(state, 0.01, cfg, phase)

        # Record plant points, tick once, check they haven't changed
        # for legs that stayed in stance.
        assert ctrl._foot_plant_world is not None
        plants_before = [tuple(p) for p in ctrl._foot_plant_world]

        offset_list = list(cfg.leg_phase_offsets)
        TWO_PI = 2.0 * math.pi
        # Determine which legs are in stance before
        in_stance_before = []
        for leg_idx in range(6):
            lp = (phase / TWO_PI + offset_list[leg_idx]) % 1.0
            in_stance_before.append(lp < cfg.duty_factor)

        _, phase2 = ctrl.compute_targets(state, 0.01, cfg, phase)

        # Check which legs are still in stance after
        for leg_idx in range(6):
            lp = (phase2 / TWO_PI + offset_list[leg_idx]) % 1.0
            in_stance_after = lp < cfg.duty_factor
            if in_stance_before[leg_idx] and in_stance_after:
                plant_now = ctrl._foot_plant_world[leg_idx]
                self.assertAlmostEqual(
                    plants_before[leg_idx][0], plant_now[0], places=10,
                    msg=f"Leg {leg_idx} plant X changed during stance",
                )
                self.assertAlmostEqual(
                    plants_before[leg_idx][1], plant_now[1], places=10,
                    msg=f"Leg {leg_idx} plant Y changed during stance",
                )


class TestStaticStability(unittest.TestCase):
    """The body center projection must stay inside the support polygon
    of the stance feet at every tick during walking.  If it doesn't,
    the robot tips over."""

    @staticmethod
    def _stance_feet_body(
        ctrl: Hexapod3DOFController,
        cfg: TeleopConfig,
        phase: float,
    ) -> list[tuple[float, float]]:
        """Return (x, y) body-frame positions of all stance feet."""
        from isaac_bridge.controllers import _world_to_body

        assert ctrl._foot_plant_world is not None
        feet: list[tuple[float, float]] = []
        for leg_idx in range(6):
            offset = cfg.leg_phase_offsets[leg_idx]
            leg_phase = (phase / (2.0 * math.pi) + offset) % 1.0
            if leg_phase < cfg.duty_factor:
                pw = ctrl._foot_plant_world[leg_idx]
                pb = _world_to_body(
                    pw[0], pw[1], pw[2],
                    ctrl._body_x, ctrl._body_y, ctrl._body_yaw,
                )
                feet.append((pb[0], pb[1]))
        return feet

    @staticmethod
    def _point_in_convex_hull(
        point: tuple[float, float],
        polygon: list[tuple[float, float]],
    ) -> bool:
        """Check if *point* lies inside the convex hull of *polygon*.

        Uses the cross-product winding test.  Returns True if inside
        or on the boundary (with a small tolerance).
        """
        n = len(polygon)
        if n < 3:
            # Degenerate — fewer than 3 stance feet means unstable by definition,
            # but with tripod gait and duty_factor>=0.5 we should always have >=3.
            return False

        # Sort polygon vertices by angle from centroid
        cx = sum(p[0] for p in polygon) / n
        cy = sum(p[1] for p in polygon) / n
        sorted_poly = sorted(polygon, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

        # Cross-product winding: point must be on the same side of every edge.
        px, py = point
        for i in range(n):
            x1, y1 = sorted_poly[i]
            x2, y2 = sorted_poly[(i + 1) % n]
            cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
            if cross < -1e-6:  # tolerance for numerical noise
                return False
        return True

    def test_cog_inside_support_polygon_during_walk(self) -> None:
        """Body origin (0,0 in body frame) must stay inside the convex hull
        of stance foot XY positions at every tick."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0

        violations = 0
        total_ticks = 0
        for tick in range(500):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            if ctrl._foot_plant_world is None:
                continue
            total_ticks += 1
            stance_feet = self._stance_feet_body(ctrl, cfg, phase)
            if len(stance_feet) < 3:
                violations += 1
                continue
            # Body center is at (0, 0) in body frame
            if not self._point_in_convex_hull((0.0, 0.0), stance_feet):
                violations += 1

        # Allow up to 5% transient violations (phase transitions)
        violation_rate = violations / max(total_ticks, 1)
        self.assertLess(
            violation_rate, 0.05,
            f"Body center outside support polygon {violations}/{total_ticks} ticks "
            f"({violation_rate:.1%}) — robot would tip over",
        )

    def test_at_least_3_stance_feet_during_walk(self) -> None:
        """With tripod gait and duty_factor >= 0.5, there should always
        be at least 3 feet in stance (the support triangle)."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0

        min_stance = 6
        for _ in range(500):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            if ctrl._foot_plant_world is None:
                continue
            stance_feet = self._stance_feet_body(ctrl, cfg, phase)
            min_stance = min(min_stance, len(stance_feet))

        self.assertGreaterEqual(
            min_stance, 3,
            f"Minimum stance feet was {min_stance} — need at least 3 for stability",
        )

    def test_cog_stable_with_yaw(self) -> None:
        """Stability check during combined forward + yaw motion."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps * 0.5, yaw=cfg.yaw_max_rps * 0.5)
        phase = 0.0

        violations = 0
        total_ticks = 0
        for _ in range(500):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            if ctrl._foot_plant_world is None:
                continue
            total_ticks += 1
            stance_feet = self._stance_feet_body(ctrl, cfg, phase)
            if len(stance_feet) >= 3:
                if not self._point_in_convex_hull((0.0, 0.0), stance_feet):
                    violations += 1

        violation_rate = violations / max(total_ticks, 1)
        self.assertLess(
            violation_rate, 0.05,
            f"COG outside support polygon with yaw: {violations}/{total_ticks} "
            f"({violation_rate:.1%})",
        )

    def test_cog_margin_at_full_speed(self) -> None:
        """At full speed the COG should still have positive stability margin
        (distance from COG to nearest support polygon edge)."""
        ctrl = Hexapod3DOFController()
        cfg = _3dof_config()
        state = _state(vx=cfg.vx_max_mps)
        phase = 0.0

        min_margin = float("inf")
        for _ in range(500):
            targets, phase = ctrl.compute_targets(state, 0.01, cfg, phase)
            if ctrl._foot_plant_world is None:
                continue
            stance_feet = self._stance_feet_body(ctrl, cfg, phase)
            if len(stance_feet) < 3:
                continue

            # Sort polygon vertices
            n = len(stance_feet)
            cx = sum(p[0] for p in stance_feet) / n
            cy = sum(p[1] for p in stance_feet) / n
            sorted_poly = sorted(
                stance_feet,
                key=lambda p: math.atan2(p[1] - cy, p[0] - cx),
            )

            # Distance from origin (0,0) to each edge
            for i in range(n):
                x1, y1 = sorted_poly[i]
                x2, y2 = sorted_poly[(i + 1) % n]
                # Signed distance from (0,0) to line through (x1,y1)-(x2,y2)
                edge_len = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
                if edge_len < 1e-9:
                    continue
                dist = ((x2 - x1) * (0.0 - y1) - (y2 - y1) * (0.0 - x1)) / edge_len
                min_margin = min(min_margin, dist)

        # Margin should be positive (inside polygon) and at least 5mm
        self.assertGreater(
            min_margin, 0.005,
            f"Minimum stability margin is {min_margin * 1000:.1f}mm — "
            f"should be > 5mm for reliable walking",
        )


if __name__ == "__main__":
    unittest.main()
