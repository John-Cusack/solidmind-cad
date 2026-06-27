"""ISO 286 press-fit bore dimensioning and profile generation.

Computes bore tolerances for clearance, transition, and interference fits
per ISO 286.  Generates a bore cross-section profile (half-section for
revolution) with optional chamfer and counterbore.  Pure Python — no Rust
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.geometry_helpers import _line

# ---------------------------------------------------------------------------
# ISO 286 tolerance data
# ---------------------------------------------------------------------------

# Diameter range breakpoints (mm).  Each range is (low, high].
_RANGES: list[tuple[float, float]] = [
    (0, 3),
    (3, 6),
    (6, 10),
    (10, 18),
    (18, 30),
    (30, 50),
    (50, 80),
    (80, 120),
    (120, 180),
    (180, 250),
    (250, 315),
    (315, 400),
]


def _range_index(d: float) -> int:
    """Return the index into _RANGES for nominal diameter *d*."""
    if d <= 0:
        raise ValueError(f"nominal diameter must be positive, got {d}")
    for i, (_lo, hi) in enumerate(_RANGES):
        if d <= hi:
            return i
    raise ValueError(f"nominal diameter {d} mm exceeds 400 mm limit")


# IT grade values in μm, indexed [range_index].  Grades 5–9.
_IT: dict[int, list[int]] = {
    5: [4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25],
    6: [6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36],
    7: [10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57],
    8: [14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89],
    9: [25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140],
}


def _it_value(grade: int, idx: int) -> int:
    """IT tolerance in μm for a given grade and range index."""
    if grade not in _IT:
        raise ValueError(f"IT grade {grade} not supported (5–9 available)")
    return _IT[grade][idx]


# Fundamental deviations for shaft letter positions (μm).
# Positions f–h: value is the UPPER deviation (always ≤ 0).
# Positions k–zc: value is the LOWER deviation (always ≥ 0).
_SHAFT_DEV: dict[str, list[int]] = {
    "f": [-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62],
    "g": [-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18],
    "h": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "k": [0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4],
    "m": [2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21],
    "n": [4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37],
    "p": [6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62],
    "r": [10, 15, 19, 23, 28, 34, 41, 48, 55, 63, 72, 78],
    "s": [14, 19, 23, 28, 35, 43, 53, 59, 68, 79, 88, 98],
}


# ---------------------------------------------------------------------------
# Tolerance zone computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToleranceZone:
    """Deviation band in μm from nominal."""

    lower_dev_um: float
    upper_dev_um: float
    grade: int
    position: str

    @property
    def tolerance_um(self) -> float:
        return self.upper_dev_um - self.lower_dev_um


def _hole_zone(position: str, grade: int, idx: int) -> ToleranceZone:
    """Compute tolerance zone for a hole (capital letter)."""
    pos = position.upper()
    tol = _it_value(grade, idx)
    if pos == "H":
        return ToleranceZone(0.0, float(tol), grade, pos)
    raise ValueError(f"Hole position '{pos}' not supported (only H)")


def _shaft_zone(position: str, grade: int, idx: int) -> ToleranceZone:
    """Compute tolerance zone for a shaft (lowercase letter)."""
    pos = position.lower()
    tol = _it_value(grade, idx)
    if pos not in _SHAFT_DEV:
        valid = ", ".join(sorted(_SHAFT_DEV))
        raise ValueError(f"Shaft position '{pos}' not in table ({valid})")
    fund_dev = _SHAFT_DEV[pos][idx]
    if pos in ("f", "g", "h"):
        # Fundamental deviation is upper deviation
        upper = float(fund_dev)
        lower = upper - tol
    else:
        # Fundamental deviation is lower deviation
        lower = float(fund_dev)
        upper = lower + tol
    return ToleranceZone(lower, upper, grade, pos)


def _parse_class(spec: str) -> tuple[str, int]:
    """Parse 'H7' → ('H', 7) or 'p6' → ('p', 6)."""
    if len(spec) < 2:
        raise ValueError(f"Invalid tolerance class: '{spec}'")
    letter = spec[0]
    try:
        grade = int(spec[1:])
    except ValueError as exc:
        raise ValueError(f"Invalid tolerance class: '{spec}'") from exc
    return letter, grade


# ---------------------------------------------------------------------------
# Fit presets
# ---------------------------------------------------------------------------

_FIT_PRESETS: dict[str, dict[str, str]] = {
    "clearance": {"hole": "H8", "shaft": "f7", "desc": "Free-running fit, wide clearance"},
    "close_clearance": {"hole": "H7", "shaft": "g6", "desc": "Close-running fit, small clearance"},
    "sliding": {"hole": "H7", "shaft": "h6", "desc": "Sliding fit, precise location"},
    "transition": {"hole": "H7", "shaft": "k6", "desc": "Transition fit, light tap to assemble"},
    "press": {"hole": "H7", "shaft": "p6", "desc": "Light press fit, arbor press required"},
    "medium_press": {"hole": "H7", "shaft": "r6", "desc": "Medium press, permanent assembly"},
    "heavy_press": {"hole": "H7", "shaft": "s6", "desc": "Heavy press / shrink fit"},
}


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def press_fit_bore(
    nominal_diameter: float,
    fit: str = "press",
    depth: float = 10.0,
    chamfer: float = 0.0,
    counterbore_diameter: float = 0.0,
    counterbore_depth: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, Any]:
    """Compute bore dimensions for a specified fit and generate profile.

    Parameters
    ----------
    nominal_diameter : float
        Nominal diameter of the mating shaft / pin / bearing (mm).
    fit : str
        Fit preset name (``clearance``, ``close_clearance``, ``sliding``,
        ``transition``, ``press``, ``medium_press``, ``heavy_press``) or
        an ISO pair like ``H7p6``.
    depth : float
        Bore depth in mm.  0 = return dimensions only (no profile).
    chamfer : float
        Entry chamfer at 45° (mm).  Applied to the outermost opening.
    counterbore_diameter : float
        Optional counterbore outer diameter (mm).  Must exceed
        ``nominal_diameter``.  0 = no counterbore.
    counterbore_depth : float
        Counterbore depth (mm).
    center_x, center_y : float
        Offset for the revolution axis.

    Returns
    -------
    dict
        ``elements`` (bore half-section for revolution), tolerance data,
        fit characteristics, and ``build_hint``.
    """
    if nominal_diameter <= 0:
        raise ValueError("nominal_diameter must be positive")
    if depth < 0:
        raise ValueError("depth must be non-negative")

    idx = _range_index(nominal_diameter)

    # --- Resolve fit to hole + shaft tolerance classes ---
    if fit in _FIT_PRESETS:
        preset = _FIT_PRESETS[fit]
        hole_class = preset["hole"]
        shaft_class = preset["shaft"]
        fit_description = preset["desc"]
    else:
        # Try parsing as ISO pair like "H7p6" or "H7/p6"
        cleaned = fit.replace("/", "").replace(" ", "")
        # Find the boundary: uppercase → lowercase transition
        split = -1
        for i in range(1, len(cleaned)):
            if cleaned[i].islower() and cleaned[i - 1].isdigit():
                split = i
                break
        if split < 0:
            # Maybe just a hole class like "H7"
            hole_class = cleaned
            shaft_class = "h6"  # default mating shaft
            fit_description = f"Hole {hole_class}, shaft h6 (default)"
        else:
            hole_class = cleaned[:split]
            shaft_class = cleaned[split:]
            fit_description = f"ISO {hole_class}/{shaft_class}"

    h_pos, h_grade = _parse_class(hole_class)
    s_pos, s_grade = _parse_class(shaft_class)

    hole = _hole_zone(h_pos, h_grade, idx)
    shaft = _shaft_zone(s_pos, s_grade, idx)

    # --- Bore dimensions ---
    bore_min = nominal_diameter + hole.lower_dev_um / 1000.0
    bore_max = nominal_diameter + hole.upper_dev_um / 1000.0
    bore_target = (bore_min + bore_max) / 2.0

    shaft_min = nominal_diameter + shaft.lower_dev_um / 1000.0
    shaft_max = nominal_diameter + shaft.upper_dev_um / 1000.0

    # Fit characteristics (positive = clearance, negative = interference)
    clearance_max_um = hole.upper_dev_um - shaft.lower_dev_um
    clearance_min_um = hole.lower_dev_um - shaft.upper_dev_um

    # --- Profile generation ---
    cx, cy = center_x, center_y
    elements: list[dict[str, Any]] = []
    bore_r = bore_target / 2.0

    if depth > 0:
        has_cb = counterbore_diameter > 0 and counterbore_depth > 0
        cb_r = counterbore_diameter / 2.0 if has_cb else bore_r

        if has_cb and counterbore_diameter <= nominal_diameter:
            raise ValueError(
                f"counterbore_diameter ({counterbore_diameter}) must exceed "
                f"nominal_diameter ({nominal_diameter})"
            )

        # Determine the outermost entry radius (for chamfer)
        entry_r = cb_r if has_cb else bore_r
        cham = min(chamfer, entry_r) if chamfer > 0 else 0.0

        # Build half-section (X = depth direction, Y = radial)
        # Going clockwise: entry top → bore bottom → axis → back to entry

        x = cx
        r = entry_r

        # 1. Entry with optional chamfer
        if cham > 0:
            elements.append(_line(x, cy + r + cham, x + cham, cy + r))
            x += cham
        else:
            # vertical from axis side at entry
            pass  # we'll start the outline from here

        # 2. Counterbore body (if present)
        if has_cb:
            cb_end_x = cx + counterbore_depth
            # Horizontal along counterbore
            elements.append(_line(x, cy + cb_r, cb_end_x, cy + cb_r))
            # Step down to bore radius
            elements.append(_line(cb_end_x, cy + cb_r, cb_end_x, cy + bore_r))
            x = cb_end_x

        # 3. Main bore body
        elements.append(_line(x, cy + bore_r, cx + depth, cy + bore_r))

        # 4. Bottom of bore (radial line to axis)
        elements.append(_line(cx + depth, cy + bore_r, cx + depth, cy))

        # 5. Along axis back to entry
        elements.append(_line(cx + depth, cy, cx, cy))

        # 6. Left edge: axis up to entry
        if cham > 0:
            elements.append(_line(cx, cy, cx, cy + entry_r + cham))
        elif has_cb:
            elements.append(_line(cx, cy, cx, cy + cb_r))
        else:
            elements.append(_line(cx, cy, cx, cy + bore_r))

    return {
        "elements": elements,
        "nominal_diameter": nominal_diameter,
        "bore_diameter_min": round(bore_min, 4),
        "bore_diameter_max": round(bore_max, 4),
        "bore_diameter_target": round(bore_target, 4),
        "hole_tolerance_class": hole_class,
        "hole_tolerance_um": hole.tolerance_um,
        "hole_lower_dev_um": hole.lower_dev_um,
        "hole_upper_dev_um": hole.upper_dev_um,
        "shaft_tolerance_class": shaft_class,
        "shaft_min": round(shaft_min, 4),
        "shaft_max": round(shaft_max, 4),
        "clearance_min_um": clearance_min_um,
        "clearance_max_um": clearance_max_um,
        "fit_type": "clearance"
        if clearance_min_um > 0
        else "interference"
        if clearance_max_um < 0
        else "transition",
        "fit_description": fit_description,
        "build_hint": (
            f"Bore to Ø{bore_target:.3f} mm "
            f"(tolerance {hole_class}: "
            f"+{hole.lower_dev_um:.0f}/+{hole.upper_dev_um:.0f} μm). "
            "Use cad.hole for simple bores, or revolve the profile "
            "for stepped / counterbored bores."
        ),
    }
