"""Standard rectangular keyway pocket geometry per shaft diameter.

Lookup tables for DIN 6885, ANSI B17.1, and Woodruff key standards.
Returns sketch elements and build hints for CAD integration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class KeywaySpec:
    """Key cross-section dimensions in mm."""

    width: float
    height: float
    shaft_depth: float
    hub_depth: float


# ---------------------------------------------------------------------------
# DIN 6885 — Parallel keys for metric shafts
# Each entry: (min_shaft_dia, max_shaft_dia) → (key_width, key_height)
# ---------------------------------------------------------------------------
_DIN6885_RANGES: list[tuple[float, float, float, float]] = [
    (6, 8, 2, 2),
    (8, 10, 3, 3),
    (10, 12, 4, 4),
    (12, 17, 5, 5),
    (17, 22, 6, 6),
    (22, 30, 8, 7),
    (30, 38, 10, 8),
    (38, 44, 12, 8),
    (44, 50, 14, 9),
    (50, 58, 16, 10),
    (58, 65, 18, 11),
    (65, 75, 20, 12),
    (75, 85, 22, 14),
    (85, 95, 25, 14),
    (95, 110, 28, 16),
    (110, 130, 32, 18),
]

# ---------------------------------------------------------------------------
# ANSI B17.1 — Parallel keys for inch shafts (common sizes)
# shaft_dia_inch → (key_width_inch, key_height_inch)
# ---------------------------------------------------------------------------
_ANSI_B17_1: dict[str, tuple[float, float]] = {
    "1/4": (1 / 16, 1 / 16),
    "5/16": (1 / 16, 1 / 16),
    "3/8": (3 / 32, 3 / 32),
    "7/16": (3 / 32, 3 / 32),
    "1/2": (1 / 8, 1 / 8),
    "9/16": (1 / 8, 1 / 8),
    "5/8": (3 / 16, 3 / 16),
    "3/4": (3 / 16, 3 / 16),
    "7/8": (3 / 16, 3 / 16),
    "1": (1 / 4, 1 / 4),
    "1-1/4": (1 / 4, 1 / 4),
    "1-3/8": (5 / 16, 5 / 16),
    "1-1/2": (3 / 8, 3 / 8),
    "1-3/4": (3 / 8, 3 / 8),
    "2": (1 / 2, 1 / 2),
    "2-1/4": (1 / 2, 1 / 2),
    "2-1/2": (5 / 8, 5 / 8),
    "2-3/4": (5 / 8, 5 / 8),
    "3": (3 / 4, 3 / 4),
    "3-1/2": (7 / 8, 7 / 8),
    "4": (1, 1),
}

# ---------------------------------------------------------------------------
# Woodruff keys — half-moon keys (ANSI B17.2 / DIN 6888)
# key_number → (width_mm, diameter_mm, height_mm)
# Woodruff key number encodes dimensions: last two digits = diameter/8 inch,
# preceding digits = width/32 inch.  Table below in metric equivalents.
# ---------------------------------------------------------------------------
_WOODRUFF: dict[str, tuple[float, float, float]] = {
    "202": (1.59, 6.35, 2.38),
    "203": (1.59, 9.53, 3.57),
    "204": (1.59, 12.70, 4.76),
    "303": (2.38, 9.53, 3.57),
    "304": (2.38, 12.70, 4.76),
    "305": (2.38, 15.88, 5.56),
    "404": (3.18, 12.70, 4.76),
    "405": (3.18, 15.88, 5.56),
    "406": (3.18, 19.05, 6.35),
    "505": (3.97, 15.88, 5.56),
    "506": (3.97, 19.05, 6.35),
    "507": (3.97, 22.23, 7.14),
    "606": (4.76, 19.05, 6.35),
    "607": (4.76, 22.23, 7.14),
    "608": (4.76, 25.40, 7.94),
    "807": (6.35, 22.23, 7.14),
    "808": (6.35, 25.40, 7.94),
    "809": (6.35, 28.58, 8.73),
    "810": (6.35, 31.75, 9.53),
    "1008": (7.94, 25.40, 7.94),
    "1009": (7.94, 28.58, 8.73),
    "1010": (7.94, 31.75, 9.53),
    "1011": (7.94, 34.93, 10.32),
    "1012": (7.94, 38.10, 11.11),
    "1210": (9.53, 31.75, 9.53),
    "1211": (9.53, 34.93, 10.32),
    "1212": (9.53, 38.10, 11.11),
}

_CLEARANCE_MM = 0.1


def _lookup_din6885(shaft_dia: float) -> tuple[float, float]:
    """Return (key_width, key_height) for a metric shaft diameter."""
    for lo, hi, w, h in _DIN6885_RANGES:
        if lo <= shaft_dia < hi:
            return (w, h)
    if math.isclose(shaft_dia, 130.0):
        return (32, 18)
    raise ValueError(f"Shaft diameter {shaft_dia} mm outside DIN 6885 range (6-130 mm)")


def _lookup_ansi(shaft_dia_inch: str) -> tuple[float, float]:
    """Return (key_width_inch, key_height_inch) for an imperial shaft."""
    spec = _ANSI_B17_1.get(shaft_dia_inch)
    if spec is None:
        valid = ", ".join(sorted(_ANSI_B17_1.keys(), key=_frac_to_float))
        raise ValueError(
            f"Shaft diameter '{shaft_dia_inch}' not in ANSI B17.1 table. Valid sizes: {valid}"
        )
    return spec


def _frac_to_float(s: str) -> float:
    """Convert fractional inch string to float for sorting."""
    parts = s.split("-")
    total = 0.0
    for p in parts:
        if "/" in p:
            n, d = p.split("/")
            total += int(n) / int(d)
        else:
            total += int(p)
    return total


def _lookup_woodruff(key_number: str) -> tuple[float, float, float]:
    """Return (width_mm, diameter_mm, height_mm) for a Woodruff key number."""
    spec = _WOODRUFF.get(key_number)
    if spec is None:
        valid = ", ".join(sorted(_WOODRUFF.keys()))
        raise ValueError(f"Woodruff key number '{key_number}' not in table. Valid numbers: {valid}")
    return spec


def _make_keyway_spec(key_width: float, key_height: float) -> KeywaySpec:
    """Build KeywaySpec from key cross-section dimensions (mm)."""
    shaft_depth = key_height / 2.0
    hub_depth = key_height - shaft_depth + _CLEARANCE_MM
    return KeywaySpec(
        width=key_width,
        height=key_height,
        shaft_depth=shaft_depth,
        hub_depth=hub_depth,
    )


def keyway_profile(
    shaft_diameter: float,
    standard: str = "din6885",
    key_length: float | None = None,
    *,
    shaft_dia_inch: str | None = None,
    woodruff_number: str | None = None,
) -> dict[str, Any]:
    """Compute keyway pocket geometry for a given shaft.

    Parameters
    ----------
    shaft_diameter:
        Shaft diameter in mm (used for DIN 6885).
    standard:
        One of ``"din6885"``, ``"ansi"``, ``"woodruff"``.
    key_length:
        Key length in mm.  Defaults to 1.5× key width for parallel keys.
    shaft_dia_inch:
        Shaft diameter as fractional inch string (required for ``"ansi"``).
    woodruff_number:
        Woodruff key number string (required for ``"woodruff"``).

    Returns
    -------
    dict with keys:
        ``spec`` — KeywaySpec dataclass,
        ``elements`` — rectangle sketch elements for the keyway pocket,
        ``key_length`` — resolved key length,
        ``build_hint`` — human-readable build instructions.
    """
    standard = standard.lower().replace(" ", "").replace("-", "")

    if standard in ("din6885", "din"):
        kw, kh = _lookup_din6885(shaft_diameter)
        spec = _make_keyway_spec(kw, kh)
        unit_label = "mm"
    elif standard == "ansi":
        if shaft_dia_inch is None:
            raise ValueError("shaft_dia_inch required for ANSI standard")
        kw_in, kh_in = _lookup_ansi(shaft_dia_inch)
        # Convert to mm for sketch elements
        kw = kw_in * 25.4
        kh = kh_in * 25.4
        spec = _make_keyway_spec(kw, kh)
        unit_label = f'mm (from {kw_in:.4f}" × {kh_in:.4f}")'
    elif standard == "woodruff":
        if woodruff_number is None:
            raise ValueError("woodruff_number required for Woodruff standard")
        w_mm, dia_mm, h_mm = _lookup_woodruff(woodruff_number)
        spec = KeywaySpec(
            width=w_mm,
            height=h_mm,
            shaft_depth=h_mm - (dia_mm / 2.0 - math.sqrt((dia_mm / 2.0) ** 2 - (w_mm / 2.0) ** 2)),
            hub_depth=h_mm / 2.0 + _CLEARANCE_MM,
        )
        if key_length is None:
            key_length = w_mm  # Woodruff key length ≈ width
        unit_label = f"mm (Woodruff #{woodruff_number}, cutter Ø{dia_mm})"
    else:
        raise ValueError(f"Unknown standard '{standard}'. Use 'din6885', 'ansi', or 'woodruff'.")

    if key_length is None:
        key_length = round(spec.width * 1.5, 1)

    # Sketch elements: rectangle centered at top of shaft (y = shaft_radius)
    # The pocket sits on the shaft surface, cut inward.
    shaft_r = shaft_diameter / 2.0
    pocket_y = shaft_r - spec.shaft_depth
    elements: list[dict[str, Any]] = [
        {
            "type": "rect",
            "x": -spec.width / 2.0,
            "y": pocket_y,
            "w": spec.width,
            "h": spec.shaft_depth,
        }
    ]

    if standard == "woodruff":
        build_hint = (
            f"Woodruff keyway: mill a half-moon slot using a Ø{_WOODRUFF[woodruff_number][1]} mm "  # type: ignore[index]
            f"Woodruff cutter. Slot width={spec.width} mm, depth={spec.shaft_depth:.2f} mm "
            f"from shaft surface. Hub keyway: rectangular pocket "
            f"{spec.width}×{spec.hub_depth:.2f} mm deep."
        )
    else:
        build_hint = (
            f"Mill rectangular keyway pocket: {spec.width}×{key_length} mm, "
            f"depth={spec.shaft_depth:.2f} mm into shaft, "
            f"{spec.hub_depth:.2f} mm into hub bore. "
            f"Key dimensions: {spec.width}×{spec.height} {unit_label}. "
            f"End-mill diameter ≤ {spec.width} mm."
        )

    return {
        "spec": spec,
        "key_length": key_length,
        "elements": elements,
        "build_hint": build_hint,
    }
