"""ISO metric fastener dimension tables.

Lookup tables for bolt head dimensions, through-hole clearances,
counterbore/countersink sizes, thread pitch, and nut dimensions.
All dimensions in mm.

Sources: ISO 4762 (socket head cap screw), ISO 4014/4017 (hex bolt),
ISO 7380 (button head), ISO 10642 (countersunk), ISO 273 (clearance holes),
ISO 4026-4029 (set screws), ISO 7092/7093 (washers), ISO 4032 (hex nut),
ISO 4035 (thin hex nut), ISO 7040/10511 (nyloc nut).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FastenerSpec:
    """Complete dimension set for a metric fastener."""

    size: str  # e.g. "M4"
    thread_diameter: float  # nominal thread OD (mm)
    pitch_coarse: float  # coarse thread pitch (mm)
    pitch_fine: float  # fine thread pitch (mm), 0 if none standard

    # Head dimensions (vary by head_type)
    head_type: str  # socket_head | hex | button_head | countersunk | set_screw
    head_diameter: float  # across-flats for hex, OD for others (mm)
    head_height: float  # mm, 0 for set screws

    # Holes
    through_hole_close: float  # close-fit clearance hole (mm)
    through_hole_normal: float  # normal-fit clearance hole (mm)
    through_hole_loose: float  # loose-fit clearance hole (mm)

    # Counterbore / countersink
    counterbore_diameter: float  # mm, 0 if not applicable
    counterbore_depth: float  # mm (= head_height + 0.5 margin)
    countersink_diameter: float  # mm, 0 if not applicable
    countersink_angle: float  # degrees, 0 if not applicable

    # Socket / wrench
    socket_size: float  # hex socket or wrench size (mm)

    # Washer (standard flat washer)
    washer_od: float  # mm
    washer_thickness: float  # mm

    # Bolt length (user-specified, echoed back)
    length: float  # mm

    # Tap drill
    tap_drill_coarse: float  # mm
    tap_drill_fine: float  # mm, 0 if no fine pitch

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "thread_diameter_mm": self.thread_diameter,
            "pitch_coarse_mm": self.pitch_coarse,
            "pitch_fine_mm": self.pitch_fine,
            "head_type": self.head_type,
            "head_diameter_mm": self.head_diameter,
            "head_height_mm": self.head_height,
            "through_hole_close_mm": self.through_hole_close,
            "through_hole_normal_mm": self.through_hole_normal,
            "through_hole_loose_mm": self.through_hole_loose,
            "counterbore_diameter_mm": self.counterbore_diameter,
            "counterbore_depth_mm": self.counterbore_depth,
            "countersink_diameter_mm": self.countersink_diameter,
            "countersink_angle_deg": self.countersink_angle,
            "socket_size_mm": self.socket_size,
            "washer_od_mm": self.washer_od,
            "washer_thickness_mm": self.washer_thickness,
            "length_mm": self.length,
            "tap_drill_coarse_mm": self.tap_drill_coarse,
            "tap_drill_fine_mm": self.tap_drill_fine,
        }


# ── Thread data: (pitch_coarse, pitch_fine, tap_drill_coarse, tap_drill_fine)
_THREAD: dict[str, tuple[float, float, float, float]] = {
    "M2": (0.4, 0.0, 1.6, 0.0),
    "M2.5": (0.45, 0.0, 2.05, 0.0),
    "M3": (0.5, 0.0, 2.5, 0.0),
    "M4": (0.7, 0.0, 3.3, 0.0),
    "M5": (0.8, 0.0, 4.2, 0.0),
    "M6": (1.0, 0.75, 5.0, 5.25),
    "M8": (1.25, 1.0, 6.8, 7.0),
    "M10": (1.5, 1.25, 8.5, 8.8),
    "M12": (1.75, 1.25, 10.2, 10.8),
    "M16": (2.0, 1.5, 14.0, 14.5),
    "M20": (2.5, 1.5, 17.5, 18.5),
    "M24": (3.0, 2.0, 21.0, 22.0),
}

# ── Clearance holes ISO 273: (close, normal, loose)
_CLEARANCE: dict[str, tuple[float, float, float]] = {
    "M2": (2.2, 2.4, 2.6),
    "M2.5": (2.7, 2.9, 3.1),
    "M3": (3.2, 3.4, 3.6),
    "M4": (4.3, 4.5, 4.8),
    "M5": (5.3, 5.5, 5.8),
    "M6": (6.4, 6.6, 7.0),
    "M8": (8.4, 9.0, 10.0),
    "M10": (10.5, 11.0, 12.0),
    "M12": (13.0, 13.5, 14.5),
    "M16": (17.0, 17.5, 18.5),
    "M20": (21.0, 22.0, 24.0),
    "M24": (25.0, 26.0, 28.0),
}

# ── Washer data ISO 7092/7093: (OD, thickness)
_WASHER: dict[str, tuple[float, float]] = {
    "M2": (5.0, 0.3),
    "M2.5": (6.0, 0.5),
    "M3": (7.0, 0.5),
    "M4": (9.0, 0.8),
    "M5": (10.0, 1.0),
    "M6": (12.0, 1.6),
    "M8": (16.0, 1.6),
    "M10": (20.0, 2.0),
    "M12": (24.0, 2.5),
    "M16": (30.0, 3.0),
    "M20": (37.0, 3.0),
    "M24": (44.0, 4.0),
}

# ── Head dimensions by type
# socket_head (ISO 4762): (head_dia, head_height, socket_size)
_SOCKET_HEAD: dict[str, tuple[float, float, float]] = {
    "M2": (3.8, 2.0, 1.5),
    "M2.5": (4.5, 2.5, 2.0),
    "M3": (5.5, 3.0, 2.5),
    "M4": (7.0, 4.0, 3.0),
    "M5": (8.5, 5.0, 4.0),
    "M6": (10.0, 6.0, 5.0),
    "M8": (13.0, 8.0, 6.0),
    "M10": (16.0, 10.0, 8.0),
    "M12": (18.0, 12.0, 10.0),
    "M16": (24.0, 16.0, 14.0),
    "M20": (30.0, 20.0, 17.0),
    "M24": (36.0, 24.0, 19.0),
}

# hex (ISO 4014/4017): (across_flats, head_height, wrench_size)
_HEX: dict[str, tuple[float, float, float]] = {
    "M3": (5.5, 2.0, 5.5),
    "M4": (7.0, 2.8, 7.0),
    "M5": (8.0, 3.5, 8.0),
    "M6": (10.0, 4.0, 10.0),
    "M8": (13.0, 5.3, 13.0),
    "M10": (16.0, 6.4, 16.0),
    "M12": (18.0, 7.5, 18.0),
    "M16": (24.0, 10.0, 24.0),
    "M20": (30.0, 12.5, 30.0),
    "M24": (36.0, 15.0, 36.0),
}

# button_head (ISO 7380): (head_dia, head_height, socket_size)
_BUTTON_HEAD: dict[str, tuple[float, float, float]] = {
    "M3": (5.7, 1.65, 2.0),
    "M4": (7.6, 2.2, 2.5),
    "M5": (9.5, 2.75, 3.0),
    "M6": (10.5, 3.3, 4.0),
    "M8": (14.0, 4.4, 5.0),
    "M10": (17.5, 5.5, 6.0),
    "M12": (21.0, 6.6, 8.0),
}

# countersunk (ISO 10642): (head_dia, head_height, socket_size)
_COUNTERSUNK: dict[str, tuple[float, float, float]] = {
    "M3": (6.72, 1.86, 2.0),
    "M4": (8.96, 2.48, 2.5),
    "M5": (11.2, 3.1, 3.0),
    "M6": (13.44, 3.72, 4.0),
    "M8": (17.92, 4.96, 5.0),
    "M10": (22.4, 6.2, 6.0),
    "M12": (26.88, 7.44, 8.0),
    "M16": (33.0, 8.8, 10.0),
    "M20": (40.0, 10.16, 12.0),
}

_HEAD_TABLES: dict[str, dict[str, tuple[float, float, float]]] = {
    "socket_head": _SOCKET_HEAD,
    "hex": _HEX,
    "button_head": _BUTTON_HEAD,
    "countersunk": _COUNTERSUNK,
}

SUPPORTED_SIZES = sorted(_THREAD.keys(), key=lambda s: float(s[1:]))
SUPPORTED_HEAD_TYPES = ["socket_head", "hex", "button_head", "countersunk", "set_screw"]
SUPPORTED_NUT_TYPES = ["hex", "thin", "nyloc"]


# ── Nut data ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NutSpec:
    """Complete dimension set for a metric nut."""

    size: str  # e.g. "M4"
    thread_diameter: float  # nominal thread OD (mm)
    pitch_coarse: float  # coarse thread pitch (mm)
    nut_type: str  # hex | thin | nyloc
    across_flats: float  # wrench size (mm)
    across_corners: float  # point-to-point (mm)
    height: float  # total nut height (mm)
    through_hole: float  # thread bore diameter (mm) — same as nominal

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "thread_diameter_mm": self.thread_diameter,
            "pitch_coarse_mm": self.pitch_coarse,
            "nut_type": self.nut_type,
            "across_flats_mm": self.across_flats,
            "across_corners_mm": self.across_corners,
            "height_mm": self.height,
            "through_hole_mm": self.through_hole,
        }


# ISO 4032 hex nut: (across_flats, height)
_NUT_HEX: dict[str, tuple[float, float]] = {
    "M2": (4.0, 1.6),
    "M2.5": (5.0, 2.0),
    "M3": (5.5, 2.4),
    "M4": (7.0, 3.2),
    "M5": (8.0, 4.7),
    "M6": (10.0, 5.2),
    "M8": (13.0, 6.8),
    "M10": (16.0, 8.4),
    "M12": (18.0, 10.8),
    "M16": (24.0, 14.8),
    "M20": (30.0, 18.0),
    "M24": (36.0, 21.5),
}

# ISO 4035 thin/jam nut: (across_flats, height)
_NUT_THIN: dict[str, tuple[float, float]] = {
    "M3": (5.5, 1.8),
    "M4": (7.0, 2.2),
    "M5": (8.0, 2.7),
    "M6": (10.0, 3.2),
    "M8": (13.0, 4.0),
    "M10": (16.0, 5.0),
    "M12": (18.0, 6.0),
    "M16": (24.0, 8.0),
    "M20": (30.0, 10.0),
    "M24": (36.0, 12.0),
}

# ISO 7040/10511 nyloc nut: (across_flats, height)
_NUT_NYLOC: dict[str, tuple[float, float]] = {
    "M3": (5.5, 4.0),
    "M4": (7.0, 5.0),
    "M5": (8.0, 5.0),
    "M6": (10.0, 6.0),
    "M8": (13.0, 8.0),
    "M10": (16.0, 10.0),
    "M12": (18.0, 12.0),
    "M16": (24.0, 16.0),
    "M20": (30.0, 20.0),
    "M24": (36.0, 21.5),
}

_NUT_TABLES: dict[str, dict[str, tuple[float, float]]] = {
    "hex": _NUT_HEX,
    "thin": _NUT_THIN,
    "nyloc": _NUT_NYLOC,
}

_COS30 = 0.8660254037844387  # cos(30°)


def match_bolt_size(hole_diameter: float) -> dict[str, Any] | None:
    """Given a hole diameter (mm), find the best matching bolt size.

    Compares against clearance hole tables (close, normal, loose fit).
    Returns the best match with fit type, or None if no match within 0.5mm.
    """
    best: dict[str, Any] | None = None
    best_delta = 999.0

    for size_str, (close, normal, loose) in _CLEARANCE.items():
        for fit_name, fit_val in [("close", close), ("normal", normal), ("loose", loose)]:
            delta = abs(hole_diameter - fit_val)
            if delta < best_delta:
                best_delta = delta
                best = {
                    "size": size_str,
                    "fit": fit_name,
                    "clearance_hole_mm": fit_val,
                    "thread_diameter_mm": float(size_str[1:]),
                    "delta_mm": round(delta, 3),
                }

    if best is not None and best_delta <= 0.5:
        return best
    return None


def lookup(
    size: str,
    length: float,
    head_type: str = "socket_head",
) -> FastenerSpec | None:
    """Look up all dimensions for a metric fastener.

    Args:
        size: Metric size string, e.g. "M4", "M8".
        length: Bolt shaft length in mm.
        head_type: One of socket_head, hex, button_head, countersunk, set_screw.

    Returns:
        FastenerSpec with all dimensions, or None if size/head_type not found.
    """
    size = size.upper()
    if size not in _THREAD:
        return None
    if head_type not in SUPPORTED_HEAD_TYPES:
        return None

    pitch_c, pitch_f, tap_c, tap_f = _THREAD[size]
    close, normal, loose = _CLEARANCE.get(size, (0.0, 0.0, 0.0))
    washer_od, washer_t = _WASHER.get(size, (0.0, 0.0))
    thread_dia = float(size[1:])

    if head_type == "set_screw":
        # Set screws have no head — socket size = nominal thread diameter
        return FastenerSpec(
            size=size,
            thread_diameter=thread_dia,
            pitch_coarse=pitch_c,
            pitch_fine=pitch_f,
            head_type="set_screw",
            head_diameter=0.0,
            head_height=0.0,
            through_hole_close=close,
            through_hole_normal=normal,
            through_hole_loose=loose,
            counterbore_diameter=0.0,
            counterbore_depth=0.0,
            countersink_diameter=0.0,
            countersink_angle=0.0,
            socket_size=thread_dia,
            washer_od=washer_od,
            washer_thickness=washer_t,
            length=length,
            tap_drill_coarse=tap_c,
            tap_drill_fine=tap_f,
        )

    table = _HEAD_TABLES.get(head_type)
    if table is None or size not in table:
        return None

    head_dia, head_h, socket = table[size]

    # Counterbore: head_diameter + 1mm clearance, depth = head_height + 0.5mm
    cb_dia = 0.0
    cb_depth = 0.0
    cs_dia = 0.0
    cs_angle = 0.0

    if head_type == "countersunk":
        cs_dia = head_dia
        cs_angle = 90.0
    else:
        cb_dia = head_dia + 1.0
        cb_depth = head_h + 0.5

    return FastenerSpec(
        size=size,
        thread_diameter=thread_dia,
        pitch_coarse=pitch_c,
        pitch_fine=pitch_f,
        head_type=head_type,
        head_diameter=head_dia,
        head_height=head_h,
        through_hole_close=close,
        through_hole_normal=normal,
        through_hole_loose=loose,
        counterbore_diameter=cb_dia,
        counterbore_depth=cb_depth,
        countersink_diameter=cs_dia,
        countersink_angle=cs_angle,
        socket_size=socket,
        washer_od=washer_od,
        washer_thickness=washer_t,
        length=length,
        tap_drill_coarse=tap_c,
        tap_drill_fine=tap_f,
    )


def nut_lookup(
    size: str,
    nut_type: str = "hex",
) -> NutSpec | None:
    """Look up dimensions for a metric nut.

    Args:
        size: Metric size string, e.g. "M4", "M8".
        nut_type: One of hex, thin, nyloc.

    Returns:
        NutSpec with all dimensions, or None if size/nut_type not found.
    """
    size = size.upper()
    if size not in _THREAD:
        return None

    table = _NUT_TABLES.get(nut_type)
    if table is None or size not in table:
        return None

    pitch_c = _THREAD[size][0]
    thread_dia = float(size[1:])
    af, height = table[size]
    ac = af / _COS30  # across corners = across_flats / cos(30°)

    return NutSpec(
        size=size,
        thread_diameter=thread_dia,
        pitch_coarse=pitch_c,
        nut_type=nut_type,
        across_flats=af,
        across_corners=round(ac, 2),
        height=height,
        through_hole=thread_dia,
    )
