"""Small analytic physics for the reference engine.

Enough to answer the TCK's physics-sanity tier honestly: a gear train
propagates its ratio, a pendulum swings at its natural period, a dropped body
falls at g and settles on the ground.  Each is a closed-form or one-line
integrator — the point is a *correct* answer to check against, not fidelity.

Pure functions over plain dicts, so this file is readable by an engine author
looking for the shape of the job.
"""

from __future__ import annotations

import math
from typing import Any

GRAVITY_M_S2 = 9.81

__all__ = [
    "GRAVITY_M_S2",
    "fall_height",
    "gear_train_speeds",
    "pendulum_angle",
    "pendulum_period",
    "settle_time",
]


# ---------------------------------------------------------------------------
# Gear trains
# ---------------------------------------------------------------------------


def _ratio(joint: dict[str, Any]) -> float:
    """Speed multiplier from parent to child across a mesh.

    Meshed external gears turn opposite ways, so the ratio is negative:
    ``ω_child = -ω_parent · z_parent / z_child``.  An explicit ``gear_ratio``
    wins when the caller states one.
    """
    teeth_parent = joint.get("teeth_parent")
    teeth_child = joint.get("teeth_child")
    if teeth_parent and teeth_child:
        magnitude = float(teeth_parent) / float(teeth_child)
    elif joint.get("gear_ratio"):
        magnitude = abs(float(joint["gear_ratio"]))
    else:
        magnitude = 1.0
    return -magnitude if not joint.get("internal") else magnitude


def gear_train_speeds(mechanism: dict[str, Any]) -> dict[str, float]:
    """Steady-state speed (RPM) of every part reachable from a drive.

    Walks outward from each driven joint through the gear meshes, applying
    each mesh's ratio.  Revolute joints carry speed unchanged — they are
    bearings, not reducers.
    """
    joints = [j for j in mechanism.get("joints") or [] if isinstance(j, dict)]
    drives = [d for d in mechanism.get("drives") or [] if isinstance(d, dict)]
    ground = {
        p.get("id")
        for p in mechanism.get("parts") or []
        if isinstance(p, dict) and p.get("is_ground")
    }

    joints_by_id = {j.get("id"): j for j in joints}
    speeds: dict[str, float] = {}

    # Seed: each drive spins the part on the far side of its joint.
    for drive in drives:
        joint = joints_by_id.get(drive.get("joint_id"))
        if joint is None:
            continue
        rpm = float(drive.get("speed_rpm") or 0.0)
        driven = joint.get("child_part")
        if driven in ground:
            driven = joint.get("parent_part")
        if driven:
            speeds[str(driven)] = rpm

    # Propagate through meshes until nothing new is reached.
    for _ in range(len(joints) + 1):
        changed = False
        for joint in joints:
            if joint.get("joint_type") not in ("gear_mesh", "belt_chain"):
                continue
            parent, child = joint.get("parent_part"), joint.get("child_part")
            if parent in speeds and child not in speeds:
                factor = _ratio(joint) if joint.get("joint_type") == "gear_mesh" else 1.0
                speeds[str(child)] = speeds[str(parent)] * factor
                changed = True
            elif child in speeds and parent not in speeds:
                factor = _ratio(joint) if joint.get("joint_type") == "gear_mesh" else 1.0
                speeds[str(parent)] = speeds[str(child)] / factor
                changed = True
        if not changed:
            break

    for part_id in ground:
        speeds[str(part_id)] = 0.0
    return speeds


# ---------------------------------------------------------------------------
# Pendulum
# ---------------------------------------------------------------------------


def pendulum_period(length_m: float) -> float:
    """Small-angle period, ``T = 2π√(L/g)``."""
    if length_m <= 0:
        raise ValueError("pendulum length must be > 0")
    return 2.0 * math.pi * math.sqrt(length_m / GRAVITY_M_S2)


def pendulum_angle(length_m: float, theta0_rad: float, t_s: float) -> float:
    """Small-angle solution ``θ(t) = θ₀·cos(√(g/L)·t)``."""
    omega = math.sqrt(GRAVITY_M_S2 / length_m)
    return theta0_rad * math.cos(omega * t_s)


# ---------------------------------------------------------------------------
# Free fall onto the ground plane
# ---------------------------------------------------------------------------


def fall_height(initial_height_m: float, t_s: float) -> float:
    """Height at time *t* for a body dropped from rest, clamped at the ground."""
    return max(0.0, initial_height_m - 0.5 * GRAVITY_M_S2 * t_s * t_s)


def settle_time(initial_height_m: float) -> float:
    """When a body dropped from rest reaches the ground: ``√(2h/g)``."""
    if initial_height_m <= 0:
        return 0.0
    return math.sqrt(2.0 * initial_height_m / GRAVITY_M_S2)
