"""Tests for the sim export module (SimModel building + URDF serialization).

Pure unit tests — no FreeCAD or network dependency.
"""
from __future__ import annotations

import math
import tempfile
import unittest
import xml.etree.ElementTree as ET

from server.motion_models import (
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.sim_export import (
    SimJoint,
    SimLink,
    SimModel,
    _box_inertia,
    _quat_inverse,
    _quat_multiply,
    _quat_to_rpy,
    build_sim_model,
    validate_urdf,
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
        """Manifest with rotated child placement produces correct RPY."""
        # Parent at identity, child rotated 90deg around Z
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
        # RPY should have ~90deg yaw
        self.assertAlmostEqual(joint.origin_rpy[0], 0.0, places=5)
        self.assertAlmostEqual(joint.origin_rpy[1], 0.0, places=5)
        self.assertAlmostEqual(joint.origin_rpy[2], math.radians(90), places=5)

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


if __name__ == "__main__":
    unittest.main()
