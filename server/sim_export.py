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

from server.models import Finding, Severity
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

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SimLink: name must be non-empty")
        if self.mass_kg is not None and self.mass_kg < 0:
            raise ValueError(f"SimLink '{self.name}': mass_kg must be >= 0, got {self.mass_kg}")
        if self.inertia is not None:
            if len(self.inertia) != 6:
                raise ValueError(f"SimLink '{self.name}': inertia must be a 6-tuple (ixx,ixy,ixz,iyy,iyz,izz)")
            ixx, _ixy, _ixz, iyy, _iyz, izz = self.inertia
            if ixx < 0 or iyy < 0 or izz < 0:
                raise ValueError(
                    f"SimLink '{self.name}': diagonal inertia values must be >= 0, "
                    f"got ixx={ixx}, iyy={iyy}, izz={izz}"
                )


# Joint types that require <limit lower= upper=> in URDF.
_LIMIT_REQUIRED_TYPES = frozenset({"revolute", "prismatic"})


@dataclass(frozen=True, slots=True)
class SimJoint:
    """A kinematic constraint between two links.

    Enforces URDF invariants at construction time:
    - ``limits`` is required for revolute and prismatic joints.
    - ``axis`` must be a unit vector.
    """
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

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SimJoint: name must be non-empty")
        if not self.parent:
            raise ValueError(f"SimJoint '{self.name}': parent must be non-empty")
        if not self.child:
            raise ValueError(f"SimJoint '{self.name}': child must be non-empty")
        if self.joint_type in _LIMIT_REQUIRED_TYPES and self.limits is None:
            raise ValueError(
                f"SimJoint '{self.name}': {self.joint_type} joints require limits "
                f"(lower, upper) — got None"
            )
        if self.limits is not None and self.limits[0] > self.limits[1]:
            raise ValueError(
                f"SimJoint '{self.name}': limits lower ({self.limits[0]}) > "
                f"upper ({self.limits[1]})"
            )
        mag_sq = sum(a * a for a in self.axis)
        if not math.isclose(mag_sq, 1.0, rel_tol=1e-6):
            raise ValueError(
                f"SimJoint '{self.name}': axis must be unit vector, "
                f"got magnitude {math.sqrt(mag_sq):.6f}"
            )


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
    *,
    ground_clearance_m: float | None = None,
) -> SimModel:
    """Transform a Mechanism + mesh manifest into a format-agnostic SimModel.

    ``body_manifest`` is a list of dicts from ``export_sim_package``, each with:
    - ``name``: body name (matches ``PartNode.body_name`` or ``PartNode.id``)
    - ``mesh_path``: path to the exported mesh file
    - ``placement``: ``{"position": [x,y,z], "rotation_quat": [w,x,y,z]}``
    - ``bbox_mm``: ``[dx, dy, dz]`` bounding box in mm (optional)
    - ``bbox_min_mm``: ``[xmin, ymin, zmin]`` bounding box min in mm (optional)
    - ``volume_mm3``: volume in mm^3 (optional)

    Parts without a matching manifest entry get a link with no mesh.

    If ``ground_clearance_m`` is set, a ``base_link`` with a fixed joint is
    prepended to raise the root link above the ground plane by that many meters.
    Use this for ground-standing robots (hexapods, wheeled bots, etc.) where the
    mesh geometry extends below the kinematic origin.
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

    # -----------------------------------------------------------------------
    # Compute each part's world-frame position AND yaw (frame orientation)
    # from the kinematic tree.  The root/ground part sits at (0,0,0) with
    # yaw=0.  For every joint, the child part's world position equals the
    # joint's world-frame origin (mm).
    #
    # For joints whose parent is the ground/root part, we auto-compute an
    # outward yaw so the child frame's local +X points from the body center
    # toward the joint position.  Body-local meshes extend along +X, so
    # this orients legs radially outward on walking robots (hexapods,
    # quadrupeds, etc.).  Subsequent joints in the chain inherit the
    # parent's yaw — their world-frame offsets are rotated into the
    # parent's local frame.
    # -----------------------------------------------------------------------
    part_world_pos: dict[str, tuple[float, float, float]] = {}
    part_world_yaw: dict[str, float] = {}  # cumulative Z rotation (rad)
    ground_parts: set[str] = set()
    for part in mechanism.parts:
        if part.is_ground:
            part_world_pos[part.id] = (0.0, 0.0, 0.0)
            part_world_yaw[part.id] = 0.0
            ground_parts.add(part.id)

    # BFS / iterative pass — compute world pos + yaw for every reachable part.
    # For root-attached joints: yaw = atan2(dy, dx) to orient child +X outward.
    # For deeper joints: yaw inherited from parent (no extra rotation).
    joint_added_yaw: dict[str, float] = {}  # joint_id -> yaw added by this joint
    remaining = list(mechanism.joints)
    max_iters = len(remaining) + 1
    for _ in range(max_iters):
        still_remaining: list[Any] = []
        for jedge in remaining:
            if jedge.parent_part in part_world_pos:
                # Place child at the joint's world origin
                part_world_pos[jedge.child_part] = tuple(jedge.origin)  # type: ignore[arg-type]

                parent_yaw = part_world_yaw.get(jedge.parent_part, 0.0)

                # Auto-compute outward yaw for root-attached joints.
                # Formula: atan2(dy, dx) orients child +X toward (dx, dy).
                # Body-local meshes extend along +X, so this aligns them
                # radially outward from the body center.
                added_yaw = 0.0
                if jedge.parent_part in ground_parts:
                    ppos = part_world_pos[jedge.parent_part]
                    dx = jedge.origin[0] - ppos[0]
                    dy = jedge.origin[1] - ppos[1]
                    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                        added_yaw = math.atan2(dy, dx)

                joint_added_yaw[jedge.id] = added_yaw
                part_world_yaw[jedge.child_part] = parent_yaw + added_yaw
            else:
                still_remaining.append(jedge)
        if len(still_remaining) == len(remaining):
            break  # No progress — remaining joints have unreachable parents
        remaining = still_remaining

    # Build joints from mechanism joints
    joints: list[SimJoint] = []
    # Track joint names for mimic references
    joint_edge_to_name: dict[str, str] = {}

    for jedge in mechanism.joints:
        joint_name = jedge.id
        joint_type = _JOINT_TYPE_MAP.get(jedge.joint_type, "fixed")
        parent_link = part_to_link.get(jedge.parent_part, jedge.parent_part)
        child_link = part_to_link.get(jedge.child_part, jedge.child_part)

        # Compute parent-relative joint origin (mm -> m for URDF).
        # jedge.origin is in world frame; subtract parent's world pos to get
        # the world-frame offset, then rotate into the parent's local frame.
        parent_pos = part_world_pos.get(jedge.parent_part, (0.0, 0.0, 0.0))
        world_dx = jedge.origin[0] - parent_pos[0]
        world_dy = jedge.origin[1] - parent_pos[1]
        world_dz = jedge.origin[2] - parent_pos[2]

        parent_yaw = part_world_yaw.get(jedge.parent_part, 0.0)
        if abs(parent_yaw) > 1e-9:
            # Rotate world offset into parent's local frame: R_z(-parent_yaw)
            c = math.cos(-parent_yaw)
            s = math.sin(-parent_yaw)
            local_dx = world_dx * c - world_dy * s
            local_dy = world_dx * s + world_dy * c
            local_dz = world_dz
        else:
            local_dx, local_dy, local_dz = world_dx, world_dy, world_dz

        origin_xyz = (local_dx / 1000.0, local_dy / 1000.0, local_dz / 1000.0)

        # Joint rpy: prefer auto-computed outward yaw (Bug 2); fall back to
        # manifest-based relative orientation when no outward yaw applies.
        added_yaw = joint_added_yaw.get(jedge.id, 0.0)
        if abs(added_yaw) > 1e-9:
            origin_rpy = (0.0, 0.0, added_yaw)
        else:
            # Bug 2 fix: with body-local meshes, pitch/roll come from joint
            # angle targets at runtime — never bake manifest quaternions here.
            origin_rpy = (0.0, 0.0, 0.0)

        # Limits — required for revolute and prismatic URDF joint types.
        # Use mechanism data when available, otherwise provide sensible defaults
        # so the SimJoint invariant (limits required) is always satisfied.
        limits: tuple[float, float] | None = None
        if jedge.joint_type in (
            JointType.REVOLUTE, JointType.GEAR_MESH,
            JointType.BELT_CHAIN, JointType.CAM,
        ):
            if jedge.min_angle_deg is not None and jedge.max_angle_deg is not None:
                limits = (
                    math.radians(jedge.min_angle_deg),
                    math.radians(jedge.max_angle_deg),
                )
            else:
                # Default: ±π (full rotation range)
                limits = (-math.pi, math.pi)
        elif jedge.joint_type == JointType.PRISMATIC:
            if jedge.min_travel_mm is not None and jedge.max_travel_mm is not None:
                limits = (
                    jedge.min_travel_mm / 1000.0,
                    jedge.max_travel_mm / 1000.0,
                )
            else:
                # Default: 0 to 1m range
                limits = (0.0, 1.0)

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

        # Bug 3 fix: rotate world-frame axis into child link's local frame.
        # The child frame is rotated by (parent_yaw + added_yaw) from world,
        # which is stored as part_world_yaw[child].  Use that so root-attached
        # joints with non-Z axes are also handled correctly.
        child_yaw = part_world_yaw.get(jedge.child_part, 0.0)
        if abs(child_yaw) > 1e-9:
            ax, ay, az = jedge.axis
            c = math.cos(-child_yaw)
            s = math.sin(-child_yaw)
            local_axis = (ax * c - ay * s, ax * s + ay * c, az)
            mag = math.sqrt(sum(a * a for a in local_axis))
            local_axis = tuple(a / mag for a in local_axis)
        else:
            local_axis = jedge.axis

        joints.append(SimJoint(
            name=joint_name,
            joint_type=joint_type,
            parent=parent_link,
            child=child_link,
            axis=local_axis,
            origin_xyz=origin_xyz,
            origin_rpy=origin_rpy,
            limits=limits,
            mimic=mimic,
            effort=effort,
            velocity=velocity,
        ))
        joint_edge_to_name[jedge.id] = joint_name

    # -----------------------------------------------------------------------
    # Ground clearance: if requested, add a base_link with a fixed joint
    # that raises the root link above the ground plane.
    # -----------------------------------------------------------------------
    if ground_clearance_m is not None and ground_clearance_m > 0:
        root_link = next((link for link in links if link.is_root), None)
        if root_link is not None:
            # Insert base_link as the new root (empty link, no mesh)
            base_link = SimLink(name="base_link", is_root=True)

            # Demote the original root link
            links = [
                SimLink(
                    name=lk.name,
                    mesh_path=lk.mesh_path,
                    position=lk.position,
                    rotation_quat=lk.rotation_quat,
                    mass_kg=lk.mass_kg,
                    inertia=lk.inertia,
                    is_root=False,
                ) if lk.is_root else lk
                for lk in links
            ]
            links.insert(0, base_link)

            # Add fixed joint from base_link to original root
            joints.insert(0, SimJoint(
                name=f"base_to_{root_link.name}",
                joint_type="fixed",
                parent="base_link",
                child=root_link.name,
                origin_xyz=(0.0, 0.0, ground_clearance_m),
            ))

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
            # Visual — no <origin> needed: export_sim_package strips the Body
            # Placement so mesh vertices are in body-local coordinates.
            # The joint <origin> element handles parent->child positioning.
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
        # SimJoint.__post_init__ enforces limits are always present for these types.
        if joint.limits is not None:
            limit_el = ET.SubElement(joint_el, "limit")
            limit_el.set("lower", _fmt(joint.limits[0]))
            limit_el.set("upper", _fmt(joint.limits[1]))
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


# ---------------------------------------------------------------------------
# URDF post-generation validator
# ---------------------------------------------------------------------------

# Maximum plausible joint origin magnitude (in meters).  Origins beyond this
# distance from the parent likely indicate absolute-instead-of-relative coords.
_MAX_ORIGIN_MAGNITUDE_M = 10.0

# Joint types that require <limit> per URDF spec.
_URDF_LIMIT_REQUIRED = frozenset({"revolute", "prismatic"})


def validate_urdf(path: str) -> list[Finding]:
    """Parse a generated URDF and check structural invariants.

    Returns a list of ``Finding`` objects.  Findings with ``severity=BLOCK``
    indicate the URDF is likely unusable in simulation; ``WARN`` signals issues
    that may cause subtle problems; ``NOTE`` is informational.

    Checks performed:
    1. Joint origin magnitude is plausible (catches absolute coords).
    2. Revolute/prismatic joints have ``<limit lower= upper=>``.
    3. Non-root links with mesh have ``<visual>``, ``<collision>``, ``<inertial>``.
    4. Inertia diagonal values are non-negative (positive semi-definite proxy).
    5. Joint axis is unit-length.
    6. Link tree is connected (no orphan links).
    7. Mesh scale factors are consistent (all 0.001 for mm→m).
    8. No duplicate mesh paths across links.
    """
    findings: list[Finding] = []
    tree = ET.parse(path)
    root = tree.getroot()

    if root.tag != "robot":
        findings.append(Finding(
            rule_id="urdf.root_tag",
            severity=Severity.BLOCK,
            message=f"Root element is <{root.tag}>, expected <robot>",
        ))
        return findings

    links = root.findall("link")
    joints = root.findall("joint")
    link_names = {lk.attrib.get("name", "") for lk in links}

    # --- Check 1 & 2 & 5: Joint-level checks ---
    for jel in joints:
        jname = jel.attrib.get("name", "?")
        jtype = jel.attrib.get("type", "")

        # Origin magnitude
        origin_el = jel.find("origin")
        if origin_el is not None:
            xyz_str = origin_el.attrib.get("xyz", "0 0 0")
            try:
                xyz = [float(v) for v in xyz_str.split()]
                mag = math.sqrt(sum(v * v for v in xyz))
                if mag > _MAX_ORIGIN_MAGNITUDE_M:
                    findings.append(Finding(
                        rule_id="urdf.origin_magnitude",
                        severity=Severity.WARN,
                        message=(
                            f"Joint '{jname}' origin magnitude {mag:.3f}m exceeds "
                            f"{_MAX_ORIGIN_MAGNITUDE_M}m — possible absolute "
                            f"(world-frame) coordinates instead of parent-relative"
                        ),
                        field=f"joint/{jname}/origin",
                    ))
            except (ValueError, IndexError):
                findings.append(Finding(
                    rule_id="urdf.origin_parse",
                    severity=Severity.BLOCK,
                    message=f"Joint '{jname}' origin xyz is malformed: '{xyz_str}'",
                    field=f"joint/{jname}/origin",
                ))

        # Limit required for revolute/prismatic
        if jtype in _URDF_LIMIT_REQUIRED:
            limit_el = jel.find("limit")
            if limit_el is None:
                findings.append(Finding(
                    rule_id="urdf.missing_limit",
                    severity=Severity.BLOCK,
                    message=f"Joint '{jname}' ({jtype}) is missing <limit> element",
                    field=f"joint/{jname}/limit",
                ))
            elif "lower" not in limit_el.attrib or "upper" not in limit_el.attrib:
                findings.append(Finding(
                    rule_id="urdf.missing_limit_bounds",
                    severity=Severity.WARN,
                    message=(
                        f"Joint '{jname}' ({jtype}) has <limit> but missing "
                        f"lower/upper bounds"
                    ),
                    field=f"joint/{jname}/limit",
                ))

        # Axis unit length
        axis_el = jel.find("axis")
        if axis_el is not None:
            axis_str = axis_el.attrib.get("xyz", "0 0 1")
            try:
                axis_vals = [float(v) for v in axis_str.split()]
                axis_mag = math.sqrt(sum(v * v for v in axis_vals))
                if not math.isclose(axis_mag, 1.0, rel_tol=1e-3):
                    findings.append(Finding(
                        rule_id="urdf.axis_not_unit",
                        severity=Severity.WARN,
                        message=(
                            f"Joint '{jname}' axis magnitude is {axis_mag:.6f}, "
                            f"expected 1.0"
                        ),
                        field=f"joint/{jname}/axis",
                    ))
            except (ValueError, IndexError):
                pass

        # Joint references valid links
        parent_el = jel.find("parent")
        child_el = jel.find("child")
        if parent_el is not None:
            plink = parent_el.attrib.get("link", "")
            if plink not in link_names:
                findings.append(Finding(
                    rule_id="urdf.dangling_parent",
                    severity=Severity.BLOCK,
                    message=f"Joint '{jname}' references unknown parent link '{plink}'",
                    field=f"joint/{jname}/parent",
                ))
        if child_el is not None:
            clink = child_el.attrib.get("link", "")
            if clink not in link_names:
                findings.append(Finding(
                    rule_id="urdf.dangling_child",
                    severity=Severity.BLOCK,
                    message=f"Joint '{jname}' references unknown child link '{clink}'",
                    field=f"joint/{jname}/child",
                ))

    # --- Check 3: Link completeness (visual, collision, inertial) ---
    # Identify child links (links that appear as a joint child).
    child_links = set()
    for jel in joints:
        child_el = jel.find("child")
        if child_el is not None:
            child_links.add(child_el.attrib.get("link", ""))

    for lel in links:
        lname = lel.attrib.get("name", "?")
        has_visual = lel.find("visual") is not None
        has_collision = lel.find("collision") is not None
        has_inertial = lel.find("inertial") is not None

        # Skip root/base links that are intentionally empty (e.g. base_link)
        # A link is considered "content-bearing" if it has any geometry or is a
        # child in a joint (i.e. not the root).
        is_content_bearing = has_visual or has_collision or lname in child_links

        if is_content_bearing:
            if not has_visual:
                findings.append(Finding(
                    rule_id="urdf.missing_visual",
                    severity=Severity.WARN,
                    message=f"Link '{lname}' has no <visual> element",
                    field=f"link/{lname}/visual",
                ))
            if not has_collision:
                findings.append(Finding(
                    rule_id="urdf.missing_collision",
                    severity=Severity.WARN,
                    message=f"Link '{lname}' has no <collision> element",
                    field=f"link/{lname}/collision",
                ))
            if not has_inertial:
                findings.append(Finding(
                    rule_id="urdf.missing_inertial",
                    severity=Severity.WARN,
                    message=f"Link '{lname}' has no <inertial> element",
                    field=f"link/{lname}/inertial",
                ))

    # --- Check 4: Inertia diagonal non-negative ---
    for lel in links:
        lname = lel.attrib.get("name", "?")
        inertia_el = lel.find("inertial/inertia")
        if inertia_el is not None:
            for diag in ("ixx", "iyy", "izz"):
                val_str = inertia_el.attrib.get(diag)
                if val_str is not None:
                    try:
                        val = float(val_str)
                        if val < 0:
                            findings.append(Finding(
                                rule_id="urdf.negative_inertia",
                                severity=Severity.BLOCK,
                                message=(
                                    f"Link '{lname}' has negative {diag}={val}"
                                ),
                                field=f"link/{lname}/inertial/inertia/{diag}",
                            ))
                    except ValueError:
                        pass

    # --- Check 6: Connected tree ---
    # Build adjacency from joints and check all links are reachable from root.
    if links and joints:
        adjacency: dict[str, set[str]] = {lk.attrib.get("name", ""): set() for lk in links}
        for jel in joints:
            parent_el = jel.find("parent")
            child_el = jel.find("child")
            if parent_el is not None and child_el is not None:
                pname = parent_el.attrib.get("link", "")
                cname = child_el.attrib.get("link", "")
                if pname in adjacency:
                    adjacency[pname].add(cname)
                if cname in adjacency:
                    adjacency[cname].add(pname)

        # BFS from first link
        visited: set[str] = set()
        queue = [links[0].attrib.get("name", "")]
        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)
            queue.extend(adjacency.get(node, set()) - visited)

        orphans = link_names - visited
        for orphan in sorted(orphans):
            findings.append(Finding(
                rule_id="urdf.disconnected_link",
                severity=Severity.BLOCK,
                message=f"Link '{orphan}' is not connected to the kinematic tree",
                field=f"link/{orphan}",
            ))

    # --- Check 7: Consistent mesh scale ---
    seen_scales: set[str] = set()
    for lel in links:
        for tag in ("visual", "collision"):
            mesh_el = lel.find(f"{tag}/geometry/mesh")
            if mesh_el is not None:
                scale = mesh_el.attrib.get("scale")
                if scale is not None:
                    seen_scales.add(scale)
    if len(seen_scales) > 1:
        findings.append(Finding(
            rule_id="urdf.inconsistent_scale",
            severity=Severity.WARN,
            message=f"Inconsistent mesh scales found: {sorted(seen_scales)}",
        ))

    # --- Check 8: Duplicate mesh paths ---
    mesh_paths: dict[str, list[str]] = {}  # path -> list of link names
    for lel in links:
        lname = lel.attrib.get("name", "?")
        for tag in ("visual", "collision"):
            mesh_el = lel.find(f"{tag}/geometry/mesh")
            if mesh_el is not None:
                fpath = mesh_el.attrib.get("filename", "")
                if fpath:
                    mesh_paths.setdefault(fpath, []).append(lname)
    for fpath, lnames in mesh_paths.items():
        # Deduplicate: same link appears twice (visual + collision) — that's fine
        unique_links = sorted(set(lnames))
        if len(unique_links) > 1:
            findings.append(Finding(
                rule_id="urdf.duplicate_mesh",
                severity=Severity.WARN,
                message=(
                    f"Mesh '{fpath}' is used by multiple links: "
                    f"{', '.join(unique_links)}"
                ),
            ))

    return findings


# ---------------------------------------------------------------------------
# Forward-kinematics URDF validator
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MeshBBox:
    """Axis-aligned bounding box in body-local mm coordinates."""
    min_pt: tuple[float, float, float]
    max_pt: tuple[float, float, float]


def _rpy_to_matrix(r: float, p: float, y: float) -> list[list[float]]:
    """Convert URDF fixed-axis XYZ roll-pitch-yaw to a 3x3 rotation matrix.

    URDF convention: R = Rz(yaw) * Ry(pitch) * Rx(roll).
    """
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ]


def _make_transform_4x4(
    xyz: tuple[float, float, float],
    rpy: tuple[float, float, float],
) -> list[list[float]]:
    """Build a 4x4 homogeneous transform from URDF origin (xyz, rpy)."""
    rot = _rpy_to_matrix(rpy[0], rpy[1], rpy[2])
    return [
        [rot[0][0], rot[0][1], rot[0][2], xyz[0]],
        [rot[1][0], rot[1][1], rot[1][2], xyz[1]],
        [rot[2][0], rot[2][1], rot[2][2], xyz[2]],
        [0.0,       0.0,       0.0,       1.0],
    ]


def _multiply_4x4(
    a: list[list[float]], b: list[list[float]],
) -> list[list[float]]:
    """Multiply two 4x4 matrices."""
    result = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            s = 0.0
            for k in range(4):
                s += a[i][k] * b[k][j]
            result[i][j] = s
    return result


def _transform_point(
    t: list[list[float]], pt: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Apply a 4x4 transform to a 3D point."""
    x = t[0][0] * pt[0] + t[0][1] * pt[1] + t[0][2] * pt[2] + t[0][3]
    y = t[1][0] * pt[0] + t[1][1] * pt[1] + t[1][2] * pt[2] + t[1][3]
    z = t[2][0] * pt[0] + t[2][1] * pt[1] + t[2][2] * pt[2] + t[2][3]
    return (x, y, z)


