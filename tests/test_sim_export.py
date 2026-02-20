"""Tests for the sim export module (SimModel building + URDF serialization).

Pure unit tests — no FreeCAD or network dependency.
"""
from __future__ import annotations

import math
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.models import Severity
from server.motion_models import (
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.sim_export import (
    MeshBBox,
    SimJoint,
    SimLink,
    SimModel,
    _aabb_overlap_volume,
    _aabb_volume,
    _box_inertia,
    _make_transform_4x4,
    _multiply_4x4,
    _quat_inverse,
    _quat_multiply,
    _quat_to_rpy,
    _rpy_to_matrix,
    _transform_bbox,
    _transform_point,
    build_sim_model,
    validate_urdf,
    validate_urdf_fk,
    write_urdf,
)


class TestQuatToRpy(unittest.TestCase):
    def test_identity_quaternion(self) -> None:
        rpy = _quat_to_rpy(1.0, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(rpy[0], 0.0, places=6)
        self.assertAlmostEqual(rpy[1], 0.0, places=6)
        self.assertAlmostEqual(rpy[2], 0.0, places=6)

    def test_90_deg_yaw(self) -> None:
        # 90deg rotation around Z: quat = (cos(45deg), 0, 0, sin(45deg))
        w = math.cos(math.radians(45))
        z = math.sin(math.radians(45))
        rpy = _quat_to_rpy(w, 0.0, 0.0, z)
        self.assertAlmostEqual(rpy[0], 0.0, places=5)
        self.assertAlmostEqual(rpy[1], 0.0, places=5)
        self.assertAlmostEqual(rpy[2], math.radians(90), places=5)

    def test_90_deg_roll(self) -> None:
        w = math.cos(math.radians(45))
        x = math.sin(math.radians(45))
        rpy = _quat_to_rpy(w, x, 0.0, 0.0)
        self.assertAlmostEqual(rpy[0], math.radians(90), places=5)
        self.assertAlmostEqual(rpy[1], 0.0, places=5)
        self.assertAlmostEqual(rpy[2], 0.0, places=5)


class TestQuatInverse(unittest.TestCase):
    def test_inverse_of_identity(self) -> None:
        inv = _quat_inverse(1.0, 0.0, 0.0, 0.0)
        self.assertEqual(inv, (1.0, 0.0, 0.0, 0.0))

    def test_q_times_q_inv_is_identity(self) -> None:
        # 90deg yaw
        w = math.cos(math.radians(45))
        z = math.sin(math.radians(45))
        q = (w, 0.0, 0.0, z)
        q_inv = _quat_inverse(*q)
        result = _quat_multiply(*q, *q_inv)
        self.assertAlmostEqual(result[0], 1.0, places=10)
        self.assertAlmostEqual(result[1], 0.0, places=10)
        self.assertAlmostEqual(result[2], 0.0, places=10)
        self.assertAlmostEqual(result[3], 0.0, places=10)


class TestQuatMultiply(unittest.TestCase):
    def test_identity_times_q(self) -> None:
        q = (0.707, 0.0, 0.707, 0.0)
        result = _quat_multiply(1.0, 0.0, 0.0, 0.0, *q)
        for i in range(4):
            self.assertAlmostEqual(result[i], q[i], places=6)

    def test_two_90_yaws_compose_to_180(self) -> None:
        w = math.cos(math.radians(45))
        z = math.sin(math.radians(45))
        q90 = (w, 0.0, 0.0, z)
        result = _quat_multiply(*q90, *q90)
        # Should be 180deg yaw: (0, 0, 0, 1)
        rpy = _quat_to_rpy(*result)
        self.assertAlmostEqual(abs(rpy[2]), math.pi, places=5)


class TestBuildSimModel(unittest.TestCase):
    def _make_simple_mechanism(self) -> Mechanism:
        return Mechanism(
            name="simple_arm",
            parts=(
                PartNode(id="base", body_name="Body_Base", is_ground=True, mass_kg=10.0),
                PartNode(id="arm", body_name="Body_Arm", mass_kg=2.0),
            ),
            joints=(
                JointEdge(
                    id="shoulder",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                    axis=(0.0, 0.0, 1.0),
                    origin=(0.0, 0.0, 50.0),
                    min_angle_deg=-90.0,
                    max_angle_deg=90.0,
                ),
            ),
            drives=(
                DriveCondition(joint_id="shoulder", speed_rpm=60.0),
            ),
        )

    def _make_manifest(self) -> list[dict]:
        return [
            {
                "name": "Body_Base",
                "label": "Base",
                "mesh_path": "/tmp/Body_Base.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
            },
            {
                "name": "Body_Arm",
                "label": "Arm",
                "mesh_path": "/tmp/Body_Arm.stl",
                "placement": {"position": [0, 0, 50], "rotation_quat": [1, 0, 0, 0]},
            },
        ]

    def test_basic_model(self) -> None:
        mech = self._make_simple_mechanism()
        manifest = self._make_manifest()
        model = build_sim_model(mech, manifest)

        self.assertEqual(model.name, "simple_arm")
        self.assertEqual(len(model.links), 2)
        self.assertEqual(len(model.joints), 1)

    def test_link_properties(self) -> None:
        mech = self._make_simple_mechanism()
        manifest = self._make_manifest()
        model = build_sim_model(mech, manifest)

        base_link = model.links[0]
        self.assertEqual(base_link.name, "base")
        self.assertTrue(base_link.is_root)
        self.assertEqual(base_link.mesh_path, "/tmp/Body_Base.stl")
        self.assertEqual(base_link.mass_kg, 10.0)
        self.assertEqual(base_link.position, (0.0, 0.0, 0.0))

        arm_link = model.links[1]
        self.assertEqual(arm_link.name, "arm")
        self.assertFalse(arm_link.is_root)
        self.assertEqual(arm_link.mesh_path, "/tmp/Body_Arm.stl")
        self.assertEqual(arm_link.position, (0.0, 0.0, 50.0))

    def test_joint_properties(self) -> None:
        mech = self._make_simple_mechanism()
        manifest = self._make_manifest()
        model = build_sim_model(mech, manifest)

        joint = model.joints[0]
        self.assertEqual(joint.name, "shoulder")
        self.assertEqual(joint.joint_type, "revolute")
        self.assertEqual(joint.parent, "base")
        self.assertEqual(joint.child, "arm")
        self.assertEqual(joint.axis, (0.0, 0.0, 1.0))
        # origin in meters (50mm = 0.05m)
        self.assertAlmostEqual(joint.origin_xyz[2], 0.05, places=6)
        # limits in radians
        self.assertIsNotNone(joint.limits)
        self.assertAlmostEqual(joint.limits[0], math.radians(-90), places=6)
        self.assertAlmostEqual(joint.limits[1], math.radians(90), places=6)

    def test_missing_manifest_entry(self) -> None:
        """Parts without manifest entries get links with no mesh."""
        mech = self._make_simple_mechanism()
        model = build_sim_model(mech, [])  # empty manifest

        self.assertEqual(len(model.links), 2)
        self.assertIsNone(model.links[0].mesh_path)
        self.assertIsNone(model.links[1].mesh_path)

    def test_gear_mesh_joint(self) -> None:
        """Gear mesh joints should produce revolute + mimic."""
        mech = Mechanism(
            name="gear_pair",
            parts=(
                PartNode(id="frame", is_ground=True),
                PartNode(id="gear_a"),
                PartNode(id="gear_b"),
            ),
            joints=(
                JointEdge(
                    id="shaft_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gear_a",
                ),
                JointEdge(
                    id="mesh_ab",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="gear_a",
                    child_part="gear_b",
                    teeth_parent=20,
                    teeth_child=40,
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        mesh_joint = model.joints[1]
        self.assertEqual(mesh_joint.joint_type, "revolute")
        self.assertIsNotNone(mesh_joint.mimic)
        self.assertEqual(mesh_joint.mimic[0], "shaft_a")
        self.assertAlmostEqual(mesh_joint.mimic[1], 0.5, places=6)

    def test_fixed_joint(self) -> None:
        mech = Mechanism(
            name="fixed_pair",
            parts=(
                PartNode(id="base", is_ground=True),
                PartNode(id="attached"),
            ),
            joints=(
                JointEdge(
                    id="weld",
                    joint_type=JointType.FIXED,
                    parent_part="base",
                    child_part="attached",
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        self.assertEqual(model.joints[0].joint_type, "fixed")
        self.assertIsNone(model.joints[0].limits)
        self.assertIsNone(model.joints[0].mimic)

    def test_prismatic_joint_limits(self) -> None:
        mech = Mechanism(
            name="slider",
            parts=(
                PartNode(id="rail", is_ground=True),
                PartNode(id="carriage"),
            ),
            joints=(
                JointEdge(
                    id="slide",
                    joint_type=JointType.PRISMATIC,
                    parent_part="rail",
                    child_part="carriage",
                    axis=(1.0, 0.0, 0.0),
                    min_travel_mm=0.0,
                    max_travel_mm=100.0,
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertEqual(joint.joint_type, "prismatic")
        self.assertIsNotNone(joint.limits)
        self.assertAlmostEqual(joint.limits[0], 0.0, places=6)
        self.assertAlmostEqual(joint.limits[1], 0.1, places=6)  # 100mm = 0.1m

    def test_effort_velocity_from_drive(self) -> None:
        """DriveCondition with torque/speed populates effort/velocity on SimJoint."""
        mech = Mechanism(
            name="driven",
            parts=(
                PartNode(id="base", is_ground=True),
                PartNode(id="arm"),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                ),
            ),
            drives=(
                DriveCondition(joint_id="j1", torque_nm=50.0, speed_rpm=120.0),
            ),
        )

        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertAlmostEqual(joint.effort, 50.0, places=6)
        # 120 RPM = 120 * 2*pi/60 = 4*pi rad/s
        self.assertAlmostEqual(joint.velocity, 120.0 * 2 * math.pi / 60.0, places=6)

    def test_effort_velocity_defaults(self) -> None:
        """No drive -> default effort=100, velocity=10."""
        mech = Mechanism(
            name="no_drive",
            parts=(
                PartNode(id="base", is_ground=True),
                PartNode(id="arm"),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertAlmostEqual(joint.effort, 100.0)
        self.assertAlmostEqual(joint.velocity, 10.0)

    def test_joint_rpy_from_rotated_placement(self) -> None:
        """Rotated manifest quaternions are NOT baked into RPY (Bug 2 fix)."""
        # Parent at identity, child rotated 90deg around Z in manifest.
        # With body-local meshes the quaternion should be ignored — RPY
        # comes only from the auto-computed outward yaw (which is non-zero
        # here because the joint origin is at (0,0,0) relative to ground,
        # so added_yaw = 0).
        w = math.cos(math.radians(45))
        z = math.sin(math.radians(45))
        mech = Mechanism(
            name="rotated",
            parts=(
                PartNode(id="base", body_name="Body_Base", is_ground=True),
                PartNode(id="arm", body_name="Body_Arm"),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "Body_Base", "mesh_path": "/m/base.stl",
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Arm", "mesh_path": "/m/arm.stl",
             "placement": {"position": [100, 0, 0], "rotation_quat": [w, 0, 0, z]}},
        ]

        model = build_sim_model(mech, manifest)
        joint = model.joints[0]
        # RPY should be (0,0,0) — manifest quaternions no longer baked in
        self.assertAlmostEqual(joint.origin_rpy[0], 0.0, places=5)
        self.assertAlmostEqual(joint.origin_rpy[1], 0.0, places=5)
        self.assertAlmostEqual(joint.origin_rpy[2], 0.0, places=5)

    def test_joint_rpy_identity_when_placements_identity(self) -> None:
        """Identity placements -> RPY stays (0,0,0)."""
        mech = Mechanism(
            name="ident",
            parts=(
                PartNode(id="base", body_name="Body_Base", is_ground=True),
                PartNode(id="arm", body_name="Body_Arm"),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "Body_Base", "mesh_path": "/m/base.stl",
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Arm", "mesh_path": "/m/arm.stl",
             "placement": {"position": [0, 0, 50], "rotation_quat": [1, 0, 0, 0]}},
        ]

        model = build_sim_model(mech, manifest)
        joint = model.joints[0]
        self.assertAlmostEqual(joint.origin_rpy[0], 0.0, places=10)
        self.assertAlmostEqual(joint.origin_rpy[1], 0.0, places=10)
        self.assertAlmostEqual(joint.origin_rpy[2], 0.0, places=10)

    def test_auto_inertia_from_bbox(self) -> None:
        """Mass + bbox in manifest -> box inertia tensor on SimLink."""
        mech = Mechanism(
            name="box_part",
            parts=(
                PartNode(id="block", body_name="Body_Block", is_ground=True, mass_kg=2.0),
            ),
            joints=(),
            drives=(),
        )
        manifest = [
            {
                "name": "Body_Block",
                "mesh_path": "/m/block.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [100.0, 200.0, 300.0],
            },
        ]

        model = build_sim_model(mech, manifest)
        link = model.links[0]
        self.assertIsNotNone(link.inertia)

        # Box inertia: I = m/12 * (b^2 + c^2)
        dx, dy, dz = 0.1, 0.2, 0.3  # meters
        mass = 2.0
        expected_ixx = mass / 12.0 * (dy ** 2 + dz ** 2)
        expected_iyy = mass / 12.0 * (dx ** 2 + dz ** 2)
        expected_izz = mass / 12.0 * (dx ** 2 + dy ** 2)
        self.assertAlmostEqual(link.inertia[0], expected_ixx, places=8)
        self.assertAlmostEqual(link.inertia[1], 0.0, places=8)
        self.assertAlmostEqual(link.inertia[2], 0.0, places=8)
        self.assertAlmostEqual(link.inertia[3], expected_iyy, places=8)
        self.assertAlmostEqual(link.inertia[4], 0.0, places=8)
        self.assertAlmostEqual(link.inertia[5], expected_izz, places=8)

    def test_explicit_inertia_preserved(self) -> None:
        """Scalar inertia_kg_m2 on PartNode -> diagonal tensor."""
        mech = Mechanism(
            name="scalar_inertia",
            parts=(
                PartNode(id="part", is_ground=True, mass_kg=1.0, inertia_kg_m2=0.05),
            ),
            joints=(),
            drives=(),
        )

        model = build_sim_model(mech, [])
        link = model.links[0]
        self.assertIsNotNone(link.inertia)
        self.assertAlmostEqual(link.inertia[0], 0.05)  # ixx
        self.assertAlmostEqual(link.inertia[1], 0.0)   # ixy
        self.assertAlmostEqual(link.inertia[2], 0.0)   # ixz
        self.assertAlmostEqual(link.inertia[3], 0.05)  # iyy
        self.assertAlmostEqual(link.inertia[4], 0.0)   # iyz
        self.assertAlmostEqual(link.inertia[5], 0.05)  # izz

    def test_auto_inertia_from_volume_no_mass(self) -> None:
        """Volume in manifest without mass -> estimate mass from default density."""
        mech = Mechanism(
            name="vol_part",
            parts=(
                PartNode(id="block", body_name="Body_Block", is_ground=True),
            ),
            joints=(),
            drives=(),
        )
        # 100x100x100 mm cube = 1e6 mm^3
        manifest = [
            {
                "name": "Body_Block",
                "mesh_path": "/m/block.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [100.0, 100.0, 100.0],
                "volume_mm3": 1e6,
            },
        ]

        model = build_sim_model(mech, manifest)
        link = model.links[0]
        # mass = 1e6 mm^3 * 1e-9 m^3/mm^3 * 1250 kg/m^3 = 1.25 kg
        self.assertIsNotNone(link.mass_kg)
        self.assertAlmostEqual(link.mass_kg, 1.25, places=8)
        self.assertIsNotNone(link.inertia)


    def test_non_root_joint_rpy_zero_no_baked_pitch(self) -> None:
        """Bug 2: rotated manifest quaternions on deeper links don't bake pitch."""
        # 3-part chain: chassis (ground) -> coxa -> femur
        # Femur has a rotated quaternion (30deg pitch) in manifest — should
        # NOT appear in the URDF joint RPY.
        w30 = math.cos(math.radians(15))
        y30 = math.sin(math.radians(15))
        mech = Mechanism(
            name="leg",
            parts=(
                PartNode(id="chassis", body_name="Body_Chassis", is_ground=True),
                PartNode(id="coxa", body_name="Body_Coxa"),
                PartNode(id="femur", body_name="Body_Femur"),
            ),
            joints=(
                JointEdge(
                    id="j_coxa",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="coxa",
                    origin=(0.0, 100.0, 0.0),
                ),
                JointEdge(
                    id="j_femur",
                    joint_type=JointType.REVOLUTE,
                    parent_part="coxa",
                    child_part="femur",
                    origin=(0.0, 200.0, 0.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "Body_Chassis", "mesh_path": "/m/chassis.stl",
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Coxa", "mesh_path": "/m/coxa.stl",
             "placement": {"position": [0, 100, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Femur", "mesh_path": "/m/femur.stl",
             "placement": {"position": [0, 200, 0], "rotation_quat": [w30, 0, y30, 0]}},
        ]

        model = build_sim_model(mech, manifest)
        femur_joint = [j for j in model.joints if j.child == "femur"][0]
        # RPY must be (0,0,0) — no baked pitch from manifest
        self.assertAlmostEqual(femur_joint.origin_rpy[0], 0.0, places=5)
        self.assertAlmostEqual(femur_joint.origin_rpy[1], 0.0, places=5)
        self.assertAlmostEqual(femur_joint.origin_rpy[2], 0.0, places=5)

    def test_joint_axis_rotated_to_local_frame(self) -> None:
        """Bug 3: world-frame joint axis is rotated into child's local frame."""
        # Coxa at (0, 100, 0) from ground -> added_yaw = atan2(100, 0) = π/2
        # Femur has world-frame axis (1, 0, 0).
        # child_yaw = π/2, rotate axis (1,0,0) by -π/2:
        # c=cos(-π/2)=0, s=sin(-π/2)=-1 → (1*0-0*(-1), 1*(-1)+0*0, 0) = (0, -1, 0)
        mech = Mechanism(
            name="axis_test",
            parts=(
                PartNode(id="chassis", is_ground=True),
                PartNode(id="coxa"),
                PartNode(id="femur"),
            ),
            joints=(
                JointEdge(
                    id="j_coxa",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="coxa",
                    origin=(0.0, 100.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                ),
                JointEdge(
                    id="j_femur",
                    joint_type=JointType.REVOLUTE,
                    parent_part="coxa",
                    child_part="femur",
                    origin=(0.0, 200.0, 0.0),
                    axis=(1.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        femur_joint = [j for j in model.joints if j.child == "femur"][0]
        # Child (femur) cumulative yaw = π/2 (inherited from coxa)
        # Rotate axis (1,0,0) by -π/2: (0, -1, 0)
        self.assertAlmostEqual(femur_joint.axis[0], 0.0, places=5)
        self.assertAlmostEqual(femur_joint.axis[1], -1.0, places=5)
        self.assertAlmostEqual(femur_joint.axis[2], 0.0, places=5)

    def test_root_joint_non_z_axis_rotated_to_child_frame(self) -> None:
        """Root-attached joint with non-Z axis: axis rotated by child's full yaw."""
        # Coxa at (0, 100, 0) -> added_yaw = atan2(100, 0) = π/2
        # child_yaw = 0 + π/2 = π/2
        # World-frame axis (1, 0, 0) in child frame:
        # c=cos(-π/2)=0, s=sin(-π/2)=-1 → (1*0-0*(-1), 1*(-1)+0*0, 0) = (0, -1, 0)
        mech = Mechanism(
            name="root_nonz",
            parts=(
                PartNode(id="chassis", is_ground=True),
                PartNode(id="coxa"),
            ),
            joints=(
                JointEdge(
                    id="j_coxa",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="coxa",
                    origin=(0.0, 100.0, 0.0),
                    axis=(1.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertAlmostEqual(joint.axis[0], 0.0, places=5)
        self.assertAlmostEqual(joint.axis[1], -1.0, places=5)
        self.assertAlmostEqual(joint.axis[2], 0.0, places=5)

    def test_hexapod_leg_urdf_axes_and_rpy(self) -> None:
        """Integration: hexapod leg exports correct URDF axes and RPY."""
        # Chassis + one leg: coxa at (70, 75, 0) -> yaw = atan2(75, 70) ≈ 0.8211
        # Femur/tibia have world-frame axis (-0.731, 0.682, 0) for pitch
        # (perpendicular to leg direction).
        import math as m
        dx, dy = 70.0, 75.0
        expected_yaw = m.atan2(dy, dx)  # ≈ 0.8211 rad

        # World-frame pitch axis perpendicular to leg direction
        r = m.sqrt(dx * dx + dy * dy)
        pitch_ax = -dy / r  # -0.731
        pitch_ay = dx / r   #  0.682

        mech = Mechanism(
            name="hexapod_leg",
            parts=(
                PartNode(id="chassis", body_name="Body_Chassis", is_ground=True),
                PartNode(id="coxa", body_name="Body_Coxa"),
                PartNode(id="femur", body_name="Body_Femur"),
                PartNode(id="tibia", body_name="Body_Tibia"),
            ),
            joints=(
                JointEdge(
                    id="j_coxa",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="coxa",
                    origin=(dx, dy, 0.0),
                    axis=(0.0, 0.0, 1.0),
                ),
                JointEdge(
                    id="j_femur",
                    joint_type=JointType.REVOLUTE,
                    parent_part="coxa",
                    child_part="femur",
                    origin=(dx + dx / r * 50, dy + dy / r * 50, 0.0),
                    axis=(pitch_ax, pitch_ay, 0.0),
                ),
                JointEdge(
                    id="j_tibia",
                    joint_type=JointType.REVOLUTE,
                    parent_part="femur",
                    child_part="tibia",
                    origin=(dx + dx / r * 100, dy + dy / r * 100, 0.0),
                    axis=(pitch_ax, pitch_ay, 0.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "Body_Chassis", "mesh_path": "/m/chassis.stl",
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Coxa", "mesh_path": "/m/coxa.stl",
             "placement": {"position": [dx, dy, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Femur", "mesh_path": "/m/femur.stl",
             "placement": {"position": [dx + dx / r * 50, dy + dy / r * 50, 0],
                           "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Tibia", "mesh_path": "/m/tibia.stl",
             "placement": {"position": [dx + dx / r * 100, dy + dy / r * 100, 0],
                           "rotation_quat": [1, 0, 0, 0]}},
        ]

        model = build_sim_model(mech, manifest)

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        joints = {j.attrib["name"]: j for j in root.findall("joint")}

        # Coxa joint: rpy should have yaw = atan2(75, 70) ≈ 0.821
        coxa_rpy = joints["j_coxa"].find("origin").attrib["rpy"]
        coxa_rpy_vals = [float(v) for v in coxa_rpy.split()]
        self.assertAlmostEqual(coxa_rpy_vals[0], 0.0, places=3)
        self.assertAlmostEqual(coxa_rpy_vals[1], 0.0, places=3)
        self.assertAlmostEqual(coxa_rpy_vals[2], expected_yaw, places=3)

        # Coxa axis stays (0 0 1) — Z axis for yaw, no rotation needed
        coxa_axis = joints["j_coxa"].find("axis").attrib["xyz"]
        coxa_axis_vals = [float(v) for v in coxa_axis.split()]
        self.assertAlmostEqual(coxa_axis_vals[0], 0.0, places=3)
        self.assertAlmostEqual(coxa_axis_vals[1], 0.0, places=3)
        self.assertAlmostEqual(coxa_axis_vals[2], 1.0, places=3)

        # Femur joint: rpy should be (0, 0, 0) — deeper joint, no added yaw
        femur_rpy = joints["j_femur"].find("origin").attrib["rpy"]
        femur_rpy_vals = [float(v) for v in femur_rpy.split()]
        for v in femur_rpy_vals:
            self.assertAlmostEqual(v, 0.0, places=3)

        # Femur axis: world-frame (-0.731, 0.682, 0) rotated into local frame
        # by -child_yaw (child inherits parent yaw = atan2(75,70)).
        # In local frame, the perpendicular-to-leg pitch axis should be (0, 1, 0)
        # since +X points outward along the leg.
        femur_axis = joints["j_femur"].find("axis").attrib["xyz"]
        femur_axis_vals = [float(v) for v in femur_axis.split()]
        self.assertAlmostEqual(femur_axis_vals[0], 0.0, places=3)
        self.assertAlmostEqual(femur_axis_vals[1], 1.0, places=3)
        self.assertAlmostEqual(femur_axis_vals[2], 0.0, places=3)

        # Tibia axis: same — perpendicular-to-leg pitch axis = (0, 1, 0) locally
        tibia_axis = joints["j_tibia"].find("axis").attrib["xyz"]
        tibia_axis_vals = [float(v) for v in tibia_axis.split()]
        self.assertAlmostEqual(tibia_axis_vals[0], 0.0, places=3)
        self.assertAlmostEqual(tibia_axis_vals[1], 1.0, places=3)
        self.assertAlmostEqual(tibia_axis_vals[2], 0.0, places=3)


class TestWriteUrdf(unittest.TestCase):
    def _make_simple_model(self) -> SimModel:
        return SimModel(
            name="test_robot",
            links=(
                SimLink(
                    name="base_link",
                    mesh_path="/meshes/base.stl",
                    position=(0.0, 0.0, 0.0),
                    rotation_quat=(1.0, 0.0, 0.0, 0.0),
                    mass_kg=5.0,
                    inertia=(0.01, 0.0, 0.0, 0.01, 0.0, 0.01),
                    is_root=True,
                ),
                SimLink(
                    name="arm_link",
                    mesh_path="/meshes/arm.stl",
                    position=(0.0, 0.0, 0.05),
                    rotation_quat=(1.0, 0.0, 0.0, 0.0),
                    mass_kg=1.0,
                ),
            ),
            joints=(
                SimJoint(
                    name="shoulder",
                    joint_type="revolute",
                    parent="base_link",
                    child="arm_link",
                    axis=(0.0, 0.0, 1.0),
                    origin_xyz=(0.0, 0.0, 0.05),
                    origin_rpy=(0.0, 0.0, 0.0),
                    limits=(-1.57, 1.57),
                ),
            ),
        )

    def test_urdf_written(self) -> None:
        model = self._make_simple_model()
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        result_path = write_urdf(model, path)
        self.assertTrue(result_path.endswith(".urdf"))

        # Parse and validate
        tree = ET.parse(result_path)
        root = tree.getroot()
        self.assertEqual(root.tag, "robot")
        self.assertEqual(root.attrib["name"], "test_robot")

    def test_urdf_links(self) -> None:
        model = self._make_simple_model()
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        links = root.findall("link")
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].attrib["name"], "base_link")
        self.assertEqual(links[1].attrib["name"], "arm_link")

        # Check base_link has visual + collision + inertial
        self.assertIsNotNone(links[0].find("visual"))
        self.assertIsNotNone(links[0].find("collision"))
        self.assertIsNotNone(links[0].find("inertial"))

        # Check mesh path and scale
        mesh = links[0].find("visual/geometry/mesh")
        self.assertIsNotNone(mesh)
        self.assertEqual(mesh.attrib["filename"], "/meshes/base.stl")
        self.assertEqual(mesh.attrib["scale"], "0.001 0.001 0.001")

        # Collision mesh also has scale
        c_mesh = links[0].find("collision/geometry/mesh")
        self.assertIsNotNone(c_mesh)
        self.assertEqual(c_mesh.attrib["scale"], "0.001 0.001 0.001")

        # Visual should NOT have an origin element (identity transform —
        # FreeCAD exports meshes in body-local frame)
        self.assertIsNone(links[0].find("visual/origin"))

        # Check inertial
        mass = links[0].find("inertial/mass")
        self.assertIsNotNone(mass)
        self.assertEqual(mass.attrib["value"], "5")

    def test_urdf_joints(self) -> None:
        model = self._make_simple_model()
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        joints = root.findall("joint")
        self.assertEqual(len(joints), 1)
        joint = joints[0]
        self.assertEqual(joint.attrib["name"], "shoulder")
        self.assertEqual(joint.attrib["type"], "revolute")

        parent = joint.find("parent")
        self.assertEqual(parent.attrib["link"], "base_link")
        child = joint.find("child")
        self.assertEqual(child.attrib["link"], "arm_link")

        axis = joint.find("axis")
        self.assertEqual(axis.attrib["xyz"], "0 0 1")

        limit = joint.find("limit")
        self.assertIsNotNone(limit)
        self.assertEqual(limit.attrib["lower"], "-1.57")
        self.assertEqual(limit.attrib["upper"], "1.57")

    def test_urdf_mimic(self) -> None:
        model = SimModel(
            name="gear_robot",
            links=(
                SimLink(name="frame", is_root=True),
                SimLink(name="gear_a"),
                SimLink(name="gear_b"),
            ),
            joints=(
                SimJoint(
                    name="shaft_a",
                    joint_type="revolute",
                    parent="frame",
                    child="gear_a",
                    limits=(-3.14, 3.14),
                ),
                SimJoint(
                    name="mesh_ab",
                    joint_type="revolute",
                    parent="gear_a",
                    child="gear_b",
                    mimic=("shaft_a", 0.5),
                    limits=(-3.14, 3.14),
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        joints = root.findall("joint")
        mimic_joint = joints[1]
        mimic_el = mimic_joint.find("mimic")
        self.assertIsNotNone(mimic_el)
        self.assertEqual(mimic_el.attrib["joint"], "shaft_a")
        self.assertEqual(mimic_el.attrib["multiplier"], "0.5")

    def test_urdf_link_without_mesh(self) -> None:
        """Links without mesh should have no visual/collision."""
        model = SimModel(
            name="minimal",
            links=(SimLink(name="empty_link"),),
            joints=(),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        link = root.findall("link")[0]
        self.assertIsNone(link.find("visual"))
        self.assertIsNone(link.find("collision"))

    def test_urdf_fixed_joint_no_limits(self) -> None:
        """Fixed joints should have no limit element."""
        model = SimModel(
            name="no_limits",
            links=(
                SimLink(name="a", is_root=True),
                SimLink(name="b"),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="fixed",
                    parent="a",
                    child="b",
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        joint = root.findall("joint")[0]
        self.assertIsNone(joint.find("limit"))

    def test_revolute_without_limits_raises(self) -> None:
        """SimJoint schema enforces that revolute joints require limits."""
        with self.assertRaises(ValueError, msg="revolute joints require limits"):
            SimJoint(
                name="j1",
                joint_type="revolute",
                parent="a",
                child="b",
                limits=None,
            )

    def test_mesh_scale_attribute(self) -> None:
        """Both visual and collision meshes have scale='0.001 0.001 0.001'."""
        model = SimModel(
            name="scaled",
            links=(
                SimLink(name="link1", mesh_path="/m/link1.stl"),
            ),
            joints=(),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        link = root.findall("link")[0]
        v_mesh = link.find("visual/geometry/mesh")
        c_mesh = link.find("collision/geometry/mesh")
        self.assertEqual(v_mesh.attrib["scale"], "0.001 0.001 0.001")
        self.assertEqual(c_mesh.attrib["scale"], "0.001 0.001 0.001")

    def test_dynamics_emitted(self) -> None:
        """Revolute joint has <dynamics> element with damping and friction."""
        model = SimModel(
            name="dyn",
            links=(
                SimLink(name="a", is_root=True),
                SimLink(name="b"),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="revolute",
                    parent="a",
                    child="b",
                    limits=(-3.14, 3.14),
                    damping=0.5,
                    friction=0.1,
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        joint = root.findall("joint")[0]
        dynamics = joint.find("dynamics")
        self.assertIsNotNone(dynamics)
        self.assertEqual(dynamics.attrib["damping"], "0.5")
        self.assertEqual(dynamics.attrib["friction"], "0.1")

    def test_dynamics_not_on_fixed(self) -> None:
        """Fixed joint has no <dynamics> element."""
        model = SimModel(
            name="fixed_no_dyn",
            links=(
                SimLink(name="a", is_root=True),
                SimLink(name="b"),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="fixed",
                    parent="a",
                    child="b",
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        joint = root.findall("joint")[0]
        self.assertIsNone(joint.find("dynamics"))

    def test_effort_velocity_in_urdf(self) -> None:
        """Effort/velocity from SimJoint appear in URDF <limit>."""
        model = SimModel(
            name="ev",
            links=(
                SimLink(name="a", is_root=True),
                SimLink(name="b"),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="revolute",
                    parent="a",
                    child="b",
                    limits=(-1.0, 1.0),
                    effort=50.0,
                    velocity=6.28,
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        limit = root.findall("joint")[0].find("limit")
        self.assertEqual(limit.attrib["effort"], "50")
        self.assertEqual(limit.attrib["velocity"], "6.28")

    def test_inertia_6tuple_in_urdf(self) -> None:
        """SimLink with 6-tuple inertia is written correctly to URDF."""
        model = SimModel(
            name="inertia_test",
            links=(
                SimLink(
                    name="link1",
                    mass_kg=2.0,
                    inertia=(0.01, 0.002, 0.003, 0.02, 0.004, 0.03),
                ),
            ),
            joints=(),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        inertia = root.findall("link")[0].find("inertial/inertia")
        self.assertIsNotNone(inertia)
        self.assertEqual(inertia.attrib["ixx"], "0.01")
        self.assertEqual(inertia.attrib["ixy"], "0.002")
        self.assertEqual(inertia.attrib["ixz"], "0.003")
        self.assertEqual(inertia.attrib["iyy"], "0.02")
        self.assertEqual(inertia.attrib["iyz"], "0.004")
        self.assertEqual(inertia.attrib["izz"], "0.03")


class TestGroundClearance(unittest.TestCase):
    """Ground clearance: base_link insertion when ground_clearance_m is set."""

    def _make_mechanism(self) -> Mechanism:
        return Mechanism(
            name="hexapod",
            parts=(
                PartNode(id="chassis", body_name="Body_Chassis", is_ground=True, mass_kg=1.0),
                PartNode(id="leg", body_name="Body_Leg", mass_kg=0.1),
            ),
            joints=(
                JointEdge(
                    id="hip",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="leg",
                    origin=(60.0, 65.0, 0.0),
                ),
            ),
            drives=(),
        )

    def _make_manifest(self) -> list[dict]:
        return [
            {
                "name": "Body_Chassis",
                "mesh_path": "/m/chassis.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [150, 100, 3],
                "bbox_min_mm": [0, 0, 0],
            },
            {
                "name": "Body_Leg",
                "mesh_path": "/m/leg.stl",
                "placement": {"position": [60, 65, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [30, 30, 120],
                "bbox_min_mm": [-15, -15, -120],
            },
        ]

    def test_no_base_link_without_param(self) -> None:
        """Without ground_clearance_m, no base_link is added."""
        model = build_sim_model(self._make_mechanism(), self._make_manifest())
        self.assertEqual(len(model.links), 2)
        self.assertEqual(model.links[0].name, "chassis")
        self.assertTrue(model.links[0].is_root)

    def test_base_link_added_with_clearance(self) -> None:
        """With ground_clearance_m, base_link + fixed joint are prepended."""
        model = build_sim_model(
            self._make_mechanism(),
            self._make_manifest(),
            ground_clearance_m=0.125,
        )
        self.assertEqual(len(model.links), 3)
        self.assertEqual(model.links[0].name, "base_link")
        self.assertTrue(model.links[0].is_root)
        self.assertIsNone(model.links[0].mesh_path)

        # Original root demoted
        chassis = next(lk for lk in model.links if lk.name == "chassis")
        self.assertFalse(chassis.is_root)

        # Fixed joint inserted
        self.assertEqual(len(model.joints), 2)
        base_joint = model.joints[0]
        self.assertEqual(base_joint.name, "base_to_chassis")
        self.assertEqual(base_joint.joint_type, "fixed")
        self.assertEqual(base_joint.parent, "base_link")
        self.assertEqual(base_joint.child, "chassis")
        self.assertAlmostEqual(base_joint.origin_xyz[2], 0.125, places=6)

    def test_urdf_with_ground_clearance(self) -> None:
        """URDF output includes base_link and fixed joint."""
        model = build_sim_model(
            self._make_mechanism(),
            self._make_manifest(),
            ground_clearance_m=0.125,
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        tree = ET.parse(path)
        root = tree.getroot()
        links = root.findall("link")
        joints = root.findall("joint")

        self.assertEqual(len(links), 3)
        self.assertEqual(links[0].attrib["name"], "base_link")
        # base_link has no visual
        self.assertIsNone(links[0].find("visual"))

        self.assertEqual(len(joints), 2)
        self.assertEqual(joints[0].attrib["name"], "base_to_chassis")
        self.assertEqual(joints[0].attrib["type"], "fixed")
        origin = joints[0].find("origin")
        self.assertIn("0.125", origin.attrib["xyz"])

    def test_zero_clearance_no_base_link(self) -> None:
        """ground_clearance_m=0 doesn't add base_link."""
        model = build_sim_model(
            self._make_mechanism(),
            self._make_manifest(),
            ground_clearance_m=0.0,
        )
        self.assertEqual(len(model.links), 2)

    def test_negative_clearance_no_base_link(self) -> None:
        """Negative clearance is ignored."""
        model = build_sim_model(
            self._make_mechanism(),
            self._make_manifest(),
            ground_clearance_m=-0.1,
        )
        self.assertEqual(len(model.links), 2)


class TestEndToEnd(unittest.TestCase):
    """Integration test: mechanism -> build_sim_model -> write_urdf -> parse."""

    def test_mechanism_to_urdf(self) -> None:
        mechanism = Mechanism(
            name="two_link",
            parts=(
                PartNode(id="ground", body_name="Body_Ground", is_ground=True),
                PartNode(id="link1", body_name="Body_Link1"),
                PartNode(id="link2", body_name="Body_Link2"),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="ground",
                    child_part="link1",
                    origin=(0.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="j2",
                    joint_type=JointType.REVOLUTE,
                    parent_part="link1",
                    child_part="link2",
                    origin=(100.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )

        manifest = [
            {"name": "Body_Ground", "mesh_path": "/m/ground.stl", "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Link1", "mesh_path": "/m/link1.stl", "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "Body_Link2", "mesh_path": "/m/link2.stl", "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
        ]

        model = build_sim_model(mechanism, manifest)
        self.assertEqual(len(model.links), 3)
        self.assertEqual(len(model.joints), 2)

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        urdf_path = write_urdf(model, path)
        tree = ET.parse(urdf_path)
        root = tree.getroot()

        self.assertEqual(root.attrib["name"], "two_link")
        self.assertEqual(len(root.findall("link")), 3)
        self.assertEqual(len(root.findall("joint")), 2)

        # j2 origin should be at 0.1m (100mm -> 0.1m)
        j2 = root.findall("joint")[1]
        origin = j2.find("origin")
        self.assertIn("0.1", origin.attrib["xyz"])


class TestBoxInertia(unittest.TestCase):
    def test_cube(self) -> None:
        """1kg cube with 1m sides."""
        inertia = _box_inertia(1.0, 1.0, 1.0, 1.0)
        expected = 1.0 / 12.0 * (1.0 + 1.0)
        self.assertAlmostEqual(inertia[0], expected)  # ixx
        self.assertAlmostEqual(inertia[3], expected)  # iyy
        self.assertAlmostEqual(inertia[5], expected)  # izz
        self.assertAlmostEqual(inertia[1], 0.0)  # ixy
        self.assertAlmostEqual(inertia[2], 0.0)  # ixz
        self.assertAlmostEqual(inertia[4], 0.0)  # iyz

    def test_rectangular(self) -> None:
        """Asymmetric box should have different diagonal values."""
        inertia = _box_inertia(2.0, 0.1, 0.2, 0.3)
        ixx = 2.0 / 12.0 * (0.2 ** 2 + 0.3 ** 2)
        iyy = 2.0 / 12.0 * (0.1 ** 2 + 0.3 ** 2)
        izz = 2.0 / 12.0 * (0.1 ** 2 + 0.2 ** 2)
        self.assertAlmostEqual(inertia[0], ixx, places=10)
        self.assertAlmostEqual(inertia[3], iyy, places=10)
        self.assertAlmostEqual(inertia[5], izz, places=10)


class TestSchemaValidation(unittest.TestCase):
    """Tests for SimLink/SimJoint __post_init__ validation."""

    def test_revolute_requires_limits(self) -> None:
        with self.assertRaises(ValueError):
            SimJoint(name="j", joint_type="revolute", parent="a", child="b", limits=None)

    def test_prismatic_requires_limits(self) -> None:
        with self.assertRaises(ValueError):
            SimJoint(name="j", joint_type="prismatic", parent="a", child="b", limits=None)

    def test_fixed_allows_no_limits(self) -> None:
        j = SimJoint(name="j", joint_type="fixed", parent="a", child="b", limits=None)
        self.assertIsNone(j.limits)

    def test_limits_lower_gt_upper_raises(self) -> None:
        with self.assertRaises(ValueError):
            SimJoint(
                name="j", joint_type="revolute", parent="a", child="b",
                limits=(1.0, -1.0),
            )

    def test_non_unit_axis_raises(self) -> None:
        with self.assertRaises(ValueError):
            SimJoint(
                name="j", joint_type="revolute", parent="a", child="b",
                axis=(1.0, 1.0, 0.0),  # magnitude sqrt(2), not 1.0
                limits=(-1.0, 1.0),
            )

    def test_negative_mass_raises(self) -> None:
        with self.assertRaises(ValueError):
            SimLink(name="lk", mass_kg=-1.0)

    def test_negative_inertia_diagonal_raises(self) -> None:
        with self.assertRaises(ValueError):
            SimLink(name="lk", inertia=(-0.01, 0.0, 0.0, 0.01, 0.0, 0.01))

    def test_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            SimLink(name="")
        with self.assertRaises(ValueError):
            SimJoint(name="", joint_type="fixed", parent="a", child="b")

    def test_valid_revolute_joint(self) -> None:
        j = SimJoint(
            name="j1", joint_type="revolute", parent="a", child="b",
            limits=(-1.57, 1.57),
        )
        self.assertEqual(j.limits, (-1.57, 1.57))

    def test_build_sim_model_provides_default_limits(self) -> None:
        """build_sim_model adds default limits for revolute joints without explicit limits."""
        mech = Mechanism(
            name="test",
            parts=(
                PartNode(id="base", is_ground=True),
                PartNode(id="arm"),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                    # No min_angle_deg / max_angle_deg
                ),
            ),
            drives=(),
        )
        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertIsNotNone(joint.limits)
        self.assertAlmostEqual(joint.limits[0], -math.pi, places=6)
        self.assertAlmostEqual(joint.limits[1], math.pi, places=6)

    def test_build_sim_model_prismatic_default_limits(self) -> None:
        """build_sim_model adds default limits for prismatic joints without explicit limits."""
        mech = Mechanism(
            name="test",
            parts=(
                PartNode(id="rail", is_ground=True),
                PartNode(id="cart"),
            ),
            joints=(
                JointEdge(
                    id="slide",
                    joint_type=JointType.PRISMATIC,
                    parent_part="rail",
                    child_part="cart",
                    axis=(1.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )
        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertIsNotNone(joint.limits)
        self.assertAlmostEqual(joint.limits[0], 0.0)
        self.assertAlmostEqual(joint.limits[1], 1.0)


class TestValidateUrdf(unittest.TestCase):
    """Tests for the post-generation URDF validator."""

    def _write_valid_urdf(self) -> str:
        """Write a valid URDF and return its path."""
        model = SimModel(
            name="valid_robot",
            links=(
                SimLink(
                    name="base_link",
                    mesh_path="/m/base.stl",
                    mass_kg=5.0,
                    inertia=(0.01, 0.0, 0.0, 0.01, 0.0, 0.01),
                    is_root=True,
                ),
                SimLink(
                    name="arm",
                    mesh_path="/m/arm.stl",
                    mass_kg=1.0,
                    inertia=(0.001, 0.0, 0.0, 0.001, 0.0, 0.001),
                ),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="revolute",
                    parent="base_link",
                    child="arm",
                    limits=(-1.57, 1.57),
                ),
            ),
        )
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)
        return path

    def test_valid_urdf_no_findings(self) -> None:
        path = self._write_valid_urdf()
        findings = validate_urdf(path)
        blockers = [f for f in findings if f.severity.value == "block"]
        self.assertEqual(blockers, [])

    def test_detects_missing_limit(self) -> None:
        """Manually write a revolute joint without <limit> and validate."""
        import xml.etree.ElementTree as ET2
        robot = ET2.Element("robot", name="bad")
        ET2.SubElement(robot, "link", name="a")
        ET2.SubElement(robot, "link", name="b")
        joint = ET2.SubElement(robot, "joint", name="j1", type="revolute")
        ET2.SubElement(joint, "parent", link="a")
        ET2.SubElement(joint, "child", link="b")
        ET2.SubElement(joint, "origin", xyz="0 0 0.1", rpy="0 0 0")
        ET2.SubElement(joint, "axis", xyz="0 0 1")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        tree = ET2.ElementTree(robot)
        tree.write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.missing_limit", rule_ids)

    def test_detects_large_origin(self) -> None:
        """Joint origin with huge magnitude triggers warning."""
        robot = ET.Element("robot", name="big_origin")
        ET.SubElement(robot, "link", name="a")
        ET.SubElement(robot, "link", name="b")
        joint = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(joint, "parent", link="a")
        ET.SubElement(joint, "child", link="b")
        ET.SubElement(joint, "origin", xyz="100 200 300", rpy="0 0 0")
        ET.SubElement(joint, "axis", xyz="0 0 1")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.origin_magnitude", rule_ids)

    def test_detects_non_unit_axis(self) -> None:
        robot = ET.Element("robot", name="bad_axis")
        ET.SubElement(robot, "link", name="a")
        ET.SubElement(robot, "link", name="b")
        joint = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(joint, "parent", link="a")
        ET.SubElement(joint, "child", link="b")
        ET.SubElement(joint, "origin", xyz="0 0 0", rpy="0 0 0")
        ET.SubElement(joint, "axis", xyz="1 1 0")  # magnitude sqrt(2)

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.axis_not_unit", rule_ids)

    def test_detects_disconnected_link(self) -> None:
        robot = ET.Element("robot", name="disconnected")
        ET.SubElement(robot, "link", name="a")
        ET.SubElement(robot, "link", name="b")
        ET.SubElement(robot, "link", name="orphan")
        joint = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(joint, "parent", link="a")
        ET.SubElement(joint, "child", link="b")
        ET.SubElement(joint, "origin", xyz="0 0 0", rpy="0 0 0")
        ET.SubElement(joint, "axis", xyz="0 0 1")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.disconnected_link", rule_ids)

    def test_detects_negative_inertia(self) -> None:
        robot = ET.Element("robot", name="neg_inertia")
        link = ET.SubElement(robot, "link", name="a")
        inertial = ET.SubElement(link, "inertial")
        ET.SubElement(inertial, "mass", value="1.0")
        inertia = ET.SubElement(inertial, "inertia")
        inertia.set("ixx", "-0.01")
        inertia.set("ixy", "0")
        inertia.set("ixz", "0")
        inertia.set("iyy", "0.01")
        inertia.set("iyz", "0")
        inertia.set("izz", "0.01")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.negative_inertia", rule_ids)

    def test_detects_dangling_parent(self) -> None:
        robot = ET.Element("robot", name="dangling")
        ET.SubElement(robot, "link", name="a")
        joint = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(joint, "parent", link="nonexistent")
        ET.SubElement(joint, "child", link="a")
        ET.SubElement(joint, "origin", xyz="0 0 0", rpy="0 0 0")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.dangling_parent", rule_ids)

    def test_detects_duplicate_mesh(self) -> None:
        robot = ET.Element("robot", name="dup_mesh")
        for lname in ("a", "b"):
            link = ET.SubElement(robot, "link", name=lname)
            vis = ET.SubElement(link, "visual")
            geom = ET.SubElement(vis, "geometry")
            ET.SubElement(geom, "mesh", filename="/m/shared.stl", scale="0.001 0.001 0.001")
        joint = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(joint, "parent", link="a")
        ET.SubElement(joint, "child", link="b")
        ET.SubElement(joint, "origin", xyz="0 0 0", rpy="0 0 0")
        ET.SubElement(joint, "axis", xyz="0 0 1")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.duplicate_mesh", rule_ids)

    def test_detects_inconsistent_scale(self) -> None:
        robot = ET.Element("robot", name="bad_scale")
        link1 = ET.SubElement(robot, "link", name="a")
        vis1 = ET.SubElement(link1, "visual")
        geom1 = ET.SubElement(vis1, "geometry")
        ET.SubElement(geom1, "mesh", filename="/m/a.stl", scale="0.001 0.001 0.001")

        link2 = ET.SubElement(robot, "link", name="b")
        vis2 = ET.SubElement(link2, "visual")
        geom2 = ET.SubElement(vis2, "geometry")
        ET.SubElement(geom2, "mesh", filename="/m/b.stl", scale="1 1 1")

        joint = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(joint, "parent", link="a")
        ET.SubElement(joint, "child", link="b")
        ET.SubElement(joint, "origin", xyz="0 0 0", rpy="0 0 0")

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        ET.ElementTree(robot).write(path, encoding="unicode")

        findings = validate_urdf(path)
        rule_ids = {f.rule_id for f in findings}
        self.assertIn("urdf.inconsistent_scale", rule_ids)


class TestFKHelpers(unittest.TestCase):
    """Unit tests for FK math helper functions."""

    def test_rpy_identity(self) -> None:
        """(0,0,0) -> identity matrix."""
        m = _rpy_to_matrix(0.0, 0.0, 0.0)
        for i in range(3):
            for j in range(3):
                expected = 1.0 if i == j else 0.0
                self.assertAlmostEqual(m[i][j], expected, places=10)

    def test_rpy_yaw_90(self) -> None:
        """(0, 0, π/2) -> 90° rotation about Z."""
        m = _rpy_to_matrix(0.0, 0.0, math.pi / 2)
        # R_z(90°) = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        self.assertAlmostEqual(m[0][0], 0.0, places=10)
        self.assertAlmostEqual(m[0][1], -1.0, places=10)
        self.assertAlmostEqual(m[1][0], 1.0, places=10)
        self.assertAlmostEqual(m[1][1], 0.0, places=10)
        self.assertAlmostEqual(m[2][2], 1.0, places=10)

    def test_rpy_pitch_90(self) -> None:
        """(0, π/2, 0) -> 90° rotation about Y."""
        m = _rpy_to_matrix(0.0, math.pi / 2, 0.0)
        # R_y(90°) = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
        self.assertAlmostEqual(m[0][0], 0.0, places=10)
        self.assertAlmostEqual(m[0][2], 1.0, places=10)
        self.assertAlmostEqual(m[2][0], -1.0, places=10)
        self.assertAlmostEqual(m[2][2], 0.0, places=10)

    def test_transform_identity(self) -> None:
        """Identity transform preserves points."""
        t = _make_transform_4x4((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        pt = (1.0, 2.0, 3.0)
        result = _transform_point(t, pt)
        for i in range(3):
            self.assertAlmostEqual(result[i], pt[i], places=10)

    def test_transform_translation(self) -> None:
        """Pure translation works."""
        t = _make_transform_4x4((10.0, 20.0, 30.0), (0.0, 0.0, 0.0))
        result = _transform_point(t, (1.0, 2.0, 3.0))
        self.assertAlmostEqual(result[0], 11.0, places=10)
        self.assertAlmostEqual(result[1], 22.0, places=10)
        self.assertAlmostEqual(result[2], 33.0, places=10)

    def test_transform_chain(self) -> None:
        """Two transforms compose correctly."""
        t1 = _make_transform_4x4((1.0, 0.0, 0.0), (0.0, 0.0, math.pi / 2))
        t2 = _make_transform_4x4((1.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        t_composed = _multiply_4x4(t1, t2)
        # After t1 rotates 90° about Z then translates (1,0,0):
        # t2 translates (1,0,0) in t1's frame = (0,1,0) in world + (1,0,0) from t1
        result = _transform_point(t_composed, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(result[0], 1.0, places=10)
        self.assertAlmostEqual(result[1], 1.0, places=10)
        self.assertAlmostEqual(result[2], 0.0, places=10)

    def test_transform_bbox(self) -> None:
        """Known bbox through known transform -> expected world AABB."""
        bbox = MeshBBox(min_pt=(0.0, -5.0, -5.0), max_pt=(10.0, 5.0, 5.0))
        # Identity transform, scale 0.001 (mm -> m)
        t = _make_transform_4x4((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        lo, hi = _transform_bbox(t, bbox, scale=0.001)
        self.assertAlmostEqual(lo[0], 0.0, places=6)
        self.assertAlmostEqual(lo[1], -0.005, places=6)
        self.assertAlmostEqual(hi[0], 0.01, places=6)
        self.assertAlmostEqual(hi[1], 0.005, places=6)

    def test_transform_bbox_rotated(self) -> None:
        """Bbox through 90° yaw rotation."""
        bbox = MeshBBox(min_pt=(0.0, -1.0, -1.0), max_pt=(10.0, 1.0, 1.0))
        t = _make_transform_4x4((0.0, 0.0, 0.0), (0.0, 0.0, math.pi / 2))
        lo, hi = _transform_bbox(t, bbox, scale=1.0)
        # After 90° yaw: x-axis becomes y-axis
        self.assertAlmostEqual(lo[0], -1.0, places=5)
        self.assertAlmostEqual(hi[1], 10.0, places=5)

    def test_aabb_volume(self) -> None:
        vol = _aabb_volume((0.0, 0.0, 0.0), (2.0, 3.0, 4.0))
        self.assertAlmostEqual(vol, 24.0)

    def test_aabb_overlap_volume(self) -> None:
        """50% overlap on each axis."""
        overlap = _aabb_overlap_volume(
            (0.0, 0.0, 0.0), (2.0, 2.0, 2.0),
            (1.0, 1.0, 1.0), (3.0, 3.0, 3.0),
        )
        self.assertAlmostEqual(overlap, 1.0)  # 1x1x1

    def test_aabb_no_overlap(self) -> None:
        overlap = _aabb_overlap_volume(
            (0.0, 0.0, 0.0), (1.0, 1.0, 1.0),
            (2.0, 2.0, 2.0), (3.0, 3.0, 3.0),
        )
        self.assertAlmostEqual(overlap, 0.0)


class TestValidateUrdfFK(unittest.TestCase):
    """Tests for the FK validator with hand-crafted URDFs."""

    def _write_urdf_xml(self, robot_el: ET.Element) -> str:
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        tree = ET.ElementTree(robot_el)
        tree.write(path, encoding="unicode", xml_declaration=True)
        return path

    def test_valid_urdf_no_fk_findings(self) -> None:
        """A well-formed simple URDF produces no FK findings."""
        model = SimModel(
            name="simple",
            links=(
                SimLink(name="base_link", is_root=True),
                SimLink(name="arm", mesh_path="/m/arm.stl"),
            ),
            joints=(
                SimJoint(
                    name="j1", joint_type="revolute", parent="base_link",
                    child="arm", limits=(-1.57, 1.57),
                    origin_xyz=(0.0, 0.0, 0.1),
                ),
            ),
        )
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)
        findings = validate_urdf_fk(path)
        blockers = [f for f in findings if f.severity == Severity.BLOCK]
        self.assertEqual(blockers, [])

    def test_chassis_height_check(self) -> None:
        """Warns when chassis Z doesn't match ground_clearance_m."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="base_link")
        ET.SubElement(robot, "link", name="chassis")
        j = ET.SubElement(robot, "joint", name="base_to_chassis", type="fixed")
        ET.SubElement(j, "parent", link="base_link")
        ET.SubElement(j, "child", link="chassis")
        ET.SubElement(j, "origin", xyz="0 0 0.16", rpy="0 0 0")
        ET.SubElement(j, "axis", xyz="0 0 1")

        path = self._write_urdf_xml(robot)
        # Should pass — 0.16m matches
        findings = validate_urdf_fk(path, ground_clearance_m=0.16)
        chassis_findings = [f for f in findings if f.rule_id == "urdf.fk.chassis_height"]
        self.assertEqual(len(chassis_findings), 0)

        # Should warn — 0.20m doesn't match 0.16m
        findings = validate_urdf_fk(path, ground_clearance_m=0.20)
        chassis_findings = [f for f in findings if f.rule_id == "urdf.fk.chassis_height"]
        self.assertEqual(len(chassis_findings), 1)

    def test_coxa_yaw_mismatch(self) -> None:
        """Warns when coxa joint yaw doesn't match radial direction."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="base_link")
        ET.SubElement(robot, "link", name="chassis")
        ET.SubElement(robot, "link", name="coxa_L1")

        # Fixed base_link -> chassis
        j0 = ET.SubElement(robot, "joint", name="base_to_chassis", type="fixed")
        ET.SubElement(j0, "parent", link="base_link")
        ET.SubElement(j0, "child", link="chassis")
        ET.SubElement(j0, "origin", xyz="0 0 0.16", rpy="0 0 0")
        ET.SubElement(j0, "axis", xyz="0 0 1")

        # Coxa joint with wrong yaw (yaw=0 but position at 45°)
        j1 = ET.SubElement(robot, "joint", name="j_coxa_L1", type="revolute")
        ET.SubElement(j1, "parent", link="chassis")
        ET.SubElement(j1, "child", link="coxa_L1")
        ET.SubElement(j1, "origin", xyz="0.07 0.07 0", rpy="0 0 0")  # yaw should be π/4
        ET.SubElement(j1, "axis", xyz="0 0 1")
        limit = ET.SubElement(j1, "limit")
        limit.set("lower", "-1.57")
        limit.set("upper", "1.57")
        limit.set("effort", "100")
        limit.set("velocity", "10")

        path = self._write_urdf_xml(robot)
        findings = validate_urdf_fk(path)
        coxa_findings = [f for f in findings if f.rule_id == "urdf.fk.coxa_yaw_matches_radial"]
        self.assertEqual(len(coxa_findings), 1)

    def test_coxa_yaw_correct(self) -> None:
        """No warning when coxa yaw matches radial direction."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="chassis")
        ET.SubElement(robot, "link", name="coxa_L1")

        yaw = math.atan2(0.075, 0.07)
        j1 = ET.SubElement(robot, "joint", name="j_coxa_L1", type="revolute")
        ET.SubElement(j1, "parent", link="chassis")
        ET.SubElement(j1, "child", link="coxa_L1")
        ET.SubElement(j1, "origin", xyz="0.07 0.075 0", rpy=f"0 0 {yaw:.6f}")
        ET.SubElement(j1, "axis", xyz="0 0 1")
        limit = ET.SubElement(j1, "limit")
        limit.set("lower", "-1.57")
        limit.set("upper", "1.57")
        limit.set("effort", "100")
        limit.set("velocity", "10")

        path = self._write_urdf_xml(robot)
        findings = validate_urdf_fk(path)
        coxa_findings = [f for f in findings if f.rule_id == "urdf.fk.coxa_yaw_matches_radial"]
        self.assertEqual(len(coxa_findings), 0)

    def test_pitch_axis_wrong(self) -> None:
        """Warns when femur axis isn't aligned with local Y."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="chassis")
        ET.SubElement(robot, "link", name="femur_L1")

        j = ET.SubElement(robot, "joint", name="j_femur_L1", type="revolute")
        ET.SubElement(j, "parent", link="chassis")
        ET.SubElement(j, "child", link="femur_L1")
        ET.SubElement(j, "origin", xyz="0 0 0", rpy="0 0 0")
        ET.SubElement(j, "axis", xyz="1 0 0")  # Wrong — should be (0, ±1, 0)
        limit = ET.SubElement(j, "limit")
        limit.set("lower", "-1.57")
        limit.set("upper", "1.57")
        limit.set("effort", "100")
        limit.set("velocity", "10")

        path = self._write_urdf_xml(robot)
        findings = validate_urdf_fk(path)
        axis_findings = [f for f in findings if f.rule_id == "urdf.fk.pitch_axis_local"]
        self.assertEqual(len(axis_findings), 1)

    def test_pitch_axis_correct(self) -> None:
        """No warning when femur axis is (0, 1, 0)."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="chassis")
        ET.SubElement(robot, "link", name="femur_L1")

        j = ET.SubElement(robot, "joint", name="j_femur_L1", type="revolute")
        ET.SubElement(j, "parent", link="chassis")
        ET.SubElement(j, "child", link="femur_L1")
        ET.SubElement(j, "origin", xyz="0 0 0", rpy="0 0 0")
        ET.SubElement(j, "axis", xyz="0 1 0")
        limit = ET.SubElement(j, "limit")
        limit.set("lower", "-1.57")
        limit.set("upper", "1.57")
        limit.set("effort", "100")
        limit.set("velocity", "10")

        path = self._write_urdf_xml(robot)
        findings = validate_urdf_fk(path)
        axis_findings = [f for f in findings if f.rule_id == "urdf.fk.pitch_axis_local"]
        self.assertEqual(len(axis_findings), 0)

    def test_link_chain_gap_detected(self) -> None:
        """BLOCK when parent mesh tip doesn't reach child joint origin."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="parent_link")
        ET.SubElement(robot, "link", name="child_link")

        j = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(j, "parent", link="parent_link")
        ET.SubElement(j, "child", link="child_link")
        ET.SubElement(j, "origin", xyz="0.5 0 0", rpy="0 0 0")  # 500mm away
        ET.SubElement(j, "axis", xyz="0 0 1")

        path = self._write_urdf_xml(robot)
        # Parent bbox only extends to 100mm along X
        bboxes = {
            "parent_link": MeshBBox(min_pt=(0.0, -10.0, -10.0), max_pt=(100.0, 10.0, 10.0)),
        }
        findings = validate_urdf_fk(path, mesh_bboxes=bboxes)
        gap_findings = [f for f in findings if f.rule_id == "urdf.fk.link_chain_gap"]
        self.assertEqual(len(gap_findings), 1)
        self.assertEqual(gap_findings[0].severity, Severity.BLOCK)

    def test_mesh_overlap_detected(self) -> None:
        """WARN when non-parent-child AABBs overlap significantly."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="root")
        ET.SubElement(robot, "link", name="a")
        ET.SubElement(robot, "link", name="b")

        j1 = ET.SubElement(robot, "joint", name="j1", type="fixed")
        ET.SubElement(j1, "parent", link="root")
        ET.SubElement(j1, "child", link="a")
        ET.SubElement(j1, "origin", xyz="0 0 0", rpy="0 0 0")
        ET.SubElement(j1, "axis", xyz="0 0 1")

        j2 = ET.SubElement(robot, "joint", name="j2", type="fixed")
        ET.SubElement(j2, "parent", link="root")
        ET.SubElement(j2, "child", link="b")
        ET.SubElement(j2, "origin", xyz="0 0 0", rpy="0 0 0")  # Same position
        ET.SubElement(j2, "axis", xyz="0 0 1")

        path = self._write_urdf_xml(robot)
        # Both links at same position with same bbox -> 100% overlap
        bboxes = {
            "a": MeshBBox(min_pt=(0.0, 0.0, 0.0), max_pt=(100.0, 100.0, 100.0)),
            "b": MeshBBox(min_pt=(0.0, 0.0, 0.0), max_pt=(100.0, 100.0, 100.0)),
        }
        findings = validate_urdf_fk(path, mesh_bboxes=bboxes)
        overlap_findings = [f for f in findings if f.rule_id == "urdf.fk.no_mesh_overlap"]
        self.assertGreater(len(overlap_findings), 0)


# ---------------------------------------------------------------------------
# Hexapod round-trip helper + test
# ---------------------------------------------------------------------------

def _make_hexapod_mechanism() -> tuple[
    Mechanism,
    list[dict[str, Any]],
    dict[str, MeshBBox],
]:
    """Build a canonical 18-DOF hexapod mechanism for testing.

    Returns (mechanism, body_manifest, mesh_bboxes).
    """
    # Leg positions: 6 legs arranged around chassis center
    # Front-left, Mid-left, Rear-left, Front-right, Mid-right, Rear-right
    leg_positions = [
        ("L1", 70.0, 75.0),
        ("L2", 0.0, 85.0),
        ("L3", -70.0, 75.0),
        ("R1", 70.0, -75.0),
        ("R2", 0.0, -85.0),
        ("R3", -70.0, -75.0),
    ]

    # Segment lengths (mm along radial direction from joint)
    coxa_len = 26.0   # half of 52mm bbox
    femur_len = 33.0   # half of 66mm bbox
    tibia_len = 66.5   # half of 133mm bbox

    parts: list[PartNode] = [
        PartNode(id="chassis", body_name="Body_Chassis", is_ground=True, mass_kg=0.5),
    ]
    joints: list[JointEdge] = []
    manifest: list[dict[str, Any]] = [
        {
            "name": "Body_Chassis",
            "mesh_path": "/m/Body_Chassis.stl",
            "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
            "bbox_mm": [190.0, 150.0, 8.0],
            "bbox_min_mm": [-95.0, -75.0, -4.0],
        },
    ]
    mesh_bboxes: dict[str, MeshBBox] = {
        "chassis": MeshBBox(min_pt=(-95.0, -75.0, -4.0), max_pt=(95.0, 75.0, 4.0)),
    }

    for leg_id, cx, cy in leg_positions:
        r = math.sqrt(cx * cx + cy * cy)
        dx = cx / r if r > 0 else 1.0
        dy = cy / r if r > 0 else 0.0

        # World-frame pitch axis: perpendicular to leg direction
        pitch_ax = -dy
        pitch_ay = dx

        coxa_id = f"coxa_{leg_id}"
        femur_id = f"femur_{leg_id}"
        tibia_id = f"tibia_{leg_id}"

        # Joint positions (world frame, mm)
        coxa_pos = (cx, cy, 0.0)
        femur_pos = (cx + dx * coxa_len * 2, cy + dy * coxa_len * 2, 0.0)
        tibia_pos = (cx + dx * (coxa_len * 2 + femur_len * 2), cy + dy * (coxa_len * 2 + femur_len * 2), 0.0)

        parts.extend([
            PartNode(id=coxa_id, body_name=f"Body_Coxa_{leg_id}", mass_kg=0.02),
            PartNode(id=femur_id, body_name=f"Body_Femur_{leg_id}", mass_kg=0.03),
            PartNode(id=tibia_id, body_name=f"Body_Tibia_{leg_id}", mass_kg=0.02),
        ])

        joints.extend([
            JointEdge(
                id=f"j_coxa_{leg_id}",
                joint_type=JointType.REVOLUTE,
                parent_part="chassis",
                child_part=coxa_id,
                origin=coxa_pos,
                axis=(0.0, 0.0, 1.0),
                min_angle_deg=-30.0,
                max_angle_deg=30.0,
            ),
            JointEdge(
                id=f"j_femur_{leg_id}",
                joint_type=JointType.REVOLUTE,
                parent_part=coxa_id,
                child_part=femur_id,
                origin=femur_pos,
                axis=(pitch_ax, pitch_ay, 0.0),
                min_angle_deg=-90.0,
                max_angle_deg=90.0,
            ),
            JointEdge(
                id=f"j_tibia_{leg_id}",
                joint_type=JointType.REVOLUTE,
                parent_part=femur_id,
                child_part=tibia_id,
                origin=tibia_pos,
                axis=(pitch_ax, pitch_ay, 0.0),
                min_angle_deg=-135.0,
                max_angle_deg=135.0,
            ),
        ])

        # Manifest entries (body-local meshes, centered at body origin)
        manifest.extend([
            {
                "name": f"Body_Coxa_{leg_id}",
                "mesh_path": f"/m/Body_Coxa_{leg_id}.stl",
                "placement": {"position": list(coxa_pos), "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [52.0, 18.0, 10.0],
                "bbox_min_mm": [0.0, -9.0, -5.0],
            },
            {
                "name": f"Body_Femur_{leg_id}",
                "mesh_path": f"/m/Body_Femur_{leg_id}.stl",
                "placement": {"position": list(femur_pos), "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [66.0, 20.0, 10.0],
                "bbox_min_mm": [0.0, -10.0, -5.0],
            },
            {
                "name": f"Body_Tibia_{leg_id}",
                "mesh_path": f"/m/Body_Tibia_{leg_id}.stl",
                "placement": {"position": list(tibia_pos), "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [133.0, 15.0, 8.0],
                "bbox_min_mm": [0.0, -7.5, -4.0],
            },
        ])

        # Mesh bboxes (body-local, mm)
        mesh_bboxes[coxa_id] = MeshBBox(
            min_pt=(0.0, -9.0, -5.0), max_pt=(52.0, 9.0, 5.0),
        )
        mesh_bboxes[femur_id] = MeshBBox(
            min_pt=(0.0, -10.0, -5.0), max_pt=(66.0, 10.0, 5.0),
        )
        mesh_bboxes[tibia_id] = MeshBBox(
            min_pt=(0.0, -7.5, -4.0), max_pt=(133.0, 7.5, 4.0),
        )

    mechanism = Mechanism(
        name="hexapod_18dof",
        parts=tuple(parts),
        joints=tuple(joints),
        drives=(),
    )
    return mechanism, manifest, mesh_bboxes


class TestHexapodRoundTrip(unittest.TestCase):
    """Full round-trip: hexapod mechanism -> build_sim_model -> write_urdf -> validate."""

    def test_hexapod_round_trip(self) -> None:
        mechanism, manifest, mesh_bboxes = _make_hexapod_mechanism()

        # Build sim model with ground clearance
        model = build_sim_model(mechanism, manifest, ground_clearance_m=0.16)

        # Check link/joint counts: 1 chassis + 6*(coxa+femur+tibia) = 19 parts + base_link = 20 links
        self.assertEqual(len(model.links), 20)
        # 1 fixed (base_to_chassis) + 18 revolute = 19 joints
        self.assertEqual(len(model.joints), 19)

        # Write URDF
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        # Parse for manual checks
        tree = ET.parse(path)
        root = tree.getroot()
        urdf_joints = {j.attrib["name"]: j for j in root.findall("joint")}

        # Check coxa yaws match atan2(dy, dx)
        for leg_id, cx, cy in [
            ("L1", 70.0, 75.0), ("L2", 0.0, 85.0), ("L3", -70.0, 75.0),
            ("R1", 70.0, -75.0), ("R2", 0.0, -85.0), ("R3", -70.0, -75.0),
        ]:
            jname = f"j_coxa_{leg_id}"
            expected_yaw = math.atan2(cy, cx)
            rpy_str = urdf_joints[jname].find("origin").attrib["rpy"]
            actual_yaw = float(rpy_str.split()[2])
            self.assertAlmostEqual(actual_yaw, expected_yaw, places=3,
                                   msg=f"{jname}: yaw {actual_yaw} != {expected_yaw}")

        # Check femur/tibia RPY = (0, 0, 0) and axis ≈ (0, ±1, 0)
        for jname, jel in urdf_joints.items():
            if "femur" in jname or "tibia" in jname:
                rpy_str = jel.find("origin").attrib["rpy"]
                rpy_vals = [float(v) for v in rpy_str.split()]
                for v in rpy_vals:
                    self.assertAlmostEqual(v, 0.0, places=3, msg=f"{jname} rpy not zero")

                axis_str = jel.find("axis").attrib["xyz"]
                axis_vals = [float(v) for v in axis_str.split()]
                self.assertAlmostEqual(axis_vals[0], 0.0, places=3, msg=f"{jname} axis[0]")
                self.assertAlmostEqual(abs(axis_vals[1]), 1.0, places=3, msg=f"{jname} axis[1]")
                self.assertAlmostEqual(axis_vals[2], 0.0, places=3, msg=f"{jname} axis[2]")

        # Structural validation: no blockers
        struct_findings = validate_urdf(path)
        struct_blockers = [f for f in struct_findings if f.severity == Severity.BLOCK]
        self.assertEqual(struct_blockers, [], f"Structural blockers: {struct_blockers}")

        # FK validation: no blockers
        fk_findings = validate_urdf_fk(
            path,
            mesh_bboxes=mesh_bboxes,
            ground_clearance_m=0.16,
        )
        fk_blockers = [f for f in fk_findings if f.severity == Severity.BLOCK]
        self.assertEqual(fk_blockers, [], f"FK blockers: {fk_blockers}")

        # FK chassis height check should pass (within tolerance)
        chassis_findings = [f for f in fk_findings if f.rule_id == "urdf.fk.chassis_height"]
        self.assertEqual(len(chassis_findings), 0)

        # All coxa yaw checks should pass
        coxa_findings = [f for f in fk_findings if f.rule_id == "urdf.fk.coxa_yaw_matches_radial"]
        self.assertEqual(len(coxa_findings), 0)

        # All pitch axis checks should pass
        axis_findings = [f for f in fk_findings if f.rule_id == "urdf.fk.pitch_axis_local"]
        self.assertEqual(len(axis_findings), 0)


class TestURDFGenerationPipeline(unittest.TestCase):
    """End-to-end pipeline: build_sim_model -> write_urdf -> validate_urdf -> validate_urdf_fk.

    Uses the simple_2body fixture to test the full chain without FreeCAD or Isaac.
    Catches both URDF yaw formula and world-coord mesh bugs.
    """

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "simple_2body"

    def setUp(self) -> None:
        """Build a mechanism matching the fixture URDF."""
        self.mechanism = Mechanism(
            name="simple_2body",
            parts=(
                PartNode(
                    id="chassis",
                    body_name="Chassis",
                    is_ground=True,
                    mass_kg=1.0,
                ),
                PartNode(
                    id="arm",
                    body_name="Arm",
                    mass_kg=0.5,
                ),
            ),
            joints=(
                JointEdge(
                    id="shoulder",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="arm",
                    axis=(0.0, 0.0, 1.0),
                    origin=(0.0, 0.0, 50.0),
                    min_angle_deg=-90.0,
                    max_angle_deg=90.0,
                ),
            ),
            drives=(),
        )
        chassis_stl = str(self.FIXTURE_DIR / "Chassis.stl")
        arm_stl = str(self.FIXTURE_DIR / "Arm.stl")
        self.manifest = [
            {
                "name": "Chassis",
                "mesh_path": chassis_stl,
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [60.0, 40.0, 10.0],
                "bbox_min_mm": [-30.0, -20.0, -5.0],
            },
            {
                "name": "Arm",
                "mesh_path": arm_stl,
                "placement": {"position": [0, 0, 50], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [80.0, 20.0, 10.0],
                "bbox_min_mm": [-40.0, -10.0, -5.0],
            },
        ]
        self.mesh_bboxes = {
            "chassis": MeshBBox(
                min_pt=(-30.0, -20.0, -5.0),
                max_pt=(30.0, 20.0, 5.0),
            ),
            "arm": MeshBBox(
                min_pt=(-40.0, -10.0, -5.0),
                max_pt=(40.0, 10.0, 5.0),
            ),
        }

    def test_fixture_files_exist(self) -> None:
        """Fixture STLs and URDF exist on disk."""
        self.assertTrue((self.FIXTURE_DIR / "Chassis.stl").exists())
        self.assertTrue((self.FIXTURE_DIR / "Arm.stl").exists())
        self.assertTrue((self.FIXTURE_DIR / "simple_2body.urdf").exists())

    def test_fixture_stl_sizes(self) -> None:
        """Binary STLs are the expected size (80-byte header + 4-byte count + 12×50 bytes)."""
        expected_size = 80 + 4 + 12 * 50  # 684 bytes
        self.assertEqual((self.FIXTURE_DIR / "Chassis.stl").stat().st_size, expected_size)
        self.assertEqual((self.FIXTURE_DIR / "Arm.stl").stat().st_size, expected_size)

    def test_build_sim_model(self) -> None:
        """build_sim_model produces correct link/joint counts and properties."""
        model = build_sim_model(self.mechanism, self.manifest)

        self.assertEqual(model.name, "simple_2body")
        self.assertEqual(len(model.links), 2)
        self.assertEqual(len(model.joints), 1)

        # Root link
        chassis = model.links[0]
        self.assertEqual(chassis.name, "chassis")
        self.assertTrue(chassis.is_root)
        self.assertEqual(chassis.mass_kg, 1.0)
        self.assertIsNotNone(chassis.mesh_path)

        # Arm link
        arm = model.links[1]
        self.assertEqual(arm.name, "arm")
        self.assertFalse(arm.is_root)
        self.assertEqual(arm.mass_kg, 0.5)

        # Joint
        joint = model.joints[0]
        self.assertEqual(joint.name, "shoulder")
        self.assertEqual(joint.joint_type, "revolute")
        self.assertAlmostEqual(joint.origin_xyz[2], 0.05, places=6)  # 50mm -> 0.05m
        self.assertAlmostEqual(joint.limits[0], math.radians(-90), places=6)
        self.assertAlmostEqual(joint.limits[1], math.radians(90), places=6)

    def test_write_urdf_roundtrip(self) -> None:
        """write_urdf produces valid XML that can be parsed back."""
        model = build_sim_model(self.mechanism, self.manifest)

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        tree = ET.parse(path)
        root = tree.getroot()
        self.assertEqual(root.tag, "robot")
        self.assertEqual(root.attrib["name"], "simple_2body")
        self.assertEqual(len(root.findall("link")), 2)
        self.assertEqual(len(root.findall("joint")), 1)

        # Mesh paths reference the fixture STLs
        meshes = root.findall(".//mesh")
        for mesh in meshes:
            self.assertIn("stl", mesh.attrib["filename"].lower())
            self.assertEqual(mesh.attrib["scale"], "0.001 0.001 0.001")

    def test_validate_urdf_no_blockers(self) -> None:
        """Structural validation passes with no blockers."""
        model = build_sim_model(self.mechanism, self.manifest)
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        findings = validate_urdf(path)
        blockers = [f for f in findings if f.severity == Severity.BLOCK]
        self.assertEqual(blockers, [], f"Structural blockers: {blockers}")

    def test_validate_urdf_fk_no_blockers(self) -> None:
        """FK validation passes with no blockers.

        Note: mesh_bboxes omitted because link_chain_gap check assumes serial
        chains extending along +X, which doesn't apply to this vertical joint.
        """
        model = build_sim_model(self.mechanism, self.manifest)
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        findings = validate_urdf_fk(path)
        blockers = [f for f in findings if f.severity == Severity.BLOCK]
        self.assertEqual(blockers, [], f"FK blockers: {blockers}")

    def test_joint_origin_is_parent_relative(self) -> None:
        """Bug 1 regression: joint origin must be parent-relative, not absolute.

        The shoulder joint is at world position (0, 0, 50mm).  Since the parent
        (chassis) is at (0, 0, 0), the parent-relative offset is (0, 0, 0.05m).
        If the yaw formula were wrong (atan2(-dx, dy) instead of atan2(dy, dx)),
        the origin could be incorrect.
        """
        model = build_sim_model(self.mechanism, self.manifest)
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        tree = ET.parse(path)
        joint = tree.getroot().findall("joint")[0]
        origin_str = joint.find("origin").attrib["xyz"]
        origin_vals = [float(v) for v in origin_str.split()]

        # Z offset should be 0.05m (50mm)
        self.assertAlmostEqual(origin_vals[0], 0.0, places=4)
        self.assertAlmostEqual(origin_vals[1], 0.0, places=4)
        self.assertAlmostEqual(origin_vals[2], 0.05, places=4)

    def test_joint_rpy_zero_for_z_axis_joint(self) -> None:
        """Joint at (0,0,50) from ground: no outward yaw needed (dx=dy=0)."""
        model = build_sim_model(self.mechanism, self.manifest)
        joint = model.joints[0]
        self.assertAlmostEqual(joint.origin_rpy[0], 0.0, places=6)
        self.assertAlmostEqual(joint.origin_rpy[1], 0.0, places=6)
        self.assertAlmostEqual(joint.origin_rpy[2], 0.0, places=6)

    def test_outward_yaw_formula_correctness(self) -> None:
        """Bug 1 regression: atan2(dy, dx) produces correct yaw for offset joints.

        If the arm is at (100, 0, 0) from chassis, yaw = atan2(0, 100) = 0.
        If at (0, 100, 0), yaw = atan2(100, 0) = π/2.
        If at (70, 75, 0), yaw = atan2(75, 70) ≈ 0.821.
        """
        test_cases = [
            ((100.0, 0.0, 0.0), 0.0),
            ((0.0, 100.0, 0.0), math.pi / 2),
            ((70.0, 75.0, 0.0), math.atan2(75, 70)),
            ((-70.0, 75.0, 0.0), math.atan2(75, -70)),
        ]
        for origin, expected_yaw in test_cases:
            mech = Mechanism(
                name="yaw_test",
                parts=(
                    PartNode(id="base", is_ground=True),
                    PartNode(id="child"),
                ),
                joints=(
                    JointEdge(
                        id="j1",
                        joint_type=JointType.REVOLUTE,
                        parent_part="base",
                        child_part="child",
                        origin=origin,
                    ),
                ),
                drives=(),
            )
            model = build_sim_model(mech, [])
            actual_yaw = model.joints[0].origin_rpy[2]
            self.assertAlmostEqual(
                actual_yaw, expected_yaw, places=5,
                msg=f"origin={origin}: yaw {actual_yaw} != {expected_yaw}",
            )

    def test_mesh_paths_are_body_local_stls(self) -> None:
        """Bug 2 regression: mesh paths point to body-local STLs, not world-coord meshes."""
        model = build_sim_model(self.mechanism, self.manifest)

        for link in model.links:
            if link.mesh_path is not None:
                # Mesh file should exist on disk
                self.assertTrue(
                    Path(link.mesh_path).exists(),
                    f"Mesh not found: {link.mesh_path}",
                )

    def test_full_pipeline_with_fixture_urdf(self) -> None:
        """Validate the hand-written fixture URDF passes all checks."""
        fixture_urdf = str(self.FIXTURE_DIR / "simple_2body.urdf")

        # Structural validation
        struct_findings = validate_urdf(fixture_urdf)
        struct_blockers = [f for f in struct_findings if f.severity == Severity.BLOCK]
        self.assertEqual(struct_blockers, [], f"Fixture URDF structural blockers: {struct_blockers}")

        # FK validation (no mesh_bboxes — vertical joint, not a serial chain)
        fk_findings = validate_urdf_fk(fixture_urdf)
        fk_blockers = [f for f in fk_findings if f.severity == Severity.BLOCK]
        self.assertEqual(fk_blockers, [], f"Fixture URDF FK blockers: {fk_blockers}")

    def test_generated_urdf_matches_fixture(self) -> None:
        """Generated URDF has same structure as hand-written fixture."""
        model = build_sim_model(self.mechanism, self.manifest)
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            generated_path = f.name
        write_urdf(model, generated_path)

        fixture_urdf = str(self.FIXTURE_DIR / "simple_2body.urdf")

        gen_tree = ET.parse(generated_path)
        fix_tree = ET.parse(fixture_urdf)

        gen_links = {lk.attrib["name"] for lk in gen_tree.getroot().findall("link")}
        fix_links = {lk.attrib["name"] for lk in fix_tree.getroot().findall("link")}
        self.assertEqual(gen_links, fix_links)

        gen_joints = {j.attrib["name"] for j in gen_tree.getroot().findall("joint")}
        fix_joints = {j.attrib["name"] for j in fix_tree.getroot().findall("joint")}
        self.assertEqual(gen_joints, fix_joints)


if __name__ == "__main__":
    unittest.main()
