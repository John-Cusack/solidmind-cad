"""Tests for the sim pipeline verification module.

Pure unit tests — no FreeCAD, Isaac, or network dependencies.
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest
from typing import Any

from server.models import Severity
from server.motion_models import (
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.sim_verify import (
    verify_mechanism_vs_tree,
    verify_mechanism_vs_urdf,
    verify_sim_package,
    verify_urdf_vs_isaac,
)


def _simple_mechanism(
    parts: list[tuple[str, str | None, bool]] | None = None,
    joints: list[tuple[str, str, str, str]] | None = None,
) -> Mechanism:
    """Build a simple mechanism for testing.

    parts: [(id, body_name, is_ground), ...]
    joints: [(id, type_str, parent, child), ...]
    """
    if parts is None:
        parts = [
            ("ground", None, True),
            ("link_a", "Body_A", False),
            ("link_b", "Body_B", False),
        ]
    if joints is None:
        joints = [
            ("j1", "revolute", "ground", "link_a"),
            ("j2", "revolute", "link_a", "link_b"),
        ]

    return Mechanism(
        name="test_mech",
        parts=tuple(
            PartNode(id=pid, body_name=bname, is_ground=gnd)
            for pid, bname, gnd in parts
        ),
        joints=tuple(
            JointEdge(
                id=jid, joint_type=JointType(jtype),
                parent_part=parent, child_part=child,
            )
            for jid, jtype, parent, child in joints
        ),
        drives=(),
    )


def _write_urdf(
    links: list[dict[str, Any]],
    joints: list[dict[str, Any]],
    tmpdir: str,
    name: str = "test_robot",
) -> str:
    """Write a minimal URDF file for testing. Returns the path."""
    import xml.etree.ElementTree as ET

    robot = ET.Element("robot", name=name)

    for link in links:
        link_el = ET.SubElement(robot, "link", name=link["name"])
        if link.get("mesh"):
            visual = ET.SubElement(link_el, "visual")
            geom = ET.SubElement(visual, "geometry")
            ET.SubElement(geom, "mesh", filename=link["mesh"])
        if link.get("mass") is not None:
            inertial = ET.SubElement(link_el, "inertial")
            ET.SubElement(inertial, "mass", value=str(link["mass"]))
            inertia = link.get("inertia", {"ixx": "0.001", "iyy": "0.001", "izz": "0.001"})
            ET.SubElement(inertial, "inertia",
                          ixx=str(inertia.get("ixx", "0.001")),
                          ixy="0", ixz="0",
                          iyy=str(inertia.get("iyy", "0.001")),
                          iyz="0",
                          izz=str(inertia.get("izz", "0.001")))

    for joint in joints:
        joint_el = ET.SubElement(robot, "joint",
                                  name=joint["name"],
                                  type=joint.get("type", "revolute"))
        ET.SubElement(joint_el, "parent", link=joint["parent"])
        ET.SubElement(joint_el, "child", link=joint["child"])
        if joint.get("type", "revolute") in ("revolute", "prismatic"):
            limits = joint.get("limits", {})
            ET.SubElement(joint_el, "limit",
                          lower=str(limits.get("lower", "-1.0472")),
                          upper=str(limits.get("upper", "1.0472")),
                          effort="100", velocity="10")

    tree = ET.ElementTree(robot)
    path = os.path.join(tmpdir, f"{name}.urdf")
    tree.write(path, xml_declaration=True)
    return path


class TestVerifyMechanismVsTree(unittest.TestCase):

    def test_all_parts_found(self) -> None:
        mech = _simple_mechanism()
        bodies = [
            {"name": "Body_A", "label": "Body_A"},
            {"name": "Body_B", "label": "Body_B"},
        ]
        findings = verify_mechanism_vs_tree(mech, bodies)
        self.assertEqual(len(findings), 0)

    def test_missing_part(self) -> None:
        mech = _simple_mechanism()
        bodies = [{"name": "Body_A", "label": "Body_A"}]
        findings = verify_mechanism_vs_tree(mech, bodies)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "mech_part_missing_body")
        self.assertEqual(findings[0].severity, Severity.BLOCK)
        self.assertIn("Body_B", findings[0].message)

    def test_case_insensitive_match(self) -> None:
        mech = _simple_mechanism()
        bodies = [
            {"name": "body_a", "label": "body_a"},
            {"name": "body_b", "label": "body_b"},
        ]
        findings = verify_mechanism_vs_tree(mech, bodies)
        self.assertEqual(len(findings), 0)

    def test_ground_parts_skipped(self) -> None:
        mech = _simple_mechanism(
            parts=[("ground", None, True)],
            joints=[],
        )
        findings = verify_mechanism_vs_tree(mech, [])
        self.assertEqual(len(findings), 0)

    def test_uses_part_id_when_no_body_name(self) -> None:
        mech = _simple_mechanism(
            parts=[("ground", None, True), ("my_link", None, False)],
            joints=[],
        )
        bodies = [{"name": "my_link", "label": "my_link"}]
        findings = verify_mechanism_vs_tree(mech, bodies)
        self.assertEqual(len(findings), 0)


class TestVerifyMechanismVsUrdf(unittest.TestCase):

    def test_urdf_file_missing(self) -> None:
        mech = _simple_mechanism()
        findings = verify_mechanism_vs_urdf(mech, "/nonexistent/path.urdf")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "urdf_file_missing")

    def test_valid_urdf(self) -> None:
        mech = _simple_mechanism()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create mesh files
            for name in ("link_a.stl", "link_b.stl"):
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write("solid test\nendsolid test\n")

            urdf_path = _write_urdf(
                links=[
                    {"name": "link_a", "mesh": "link_a.stl", "mass": 0.5},
                    {"name": "link_b", "mesh": "link_b.stl", "mass": 0.3},
                ],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "link_a", "child": "link_b",
                     "limits": {"lower": "-1.5", "upper": "1.5"}},
                ],
                tmpdir=tmpdir,
            )
            findings = verify_mechanism_vs_urdf(mech, urdf_path)
            # Should have no blockers (mesh exists, mass > 0)
            blockers = [f for f in findings if f.severity == Severity.BLOCK]
            self.assertEqual(len(blockers), 0)

    def test_missing_mesh_file(self) -> None:
        mech = _simple_mechanism()
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[
                    {"name": "link_a", "mesh": "nonexistent.stl", "mass": 0.5},
                    {"name": "link_b", "mesh": "link_b.stl", "mass": 0.3},
                ],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "link_a", "child": "link_b"},
                ],
                tmpdir=tmpdir,
            )
            findings = verify_mechanism_vs_urdf(mech, urdf_path)
            mesh_findings = [f for f in findings if f.rule_id == "urdf_mesh_missing"]
            self.assertTrue(len(mesh_findings) >= 1)

    def test_zero_mass_warning(self) -> None:
        mech = _simple_mechanism()
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[
                    {"name": "link_a", "mass": 0.0},
                    {"name": "link_b", "mass": 0.5},
                ],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "link_a", "child": "link_b"},
                ],
                tmpdir=tmpdir,
            )
            findings = verify_mechanism_vs_urdf(mech, urdf_path)
            mass_findings = [f for f in findings if f.rule_id == "urdf_link_zero_mass"]
            self.assertEqual(len(mass_findings), 1)
            self.assertIn("link_a", mass_findings[0].message)

    def test_default_limits_noted(self) -> None:
        """Joints with ±60° (±1.0472 rad) default limits and no mechanism spec get a note."""
        mech = _simple_mechanism()
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[
                    {"name": "link_a", "mass": 0.5},
                    {"name": "link_b", "mass": 0.3},
                ],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "link_a", "child": "link_b",
                     "limits": {"lower": str(-math.radians(60)), "upper": str(math.radians(60))}},
                    {"name": "j2", "type": "revolute", "parent": "link_a", "child": "link_b",
                     "limits": {"lower": str(-math.radians(60)), "upper": str(math.radians(60))}},
                ],
                tmpdir=tmpdir,
            )
            findings = verify_mechanism_vs_urdf(mech, urdf_path)
            default_findings = [f for f in findings if f.rule_id == "urdf_joint_default_limits"]
            self.assertTrue(len(default_findings) >= 1)

    def test_joint_type_mismatch(self) -> None:
        mech = _simple_mechanism(
            joints=[("j1", "prismatic", "ground", "link_a")],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "link_a", "mass": 0.5}],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "ground", "child": "link_a"},
                ],
                tmpdir=tmpdir,
            )
            findings = verify_mechanism_vs_urdf(mech, urdf_path)
            type_findings = [f for f in findings if f.rule_id == "urdf_joint_type_mismatch"]
            self.assertEqual(len(type_findings), 1)


class TestVerifyUrdfVsIsaac(unittest.TestCase):

    def test_diagnose_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "link_a"}],
                joints=[],
                tmpdir=tmpdir,
            )
            findings = verify_urdf_vs_isaac(urdf_path, {"error": "Stage not available"})
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].rule_id, "isaac_diagnose_error")

    def test_matching_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "link_a"}, {"name": "link_b"}],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "link_a", "child": "link_b"},
                ],
                tmpdir=tmpdir,
            )
            isaac_diag: dict[str, Any] = {
                "type_counts": {"PhysicsRevoluteJoint": 1, "Xform": 2},
                "joint_details": [
                    {
                        "path": "/robot/j1",
                        "type": "PhysicsRevoluteJoint",
                        "physics_body0": ["/robot/link_a"],
                        "physics_body1": ["/robot/link_b"],
                        "drive_angular_stiffness": 1000,
                        "drive_angular_damping": 100,
                    },
                ],
                "articulation_info": {
                    "prim_path": "/robot",
                    "dof_count": 1,
                    "dof_names": ["j1"],
                },
            }
            findings = verify_urdf_vs_isaac(urdf_path, isaac_diag)
            blockers = [f for f in findings if f.severity == Severity.BLOCK]
            self.assertEqual(len(blockers), 0)

    def test_missing_joints_in_isaac(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "a", "child": "b"},
                    {"name": "j2", "type": "revolute", "parent": "b", "child": "c"},
                ],
                tmpdir=tmpdir,
            )
            isaac_diag: dict[str, Any] = {
                "type_counts": {"PhysicsRevoluteJoint": 1},
                "joint_details": [],
            }
            findings = verify_urdf_vs_isaac(urdf_path, isaac_diag)
            count_findings = [f for f in findings if "count" in f.rule_id]
            self.assertTrue(len(count_findings) >= 1)

    def test_dof_count_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "a", "child": "b"},
                    {"name": "j2", "type": "revolute", "parent": "b", "child": "c"},
                ],
                tmpdir=tmpdir,
            )
            isaac_diag: dict[str, Any] = {
                "type_counts": {"PhysicsRevoluteJoint": 2},
                "joint_details": [],
                "articulation_info": {
                    "prim_path": "/robot",
                    "dof_count": 1,
                    "dof_names": ["j1"],
                },
            }
            findings = verify_urdf_vs_isaac(urdf_path, isaac_diag)
            dof_findings = [f for f in findings if f.rule_id == "isaac_dof_count_low"]
            self.assertEqual(len(dof_findings), 1)

    def test_joint_no_drive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "a"}, {"name": "b"}],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "a", "child": "b"},
                ],
                tmpdir=tmpdir,
            )
            isaac_diag: dict[str, Any] = {
                "type_counts": {"PhysicsRevoluteJoint": 1},
                "joint_details": [
                    {
                        "path": "/robot/j1",
                        "type": "PhysicsRevoluteJoint",
                        "physics_body0": ["/a"],
                        "physics_body1": ["/b"],
                        "drive_angular_stiffness": 0,
                        "drive_angular_damping": 0,
                    },
                ],
            }
            findings = verify_urdf_vs_isaac(urdf_path, isaac_diag)
            drive_findings = [f for f in findings if f.rule_id == "isaac_joint_no_drive"]
            self.assertEqual(len(drive_findings), 1)

    def test_joint_missing_body_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            urdf_path = _write_urdf(
                links=[{"name": "a"}, {"name": "b"}],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "a", "child": "b"},
                ],
                tmpdir=tmpdir,
            )
            isaac_diag: dict[str, Any] = {
                "type_counts": {"PhysicsRevoluteJoint": 1},
                "joint_details": [
                    {
                        "path": "/robot/j1",
                        "type": "PhysicsRevoluteJoint",
                        "physics_body0": [],
                        "physics_body1": ["/b"],
                    },
                ],
            }
            findings = verify_urdf_vs_isaac(urdf_path, isaac_diag)
            body_findings = [f for f in findings if f.rule_id == "isaac_joint_missing_body_target"]
            self.assertEqual(len(body_findings), 1)


class TestVerifySimPackageCombined(unittest.TestCase):

    def test_all_stages(self) -> None:
        mech = _simple_mechanism()
        bodies = [
            {"name": "Body_A", "label": "Body_A"},
            {"name": "Body_B", "label": "Body_B"},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("link_a.stl", "link_b.stl"):
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write("solid test\nendsolid test\n")

            urdf_path = _write_urdf(
                links=[
                    {"name": "link_a", "mesh": "link_a.stl", "mass": 0.5},
                    {"name": "link_b", "mesh": "link_b.stl", "mass": 0.3},
                ],
                joints=[
                    {"name": "j1", "type": "revolute", "parent": "link_a", "child": "link_b",
                     "limits": {"lower": "-1.5", "upper": "1.5"}},
                ],
                tmpdir=tmpdir,
            )

            isaac_diag: dict[str, Any] = {
                "type_counts": {"PhysicsRevoluteJoint": 1},
                "joint_details": [
                    {
                        "path": "/robot/j1",
                        "type": "PhysicsRevoluteJoint",
                        "physics_body0": ["/link_a"],
                        "physics_body1": ["/link_b"],
                        "drive_angular_stiffness": 1000,
                        "drive_angular_damping": 100,
                    },
                ],
                "articulation_info": {
                    "prim_path": "/robot",
                    "dof_count": 1,
                    "dof_names": ["j1"],
                },
            }

            result = verify_sim_package(
                mechanism=mech,
                model_tree_bodies=bodies,
                urdf_path=urdf_path,
                isaac_diagnose=isaac_diag,
            )

            self.assertEqual(len(result["stages_run"]), 3)
            self.assertIn("mechanism_vs_freecad", result["stages_run"])
            self.assertIn("mechanism_vs_urdf", result["stages_run"])
            self.assertIn("urdf_vs_isaac", result["stages_run"])
            self.assertEqual(result["blockers"], 0)
            self.assertTrue(result["passed"])

    def test_partial_stages(self) -> None:
        """Only run stage 1 when only model tree is provided."""
        mech = _simple_mechanism()
        bodies = [
            {"name": "Body_A", "label": "Body_A"},
            {"name": "Body_B", "label": "Body_B"},
        ]
        result = verify_sim_package(
            mechanism=mech,
            model_tree_bodies=bodies,
        )
        self.assertEqual(result["stages_run"], ["mechanism_vs_freecad"])
        self.assertTrue(result["passed"])

    def test_no_stages(self) -> None:
        mech = _simple_mechanism()
        result = verify_sim_package(mechanism=mech)
        self.assertEqual(result["stages_run"], [])
        self.assertTrue(result["passed"])

    def test_blocker_fails_passed(self) -> None:
        mech = _simple_mechanism()
        bodies = [{"name": "Body_A", "label": "Body_A"}]  # Body_B missing
        result = verify_sim_package(
            mechanism=mech,
            model_tree_bodies=bodies,
        )
        self.assertFalse(result["passed"])
        self.assertGreater(result["blockers"], 0)


if __name__ == "__main__":
    unittest.main()
