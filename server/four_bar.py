"""Four-bar linkage analysis.

Classifies Grashof type, solves position kinematics via circle-circle
intersection, traces coupler curves, computes transmission angles and
mechanical advantage, and detects dead points.  Pure Python — no Rust
dependency.
"""
from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TWO_PI = 2.0 * math.pi
_DEG = math.degrees
_RAD = math.radians
_EPS = 1.0e-9


def _normalize_angle(a: float) -> float:
    """Wrap angle into [0, 2*pi)."""
    a = a % _TWO_PI
    if a < 0.0:
        a += _TWO_PI
    return a


def _circle_circle_intersection(
    cx1: float,
    cy1: float,
    r1: float,
    cx2: float,
    cy2: float,
    r2: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return the two intersection points of two circles, or *None*.

    Returns ``(open_solution, crossed_solution)`` where the *open*
    solution has a positive cross-product determinant (counter-clockwise
    winding).
    """
    dx = cx2 - cx1
    dy = cy2 - cy1
    d = math.hypot(dx, dy)

    if d < _EPS:
        return None  # concentric
    if d > r1 + r2 + _EPS:
        return None  # too far apart
    if d < abs(r1 - r2) - _EPS:
        return None  # one circle inside the other

    a = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
    h_sq = r1 * r1 - a * a
    if h_sq < 0.0:
        h_sq = 0.0  # numerical clamp
    h = math.sqrt(h_sq)

    mx = cx1 + a * dx / d
    my = cy1 + a * dy / d

    px1 = mx + h * dy / d
    py1 = my - h * dx / d
    px2 = mx - h * dy / d
    py2 = my + h * dx / d

    # Open configuration: positive cross-product determinant relative to
    # the line from center1 to center2.
    cross1 = dx * (py1 - cy1) - dy * (px1 - cx1)
    if cross1 >= 0.0:
        return (px1, py1), (px2, py2)
    return (px2, py2), (px1, py1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_grashof(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
) -> str:
    """Classify a four-bar linkage by Grashof's criterion.

    Parameters
    ----------
    ground : float
        Length of the fixed link.
    input_link : float
        Length of the driving (input) crank.
    coupler : float
        Length of the coupler link.
    output_link : float
        Length of the driven (output / follower) link.

    Returns
    -------
    str
        One of ``"double_crank"``, ``"crank_rocker"``,
        ``"double_rocker"`` (Grashof), ``"non_grashof_double_rocker"``,
        or ``"change_point"``.
    """
    links = {
        "ground": ground,
        "input_link": input_link,
        "coupler": coupler,
        "output_link": output_link,
    }
    for name, val in links.items():
        if val <= 0.0:
            raise ValueError(f"{name} must be positive, got {val}")

    lengths = sorted(links.values())
    s = lengths[0]
    l_val = lengths[3]
    p = lengths[1]
    q = lengths[2]

    lhs = s + l_val
    rhs = p + q

    if abs(lhs - rhs) < _EPS * max(lhs, rhs):
        return "change_point"

    if lhs > rhs:
        return "non_grashof_double_rocker"

    # Grashof — identify which link is shortest.
    shortest_name = min(links, key=links.get)  # type: ignore[arg-type]
    if shortest_name == "ground":
        return "double_crank"
    if shortest_name in ("input_link", "output_link"):
        return "crank_rocker"
    # shortest_name == "coupler"
    return "double_rocker"


def solve_position(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
    input_angle_deg: float,
    *,
    crossed: bool = False,
) -> dict[str, float]:
    """Solve position kinematics for a given input angle.

    Parameters
    ----------
    ground : float
        Fixed link length.
    input_link : float
        Input crank length.
    coupler : float
        Coupler link length.
    output_link : float
        Output (follower) link length.
    input_angle_deg : float
        Input crank angle measured CCW from the positive x-axis (degrees).
    crossed : bool
        If *True*, return the crossed (second) configuration.

    Returns
    -------
    dict
        Keys: ``output_angle_deg``, ``coupler_angle_deg``,
        ``A_x``, ``A_y`` (input crank end / coupler start),
        ``B_x``, ``B_y`` (coupler end / output link end).

    Raises
    ------
    ValueError
        If the mechanism cannot assemble at the given angle.
    """
    theta2 = _RAD(input_angle_deg)

    # Fixed pivots
    _o2x, _o2y = 0.0, 0.0
    o4x, o4y = ground, 0.0

    # Input crank end
    ax = input_link * math.cos(theta2)
    ay = input_link * math.sin(theta2)

    # Circle-circle intersection: center A radius coupler, center O4
    # radius output_link.
    result = _circle_circle_intersection(ax, ay, coupler, o4x, o4y, output_link)
    if result is None:
        raise ValueError(
            f"Mechanism cannot assemble at input_angle_deg={input_angle_deg:.2f} "
            f"(links {ground}, {input_link}, {coupler}, {output_link})"
        )

    sol_open, sol_crossed = result
    bx, by = sol_crossed if crossed else sol_open

    coupler_angle = math.atan2(by - ay, bx - ax)
    output_angle = math.atan2(by - o4y, bx - o4x)

    return {
        "output_angle_deg": _DEG(output_angle),
        "coupler_angle_deg": _DEG(coupler_angle),
        "output_link_angle_deg": _DEG(output_angle),
        "A_x": ax,
        "A_y": ay,
        "B_x": bx,
        "B_y": by,
    }


def transmission_angle(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
    input_angle_deg: float,
) -> float:
    """Return the transmission angle in degrees (0-180).

    The transmission angle *mu* is the acute/obtuse angle between the
    coupler and the output link at their moving joint.  Good designs keep
    min(mu) > 40 deg.
    """
    pos = solve_position(ground, input_link, coupler, output_link, input_angle_deg)

    theta3 = _RAD(pos["coupler_angle_deg"])
    theta4 = _RAD(pos["output_link_angle_deg"])

    mu = abs(theta3 - theta4)
    mu = mu % math.pi  # fold into [0, pi)
    return _DEG(mu)


def coupler_curve(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
    coupler_point_x: float = 0.0,
    coupler_point_y: float = 0.0,
    num_points: int = 360,
    *,
    input_angle_start: float = 0.0,
    input_angle_end: float = 360.0,
) -> list[list[float]]:
    """Trace a coupler curve.

    Parameters
    ----------
    coupler_point_x, coupler_point_y : float
        Coordinates of the traced point expressed in the coupler-link
        local frame.  Origin is at *A* (input crank end), x-axis along
        coupler toward *B*.
    num_points : int
        Number of samples over the sweep.
    input_angle_start, input_angle_end : float
        Sweep range in degrees.

    Returns
    -------
    list[list[float]]
        ``[[x, y], ...]`` world-frame coordinates of the coupler point.
    """
    points: list[list[float]] = []
    step = (input_angle_end - input_angle_start) / max(num_points - 1, 1)

    for i in range(num_points):
        angle = input_angle_start + i * step
        try:
            pos = solve_position(ground, input_link, coupler, output_link, angle)
        except ValueError:
            continue  # skip non-assemblable angles

        ax, ay = pos["A_x"], pos["A_y"]
        theta3 = _RAD(pos["coupler_angle_deg"])

        # Transform coupler point from local frame to world frame.
        cos3 = math.cos(theta3)
        sin3 = math.sin(theta3)
        wx = ax + coupler_point_x * cos3 - coupler_point_y * sin3
        wy = ay + coupler_point_x * sin3 + coupler_point_y * cos3
        points.append([round(wx, 6), round(wy, 6)])

    return points


def dead_point_detection(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
) -> list[float]:
    """Detect dead-point angles of the input crank (degrees).

    Dead points occur when the input link and coupler become collinear —
    either fully extended or fully folded.  At these angles the
    transmission angle is 0 or 180 deg and the mechanism can lock.
    """
    dead_points: list[float] = []

    # Extended configuration: diagonal = input + coupler
    d_ext = input_link + coupler
    # Folded configuration: diagonal = |input - coupler|
    d_fold = abs(input_link - coupler)

    for d in (d_ext, d_fold):
        # Law of cosines in the triangle O2-A-O4:
        # d^2 = ground^2 + input^2 - 2*ground*input*cos(theta2)
        # But d is the distance from O2 to O4 through A, which is already
        # the diagonal from O2 to B when coupler is collinear with input.
        # We need the diagonal from O4 to A being compatible with output_link:
        # Actually the dead-point condition is that input and coupler are
        # collinear, so the point B coincides with the line O2-A at
        # distance (input +/- coupler) from O2.
        #
        # For the mechanism to still close, O4-B must equal output_link,
        # i.e. we need a triangle O2-B-O4 with sides ground, d, output_link.
        cos_val_num = ground * ground + d * d - output_link * output_link
        cos_val_den = 2.0 * ground * d
        if abs(cos_val_den) < _EPS:
            continue
        cos_theta2 = cos_val_num / cos_val_den
        if abs(cos_theta2) > 1.0 + _EPS:
            continue
        cos_theta2 = max(-1.0, min(1.0, cos_theta2))
        theta2 = math.acos(cos_theta2)

        # Two symmetric solutions: +theta2 and -theta2 (= 360 - theta2).
        for angle in (theta2, _TWO_PI - theta2):
            deg = round(_DEG(_normalize_angle(angle)), 4)
            # Avoid duplicates (0 and 360 are the same).
            if not any(abs(deg - existing) < 0.01 for existing in dead_points):
                dead_points.append(deg)

    dead_points.sort()
    return dead_points


def _mechanical_advantage(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
    input_angle_deg: float,
) -> float:
    """Compute instantaneous mechanical advantage at an input angle.

    MA approx (output_link * sin(mu)) / (input_link * sin(alpha))
    where mu is the transmission angle and alpha is the angle between
    the input link and the coupler at their moving joint.
    """
    pos = solve_position(ground, input_link, coupler, output_link, input_angle_deg)

    theta2 = _RAD(input_angle_deg)
    theta3 = _RAD(pos["coupler_angle_deg"])
    theta4 = _RAD(pos["output_link_angle_deg"])

    mu = abs(theta3 - theta4)
    alpha = abs(theta3 - theta2 - math.pi)  # angle at A between input and coupler

    sin_mu = abs(math.sin(mu))
    sin_alpha = abs(math.sin(alpha))

    if sin_alpha < _EPS:
        return float("inf")  # dead point — infinite MA (locked)

    return (output_link * sin_mu) / (input_link * sin_alpha)


def _validate_link_lengths(
    ground: float,
    input_link: float,
    coupler: float,
    output_link: float,
) -> None:
    """Raise if link lengths cannot form a four-bar."""
    for name, val in [
        ("ground", ground),
        ("input_link", input_link),
        ("coupler", coupler),
        ("output_link", output_link),
    ]:
        if val <= 0.0:
            raise ValueError(f"{name} must be positive, got {val}")

    lengths = sorted([ground, input_link, coupler, output_link])
    if lengths[3] >= lengths[0] + lengths[1] + lengths[2]:
        raise ValueError(
            "Impossible four-bar: longest link exceeds the sum of the "
            f"other three ({lengths[3]:.4f} >= {sum(lengths[:3]):.4f})"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def four_bar_analysis(
    ground_length: float,
    input_length: float,
    coupler_length: float,
    output_length: float,
    coupler_point_x: float = 0.0,
    coupler_point_y: float = 0.0,
    input_angle_start: float = 0.0,
    input_angle_end: float = 360.0,
    num_points: int = 360,
) -> dict[str, Any]:
    """Full four-bar linkage analysis.

    Parameters
    ----------
    ground_length : float
        Fixed frame link length.
    input_length : float
        Input crank length.
    coupler_length : float
        Coupler link length.
    output_length : float
        Output (follower) link length.
    coupler_point_x, coupler_point_y : float
        Traced point in coupler-local coordinates (origin at A, x toward B).
    input_angle_start, input_angle_end : float
        Sweep range in degrees for the coupler curve.
    num_points : int
        Number of sample points for the coupler curve.

    Returns
    -------
    dict
        Keys: ``grashof_type``, ``elements``, ``transmission_angle_range``,
        ``dead_points``, ``mechanical_advantage_at_angles``, ``build_hint``.
    """
    g = ground_length
    a = input_length
    b = coupler_length
    c = output_length

    _validate_link_lengths(g, a, b, c)

    grashof = classify_grashof(g, a, b, c)
    dead_pts = dead_point_detection(g, a, b, c)

    # --- Coupler curve ---------------------------------------------------
    curve = coupler_curve(
        g, a, b, c,
        coupler_point_x, coupler_point_y,
        num_points,
        input_angle_start=input_angle_start,
        input_angle_end=input_angle_end,
    )

    # --- Transmission angle sweep ----------------------------------------
    mu_min = 180.0
    mu_max = 0.0
    step = (input_angle_end - input_angle_start) / max(num_points - 1, 1)
    for i in range(num_points):
        angle = input_angle_start + i * step
        try:
            mu = transmission_angle(g, a, b, c, angle)
        except ValueError:
            continue
        if mu < mu_min:
            mu_min = mu
        if mu > mu_max:
            mu_max = mu

    # --- Mechanical advantage at key angles ------------------------------
    ma_angles: list[dict[str, float]] = []
    sample_angles = [
        input_angle_start,
        input_angle_start + (input_angle_end - input_angle_start) * 0.25,
        input_angle_start + (input_angle_end - input_angle_start) * 0.50,
        input_angle_start + (input_angle_end - input_angle_start) * 0.75,
        input_angle_end,
    ]
    for angle in sample_angles:
        try:
            ma = _mechanical_advantage(g, a, b, c, angle)
            ma_angles.append({
                "angle_deg": round(angle, 2),
                "mechanical_advantage": round(ma, 4),
            })
        except ValueError:
            continue

    # --- Sketch elements (spline for coupler curve) ----------------------
    elements: list[dict[str, Any]] = []
    if len(curve) >= 2:
        elements.append({
            "type": "spline",
            "points": curve,
            "periodic": grashof in ("double_crank", "change_point"),
        })

    # --- Build hint ------------------------------------------------------
    if grashof == "double_crank":
        hint = (
            "Full-rotation input and output.  The coupler curve is a "
            "closed loop.  Use the spline element directly as a cam "
            "profile or guide rail."
        )
    elif grashof == "crank_rocker":
        hint = (
            "Full-rotation input, oscillating output.  The coupler "
            "curve is closed.  Useful for pick-and-place or oscillating "
            "mechanisms."
        )
    elif grashof == "double_rocker":
        hint = (
            "Both input and output oscillate (Grashof double-rocker).  "
            "The coupler curve is a partial arc.  Sweep range may be "
            "limited."
        )
    elif grashof == "change_point":
        hint = (
            "Change-point mechanism — transitions between configurations "
            "at dead points.  Requires a flywheel or spring to pass "
            "through singularities."
        )
    else:
        hint = (
            "Non-Grashof double-rocker — no link can fully rotate.  "
            "The input sweep is limited.  Use dead_points to determine "
            "the usable range."
        )

    return {
        "grashof_type": grashof,
        "elements": elements,
        "transmission_angle_range": {
            "min_deg": round(mu_min, 2),
            "max_deg": round(mu_max, 2),
        },
        "dead_points": dead_pts,
        "mechanical_advantage_at_angles": ma_angles,
        "build_hint": hint,
    }
