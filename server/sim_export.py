"""Simulation description export — format-agnostic intermediate representation.

Transforms a ``Mechanism`` (kinematic graph) + body mesh manifest into a
``SimModel`` (links + joints), then serializes to URDF (or future SDF/USD/MJCF).

The key design is separation of concerns:
- ``build_sim_model()`` assembles the kinematic tree from mechanism + manifest.
- ``write_urdf()`` serializes a ``SimModel`` to URDF XML (stdlib xml.etree).
- Future format writers (``write_sdf``, ``write_usd``, ``write_mjcf``) share
  the same ``build_sim_model()`` logic.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.motion_models import JointType, Mechanism

# Default density for mass estimation when only volume is available (kg/m^3).
# PLA plastic — conservative for simulation stability.
_DEFAULT_DENSITY_KG_M3 = 1250.0


@dataclass(frozen=True, slots=True)
class SimLink:
    """A rigid body (link) in the sim description."""
    name: str
    mesh_path: str | None = None
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)  # w,x,y,z
    mass_kg: float | None = None
    inertia: tuple[float, float, float, float, float, float] | None = None  # ixx,ixy,ixz,iyy,iyz,izz
    is_root: bool = False


@dataclass(frozen=True, slots=True)
class SimJoint:
    """A kinematic constraint between two links."""
    name: str
    joint_type: str  # "revolute", "prismatic", "fixed", "planar", "continuous"
    parent: str
    child: str
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    origin_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    origin_rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)
    limits: tuple[float, float] | None = None  # (lower, upper) in radians or meters
    mimic: tuple[str, float] | None = None  # (master_joint_name, multiplier)
    effort: float = 100.0   # max effort (Nm or N)
    velocity: float = 10.0  # max velocity (rad/s or m/s)
    damping: float = 0.1    # joint damping coefficient
    friction: float = 0.0   # joint friction


@dataclass(frozen=True, slots=True)
class SimModel:
    """Format-agnostic sim description: links + joints."""
    name: str
    links: tuple[SimLink, ...] = ()
    joints: tuple[SimJoint, ...] = ()


# ---------------------------------------------------------------------------
# Joint type mapping: motion_models.JointType -> URDF joint type string
# ---------------------------------------------------------------------------

_JOINT_TYPE_MAP: dict[JointType, str] = {
    JointType.REVOLUTE: "revolute",
    JointType.PRISMATIC: "prismatic",
    JointType.FIXED: "fixed",
    JointType.GEAR_MESH: "revolute",  # + mimic
    JointType.BELT_CHAIN: "revolute",  # + mimic
    JointType.PLANAR: "planar",
    JointType.CAM: "revolute",
}


# ---------------------------------------------------------------------------
# Quaternion helpers
# ---------------------------------------------------------------------------

def _quat_to_rpy(w: float, x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert quaternion (w,x,y,z) to roll-pitch-yaw (radians)."""
    # Roll (x-axis rotation)
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Pitch (y-axis rotation)
    sinp = 2.0 * (w * y - z * x)
    if abs(sinp) >= 1.0:
        pitch = math.copysign(math.pi / 2.0, sinp)
    else:
        pitch = math.asin(sinp)

    # Yaw (z-axis rotation)
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (roll, pitch, yaw)


def _quat_inverse(w: float, x: float, y: float, z: float) -> tuple[float, float, float, float]:
    """Return the inverse (conjugate) of a unit quaternion (w,x,y,z)."""
    return (w, -x, -y, -z)


def _quat_multiply(
    w1: float, x1: float, y1: float, z1: float,
    w2: float, x2: float, y2: float, z2: float,
) -> tuple[float, float, float, float]:
    """Multiply two quaternions (w,x,y,z): q1 * q2."""
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


# ---------------------------------------------------------------------------
# Inertia helpers
# ---------------------------------------------------------------------------

