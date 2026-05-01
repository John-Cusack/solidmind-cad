"""Simulation description export — format-agnostic intermediate representation.

Transforms a ``Mechanism`` (kinematic graph) + body mesh manifest into a
``SimModel`` (links + joints), then serializes to URDF/SDF.

The key design is separation of concerns:
- ``build_sim_model()`` assembles the kinematic tree from mechanism + manifest.
- ``write_urdf()`` serializes a ``SimModel`` to URDF XML (stdlib xml.etree).
- ``write_sdf()`` serializes a ``SimModel`` to SDF XML.
- Future format writers (``write_usd``, ``write_mjcf``) share the same
  ``build_sim_model()`` logic.
"""
from __future__ import annotations

import logging
import math
import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from server.models import Finding, Severity
from server.motion_models import JointType, Mechanism

# Default density for mass estimation when only volume is available (kg/m^3).
# PLA plastic — conservative for simulation stability.
_DEFAULT_DENSITY_KG_M3 = 1250.0


@dataclass(frozen=True, slots=True)
class CollisionShape:
    """Primitive collision geometry for a SimLink.

    When set on a :class:`SimLink`, ``write_sdf`` emits a primitive
    ``<collision>`` block (``<box>`` or ``<cylinder>``) instead of the
    mesh-based collision that gets reused from the visual.

    Mesh-based collision is the default for legacy paths that don't
    set this field, but it triggers DART/ODE crashes in Gazebo Harmonic
    on complex shapes (e.g. multi-blade propellers).  Drone exports
    via :class:`server.airframes.MulticopterAirframe` always populate
    a primitive shape here.

    All dimensions are in metres.  Only one of (``size_m``,
    ``radius_m``+``length_m``) is required depending on ``kind``.
    """

    kind: str   # "box" | "cylinder"
    size_m: tuple[float, float, float] | None = None
    radius_m: float | None = None
    length_m: float | None = None


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
    visual_origin_xyz: tuple[float, float, float] | None = None  # mesh offset from link frame (meters)
    collision_shape: CollisionShape | None = None
    """Optional primitive collision geometry.  When set, ``write_sdf`` emits
    a ``<box>`` or ``<cylinder>`` collision instead of reusing the visual
    mesh — avoids DART/ODE crashes on complex shapes (multi-blade props,
    high-triangle-count chassis meshes, etc.)."""

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
# CONTINUOUS added conditionally for backward compat with running servers
# that haven't reloaded motion_models yet.
if hasattr(JointType, "CONTINUOUS"):
    _JOINT_TYPE_MAP[JointType.CONTINUOUS] = "continuous"


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


def _quat_rotate_point(
    w: float, x: float, y: float, z: float,
    px: float, py: float, pz: float,
) -> tuple[float, float, float]:
    """Rotate a 3D point by a unit quaternion using the optimized formula.

    v' = v + 2w(q_vec × v) + 2(q_vec × (q_vec × v))
    where q_vec = (x, y, z), v = (px, py, pz).
    """
    # Cross 1: q_vec × v
    c1x = y * pz - z * py
    c1y = z * px - x * pz
    c1z = x * py - y * px
    # Cross 2: q_vec × c1
    c2x = y * c1z - z * c1y
    c2y = z * c1x - x * c1z
    c2z = x * c1y - y * c1x
    return (
        px + 2.0 * (w * c1x + c2x),
        py + 2.0 * (w * c1y + c2y),
        pz + 2.0 * (w * c1z + c2z),
    )


def _quat_from_yaw(yaw_rad: float) -> tuple[float, float, float, float]:
    """Create a quaternion (w,x,y,z) from a pure Z-axis rotation."""
    half = yaw_rad * 0.5
    return (math.cos(half), 0.0, 0.0, math.sin(half))


# ---------------------------------------------------------------------------
# Inertia helpers
# ---------------------------------------------------------------------------

def _box_inertia(
    mass_kg: float,
    dx_m: float, dy_m: float, dz_m: float,
) -> tuple[float, float, float, float, float, float]:
    """Compute box inertia tensor as a 6-tuple, in URDF/SDF order.

    Thin wrapper that delegates to :func:`server.inertia.box_inertia`
    so the formula has a single source of truth.  Kept around for
    backwards compatibility with callers that work in tuple-of-floats
    form (the legacy hexapod path through :func:`build_sim_model`).
    New code should prefer :class:`server.inertia.Inertia6` directly.
    """
    from server.inertia import box_inertia as _box_inertia_fn
    return _box_inertia_fn(mass_kg, dx_m, dy_m, dz_m).as_tuple()


logger = logging.getLogger("solidmind.sim_export")


def _is_binary_stl(stl_path: str) -> bool:
    """Detect whether an STL file is binary (vs ASCII) format.

    Binary STL: 80-byte header + 4-byte uint32 triangle count + 50 bytes per
    triangle.  ASCII STL starts with ``solid <name>`` and contains readable text.
    """
    try:
        with open(stl_path, "rb") as f:
            header = f.read(80)
            if len(header) < 80:
                return False
            count_bytes = f.read(4)
            if len(count_bytes) < 4:
                return False
            tri_count = struct.unpack("<I", count_bytes)[0]
            expected_size = 84 + tri_count * 50
            f.seek(0, 2)
            actual_size = f.tell()
            return actual_size == expected_size
    except OSError:
        return False


def _transform_stl_to_link_local(
    stl_path: str,
    world_pos_mm: tuple[float, float, float],
    world_quat: tuple[float, float, float, float],
) -> None:
    """Transform STL vertices from world coordinates to link-local.

    Applies the inverse of the link's world transform: translate by -world_pos,
    then rotate by inverse(world_quat).  Normals are rotated but not translated.

    Supports both ASCII and binary STL formats.

    Args:
        stl_path: Path to the STL file (modified in-place).
        world_pos_mm: Link world position in mm (x, y, z).
        world_quat: Link world orientation quaternion (w, x, y, z).
    """
    # Early return if identity transform
    if (abs(world_pos_mm[0]) < 1e-6
            and abs(world_pos_mm[1]) < 1e-6
            and abs(world_pos_mm[2]) < 1e-6
            and abs(world_quat[0] - 1.0) < 1e-9
            and abs(world_quat[1]) < 1e-9
            and abs(world_quat[2]) < 1e-9
            and abs(world_quat[3]) < 1e-9):
        return  # Already at origin with identity rotation — nothing to do

    inv_w, inv_x, inv_y, inv_z = _quat_inverse(*world_quat)
    px, py, pz = world_pos_mm

    if _is_binary_stl(stl_path):
        _transform_binary_stl(stl_path, px, py, pz, inv_w, inv_x, inv_y, inv_z)
    else:
        _transform_ascii_stl(stl_path, px, py, pz, inv_w, inv_x, inv_y, inv_z)

    logger.debug(
        "Transformed %s to link-local: translate=(%.1f, %.1f, %.1f) quat=(%s)",
        stl_path, -px, -py, -pz,
        ", ".join(f"{v:.4f}" for v in world_quat),
    )


# Minimum extent threshold (mm) below which a mesh axis is considered flat.
_MIN_MESH_EXTENT_MM = 0.1


def _get_stl_extent(stl_path: str) -> tuple[int, tuple[float, float, float], tuple[float, float, float]] | None:
    """Read an STL and return (triangle_count, min_vertex, max_vertex).

    Returns ``None`` if the file cannot be read or has no triangles.
    Works with both ASCII and binary STL formats.
    """
    try:
        if _is_binary_stl(stl_path):
            return _get_binary_stl_extent(stl_path)
        return _get_ascii_stl_extent(stl_path)
    except (OSError, struct.error, ValueError) as exc:
        logger.debug("Could not read STL extent for %s: %s", stl_path, exc)
        return None


def _get_binary_stl_extent(stl_path: str) -> tuple[int, tuple[float, float, float], tuple[float, float, float]] | None:
    with open(stl_path, "rb") as f:
        f.seek(80)
        count_bytes = f.read(4)
        if len(count_bytes) < 4:
            return None
        tri_count = struct.unpack("<I", count_bytes)[0]
        if tri_count == 0:
            return None
        data = f.read()

    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    for i in range(tri_count):
        offset = i * 50
        # Skip normal (12 bytes), read 3 vertices
        for v in range(3):
            v_offset = offset + 12 + v * 12
            vx, vy, vz = struct.unpack_from("<3f", data, v_offset)
            xmin, xmax = min(xmin, vx), max(xmax, vx)
            ymin, ymax = min(ymin, vy), max(ymax, vy)
            zmin, zmax = min(zmin, vz), max(zmax, vz)
    return tri_count, (xmin, ymin, zmin), (xmax, ymax, zmax)


