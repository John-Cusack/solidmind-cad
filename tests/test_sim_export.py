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
    _is_binary_stl,
    _make_transform_4x4,
    _multiply_4x4,
    _quat_from_yaw,
    _quat_inverse,
    _quat_multiply,
    _quat_rotate_point,
    _quat_to_rpy,
    _rpy_to_matrix,
    _transform_bbox,
    _transform_point,
    _transform_stl_to_link_local,
    build_sim_model,
    extract_leg_geometry,
    validate_sdf,
    validate_urdf,
    validate_urdf_fk,
    write_sdf,
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
        """No drive, no JointEdge actuator fields -> sensible servo defaults."""
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
        self.assertAlmostEqual(joint.effort, 10.0)
        self.assertAlmostEqual(joint.velocity, 6.28)
        self.assertAlmostEqual(joint.damping, 0.1)
        self.assertAlmostEqual(joint.friction, 0.0)

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


    def test_non_root_joint_rpy_composes_manifest_pitch(self) -> None:
        """Manifest pitch/roll on deeper links is composed into joint RPY.

        For non-planar robots (robot arms, tilted brackets), the manifest
        quaternion's pitch/roll must appear in the relative joint RPY so
        the child frame is correctly oriented.
        """
        # 3-part chain: chassis (ground) -> coxa -> femur
        # Femur has a rotated quaternion (30deg pitch) in manifest.
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
        # RPY must reflect the 30deg manifest pitch
        self.assertAlmostEqual(femur_joint.origin_rpy[0], 0.0, places=5)
        self.assertAlmostEqual(femur_joint.origin_rpy[1], math.radians(30), places=5)
        self.assertAlmostEqual(femur_joint.origin_rpy[2], 0.0, places=5)

    def test_joint_axis_rotated_to_local_frame(self) -> None:
        """Joint axis is specified in child's local frame and passes through unchanged."""
        # Axis (1, 0, 0) in local frame stays (1, 0, 0) in URDF.
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
        # Local-frame axis passes through unchanged
        self.assertAlmostEqual(femur_joint.axis[0], 1.0, places=5)
        self.assertAlmostEqual(femur_joint.axis[1], 0.0, places=5)
        self.assertAlmostEqual(femur_joint.axis[2], 0.0, places=5)

    def test_root_joint_non_z_axis_passes_through(self) -> None:
        """Root-attached joint with non-Z axis: local-frame axis passes through."""
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
        self.assertAlmostEqual(joint.axis[0], 1.0, places=5)
        self.assertAlmostEqual(joint.axis[1], 0.0, places=5)
        self.assertAlmostEqual(joint.axis[2], 0.0, places=5)

    def test_hexapod_leg_urdf_axes_and_rpy(self) -> None:
        """Integration: hexapod leg exports correct URDF axes and RPY."""
        # Chassis + one leg: coxa at (70, 75, 0) -> yaw = atan2(75, 70) ≈ 0.8211
        # Femur/tibia have local-frame axis (0, 1, 0) for pitch.
        import math as m
        dx, dy = 70.0, 75.0
        expected_yaw = m.atan2(dy, dx)  # ≈ 0.8211 rad
        r = m.sqrt(dx * dx + dy * dy)

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
                    axis=(0.0, 1.0, 0.0),  # local Y pitch
                ),
                JointEdge(
                    id="j_tibia",
                    joint_type=JointType.REVOLUTE,
                    parent_part="femur",
                    child_part="tibia",
                    origin=(dx + dx / r * 100, dy + dy / r * 100, 0.0),
                    axis=(0.0, 1.0, 0.0),  # local Y pitch
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

        # Femur axis: local-frame (0, 1, 0) passes through unchanged.
        femur_axis = joints["j_femur"].find("axis").attrib["xyz"]
        femur_axis_vals = [float(v) for v in femur_axis.split()]
        self.assertAlmostEqual(femur_axis_vals[0], 0.0, places=3)
        self.assertAlmostEqual(femur_axis_vals[1], 1.0, places=3)
        self.assertAlmostEqual(femur_axis_vals[2], 0.0, places=3)

        # Tibia axis: same — local-frame (0, 1, 0) passes through
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

    def test_ground_clearance_corrects_root_child_z(self) -> None:
        """Ground clearance preserves relative Z deltas — correction belongs in base_to_frame only."""
        # Chassis at origin, two children at different Z heights (like a hexapod).
        mechanism = Mechanism(
            name="hex_mini",
            parts=(
                PartNode(id="chassis", body_name="Body_Chassis", is_ground=True, mass_kg=1.0),
                PartNode(id="coxa_l1", body_name="Body_Coxa_L1", mass_kg=0.1),
                PartNode(id="femur_l1", body_name="Body_Femur_L1", mass_kg=0.1),
            ),
            joints=(
                JointEdge(
                    id="hip_yaw_L1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="coxa_l1",
                    origin=(52.0, 30.0, 100.0),  # world Z = 100mm
                ),
                JointEdge(
                    id="hip_pitch_L1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="coxa_l1",
                    child_part="femur_l1",
                    origin=(72.0, 30.0, 100.0),  # same Z, 20mm further out
                ),
            ),
            drives=(),
        )
        manifest = [
            {
                "name": "Body_Chassis",
                "mesh_path": "/m/chassis.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [120, 80, 3],
                "bbox_min_mm": [0, 0, 0],
            },
            {
                "name": "Body_Coxa_L1",
                "mesh_path": "/m/coxa.stl",
                "placement": {"position": [52, 30, 100], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [20, 10, 10],
                "bbox_min_mm": [0, 0, 0],
            },
            {
                "name": "Body_Femur_L1",
                "mesh_path": "/m/femur.stl",
                "placement": {"position": [72, 30, 100], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [40, 10, 10],
                "bbox_min_mm": [0, 0, 0],
            },
        ]

        gc = 0.1  # 100mm = 0.1m
        model = build_sim_model(mechanism, manifest, ground_clearance_m=gc)

        # base_to_chassis should have z = ground_clearance_m
        base_jt = next(j for j in model.joints if j.name == "base_to_chassis")
        self.assertAlmostEqual(base_jt.origin_xyz[2], gc, places=6)

        # hip_yaw_L1 (chassis -> coxa) should have z = 0.1 (the real relative delta)
        # Joint origins are pure relative deltas; no z-correction applied.
        hip_jt = next(j for j in model.joints if j.name == "hip_yaw_L1")
        self.assertAlmostEqual(hip_jt.origin_xyz[2], 0.1, places=4,
                               msg="Root child joint Z should be the real relative delta")

        # hip_pitch_L1 (coxa -> femur) is a deeper joint: delta Z = 0
        # (both coxa and femur are at z=100mm, so dz=0).  Should be unaffected.
        pitch_jt = next(j for j in model.joints if j.name == "hip_pitch_L1")
        self.assertAlmostEqual(pitch_jt.origin_xyz[2], 0.0, places=4,
                               msg="Deeper joint Z delta should remain ~0")

        # FK check: base_link(0,0,0) + base_to_chassis(0,0,0.1) + hip_yaw(~0.052,~0.03,0.1)
        # => coxa world pos = (0.052, 0.03, 0.2) = gc + 100mm coxa offset
        coxa_fk_z = base_jt.origin_xyz[2] + hip_jt.origin_xyz[2]
        self.assertAlmostEqual(coxa_fk_z, gc + 0.1, places=4,
                               msg="FK chain should place coxa at ground_clearance + relative delta")

    def test_ground_clearance_no_overcorrect_when_root_placed(self) -> None:
        """When the root body is already at z=ground_clearance, no Z correction needed."""
        # Same mechanism but chassis manifest is already at z=100mm.
        mechanism = Mechanism(
            name="hex_placed",
            parts=(
                PartNode(id="chassis", body_name="Body_Chassis", is_ground=True, mass_kg=1.0),
                PartNode(id="coxa_l1", body_name="Body_Coxa_L1", mass_kg=0.1),
            ),
            joints=(
                JointEdge(
                    id="hip_yaw_L1",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part="coxa_l1",
                    origin=(52.0, 30.0, 100.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {
                "name": "Body_Chassis",
                "mesh_path": "/m/chassis.stl",
                "placement": {"position": [0, 0, 100], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [120, 80, 3],
                "bbox_min_mm": [0, 0, 0],
            },
            {
                "name": "Body_Coxa_L1",
                "mesh_path": "/m/coxa.stl",
                "placement": {"position": [52, 30, 100], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [20, 10, 10],
                "bbox_min_mm": [0, 0, 0],
            },
        ]

        gc = 0.1  # 100mm = 0.1m — same as root's manifest Z
        model = build_sim_model(mechanism, manifest, ground_clearance_m=gc)

        # base_to_chassis has z = gc
        base_jt = next(j for j in model.joints if j.name == "base_to_chassis")
        self.assertAlmostEqual(base_jt.origin_xyz[2], gc, places=6)

        # hip_yaw: chassis→coxa delta is (52,30,0) since both at z=100.
        # No correction needed because root manifest Z matches ground_clearance.
        hip_jt = next(j for j in model.joints if j.name == "hip_yaw_L1")
        self.assertAlmostEqual(hip_jt.origin_xyz[2], 0.0, places=4,
                               msg="No overcorrection when root already at height")

        # FK chain: base_link(0) + base_to(0.1) + hip_yaw(0) = 0.1m = 100mm correct
        coxa_fk_z = base_jt.origin_xyz[2] + hip_jt.origin_xyz[2]
        self.assertAlmostEqual(coxa_fk_z, gc, places=4)


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
        """build_sim_model adds default ±90° limits for revolute joints without explicit limits."""
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
        self.assertAlmostEqual(joint.limits[0], -math.radians(90), places=6)
        self.assertAlmostEqual(joint.limits[1], math.radians(90), places=6)

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


class TestJointEdgeActuatorFields(unittest.TestCase):
    """Tests for JointEdge actuator fields flowing through to SimJoint."""

    def test_actuator_fields_passthrough(self) -> None:
        """JointEdge damping/friction/effort_nm/velocity_rad_s flow to SimJoint."""
        mech = Mechanism(
            name="actuated",
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
                    damping=0.5,
                    friction=0.02,
                    effort_nm=3.0,
                    velocity_rad_s=12.0,
                ),
            ),
            drives=(),
        )

        model = build_sim_model(mech, [])
        joint = model.joints[0]
        self.assertAlmostEqual(joint.effort, 3.0)
        self.assertAlmostEqual(joint.velocity, 12.0)
        self.assertAlmostEqual(joint.damping, 0.5)
        self.assertAlmostEqual(joint.friction, 0.02)

    def test_joint_edge_overrides_drive(self) -> None:
        """JointEdge actuator fields take priority over DriveCondition."""
        mech = Mechanism(
            name="override",
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
                    effort_nm=2.0,
                    velocity_rad_s=5.0,
                ),
            ),
            drives=(
                DriveCondition(joint_id="j1", torque_nm=50.0, speed_rpm=120.0),
            ),
        )

        model = build_sim_model(mech, [])
        joint = model.joints[0]
        # JointEdge fields win over DriveCondition
        self.assertAlmostEqual(joint.effort, 2.0)
        self.assertAlmostEqual(joint.velocity, 5.0)

    def test_joint_edge_serialization(self) -> None:
        """JointEdge actuator fields survive to_dict/from_dict round-trip."""
        edge = JointEdge(
            id="j1",
            joint_type=JointType.REVOLUTE,
            parent_part="base",
            child_part="arm",
            damping=0.3,
            friction=0.01,
            effort_nm=5.0,
            velocity_rad_s=8.0,
        )
        d = edge.to_dict()
        restored = JointEdge.from_dict(d)
        self.assertAlmostEqual(restored.damping, 0.3)
        self.assertAlmostEqual(restored.friction, 0.01)
        self.assertAlmostEqual(restored.effort_nm, 5.0)
        self.assertAlmostEqual(restored.velocity_rad_s, 8.0)


class TestRelativeMeshPaths(unittest.TestCase):
    """Tests for relative mesh paths in URDF output."""

    def test_write_urdf_relative_paths(self) -> None:
        """write_urdf with base_dir produces relative mesh filenames."""
        model = SimModel(
            name="rel_test",
            links=(
                SimLink(name="base", is_root=True),
                SimLink(name="link1", mesh_path="/tmp/pkg/meshes/link1.stl"),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="fixed",
                    parent="base",
                    child="link1",
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path, base_dir="/tmp/pkg")
        tree = ET.parse(path)
        root = tree.getroot()

        link_el = root.findall("link")[1]
        mesh_el = link_el.find("visual/geometry/mesh")
        self.assertEqual(mesh_el.attrib["filename"], "meshes/link1.stl")

        c_mesh_el = link_el.find("collision/geometry/mesh")
        self.assertEqual(c_mesh_el.attrib["filename"], "meshes/link1.stl")

    def test_write_urdf_absolute_paths_without_base_dir(self) -> None:
        """write_urdf without base_dir keeps absolute mesh filenames."""
        model = SimModel(
            name="abs_test",
            links=(
                SimLink(name="base", is_root=True),
                SimLink(name="link1", mesh_path="/tmp/pkg/meshes/link1.stl"),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="fixed",
                    parent="base",
                    child="link1",
                ),
            ),
        )

        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name

        write_urdf(model, path)
        tree = ET.parse(path)
        root = tree.getroot()

        link_el = root.findall("link")[1]
        mesh_el = link_el.find("visual/geometry/mesh")
        self.assertEqual(mesh_el.attrib["filename"], "/tmp/pkg/meshes/link1.stl")


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
        coxa_findings = [f for f in findings if f.rule_id == "urdf.fk.root_joint_yaw_matches_radial"]
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
        coxa_findings = [f for f in findings if f.rule_id == "urdf.fk.root_joint_yaw_matches_radial"]
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
        axis_findings = [f for f in findings if f.rule_id == "urdf.fk.chain_joint_axis"]
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
        axis_findings = [f for f in findings if f.rule_id == "urdf.fk.chain_joint_axis"]
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
        ("L1", 52.0, 30.0),
        ("L2", 0.0, 60.0),
        ("L3", -52.0, 30.0),
        ("R1", 52.0, -30.0),
        ("R2", 0.0, -60.0),
        ("R3", -52.0, -30.0),
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
            "bbox_mm": [190.0, 190.0, 8.0],
            "bbox_min_mm": [-95.0, -95.0, -4.0],
        },
    ]
    mesh_bboxes: dict[str, MeshBBox] = {
        "chassis": MeshBBox(min_pt=(-95.0, -95.0, -4.0), max_pt=(95.0, 95.0, 4.0)),
    }

    for leg_id, cx, cy in leg_positions:
        r = math.sqrt(cx * cx + cy * cy)
        dx = cx / r if r > 0 else 1.0
        dy = cy / r if r > 0 else 0.0

        # Local-frame pitch axis: Y-axis in child link frame
        pitch_ax = 0.0
        pitch_ay = 1.0

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
            ("L1", 52.0, 30.0), ("L2", 0.0, 60.0), ("L3", -52.0, 30.0),
            ("R1", 52.0, -30.0), ("R2", 0.0, -60.0), ("R3", -52.0, -30.0),
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
        coxa_findings = [f for f in fk_findings if f.rule_id == "urdf.fk.root_joint_yaw_matches_radial"]
        self.assertEqual(len(coxa_findings), 0)

        # All pitch axis checks should pass
        axis_findings = [f for f in fk_findings if f.rule_id == "urdf.fk.chain_joint_axis"]
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

    def test_generated_urdf_numeric_match(self) -> None:
        """Golden-file regression: generated URDF numeric values match fixture within tolerance."""
        TOL = 1e-4

        model = build_sim_model(self.mechanism, self.manifest)
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            generated_path = f.name
        write_urdf(model, generated_path)

        fixture_urdf = str(self.FIXTURE_DIR / "simple_2body.urdf")

        gen_root = ET.parse(generated_path).getroot()
        fix_root = ET.parse(fixture_urdf).getroot()

        # Build lookup dicts keyed by joint/link name
        gen_joints = {j.attrib["name"]: j for j in gen_root.findall("joint")}
        fix_joints = {j.attrib["name"]: j for j in fix_root.findall("joint")}

        # Compare joint numeric values
        for jname, fix_j in fix_joints.items():
            self.assertIn(jname, gen_joints, f"Missing joint '{jname}' in generated URDF")
            gen_j = gen_joints[jname]

            # origin xyz
            fix_origin = fix_j.find("origin")
            gen_origin = gen_j.find("origin")
            if fix_origin is not None:
                self.assertIsNotNone(gen_origin, f"Joint '{jname}' missing <origin>")
                fix_xyz = [float(v) for v in fix_origin.attrib.get("xyz", "0 0 0").split()]
                gen_xyz = [float(v) for v in gen_origin.attrib.get("xyz", "0 0 0").split()]
                for i, (fv, gv) in enumerate(zip(fix_xyz, gen_xyz)):
                    self.assertAlmostEqual(
                        gv, fv, delta=TOL,
                        msg=f"Joint '{jname}' origin xyz[{i}]: {gv} != {fv}",
                    )

                # origin rpy
                fix_rpy = [float(v) for v in fix_origin.attrib.get("rpy", "0 0 0").split()]
                gen_rpy = [float(v) for v in gen_origin.attrib.get("rpy", "0 0 0").split()]
                for i, (fv, gv) in enumerate(zip(fix_rpy, gen_rpy)):
                    self.assertAlmostEqual(
                        gv, fv, delta=TOL,
                        msg=f"Joint '{jname}' origin rpy[{i}]: {gv} != {fv}",
                    )

            # axis xyz
            fix_axis = fix_j.find("axis")
            gen_axis = gen_j.find("axis")
            if fix_axis is not None:
                self.assertIsNotNone(gen_axis, f"Joint '{jname}' missing <axis>")
                fix_ax = [float(v) for v in fix_axis.attrib.get("xyz", "0 0 1").split()]
                gen_ax = [float(v) for v in gen_axis.attrib.get("xyz", "0 0 1").split()]
                for i, (fv, gv) in enumerate(zip(fix_ax, gen_ax)):
                    self.assertAlmostEqual(
                        gv, fv, delta=TOL,
                        msg=f"Joint '{jname}' axis xyz[{i}]: {gv} != {fv}",
                    )

        # Compare link inertial mass values
        gen_links_map = {lk.attrib["name"]: lk for lk in gen_root.findall("link")}
        fix_links_map = {lk.attrib["name"]: lk for lk in fix_root.findall("link")}

        for lname, fix_lk in fix_links_map.items():
            fix_mass_el = fix_lk.find("inertial/mass")
            if fix_mass_el is not None:
                self.assertIn(lname, gen_links_map, f"Missing link '{lname}' in generated URDF")
                gen_mass_el = gen_links_map[lname].find("inertial/mass")
                self.assertIsNotNone(
                    gen_mass_el, f"Link '{lname}' missing <inertial><mass> in generated URDF",
                )
                fix_mass = float(fix_mass_el.attrib["value"])
                gen_mass = float(gen_mass_el.attrib["value"])
                self.assertAlmostEqual(
                    gen_mass, fix_mass, delta=TOL,
                    msg=f"Link '{lname}' mass: {gen_mass} != {fix_mass}",
                )


class TestChildDetachedFromParent(unittest.TestCase):
    """Tests for Check 2b: urdf.fk.child_detached_from_parent."""

    def _write_urdf_xml(self, robot_el: ET.Element) -> str:
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        tree = ET.ElementTree(robot_el)
        tree.write(path, encoding="unicode", xml_declaration=True)
        return path

    def test_child_outside_parent_bbox_block(self) -> None:
        """BLOCK when child joint origin is outside parent AABB + margin."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="chassis")
        ET.SubElement(robot, "link", name="servo_A")
        ET.SubElement(robot, "link", name="servo_B")

        # Two children → multi-child parent
        j1 = ET.SubElement(robot, "joint", name="j_servo_A", type="fixed")
        ET.SubElement(j1, "parent", link="chassis")
        ET.SubElement(j1, "child", link="servo_A")
        ET.SubElement(j1, "origin", xyz="0.2 0 0", rpy="0 0 0")  # 200mm
        ET.SubElement(j1, "axis", xyz="0 0 1")

        j2 = ET.SubElement(robot, "joint", name="j_servo_B", type="fixed")
        ET.SubElement(j2, "parent", link="chassis")
        ET.SubElement(j2, "child", link="servo_B")
        ET.SubElement(j2, "origin", xyz="0 0 0", rpy="0 0 0")  # centered
        ET.SubElement(j2, "axis", xyz="0 0 1")

        path = self._write_urdf_xml(robot)
        # Chassis is 100×100×10mm centered at origin
        bboxes = {
            "chassis": MeshBBox(
                min_pt=(-50.0, -50.0, -5.0), max_pt=(50.0, 50.0, 5.0)
            ),
        }
        findings = validate_urdf_fk(path, mesh_bboxes=bboxes)
        detach = [
            f for f in findings
            if f.rule_id == "urdf.fk.child_detached_from_parent"
        ]
        self.assertEqual(len(detach), 1)
        self.assertEqual(detach[0].severity, Severity.BLOCK)
        self.assertIn("servo_A", detach[0].message)

    def test_child_beyond_inscribed_radius_warn(self) -> None:
        """WARN when child inside AABB but beyond inscribed circle radius."""
        robot = ET.Element("robot", name="test")
        ET.SubElement(robot, "link", name="plate")
        ET.SubElement(robot, "link", name="servo_LF")
        ET.SubElement(robot, "link", name="servo_LM")

        # servo_LF at (60, 65)mm → dist ~88.5mm from center
        j1 = ET.SubElement(robot, "joint", name="j_servo_LF", type="fixed")
        ET.SubElement(j1, "parent", link="plate")
        ET.SubElement(j1, "child", link="servo_LF")
        ET.SubElement(j1, "origin", xyz="0.060 0.065 0", rpy="0 0 0")
        ET.SubElement(j1, "axis", xyz="0 0 1")

        # servo_LM at (0, 65)mm → dist 65mm (within inscribed)
        j2 = ET.SubElement(robot, "joint", name="j_servo_LM", type="fixed")
        ET.SubElement(j2, "parent", link="plate")
        ET.SubElement(j2, "child", link="servo_LM")
        ET.SubElement(j2, "origin", xyz="0 0.065 0", rpy="0 0 0")
        ET.SubElement(j2, "axis", xyz="0 0 1")

        path = self._write_urdf_xml(robot)
        # Plate 150×150mm AABB → inscribed radius = 75mm
        bboxes = {
            "plate": MeshBBox(
                min_pt=(-75.0, -75.0, -5.0), max_pt=(75.0, 75.0, 5.0)
            ),
        }
        findings = validate_urdf_fk(path, mesh_bboxes=bboxes)
        detach = [
            f for f in findings
            if f.rule_id == "urdf.fk.child_detached_from_parent"
        ]
        # Only servo_LF should trigger (WARN), servo_LM is within inscribed
        self.assertEqual(len(detach), 1)
        self.assertEqual(detach[0].severity, Severity.WARN)
        self.assertIn("servo_LF", detach[0].message)


class TestContinuousJoint(unittest.TestCase):
    """Tests for CONTINUOUS joint type support."""

    def _make_quadcopter_mechanism(self) -> Mechanism:
        return Mechanism(
            name="Quadcopter",
            parts=(
                PartNode(id="frame", body_name="Frame", is_ground=True, mass_kg=1.2),
                PartNode(id="motor_fr", body_name="Motor_FR", mass_kg=0.095),
                PartNode(id="prop_fr", body_name="Prop_FR", mass_kg=0.025),
            ),
            joints=(
                JointEdge(
                    id="mount_fr",
                    joint_type=JointType.FIXED,
                    parent_part="frame",
                    child_part="motor_fr",
                    origin=(250.0, 0.0, 6.0),
                ),
                JointEdge(
                    id="spin_fr",
                    joint_type=JointType.CONTINUOUS,
                    parent_part="motor_fr",
                    child_part="prop_fr",
                    axis=(0.0, 0.0, 1.0),
                    origin=(250.0, 0.0, 31.0),
                    damping=0.001,
                    effort_nm=0.5,
                    velocity_rad_s=837.0,
                ),
            ),
            drives=(),
        )

    def _make_manifest(self) -> list[dict]:
        return [
            {
                "name": "Frame",
                "mesh_path": "/tmp/Frame.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [500, 500, 6],
            },
            {
                "name": "Motor_FR",
                "mesh_path": "/tmp/Motor_FR.stl",
                "placement": {"position": [250, 0, 6], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [35, 35, 25],
            },
            {
                "name": "Prop_FR",
                "mesh_path": "/tmp/Prop_FR.stl",
                "placement": {"position": [250, 0, 31], "rotation_quat": [1, 0, 0, 0]},
                "bbox_mm": [304, 304, 3],
            },
        ]

    def test_continuous_joint_type_in_model(self) -> None:
        mech = self._make_quadcopter_mechanism()
        manifest = self._make_manifest()
        model = build_sim_model(mech, manifest)

        spin_joint = [j for j in model.joints if j.name == "spin_fr"][0]
        self.assertEqual(spin_joint.joint_type, "continuous")

    def test_continuous_joint_no_limits(self) -> None:
        mech = self._make_quadcopter_mechanism()
        manifest = self._make_manifest()
        model = build_sim_model(mech, manifest)

        spin_joint = [j for j in model.joints if j.name == "spin_fr"][0]
        self.assertIsNone(spin_joint.limits)

    def test_continuous_joint_urdf_output(self) -> None:
        mech = self._make_quadcopter_mechanism()
        manifest = self._make_manifest()
        model = build_sim_model(mech, manifest)

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".urdf", delete=False) as f:
            path = f.name
        write_urdf(model, path)

        tree = ET.parse(path)
        root = tree.getroot()

        spin_el = None
        for jel in root.findall("joint"):
            if jel.attrib.get("name") == "spin_fr":
                spin_el = jel
                break

        self.assertIsNotNone(spin_el)
        self.assertEqual(spin_el.attrib["type"], "continuous")
        # No <limit> element for continuous joints
        self.assertIsNone(spin_el.find("limit"))
        # Should have <dynamics>
        dyn = spin_el.find("dynamics")
        self.assertIsNotNone(dyn)
        self.assertEqual(dyn.attrib["damping"], "0.001")

        Path(path).unlink(missing_ok=True)


class TestCaseInsensitiveManifestMatch(unittest.TestCase):
    """Tests for case-insensitive manifest name matching."""

    def test_lowercase_part_id_matches_pascalcase_manifest(self) -> None:
        mech = Mechanism(
            name="test",
            parts=(
                PartNode(id="frame", is_ground=True, mass_kg=1.0),
                PartNode(id="arm", mass_kg=0.5),
            ),
            joints=(
                JointEdge(
                    id="j1",
                    joint_type=JointType.FIXED,
                    parent_part="frame",
                    child_part="arm",
                    origin=(100.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {
                "name": "Frame",
                "mesh_path": "/tmp/Frame.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
            },
            {
                "name": "Arm",
                "mesh_path": "/tmp/Arm.stl",
                "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]},
            },
        ]
        model = build_sim_model(mech, manifest)
        frame_link = [lk for lk in model.links if lk.name == "frame"][0]
        arm_link = [lk for lk in model.links if lk.name == "arm"][0]
        self.assertEqual(frame_link.mesh_path, "/tmp/Frame.stl")
        self.assertEqual(arm_link.mesh_path, "/tmp/Arm.stl")

    def test_body_name_case_mismatch(self) -> None:
        mech = Mechanism(
            name="test",
            parts=(
                PartNode(id="base", body_name="motor_fr", is_ground=True, mass_kg=1.0),
            ),
            joints=(),
            drives=(),
        )
        manifest = [
            {
                "name": "Motor_FR",
                "mesh_path": "/tmp/Motor_FR.stl",
                "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
            },
        ]
        model = build_sim_model(mech, manifest)
        self.assertEqual(model.links[0].mesh_path, "/tmp/Motor_FR.stl")


class TestQuatRotatePoint(unittest.TestCase):
    def test_identity_rotation(self) -> None:
        result = _quat_rotate_point(1.0, 0.0, 0.0, 0.0, 1.0, 2.0, 3.0)
        self.assertAlmostEqual(result[0], 1.0, places=10)
        self.assertAlmostEqual(result[1], 2.0, places=10)
        self.assertAlmostEqual(result[2], 3.0, places=10)

    def test_90_yaw_rotates_x_to_y(self) -> None:
        # 90° about Z: (1,0,0) -> (0,1,0)
        w = math.cos(math.radians(45))
        z = math.sin(math.radians(45))
        result = _quat_rotate_point(w, 0.0, 0.0, z, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(result[0], 0.0, places=5)
        self.assertAlmostEqual(result[1], 1.0, places=5)
        self.assertAlmostEqual(result[2], 0.0, places=5)

    def test_90_pitch_rotates_x_to_neg_z(self) -> None:
        # 90° about Y: (1,0,0) -> (0,0,-1)
        w = math.cos(math.radians(45))
        y = math.sin(math.radians(45))
        result = _quat_rotate_point(w, 0.0, y, 0.0, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(result[0], 0.0, places=5)
        self.assertAlmostEqual(result[1], 0.0, places=5)
        self.assertAlmostEqual(result[2], -1.0, places=5)

    def test_inverse_rotation_undoes(self) -> None:
        # Rotate then inverse should give back original point
        w = math.cos(math.radians(30))
        z = math.sin(math.radians(30))
        p = (3.0, 4.0, 5.0)
        rotated = _quat_rotate_point(w, 0.0, 0.0, z, *p)
        inv_w, inv_x, inv_y, inv_z = _quat_inverse(w, 0.0, 0.0, z)
        restored = _quat_rotate_point(inv_w, inv_x, inv_y, inv_z, *rotated)
        self.assertAlmostEqual(restored[0], p[0], places=10)
        self.assertAlmostEqual(restored[1], p[1], places=10)
        self.assertAlmostEqual(restored[2], p[2], places=10)


class TestQuatFromYaw(unittest.TestCase):
    def test_zero_is_identity(self) -> None:
        q = _quat_from_yaw(0.0)
        self.assertAlmostEqual(q[0], 1.0)
        self.assertAlmostEqual(q[1], 0.0)
        self.assertAlmostEqual(q[2], 0.0)
        self.assertAlmostEqual(q[3], 0.0)

    def test_90_deg(self) -> None:
        q = _quat_from_yaw(math.pi / 2)
        rpy = _quat_to_rpy(*q)
        self.assertAlmostEqual(rpy[0], 0.0, places=5)
        self.assertAlmostEqual(rpy[1], 0.0, places=5)
        self.assertAlmostEqual(rpy[2], math.pi / 2, places=5)


class TestAsciiStlTransform(unittest.TestCase):
    """Test ASCII STL transformation with full quaternion rotation."""

    _ASCII_STL = """\
solid test
  facet normal 0.000000 0.000000 1.000000
    outer loop
      vertex 100.000000 200.000000 0.000000
      vertex 110.000000 200.000000 0.000000
      vertex 105.000000 210.000000 0.000000
    endloop
  endfacet
endsolid test
"""

    def test_identity_no_op(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".stl", delete=False) as f:
            f.write(self._ASCII_STL)
            path = f.name
        _transform_stl_to_link_local(path, (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))
        with open(path) as f:
            content = f.read()
        # File should be unchanged (identity transform early return)
        self.assertEqual(content, self._ASCII_STL)

    def test_translate_only(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".stl", delete=False) as f:
            f.write(self._ASCII_STL)
            path = f.name
        _transform_stl_to_link_local(path, (100.0, 200.0, 0.0), (1.0, 0.0, 0.0, 0.0))
        with open(path) as f:
            content = f.read()
        # First vertex at (100, 200, 0) should become (0, 0, 0)
        self.assertIn("0.000000 0.000000 0.000000", content)

    def test_yaw_rotation(self) -> None:
        """45° yaw via quaternion matches expected rotation."""
        stl = """\
solid test
  facet normal 1.000000 0.000000 0.000000
    outer loop
      vertex 50.000000 0.000000 0.000000
      vertex 50.000000 0.000000 10.000000
      vertex 60.000000 0.000000 0.000000
    endloop
  endfacet
endsolid test
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".stl", delete=False) as f:
            f.write(stl)
            path = f.name
        yaw = math.pi / 4  # 45°
        quat = _quat_from_yaw(yaw)
        _transform_stl_to_link_local(path, (0.0, 0.0, 0.0), quat)
        with open(path) as f:
            lines = f.readlines()
        # Find first vertex line
        for line in lines:
            if "vertex" in line and "50" not in line:
                continue
            if line.strip().startswith("vertex"):
                vals = [float(v) for v in line.split()[1:]]
                # After -45° rotation: (50, 0, 0) -> (35.355, -35.355, 0)
                if abs(vals[2]) < 1:  # The Z=0 vertex
                    expected_x = 50 * math.cos(-yaw)
                    expected_y = 50 * math.sin(-yaw)
                    self.assertAlmostEqual(vals[0], expected_x, places=2)
                    self.assertAlmostEqual(vals[1], expected_y, places=2)
                    break


class TestBinaryStlTransform(unittest.TestCase):
    """Test binary STL transformation."""

    def _make_binary_stl(self, vertices: list[tuple[float, float, float]]) -> bytes:
        """Create a minimal binary STL with one triangle."""
        import struct
        header = b"\x00" * 80
        tri_count = len(vertices) // 3
        data = bytearray()
        for i in range(tri_count):
            # Normal
            data += struct.pack("<3f", 0.0, 0.0, 1.0)
            # 3 vertices
            for j in range(3):
                v = vertices[i * 3 + j]
                data += struct.pack("<3f", v[0], v[1], v[2])
            # Attribute byte count
            data += struct.pack("<H", 0)
        return header + struct.pack("<I", tri_count) + bytes(data)

    def test_detect_binary(self) -> None:
        data = self._make_binary_stl([
            (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
        ])
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(data)
            path = f.name
        self.assertTrue(_is_binary_stl(path))

    def test_detect_ascii(self) -> None:
        ascii_stl = "solid test\nendsolid test\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".stl", delete=False) as f:
            f.write(ascii_stl)
            path = f.name
        self.assertFalse(_is_binary_stl(path))

    def test_binary_translate(self) -> None:
        import struct as st
        data = self._make_binary_stl([
            (100.0, 200.0, 0.0), (110.0, 200.0, 0.0), (105.0, 210.0, 0.0),
        ])
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(data)
            path = f.name
        _transform_stl_to_link_local(path, (100.0, 200.0, 0.0), (1.0, 0.0, 0.0, 0.0))
        with open(path, "rb") as f:
            f.read(80)  # header
            f.read(4)   # count
            f.read(12)  # normal
            v1 = st.unpack("<3f", f.read(12))
        self.assertAlmostEqual(v1[0], 0.0, places=2)
        self.assertAlmostEqual(v1[1], 0.0, places=2)
        self.assertAlmostEqual(v1[2], 0.0, places=2)

    def test_binary_rotate(self) -> None:
        import struct as st
        data = self._make_binary_stl([
            (50.0, 0.0, 0.0), (50.0, 0.0, 10.0), (60.0, 0.0, 0.0),
        ])
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(data)
            path = f.name
        yaw = math.pi / 4
        quat = _quat_from_yaw(yaw)
        _transform_stl_to_link_local(path, (0.0, 0.0, 0.0), quat)
        with open(path, "rb") as f:
            f.read(80)  # header
            f.read(4)   # count
            f.read(12)  # normal
            v1 = st.unpack("<3f", f.read(12))
        expected_x = 50 * math.cos(-yaw)
        expected_y = 50 * math.sin(-yaw)
        self.assertAlmostEqual(v1[0], expected_x, places=2)
        self.assertAlmostEqual(v1[1], expected_y, places=2)


class TestMeshTransformErrorMode(unittest.TestCase):
    """Test mesh_transform_error_mode parameter."""

    def test_warn_mode_continues(self) -> None:
        mech = Mechanism(
            name="test",
            parts=(
                PartNode(id="base", is_ground=True),
                PartNode(id="arm"),
            ),
            joints=(
                JointEdge(
                    id="j1", joint_type=JointType.REVOLUTE,
                    parent_part="base", child_part="arm",
                    origin=(100.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "base", "mesh_path": None,
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "arm", "mesh_path": "/nonexistent/arm.stl",
             "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
        ]
        # warn mode (default) should not raise
        model = build_sim_model(mech, manifest, mesh_transform_error_mode="warn")
        self.assertEqual(len(model.links), 2)

    def test_fail_mode_raises(self) -> None:
        mech = Mechanism(
            name="test",
            parts=(
                PartNode(id="base", is_ground=True),
                PartNode(id="arm"),
            ),
            joints=(
                JointEdge(
                    id="j1", joint_type=JointType.REVOLUTE,
                    parent_part="base", child_part="arm",
                    origin=(100.0, 0.0, 0.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "base", "mesh_path": None,
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "arm", "mesh_path": "/nonexistent/arm.stl",
             "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
        ]
        with self.assertRaises(FileNotFoundError):
            build_sim_model(mech, manifest, mesh_transform_error_mode="fail")


class TestFullQuaternionBFS(unittest.TestCase):
    """Verify full-quaternion BFS produces correct results for various robot types."""

    def test_hexapod_pure_yaw_no_regression(self) -> None:
        """Hexapod with pure-yaw placements produces identical results to old yaw-only."""
        dx, dy = 70.71, 70.71
        expected_yaw = math.atan2(dy, dx)

        mech = Mechanism(
            name="hexapod",
            parts=(
                PartNode(id="chassis", is_ground=True),
                PartNode(id="coxa"),
                PartNode(id="femur"),
            ),
            joints=(
                JointEdge(
                    id="j_coxa", joint_type=JointType.REVOLUTE,
                    parent_part="chassis", child_part="coxa",
                    axis=(0.0, 0.0, 1.0),
                ),
                JointEdge(
                    id="j_femur", joint_type=JointType.REVOLUTE,
                    parent_part="coxa", child_part="femur",
                    origin=(dx + 50 * dx / 100, dy + 50 * dy / 100, 0.0),
                    axis=(0.0, 1.0, 0.0),
                ),
            ),
            drives=(),
        )
        manifest = [
            {"name": "chassis", "mesh_path": None,
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "coxa", "mesh_path": None,
             "placement": {"position": [dx, dy, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "femur", "mesh_path": None,
             "placement": {"position": [dx + 50 * dx / 100, dy + 50 * dy / 100, 0],
                           "rotation_quat": [1, 0, 0, 0]}},
        ]
        model = build_sim_model(mech, manifest)

        coxa_j = next(j for j in model.joints if j.name == "j_coxa")
        femur_j = next(j for j in model.joints if j.name == "j_femur")

        # Coxa: yaw = atan2(70.71, 70.71) ≈ 0.785
        self.assertAlmostEqual(coxa_j.origin_rpy[2], expected_yaw, places=3)
        self.assertAlmostEqual(coxa_j.origin_rpy[0], 0.0, places=6)
        self.assertAlmostEqual(coxa_j.origin_rpy[1], 0.0, places=6)

        # Femur: deeper joint, no added rotation → RPY = (0,0,0)
        for v in femur_j.origin_rpy:
            self.assertAlmostEqual(v, 0.0, places=6)

    def test_relative_quat_for_composed_yaws(self) -> None:
        """Two root-attached legs at different angles produce correct relative RPY."""
        mech = Mechanism(
            name="two_legs",
            parts=(
                PartNode(id="chassis", is_ground=True),
                PartNode(id="leg_a"),
                PartNode(id="leg_b"),
            ),
            joints=(
                JointEdge(
                    id="j_a", joint_type=JointType.REVOLUTE,
                    parent_part="chassis", child_part="leg_a",
                    origin=(100.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="j_b", joint_type=JointType.REVOLUTE,
                    parent_part="chassis", child_part="leg_b",
                    origin=(0.0, 100.0, 0.0),
                ),
            ),
            drives=(),
        )
        model = build_sim_model(mech, [])

        j_a = next(j for j in model.joints if j.name == "j_a")
        j_b = next(j for j in model.joints if j.name == "j_b")

        # leg_a at (100, 0) → yaw = 0
        self.assertAlmostEqual(j_a.origin_rpy[2], 0.0, places=3)
        # leg_b at (0, 100) → yaw = π/2
        self.assertAlmostEqual(j_b.origin_rpy[2], math.pi / 2, places=3)


class TestMechanismStructuralValidation(unittest.TestCase):
    """Test validate_mechanism_structure from motion_validators."""

    def test_valid_mechanism_no_errors(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="simple",
            parts=(
                PartNode(id="frame", is_ground=True),
                PartNode(id="wheel"),
            ),
            joints=(
                JointEdge(
                    id="j1", joint_type=JointType.REVOLUTE,
                    parent_part="frame", child_part="wheel",
                ),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])

    def test_duplicate_part_id(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="dup",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="a"),
            ),
            joints=(),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertTrue(any("Duplicate part ID" in e for e in errors))

    def test_duplicate_joint_id(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="dup",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
                PartNode(id="c"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b"),
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="c"),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("Duplicate joint ID" in e for e in errors))

    def test_dangling_parent_reference(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="dangle",
            parts=(
                PartNode(id="a", is_ground=True),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="nonexistent"),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("unknown child_part" in e for e in errors))

    def test_zero_axis_vector(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="zero_axis",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b",
                          axis=(0.0, 0.0, 0.0)),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("zero vector" in e for e in errors))

    def test_non_unit_axis_warning(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="non_unit",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b",
                          axis=(0.0, 0.0, 2.0)),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertTrue(any("not unit-length" in w for w in warnings))

    def test_limit_consistency(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="bad_limits",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b",
                          min_angle_deg=90, max_angle_deg=10),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("min_angle_deg" in e for e in errors))

    def test_cycle_detection(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="cycle",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
                PartNode(id="c"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b"),
                JointEdge(id="j2", joint_type=JointType.REVOLUTE,
                          parent_part="b", child_part="c"),
                JointEdge(id="j3", joint_type=JointType.REVOLUTE,
                          parent_part="c", child_part="a"),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("Cycle" in e for e in errors))

    def test_duplicate_joint_connection(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="dup_conn",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b"),
                JointEdge(id="j2", joint_type=JointType.FIXED,
                          parent_part="a", child_part="b"),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("Duplicate joint connection" in e for e in errors))

    def test_negative_mass(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="neg_mass",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b", mass_kg=-1.0),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b"),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("negative" in e for e in errors))

    def test_zero_mass_warning(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="zero_mass",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b", mass_kg=0.0),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b"),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertTrue(any("mass_kg=0" in w for w in warnings))

    def test_no_ground_is_warning(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="no_ground",
            parts=(
                PartNode(id="a"),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.REVOLUTE,
                          parent_part="a", child_part="b"),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertTrue(any("ground" in w.lower() for w in warnings))

    def test_dangling_drive_reference(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="bad_drive",
            parts=(
                PartNode(id="a", is_ground=True),
            ),
            joints=(),
            drives=(
                DriveCondition(joint_id="nonexistent", speed_rpm=100),
            ),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("unknown joint_id" in e for e in errors))

    def test_fixed_joint_non_default_axis_warning(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="fixed_axis",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.FIXED,
                          parent_part="a", child_part="b",
                          axis=(1.0, 0.0, 0.0)),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertTrue(any("fixed" in w.lower() for w in warnings))

    def test_prismatic_non_principal_axis_warning(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="prism_diag",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.PRISMATIC,
                          parent_part="a", child_part="b",
                          axis=(0.707, 0.707, 0.0)),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertTrue(any("prismatic axis not aligned" in w for w in warnings))

    def test_continuous_with_limits_warning(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="cont_limits",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.CONTINUOUS,
                          parent_part="a", child_part="b",
                          min_angle_deg=-90, max_angle_deg=90),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertTrue(any("continuous joint has angle limits" in w for w in warnings))

    def test_prismatic_principal_axis_ok(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="prism_ok",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.PRISMATIC,
                          parent_part="a", child_part="b",
                          axis=(0.0, 0.0, 1.0)),
            ),
            drives=(),
        )
        errors, warnings = validate_mechanism_structure(mech)
        self.assertEqual(errors, [])
        self.assertFalse(any("prismatic axis not aligned" in w for w in warnings))

    def test_negative_teeth_count(self) -> None:
        from server.motion_validators import validate_mechanism_structure
        mech = Mechanism(
            name="neg_teeth",
            parts=(
                PartNode(id="a", is_ground=True),
                PartNode(id="b"),
            ),
            joints=(
                JointEdge(id="j1", joint_type=JointType.GEAR_MESH,
                          parent_part="a", child_part="b",
                          teeth_parent=-10, teeth_child=20),
            ),
            drives=(),
        )
        errors, _ = validate_mechanism_structure(mech)
        self.assertTrue(any("teeth_parent" in e for e in errors))


class TestTopologyOptimization(unittest.TestCase):
    """Tests for co-located fixed + non-fixed sibling rechaining."""

    def _make_quadcopter_mechanism(self) -> Mechanism:
        """Quadcopter: frame + 4×(motor + prop).

        Motors connect to frame via fixed joints, props via revolute.
        Motor and prop at each arm share the same position.
        """
        parts = [
            PartNode(id="frame", is_ground=True),
        ]
        joints: list[JointEdge] = []
        positions = [
            (100.0, 100.0, 0.0),
            (100.0, -100.0, 0.0),
            (-100.0, -100.0, 0.0),
            (-100.0, 100.0, 0.0),
        ]
        for i, pos in enumerate(positions):
            motor_id = f"motor_{i}"
            prop_id = f"prop_{i}"
            parts.append(PartNode(id=motor_id))
            parts.append(PartNode(id=prop_id))
            # Fixed joint: frame → motor
            joints.append(JointEdge(
                id=f"frame_to_motor_{i}",
                joint_type=JointType.FIXED,
                parent_part="frame",
                child_part=motor_id,
                origin=list(pos),
            ))
            # Revolute joint: frame → prop (BEFORE optimization)
            joints.append(JointEdge(
                id=f"frame_to_prop_{i}",
                joint_type=JointType.REVOLUTE,
                parent_part="frame",
                child_part=prop_id,
                origin=list(pos),
                axis=(0.0, 0.0, 1.0),
            ))

        return Mechanism(
            name="quadcopter",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=(),
        )

    def _make_manifest(self, mechanism: Mechanism) -> list[dict[str, Any]]:
        """Minimal manifest with positions matching mechanism origins."""
        manifest = []
        # Map part positions from joint origins
        pos_map: dict[str, tuple[float, float, float]] = {"frame": (0.0, 0.0, 0.0)}
        for j in mechanism.joints:
            pos_map[j.child_part] = (j.origin[0], j.origin[1], j.origin[2])

        for part in mechanism.parts:
            pos = pos_map.get(part.id, (0.0, 0.0, 0.0))
            manifest.append({
                "name": part.id,
                "mesh_path": None,
                "placement": {
                    "position": list(pos),
                    "rotation_quat": [1.0, 0.0, 0.0, 0.0],
                },
            })
        return manifest

    def test_props_reparented_to_motors(self) -> None:
        """Propeller revolute joints should be rechained through motors."""
        mech = self._make_quadcopter_mechanism()
        manifest = self._make_manifest(mech)
        model = build_sim_model(mech, manifest)

        # Each prop's revolute joint should have motor as parent, not frame
        for i in range(4):
            prop_joint = next(
                j for j in model.joints if j.name == f"frame_to_prop_{i}"
            )
            self.assertEqual(
                prop_joint.parent, f"motor_{i}",
                f"prop_{i} revolute joint should be parented to motor_{i}, "
                f"not {prop_joint.parent}",
            )

    def test_fixed_joints_unchanged(self) -> None:
        """Fixed joints (frame → motor) should keep their original parent."""
        mech = self._make_quadcopter_mechanism()
        manifest = self._make_manifest(mech)
        model = build_sim_model(mech, manifest)

        for i in range(4):
            motor_joint = next(
                j for j in model.joints if j.name == f"frame_to_motor_{i}"
            )
            self.assertEqual(motor_joint.parent, "frame")

    def test_prop_joint_origin_near_zero(self) -> None:
        """After rechaining, prop joint origin should be ~zero (co-located)."""
        mech = self._make_quadcopter_mechanism()
        manifest = self._make_manifest(mech)
        model = build_sim_model(mech, manifest)

        for i in range(4):
            prop_joint = next(
                j for j in model.joints if j.name == f"frame_to_prop_{i}"
            )
            # Motor and prop are at the same position, so the origin
            # from motor to prop should be approximately zero
            for dim in range(3):
                self.assertAlmostEqual(
                    prop_joint.origin_xyz[dim], 0.0, places=4,
                    msg=f"prop_{i} joint origin dim {dim} should be ~0",
                )

    def test_no_rechaining_when_not_colocated(self) -> None:
        """Non-co-located siblings should NOT be rechained."""
        parts = [
            PartNode(id="frame", is_ground=True),
            PartNode(id="motor"),
            PartNode(id="sensor"),
        ]
        joints = [
            JointEdge(
                id="frame_to_motor",
                joint_type=JointType.FIXED,
                parent_part="frame",
                child_part="motor",
                origin=[100.0, 0.0, 0.0],
            ),
            JointEdge(
                id="frame_to_sensor",
                joint_type=JointType.REVOLUTE,
                parent_part="frame",
                child_part="sensor",
                origin=[-100.0, 0.0, 0.0],  # Far away from motor
                axis=(0.0, 0.0, 1.0),
            ),
        ]
        mech = Mechanism(
            name="spread",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=(),
        )
        manifest = [
            {"name": "frame", "mesh_path": None,
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "motor", "mesh_path": None,
             "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "sensor", "mesh_path": None,
             "placement": {"position": [-100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
        ]
        model = build_sim_model(mech, manifest)

        sensor_joint = next(j for j in model.joints if j.name == "frame_to_sensor")
        self.assertEqual(
            sensor_joint.parent, "frame",
            "Sensor should remain parented to frame (not co-located with motor)",
        )

    def test_no_rechaining_when_both_non_fixed(self) -> None:
        """Two non-fixed co-located siblings should NOT be rechained."""
        parts = [
            PartNode(id="frame", is_ground=True),
            PartNode(id="joint_a"),
            PartNode(id="joint_b"),
        ]
        joints = [
            JointEdge(
                id="frame_to_a",
                joint_type=JointType.REVOLUTE,
                parent_part="frame",
                child_part="joint_a",
                origin=[100.0, 0.0, 0.0],
                axis=(0.0, 0.0, 1.0),
            ),
            JointEdge(
                id="frame_to_b",
                joint_type=JointType.REVOLUTE,
                parent_part="frame",
                child_part="joint_b",
                origin=[100.0, 0.0, 0.0],
                axis=(0.0, 1.0, 0.0),
            ),
        ]
        mech = Mechanism(
            name="twin_revolute",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=(),
        )
        manifest = [
            {"name": "frame", "mesh_path": None,
             "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "joint_a", "mesh_path": None,
             "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
            {"name": "joint_b", "mesh_path": None,
             "placement": {"position": [100, 0, 0], "rotation_quat": [1, 0, 0, 0]}},
        ]
        model = build_sim_model(mech, manifest)

        joint_b = next(j for j in model.joints if j.name == "frame_to_b")
        self.assertEqual(
            joint_b.parent, "frame",
            "Two revolute co-located siblings should not be rechained",
        )


class TestSdfExport(unittest.TestCase):
    def _simple_model(self) -> SimModel:
        return SimModel(
            name="sdf_robot",
            links=(
                SimLink(
                    name="base",
                    mesh_path="/tmp/base.stl",
                    mass_kg=2.0,
                    inertia=(1.0, 0.0, 0.0, 1.0, 0.0, 1.0),
                    is_root=True,
                ),
                SimLink(
                    name="arm",
                    mesh_path="/tmp/arm.stl",
                    mass_kg=1.0,
                    inertia=(0.5, 0.0, 0.0, 0.5, 0.0, 0.5),
                ),
            ),
            joints=(
                SimJoint(
                    name="j1",
                    joint_type="revolute",
                    parent="base",
                    child="arm",
                    axis=(0.0, 0.0, 1.0),
                    origin_xyz=(0.0, 0.0, 0.1),
                    limits=(-1.0, 1.0),
                ),
            ),
        )

    def test_write_sdf_basic_structure(self) -> None:
        model = self._simple_model()
        with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as f:
            path = f.name

        sdf_path = write_sdf(model, path)
        self.assertTrue(sdf_path.endswith(".sdf"))

        tree = ET.parse(sdf_path)
        root = tree.getroot()
        self.assertEqual(root.tag, "sdf")
        model_el = root.find("model")
        self.assertIsNotNone(model_el)
        self.assertEqual(model_el.attrib.get("name"), "sdf_robot")

        links = model_el.findall("link")
        self.assertEqual(len(links), 2)
        mesh_scale = links[0].find("visual/geometry/mesh/scale")
        self.assertIsNotNone(mesh_scale)
        self.assertEqual(mesh_scale.text, "0.001 0.001 0.001")

    def test_validate_sdf_detects_dangling_child(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sdf", mode="w", delete=False) as f:
            f.write(
                """<?xml version='1.0'?>
<sdf version="1.10">
  <model name="bad">
    <link name="base"/>
    <joint name="bad_joint" type="revolute">
      <parent>base</parent>
      <child>missing</child>
    </joint>
  </model>
</sdf>
"""
            )
            path = f.name

        findings = validate_sdf(path)
        self.assertTrue(any(f.rule_id == "sdf.dangling_child" for f in findings))
        self.assertTrue(any(f.severity == Severity.BLOCK for f in findings))


class TestExtractLegGeometry(unittest.TestCase):
    """Tests for extract_leg_geometry — reads leg chains from a SimModel."""

    def _make_hexapod_sim_model(self) -> SimModel:
        """Build a minimal 6-leg, 3-DOF SimModel (in meters)."""
        links = [SimLink(name="chassis", is_root=True)]
        joints: list[SimJoint] = []

        legs = ["lf", "lm", "lr", "rf", "rm", "rr"]
        hip_positions = [
            (0.07, 0.075), (0.0, 0.075), (-0.07, 0.075),
            (0.07, -0.075), (0.0, -0.075), (-0.07, -0.075),
        ]

        for i, leg in enumerate(legs):
            hx, hy = hip_positions[i]
            links.append(SimLink(name=f"coxa_{leg}"))
            links.append(SimLink(name=f"femur_{leg}"))
            links.append(SimLink(name=f"tibia_{leg}"))

            joints.append(SimJoint(
                name=f"coxa_{leg}", joint_type="revolute",
                parent="chassis", child=f"coxa_{leg}",
                origin_xyz=(hx, hy, 0.0),
                limits=(-0.785, 0.785),
            ))
            joints.append(SimJoint(
                name=f"femur_{leg}", joint_type="revolute",
                parent=f"coxa_{leg}", child=f"femur_{leg}",
                origin_xyz=(hx + 0.052, hy, 0.0),
                axis=(0.0, 1.0, 0.0),
                limits=(-1.57, 1.57),
            ))
            joints.append(SimJoint(
                name=f"tibia_{leg}", joint_type="revolute",
                parent=f"femur_{leg}", child=f"tibia_{leg}",
                origin_xyz=(hx + 0.052 + 0.066, hy, 0.0),
                axis=(0.0, 1.0, 0.0),
                limits=(-2.09, 0.0),
            ))

        return SimModel(
            name="hexapod",
            links=tuple(links),
            joints=tuple(joints),
        )

    def test_finds_6_legs(self) -> None:
        model = self._make_hexapod_sim_model()
        result = extract_leg_geometry(model)
        self.assertEqual(result["n_legs"], 6)

    def test_dofs_per_leg(self) -> None:
        model = self._make_hexapod_sim_model()
        result = extract_leg_geometry(model)
        self.assertEqual(result["dofs_per_leg"], 3)

    def test_segment_lengths(self) -> None:
        model = self._make_hexapod_sim_model()
        result = extract_leg_geometry(model)
        leg0 = result["legs"][0]
        # coxa→femur = 0.052m, femur→tibia = 0.066m
        self.assertAlmostEqual(leg0["segment_lengths_m"][0], 0.052, places=3)
        self.assertAlmostEqual(leg0["segment_lengths_m"][1], 0.066, places=3)

    def test_hip_mounts(self) -> None:
        model = self._make_hexapod_sim_model()
        result = extract_leg_geometry(model)
        self.assertEqual(len(result["hip_mounts"]), 6)
        # First hip mount should be at (0.07, 0.075)
        hm0 = result["hip_mounts"][0]
        self.assertAlmostEqual(hm0[0], 0.07, places=4)
        self.assertAlmostEqual(hm0[1], 0.075, places=4)

    def test_body_dims(self) -> None:
        model = self._make_hexapod_sim_model()
        result = extract_leg_geometry(model)
        body_len, body_wid = result["body_dims_m"]
        self.assertAlmostEqual(body_len, 0.14, places=3)
        self.assertAlmostEqual(body_wid, 0.15, places=3)

    def test_empty_model(self) -> None:
        model = SimModel(name="empty")
        result = extract_leg_geometry(model)
        self.assertEqual(result["n_legs"], 0)


if __name__ == "__main__":
    unittest.main()
