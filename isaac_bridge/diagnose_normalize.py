"""Normalize Isaac's scene report into the contract's ``diagnose`` shape.

``diagnose`` is a capability-gated verb whose *result* is normalized by
contract: generic joint-type counts, DOF, and per-joint connectivity — vendor
scene-graph vocabulary (``PhysicsRevoluteJoint``, USD prim paths, applied
schemas) stays on this side of the boundary (``docs/engine-contract.md`` §3.2).

Core reads only the normalized block, so one generic urdf-vs-diagnose check
works against every engine.  The raw USD detail is still returned alongside it
for humans debugging a scene.

Pure functions — no Isaac imports, so this is testable anywhere.
"""

from __future__ import annotations

from typing import Any

__all__ = ["normalize_diagnose"]

# USD typed-schema joint names → the contract's generic vocabulary.
_JOINT_TYPE_MAP: dict[str, str] = {
    "PhysicsRevoluteJoint": "revolute",
    "PhysicsPrismaticJoint": "prismatic",
    "PhysicsFixedJoint": "fixed",
    "PhysicsSphericalJoint": "spherical",
    "PhysicsDistanceJoint": "distance",
    "PhysicsJoint": "other",
}

# Joint kinds that carry a degree of freedom (i.e. should have a drive).
_ACTUATED = ("revolute", "prismatic")


def _generic_type(usd_type: str) -> str:
    return _JOINT_TYPE_MAP.get(usd_type, "other")


def _has_drive(joint_detail: dict[str, Any], generic_type: str) -> bool | None:
    """True when the joint has non-zero stiffness or damping.

    ``None`` when the scene reports no drive attributes at all — absent data
    is not the same as a joint with no actuation force.
    """
    namespace = "angular" if generic_type == "revolute" else "linear"
    stiffness = joint_detail.get(f"drive_{namespace}_stiffness")
    damping = joint_detail.get(f"drive_{namespace}_damping")
    if stiffness is None and damping is None:
        return None
    return bool((stiffness or 0.0) != 0.0 or (damping or 0.0) != 0.0)


def normalize_diagnose(
    *,
    type_counts: dict[str, int],
    joint_details: list[dict[str, Any]],
    articulation: dict[str, Any] | None = None,
    link_count: int | None = None,
) -> dict[str, Any]:
    """Build the contract-normalized portion of a ``diagnose`` result."""
    joint_counts: dict[str, int] = {}
    for usd_type, count in type_counts.items():
        if usd_type not in _JOINT_TYPE_MAP:
            continue
        generic = _generic_type(usd_type)
        joint_counts[generic] = joint_counts.get(generic, 0) + int(count)

    joints: list[dict[str, Any]] = []
    for detail in joint_details:
        generic = _generic_type(str(detail.get("type", "")))
        entry: dict[str, Any] = {
            "name": str(detail.get("path", "")),
            "type": generic,
            # A joint missing either body target is disconnected: it will
            # silently do nothing in the simulation.
            "connected": bool(detail.get("physics_body0")) and bool(detail.get("physics_body1")),
        }
        drive = _has_drive(detail, generic)
        if drive is not None:
            entry["has_drive"] = drive
        joints.append(entry)

    normalized: dict[str, Any] = {
        "joint_counts": joint_counts,
        "joint_total": sum(joint_counts.values()),
        "joints": joints,
    }
    if link_count is not None:
        normalized["link_count"] = int(link_count)
    if articulation:
        if "dof_count" in articulation:
            normalized["dof_count"] = int(articulation["dof_count"])
        if "dof_names" in articulation:
            normalized["dof_names"] = list(articulation["dof_names"])
    return normalized