def _get_ascii_stl_extent(stl_path: str) -> tuple[int, tuple[float, float, float], tuple[float, float, float]] | None:
    _VERTEX_RE_SIMPLE = re.compile(
        r"^\s*vertex\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    )
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    vertex_count = 0
    with open(stl_path, "r") as f:
        for line in f:
            m = _VERTEX_RE_SIMPLE.match(line)
            if m:
                vx, vy, vz = float(m.group(1)), float(m.group(2)), float(m.group(3))
                xmin, xmax = min(xmin, vx), max(xmax, vx)
                ymin, ymax = min(ymin, vy), max(ymax, vy)
                zmin, zmax = min(zmin, vz), max(zmax, vz)
                vertex_count += 1
    if vertex_count == 0:
        return None
    tri_count = vertex_count // 3
    return tri_count, (xmin, ymin, zmin), (xmax, ymax, zmax)


def validate_stl_extent(stl_path: str, link_name: str = "") -> list[Finding]:
    """Check that an STL mesh has non-zero 3D extent (not flat/degenerate).

    Returns findings with:
    - BLOCK if the mesh has zero triangles
    - WARN if any axis extent is below ``_MIN_MESH_EXTENT_MM``
    """
    findings: list[Finding] = []
    label = f"'{link_name}' " if link_name else ""

    extent_info = _get_stl_extent(stl_path)
    if extent_info is None:
        findings.append(Finding(
            rule_id="stl.no_triangles",
            severity=Severity.WARN,
            message=f"Mesh {label}({os.path.basename(stl_path)}) has no readable triangles.",
            field=f"mesh.{link_name}" if link_name else "mesh",
        ))
        return findings

    tri_count, (xmin, ymin, zmin), (xmax, ymax, zmax) = extent_info
    dx = xmax - xmin
    dy = ymax - ymin
    dz = zmax - zmin

    if tri_count == 0:
        findings.append(Finding(
            rule_id="stl.no_triangles",
            severity=Severity.WARN,
            message=f"Mesh {label}({os.path.basename(stl_path)}) has 0 triangles.",
            field=f"mesh.{link_name}" if link_name else "mesh",
        ))
        return findings

    flat_axes: list[str] = []
    if dx < _MIN_MESH_EXTENT_MM:
        flat_axes.append(f"X={dx:.4f}")
    if dy < _MIN_MESH_EXTENT_MM:
        flat_axes.append(f"Y={dy:.4f}")
    if dz < _MIN_MESH_EXTENT_MM:
        flat_axes.append(f"Z={dz:.4f}")

    if flat_axes:
        findings.append(Finding(
            rule_id="stl.flat_mesh",
            severity=Severity.WARN,
            message=(
                f"Mesh {label}({os.path.basename(stl_path)}) is flat/degenerate: "
                f"extent below {_MIN_MESH_EXTENT_MM}mm on {', '.join(flat_axes)}. "
                f"Full extent: ({dx:.2f}, {dy:.2f}, {dz:.2f}) mm, "
                f"{tri_count} triangles."
            ),
            field=f"mesh.{link_name}" if link_name else "mesh",
        ))

    logger.debug(
        "STL extent %s: triangles=%d, extent=(%.2f, %.2f, %.2f) mm",
        os.path.basename(stl_path), tri_count, dx, dy, dz,
    )
    return findings


def _transform_ascii_stl(
    stl_path: str,
    px: float, py: float, pz: float,
    inv_w: float, inv_x: float, inv_y: float, inv_z: float,
) -> None:
    """Transform an ASCII STL file's vertices and normals in-place."""
    _VERTEX_RE = re.compile(
        r"^(\s*vertex\s+)"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
        r"(\s*)$"
    )
    _NORMAL_RE = re.compile(
        r"^(\s*facet\s+normal\s+)"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
        r"(\s*)$"
    )

    with open(stl_path, "r") as f:
        lines = f.readlines()

    out_lines: list[str] = []
    for line in lines:
        vm = _VERTEX_RE.match(line)
        if vm:
            x, y, z = float(vm.group(2)), float(vm.group(3)), float(vm.group(4))
            # Translate then rotate by inverse quaternion
            tx, ty, tz = x - px, y - py, z - pz
            lx, ly, lz = _quat_rotate_point(inv_w, inv_x, inv_y, inv_z, tx, ty, tz)
            out_lines.append(f"{vm.group(1)}{lx:.6f} {ly:.6f} {lz:.6f}{vm.group(5)}")
            continue

        nm = _NORMAL_RE.match(line)
        if nm:
            nx, ny, nz = float(nm.group(2)), float(nm.group(3)), float(nm.group(4))
            # Rotate only (normals are direction vectors)
            lnx, lny, lnz = _quat_rotate_point(inv_w, inv_x, inv_y, inv_z, nx, ny, nz)
            out_lines.append(f"{nm.group(1)}{lnx:.6f} {lny:.6f} {lnz:.6f}{nm.group(5)}")
            continue

        out_lines.append(line)

    with open(stl_path, "w") as f:
        f.writelines(out_lines)


def _transform_binary_stl(
    stl_path: str,
    px: float, py: float, pz: float,
    inv_w: float, inv_x: float, inv_y: float, inv_z: float,
) -> None:
    """Transform a binary STL file's vertices and normals in-place."""
    with open(stl_path, "rb") as f:
        header = f.read(80)
        count_bytes = f.read(4)
        tri_count = struct.unpack("<I", count_bytes)[0]
        data = bytearray(f.read())

    for i in range(tri_count):
        offset = i * 50
        # Normal (3 × float32 = 12 bytes)
        nx, ny, nz = struct.unpack_from("<3f", data, offset)
        rnx, rny, rnz = _quat_rotate_point(inv_w, inv_x, inv_y, inv_z, nx, ny, nz)
        struct.pack_into("<3f", data, offset, rnx, rny, rnz)
        # 3 vertices (3 × 3 × float32 = 36 bytes)
        for v in range(3):
            v_offset = offset + 12 + v * 12
            vx, vy, vz = struct.unpack_from("<3f", data, v_offset)
            tx, ty, tz = vx - px, vy - py, vz - pz
            lx, ly, lz = _quat_rotate_point(inv_w, inv_x, inv_y, inv_z, tx, ty, tz)
            struct.pack_into("<3f", data, v_offset, lx, ly, lz)

    with open(stl_path, "wb") as f:
        f.write(header)
        f.write(count_bytes)
        f.write(data)


def build_sim_model(
    mechanism: Mechanism,
    body_manifest: list[dict[str, Any]],
    *,
    ground_clearance_m: float | None = None,
    mesh_transform_error_mode: str = "warn",
    require_explicit_joint_origins: bool = False,
    mesh_findings: list[Finding] | None = None,
    drone_config: dict[str, Any] | None = None,
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

    Note on rest-pose angles: If a mechanism joint has a nonzero rest angle
    (e.g. a hexapod leg at its neutral stance pitch), this should be tracked
    via ``initial_joint_positions`` in the Isaac ``URDFImportConfig`` — NOT
    baked into the joint RPY.  The joint RPY encodes only the frame-to-frame
    rotation between parent and child links at the zero-angle configuration.
    """
    # Index manifest by body name for O(1) lookup (case-insensitive keys)
    # Also index by label (FreeCAD Label vs internal Name) and by
    # stripped variants: "Body_Chassis" → "chassis", "Body Chassis" → "chassis"
    manifest_by_name: dict[str, dict[str, Any]] = {}
    for entry in body_manifest:
        key = entry["name"].lower()
        manifest_by_name[key] = entry
        # Also index by label if present
        if "label" in entry:
            manifest_by_name[entry["label"].lower()] = entry
        # Strip common FreeCAD prefixes: "Body_", "Body ", "Body"
        for prefix in ("body_", "body ", "body"):
            if key.startswith(prefix) and len(key) > len(prefix):
                manifest_by_name[key[len(prefix):]] = entry

    # Build links from mechanism parts
    links: list[SimLink] = []
    part_to_link: dict[str, str] = {}  # part_id -> link_name
    # Track placements for joint RPY computation (Bug 2)
    link_placement: dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]] = {}

    for part in mechanism.parts:
        link_name = part.id

        # Find mesh from manifest — try body_name, part id, and stripped
        # variants (case-insensitive).  FreeCAD names like "Body_Chassis"
        # should match mechanism part ids like "chassis".
        manifest_entry = (
            manifest_by_name.get((part.body_name or "").lower())
            or manifest_by_name.get(part.id.lower())
        )

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

    # ── Fallback: assign unmapped manifest meshes to ground link ─────
    # When the ground part's id/body_name doesn't match any manifest
    # entry (common with FreeCAD body names like "Body_Chassis" vs
    # mechanism part id "chassis"), find the unmatched mesh and attach it.
    mapped_names = set()
    for part in mechanism.parts:
        key1 = (part.body_name or "").lower()
        key2 = part.id.lower()
        if key1 in manifest_by_name:
            mapped_names.add(key1)
        if key2 in manifest_by_name:
            mapped_names.add(key2)
    unmapped = [e for k, e in manifest_by_name.items() if k not in mapped_names]

    for link in links:
        if link.is_root and link.mesh_path is None and unmapped:
            # Try to find the chassis/body mesh among unmapped entries
            best = None
            for entry in unmapped:
                name_lower = entry["name"].lower()
                if "chassis" in name_lower or "body" in name_lower or "base" in name_lower:
                    best = entry
                    break
            if best is None:
                best = unmapped[0]  # last resort: first unmapped mesh
            link.mesh_path = best.get("mesh_path")
            plc = best.get("placement", {})
            pos = plc.get("position", [0.0, 0.0, 0.0])
            link.position = (pos[0], pos[1], pos[2])
            quat = plc.get("rotation_quat", [1.0, 0.0, 0.0, 0.0])
            link.rotation_quat = (quat[0], quat[1], quat[2], quat[3])
            link_placement[link.name] = (link.position, link.rotation_quat)
            # Compute inertia from bbox if available
            bbox_mm = best.get("bbox_mm")
            volume_mm3 = best.get("volume_mm3")
            if link.inertia is None and bbox_mm is not None and len(bbox_mm) == 3:
                mass = link.mass_kg
                if mass is None and volume_mm3 is not None and volume_mm3 > 0:
                    mass = (volume_mm3 * 1e-9) * _DEFAULT_DENSITY_KG_M3
                    link.mass_kg = mass
                if mass is not None and mass > 0:
                    dx_m = bbox_mm[0] / 1000.0
                    dy_m = bbox_mm[1] / 1000.0
                    dz_m = bbox_mm[2] / 1000.0
                    link.inertia = _box_inertia(mass, dx_m, dy_m, dz_m)

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
    # Build manifest placement lookup for fallback joint origins.
    # When the mechanism doesn't specify joint origins (all zeros), we use
    # the child body's FreeCAD Placement position as the joint's world-frame
    # origin.  build_sim_model transforms meshes to link-local coordinates
    # after computing world positions, so the joint origin correctly
    # re-positions the child frame in world space.
    manifest_pos_by_part: dict[str, tuple[float, float, float]] = {}
    for part in mechanism.parts:
        m_entry = manifest_by_name.get((part.body_name or "").lower()) or manifest_by_name.get(part.id.lower())
        if m_entry is not None:
            plc = m_entry.get("placement", {})
            pos = plc.get("position", [0.0, 0.0, 0.0])
            manifest_pos_by_part[part.id] = (pos[0], pos[1], pos[2])

    _IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)

    part_world_pos: dict[str, tuple[float, float, float]] = {}
    part_world_quat: dict[str, tuple[float, float, float, float]] = {}
    ground_parts: set[str] = set()
    for part in mechanism.parts:
        if part.is_ground:
            # Use manifest position when available so child offsets are
            # relative to the actual body position (not world origin).
            ground_pos = manifest_pos_by_part.get(part.id, (0.0, 0.0, 0.0))
            part_world_pos[part.id] = ground_pos
            part_world_quat[part.id] = _IDENTITY_QUAT
            ground_parts.add(part.id)

    # Build a set of part IDs that are explicitly-placed rotors so the
    # BFS can skip the radial auto-yaw heuristic for them.  The auto-yaw
    # is meant for hexapod legs (whose body meshes extend along +X);
    # rotors are point-symmetric about Z and don't need it.  The check
    # is opt-in via the optional ``drone_config["rotors"][i]["joint"]``
    # field — when present, the joint's child_part is treated as
    # already-placed.
    rotor_part_ids: set[str] = set()
    if drone_config is not None:
        rotor_entries = drone_config.get("rotors") or []
        joint_to_child = {j.id: j.child_part for j in mechanism.joints}
        for entry in rotor_entries:
            if not isinstance(entry, dict):
                continue
            joint_name = entry.get("joint")
            if not joint_name:
                continue
            child_id = joint_to_child.get(str(joint_name))
            if child_id:
                rotor_part_ids.add(child_id)

    # BFS / iterative pass — compute world pos + orientation quaternion for
    # every reachable part.  For root-attached joints: auto-compute outward
    # yaw to orient child +X radially outward.  For deeper joints: inherit
    # parent quaternion (no extra rotation).  Using full quaternions instead
    # of yaw-only allows correct transforms for non-planar robots (arms,
    # tilted brackets) while producing identical results for planar robots
    # (hexapods, wheeled bots) where all rotations are pure-Z.
    joint_added_quat: dict[str, tuple[float, float, float, float]] = {}
    remaining = list(mechanism.joints)
    max_iters = len(remaining) + 1
    for _ in range(max_iters):
        still_remaining: list[Any] = []
        for jedge in remaining:
            if jedge.parent_part in part_world_pos:
                # ``JointEdge.origin`` is in MILLIMETRES, matching FreeCAD's
                # native unit and the rest of the manifest (``placement.position``,
                # ``bbox_mm``, ...).  ``SimJoint.origin_xyz`` is in METRES (URDF/SDF
                # convention) — converted via ``/ 1000`` further below.  The
                # conversion happens at the boundary between FreeCAD-side data
                # (mm) and URDF/SDF-side data (m); it does NOT happen on
                # ``part_world_pos``.
                origin = tuple(jedge.origin)
                if abs(origin[0]) < 1e-3 and abs(origin[1]) < 1e-3 and abs(origin[2]) < 1e-3:
                    fallback = manifest_pos_by_part.get(jedge.child_part)
                    if fallback is not None:
                        if require_explicit_joint_origins:
                            logger.warning(
                                "Joint '%s': origin is (0,0,0), using manifest "
                                "fallback for child '%s'. Set explicit origins "
                                "for production use.",
                                jedge.id, jedge.child_part,
                            )
                        origin = fallback

                # Place child at the joint's world origin (in mm)
                part_world_pos[jedge.child_part] = origin  # type: ignore[assignment]

                parent_quat = part_world_quat.get(jedge.parent_part, _IDENTITY_QUAT)

                # Auto-compute outward yaw for root-attached joints.
                # Formula: atan2(dy, dx) orients child +X toward (dx, dy).
                # Body-local meshes extend along +X, so this aligns them
                # radially outward from the body center.  This is what
                # hexapod legs and quadruped legs need: each leg's body
                # mesh is the same template, oriented by the joint frame.
                #
                # SKIP auto-yaw for parts whose mesh is already placed
                # explicitly in world coordinates — drone rotors are the
                # canonical case.  These are identified through the
                # ``drone_config["rotors"]`` list (see ``rotor_part_ids``
                # built earlier), not via geometric heuristics: the
                # previous "manifest position ≈ joint origin → already
                # placed" rule misclassified hexapod legs whose manifest
                # was authored at the joint origin.
                added_quat = _IDENTITY_QUAT
                if (
                    jedge.parent_part in ground_parts
                    and jedge.child_part not in rotor_part_ids
                ):
                    ppos = part_world_pos[jedge.parent_part]
                    dx = origin[0] - ppos[0]
                    dy = origin[1] - ppos[1]
                    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                        added_yaw = math.atan2(dy, dx)
                        added_quat = _quat_from_yaw(added_yaw)

                joint_added_quat[jedge.id] = added_quat
                part_world_quat[jedge.child_part] = _quat_multiply(
                    *parent_quat, *added_quat,
                )
            else:
                still_remaining.append(jedge)
        if len(still_remaining) == len(remaining):
            break  # No progress — remaining joints have unreachable parents
        remaining = still_remaining

    # -----------------------------------------------------------------------
    # Second pass: compose manifest quaternion (pitch/roll from FreeCAD
    # Placement) with the BFS-computed quaternion (auto-yaw).  This handles
    # non-planar robots where bodies are tilted in the CAD model:
    #   link_world_quat = auto_yaw_quat * manifest_quat
    # For hexapods: auto_yaw(45°) * identity = yaw(45°)  (no regression)
    # For robot arms: identity * pitch(30°) = pitch(30°)  (correct)
    # -----------------------------------------------------------------------
    manifest_quat_by_part: dict[str, tuple[float, float, float, float]] = {}
    for part in mechanism.parts:
        m_entry = manifest_by_name.get((part.body_name or "").lower()) or manifest_by_name.get(part.id.lower())
        if m_entry is not None:
            plc = m_entry.get("placement", {})
            quat = plc.get("rotation_quat", [1.0, 0.0, 0.0, 0.0])
            manifest_quat_by_part[part.id] = (quat[0], quat[1], quat[2], quat[3])

    for pid in list(part_world_quat):
        if pid in ground_parts:
            continue
        mq = manifest_quat_by_part.get(pid, _IDENTITY_QUAT)
        # Only compose when the manifest quaternion has non-trivial
        # pitch/roll (x or y components).  Pure-yaw manifest quats
        # (only w,z non-zero) are already handled by the BFS auto-yaw
        # and must NOT be double-applied.
        if mq != _IDENTITY_QUAT and (abs(mq[1]) > 1e-6 or abs(mq[2]) > 1e-6):
            part_world_quat[pid] = _quat_multiply(
                *part_world_quat[pid], *mq,
            )
            logger.debug(
                "Composed manifest quat %s onto BFS quat for part '%s'",
                mq, pid,
            )

    # -----------------------------------------------------------------------
    # Topology optimization: chain co-located fixed + non-fixed siblings.
    #
    # When two joints share the same parent and the same (or nearly the same)
    # world-frame origin, and one is ``fixed`` while the other is not, the
    # non-fixed joint should go *through* the fixed joint's child rather than
    # being a direct sibling.  Example:
    #
    #   BEFORE (siblings):          AFTER (chained):
    #   frame ──fixed──▶ motor      frame ──fixed──▶ motor ──revolute──▶ prop
    #   frame ──revolute──▶ prop
    #
    # This matters for Isaac Sim's ``merge_fixed_joints=true``: fixed-joint
    # children are absorbed into their parent's articulation root.  When motor
    # and prop are siblings, merging the fixed motor joint leaves the prop
    # connected to frame directly — the motor mesh is orphaned.  Chaining
    # ensures the prop revolute joint originates from the (merged) motor link.
    # -----------------------------------------------------------------------
    _COLOC_THRESHOLD_MM = 5.0  # co-location distance threshold

    # Index joints by parent for efficient grouping
    _joints_by_parent: dict[str, list[Any]] = {}
    for jedge in mechanism.joints:
        _joints_by_parent.setdefault(jedge.parent_part, []).append(jedge)

    # Track reparented joints: joint_id -> new_parent_part
    _reparented: dict[str, str] = {}

    for parent_id, siblings in _joints_by_parent.items():
        if len(siblings) < 2:
            continue

        # Separate fixed and non-fixed joints
        fixed_joints = [j for j in siblings if j.joint_type == JointType.FIXED]
        non_fixed_joints = [j for j in siblings if j.joint_type != JointType.FIXED]

        if not fixed_joints or not non_fixed_joints:
            continue

        # For each non-fixed joint, check if there's a co-located fixed joint
        for nf_joint in non_fixed_joints:
            nf_pos = part_world_pos.get(nf_joint.child_part, (0.0, 0.0, 0.0))
            best_fixed = None
            best_dist = float("inf")

            for f_joint in fixed_joints:
                f_pos = part_world_pos.get(f_joint.child_part, (0.0, 0.0, 0.0))
                dist = math.sqrt(
                    (nf_pos[0] - f_pos[0]) ** 2
                    + (nf_pos[1] - f_pos[1]) ** 2
                    + (nf_pos[2] - f_pos[2]) ** 2
                )
                if dist < _COLOC_THRESHOLD_MM and dist < best_dist:
                    best_fixed = f_joint
                    best_dist = dist

            if best_fixed is not None:
                new_parent = best_fixed.child_part
                logger.info(
                    "Topology optimization: rechaining joint '%s' (%s) "
                    "from parent '%s' to '%s' (co-located with fixed "
                    "joint '%s', dist=%.2f mm)",
                    nf_joint.id, nf_joint.joint_type.value,
                    parent_id, new_parent,
                    best_fixed.id, best_dist,
                )
                _reparented[nf_joint.id] = new_parent

    # Build joints from mechanism joints
    joints: list[SimJoint] = []
    # Track joint names for mimic references
    joint_edge_to_name: dict[str, str] = {}
    # Parts that are children of fixed joints (e.g. servos)
    _fixed_joint_children: set[str] = {
        j.child_part for j in mechanism.joints if j.joint_type == JointType.FIXED
    }

    for jedge in mechanism.joints:
        joint_name = jedge.id
        joint_type = _JOINT_TYPE_MAP.get(jedge.joint_type, "fixed")
        # Use reparented parent if topology optimization rechained this joint
        effective_parent = _reparented.get(jedge.id, jedge.parent_part)
        parent_link = part_to_link.get(effective_parent, effective_parent)
        child_link = part_to_link.get(jedge.child_part, jedge.child_part)

        # Compute parent-relative joint origin (mm -> m for URDF).
        # The child's world position (from BFS, with manifest fallback) is the
        # joint's world-frame origin.  Subtract parent's world pos to get the
        # world-frame offset, then rotate into the parent's local frame using
        # the full inverse parent quaternion.
        child_world = part_world_pos.get(jedge.child_part, (0.0, 0.0, 0.0))
        parent_pos = part_world_pos.get(effective_parent, (0.0, 0.0, 0.0))
        world_dx = child_world[0] - parent_pos[0]
        world_dy = child_world[1] - parent_pos[1]
        world_dz = child_world[2] - parent_pos[2]

        parent_quat = part_world_quat.get(effective_parent, _IDENTITY_QUAT)
        parent_inv = _quat_inverse(*parent_quat)

        # Rotate world offset into parent's local frame using full quaternion
        local_dx, local_dy, local_dz = _quat_rotate_point(
            *parent_inv, world_dx, world_dy, world_dz,
        )

        origin_xyz = (local_dx / 1000.0, local_dy / 1000.0, local_dz / 1000.0)

        # Joint RPY: relative quaternion from parent frame to child frame.
        # q_relative = q_parent_inv * q_child → converted to RPY.
        # For hexapods with pure-yaw BFS, this produces (0, 0, yaw) for
        # root-attached joints and (0, 0, 0) for deeper joints — identical
        # to the previous yaw-only computation.  For non-planar robots it
        # correctly encodes pitch/roll frame tilts.
        child_quat = part_world_quat.get(jedge.child_part, _IDENTITY_QUAT)
        relative_quat = _quat_multiply(*parent_inv, *child_quat)
        origin_rpy_raw = _quat_to_rpy(*relative_quat)
        origin_rpy = tuple(
            0.0 if abs(v) < 1e-9 else v for v in origin_rpy_raw
        )

        # Limits — required for revolute and prismatic URDF joint types.
        # Use mechanism data when available, otherwise provide sensible defaults
        # so the SimJoint invariant (limits required) is always satisfied.
        limits: tuple[float, float] | None = None
        _continuous = getattr(JointType, "CONTINUOUS", None)
        if _continuous is not None and jedge.joint_type == _continuous:
            pass  # Continuous joints have no limits in URDF
        elif jedge.joint_type in (
            JointType.REVOLUTE, JointType.GEAR_MESH,
            JointType.BELT_CHAIN, JointType.CAM,
        ):
            if jedge.min_angle_deg is not None and jedge.max_angle_deg is not None:
                limits = (
                    math.radians(jedge.min_angle_deg),
                    math.radians(jedge.max_angle_deg),
                )
            else:
                # Default: ±90° — typical servo range.  Full rotation (±180°)
                # causes simulation instability and is rarely correct.
                limits = (-math.radians(90), math.radians(90))
        elif jedge.joint_type == JointType.PRISMATIC:
            if jedge.min_travel_mm is not None and jedge.max_travel_mm is not None:
                limits = (
                    jedge.min_travel_mm / 1000.0,
                    jedge.max_travel_mm / 1000.0,
                )
            else:
                # Default: 0 to 1m range
                limits = (0.0, 1.0)

        # Effort/velocity/damping/friction: JointEdge → DriveCondition → defaults
        drive = drives_by_joint.get(jedge.id)
        effort = jedge.effort_nm or (drive.torque_nm if drive and drive.torque_nm else 10.0)
        velocity = jedge.velocity_rad_s or (
            (drive.speed_rpm * 2.0 * math.pi / 60.0) if drive and drive.speed_rpm else 6.28
        )
        damping = jedge.damping if jedge.damping is not None else 0.1
        friction = jedge.friction if jedge.friction is not None else 0.0

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

        # Joint axis is specified in the child link's local frame directly.
        # No world-to-local rotation needed — Z-axis joints are invariant
        # under Z-rotation, and pitch joints (0,1,0) are already local.
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
            damping=damping,
            friction=friction,
        ))
        joint_edge_to_name[jedge.id] = joint_name

    # -----------------------------------------------------------------------
    # Transform meshes from world coordinates to link-local coordinates.
    # export_sim_package exports meshes in world coords (including Body
    # Placement).  Each link's world position and orientation quaternion are
    # known from the BFS above.  We transform each STL so vertices are
    # relative to the link frame — the joint <origin> elements handle
    # parent-to-child positioning.
    # -----------------------------------------------------------------------
    mesh_extent_findings: list[Finding] = []
    for link in links:
        if link.mesh_path is None:
            continue
        world_pos = part_world_pos.get(link.name, (0.0, 0.0, 0.0))
        world_quat = part_world_quat.get(link.name, _IDENTITY_QUAT)
        try:
            _transform_stl_to_link_local(link.mesh_path, world_pos, world_quat)
        except Exception as exc:
            if mesh_transform_error_mode == "fail":
                raise
            logger.warning(
                "Failed to transform mesh %s to link-local: %s",
                link.mesh_path, exc,
            )

        # Post-transform STL sanity: check 3D extent
        mesh_extent_findings.extend(validate_stl_extent(link.mesh_path, link.name))

    for finding in mesh_extent_findings:
        logger.warning("Mesh extent issue: %s", finding.message)
    if mesh_findings is not None:
        mesh_findings.extend(mesh_extent_findings)

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
            base_joint_name = f"base_to_{root_link.name}"
            joints.insert(0, SimJoint(
                name=base_joint_name,
                joint_type="fixed",
                parent="base_link",
                child=root_link.name,
                origin_xyz=(0.0, 0.0, ground_clearance_m),
            ))

            # No Z correction needed: child joint origins are already
            # computed as (child_world - parent_world) / 1000 — pure
            # relative deltas.  The base_to_frame joint handles the
            # ground clearance lift; child joints are unaffected.

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


def write_urdf(
    model: SimModel,
    output_path: str,
    *,
    base_dir: str | None = None,
    absolute_mesh_paths: bool = False,
) -> str:
    """Serialize a SimModel to URDF XML.

    Args:
        model: The SimModel to serialize.
        output_path: Path to write the URDF file.
        base_dir: When set, mesh filenames are written relative to this
            directory.  Makes the URDF portable across machines.
            Ignored when ``absolute_mesh_paths`` is True.
        absolute_mesh_paths: When True, mesh filenames are written as
            absolute paths.  Ensures Gazebo (and other simulators) can
            always resolve mesh files regardless of working directory.

    Returns the absolute path of the written file.
    """
    robot = ET.Element("robot", name=model.name)

    for link in model.links:
        link_el = ET.SubElement(robot, "link", name=link.name)

        if link.mesh_path is not None:
            mesh_filename = link.mesh_path
            if absolute_mesh_paths:
                mesh_filename = os.path.abspath(link.mesh_path)
            elif base_dir:
                mesh_filename = os.path.relpath(link.mesh_path, base_dir)

            visual = ET.SubElement(link_el, "visual")
            geometry = ET.SubElement(visual, "geometry")
            mesh = ET.SubElement(geometry, "mesh")
            mesh.set("filename", mesh_filename)
            mesh.set("scale", "0.001 0.001 0.001")  # FreeCAD mm -> URDF m

            collision = ET.SubElement(link_el, "collision")
            c_geometry = ET.SubElement(collision, "geometry")
            c_mesh = ET.SubElement(c_geometry, "mesh")
            c_mesh.set("filename", mesh_filename)
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

        # Dynamics (damping + friction) for revolute, continuous, and prismatic joints
        if joint.joint_type in ("revolute", "continuous", "prismatic"):
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
# SDF writer
# ---------------------------------------------------------------------------

def _pose_str(
    x: float,
    y: float,
    z: float,
    roll: float,
    pitch: float,
    yaw: float,
) -> str:
    return f"{_fmt(x)} {_fmt(y)} {_fmt(z)} {_fmt(roll)} {_fmt(pitch)} {_fmt(yaw)}"


def _find_root_link_name(model: SimModel) -> str:
    """Return the name of the kinematic-root link (no parent joint)."""
    children = {j.child for j in model.joints}
    for link in model.links:
        if link.name not in children:
            return link.name
    return model.links[0].name if model.links else ""


def _normalize_turning_direction(value: Any) -> str:
    """Accept 'ccw'/'cw' or +1/-1 and return the gz canonical string."""
    if value in ("ccw", "CCW", 1, "1", True):
        return "ccw"
    if value in ("cw", "CW", -1, "-1"):
        return "cw"
    return "ccw"


# Default motor parameters tuned to match Gazebo Harmonic's stock
# X3/X500 quadrotor — produces ~16 N total thrust at full throttle on
# a 4-rotor airframe.  Phase 4's airframe generator will override these
# with values derived from BEMT thrust curves per drone.
_DEFAULT_MOTOR_CONSTANT = 8.54858e-06
_DEFAULT_MOMENT_CONSTANT = 0.016
_DEFAULT_MAX_ROT_VELOCITY = 1000.0
_DEFAULT_TIME_CONSTANT_UP = 0.0125
_DEFAULT_TIME_CONSTANT_DOWN = 0.025


def _emit_multicopter_motor_plugin(
    model_el: ET.Element,
    *,
    model_name: str,
    joint_name: str,
    link_name: str,
    direction: str,
    actuator_number: int,
    motor_constant: float = _DEFAULT_MOTOR_CONSTANT,
    moment_constant: float = _DEFAULT_MOMENT_CONSTANT,
    max_rot_velocity: float = _DEFAULT_MAX_ROT_VELOCITY,
) -> None:
    """Emit one Gazebo-canonical MulticopterMotorModel plugin for a rotor.

    The plugin must be one-per-rotor (NOT one plugin with multiple <rotor>
    children).  This is the schema PX4 SITL and Gazebo Harmonic 10's
    ``gz-sim-multicopter-motor-model-system`` accept.  Each plugin reads
    ``velocity[actuator_number]`` from ``gz.msgs.Actuators`` published on
    ``/<model_name>/command/motor_speed`` and applies thrust to the
    ``link_name`` link via the ``joint_name`` revolute/continuous joint.
    """
    plugin = ET.SubElement(
        model_el, "plugin",
        filename="gz-sim-multicopter-motor-model-system",
        name="gz::sim::systems::MulticopterMotorModel",
    )
    # NOTE: deliberately no <robotNamespace> — gz-sim's MulticopterMotorModel
    # subscribes to /model/<model_name>/<commandSubTopic> by default, which
    # matches what PX4's gz_bridge publishes.  Setting robotNamespace breaks
    # the topic match between PX4's actuator output and the motor plugin.
    ET.SubElement(plugin, "jointName").text = joint_name
    ET.SubElement(plugin, "linkName").text = link_name
    ET.SubElement(plugin, "turningDirection").text = direction
    ET.SubElement(plugin, "timeConstantUp").text = _fmt(_DEFAULT_TIME_CONSTANT_UP)
    ET.SubElement(plugin, "timeConstantDown").text = _fmt(_DEFAULT_TIME_CONSTANT_DOWN)
    ET.SubElement(plugin, "maxRotVelocity").text = _fmt(max_rot_velocity)
    ET.SubElement(plugin, "motorConstant").text = _fmt(motor_constant)
    ET.SubElement(plugin, "momentConstant").text = _fmt(moment_constant)
    ET.SubElement(plugin, "commandSubTopic").text = "command/motor_speed"
    # gz-sim Multicopter plugin uses <motorNumber>, not <actuator_number>
    ET.SubElement(plugin, "motorNumber").text = str(actuator_number)
    ET.SubElement(plugin, "rotorDragCoefficient").text = "8.06428e-05"
    ET.SubElement(plugin, "rollingMomentCoefficient").text = "1e-06"
    ET.SubElement(plugin, "motorSpeedPubTopic").text = f"motor_speed/{actuator_number}"
    ET.SubElement(plugin, "rotorVelocitySlowdownSim").text = "10"
    # Required: PX4's gz_bridge publishes velocity (rad/s) commands
    ET.SubElement(plugin, "motorType").text = "velocity"


def _emit_mesh_collision(link_el: ET.Element, link_name: str, mesh_uri: str) -> None:
    """Emit a ``<collision>`` block referencing a mesh (legacy default).

    Used for hexapod legs and any other path that hasn't migrated to
    explicit primitive collisions.  Mesh collision can crash Gazebo
    Harmonic's DART/ODE physics on complex shapes, so prefer
    :func:`_emit_primitive_collision` where the link has a known
    primitive shape (drone rotors, chassis boxes, etc.).
    """
    coll = ET.SubElement(link_el, "collision", name=f"{link_name}_collision")
    ET.SubElement(coll, "pose").text = "0 0 0 0 0 0"
    geom = ET.SubElement(coll, "geometry")
    mesh = ET.SubElement(geom, "mesh")
    ET.SubElement(mesh, "uri").text = mesh_uri
    ET.SubElement(mesh, "scale").text = "0.001 0.001 0.001"


def _emit_primitive_collision(
    link_el: ET.Element,
    link_name: str,
    shape: "CollisionShape",
) -> None:
    """Emit a ``<collision>`` block with primitive geometry.

    Box: requires ``size_m = (dx, dy, dz)``.
    Cylinder: requires ``radius_m`` and ``length_m``.

    Primitive collision is what Gazebo's DART/ODE physics handles
    cleanly — mesh collision on multi-blade propellers, for example,
    causes intermittent abort()s during contact resolution.
    """
    coll = ET.SubElement(link_el, "collision", name=f"{link_name}_collision")
    ET.SubElement(coll, "pose").text = "0 0 0 0 0 0"
    geom = ET.SubElement(coll, "geometry")
    if shape.kind == "box":
        if shape.size_m is None:
            raise ValueError(
                f"CollisionShape kind='box' requires size_m on link {link_name!r}"
            )
        box = ET.SubElement(geom, "box")
        dx, dy, dz = shape.size_m
        ET.SubElement(box, "size").text = f"{dx} {dy} {dz}"
    elif shape.kind == "cylinder":
        if shape.radius_m is None or shape.length_m is None:
            raise ValueError(
                f"CollisionShape kind='cylinder' requires radius_m + length_m "
                f"on link {link_name!r}"
            )
        cyl = ET.SubElement(geom, "cylinder")
        ET.SubElement(cyl, "radius").text = _fmt(shape.radius_m)
        ET.SubElement(cyl, "length").text = _fmt(shape.length_m)
    else:
        raise ValueError(
            f"Unknown CollisionShape kind {shape.kind!r} on link {link_name!r}; "
            "expected 'box' or 'cylinder'"
        )


def _emit_primitive_visual(
    link_el: ET.Element,
    link_name: str,
    shape: "CollisionShape",
) -> None:
    """Emit a ``<visual>`` block with primitive geometry, matching the
    collision shape.  Without a visual, mesh-less drones (chassis +
    rotors built procedurally via :class:`MulticopterAirframe`) are
    invisible in the Gazebo GUI even though physics is correct.

    Material is a soft gray so the user can see the airframe against
    the default sky/ground.
    """
    visual = ET.SubElement(link_el, "visual", name=f"{link_name}_visual")
    ET.SubElement(visual, "pose").text = "0 0 0 0 0 0"
    geom = ET.SubElement(visual, "geometry")
    if shape.kind == "box":
        box = ET.SubElement(geom, "box")
        dx, dy, dz = shape.size_m  # type: ignore[misc]
        ET.SubElement(box, "size").text = f"{dx} {dy} {dz}"
    elif shape.kind == "cylinder":
        cyl = ET.SubElement(geom, "cylinder")
        ET.SubElement(cyl, "radius").text = _fmt(shape.radius_m)  # type: ignore[arg-type]
        ET.SubElement(cyl, "length").text = _fmt(shape.length_m)  # type: ignore[arg-type]
    material = ET.SubElement(visual, "material")
    # Tinted slightly different per body part so the user can tell the
    # chassis apart from the rotors at a glance.
    if "rotor" in link_name:
        ET.SubElement(material, "ambient").text = "0.2 0.2 0.2 1"
        ET.SubElement(material, "diffuse").text = "0.3 0.3 0.3 1"
    else:
        ET.SubElement(material, "ambient").text = "0.6 0.2 0.2 1"
        ET.SubElement(material, "diffuse").text = "0.8 0.3 0.3 1"


def _emit_imu_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(link_el, "sensor", name="imu_sensor", type="imu")
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "250"
    imu = ET.SubElement(sensor, "imu")
    for parent_tag, stddev, bias_mean, bias_stddev in (
        ("angular_velocity", 0.0001, 7.5e-06, 8.0e-07),
        ("linear_acceleration", 0.001, 1.0e-04, 1.0e-03),
    ):
        parent_el = ET.SubElement(imu, parent_tag)
        for axis in ("x", "y", "z"):
            axis_el = ET.SubElement(parent_el, axis)
            noise = ET.SubElement(axis_el, "noise", type="gaussian")
            ET.SubElement(noise, "mean").text = "0"
            ET.SubElement(noise, "stddev").text = _fmt(stddev)
            ET.SubElement(noise, "bias_mean").text = _fmt(bias_mean)
            ET.SubElement(noise, "bias_stddev").text = _fmt(bias_stddev)


def _emit_gps_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(link_el, "sensor", name="navsat_sensor", type="navsat")
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "20"


def _emit_barometer_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(
        link_el, "sensor", name="air_pressure_sensor", type="air_pressure",
    )
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "50"
    air = ET.SubElement(sensor, "air_pressure")
    ET.SubElement(air, "reference_altitude").text = "0"
    pressure = ET.SubElement(air, "pressure")
    noise = ET.SubElement(pressure, "noise", type="gaussian")
    ET.SubElement(noise, "mean").text = "0"
    ET.SubElement(noise, "stddev").text = "1.0"


def _emit_magnetometer_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(
        link_el, "sensor", name="magnetometer_sensor", type="magnetometer",
    )
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "50"
    mag = ET.SubElement(sensor, "magnetometer")
    for axis in ("x", "y", "z"):
        axis_el = ET.SubElement(mag, axis)
        noise = ET.SubElement(axis_el, "noise", type="gaussian")
        ET.SubElement(noise, "mean").text = "0"
        ET.SubElement(noise, "stddev").text = "1.0e-06"
        ET.SubElement(noise, "bias_mean").text = "1.0e-06"
        ET.SubElement(noise, "bias_stddev").text = "3.0e-07"


def write_sdf(
    model: SimModel,
    output_path: str,
    *,
    base_dir: str | None = None,
    absolute_mesh_paths: bool = False,
    drone_config: dict[str, Any] | None = None,
) -> str:
    """Serialize a SimModel to SDF XML.

    Meshes are exported in FreeCAD mm units; SDF mesh scale is set to 0.001
    to convert to meters at runtime, matching URDF export behavior.

    Args:
        model: The SimModel to serialize.
        output_path: Path to write the SDF file.
        base_dir: When set, mesh URIs are written relative to this
            directory.  Ignored when ``absolute_mesh_paths`` is True.
        absolute_mesh_paths: When True, mesh URIs are written as absolute
            paths.  Ensures Gazebo can always resolve mesh files.
        drone_config: Optional drone plugin configuration.
    """
    sdf = ET.Element("sdf", version="1.10")
    model_el = ET.SubElement(sdf, "model", name=model.name)
    ET.SubElement(model_el, "static").text = "false"

    for link in model.links:
        link_el = ET.SubElement(model_el, "link", name=link.name)
        roll, pitch, yaw = _quat_to_rpy(*link.rotation_quat)
        # SimLink positions are tracked in mm from FreeCAD export.
        ET.SubElement(link_el, "pose").text = _pose_str(
            link.position[0] / 1000.0,
            link.position[1] / 1000.0,
            link.position[2] / 1000.0,
            roll,
            pitch,
            yaw,
        )

        if link.mesh_path is not None:
            mesh_uri = link.mesh_path
            if absolute_mesh_paths:
                mesh_uri = os.path.abspath(link.mesh_path)
            elif base_dir:
                mesh_uri = os.path.relpath(link.mesh_path, base_dir)
            visual_node = ET.SubElement(link_el, "visual", name=f"{link.name}_visual")
            ET.SubElement(visual_node, "pose").text = "0 0 0 0 0 0"
            visual_geom = ET.SubElement(visual_node, "geometry")
            visual_mesh = ET.SubElement(visual_geom, "mesh")
            ET.SubElement(visual_mesh, "uri").text = mesh_uri
            ET.SubElement(visual_mesh, "scale").text = "0.001 0.001 0.001"
            # Collision geometry: prefer the explicit primitive on the
            # link (set by drone exports via airframes/multicopter.py).
            # If the link doesn't declare a primitive, fall back to the
            # mesh — same as before for hexapod legs etc.
            if link.collision_shape is None:
                _emit_mesh_collision(link_el, link.name, mesh_uri)
            else:
                _emit_primitive_collision(link_el, link.name, link.collision_shape)
        elif link.collision_shape is not None:
            # Mesh-less link with an explicit primitive collision (e.g.
            # the chassis of a procedurally-built drone).  Emit BOTH a
            # visual (so it renders in Gazebo) and a collision (so
            # physics works).  Without the visual, the link is invisible
            # in the GUI even though physics is correct.
            _emit_primitive_visual(link_el, link.name, link.collision_shape)
            _emit_primitive_collision(link_el, link.name, link.collision_shape)

        if link.mass_kg is not None:
            inertial = ET.SubElement(link_el, "inertial")
            ET.SubElement(inertial, "mass").text = _fmt(link.mass_kg)
            if link.inertia is not None:
                inertia = ET.SubElement(inertial, "inertia")
                ET.SubElement(inertia, "ixx").text = _fmt(link.inertia[0])
                ET.SubElement(inertia, "ixy").text = _fmt(link.inertia[1])
                ET.SubElement(inertia, "ixz").text = _fmt(link.inertia[2])
                ET.SubElement(inertia, "iyy").text = _fmt(link.inertia[3])
                ET.SubElement(inertia, "iyz").text = _fmt(link.inertia[4])
                ET.SubElement(inertia, "izz").text = _fmt(link.inertia[5])

    for joint in model.joints:
        joint_type = "revolute" if joint.joint_type == "continuous" else joint.joint_type
        joint_el = ET.SubElement(model_el, "joint", name=joint.name, type=joint_type)
        ET.SubElement(joint_el, "parent").text = joint.parent
        ET.SubElement(joint_el, "child").text = joint.child
        # SDF 1.10: <pose> defaults to child link's frame.  When the child link
        # is already positioned at the joint's world location (via its <pose>),
        # writing the joint origin here causes a DOUBLE OFFSET — the rotation
        # axis ends up displaced from the child link by (origin_xyz, origin_rpy),
        # so the rotor visually spins around a point offset from its hub.
        # Omit the joint <pose> so it defaults to (0, 0, 0) in child frame =
        # joint origin coincides with child link origin.  Matches x500 stock SDF.

        axis = ET.SubElement(joint_el, "axis")
        ET.SubElement(axis, "xyz").text = _xyz_str(joint.axis)
        if joint.limits is not None:
            limit = ET.SubElement(axis, "limit")
            ET.SubElement(limit, "lower").text = _fmt(joint.limits[0])
            ET.SubElement(limit, "upper").text = _fmt(joint.limits[1])
            ET.SubElement(limit, "effort").text = _fmt(joint.effort)
            ET.SubElement(limit, "velocity").text = _fmt(joint.velocity)
        if joint.joint_type in ("revolute", "continuous", "prismatic"):
            dynamics = ET.SubElement(axis, "dynamics")
            ET.SubElement(dynamics, "damping").text = _fmt(joint.damping)
            ET.SubElement(dynamics, "friction").text = _fmt(joint.friction)

    if drone_config:
        # ------------------------------------------------------------------
        # Multicopter motor model plugins — one per rotor (Gazebo canonical).
        # ------------------------------------------------------------------
        rotors = drone_config.get("rotors") or []
        if isinstance(rotors, list) and rotors:
            joint_to_child = {j.name: j.child for j in model.joints}
            for idx, rotor in enumerate(rotors):
                if not isinstance(rotor, dict):
                    continue
                joint_name = str(rotor.get("joint", f"rotor_{idx}_joint"))
                link_name = str(
                    rotor.get("link") or joint_to_child.get(joint_name, "")
                )
                if not link_name:
                    # No way to bind the plugin to a link — skip rather than
                    # emit an invalid plugin block that PX4 will reject.
                    continue
                _emit_multicopter_motor_plugin(
                    model_el,
                    model_name=model.name,
                    joint_name=joint_name,
                    link_name=link_name,
                    direction=_normalize_turning_direction(
                        rotor.get("direction", "ccw"),
                    ),
                    actuator_number=int(rotor.get("index", idx)),
                    motor_constant=float(
                        rotor.get("motor_constant", _DEFAULT_MOTOR_CONSTANT),
                    ),
                    moment_constant=float(
                        rotor.get("moment_constant", _DEFAULT_MOMENT_CONSTANT),
                    ),
                    max_rot_velocity=float(
                        rotor.get("max_rot_velocity", _DEFAULT_MAX_ROT_VELOCITY),
                    ),
                )

        # ------------------------------------------------------------------
        # Sensor declarations (IMU/GPS/barometer/magnetometer) on the root
        # link.  Default ON when drone_config is present — PX4 SITL needs
        # these topics to converge its EKF.  Set ``sensors=False`` to opt
        # out (e.g. for non-PX4 flight stacks).
        # ------------------------------------------------------------------
        sensors_cfg = drone_config.get("sensors", True)
        if sensors_cfg:
            if not isinstance(sensors_cfg, dict):
                sensors_cfg = {}
            sensor_link_name = (
                sensors_cfg.get("link") or _find_root_link_name(model)
            )
            for link_el in model_el.findall("link"):
                if link_el.get("name") != sensor_link_name:
                    continue
                if sensors_cfg.get("imu", True):
                    _emit_imu_sensor(link_el)
                if sensors_cfg.get("gps", True):
                    _emit_gps_sensor(link_el)
                if sensors_cfg.get("barometer", True):
                    _emit_barometer_sensor(link_el)
                if sensors_cfg.get("magnetometer", True):
                    _emit_magnetometer_sensor(link_el)
                break

    tree = ET.ElementTree(sdf)
    ET.indent(tree, space="  ")
    out_path = str(Path(output_path).resolve())
    tree.write(out_path, encoding="unicode", xml_declaration=True)
    return out_path


def validate_sdf(path: str, *, drone_mode: bool = False) -> list[Finding]:
    """Validate generated SDF structure with deterministic checks."""
    findings: list[Finding] = []
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return [Finding(
            rule_id="sdf.parse_error",
            severity=Severity.BLOCK,
            message=f"SDF parse error: {exc}",
        )]

    root = tree.getroot()
    if root.tag != "sdf":
        findings.append(Finding(
            rule_id="sdf.root_tag",
            severity=Severity.BLOCK,
            message=f"Root element is <{root.tag}>, expected <sdf>",
        ))
        return findings

    model_el = root.find("model")
    if model_el is None:
        findings.append(Finding(
            rule_id="sdf.model_missing",
            severity=Severity.BLOCK,
            message="SDF is missing <model> element.",
        ))
        return findings

    links = model_el.findall("link")
    joints = model_el.findall("joint")
    if not links:
        findings.append(Finding(
            rule_id="sdf.links_missing",
            severity=Severity.BLOCK,
            message="SDF model has no <link> elements.",
        ))
        return findings

    link_names = {lk.attrib.get("name", "") for lk in links}
    if len(links) > 1 and not joints:
        findings.append(Finding(
            rule_id="sdf.joints_missing",
            severity=Severity.WARN,
            message="Model has multiple links but no joints.",
        ))

    for jel in joints:
        jname = jel.attrib.get("name", "?")
        parent = (jel.findtext("parent") or "").strip()
        child = (jel.findtext("child") or "").strip()
        if not parent or parent not in link_names:
            findings.append(Finding(
                rule_id="sdf.dangling_parent",
                severity=Severity.BLOCK,
                message=f"Joint '{jname}' references unknown parent '{parent}'.",
                field=f"joint/{jname}/parent",
            ))
        if not child or child not in link_names:
            findings.append(Finding(
                rule_id="sdf.dangling_child",
                severity=Severity.BLOCK,
                message=f"Joint '{jname}' references unknown child '{child}'.",
                field=f"joint/{jname}/child",
            ))

    if drone_mode:
        plugins = model_el.findall("plugin")
        if not plugins:
            findings.append(Finding(
                rule_id="sdf.drone.plugin_missing",
                severity=Severity.WARN,
                message="Drone-mode SDF has no <plugin> control block.",
            ))
        if len(links) < 2:
            findings.append(Finding(
                rule_id="sdf.drone.too_few_links",
                severity=Severity.WARN,
                message="Drone-mode SDF should include at least 2 links.",
            ))

    return findings


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

    # --- Check 4b: Zero mass on child links ---
    for lel in links:
        lname = lel.attrib.get("name", "?")
        mass_el = lel.find("inertial/mass")
        if mass_el is not None and lname in child_links:
            try:
                mass_val = float(mass_el.attrib.get("value", "1"))
                if mass_val == 0.0:
                    findings.append(Finding(
                        rule_id="urdf.zero_mass",
                        severity=Severity.WARN,
                        message=(
                            f"Link '{lname}' has zero mass but is a child link "
                            f"— may cause simulation instability"
                        ),
                        field=f"link/{lname}/inertial/mass",
                    ))
            except ValueError:
                pass

    # --- Check 4c: Near-zero inertia ---
    for lel in links:
        lname = lel.attrib.get("name", "?")
        inertia_el = lel.find("inertial/inertia")
        if inertia_el is not None:
            diag_vals = []
            for diag in ("ixx", "iyy", "izz"):
                val_str = inertia_el.attrib.get(diag)
                if val_str is not None:
                    try:
                        diag_vals.append(float(val_str))
                    except ValueError:
                        pass
            if diag_vals and all(v < 1e-12 for v in diag_vals):
                findings.append(Finding(
                    rule_id="urdf.tiny_inertia",
                    severity=Severity.WARN,
                    message=(
                        f"Link '{lname}' has near-zero inertia (all diag < 1e-12), "
                        f"may cause simulation instability"
                    ),
                    field=f"link/{lname}/inertial/inertia",
                ))

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
    robot_type: str | None = None,
) -> list[Finding]:
    """Parse a URDF and run forward-kinematics geometric checks.

    Catches assembly bugs that structural validation (``validate_urdf``) misses:
    world-frame meshes, baked pitch in RPY, world-frame axes, yaw conventions.

    Args:
        path: Path to the URDF file.
        mesh_bboxes: Per-link body-local bounding boxes in mm.
        ground_clearance_m: Expected chassis height above ground (meters).
        robot_type: Robot category hint for generalized checks.  When
            ``"legged"`` the tibia/coxa/femur name heuristics are replaced
            by structural analysis (leaf links, root-attached joints, chain
            joints).  ``None`` preserves the legacy name-based behaviour.

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

    # --- Check 2b: urdf.fk.child_detached_from_parent ---
    # For multi-child parents (chassis, hubs), verify child joint origins
    # are within the parent's XY footprint.
    if mesh_bboxes:
        _SCALE_2B = 0.001  # mm → m
        _MARGIN_2B = 0.010  # 10 mm

        for jname, jd in joint_data.items():
            parent = jd["parent"]
            if parent not in mesh_bboxes or parent not in link_world_tf:
                continue
            if parent_child_count.get(parent, 0) <= 1:
                continue
            child = jd["child"]
            if child not in link_world_tf:
                continue

            p_lo, p_hi = _transform_bbox(
                link_world_tf[parent], mesh_bboxes[parent], _SCALE_2B)
            p_cx = (p_lo[0] + p_hi[0]) / 2
            p_cy = (p_lo[1] + p_hi[1]) / 2
            half_x = (p_hi[0] - p_lo[0]) / 2
            half_y = (p_hi[1] - p_lo[1]) / 2

            c_origin = (
                link_world_tf[child][0][3],
                link_world_tf[child][1][3],
                link_world_tf[child][2][3],
            )
            dx = abs(c_origin[0] - p_cx)
            dy = abs(c_origin[1] - p_cy)

            if dx > half_x + _MARGIN_2B or dy > half_y + _MARGIN_2B:
                findings.append(Finding(
                    rule_id="urdf.fk.child_detached_from_parent",
                    severity=Severity.BLOCK,
                    message=(
                        f"Child '{child}' joint origin outside parent "
                        f"'{parent}' AABB + 10mm margin "
                        f"(dx={dx * 1000:.1f}mm vs {(half_x + _MARGIN_2B) * 1000:.1f}mm, "
                        f"dy={dy * 1000:.1f}mm vs {(half_y + _MARGIN_2B) * 1000:.1f}mm)"
                    ),
                    field=f"joint/{jname}",
                ))
            else:
                inscribed_r = min(half_x, half_y)
                dist = math.sqrt(
                    (c_origin[0] - p_cx) ** 2 + (c_origin[1] - p_cy) ** 2
                )
                if dist > inscribed_r + _MARGIN_2B:
                    findings.append(Finding(
                        rule_id="urdf.fk.child_detached_from_parent",
                        severity=Severity.WARN,
                        message=(
                            f"Child '{child}' at XY dist {dist * 1000:.1f}mm "
                            f"from parent '{parent}' center exceeds inscribed "
                            f"radius {inscribed_r * 1000:.1f}mm + 10mm margin"
                        ),
                        field=f"joint/{jname}",
                    ))

    # --- Check 3: urdf.fk.leaf_link_height (née tibia_tip_height) ---
    # Leaf-link tips should be between ground (z=0) and chassis height at
    # zero joint angles.  When *robot_type="legged"* we check ALL leaf links
    # (links that are never a parent in any joint, excluding root/base_link).
    # Otherwise we fall back to the legacy "tibia" name heuristic.
    if mesh_bboxes:
        # Build set of leaf links — links that never appear as a parent.
        parent_links_all = {jd["parent"] for jd in joint_data.values()}
        leaf_links = (link_names - parent_links_all) - root_links

        # Decide which links to check.
        _has_tibia = any("tibia" in ln.lower() for ln in leaf_links)
        if robot_type == "legged" or _has_tibia:
            tip_check_links = leaf_links
        else:
            tip_check_links = set()  # no candidates → skip

        for lname in tip_check_links:
            if lname not in mesh_bboxes or lname not in link_world_tf:
                continue
            leaf_bbox = mesh_bboxes[lname]
            leaf_tf = link_world_tf[lname]

            # Leaf tip: max local +X
            mid_y = (leaf_bbox.min_pt[1] + leaf_bbox.max_pt[1]) / 2.0
            mid_z = (leaf_bbox.min_pt[2] + leaf_bbox.max_pt[2]) / 2.0
            tip_local_m = (
                leaf_bbox.max_pt[0] * 0.001,
                mid_y * 0.001,
                mid_z * 0.001,
            )
            tip_world = _transform_point(leaf_tf, tip_local_m)
            tip_z = tip_world[2]

            chassis_max_z = ground_clearance_m if ground_clearance_m else 0.5
            if tip_z < -0.010 or tip_z > chassis_max_z + 0.050:
                findings.append(Finding(
                    rule_id="urdf.fk.leaf_link_height",
                    severity=Severity.WARN,
                    message=(
                        f"Leaf link '{lname}' tip at Z={tip_z:.4f}m "
                        f"(expected between 0 and {chassis_max_z:.3f}m)"
                    ),
                    field=f"link/{lname}",
                ))

    # --- Check 4: urdf.fk.root_joint_yaw_matches_radial (née coxa_yaw) ---
    # Root-attached revolute joints should have RPY yaw ≈ atan2(origin_y, origin_x).
    # When *robot_type="legged"* we check ALL root-attached revolute joints.
    # Otherwise we fall back to the legacy "coxa" name heuristic.
    def _is_root_attached(jd_inner: dict[str, Any]) -> bool:
        """Return True if *jd_inner*'s parent is root or one fixed-joint hop away."""
        p = jd_inner["parent"]
        if p in root_links:
            return True
        pj = child_to_joint.get(p)
        if pj is None:
            return False
        pjd = joint_data.get(pj, {})
        return pjd.get("type") == "fixed" and pjd.get("parent") in root_links

    for jname, jd in joint_data.items():
        # Decide whether this joint is a candidate.
        if robot_type == "legged":
            if jd["type"] != "revolute":
                continue
            if not _is_root_attached(jd):
                continue
        else:
            if "coxa" not in jname.lower():
                continue
            if not _is_root_attached(jd):
                continue

        origin = jd["xyz"]
        parent = jd["parent"]

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
                    rule_id="urdf.fk.root_joint_yaw_matches_radial",
                    severity=Severity.WARN,
                    message=(
                        f"Root-attached joint '{jname}' yaw={actual_yaw:.4f} rad, "
                        f"expected atan2(y,x)={expected_yaw:.4f} rad "
                        f"(diff {yaw_diff:.4f} rad)"
                    ),
                    field=f"joint/{jname}",
                ))

    # --- Check 5: urdf.fk.chain_joint_axis (née pitch_axis_local) ---
    # Non-root revolute joint axes in serial chains should be ≈ (0, ±1, 0)
    # in the local frame.  When *robot_type="legged"* we check ALL non-root
    # revolute joints (not just ones named "femur"/"tibia").
    # Build set of root-attached joint names for exclusion.
    _root_joint_names: set[str] = set()
    for jn, jdi in joint_data.items():
        if _is_root_attached(jdi):
            _root_joint_names.add(jn)

    for jname, jd in joint_data.items():
        if jd["type"] != "revolute":
            continue
        if robot_type == "legged":
            # Skip root-attached joints — they are yaw joints, not pitch.
            if jname in _root_joint_names:
                continue
        else:
            name_lower = jname.lower()
            if "femur" not in name_lower and "tibia" not in name_lower:
                continue

        axis = jd["axis"]
        # Check if axis is approximately (0, ±1, 0)
        dot_y = abs(axis[1])
        if dot_y < 0.99:
            findings.append(Finding(
                rule_id="urdf.fk.chain_joint_axis",
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


# ---------------------------------------------------------------------------
# Leg geometry extraction from SimModel
# ---------------------------------------------------------------------------

def extract_leg_geometry(
    sim_model: SimModel,
) -> dict[str, Any]:
    """Extract leg geometry from a built SimModel.

    Walks the link/joint tree to identify leg chains (root → coxa → femur → tibia)
    and computes segment lengths from joint-to-joint distances.

    Returns a dict with:
    - ``legs``: list of per-leg dicts with joint_names, segment_lengths_m,
      hip_position_m, hip_angle_rad
    - ``n_legs``: number of legs found
    - ``dofs_per_leg``: joints per leg chain (2 or 3)
    - ``hip_mounts``: list of ``(x_m, y_m, angle_rad)`` per leg
    - ``body_dims_m``: ``(length, width)`` estimated from hip positions

    Only revolute/continuous joints are followed as leg segments.
    """
    _EMPTY: dict[str, Any] = {
        "legs": [], "n_legs": 0, "dofs_per_leg": 0,
        "hip_mounts": [], "body_dims_m": (0.0, 0.0),
    }

    # Build adjacency: parent_link → [(joint, child_link)]
    children_of: dict[str, list[tuple[SimJoint, str]]] = {}
    for jnt in sim_model.joints:
        children_of.setdefault(jnt.parent, []).append((jnt, jnt.child))

    # Find root link (is_root=True or the one with no parent)
    child_set = {jnt.child for jnt in sim_model.joints}
    root_links = [lnk for lnk in sim_model.links if lnk.is_root]
    if not root_links:
        root_links = [lnk for lnk in sim_model.links if lnk.name not in child_set]
    if not root_links:
        return _EMPTY

    root_name = root_links[0].name

    # Build joint origin lookup (SimJoint origin_xyz is in meters)
    joint_origin: dict[str, tuple[float, float, float]] = {}
    for jnt in sim_model.joints:
        joint_origin[jnt.name] = jnt.origin_xyz

    _MOVABLE_TYPES = frozenset({"revolute", "continuous"})

    def _walk_chain(link: str) -> list[list[SimJoint]]:
        """DFS from link, collecting chains of movable joints."""
        kids = children_of.get(link, [])
        movable = [(j, c) for j, c in kids if j.joint_type in _MOVABLE_TYPES]
        fixed = [(j, c) for j, c in kids if j.joint_type not in _MOVABLE_TYPES]

        chains: list[list[SimJoint]] = []

        # Follow fixed joints transparently (e.g. base_link → chassis)
        for _fj, fchild in fixed:
            chains.extend(_walk_chain(fchild))

        # Each movable joint starts or extends a chain
        for mj, mchild in movable:
            sub_chains = _walk_chain(mchild)
            if sub_chains:
                for sc in sub_chains:
                    chains.append([mj] + sc)
            else:
                chains.append([mj])

        return chains

    raw_chains = _walk_chain(root_name)
    if not raw_chains:
        return _EMPTY

    # All leg chains should have the same length; use the mode
    from collections import Counter
    chain_lengths = [len(c) for c in raw_chains]
    length_counts = Counter(chain_lengths)
    target_len = length_counts.most_common(1)[0][0]
    chains = [c for c in raw_chains if len(c) == target_len]

    # Build per-leg dicts
    legs: list[dict[str, Any]] = []
    hip_mounts: list[tuple[float, float, float]] = []

    for chain in chains:
        joint_names = tuple(j.name for j in chain)
        seg_lengths: list[float] = []
        for i in range(len(chain) - 1):
            o1 = joint_origin[chain[i].name]
            o2 = joint_origin[chain[i + 1].name]
            dist = math.sqrt(
                (o2[0] - o1[0]) ** 2
                + (o2[1] - o1[1]) ** 2
                + (o2[2] - o1[2]) ** 2
            )
            seg_lengths.append(dist)

        hip_pos = joint_origin[chain[0].name]
        hip_angle = math.atan2(hip_pos[1], hip_pos[0])

        legs.append({
            "joint_names": joint_names,
            "segment_lengths_m": tuple(seg_lengths),
            "hip_position_m": hip_pos,
            "hip_angle_rad": hip_angle,
        })
        hip_mounts.append((hip_pos[0], hip_pos[1], hip_angle))

    # Estimate body dimensions from hip positions
    if hip_mounts:
        xs = [abs(m[0]) for m in hip_mounts]
        ys = [abs(m[1]) for m in hip_mounts]
        body_length = max(xs) * 2.0 if xs else 0.0
        body_width = max(ys) * 2.0 if ys else 0.0
    else:
        body_length = 0.0
        body_width = 0.0

    return {
        "legs": legs,
        "n_legs": len(legs),
        "dofs_per_leg": target_len,
        "hip_mounts": hip_mounts,
        "body_dims_m": (body_length, body_width),
    }
