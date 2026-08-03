"""Canonical sim-package manifest — Engine Integration Contract v1 §6.

``cad.export_sim_package`` writes meshes and (optionally) a URDF, but until now
the *model description* only ever existed in memory and travelled back over MCP.
A non-Python engine had nothing to read.  This module serializes that same data
to ``manifest.json`` beside the meshes, validated by
``schemas/sim_package.schema.json``.

Everything here is pure: dicts in, dicts out, no FreeCAD and no sockets.

Units
-----
Manifest values are SI throughout — metres, kilograms, radians.  Meshes are the
one exception: FreeCAD exports STL in millimetres, so every mesh entry declares
``{"unit": "mm", "scale_to_m": 0.001}``.  That matches the
``scale="0.001 0.001 0.001"`` attribute :func:`server.sim_export.write_urdf`
emits for the same files.

Two modes
---------
**full** (a mechanism was supplied): links carry mass/inertia/collision from the
:class:`~server.sim_export.SimModel`, joints describe the kinematic tree, and
mesh vertices have been rewritten to link-local coordinates by
``build_sim_model``.

**reduced** (bodies only): links carry pose and mesh only, ``joints`` is empty,
and mesh vertices are still in world coordinates.

Both are schema-valid; ``mode`` and each mesh's ``frame`` say which is which.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from server.sim_export import (
    _DEFAULT_MAX_ROT_VELOCITY,
    _DEFAULT_MOMENT_CONSTANT,
    _DEFAULT_MOTOR_CONSTANT,
    SimJoint,
    SimLink,
    SimModel,
)

__all__ = [
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "build_manifest",
    "write_manifest",
]

SCHEMA_VERSION = "1.0.0"
MANIFEST_FILENAME = "manifest.json"

# FreeCAD exports meshes in millimetres regardless of the requested format.
_MESH_UNIT = "mm"
_MESH_SCALE_TO_M = 0.001

_MM_TO_M = 0.001
_IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)

_SENSOR_TYPES = ("imu", "gps", "barometer", "magnetometer")


def _generator() -> str:
    """Return the producing tool + version, e.g. ``solidmind-cad 0.2.0``."""
    try:
        from importlib.metadata import version

        return f"solidmind-cad {version('solidmind-cad')}"
    except Exception:  # noqa: BLE001 — version metadata is best-effort
        return "solidmind-cad"


# ---------------------------------------------------------------------------
# Quaternion helpers (local by design — this module stays self-contained)
# ---------------------------------------------------------------------------


def _quat_multiply(
    q1: tuple[float, float, float, float],
    q2: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Hamilton product of two (w, x, y, z) quaternions."""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def _quat_from_rpy(rpy: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Convert roll-pitch-yaw (radians, ZYX order) to a (w, x, y, z) quaternion."""
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    return (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def _quat_rotate(
    q: tuple[float, float, float, float],
    v: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Rotate vector *v* by quaternion *q* (w, x, y, z)."""
    w, x, y, z = q
    vx, vy, vz = v
    # t = 2 * (q_vec × v);  v' = v + w*t + q_vec × t
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _normalize_direction(value: Any) -> str:
    """Accept ``'ccw'``/``'cw'`` or ``+1``/``-1`` and return the canonical string."""
    if value in ("cw", "CW", -1, "-1"):
        return "cw"
    return "ccw"


# ---------------------------------------------------------------------------
# Forward kinematics
# ---------------------------------------------------------------------------


def _link_world_poses(
    model: SimModel,
) -> dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]]:
    """Compute every link's world pose (metres, quaternion) from the joint tree.

    Roots start at their own ``SimLink`` placement (millimetres from FreeCAD);
    each child is placed by its joint origin, which the URDF convention defines
    in the parent's frame.  Poses derived this way are consistent with the
    link-local meshes ``build_sim_model`` writes — the joint tree, not the
    original CAD placement, is what positions a link in a full export.
    """
    children: dict[str, list[SimJoint]] = {}
    child_names: set[str] = set()
    for joint in model.joints:
        children.setdefault(joint.parent, []).append(joint)
        child_names.add(joint.child)

    poses: dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]] = {}
    queue: list[str] = []
    for link in model.links:
        if link.name in child_names:
            continue
        poses[link.name] = (
            (
                link.position[0] * _MM_TO_M,
                link.position[1] * _MM_TO_M,
                link.position[2] * _MM_TO_M,
            ),
            link.rotation_quat,
        )
        queue.append(link.name)

    while queue:
        parent = queue.pop(0)
        parent_pos, parent_quat = poses[parent]
        for joint in children.get(parent, ()):
            if joint.child in poses:
                continue  # already placed — ignore cycles / duplicate parents
            offset = _quat_rotate(parent_quat, joint.origin_xyz)
            poses[joint.child] = (
                (
                    parent_pos[0] + offset[0],
                    parent_pos[1] + offset[1],
                    parent_pos[2] + offset[2],
                ),
                _quat_multiply(parent_quat, _quat_from_rpy(joint.origin_rpy)),
            )
            queue.append(joint.child)

    # Links unreachable from any root (malformed trees) keep their own placement.
    for link in model.links:
        poses.setdefault(
            link.name,
            (
                (
                    link.position[0] * _MM_TO_M,
                    link.position[1] * _MM_TO_M,
                    link.position[2] * _MM_TO_M,
                ),
                link.rotation_quat,
            ),
        )
    return poses


