"""Power transmission belt-drive layout computation.

Belt length, wrap angles, speed ratios, timing belt tooth counts,
and groove profile sketch elements for V-belt and timing belt drives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# V-belt cross-section lookup (ISO 4184 / RMA)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VBeltSpec:
    """V-belt cross-section dimensions."""

    designation: str
    top_width_mm: float
    height_mm: float
    weight_per_m_kg: float
    min_pulley_dia_mm: float
    groove_angle_deg: float


_VBELT_PROFILES: dict[str, VBeltSpec] = {
    "A": VBeltSpec("A", 13.0, 8.0, 0.10, 75.0, 34.0),
    "B": VBeltSpec("B", 17.0, 11.0, 0.18, 125.0, 34.0),
    "C": VBeltSpec("C", 22.0, 14.0, 0.30, 200.0, 34.0),
    "D": VBeltSpec("D", 32.0, 19.0, 0.60, 315.0, 36.0),
    "E": VBeltSpec("E", 38.0, 23.0, 0.90, 500.0, 36.0),
    # Narrow / wedge belts
    "3V": VBeltSpec("3V", 9.5, 8.0, 0.07, 63.0, 36.0),
    "5V": VBeltSpec("5V", 15.9, 13.5, 0.19, 180.0, 36.0),
    "8V": VBeltSpec("8V", 25.4, 23.0, 0.52, 315.0, 36.0),
    # Metric
    "SPZ": VBeltSpec("SPZ", 9.7, 8.0, 0.07, 63.0, 34.0),
    "SPA": VBeltSpec("SPA", 12.7, 10.0, 0.11, 90.0, 34.0),
    "SPB": VBeltSpec("SPB", 16.3, 13.0, 0.18, 140.0, 34.0),
    "SPC": VBeltSpec("SPC", 22.0, 18.0, 0.35, 224.0, 34.0),
}

# ---------------------------------------------------------------------------
# Timing belt pitch lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimingBeltSpec:
    """Timing belt profile dimensions."""

    designation: str
    pitch_mm: float
    tooth_height_mm: float
    belt_thickness_mm: float
    min_pulley_teeth: int


_TIMING_PROFILES: dict[str, TimingBeltSpec] = {
    "MXL": TimingBeltSpec("MXL", 2.032, 0.51, 1.14, 10),
    "XL": TimingBeltSpec("XL", 5.080, 1.27, 2.30, 10),
    "L": TimingBeltSpec("L", 9.525, 1.91, 3.60, 10),
    "H": TimingBeltSpec("H", 12.700, 2.29, 4.30, 14),
    "XH": TimingBeltSpec("XH", 22.225, 6.35, 11.20, 18),
    "XXH": TimingBeltSpec("XXH", 31.750, 9.53, 15.70, 18),
    "T2.5": TimingBeltSpec("T2.5", 2.500, 0.70, 1.30, 10),
    "T5": TimingBeltSpec("T5", 5.000, 1.20, 2.20, 10),
    "T10": TimingBeltSpec("T10", 10.000, 2.50, 4.50, 12),
    "T20": TimingBeltSpec("T20", 20.000, 5.00, 8.00, 15),
    "HTD-3M": TimingBeltSpec("HTD-3M", 3.000, 1.22, 2.40, 10),
    "HTD-5M": TimingBeltSpec("HTD-5M", 5.000, 2.06, 3.80, 14),
    "HTD-8M": TimingBeltSpec("HTD-8M", 8.000, 3.36, 5.60, 22),
    "HTD-14M": TimingBeltSpec("HTD-14M", 14.000, 6.02, 10.00, 28),
    "GT2": TimingBeltSpec("GT2", 2.000, 0.76, 1.38, 16),
    "GT3": TimingBeltSpec("GT3", 3.000, 1.14, 2.41, 16),
    "GT5": TimingBeltSpec("GT5", 5.000, 1.91, 3.81, 22),
}


# ---------------------------------------------------------------------------
# Core formulas
# ---------------------------------------------------------------------------


def _belt_length(center_dist: float, d_large: float, d_small: float) -> float:
    """Open belt length from center distance and pulley diameters."""
    return (
        2.0 * center_dist
        + math.pi * (d_large + d_small) / 2.0
        + (d_large - d_small) ** 2 / (4.0 * center_dist)
    )


def _wrap_angles(
    center_dist: float,
    d_large: float,
    d_small: float,
) -> tuple[float, float]:
    """Wrap angles in degrees for small and large pulleys (open belt)."""
    ratio = (d_large - d_small) / (2.0 * center_dist)
    ratio = max(-1.0, min(1.0, ratio))  # clamp for numerical safety
    alpha = math.degrees(math.asin(ratio))
    theta_small = 180.0 - 2.0 * alpha
    theta_large = 180.0 + 2.0 * alpha
    return theta_small, theta_large


def _timing_teeth(diameter: float, pitch: float) -> int:
    """Number of teeth on a timing pulley given pitch diameter and belt pitch."""
    circumference = math.pi * diameter
    return max(1, round(circumference / pitch))


def _vbelt_groove_elements(spec: VBeltSpec) -> list[dict[str, Any]]:
    """Return sketch elements for one V-belt groove cross-section.

    Groove is a symmetric trapezoid centered at origin, open at top.
    """
    half_angle = math.radians(spec.groove_angle_deg / 2.0)
    depth = spec.height_mm * 1.05  # groove slightly deeper than belt
    top_half = spec.top_width_mm / 2.0
    bottom_half = top_half - depth * math.tan(half_angle)
    return [
        {"type": "line", "x1": -top_half, "y1": 0, "x2": -bottom_half, "y2": -depth},
        {"type": "line", "x1": -bottom_half, "y1": -depth, "x2": bottom_half, "y2": -depth},
        {"type": "line", "x1": bottom_half, "y1": -depth, "x2": top_half, "y2": 0},
    ]


def _timing_tooth_elements(spec: TimingBeltSpec) -> list[dict[str, Any]]:
    """Return sketch elements for one timing-belt tooth groove.

    Simplified trapezoidal tooth profile centered at origin.
    """
    p = spec.pitch_mm
    h = spec.tooth_height_mm
    # Tooth land ≈ 40% of pitch, root ≈ 60% of pitch (typical)
    tooth_top = p * 0.4
    tooth_bot = p * 0.3
    return [
        {"type": "line", "x1": -tooth_top / 2, "y1": 0, "x2": -tooth_bot / 2, "y2": -h},
        {"type": "line", "x1": -tooth_bot / 2, "y1": -h, "x2": tooth_bot / 2, "y2": -h},
        {"type": "line", "x1": tooth_bot / 2, "y1": -h, "x2": tooth_top / 2, "y2": 0},
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def belt_drive_layout(
    driver_diameter: float,
    driven_diameter: float,
    center_distance: float,
    belt_type: str = "timing",
    belt_profile: str | None = None,
) -> dict[str, Any]:
    """Compute belt drive layout geometry.

    Parameters
    ----------
    driver_diameter:
        Driver (input) pulley pitch diameter in mm.
    driven_diameter:
        Driven (output) pulley pitch diameter in mm.
    center_distance:
        Center-to-center distance between pulleys in mm.
    belt_type:
        ``"timing"`` or ``"vbelt"``.
    belt_profile:
        Profile designation.  For timing belts: ``"GT2"``, ``"HTD-5M"``,
        ``"XL"``, etc.  For V-belts: ``"A"``, ``"B"``, ``"SPA"``, etc.
        Defaults to ``"HTD-5M"`` (timing) or ``"A"`` (V-belt).

    Returns
    -------
    dict with:
        ``wrap_angle_driver`` — wrap angle on driver pulley (degrees),
        ``wrap_angle_driven`` — wrap angle on driven pulley (degrees),
        ``belt_length`` — required belt length (mm),
        ``speed_ratio`` — driven/driver speed ratio,
        ``driver_teeth`` / ``driven_teeth`` — (timing only),
        ``belt_teeth`` — total teeth on timing belt,
        ``profile_spec`` — profile dataclass,
        ``groove_elements`` — sketch elements for one groove profile,
        ``build_hint`` — human-readable summary.
    """
    belt_type = belt_type.lower().replace("-", "").replace("_", "")

    d_small = min(driver_diameter, driven_diameter)
    d_large = max(driver_diameter, driven_diameter)

    # Validate center distance
    min_cd = (d_large + d_small) / 2.0
    if center_distance < min_cd:
        raise ValueError(
            f"Center distance {center_distance} mm is less than minimum "
            f"{min_cd:.1f} mm (pulleys would overlap)"
        )

    # Core geometry
    belt_len = _belt_length(center_distance, d_large, d_small)
    if driver_diameter <= driven_diameter:
        theta_driver, theta_driven = _wrap_angles(center_distance, driven_diameter, driver_diameter)
    else:
        theta_driven, theta_driver = _wrap_angles(center_distance, driver_diameter, driven_diameter)

    speed_ratio = driven_diameter / driver_diameter if driver_diameter > 0 else 0.0

    result: dict[str, Any] = {
        "wrap_angle_driver": round(theta_driver, 2),
        "wrap_angle_driven": round(theta_driven, 2),
        "belt_length": round(belt_len, 2),
        "speed_ratio": round(speed_ratio, 4),
    }

    if belt_type in ("timing", "toothed", "synchronous"):
        profile_key = (belt_profile or "HTD-5M").upper().replace(" ", "")
        spec = _TIMING_PROFILES.get(profile_key)
        if spec is None:
            valid = ", ".join(sorted(_TIMING_PROFILES.keys()))
            raise ValueError(f"Unknown timing belt profile '{profile_key}'. Valid: {valid}")

        # Minimum pulley check
        driver_teeth = _timing_teeth(driver_diameter, spec.pitch_mm)
        driven_teeth = _timing_teeth(driven_diameter, spec.pitch_mm)
        belt_teeth = max(1, round(belt_len / spec.pitch_mm))

        warnings: list[str] = []
        if driver_teeth < spec.min_pulley_teeth:
            warnings.append(
                f"Driver has {driver_teeth} teeth, minimum {spec.min_pulley_teeth} "
                f"for {spec.designation}"
            )
        if driven_teeth < spec.min_pulley_teeth:
            warnings.append(
                f"Driven has {driven_teeth} teeth, minimum {spec.min_pulley_teeth} "
                f"for {spec.designation}"
            )

        result.update(
            {
                "driver_teeth": driver_teeth,
                "driven_teeth": driven_teeth,
                "belt_teeth": belt_teeth,
                "belt_pitch_mm": spec.pitch_mm,
                "profile_spec": spec,
                "groove_elements": _timing_tooth_elements(spec),
            }
        )

        build_hint = (
            f"{spec.designation} timing belt: {belt_teeth}T belt, "
            f"driver {driver_teeth}T (Ø{driver_diameter} mm), "
            f"driven {driven_teeth}T (Ø{driven_diameter} mm), "
            f"C-C={center_distance} mm, belt length={belt_len:.1f} mm, "
            f"speed ratio={speed_ratio:.3f}. "
            f"Wrap angles: driver={theta_driver:.1f}°, driven={theta_driven:.1f}°."
        )
        if warnings:
            build_hint += " WARNINGS: " + "; ".join(warnings)

    elif belt_type in ("vbelt", "v"):
        profile_key = (belt_profile or "A").upper()
        spec_v = _VBELT_PROFILES.get(profile_key)
        if spec_v is None:
            valid = ", ".join(sorted(_VBELT_PROFILES.keys()))
            raise ValueError(f"Unknown V-belt profile '{profile_key}'. Valid: {valid}")

        warnings = []
        if d_small < spec_v.min_pulley_dia_mm:
            warnings.append(
                f"Smaller pulley Ø{d_small} mm is below minimum "
                f"Ø{spec_v.min_pulley_dia_mm} mm for {spec_v.designation} belt"
            )

        # Wrap angle check — minimum 120° recommended
        if theta_driver < 120.0 or theta_driven < 120.0:
            warnings.append(
                f"Wrap angle below 120° ({min(theta_driver, theta_driven):.1f}°) — "
                f"consider an idler or increasing center distance"
            )

        result.update(
            {
                "profile_spec": spec_v,
                "groove_elements": _vbelt_groove_elements(spec_v),
                "groove_angle_deg": spec_v.groove_angle_deg,
            }
        )

        build_hint = (
            f"{spec_v.designation} V-belt: "
            f"driver Ø{driver_diameter} mm, driven Ø{driven_diameter} mm, "
            f"C-C={center_distance} mm, belt length={belt_len:.1f} mm, "
            f"speed ratio={speed_ratio:.3f}. "
            f"Wrap angles: driver={theta_driver:.1f}°, driven={theta_driven:.1f}°. "
            f"Belt: {spec_v.top_width_mm}×{spec_v.height_mm} mm, "
            f"~{spec_v.weight_per_m_kg:.2f} kg/m."
        )
        if warnings:
            build_hint += " WARNINGS: " + "; ".join(warnings)

    else:
        raise ValueError(f"Unknown belt_type '{belt_type}'. Use 'timing' or 'vbelt'.")

    result["build_hint"] = build_hint
    return result