def _box_inertia(
    mass_kg: float,
    dx_m: float, dy_m: float, dz_m: float,
) -> tuple[float, float, float, float, float, float]:
    """Compute box inertia tensor (ixx, ixy, ixz, iyy, iyz, izz) from mass and dimensions in meters."""
    ixx = mass_kg / 12.0 * (dy_m ** 2 + dz_m ** 2)
    iyy = mass_kg / 12.0 * (dx_m ** 2 + dz_m ** 2)
    izz = mass_kg / 12.0 * (dx_m ** 2 + dy_m ** 2)
    return (ixx, 0.0, 0.0, iyy, 0.0, izz)


def build_sim_model(
    mechanism: Mechanism,
    body_manifest: list[dict[str, Any]],
) -> SimModel:
    """Transform a Mechanism + mesh manifest into a format-agnostic SimModel.

    ``body_manifest`` is a list of dicts from ``export_sim_package``, each with:
    - ``name``: body name (matches ``PartNode.body_name`` or ``PartNode.id``)
    - ``mesh_path``: path to the exported mesh file
    - ``placement``: ``{"position": [x,y,z], "rotation_quat": [w,x,y,z]}``
    - ``bbox_mm``: ``[dx, dy, dz]`` bounding box in mm (optional)
    - ``volume_mm3``: volume in mm^3 (optional)

    Parts without a matching manifest entry get a link with no mesh.
    """
    # Index manifest by body name for O(1) lookup
    manifest_by_name: dict[str, dict[str, Any]] = {}
    for entry in body_manifest:
        manifest_by_name[entry["name"]] = entry

    # Build links from mechanism parts
    links: list[SimLink] = []
    part_to_link: dict[str, str] = {}  # part_id -> link_name
    # Track placements for joint RPY computation (Bug 2)
    link_placement: dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]] = {}

    for part in mechanism.parts:
        link_name = part.id

        # Find mesh from manifest — match on body_name first, then part id
        manifest_entry = manifest_by_name.get(part.body_name or "") or manifest_by_name.get(part.id)

        mesh_path: str | None = None
        position = (0.0, 0.0, 0.0)
        rotation_quat = (1.0, 0.0, 0.0, 0.0)
        bbox_mm: list[float] | None = None
        volume_mm3: float | None = None

        if manifest_entry is not None:
            mesh_path = manifest_entry.get("mesh_path")
            plc = manifest_entry.get("placement", {})
            pos = plc.get("position", [0.0, 0.0, 0.0])
            position = (pos[0], pos[1], pos[2])
            quat = plc.get("rotation_quat", [1.0, 0.0, 0.0, 0.0])
            rotation_quat = (quat[0], quat[1], quat[2], quat[3])
            bbox_mm = manifest_entry.get("bbox_mm")
            volume_mm3 = manifest_entry.get("volume_mm3")

        link_placement[link_name] = (position, rotation_quat)

        # Compute inertia tensor (Bug 5)
        mass_kg = part.mass_kg
        inertia: tuple[float, float, float, float, float, float] | None = None

        if part.inertia_kg_m2 is not None:
            # Existing scalar → diagonal tensor (backward compat)
            val = part.inertia_kg_m2
            inertia = (val, 0.0, 0.0, val, 0.0, val)
        elif bbox_mm is not None and len(bbox_mm) == 3:
            # Auto-compute from bbox
            if mass_kg is None and volume_mm3 is not None and volume_mm3 > 0:
                # Estimate mass from volume (mm^3 → m^3) and default density
                mass_kg = (volume_mm3 * 1e-9) * _DEFAULT_DENSITY_KG_M3
            if mass_kg is not None and mass_kg > 0:
                dx_m = bbox_mm[0] / 1000.0
                dy_m = bbox_mm[1] / 1000.0
                dz_m = bbox_mm[2] / 1000.0
                inertia = _box_inertia(mass_kg, dx_m, dy_m, dz_m)

        links.append(SimLink(
            name=link_name,
            mesh_path=mesh_path,
            position=position,
            rotation_quat=rotation_quat,
            mass_kg=mass_kg,
            inertia=inertia,
            is_root=part.is_ground,
        ))
        part_to_link[part.id] = link_name

    # Index drives by joint_id for O(1) lookup (Bug 3)
    drives_by_joint: dict[str, Any] = {}
    for drive in mechanism.drives:
        drives_by_joint[drive.joint_id] = drive

    # Build joints from mechanism joints
    joints: list[SimJoint] = []
    # Track joint names for mimic references
    joint_edge_to_name: dict[str, str] = {}

    for jedge in mechanism.joints:
        joint_name = jedge.id
        joint_type = _JOINT_TYPE_MAP.get(jedge.joint_type, "fixed")
        parent_link = part_to_link.get(jedge.parent_part, jedge.parent_part)
        child_link = part_to_link.get(jedge.child_part, jedge.child_part)

        # Origin from joint's origin field (mm -> m for URDF)
        origin_xyz = (
            jedge.origin[0] / 1000.0,
            jedge.origin[1] / 1000.0,
            jedge.origin[2] / 1000.0,
        )

        # Compute relative orientation from manifest placements (Bug 2)
        parent_plc = link_placement.get(parent_link)
        child_plc = link_placement.get(child_link)
        if parent_plc is not None and child_plc is not None:
            q_parent = parent_plc[1]
            q_child = child_plc[1]
            q_parent_inv = _quat_inverse(*q_parent)
            q_relative = _quat_multiply(*q_parent_inv, *q_child)
            origin_rpy = _quat_to_rpy(*q_relative)
        else:
            origin_rpy = (0.0, 0.0, 0.0)

        # Limits
        limits: tuple[float, float] | None = None
        if jedge.joint_type == JointType.REVOLUTE:
            if jedge.min_angle_deg is not None and jedge.max_angle_deg is not None:
                limits = (
                    math.radians(jedge.min_angle_deg),
                    math.radians(jedge.max_angle_deg),
                )
        elif jedge.joint_type == JointType.PRISMATIC:
            if jedge.min_travel_mm is not None and jedge.max_travel_mm is not None:
                limits = (
                    jedge.min_travel_mm / 1000.0,
                    jedge.max_travel_mm / 1000.0,
                )

        # Effort/velocity from DriveCondition (Bug 3)
        drive = drives_by_joint.get(jedge.id)
        effort = drive.torque_nm if drive and drive.torque_nm else 100.0
        velocity = (drive.speed_rpm * 2.0 * math.pi / 60.0) if drive and drive.speed_rpm else 10.0

        # Mimic for gear_mesh / belt_chain
        mimic: tuple[str, float] | None = None
        if jedge.joint_type in (JointType.GEAR_MESH, JointType.BELT_CHAIN):
            # Gear ratio: child speed = parent speed * ratio
            ratio = jedge.gear_ratio
            if ratio is None and jedge.teeth_parent and jedge.teeth_child:
                ratio = jedge.teeth_parent / jedge.teeth_child
            if ratio is not None:
                # Find the parent's joint to reference as mimic master
                # For gear meshes, the parent part's joint is the master
                parent_joints = [
                    j for j in mechanism.joints
                    if (j.child_part == jedge.parent_part or j.parent_part == jedge.parent_part)
                    and j.id != jedge.id
                ]
                if parent_joints:
                    mimic = (parent_joints[0].id, ratio)

        joints.append(SimJoint(
            name=joint_name,
            joint_type=joint_type,
            parent=parent_link,
            child=child_link,
            axis=jedge.axis,
            origin_xyz=origin_xyz,
            origin_rpy=origin_rpy,
            limits=limits,
            mimic=mimic,
            effort=effort,
            velocity=velocity,
        ))
        joint_edge_to_name[jedge.id] = joint_name

    return SimModel(
        name=mechanism.name,
        links=tuple(links),
        joints=tuple(joints),
    )