# ---------------------------------------------------------------------------
# Manifest sections
# ---------------------------------------------------------------------------


def _pose_entry(
    xyz_m: tuple[float, float, float],
    quat_wxyz: tuple[float, float, float, float],
) -> dict[str, Any]:
    return {
        "xyz_m": [float(xyz_m[0]), float(xyz_m[1]), float(xyz_m[2])],
        "quat_wxyz": [
            float(quat_wxyz[0]),
            float(quat_wxyz[1]),
            float(quat_wxyz[2]),
            float(quat_wxyz[3]),
        ],
    }


def _mesh_entry(mesh_path: str, output_dir: str, frame: str) -> dict[str, Any]:
    return {
        "file": os.path.relpath(mesh_path, output_dir),
        "unit": _MESH_UNIT,
        "scale_to_m": _MESH_SCALE_TO_M,
        "frame": frame,
    }


def _collision_entry(link: SimLink) -> dict[str, Any] | None:
    shape = link.collision_shape
    if shape is None:
        return None
    entry: dict[str, Any] = {"kind": shape.kind}
    if shape.size_m is not None:
        entry["size_m"] = [float(v) for v in shape.size_m]
    if shape.radius_m is not None:
        entry["radius_m"] = float(shape.radius_m)
    if shape.length_m is not None:
        entry["length_m"] = float(shape.length_m)
    return entry


def _reduced_links(body_manifest: list[dict[str, Any]], output_dir: str) -> list[dict[str, Any]]:
    """Bodies-only links: pose + mesh, straight from the addon's body manifest."""
    links: list[dict[str, Any]] = []
    for entry in body_manifest:
        placement = entry.get("placement", {}) or {}
        position = placement.get("position", [0.0, 0.0, 0.0])
        quat = placement.get("rotation_quat", list(_IDENTITY_QUAT))
        link: dict[str, Any] = {
            "name": str(entry.get("name", "")),
            "world_pose": _pose_entry(
                (
                    float(position[0]) * _MM_TO_M,
                    float(position[1]) * _MM_TO_M,
                    float(position[2]) * _MM_TO_M,
                ),
                (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3])),
            ),
        }
        mesh_path = entry.get("mesh_path")
        if mesh_path:
            # Bodies-only exports leave mesh vertices in world coordinates.
            link["mesh"] = _mesh_entry(str(mesh_path), output_dir, "world")
        links.append(link)
    return links


def _full_links(model: SimModel, output_dir: str) -> list[dict[str, Any]]:
    """Mechanism-backed links: pose from the joint tree + mass/inertia/collision."""
    poses = _link_world_poses(model)
    links: list[dict[str, Any]] = []
    for link in model.links:
        xyz_m, quat = poses[link.name]
        entry: dict[str, Any] = {
            "name": link.name,
            "is_root": bool(link.is_root),
            "world_pose": _pose_entry(xyz_m, quat),
        }
        if link.mesh_path:
            # build_sim_model rewrote these meshes into link-local coordinates.
            entry["mesh"] = _mesh_entry(link.mesh_path, output_dir, "link_local")
        if link.mass_kg is not None:
            entry["mass_kg"] = float(link.mass_kg)
        if link.inertia is not None:
            ixx, ixy, ixz, iyy, iyz, izz = link.inertia
            entry["inertia"] = {
                "ixx": float(ixx),
                "ixy": float(ixy),
                "ixz": float(ixz),
                "iyy": float(iyy),
                "iyz": float(iyz),
                "izz": float(izz),
            }
        collision = _collision_entry(link)
        if collision is not None:
            entry["collision"] = collision
        links.append(entry)
    return links


def _joints(model: SimModel) -> list[dict[str, Any]]:
    joints: list[dict[str, Any]] = []
    for joint in model.joints:
        entry: dict[str, Any] = {
            "name": joint.name,
            "type": joint.joint_type,
            "parent": joint.parent,
            "child": joint.child,
            "origin_xyz_m": [float(v) for v in joint.origin_xyz],
            "origin_rpy_rad": [float(v) for v in joint.origin_rpy],
            "axis": [float(v) for v in joint.axis],
            "effort": float(joint.effort),
            "velocity": float(joint.velocity),
            "damping": float(joint.damping),
            "friction": float(joint.friction),
        }
        if joint.limits is not None:
            entry["limits"] = {"lower": float(joint.limits[0]), "upper": float(joint.limits[1])}
        if joint.mimic is not None:
            entry["mimic"] = {"joint": joint.mimic[0], "multiplier": float(joint.mimic[1])}
        joints.append(entry)
    return joints


