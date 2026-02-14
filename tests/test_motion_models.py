"""Tests for server.motion_models — data model round-trips and helpers."""
from __future__ import annotations

import unittest

from server.motion_models import (
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)


class TestPartNode(unittest.TestCase):
    def test_round_trip(self):
        p = PartNode(id="sun", body_name="Body_Sun", mass_kg=0.1, is_ground=False)
        d = p.to_dict()
        p2 = PartNode.from_dict(d)
        self.assertEqual(p, p2)

    def test_defaults(self):
        p = PartNode(id="x")
        self.assertIsNone(p.body_name)
        self.assertIsNone(p.mass_kg)
        self.assertFalse(p.is_ground)


class TestJointEdge(unittest.TestCase):
    def test_round_trip_gear_mesh(self):
        j = JointEdge(
            id="sun_planet",
            joint_type=JointType.GEAR_MESH,
            parent_part="sun",
            child_part="planet1",
            gear_ratio=2.0,
            teeth_parent=18,
            teeth_child=9,
            mesh_efficiency=0.98,
        )
        d = j.to_dict()
        j2 = JointEdge.from_dict(d)
        self.assertEqual(j, j2)

    def test_axis_tuple(self):
        j = JointEdge.from_dict({
            "id": "j1",
            "joint_type": "revolute",
            "parent_part": "a",
            "child_part": "b",
            "axis": [1, 0, 0],
        })
        self.assertEqual(j.axis, (1, 0, 0))


class TestDriveCondition(unittest.TestCase):
    def test_round_trip(self):
        d = DriveCondition(joint_id="j1", speed_rpm=1000, torque_nm=5.0)
        d2 = DriveCondition.from_dict(d.to_dict())
        self.assertEqual(d, d2)


class TestMechanism(unittest.TestCase):
    def _make_simple_gear_pair(self) -> Mechanism:
        return Mechanism(
            name="gear_pair",
            parts=(
                PartNode(id="input_shaft", is_ground=False),
                PartNode(id="output_shaft", is_ground=False),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="mesh1",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="input_shaft",
                    child_part="output_shaft",
                    teeth_parent=20,
                    teeth_child=40,
                    gear_ratio=0.5,
                ),
                JointEdge(
                    id="input_rev",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="input_shaft",
                ),
                JointEdge(
                    id="output_rev",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="output_shaft",
                ),
            ),
            drives=(
                DriveCondition(joint_id="mesh1", speed_rpm=1000, torque_nm=5.0),
            ),
            expected_outputs={
                "output_shaft_speed_rpm": 2000.0,
                "output_shaft_torque_nm": 2.5,
            },
        )

    def test_round_trip(self):
        m = self._make_simple_gear_pair()
        d = m.to_dict()
        m2 = Mechanism.from_dict(d)
        self.assertEqual(m.name, m2.name)
        self.assertEqual(len(m.parts), len(m2.parts))
        self.assertEqual(len(m.joints), len(m2.joints))

    def test_helpers(self):
        m = self._make_simple_gear_pair()
        self.assertEqual(len(m.ground_parts()), 1)
        self.assertEqual(len(m.moving_parts()), 2)
        self.assertIsNotNone(m.get_part("input_shaft"))
        self.assertIsNone(m.get_part("nonexistent"))
        self.assertIsNotNone(m.get_joint("mesh1"))
        self.assertEqual(len(m.joints_for_part("input_shaft")), 2)  # mesh1 + input_rev


if __name__ == "__main__":
    unittest.main()
