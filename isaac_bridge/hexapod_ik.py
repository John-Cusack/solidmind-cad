"""Standalone 3-DOF hexapod leg inverse kinematics.

Pure-math module (stdlib ``math`` only, no numpy/Isaac).  Provides
forward kinematics, inverse kinematics with workspace clamping, and
coordinate transforms between body and hip frames.

IK convention: elbow-down (tibia angle <= 0).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegGeometry:
    """Segment lengths for a 3-DOF leg (meters)."""

    l_coxa: float = 0.03
    l_femur: float = 0.06
    l_tibia: float = 0.08


@dataclass(frozen=True, slots=True)
class LegAngles:
    """Joint angles for a 3-DOF leg (radians)."""

    coxa: float
    femur: float
    tibia: float


@dataclass(frozen=True, slots=True)
class HipMount:
    """Hip mounting position on the body frame.

    ``x``, ``y`` are the hip pivot location in the body frame (meters).
    ``angle`` is the mounting yaw in radians (0 = forward along +X).
    """

    x: float
    y: float
    angle: float


def forward_kinematics(angles: LegAngles, geom: LegGeometry) -> tuple[float, float, float]:
    """Compute foot position in the hip frame from joint angles.

    Returns (px, py, pz) in meters relative to the hip pivot.
    """
    # Coxa rotates in the XY plane
    r_coxa = geom.l_coxa
    # Femur + tibia project onto the radial–vertical plane
    femur_x = geom.l_femur * math.cos(angles.femur)
    femur_z = geom.l_femur * math.sin(angles.femur)
    tibia_x = geom.l_tibia * math.cos(angles.femur + angles.tibia)
    tibia_z = geom.l_tibia * math.sin(angles.femur + angles.tibia)

    r_total = r_coxa + femur_x + tibia_x
    pz = femur_z + tibia_z

    px = r_total * math.cos(angles.coxa)
    py = r_total * math.sin(angles.coxa)
    return px, py, pz


def inverse_kinematics(
    px: float, py: float, pz: float, geom: LegGeometry,
) -> LegAngles:
    """Solve 3-DOF IK for a foot position in the hip frame.

    Uses elbow-down convention (tibia <= 0).  Clamps unreachable
    points to the workspace boundary — never raises or returns NaN.

    Args:
        px, py, pz: Foot position in hip frame (meters).
        geom: Leg segment lengths.

    Returns:
        ``LegAngles`` with coxa, femur, tibia in radians.
    """
    l_c = geom.l_coxa
    l_f = geom.l_femur
    l_t = geom.l_tibia

    # Step 1: coxa angle from XY projection
    theta_coxa = math.atan2(py, px)

    # Step 2: radial distance from coxa pivot to foot in XY plane
    r_xy = math.sqrt(px * px + py * py) - l_c
    # Prevent negative radial distance (foot inside coxa)
    if r_xy < 0.0:
        r_xy = 0.0

    dz = pz

    # Step 3: distance from femur pivot to foot
    d_sq = r_xy * r_xy + dz * dz
    d = math.sqrt(d_sq)

    # Clamp D to reachable range [|l_f - l_t|, l_f + l_t]
    d_min = abs(l_f - l_t)
    d_max = l_f + l_t
    if d < d_min:
        d = d_min
        d_sq = d * d
    elif d > d_max:
        d = d_max
        d_sq = d * d

    # Step 4: tibia angle via law of cosines (elbow-down: negative)
    # D² = l_f² + l_t² + 2·l_f·l_t·cos(θ_t)  →  cos(θ_t) = (D² - l_f² - l_t²) / (2·l_f·l_t)
    cos_tibia = (d_sq - l_f * l_f - l_t * l_t) / (2.0 * l_f * l_t)
    cos_tibia = max(-1.0, min(1.0, cos_tibia))
    theta_tibia = -math.acos(cos_tibia)

    # Step 5: femur angle
    # θ_f = atan2(dz, r) - atan2(l_t·sin(θ_t), l_f + l_t·cos(θ_t))
    theta_femur = math.atan2(dz, r_xy) - math.atan2(
        l_t * math.sin(theta_tibia),
        l_f + l_t * math.cos(theta_tibia),
    )

    return LegAngles(coxa=theta_coxa, femur=theta_femur, tibia=theta_tibia)


def body_to_hip_frame(
    point_body: tuple[float, float, float],
    mount: HipMount,
) -> tuple[float, float, float]:
    """Transform a point from body frame to hip frame.

    Translates by (-mount.x, -mount.y, 0) then rotates by -mount.angle
    around Z.
    """
    dx = point_body[0] - mount.x
    dy = point_body[1] - mount.y
    dz = point_body[2]

    cos_a = math.cos(-mount.angle)
    sin_a = math.sin(-mount.angle)

    hx = dx * cos_a - dy * sin_a
    hy = dx * sin_a + dy * cos_a
    return hx, hy, dz


def default_foot_position(
    mount: HipMount,
    geom: LegGeometry,
    stance_z: float = -0.08,
) -> tuple[float, float, float]:
    """Compute the neutral stance foot position in body frame.

    The foot extends straight out from the hip along the mounting
    angle at a radial distance of l_coxa + l_femur + l_tibia projected
    to horizontal, with the given stance_z height.
    """
    # Fully extended horizontal reach is not realistic; use a natural
    # reach where femur is ~horizontal and tibia hangs down.
    # Natural reach: coxa + femur (horizontal) + tibia * cos(natural_tibia)
    # For a natural stance, compute the reach that produces stance_z.
    l_c = geom.l_coxa
    l_f = geom.l_femur
    l_t = geom.l_tibia

    # Target: foot at (r_total, 0, stance_z) in hip frame where
    # r_total = l_c + r, and we solve IK for (r_total, 0, stance_z).
    # Use a reasonable default: extend horizontally at stance_z.
    # The IK will clamp if needed.
    r_natural = l_c + math.sqrt(max(0.0, l_f * l_f + l_t * l_t + 2 * l_f * l_t
                                     - stance_z * stance_z)) * 0.5 + l_c * 0.5
    # Simpler: just use l_coxa + l_femur as the radial reach
    r_reach = l_c + l_f

    fx = mount.x + r_reach * math.cos(mount.angle)
    fy = mount.y + r_reach * math.sin(mount.angle)
    fz = stance_z
    return fx, fy, fz