def _bbox_corners(bbox: MeshBBox) -> list[tuple[float, float, float]]:
    """Return the 8 corners of an axis-aligned bounding box."""
    lo, hi = bbox.min_pt, bbox.max_pt
    return [
        (lo[0], lo[1], lo[2]),
        (lo[0], lo[1], hi[2]),
        (lo[0], hi[1], lo[2]),
        (lo[0], hi[1], hi[2]),
        (hi[0], lo[1], lo[2]),
        (hi[0], lo[1], hi[2]),
        (hi[0], hi[1], lo[2]),
        (hi[0], hi[1], hi[2]),
    ]


def _transform_bbox(
    t: list[list[float]],
    bbox: MeshBBox,
    scale: float = 1.0,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Transform bbox corners through T (with uniform scale), return world AABB (min, max)."""
    corners = _bbox_corners(bbox)
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for corner in corners:
        scaled = (corner[0] * scale, corner[1] * scale, corner[2] * scale)
        wp = _transform_point(t, scaled)
        xs.append(wp[0])
        ys.append(wp[1])
        zs.append(wp[2])
    return (
        (min(xs), min(ys), min(zs)),
        (max(xs), max(ys), max(zs)),
    )


def _dot3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _aabb_volume(
    lo: tuple[float, float, float], hi: tuple[float, float, float],
) -> float:
    dx = max(0.0, hi[0] - lo[0])
    dy = max(0.0, hi[1] - lo[1])
    dz = max(0.0, hi[2] - lo[2])
    return dx * dy * dz


def _aabb_overlap_volume(
    lo1: tuple[float, float, float], hi1: tuple[float, float, float],
    lo2: tuple[float, float, float], hi2: tuple[float, float, float],
) -> float:
    """Compute overlap volume of two AABBs. Returns 0 if no overlap."""
    ox = max(0.0, min(hi1[0], hi2[0]) - max(lo1[0], lo2[0]))
    oy = max(0.0, min(hi1[1], hi2[1]) - max(lo1[1], lo2[1]))
    oz = max(0.0, min(hi1[2], hi2[2]) - max(lo1[2], lo2[2]))
    return ox * oy * oz


def validate_urdf_fk(
    path: str,
    mesh_bboxes: dict[str, MeshBBox] | None = None,
    ground_clearance_m: float | None = None,
) -> list[Finding]:
    """Parse a URDF and run forward-kinematics geometric checks.

    Catches assembly bugs that structural validation (``validate_urdf``) misses:
    world-frame meshes, baked pitch in RPY, world-frame axes, yaw conventions.

    Args:
        path: Path to the URDF file.
        mesh_bboxes: Per-link body-local bounding boxes in mm.
        ground_clearance_m: Expected chassis height above ground (meters).

    Returns:
        List of Finding objects.
    """
    findings: list[Finding] = []
    tree = ET.parse(path)
    root = tree.getroot()

    if root.tag != "robot":
        return findings

    # Parse links and joints from URDF
    links_el = root.findall("link")
    joints_el = root.findall("joint")

    link_names = {lk.attrib.get("name", "") for lk in links_el}

    # Build parent->child adjacency and joint data
    joint_data: dict[str, dict[str, Any]] = {}  # joint_name -> {parent, child, xyz, rpy, axis, type}
    parent_to_joints: dict[str, list[str]] = {}  # parent_link -> [joint_names]
    child_to_joint: dict[str, str] = {}  # child_link -> joint_name

    for jel in joints_el:
        jname = jel.attrib.get("name", "")
        jtype = jel.attrib.get("type", "")
        parent_el = jel.find("parent")
        child_el = jel.find("child")
        if parent_el is None or child_el is None:
            continue
        plink = parent_el.attrib.get("link", "")
        clink = child_el.attrib.get("link", "")

        origin_el = jel.find("origin")
        xyz = (0.0, 0.0, 0.0)
        rpy = (0.0, 0.0, 0.0)
        if origin_el is not None:
            xyz_str = origin_el.attrib.get("xyz", "0 0 0")
            rpy_str = origin_el.attrib.get("rpy", "0 0 0")
            try:
                xyz_vals = [float(v) for v in xyz_str.split()]
                xyz = (xyz_vals[0], xyz_vals[1], xyz_vals[2])
            except (ValueError, IndexError):
                pass
            try:
                rpy_vals = [float(v) for v in rpy_str.split()]
                rpy = (rpy_vals[0], rpy_vals[1], rpy_vals[2])
            except (ValueError, IndexError):
                pass

        axis_el = jel.find("axis")
        axis = (0.0, 0.0, 1.0)
        if axis_el is not None:
            try:
                axis_vals = [float(v) for v in axis_el.attrib.get("xyz", "0 0 1").split()]
                axis = (axis_vals[0], axis_vals[1], axis_vals[2])
            except (ValueError, IndexError):
                pass

        joint_data[jname] = {
            "parent": plink,
            "child": clink,
            "xyz": xyz,
            "rpy": rpy,
            "axis": axis,
            "type": jtype,
        }
        parent_to_joints.setdefault(plink, []).append(jname)
        child_to_joint[clink] = jname

    # Find root link (not a child of any joint)
    child_links = set(child_to_joint.keys())
    root_links = link_names - child_links
    if not root_links:
        return findings
    root_link = sorted(root_links)[0]

    # Compute world transforms for each link via BFS
    identity_4x4 = _make_transform_4x4((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    link_world_tf: dict[str, list[list[float]]] = {root_link: identity_4x4}

    queue = [root_link]
    visited: set[str] = {root_link}
    while queue:
        current = queue.pop(0)
        for jname in parent_to_joints.get(current, []):
            jd = joint_data[jname]
            child = jd["child"]
            if child in visited:
                continue
            joint_tf = _make_transform_4x4(jd["xyz"], jd["rpy"])
            parent_tf = link_world_tf[current]
            link_world_tf[child] = _multiply_4x4(parent_tf, joint_tf)
            visited.add(child)
            queue.append(child)

    # --- Check 1: urdf.fk.chassis_height ---
    # Find the chassis link (first link with a mesh that's a direct child of
    # root_link via fixed joint, or the root_link itself if it has a mesh).
    if ground_clearance_m is not None:
        chassis_link = None
        # Check if root_link has a direct fixed-joint child (base_link pattern)
        for jname in parent_to_joints.get(root_link, []):
            jd = joint_data[jname]
            if jd["type"] == "fixed":
                chassis_link = jd["child"]
                break
        if chassis_link is None:
            chassis_link = root_link

        if chassis_link in link_world_tf:
            chassis_tf = link_world_tf[chassis_link]
            chassis_z = chassis_tf[2][3]  # Z translation in world
            diff = abs(chassis_z - ground_clearance_m)
            if diff > 0.010:  # 10mm tolerance
                findings.append(Finding(
                    rule_id="urdf.fk.chassis_height",
                    severity=Severity.WARN,
                    message=(
                        f"Chassis '{chassis_link}' at Z={chassis_z:.4f}m, "
                        f"expected {ground_clearance_m:.4f}m (diff {diff:.4f}m)"
                    ),
                    field=f"link/{chassis_link}",
                ))

    # --- Check 2: urdf.fk.link_chain_gap ---
    # Parent mesh tip (max local +X, transformed to world) should be within
    # 5mm of child joint origin.  Skip multi-child parents (e.g. chassis)
    # where the "+X tip" concept doesn't apply.
    if mesh_bboxes:
        _SCALE_MM_TO_M = 0.001
        _GAP_TOLERANCE_M = 0.005  # 5mm

        # Count children per parent to skip multi-child bodies
        parent_child_count: dict[str, int] = {}
        for jd in joint_data.values():
            parent_child_count[jd["parent"]] = parent_child_count.get(jd["parent"], 0) + 1

        for jname, jd in joint_data.items():
            parent = jd["parent"]
            if parent not in mesh_bboxes or parent not in link_world_tf:
                continue
            # Skip multi-child parents (chassis, hubs) — tip check only
            # makes sense for serial chain links with one child.
            if parent_child_count.get(parent, 0) > 1:
                continue
            parent_bbox = mesh_bboxes[parent]
            parent_tf = link_world_tf[parent]

            # Parent mesh tip: point at max local +X, centered Y/Z
            mid_y = (parent_bbox.min_pt[1] + parent_bbox.max_pt[1]) / 2.0
            mid_z = (parent_bbox.min_pt[2] + parent_bbox.max_pt[2]) / 2.0
            tip_local_mm = (parent_bbox.max_pt[0], mid_y, mid_z)
            tip_local_m = (
                tip_local_mm[0] * _SCALE_MM_TO_M,
                tip_local_mm[1] * _SCALE_MM_TO_M,
                tip_local_mm[2] * _SCALE_MM_TO_M,
            )
            tip_world = _transform_point(parent_tf, tip_local_m)

            # Child joint origin in world
            child = jd["child"]
            if child in link_world_tf:
                child_origin = (
                    link_world_tf[child][0][3],
                    link_world_tf[child][1][3],
                    link_world_tf[child][2][3],
                )
                gap = math.sqrt(sum(
                    (tip_world[i] - child_origin[i]) ** 2 for i in range(3)
                ))
                if gap > _GAP_TOLERANCE_M:
                    findings.append(Finding(
                        rule_id="urdf.fk.link_chain_gap",
                        severity=Severity.BLOCK,
                        message=(
                            f"Gap {gap * 1000:.1f}mm between '{parent}' mesh tip "
                            f"and '{child}' joint origin (max 5mm)"
                        ),
                        field=f"joint/{jname}",
                    ))

    # --- Check 3: urdf.fk.tibia_tip_height ---
    # Tibia tips should be between ground (z=0) and chassis height at zero angles.
    if mesh_bboxes:
        for lname in link_names:
            if "tibia" not in lname.lower():
                continue
            if lname not in mesh_bboxes or lname not in link_world_tf:
                continue
            tibia_bbox = mesh_bboxes[lname]
            tibia_tf = link_world_tf[lname]

            # Tibia tip: max local +X
            mid_y = (tibia_bbox.min_pt[1] + tibia_bbox.max_pt[1]) / 2.0
            mid_z = (tibia_bbox.min_pt[2] + tibia_bbox.max_pt[2]) / 2.0
            tip_local_m = (
                tibia_bbox.max_pt[0] * 0.001,
                mid_y * 0.001,
                mid_z * 0.001,
            )
            tip_world = _transform_point(tibia_tf, tip_local_m)
            tip_z = tip_world[2]

            chassis_max_z = ground_clearance_m if ground_clearance_m else 0.5
            if tip_z < -0.010 or tip_z > chassis_max_z + 0.050:
                findings.append(Finding(
                    rule_id="urdf.fk.tibia_tip_height",
                    severity=Severity.WARN,
                    message=(
                        f"Tibia '{lname}' tip at Z={tip_z:.4f}m "
                        f"(expected between 0 and {chassis_max_z:.3f}m)"
                    ),
                    field=f"link/{lname}",
                ))

    # --- Check 4: urdf.fk.coxa_yaw_matches_radial ---
    # Coxa joints attached to chassis should have RPY yaw ≈ atan2(origin_y, origin_x).
    for jname, jd in joint_data.items():
        if "coxa" not in jname.lower():
            continue
        origin = jd["xyz"]
        # Only check if this is a root-attached joint (parent is root or chassis)
        parent = jd["parent"]
        if parent not in root_links:
            # Check if parent is attached to root via fixed joint
            parent_joint_name = child_to_joint.get(parent)
            if parent_joint_name is None:
                continue
            pjd = joint_data.get(parent_joint_name, {})
            if pjd.get("type") != "fixed" or pjd.get("parent") not in root_links:
                continue

        # Compute the world-frame position of this joint
        if parent in link_world_tf:
            parent_tf = link_world_tf[parent]
            joint_world = _transform_point(parent_tf, origin)
            expected_yaw = math.atan2(joint_world[1], joint_world[0])

            # The joint rpy yaw is in the URDF
            actual_yaw = jd["rpy"][2]
            yaw_diff = abs(actual_yaw - expected_yaw)
            # Handle wraparound
            if yaw_diff > math.pi:
                yaw_diff = 2 * math.pi - yaw_diff
            if yaw_diff > 0.02:  # ~1.1 degrees
                findings.append(Finding(
                    rule_id="urdf.fk.coxa_yaw_matches_radial",
                    severity=Severity.WARN,
                    message=(
                        f"Coxa joint '{jname}' yaw={actual_yaw:.4f} rad, "
                        f"expected atan2(y,x)={expected_yaw:.4f} rad "
                        f"(diff {yaw_diff:.4f} rad)"
                    ),
                    field=f"joint/{jname}",
                ))

    # --- Check 5: urdf.fk.pitch_axis_local ---
    # Femur/tibia joint axes should be ≈ (0, ±1, 0) in local frame.
    for jname, jd in joint_data.items():
        name_lower = jname.lower()
        if "femur" not in name_lower and "tibia" not in name_lower:
            continue
        if jd["type"] != "revolute":
            continue
        axis = jd["axis"]
        # Check if axis is approximately (0, ±1, 0)
        dot_y = abs(axis[1])
        if dot_y < 0.99:
            findings.append(Finding(
                rule_id="urdf.fk.pitch_axis_local",
                severity=Severity.WARN,
                message=(
                    f"Joint '{jname}' axis ({axis[0]:.3f}, {axis[1]:.3f}, "
                    f"{axis[2]:.3f}) not aligned with local Y (dot={dot_y:.4f}, "
                    f"expected >0.99)"
                ),
                field=f"joint/{jname}/axis",
            ))

    # --- Check 6: urdf.fk.bilateral_symmetry ---
    # Collect leg joint world positions; left legs (y>0) should mirror right (y<0).
    leg_positions: dict[str, tuple[float, float, float]] = {}
    for jname, jd in joint_data.items():
        if "coxa" not in jname.lower():
            continue
        child = jd["child"]
        if child in link_world_tf:
            tf = link_world_tf[child]
            leg_positions[jname] = (tf[0][3], tf[1][3], tf[2][3])

    if len(leg_positions) >= 4:
        left_legs = {n: p for n, p in leg_positions.items() if p[1] > 0.001}
        right_legs = {n: p for n, p in leg_positions.items() if p[1] < -0.001}

        if left_legs and right_legs and len(left_legs) == len(right_legs):
            left_sorted = sorted(left_legs.values(), key=lambda p: (p[0], p[1]))
            right_sorted = sorted(right_legs.values(), key=lambda p: (p[0], -p[1]))

            for lp, rp in zip(left_sorted, right_sorted):
                dx = abs(lp[0] - rp[0])
                dy = abs(lp[1] - (-rp[1]))  # mirror about XZ
                dz = abs(lp[2] - rp[2])
                max_diff_mm = max(dx, dy, dz) * 1000
                if max_diff_mm > 1.0:
                    findings.append(Finding(
                        rule_id="urdf.fk.bilateral_symmetry",
                        severity=Severity.WARN,
                        message=(
                            f"Bilateral asymmetry: left ({lp[0]:.4f}, {lp[1]:.4f}, "
                            f"{lp[2]:.4f}) vs mirrored right ({rp[0]:.4f}, "
                            f"{-rp[1]:.4f}, {rp[2]:.4f}), max diff {max_diff_mm:.1f}mm"
                        ),
                    ))

    # --- Check 7: urdf.fk.no_mesh_overlap ---
    # Non-parent-child link world AABBs shouldn't overlap > 10% of smaller volume.
    if mesh_bboxes:
        _SCALE = 0.001
        link_world_aabb: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {}
        for lname in link_names:
            if lname in mesh_bboxes and lname in link_world_tf:
                lo, hi = _transform_bbox(link_world_tf[lname], mesh_bboxes[lname], _SCALE)
                link_world_aabb[lname] = (lo, hi)

        # Build parent-child set for exclusion
        parent_child_pairs: set[frozenset[str]] = set()
        for jd in joint_data.values():
            parent_child_pairs.add(frozenset({jd["parent"], jd["child"]}))

        aabb_names = sorted(link_world_aabb.keys())
        for i in range(len(aabb_names)):
            for j in range(i + 1, len(aabb_names)):
                n1, n2 = aabb_names[i], aabb_names[j]
                if frozenset({n1, n2}) in parent_child_pairs:
                    continue
                lo1, hi1 = link_world_aabb[n1]
                lo2, hi2 = link_world_aabb[n2]
                overlap = _aabb_overlap_volume(lo1, hi1, lo2, hi2)
                if overlap <= 0:
                    continue
                v1 = _aabb_volume(lo1, hi1)
                v2 = _aabb_volume(lo2, hi2)
                smaller = min(v1, v2)
                if smaller > 0 and overlap / smaller > 0.10:
                    findings.append(Finding(
                        rule_id="urdf.fk.no_mesh_overlap",
                        severity=Severity.WARN,
                        message=(
                            f"Links '{n1}' and '{n2}' AABBs overlap "
                            f"{overlap / smaller * 100:.0f}% of smaller volume"
                        ),
                    ))

    return findings
