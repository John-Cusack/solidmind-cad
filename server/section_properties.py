"""Structural cross-section analysis — pure computation, no geometry output.

Closed-form formulas for standard shapes plus arbitrary polygon support
via the shoelace formula and parallel-axis theorem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SectionResult:
    """Computed cross-section properties."""

    area: float
    centroid_x: float
    centroid_y: float
    Ixx: float
    Iyy: float
    Ixy: float
    Sx: float
    Sy: float
    rx: float
    ry: float


def _result_dict(r: SectionResult) -> dict[str, float]:
    """Convert SectionResult to a plain dict."""
    return {
        "area": r.area,
        "centroid_x": r.centroid_x,
        "centroid_y": r.centroid_y,
        "Ixx": r.Ixx,
        "Iyy": r.Iyy,
        "Ixy": r.Ixy,
        "Sx": r.Sx,
        "Sy": r.Sy,
        "rx": r.rx,
        "ry": r.ry,
    }


# ---------------------------------------------------------------------------
# Standard shapes
# ---------------------------------------------------------------------------


def rectangle(width: float, height: float) -> SectionResult:
    """Solid rectangle, origin at centroid."""
    a = width * height
    ixx = width * height**3 / 12.0
    iyy = height * width**3 / 12.0
    return SectionResult(
        area=a,
        centroid_x=0.0,
        centroid_y=0.0,
        Ixx=ixx,
        Iyy=iyy,
        Ixy=0.0,
        Sx=ixx / (height / 2.0),
        Sy=iyy / (width / 2.0),
        rx=math.sqrt(ixx / a),
        ry=math.sqrt(iyy / a),
    )


def circle(diameter: float) -> SectionResult:
    """Solid circle."""
    r = diameter / 2.0
    a = math.pi * r**2
    i = math.pi * diameter**4 / 64.0
    return SectionResult(
        area=a,
        centroid_x=0.0,
        centroid_y=0.0,
        Ixx=i,
        Iyy=i,
        Ixy=0.0,
        Sx=i / r,
        Sy=i / r,
        rx=math.sqrt(i / a),
        ry=math.sqrt(i / a),
    )


def hollow_circle(outer_diameter: float, inner_diameter: float) -> SectionResult:
    """Hollow circular tube."""
    if inner_diameter >= outer_diameter:
        raise ValueError("inner_diameter must be less than outer_diameter")
    ro = outer_diameter / 2.0
    ri = inner_diameter / 2.0
    a = math.pi * (ro**2 - ri**2)
    i = math.pi * (outer_diameter**4 - inner_diameter**4) / 64.0
    return SectionResult(
        area=a,
        centroid_x=0.0,
        centroid_y=0.0,
        Ixx=i,
        Iyy=i,
        Ixy=0.0,
        Sx=i / ro,
        Sy=i / ro,
        rx=math.sqrt(i / a),
        ry=math.sqrt(i / a),
    )


def i_beam(
    flange_width: float,
    flange_thickness: float,
    web_height: float,
    web_thickness: float,
) -> SectionResult:
    """I-beam (wide-flange) symmetric about both axes.

    Layout (bottom to top): bottom flange, web, top flange.
    Total height = web_height + 2 * flange_thickness.
    """
    bf = flange_width
    tf = flange_thickness
    hw = web_height
    tw = web_thickness
    h_total = hw + 2.0 * tf

    # Areas
    a_flange = bf * tf
    a_web = tw * hw
    a_total = 2.0 * a_flange + a_web

    # Centroid at mid-height by symmetry
    cy = h_total / 2.0

    # Ixx about centroid (parallel axis)
    ixx_web = tw * hw**3 / 12.0
    ixx_flange_self = bf * tf**3 / 12.0
    d_flange = (hw + tf) / 2.0  # distance from centroid to flange centroid
    ixx = ixx_web + 2.0 * (ixx_flange_self + a_flange * d_flange**2)

    # Iyy about centroid
    iyy_web = hw * tw**3 / 12.0
    iyy_flange = tf * bf**3 / 12.0
    iyy = iyy_web + 2.0 * iyy_flange

    ymax = h_total / 2.0
    xmax = bf / 2.0

    return SectionResult(
        area=a_total,
        centroid_x=0.0,
        centroid_y=cy,
        Ixx=ixx,
        Iyy=iyy,
        Ixy=0.0,
        Sx=ixx / ymax,
        Sy=iyy / xmax,
        rx=math.sqrt(ixx / a_total),
        ry=math.sqrt(iyy / a_total),
    )


def c_channel(
    flange_width: float,
    flange_thickness: float,
    web_height: float,
    web_thickness: float,
) -> SectionResult:
    """C-channel (American standard channel).

    Web on the left, flanges extend to the right.
    Total height = web_height + 2 * flange_thickness.
    """
    bf = flange_width
    tf = flange_thickness
    hw = web_height
    tw = web_thickness
    h_total = hw + 2.0 * tf

    # Component areas
    a_web = tw * hw
    a_flange = bf * tf
    a_total = a_web + 2.0 * a_flange

    # Centroid Y: symmetric about horizontal axis
    cy = h_total / 2.0

    # Centroid X: web centroid at tw/2, flange centroid at tw + (bf-tw)/2
    # (flanges include the web corner region for simplicity — standard
    #  decomposition: web is tw×hw, each flange is bf×tf)
    tw / 2.0
    (tw + bf) / 2.0  # flange extends from 0 to tw+bf... actually:
    # Better decomposition: web = tw × hw, flanges = bf × tf with flange
    # starting at x=0.  Flange centroid at bf/2.
    bf / 2.0
    cx = (a_web * (tw / 2.0) + 2.0 * a_flange * (bf / 2.0)) / a_total

    # Ixx about centroidal horizontal axis (symmetric)
    ixx_web = tw * hw**3 / 12.0
    ixx_flange_self = bf * tf**3 / 12.0
    d_flange_y = (hw + tf) / 2.0
    ixx = ixx_web + 2.0 * (ixx_flange_self + a_flange * d_flange_y**2)

    # Iyy about centroidal vertical axis (parallel axis)
    iyy_web_self = hw * tw**3 / 12.0
    d_web_x = tw / 2.0 - cx
    iyy_web = iyy_web_self + a_web * d_web_x**2

    iyy_flange_self = tf * bf**3 / 12.0
    d_flange_x = bf / 2.0 - cx
    iyy_flange = iyy_flange_self + a_flange * d_flange_x**2
    iyy = iyy_web + 2.0 * iyy_flange

    ymax = h_total / 2.0
    # xmax: furthest point from centroid X
    xmax = max(cx, bf - cx, tw - cx)
    # For a channel the flange tip at x=bf is typically furthest
    xmax_right = bf - cx
    xmax_left = cx
    xmax = max(xmax_right, xmax_left)

    return SectionResult(
        area=a_total,
        centroid_x=cx,
        centroid_y=cy,
        Ixx=ixx,
        Iyy=iyy,
        Ixy=0.0,
        Sx=ixx / ymax,
        Sy=iyy / xmax,
        rx=math.sqrt(ixx / a_total),
        ry=math.sqrt(iyy / a_total),
    )


def angle(
    leg1_length: float,
    leg2_length: float,
    thickness: float,
) -> SectionResult:
    """L-section (equal or unequal angle).

    Leg 1 is horizontal (along X), leg 2 is vertical (along Y).
    Corner at origin.
    """
    t = thickness
    l1 = leg1_length
    l2 = leg2_length

    # Decompose into two rectangles (overlap at corner included in leg1)
    # Leg1: horizontal, l1 × t, centroid at (l1/2, t/2)
    # Leg2: vertical (above leg1), t × (l2 - t), centroid at (t/2, t + (l2-t)/2)
    a1 = l1 * t
    a2 = t * (l2 - t)
    a_total = a1 + a2

    cx = (a1 * l1 / 2.0 + a2 * t / 2.0) / a_total
    cy = (a1 * t / 2.0 + a2 * (t + (l2 - t) / 2.0)) / a_total

    # Ixx about centroid
    ixx1_self = l1 * t**3 / 12.0
    d1y = t / 2.0 - cy
    ixx2_self = t * (l2 - t) ** 3 / 12.0
    d2y = (t + (l2 - t) / 2.0) - cy
    ixx = ixx1_self + a1 * d1y**2 + ixx2_self + a2 * d2y**2

    # Iyy about centroid
    iyy1_self = t * l1**3 / 12.0
    d1x = l1 / 2.0 - cx
    iyy2_self = (l2 - t) * t**3 / 12.0
    d2x = t / 2.0 - cx
    iyy = iyy1_self + a1 * d1x**2 + iyy2_self + a2 * d2x**2

    # Ixy about centroid
    ixy1 = a1 * d1x * d1y
    ixy2 = a2 * d2x * d2y
    ixy = ixy1 + ixy2

    ymax = max(cy, l2 - cy)
    xmax = max(cx, l1 - cx)

    return SectionResult(
        area=a_total,
        centroid_x=cx,
        centroid_y=cy,
        Ixx=ixx,
        Iyy=iyy,
        Ixy=ixy,
        Sx=ixx / ymax,
        Sy=iyy / xmax,
        rx=math.sqrt(ixx / a_total),
        ry=math.sqrt(iyy / a_total),
    )


def t_section(
    flange_width: float,
    flange_thickness: float,
    web_height: float,
    web_thickness: float,
) -> SectionResult:
    """T-section (flange on top, web below).

    Flange centered on web.  Total height = web_height + flange_thickness.
    """
    bf = flange_width
    tf = flange_thickness
    hw = web_height
    tw = web_thickness
    h_total = hw + tf

    a_flange = bf * tf
    a_web = tw * hw
    a_total = a_flange + a_web

    # Y from bottom of web
    cy = (a_web * hw / 2.0 + a_flange * (hw + tf / 2.0)) / a_total
    cx = 0.0  # symmetric about vertical axis

    # Ixx about centroid
    ixx_web_self = tw * hw**3 / 12.0
    d_web = hw / 2.0 - cy
    ixx_flange_self = bf * tf**3 / 12.0
    d_flange = (hw + tf / 2.0) - cy
    ixx = ixx_web_self + a_web * d_web**2 + ixx_flange_self + a_flange * d_flange**2

    # Iyy about centroid (symmetric)
    iyy_web = hw * tw**3 / 12.0
    iyy_flange = tf * bf**3 / 12.0
    iyy = iyy_web + iyy_flange

    ymax = max(cy, h_total - cy)
    xmax = bf / 2.0

    return SectionResult(
        area=a_total,
        centroid_x=cx,
        centroid_y=cy,
        Ixx=ixx,
        Iyy=iyy,
        Ixy=0.0,
        Sx=ixx / ymax,
        Sy=iyy / xmax,
        rx=math.sqrt(ixx / a_total),
        ry=math.sqrt(iyy / a_total),
    )


# ---------------------------------------------------------------------------
# Arbitrary polygon
# ---------------------------------------------------------------------------


def polygon(vertices: list[list[float]]) -> SectionResult:
    """Compute section properties for an arbitrary polygon.

    Uses the shoelace formula for area/centroid and the second-moment
    integrals for a simple (non-self-intersecting) polygon.

    Parameters
    ----------
    vertices:
        List of ``[x, y]`` coordinate pairs defining the polygon boundary
        in order (clockwise or counter-clockwise).  The polygon is
        automatically closed.
    """
    n = len(vertices)
    if n < 3:
        raise ValueError("Polygon requires at least 3 vertices")

    # Ensure we work with float tuples
    pts = [(float(v[0]), float(v[1])) for v in vertices]

    # Signed area (shoelace)
    signed_area = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        signed_area += x0 * y1 - x1 * y0
    signed_area /= 2.0
    area = abs(signed_area)
    if area < 1e-12:
        raise ValueError("Degenerate polygon (zero area)")

    # Centroid
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    cx /= 6.0 * signed_area
    cy /= 6.0 * signed_area

    # Second moments about centroid
    ixx = 0.0
    iyy = 0.0
    ixy = 0.0
    for i in range(n):
        x0, y0 = pts[i][0] - cx, pts[i][1] - cy
        x1, y1 = pts[(i + 1) % n][0] - cx, pts[(i + 1) % n][1] - cy
        cross = x0 * y1 - x1 * y0
        ixx += (y0**2 + y0 * y1 + y1**2) * cross
        iyy += (x0**2 + x0 * x1 + x1**2) * cross
        ixy += (x0 * y1 + 2.0 * x0 * y0 + 2.0 * x1 * y1 + x1 * y0) * cross
    ixx = abs(ixx) / 12.0
    iyy = abs(iyy) / 12.0
    ixy = ixy / 24.0

    # Extreme distances from centroid
    ymax = max(abs(v[1] - cy) for v in pts)
    xmax = max(abs(v[0] - cx) for v in pts)
    ymax = max(ymax, 1e-12)
    xmax = max(xmax, 1e-12)

    return SectionResult(
        area=area,
        centroid_x=cx,
        centroid_y=cy,
        Ixx=ixx,
        Iyy=iyy,
        Ixy=ixy,
        Sx=ixx / ymax,
        Sy=iyy / xmax,
        rx=math.sqrt(ixx / area),
        ry=math.sqrt(iyy / area),
    )


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

_SHAPE_DISPATCH: dict[str, Any] = {
    "rectangle": rectangle,
    "circle": circle,
    "hollow_circle": hollow_circle,
    "i_beam": i_beam,
    "c_channel": c_channel,
    "angle": angle,
    "t_section": t_section,
    "polygon": polygon,
}


def compute_section(shape: str, **params: Any) -> dict[str, float]:
    """Compute cross-section properties for a named shape.

    Parameters
    ----------
    shape:
        Shape name — one of ``"rectangle"``, ``"circle"``,
        ``"hollow_circle"``, ``"i_beam"``, ``"c_channel"``, ``"angle"``,
        ``"t_section"``, ``"polygon"``.
    **params:
        Shape-specific keyword arguments forwarded to the shape function.

    Returns
    -------
    dict with keys: ``area``, ``centroid_x``, ``centroid_y``,
    ``Ixx``, ``Iyy``, ``Ixy``, ``Sx``, ``Sy``, ``rx``, ``ry``.
    """
    func = _SHAPE_DISPATCH.get(shape)
    if func is None:
        valid = ", ".join(sorted(_SHAPE_DISPATCH.keys()))
        raise ValueError(f"Unknown shape '{shape}'. Valid shapes: {valid}")
    result = func(**params)
    return _result_dict(result)
