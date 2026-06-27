"""Tests for the 3-DOF hexapod leg IK solver."""
from __future__ import annotations

import math
import unittest

from isaac_bridge.hexapod_ik import (
    HipMount,
    LegAngles,
    LegGeometry,
    body_to_hip_frame,
    default_foot_position,
    forward_kinematics,
    inverse_kinematics,
)

_DEFAULT_GEOM = LegGeometry()


class TestFKIKRoundtrip(unittest.TestCase):
    """FK(IK(point)) should recover the original point."""

    def _assert_roundtrip(self, px: float, py: float, pz: float) -> None:
        angles = inverse_kinematics(px, py, pz, _DEFAULT_GEOM)
        rx, ry, rz = forward_kinematics(angles, _DEFAULT_GEOM)
        self.assertAlmostEqual(rx, px, places=5, msg=f"X mismatch for ({px},{py},{pz})")
        self.assertAlmostEqual(ry, py, places=5, msg=f"Y mismatch for ({px},{py},{pz})")
        self.assertAlmostEqual(rz, pz, places=5, msg=f"Z mismatch for ({px},{py},{pz})")

    def test_straight_ahead(self) -> None:
        self._assert_roundtrip(0.12, 0.0, -0.06)

    def test_below_body(self) -> None:
        self._assert_roundtrip(0.10, 0.0, -0.08)

    def test_side_reach(self) -> None:
        self._assert_roundtrip(0.0, 0.12, -0.04)

    def test_diagonal(self) -> None:
        self._assert_roundtrip(0.08, 0.08, -0.05)

    def test_negative_y(self) -> None:
        self._assert_roundtrip(0.06, -0.06, -0.06)

    def test_near_ground(self) -> None:
        self._assert_roundtrip(0.10, 0.0, -0.10)


class TestWorkspaceClamping(unittest.TestCase):
    """Unreachable points should be clamped — no NaN, no exceptions."""

    def test_far_point_no_nan(self) -> None:
        angles = inverse_kinematics(1.0, 0.0, 0.0, _DEFAULT_GEOM)
        self.assertFalse(math.isnan(angles.coxa))
        self.assertFalse(math.isnan(angles.femur))
        self.assertFalse(math.isnan(angles.tibia))

    def test_near_point_no_nan(self) -> None:
        angles = inverse_kinematics(0.01, 0.0, 0.0, _DEFAULT_GEOM)
        self.assertFalse(math.isnan(angles.coxa))
        self.assertFalse(math.isnan(angles.femur))
        self.assertFalse(math.isnan(angles.tibia))

    def test_origin_no_nan(self) -> None:
        angles = inverse_kinematics(0.0, 0.0, 0.0, _DEFAULT_GEOM)
        self.assertFalse(math.isnan(angles.coxa))
        self.assertFalse(math.isnan(angles.femur))
        self.assertFalse(math.isnan(angles.tibia))

    def test_far_below_no_nan(self) -> None:
        angles = inverse_kinematics(0.05, 0.0, -1.0, _DEFAULT_GEOM)
        self.assertFalse(math.isnan(angles.coxa))
        self.assertFalse(math.isnan(angles.femur))
        self.assertFalse(math.isnan(angles.tibia))

    def test_clamped_fk_within_reach(self) -> None:
        """FK of clamped IK should be within workspace bounds."""
        geom = _DEFAULT_GEOM
        max_reach = geom.l_coxa + geom.l_femur + geom.l_tibia
        angles = inverse_kinematics(1.0, 0.0, 0.0, geom)
        px, py, pz = forward_kinematics(angles, geom)
        dist = math.sqrt(px * px + py * py + pz * pz)
        self.assertLessEqual(dist, max_reach + 1e-6)


class TestElbowDown(unittest.TestCase):
    """Tibia angle should always be <= 0 (elbow-down convention)."""

    def test_various_positions(self) -> None:
        positions = [
            (0.12, 0.0, -0.06),
            (0.10, 0.0, -0.08),
            (0.06, 0.06, -0.04),
            (0.0, 0.12, -0.05),
            (0.15, 0.0, 0.0),
            (0.05, 0.0, -0.12),
        ]
        for px, py, pz in positions:
            angles = inverse_kinematics(px, py, pz, _DEFAULT_GEOM)
            self.assertLessEqual(
                angles.tibia, 1e-10,
                f"Tibia should be <= 0 for ({px},{py},{pz}), got {angles.tibia}",
            )


class TestCoxaQuadrants(unittest.TestCase):
    """Coxa angle should match atan2(py, px) for all four quadrants."""

    def test_positive_x_positive_y(self) -> None:
        angles = inverse_kinematics(0.10, 0.05, -0.05, _DEFAULT_GEOM)
        expected = math.atan2(0.05, 0.10)
        self.assertAlmostEqual(angles.coxa, expected, places=6)

    def test_positive_x_negative_y(self) -> None:
        angles = inverse_kinematics(0.10, -0.05, -0.05, _DEFAULT_GEOM)
        expected = math.atan2(-0.05, 0.10)
        self.assertAlmostEqual(angles.coxa, expected, places=6)

    def test_negative_x_positive_y(self) -> None:
        angles = inverse_kinematics(-0.10, 0.05, -0.05, _DEFAULT_GEOM)
        expected = math.atan2(0.05, -0.10)
        self.assertAlmostEqual(angles.coxa, expected, places=6)

    def test_negative_x_negative_y(self) -> None:
        angles = inverse_kinematics(-0.10, -0.05, -0.05, _DEFAULT_GEOM)
        expected = math.atan2(-0.05, -0.10)
        self.assertAlmostEqual(angles.coxa, expected, places=6)


