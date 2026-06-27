"""Turned profile (body of revolution) generator.

Generates a closed 2-D sketch profile for ``cad.revolution`` — suitable for
shafts, arbors, spindles, bushings, pins, standoffs, and any lathe-turned
part.  Pure Python — no Rust dependency.

The profile is a half-section drawn in the XY plane, where X is the axial
direction and Y is the radial direction (positive = away from axis).  The
revolution axis is the X axis at ``y = center_y``.
"""

from __future__ import annotations

import math
from typing import Any

from server.geometry_helpers import _arc, _line

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg_end_r(seg: dict[str, Any]) -> float:
    """End radius of a segment (accounts for taper)."""
    return seg.get("taper_diameter", seg["diameter"]) / 2.0


def _clamp_transition(seg: dict[str, Any], step: float) -> tuple[float, float]:
    """Return (fillet, chamfer) clamped to step height, max one non-zero."""
    f = min(seg.get("fillet", 0.0), step)
    c = min(seg.get("chamfer", 0.0), step)
    return (f, c)


# ---------------------------------------------------------------------------
# Transition emitters
# ---------------------------------------------------------------------------


def _emit_step_up(
    elements: list[dict[str, Any]],
    x_j: float,
    cy: float,
    r_prev: float,
    r_curr: float,
    fillet: float,
    chamfer: float,
) -> None:
    """Emit transition geometry for a step UP (r_curr > r_prev).

    Concave corner at (x_j, r_prev).  Fillet/chamfer eats from the
    **previous** segment end (eat_left).
    """
    if fillet > 0:
        f = fillet
        # Arc: center (x_j - f, cy + r_prev + f), radius f, 270° → 360°
        elements.append(_arc(x_j - f, cy + r_prev + f, f, 270.0, 360.0))
        if r_prev + f < r_curr - 1e-9:
            elements.append(_line(x_j, cy + r_prev + f, x_j, cy + r_curr))
    elif chamfer > 0:
        c = chamfer
        elements.append(_line(x_j - c, cy + r_prev, x_j, cy + r_prev + c))
        if r_prev + c < r_curr - 1e-9:
            elements.append(_line(x_j, cy + r_prev + c, x_j, cy + r_curr))
    else:
        elements.append(_line(x_j, cy + r_prev, x_j, cy + r_curr))


