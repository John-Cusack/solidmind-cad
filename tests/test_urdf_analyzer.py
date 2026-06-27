"""Tests for server.urdf_analyzer — URDF parsing and morphology classification."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from server.urdf_analyzer import analyze_urdf

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HEXAPOD_URDF = _PROJECT_ROOT / "hexapod_sim_pkg" / "Hexapod_v2_1DOF.urdf"


class TestAnalyzeHexapodURDF(unittest.TestCase):
    """Test URDF analysis against the real hexapod URDF."""

    @classmethod
    def setUpClass(cls) -> None:
        if not _HEXAPOD_URDF.is_file():
            raise unittest.SkipTest("Hexapod URDF not found")
        cls.analysis = analyze_urdf(_HEXAPOD_URDF)

    def test_robot_name(self) -> None:
        self.assertEqual(self.analysis.robot_name, "Hexapod_v2_1DOF")

    def test_morphology(self) -> None:
        self.assertEqual(self.analysis.morphology, "hexapod_1dof")

    def test_actuated_joints(self) -> None:
        self.assertEqual(len(self.analysis.actuated_joints), 6)
        expected = {"hip_lf", "hip_lm", "hip_lr", "hip_rf", "hip_rm", "hip_rr"}
        self.assertEqual(set(self.analysis.actuated_joints), expected)

    def test_joint_types(self) -> None:
        for jname in self.analysis.actuated_joints:
            self.assertEqual(self.analysis.joint_types[jname], "revolute")
        # Fixed joints should also be present
        self.assertIn("fix_servo_lf", self.analysis.joint_types)
        self.assertEqual(self.analysis.joint_types["fix_servo_lf"], "fixed")

    def test_joint_limits(self) -> None:
        for jname in self.analysis.actuated_joints:
            self.assertIn(jname, self.analysis.joint_limits)
            lo, hi = self.analysis.joint_limits[jname]
            self.assertAlmostEqual(lo, -1.0472, places=3)
            self.assertAlmostEqual(hi, 1.0472, places=3)

    def test_joint_effort(self) -> None:
        for jname in self.analysis.actuated_joints:
            self.assertIn(jname, self.analysis.joint_effort)
            self.assertAlmostEqual(self.analysis.joint_effort[jname], 1.5)

    def test_joint_velocity(self) -> None:
        for jname in self.analysis.actuated_joints:
            self.assertIn(jname, self.analysis.joint_velocity)
            self.assertAlmostEqual(self.analysis.joint_velocity[jname], 6.17847, places=3)

    def test_joint_damping(self) -> None:
        for jname in self.analysis.actuated_joints:
            self.assertIn(jname, self.analysis.joint_damping)
            self.assertAlmostEqual(self.analysis.joint_damping[jname], 0.1)

    def test_base_link(self) -> None:
        self.assertEqual(self.analysis.base_link, "base_link")

    def test_foot_links(self) -> None:
        # Leaf links with collision geometry should be the leg links
        expected_feet = {"leg_lf", "leg_lm", "leg_lr", "leg_rf", "leg_rm", "leg_rr"}
        self.assertEqual(set(self.analysis.foot_links), expected_feet)

    def test_total_mass(self) -> None:
        # chassis=0.15, 6 servos * 0.055 = 0.33, 6 legs * 0.03 = 0.18
        # total = 0.15 + 0.33 + 0.18 = 0.66 (approximately)
        self.assertAlmostEqual(self.analysis.total_mass_kg, 0.66, places=2)

    def test_standing_height(self) -> None:
        # base_link → chassis has z=0.125
        self.assertAlmostEqual(self.analysis.standing_height_m, 0.125, places=3)

    def test_frozen_dataclass(self) -> None:
        with self.assertRaises(AttributeError):
            self.analysis.robot_name = "other"  # type: ignore[misc]


class TestAnalyzeMinimalURDF(unittest.TestCase):
    """Test with a minimal synthetic URDF."""

    def test_two_joint_robot(self) -> None:
        urdf = """\
<?xml version="1.0"?>
<robot name="test_bot">
  <link name="base"><inertial><mass value="1.0"/></inertial></link>
  <link name="arm1"><inertial><mass value="0.5"/><inertia ixx="0" ixy="0" ixz="0" iyy="0" iyz="0" izz="0"/></inertial><collision><geometry><box size="0.1 0.1 0.1"/></geometry></collision></link>
  <link name="arm2"><inertial><mass value="0.3"/><inertia ixx="0" ixy="0" ixz="0" iyy="0" iyz="0" izz="0"/></inertial><collision><geometry><box size="0.1 0.1 0.1"/></geometry></collision></link>
  <joint name="j1" type="revolute">
    <parent link="base"/><child link="arm1"/>
    <axis xyz="0 0 1"/>
    <limit lower="-1.57" upper="1.57" effort="10" velocity="3"/>
  </joint>
  <joint name="j2" type="prismatic">
    <parent link="arm1"/><child link="arm2"/>
    <axis xyz="0 0 1"/>
    <limit lower="0" upper="0.5" effort="5" velocity="1"/>
  </joint>
</robot>"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".urdf", delete=False,
        ) as f:
            f.write(urdf)
            f.flush()
            try:
                analysis = analyze_urdf(f.name)
            finally:
                os.unlink(f.name)

        self.assertEqual(analysis.robot_name, "test_bot")
        self.assertEqual(len(analysis.actuated_joints), 2)
        self.assertEqual(analysis.joint_types["j1"], "revolute")
        self.assertEqual(analysis.joint_types["j2"], "prismatic")
        self.assertEqual(analysis.morphology, "unknown")
        self.assertEqual(analysis.base_link, "base")
        self.assertAlmostEqual(analysis.total_mass_kg, 1.8)
        # arm2 is leaf with collision
        self.assertIn("arm2", analysis.foot_links)

    def test_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            analyze_urdf("/nonexistent/path.urdf")


if __name__ == "__main__":
    unittest.main()