class TestBodyToHipFrame(unittest.TestCase):
    """Coordinate transform from body to hip frame."""

    def test_identity_mount(self) -> None:
        mount = HipMount(x=0.0, y=0.0, angle=0.0)
        hx, hy, hz = body_to_hip_frame((0.1, 0.2, -0.05), mount)
        self.assertAlmostEqual(hx, 0.1, places=6)
        self.assertAlmostEqual(hy, 0.2, places=6)
        self.assertAlmostEqual(hz, -0.05, places=6)

    def test_offset_mount(self) -> None:
        mount = HipMount(x=0.08, y=0.05, angle=0.0)
        hx, hy, hz = body_to_hip_frame((0.18, 0.05, -0.03), mount)
        self.assertAlmostEqual(hx, 0.10, places=6)
        self.assertAlmostEqual(hy, 0.0, places=6)
        self.assertAlmostEqual(hz, -0.03, places=6)

    def test_rotated_mount_90deg(self) -> None:
        mount = HipMount(x=0.0, y=0.05, angle=math.pi / 2)
        # Point at (0.0, 0.15, 0) in body frame → offset is (0, 0.1, 0)
        # Rotate by -90° around Z: (0, 0.1) → (0.1, 0)
        hx, hy, hz = body_to_hip_frame((0.0, 0.15, 0.0), mount)
        self.assertAlmostEqual(hx, 0.1, places=5)
        self.assertAlmostEqual(hy, 0.0, places=5)

    def test_z_preserved(self) -> None:
        mount = HipMount(x=0.05, y=0.03, angle=1.0)
        _, _, hz = body_to_hip_frame((0.1, 0.1, -0.07), mount)
        self.assertAlmostEqual(hz, -0.07, places=6)


class TestDefaultFootSymmetry(unittest.TestCase):
    """Left/right mirrored mounts should produce Y-mirrored feet."""

    def test_left_right_mirror(self) -> None:
        geom = _DEFAULT_GEOM
        # Left-front and right-front mounts, mirrored about X axis
        mount_lf = HipMount(x=0.08, y=0.055, angle=math.pi / 4)
        mount_rf = HipMount(x=0.08, y=-0.055, angle=-math.pi / 4)

        foot_lf = default_foot_position(mount_lf, geom)
        foot_rf = default_foot_position(mount_rf, geom)

        # X should be equal, Y should be opposite, Z should be equal
        self.assertAlmostEqual(foot_lf[0], foot_rf[0], places=6)
        self.assertAlmostEqual(foot_lf[1], -foot_rf[1], places=6)
        self.assertAlmostEqual(foot_lf[2], foot_rf[2], places=6)

    def test_mid_legs_mirror(self) -> None:
        geom = _DEFAULT_GEOM
        mount_lm = HipMount(x=0.0, y=0.055, angle=math.pi / 2)
        mount_rm = HipMount(x=0.0, y=-0.055, angle=-math.pi / 2)

        foot_lm = default_foot_position(mount_lm, geom)
        foot_rm = default_foot_position(mount_rm, geom)

        self.assertAlmostEqual(foot_lm[0], foot_rm[0], places=6)
        self.assertAlmostEqual(foot_lm[1], -foot_rm[1], places=6)
        self.assertAlmostEqual(foot_lm[2], foot_rm[2], places=6)


class TestFKConsistency(unittest.TestCase):
    """FK produces expected results for known angle inputs."""

    def test_zero_angles(self) -> None:
        """All zeros → foot straight out along X at coxa + femur + tibia."""
        geom = _DEFAULT_GEOM
        angles = LegAngles(coxa=0.0, femur=0.0, tibia=0.0)
        px, py, pz = forward_kinematics(angles, geom)
        expected_r = geom.l_coxa + geom.l_femur + geom.l_tibia
        self.assertAlmostEqual(px, expected_r, places=6)
        self.assertAlmostEqual(py, 0.0, places=6)
        self.assertAlmostEqual(pz, 0.0, places=6)

    def test_coxa_90(self) -> None:
        """Coxa at 90° → foot along +Y."""
        geom = _DEFAULT_GEOM
        angles = LegAngles(coxa=math.pi / 2, femur=0.0, tibia=0.0)
        px, py, pz = forward_kinematics(angles, geom)
        expected_r = geom.l_coxa + geom.l_femur + geom.l_tibia
        self.assertAlmostEqual(px, 0.0, places=5)
        self.assertAlmostEqual(py, expected_r, places=5)
        self.assertAlmostEqual(pz, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