def _emit_step_down(
    elements: list[dict[str, Any]],
    x_j: float,
    cy: float,
    r_prev: float,
    r_curr: float,
    fillet: float,
    chamfer: float,
) -> None:
    """Emit transition geometry for a step DOWN (r_curr < r_prev).

    Concave corner at (x_j, r_curr).  Fillet/chamfer eats from the
    **current** segment start (eat_right).
    """
    if fillet > 0:
        f = fillet
        if r_prev - f > r_curr + 1e-9:
            elements.append(_line(x_j, cy + r_prev, x_j, cy + r_curr + f))
        # Arc: center (x_j + f, cy + r_curr + f), radius f, 180° → 270°
        elements.append(_arc(x_j + f, cy + r_curr + f, f, 180.0, 270.0))
    elif chamfer > 0:
        c = chamfer
        if r_prev - c > r_curr + 1e-9:
            elements.append(_line(x_j, cy + r_prev, x_j, cy + r_curr + c))
        elements.append(_line(x_j, cy + r_curr + c, x_j + c, cy + r_curr))
    else:
        elements.append(_line(x_j, cy + r_prev, x_j, cy + r_curr))


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def turned_profile(
    segments: list[dict[str, Any]],
    bore_diameter: float = 0.0,
    lead_chamfer: float = 0.0,
    trail_chamfer: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, Any]:
    """Generate a closed 2-D revolution profile from turned segments.

    Parameters
    ----------
    segments : list of dict
        Each dict has:

        - ``diameter`` (float, required): outer diameter of this section (mm).
        - ``length`` (float, required): axial length of this section (mm).
        - ``taper_diameter`` (float, optional): end diameter for tapered/conical
          sections.  If omitted, the section is cylindrical.
        - ``fillet`` (float, optional): fillet radius at the entry junction with
          the previous segment (concave corner stress relief).
        - ``chamfer`` (float, optional): 45° chamfer size at the entry junction.

        Only one of ``fillet`` or ``chamfer`` per segment.

    bore_diameter : float
        Center bore diameter in mm (0 = solid).
    lead_chamfer : float
        45° chamfer at the left (start) end of the shaft.
    trail_chamfer : float
        45° chamfer at the right (end) end of the shaft.
    center_x, center_y : float
        Offset for the revolution axis (profile bottom).

    Returns
    -------
    dict
        ``elements`` — closed sketch profile for ``cad.revolution``.
        Plus metadata: ``total_length``, ``max_diameter``, ``bore_diameter``,
        ``volume_mm3``, ``segment_count``, ``build_hint``.
    """
    if not segments:
        raise ValueError("segments must not be empty")

    n = len(segments)
    cx, cy = center_x, center_y
    r_bore = bore_diameter / 2.0
    r_axis = r_bore  # bottom of profile (bore or axis)

    # --- validate ---
    for i, seg in enumerate(segments):
        if "diameter" not in seg or "length" not in seg:
            raise ValueError(f"segment {i}: 'diameter' and 'length' required")
        if seg["diameter"] <= 0 or seg["length"] <= 0:
            raise ValueError(f"segment {i}: diameter and length must be positive")
        r_start = seg["diameter"] / 2.0
        r_end = _seg_end_r(seg)
        if r_start <= r_bore or r_end <= r_bore:
            raise ValueError(
                f"segment {i}: radius ({min(r_start, r_end):.3f}) must "
                f"exceed bore radius ({r_bore:.3f})"
            )
        if seg.get("fillet", 0) > 0 and seg.get("chamfer", 0) > 0:
            raise ValueError(f"segment {i}: specify fillet or chamfer, not both")

    # --- cumulative x positions ---
    x_pos = [0.0]
    for seg in segments:
        x_pos.append(x_pos[-1] + seg["length"])
    total_length = x_pos[-1]

    # --- compute eat amounts at each inter-segment junction ---
    # eat_left[j]: previous segment shortened at its end (step-up transitions)
    # eat_right[j]: current segment shortened at its start (step-down transitions)
    eat_left = [0.0] * (n + 1)
    eat_right = [0.0] * (n + 1)

    for j in range(1, n):
        r_prev = _seg_end_r(segments[j - 1])
        r_curr = segments[j]["diameter"] / 2.0
        if abs(r_prev - r_curr) < 1e-9:
            continue
        step = abs(r_curr - r_prev)
        f, c = _clamp_transition(segments[j], step)
        t = max(f, c)
        if r_curr > r_prev:
            eat_left[j] = t  # step up: eats from previous end
        else:
            eat_right[j] = t  # step down: eats from current start

    # --- lead / trail chamfer sizing ---
    r_first = segments[0]["diameter"] / 2.0
    r_last_end = _seg_end_r(segments[-1])
    lead_c = min(lead_chamfer, r_first - r_axis) if lead_chamfer > 0 else 0.0
    trail_c = min(trail_chamfer, r_last_end - r_axis) if trail_chamfer > 0 else 0.0

    elements: list[dict[str, Any]] = []

    # =================================================================
    # LEFT EDGE  (bottom → top)
    # =================================================================
    if lead_c > 0:
        elements.append(_line(cx, cy + r_axis, cx, cy + r_first - lead_c))
        elements.append(_line(cx, cy + r_first - lead_c, cx + lead_c, cy + r_first))
    else:
        elements.append(_line(cx, cy + r_axis, cx, cy + r_first))

    # =================================================================
    # TOP OUTLINE  (left → right across all segments)
    # =================================================================
    for i in range(n):
        seg = segments[i]
        seg_r = seg["diameter"] / 2.0
        end_r = _seg_end_r(seg)
        x_start = cx + x_pos[i]
        x_end = cx + x_pos[i + 1]

        # Effective body start (adjusted by lead chamfer or step-down eat)
        if i == 0:
            x_body_start = cx + lead_c
        else:
            x_body_start = x_start + eat_right[i]

        # Effective body end (adjusted by trail chamfer or step-up eat)
        if i == n - 1:
            x_body_end = x_end - trail_c
        else:
            x_body_end = x_end - eat_left[i + 1]

        # --- Transition at entry junction i (between segment i-1 and i) ---
        if i > 0:
            r_prev = _seg_end_r(segments[i - 1])
            if abs(r_prev - seg_r) > 1e-9:
                step = abs(seg_r - r_prev)
                f, c = _clamp_transition(seg, step)
                if seg_r > r_prev:
                    _emit_step_up(elements, x_start, cy, r_prev, seg_r, f, c)
                else:
                    _emit_step_down(elements, x_start, cy, r_prev, seg_r, f, c)

        # --- Segment body (horizontal or taper) ---
        elements.append(_line(x_body_start, cy + seg_r, x_body_end, cy + end_r))

    # =================================================================
    # RIGHT EDGE  (top → bottom)
    # =================================================================
    if trail_c > 0:
        x_right = cx + total_length
        elements.append(
            _line(x_right - trail_c, cy + r_last_end, x_right, cy + r_last_end - trail_c)
        )
        if r_last_end - trail_c > r_axis + 1e-9:
            elements.append(_line(x_right, cy + r_last_end - trail_c, x_right, cy + r_axis))
    else:
        elements.append(_line(cx + total_length, cy + r_last_end, cx + total_length, cy + r_axis))

    # =================================================================
    # BOTTOM EDGE  (right → left along bore / axis)
    # =================================================================
    elements.append(_line(cx + total_length, cy + r_axis, cx, cy + r_axis))

    # --- metadata ---
    max_d = max(
        max(seg["diameter"], seg.get("taper_diameter", seg["diameter"])) for seg in segments
    )
    min_d = min(
        min(seg["diameter"], seg.get("taper_diameter", seg["diameter"])) for seg in segments
    )

    # Approximate volume (frustum sum minus bore)
    volume = 0.0
    for seg in segments:
        r1 = seg["diameter"] / 2.0
        r2 = _seg_end_r(seg)
        length = seg["length"]
        volume += math.pi * length / 3.0 * (r1**2 + r2**2 + r1 * r2)
    volume -= math.pi * r_bore**2 * total_length

    return {
        "elements": elements,
        "total_length": round(total_length, 4),
        "max_diameter": round(max_d, 4),
        "min_diameter": round(min_d, 4),
        "bore_diameter": bore_diameter,
        "volume_mm3": round(volume, 2),
        "segment_count": n,
        "build_hint": (
            "cad.sketch with these elements → "
            "cad.revolution(axis='x', angle=360). "
            + (
                f"Profile includes a Ø{bore_diameter} mm center bore."
                if bore_diameter > 0
                else "Add a center bore with cad.hole if needed."
            )
        ),
    }