# ---------------------------------------------------------------------------
# URDF writer
# ---------------------------------------------------------------------------

def _fmt(val: float) -> str:
    """Format a float, stripping trailing zeros."""
    return f"{val:.6g}"


def _xyz_str(xyz: tuple[float, float, float]) -> str:
    return f"{_fmt(xyz[0])} {_fmt(xyz[1])} {_fmt(xyz[2])}"


def _rpy_str(rpy: tuple[float, float, float]) -> str:
    return f"{_fmt(rpy[0])} {_fmt(rpy[1])} {_fmt(rpy[2])}"


def write_urdf(model: SimModel, output_path: str) -> str:
    """Serialize a SimModel to URDF XML.

    Returns the absolute path of the written file.
    """
    robot = ET.Element("robot", name=model.name)

    for link in model.links:
        link_el = ET.SubElement(robot, "link", name=link.name)

        if link.mesh_path is not None:
            # Visual — no <origin> needed: FreeCAD's Shape.exportStl() exports
            # vertices in body-local coordinates (pre-Placement). The joint
            # <origin> element handles parent->child frame positioning.
            visual = ET.SubElement(link_el, "visual")
            geometry = ET.SubElement(visual, "geometry")
            mesh = ET.SubElement(geometry, "mesh")
            mesh.set("filename", link.mesh_path)
            mesh.set("scale", "0.001 0.001 0.001")  # FreeCAD mm -> URDF m

            # Collision (same mesh)
            collision = ET.SubElement(link_el, "collision")
            c_geometry = ET.SubElement(collision, "geometry")
            c_mesh = ET.SubElement(c_geometry, "mesh")
            c_mesh.set("filename", link.mesh_path)
            c_mesh.set("scale", "0.001 0.001 0.001")  # FreeCAD mm -> URDF m

        # Inertial (optional)
        if link.mass_kg is not None:
            inertial = ET.SubElement(link_el, "inertial")
            mass_el = ET.SubElement(inertial, "mass")
            mass_el.set("value", _fmt(link.mass_kg))
            if link.inertia is not None:
                inertia_el = ET.SubElement(inertial, "inertia")
                inertia_el.set("ixx", _fmt(link.inertia[0]))
                inertia_el.set("ixy", _fmt(link.inertia[1]))
                inertia_el.set("ixz", _fmt(link.inertia[2]))
                inertia_el.set("iyy", _fmt(link.inertia[3]))
                inertia_el.set("iyz", _fmt(link.inertia[4]))
                inertia_el.set("izz", _fmt(link.inertia[5]))

    for joint in model.joints:
        joint_el = ET.SubElement(robot, "joint", name=joint.name, type=joint.joint_type)

        ET.SubElement(joint_el, "parent", link=joint.parent)
        ET.SubElement(joint_el, "child", link=joint.child)

        origin = ET.SubElement(joint_el, "origin")
        origin.set("xyz", _xyz_str(joint.origin_xyz))
        origin.set("rpy", _rpy_str(joint.origin_rpy))

        axis_el = ET.SubElement(joint_el, "axis")
        axis_el.set("xyz", _xyz_str(joint.axis))

        # URDF spec requires <limit> on revolute and prismatic joints.
        # effort and velocity are always required when limit is present.
        _LIMIT_REQUIRED = {"revolute", "prismatic"}
        if joint.limits is not None:
            limit_el = ET.SubElement(joint_el, "limit")
            limit_el.set("lower", _fmt(joint.limits[0]))
            limit_el.set("upper", _fmt(joint.limits[1]))
            limit_el.set("effort", _fmt(joint.effort))
            limit_el.set("velocity", _fmt(joint.velocity))
        elif joint.joint_type in _LIMIT_REQUIRED:
            # No explicit limits — emit defaults so URDF is valid
            limit_el = ET.SubElement(joint_el, "limit")
            limit_el.set("effort", _fmt(joint.effort))
            limit_el.set("velocity", _fmt(joint.velocity))

        if joint.mimic is not None:
            mimic_el = ET.SubElement(joint_el, "mimic")
            mimic_el.set("joint", joint.mimic[0])
            mimic_el.set("multiplier", _fmt(joint.mimic[1]))

        # Dynamics (damping + friction) for revolute and prismatic joints
        if joint.joint_type in ("revolute", "prismatic"):
            dynamics = ET.SubElement(joint_el, "dynamics")
            dynamics.set("damping", _fmt(joint.damping))
            dynamics.set("friction", _fmt(joint.friction))

    # Write with XML declaration
    tree = ET.ElementTree(robot)
    ET.indent(tree, space="  ")
    out_path = str(Path(output_path).resolve())
    tree.write(out_path, encoding="unicode", xml_declaration=True)

    return out_path
