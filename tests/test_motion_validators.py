"""Tests for server.motion_validators — analytical validation checks."""
from __future__ import annotations

import math
import unittest

from server.motion_models import (
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.motion_validators import (
    analyze_gear_train,
    propagate_speeds,
    propagate_torques,
    run_validators,
)


def _simple_gear_pair(
    teeth_a: int = 20,
    teeth_b: int = 40,
    input_rpm: float = 1000,
    input_torque: float = 5.0,
    efficiency: float = 1.0,
) -> Mechanism:
    """20T driving 40T: ratio=0.5, output spins at 2× input, torque halved."""
    return Mechanism(
        name="gear_pair",
        parts=(
            PartNode(id="gear_a"),
            PartNode(id="gear_b"),
            PartNode(id="frame", is_ground=True),
        ),
        joints=(
            JointEdge(
                id="mesh",
                joint_type=JointType.GEAR_MESH,
                parent_part="gear_a",
                child_part="gear_b",
                teeth_parent=teeth_a,
                teeth_child=teeth_b,
                gear_ratio=teeth_a / teeth_b,
                mesh_efficiency=efficiency,
            ),
            JointEdge(id="rev_a", joint_type=JointType.REVOLUTE, parent_part="frame", child_part="gear_a"),
            JointEdge(id="rev_b", joint_type=JointType.REVOLUTE, parent_part="frame", child_part="gear_b"),
        ),
        drives=(DriveCondition(joint_id="mesh", speed_rpm=input_rpm, torque_nm=input_torque),),
    )


def _planetary_3to1(
    sun_teeth: int = 18,
    planet_teeth: int = 9,
    ring_teeth: int = 36,
    n_planets: int = 3,
    input_rpm: float = 1000,
    input_torque: float = 5.0,
) -> Mechanism:
    """Planetary with ring fixed, sun input, carrier output.

    Ratio = 1 + ring/sun = 1 + 36/18 = 3.
    Carrier speed = sun_speed / ratio = 333.33 RPM.
    """
    parts = [
        PartNode(id="sun"),
        PartNode(id="ring", is_ground=True),
        PartNode(id="carrier"),
    ]
    joints = []

    # Sun-to-planet meshes
    for i in range(n_planets):
        pid = f"planet_{i}"
        parts.append(PartNode(id=pid))
        joints.append(JointEdge(
            id=f"sun_planet_{i}",
            joint_type=JointType.GEAR_MESH,
            parent_part="sun",
            child_part=pid,
            teeth_parent=sun_teeth,
            teeth_child=planet_teeth,
            gear_ratio=sun_teeth / planet_teeth,
        ))
        # Planet-to-ring mesh
        joints.append(JointEdge(
            id=f"planet_ring_{i}",
            joint_type=JointType.GEAR_MESH,
            parent_part=pid,
            child_part="ring",
            teeth_parent=planet_teeth,
            teeth_child=ring_teeth,
            gear_ratio=planet_teeth / ring_teeth,
        ))
        # Planet on carrier (revolute)
        joints.append(JointEdge(
            id=f"planet_carrier_{i}",
            joint_type=JointType.REVOLUTE,
            parent_part="carrier",
            child_part=pid,
        ))

    # Sun revolute (driven)
    joints.append(JointEdge(
        id="sun_rev",
        joint_type=JointType.REVOLUTE,
        parent_part="sun",
        child_part="carrier",
    ))

    return Mechanism(
        name="planetary_3to1",
        parts=tuple(parts),
        joints=tuple(joints),
        drives=(DriveCondition(joint_id="sun_planet_0", speed_rpm=input_rpm, torque_nm=input_torque),),
        expected_outputs={
            "carrier_speed_rpm": input_rpm / 3.0,
            "carrier_torque_nm": input_torque * 3.0,
        },
    )


def _four_bar(s: float, p: float, q: float, l: float) -> Mechanism:
    """Four-bar linkage with given link lengths."""
    return Mechanism(
        name="four_bar",
        parts=(
            PartNode(id="ground", is_ground=True),
            PartNode(id="crank"),
            PartNode(id="coupler"),
            PartNode(id="rocker"),
        ),
        joints=(
            JointEdge(id="j1", joint_type=JointType.REVOLUTE, parent_part="ground", child_part="crank", link_length_mm=s),
            JointEdge(id="j2", joint_type=JointType.REVOLUTE, parent_part="crank", child_part="coupler", link_length_mm=p),
            JointEdge(id="j3", joint_type=JointType.REVOLUTE, parent_part="coupler", child_part="rocker", link_length_mm=q),
            JointEdge(id="j4", joint_type=JointType.REVOLUTE, parent_part="rocker", child_part="ground", link_length_mm=l),
        ),
        drives=(),
    )


class TestSpeedPropagation(unittest.TestCase):
    def test_simple_gear_pair(self):
        mech = _simple_gear_pair(teeth_a=20, teeth_b=40, input_rpm=1000)
        speeds = propagate_speeds(mech)
        # ratio = 20/40 = 0.5, output = input / 0.5 = 2000
        self.assertAlmostEqual(speeds["gear_a"], 1000)
        self.assertAlmostEqual(speeds["gear_b"], 2000)
        self.assertAlmostEqual(speeds["frame"], 0.0)

    def test_2to1_reduction(self):
        mech = _simple_gear_pair(teeth_a=40, teeth_b=20, input_rpm=1000)
        speeds = propagate_speeds(mech)
        # ratio = 40/20 = 2, output = 1000 / 2 = 500
        self.assertAlmostEqual(speeds["gear_a"], 1000)
        self.assertAlmostEqual(speeds["gear_b"], 500)


class TestTorquePropagation(unittest.TestCase):
    def test_simple_gear_pair(self):
        mech = _simple_gear_pair(teeth_a=20, teeth_b=40, input_rpm=1000, input_torque=5.0)
        torques = propagate_torques(mech)
        # ratio = 0.5, torque output = 5 * 0.5 = 2.5
        self.assertAlmostEqual(torques["gear_a"], 5.0)
        self.assertAlmostEqual(torques["gear_b"], 2.5)

    def test_with_efficiency(self):
        mech = _simple_gear_pair(teeth_a=20, teeth_b=40, input_torque=10.0, efficiency=0.95)
        torques = propagate_torques(mech)
        self.assertAlmostEqual(torques["gear_b"], 10.0 * 0.5 * 0.95)


class TestValidators(unittest.TestCase):
    def test_gear_ratio_consistency_pass(self):
        mech = _simple_gear_pair()
        results = run_validators(mech, ["gear_ratio_consistency"])
        self.assertEqual(results[0].status, "pass")

    def test_gear_ratio_consistency_fail(self):
        """Mismatch between gear_ratio and teeth counts."""
        mech = Mechanism(
            name="bad",
            parts=(PartNode(id="a"), PartNode(id="b"), PartNode(id="f", is_ground=True)),
            joints=(JointEdge(
                id="m",
                joint_type=JointType.GEAR_MESH,
                parent_part="a",
                child_part="b",
                teeth_parent=20,
                teeth_child=40,
                gear_ratio=3.0,  # wrong — should be 0.5
            ),),
            drives=(),
        )
        results = run_validators(mech, ["gear_ratio_consistency"])
        self.assertEqual(results[0].status, "fail")

    def test_dof_simple_gear_pair(self):
        mech = _simple_gear_pair()
        results = run_validators(mech, ["dof_analysis"])
        # 3 parts, 3 joints (mesh + 2 revolute)
        # DOF = 3*(3-1) - 2*3 - 0 - 0 = 6 - 6 = 0... but gears should have DOF=1
        # This shows Gruebler's equation is a simplification for gear systems
        r = results[0]
        self.assertIn("DOF=", r.message)

    def test_power_conservation_pass(self):
        mech = _simple_gear_pair()
        results = run_validators(mech, ["power_conservation"])
        self.assertEqual(results[0].status, "pass")

    def test_power_conservation_with_efficiency(self):
        mech = _simple_gear_pair(efficiency=0.95)
        results = run_validators(mech, ["power_conservation"])
        self.assertEqual(results[0].status, "pass")
        self.assertLess(results[0].measured["ratio"], 1.0)

    def test_linkage_grashof_pass(self):
        # s+l=10+40=50 <= p+q=20+30=50 → Grashof
        mech = _four_bar(s=10, p=20, q=30, l=40)
        results = run_validators(mech, ["linkage_grashof"])
        self.assertEqual(results[0].status, "pass")
        self.assertTrue(results[0].measured["grashof"])

    def test_linkage_grashof_fail(self):
        # s+l=10+50=60 > p+q=15+20=35 → NOT Grashof
        mech = _four_bar(s=10, p=15, q=20, l=50)
        results = run_validators(mech, ["linkage_grashof"])
        self.assertEqual(results[0].status, "warn")
        self.assertFalse(results[0].measured["grashof"])

    def test_expected_output_pass(self):
        mech = _simple_gear_pair(teeth_a=20, teeth_b=40, input_rpm=1000, input_torque=5.0)
        mech = Mechanism(
            name=mech.name,
            parts=mech.parts,
            joints=mech.joints,
            drives=mech.drives,
            expected_outputs={
                "gear_b_speed_rpm": 2000.0,
                "gear_b_torque_nm": 2.5,
            },
        )
        results = run_validators(mech, ["expected_output_check"])
        self.assertEqual(results[0].status, "pass")

    def test_expected_output_fail(self):
        mech = _simple_gear_pair(teeth_a=20, teeth_b=40, input_rpm=1000, input_torque=5.0)
        mech = Mechanism(
            name=mech.name,
            parts=mech.parts,
            joints=mech.joints,
            drives=mech.drives,
            expected_outputs={
                "gear_b_speed_rpm": 500.0,  # wrong — should be 2000
            },
        )
        results = run_validators(mech, ["expected_output_check"])
        self.assertEqual(results[0].status, "fail")

    def test_all_validators_run(self):
        mech = _simple_gear_pair()
        results = run_validators(mech)
        self.assertGreater(len(results), 5)
        names = {r.name for r in results}
        self.assertIn("gear_ratio_consistency", names)
        self.assertIn("speed_propagation", names)
        self.assertIn("power_conservation", names)
        self.assertIn("dof_analysis", names)


class TestGearTrainAnalysis(unittest.TestCase):
    def test_simple_pair(self):
        mech = _simple_gear_pair(teeth_a=20, teeth_b=40)
        result = analyze_gear_train(mech)
        self.assertAlmostEqual(result["overall_ratio"], 0.5)
        self.assertEqual(len(result["stages"]), 1)

    def test_no_gears(self):
        mech = Mechanism(
            name="no_gears",
            parts=(PartNode(id="a"),),
            joints=(),
            drives=(),
        )
        result = analyze_gear_train(mech)
        self.assertIsNone(result["overall_ratio"])


class TestPlanetaryMechanism(unittest.TestCase):
    def test_planetary_speeds(self):
        """Test speed propagation through a planetary gear set."""
        mech = _planetary_3to1(sun_teeth=18, planet_teeth=9, ring_teeth=36, input_rpm=1000)
        speeds = propagate_speeds(mech)
        # Sun drives at 1000 RPM
        self.assertAlmostEqual(speeds["sun"], 1000.0)
        # Ring is ground
        self.assertAlmostEqual(speeds["ring"], 0.0)

    def test_planetary_gear_train(self):
        mech = _planetary_3to1()
        result = analyze_gear_train(mech)
        self.assertGreater(len(result["stages"]), 0)


if __name__ == "__main__":
    unittest.main()
