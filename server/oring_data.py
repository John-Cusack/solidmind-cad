"""Standard O-ring seal groove dimensions.

AS568 dash-number lookup and groove design per Parker O-Ring Handbook.
Returns sketch elements for groove cross-section profiles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ORingSpec:
    """O-ring nominal dimensions in mm."""

    id_mm: float
    cs_mm: float


# ---------------------------------------------------------------------------
# AS568 dash-number table
# Organised by series: 0xx (CS varies), 1xx (CS≈1.78), 2xx (CS≈3.53),
# 3xx (CS≈5.33), 4xx (CS≈5.33 large bore).
# ---------------------------------------------------------------------------
_AS568: dict[int, ORingSpec] = {
    # 0xx series — small cross-sections
    1: ORingSpec(id_mm=0.74, cs_mm=1.02),
    2: ORingSpec(id_mm=1.07, cs_mm=1.27),
    3: ORingSpec(id_mm=1.42, cs_mm=1.52),
    4: ORingSpec(id_mm=1.78, cs_mm=1.78),
    5: ORingSpec(id_mm=2.57, cs_mm=1.78),
    6: ORingSpec(id_mm=2.90, cs_mm=1.78),
    7: ORingSpec(id_mm=3.68, cs_mm=1.78),
    8: ORingSpec(id_mm=4.47, cs_mm=1.78),
    9: ORingSpec(id_mm=5.28, cs_mm=1.78),
    10: ORingSpec(id_mm=6.07, cs_mm=1.78),
    11: ORingSpec(id_mm=7.65, cs_mm=1.78),
    12: ORingSpec(id_mm=9.25, cs_mm=1.78),
    13: ORingSpec(id_mm=10.82, cs_mm=1.78),
    14: ORingSpec(id_mm=12.42, cs_mm=1.78),
    15: ORingSpec(id_mm=14.00, cs_mm=1.78),
    16: ORingSpec(id_mm=15.60, cs_mm=1.78),
    17: ORingSpec(id_mm=17.17, cs_mm=1.78),
    18: ORingSpec(id_mm=18.77, cs_mm=1.78),
    19: ORingSpec(id_mm=20.35, cs_mm=1.78),
    20: ORingSpec(id_mm=21.95, cs_mm=1.78),
    21: ORingSpec(id_mm=23.52, cs_mm=1.78),
    22: ORingSpec(id_mm=25.12, cs_mm=1.78),
    23: ORingSpec(id_mm=26.70, cs_mm=1.78),
    24: ORingSpec(id_mm=28.30, cs_mm=1.78),
    25: ORingSpec(id_mm=29.87, cs_mm=1.78),
    26: ORingSpec(id_mm=31.47, cs_mm=1.78),
    27: ORingSpec(id_mm=33.05, cs_mm=1.78),
    28: ORingSpec(id_mm=34.65, cs_mm=1.78),
    29: ORingSpec(id_mm=37.82, cs_mm=1.78),
    30: ORingSpec(id_mm=40.95, cs_mm=1.78),
    31: ORingSpec(id_mm=44.12, cs_mm=1.78),
    32: ORingSpec(id_mm=47.29, cs_mm=1.78),
    33: ORingSpec(id_mm=50.47, cs_mm=1.78),
    34: ORingSpec(id_mm=53.64, cs_mm=1.78),
    35: ORingSpec(id_mm=56.82, cs_mm=1.78),
    36: ORingSpec(id_mm=59.99, cs_mm=1.78),
    37: ORingSpec(id_mm=63.17, cs_mm=1.78),
    38: ORingSpec(id_mm=66.34, cs_mm=1.78),
    39: ORingSpec(id_mm=69.52, cs_mm=1.78),
    40: ORingSpec(id_mm=72.69, cs_mm=1.78),
    41: ORingSpec(id_mm=75.87, cs_mm=1.78),
    42: ORingSpec(id_mm=82.22, cs_mm=1.78),
    43: ORingSpec(id_mm=88.57, cs_mm=1.78),
    44: ORingSpec(id_mm=94.92, cs_mm=1.78),
    45: ORingSpec(id_mm=101.27, cs_mm=1.78),
    46: ORingSpec(id_mm=107.62, cs_mm=1.78),
    47: ORingSpec(id_mm=113.97, cs_mm=1.78),
    48: ORingSpec(id_mm=120.32, cs_mm=1.78),
    49: ORingSpec(id_mm=126.67, cs_mm=1.78),
    50: ORingSpec(id_mm=133.02, cs_mm=1.78),
    # 1xx series — CS ≈ 1.78 mm
    102: ORingSpec(id_mm=2.57, cs_mm=1.78),
    103: ORingSpec(id_mm=3.63, cs_mm=1.78),
    104: ORingSpec(id_mm=4.42, cs_mm=1.78),
    105: ORingSpec(id_mm=5.23, cs_mm=1.78),
    106: ORingSpec(id_mm=6.02, cs_mm=1.78),
    107: ORingSpec(id_mm=7.59, cs_mm=1.78),
    108: ORingSpec(id_mm=9.19, cs_mm=1.78),
    109: ORingSpec(id_mm=10.77, cs_mm=1.78),
    110: ORingSpec(id_mm=12.37, cs_mm=1.78),
    111: ORingSpec(id_mm=13.94, cs_mm=1.78),
    112: ORingSpec(id_mm=15.54, cs_mm=1.78),
    113: ORingSpec(id_mm=17.12, cs_mm=1.78),
    114: ORingSpec(id_mm=18.72, cs_mm=1.78),
    115: ORingSpec(id_mm=20.29, cs_mm=1.78),
    116: ORingSpec(id_mm=21.89, cs_mm=1.78),
    # 2xx series — CS ≈ 3.53 mm
    201: ORingSpec(id_mm=4.34, cs_mm=3.53),
    202: ORingSpec(id_mm=5.94, cs_mm=3.53),
    203: ORingSpec(id_mm=7.52, cs_mm=3.53),
    204: ORingSpec(id_mm=9.12, cs_mm=3.53),
    205: ORingSpec(id_mm=10.69, cs_mm=3.53),
    206: ORingSpec(id_mm=12.29, cs_mm=3.53),
    207: ORingSpec(id_mm=13.87, cs_mm=3.53),
    208: ORingSpec(id_mm=15.47, cs_mm=3.53),
    209: ORingSpec(id_mm=17.04, cs_mm=3.53),
    210: ORingSpec(id_mm=18.64, cs_mm=3.53),
    211: ORingSpec(id_mm=20.22, cs_mm=3.53),
    212: ORingSpec(id_mm=21.82, cs_mm=3.53),
    213: ORingSpec(id_mm=23.39, cs_mm=3.53),
    214: ORingSpec(id_mm=24.99, cs_mm=3.53),
    215: ORingSpec(id_mm=26.57, cs_mm=3.53),
    216: ORingSpec(id_mm=28.17, cs_mm=3.53),
    217: ORingSpec(id_mm=29.74, cs_mm=3.53),
    218: ORingSpec(id_mm=31.34, cs_mm=3.53),
    219: ORingSpec(id_mm=32.92, cs_mm=3.53),
    220: ORingSpec(id_mm=34.52, cs_mm=3.53),
    221: ORingSpec(id_mm=36.09, cs_mm=3.53),
    222: ORingSpec(id_mm=37.69, cs_mm=3.53),
    223: ORingSpec(id_mm=39.29, cs_mm=3.53),
    224: ORingSpec(id_mm=40.87, cs_mm=3.53),
    225: ORingSpec(id_mm=44.04, cs_mm=3.53),
    226: ORingSpec(id_mm=47.22, cs_mm=3.53),
    227: ORingSpec(id_mm=50.39, cs_mm=3.53),
    228: ORingSpec(id_mm=53.57, cs_mm=3.53),
    # 3xx series — CS ≈ 5.33 mm
    309: ORingSpec(id_mm=9.19, cs_mm=5.33),
    310: ORingSpec(id_mm=10.77, cs_mm=5.33),
    311: ORingSpec(id_mm=12.37, cs_mm=5.33),
    312: ORingSpec(id_mm=13.94, cs_mm=5.33),
    313: ORingSpec(id_mm=15.54, cs_mm=5.33),
    314: ORingSpec(id_mm=17.12, cs_mm=5.33),
    315: ORingSpec(id_mm=18.72, cs_mm=5.33),
    316: ORingSpec(id_mm=20.29, cs_mm=5.33),
    317: ORingSpec(id_mm=21.89, cs_mm=5.33),
    318: ORingSpec(id_mm=23.47, cs_mm=5.33),
    319: ORingSpec(id_mm=25.07, cs_mm=5.33),
    320: ORingSpec(id_mm=26.64, cs_mm=5.33),
    321: ORingSpec(id_mm=28.24, cs_mm=5.33),
    322: ORingSpec(id_mm=29.82, cs_mm=5.33),
    323: ORingSpec(id_mm=31.42, cs_mm=5.33),
    324: ORingSpec(id_mm=32.99, cs_mm=5.33),
    325: ORingSpec(id_mm=34.59, cs_mm=5.33),
    326: ORingSpec(id_mm=37.77, cs_mm=5.33),
    327: ORingSpec(id_mm=40.94, cs_mm=5.33),
    328: ORingSpec(id_mm=44.12, cs_mm=5.33),
    329: ORingSpec(id_mm=47.29, cs_mm=5.33),
    330: ORingSpec(id_mm=50.47, cs_mm=5.33),
    # 4xx series — CS ≈ 5.33 mm, large bore
    425: ORingSpec(id_mm=101.19, cs_mm=5.33),
    426: ORingSpec(id_mm=104.37, cs_mm=5.33),
    427: ORingSpec(id_mm=107.54, cs_mm=5.33),
    428: ORingSpec(id_mm=110.72, cs_mm=5.33),
    429: ORingSpec(id_mm=113.89, cs_mm=5.33),
    430: ORingSpec(id_mm=117.07, cs_mm=5.33),
    431: ORingSpec(id_mm=120.24, cs_mm=5.33),
    432: ORingSpec(id_mm=126.59, cs_mm=5.33),
    433: ORingSpec(id_mm=132.94, cs_mm=5.33),
    434: ORingSpec(id_mm=139.29, cs_mm=5.33),
    435: ORingSpec(id_mm=145.64, cs_mm=5.33),
    436: ORingSpec(id_mm=151.99, cs_mm=5.33),
    437: ORingSpec(id_mm=158.34, cs_mm=5.33),
    438: ORingSpec(id_mm=164.69, cs_mm=5.33),
    439: ORingSpec(id_mm=171.04, cs_mm=5.33),
    440: ORingSpec(id_mm=177.39, cs_mm=5.33),
    441: ORingSpec(id_mm=183.74, cs_mm=5.33),
    442: ORingSpec(id_mm=190.09, cs_mm=5.33),
    443: ORingSpec(id_mm=196.44, cs_mm=5.33),
    444: ORingSpec(id_mm=202.79, cs_mm=5.33),
    445: ORingSpec(id_mm=215.49, cs_mm=5.33),
    446: ORingSpec(id_mm=228.19, cs_mm=5.33),
    447: ORingSpec(id_mm=240.89, cs_mm=5.33),
    448: ORingSpec(id_mm=253.59, cs_mm=5.33),
    449: ORingSpec(id_mm=266.29, cs_mm=5.33),
    450: ORingSpec(id_mm=278.99, cs_mm=5.33),
}


# ---------------------------------------------------------------------------
# Groove design parameters per application type
# ---------------------------------------------------------------------------
_APPLICATION_PARAMS: dict[str, dict[str, float]] = {
    "static_radial": {
        "squeeze_min": 0.20,
        "squeeze_max": 0.25,
        "squeeze_nom": 0.225,
        "groove_width_factor": 1.5,
    },
    "static_face": {
        "squeeze_min": 0.20,
        "squeeze_max": 0.25,
        "squeeze_nom": 0.225,
        "groove_width_factor": 1.3,
    },
    "dynamic_reciprocating": {
        "squeeze_min": 0.10,
        "squeeze_max": 0.15,
        "squeeze_nom": 0.125,
        "groove_width_factor": 1.4,
    },
    "dynamic_rotary": {
        "squeeze_min": 0.08,
        "squeeze_max": 0.12,
        "squeeze_nom": 0.10,
        "groove_width_factor": 1.4,
    },
}


def _resolve_oring(
    dash_number: int | None = None,
    oring_id_mm: float | None = None,
    cross_section_mm: float | None = None,
) -> ORingSpec:
    """Resolve O-ring spec from dash number or explicit dimensions."""
    if dash_number is not None:
        spec = _AS568.get(dash_number)
        if spec is None:
            raise ValueError(
                f"AS568 dash number {dash_number} not in table. Available: {sorted(_AS568.keys())}"
            )
        return spec
    if oring_id_mm is not None and cross_section_mm is not None:
        return ORingSpec(id_mm=oring_id_mm, cs_mm=cross_section_mm)
    raise ValueError("Provide either dash_number or both oring_id_mm and cross_section_mm")


def oring_groove(
    dash_number: int | None = None,
    oring_id_mm: float | None = None,
    cross_section_mm: float | None = None,
    application: str = "static_radial",
    groove_type: str = "bore",
) -> dict[str, Any]:
    """Compute O-ring groove dimensions and sketch elements.

    Parameters
    ----------
    dash_number:
        AS568 dash number.  If provided, ``oring_id_mm`` and
        ``cross_section_mm`` are ignored.
    oring_id_mm:
        O-ring inner diameter in mm (used when *dash_number* is ``None``).
    cross_section_mm:
        O-ring cross-section diameter in mm.
    application:
        One of ``"static_radial"``, ``"static_face"``,
        ``"dynamic_reciprocating"``, ``"dynamic_rotary"``.
    groove_type:
        ``"bore"`` (radial groove in a bore/piston) or ``"face"``
        (axial groove on a flat face).

    Returns
    -------
    dict with:
        ``oring`` — ORingSpec,
        ``groove_depth`` — groove depth in mm,
        ``groove_width`` — groove width in mm,
        ``groove_od`` — outer diameter of groove (for bore type),
        ``squeeze_pct`` — actual squeeze percentage,
        ``gland_fill_pct`` — percentage of groove volume filled by O-ring,
        ``elements`` — sketch elements for the groove cross-section,
        ``analysis`` — human-readable summary.
    """
    spec = _resolve_oring(dash_number, oring_id_mm, cross_section_mm)
    params = _APPLICATION_PARAMS.get(application)
    if params is None:
        valid = ", ".join(sorted(_APPLICATION_PARAMS.keys()))
        raise ValueError(f"Unknown application '{application}'. Valid: {valid}")

    cs = spec.cs_mm
    squeeze_nom = params["squeeze_nom"]
    gw_factor = params["groove_width_factor"]

    # Override width factor for face grooves
    if groove_type == "face" and application.startswith("static"):
        gw_factor = 1.3

    groove_depth = round(cs * (1.0 - squeeze_nom), 3)
    groove_width = round(cs * gw_factor, 3)
    squeeze_pct = round(squeeze_nom * 100.0, 1)

    # Gland fill: cross-section area of O-ring vs groove cross-section area
    oring_area = math.pi * (cs / 2.0) ** 2
    groove_area = groove_depth * groove_width
    gland_fill_pct = round((oring_area / groove_area) * 100.0, 1)

    # Groove diameters (for bore/piston radial grooves)
    if groove_type == "bore":
        # Bore seal: groove cut into bore wall; groove OD = oring ID + 2*groove_depth
        groove_id = spec.id_mm
        groove_od = round(groove_id + 2.0 * groove_depth, 3)
    else:
        groove_id = spec.id_mm
        groove_od = round(groove_id + 2.0 * cs, 3)

    # Sketch elements: groove cross-section rectangle
    # For bore type: radial cross-section for revolution
    # Origin at bore center-line; groove sits at oring ID radius
    if groove_type == "bore":
        r_inner = spec.id_mm / 2.0
        elements: list[dict[str, Any]] = [
            {
                "type": "rect",
                "x": r_inner,
                "y": -groove_width / 2.0,
                "w": groove_depth,
                "h": groove_width,
            }
        ]
    else:
        # Face seal: axial cross-section rectangle at oring ID radius
        r_inner = spec.id_mm / 2.0
        elements = [
            {
                "type": "rect",
                "x": r_inner,
                "y": 0.0,
                "w": (groove_od - groove_id) / 2.0,
                "h": groove_depth,
            }
        ]

    # Acceptability checks
    warnings: list[str] = []
    if gland_fill_pct > 85.0:
        warnings.append(f"Gland fill {gland_fill_pct}% exceeds 85% — risk of hydraulic lock")
    if gland_fill_pct < 60.0:
        warnings.append(f"Gland fill {gland_fill_pct}% below 60% — groove may be oversized")
    if squeeze_pct < params["squeeze_min"] * 100:
        warnings.append(f"Squeeze {squeeze_pct}% below minimum for {application}")
    if squeeze_pct > params["squeeze_max"] * 100:
        warnings.append(f"Squeeze {squeeze_pct}% above maximum for {application}")

    oring_label = (
        f"AS568-{dash_number}" if dash_number is not None else f"ID {spec.id_mm}×CS {spec.cs_mm} mm"
    )
    analysis = (
        f"O-ring: {oring_label} (ID={spec.id_mm} mm, CS={spec.cs_mm} mm). "
        f"Application: {application}, groove type: {groove_type}. "
        f"Groove depth={groove_depth} mm, width={groove_width} mm. "
        f"Squeeze={squeeze_pct}%, gland fill={gland_fill_pct}%."
    )
    if warnings:
        analysis += " WARNINGS: " + "; ".join(warnings)

    return {
        "oring": spec,
        "groove_depth": groove_depth,
        "groove_width": groove_width,
        "groove_id": groove_id,
        "groove_od": groove_od,
        "squeeze_pct": squeeze_pct,
        "gland_fill_pct": gland_fill_pct,
        "elements": elements,
        "analysis": analysis,
    }