def _actuators(
    model: SimModel,
    drone_config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Abstract rotor specs from ``drone_config`` — never vendor plugin config.

    ``max_thrust_N`` is the physical thrust at the rotor's maximum angular
    rate: for the ``k·ω²`` thrust model both Gazebo's motor plugin and the PX4
    airframe generator use, that is ``motor_constant * max_rot_velocity²``.
    """
    if not drone_config:
        return []
    rotors = drone_config.get("rotors") or []
    if not isinstance(rotors, list):
        return []

    joint_to_child = {j.name: j.child for j in model.joints}
    poses = _link_world_poses(model)

    actuators: list[dict[str, Any]] = []
    for idx, rotor in enumerate(rotors):
        if not isinstance(rotor, dict):
            continue
        joint_name = str(rotor.get("joint", f"rotor_{idx}_joint"))
        link_name = str(rotor.get("link") or joint_to_child.get(joint_name, ""))
        motor_constant = float(rotor.get("motor_constant", _DEFAULT_MOTOR_CONSTANT))
        max_rot_velocity = float(rotor.get("max_rot_velocity", _DEFAULT_MAX_ROT_VELOCITY))

        entry: dict[str, Any] = {
            "type": "rotor",
            "name": str(rotor.get("name") or f"rotor_{rotor.get('index', idx)}"),
            "index": int(rotor.get("index", idx)),
            "joint": joint_name,
            "direction": _normalize_direction(rotor.get("direction", "ccw")),
            "max_thrust_N": motor_constant * max_rot_velocity * max_rot_velocity,
            "moment_constant": float(rotor.get("moment_constant", _DEFAULT_MOMENT_CONSTANT)),
        }
        if link_name:
            entry["link"] = link_name
            if link_name in poses:
                entry["position_m"] = [float(v) for v in poses[link_name][0]]
        actuators.append(entry)
    return actuators


def _sensors(model: SimModel, drone_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Sensor presence entries, mirroring what drone mode emits today.

    ``drone_config["sensors"]`` is truthy-by-default (PX4's EKF needs the
    topics); a dict selectively disables members and may override the link.
    """
    if not drone_config:
        return []
    sensors_cfg = drone_config.get("sensors", True)
    if not sensors_cfg:
        return []
    if not isinstance(sensors_cfg, dict):
        sensors_cfg = {}

    link_name = sensors_cfg.get("link")
    if not link_name:
        root = next((lk for lk in model.links if lk.is_root), None)
        link_name = root.name if root is not None else (model.links[0].name if model.links else "")
    if not link_name:
        return []

    return [
        {"type": sensor, "link": str(link_name)}
        for sensor in _SENSOR_TYPES
        if sensors_cfg.get(sensor, True)
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    name: str,
    output_dir: str,
    body_manifest: list[dict[str, Any]] | None = None,
    sim_model: SimModel | None = None,
    drone_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical package manifest as a plain dict.

    Args:
        name: Package/model name.
        output_dir: Package directory — mesh paths are recorded relative to it.
        body_manifest: Per-body export records from the FreeCAD addon.  Used for
            the reduced (bodies-only) mode.
        sim_model: When provided, the manifest is a **full** export: links,
            joints, mass and inertia come from this model.
        drone_config: Optional rotor/sensor configuration, translated into
            abstract ``actuators``/``sensors`` entries.

    Returns:
        A dict validating against ``schemas/sim_package.schema.json``.
    """
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "generator": _generator(),
        "mode": "full" if sim_model is not None else "reduced",
        "units": {"length": "m", "mass": "kg", "angle": "rad"},
    }

    if sim_model is not None:
        manifest["links"] = _full_links(sim_model, output_dir)
        manifest["joints"] = _joints(sim_model)
        actuators = _actuators(sim_model, drone_config)
        if actuators:
            manifest["actuators"] = actuators
        sensors = _sensors(sim_model, drone_config)
        if sensors:
            manifest["sensors"] = sensors
    else:
        manifest["links"] = _reduced_links(body_manifest or [], output_dir)
        manifest["joints"] = []

    return manifest


def write_manifest(
    manifest: dict[str, Any],
    output_dir: str,
    *,
    filename: str = MANIFEST_FILENAME,
) -> str:
    """Write *manifest* to ``<output_dir>/<filename>`` and return the path."""
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path
