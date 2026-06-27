"""Ratchet click tooth profile generation.

Ratchet teeth are asymmetric: steep locking face (< friction angle to prevent
back-driving) and gradual driving face. Pure Python — no Rust dependency.
"""

from __future__ import annotations

import math
from typing import Any


def ratchet_click_profile(
    pitch_diameter: float,
    teeth: int,
    locking_face_angle_deg: float = 5.0,
    drive_face_angle_deg: float = 45.0,
    tooth_height: float | None = None,
    tip_radius: float = 0.0,
    root_radius: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, Any]:
    """Generate a single ratchet tooth slot for pocket + polar_pattern.

    Returns sketch elements describing the gap between two adjacent ratchet
    teeth.  The profile is a closed wire suitable for FreeCAD pocket operations.
    """
    if teeth < 4:
        raise ValueError(f"teeth must be >= 4, got {teeth}")
    if locking_face_angle_deg >= drive_face_angle_deg:
        raise ValueError("locking_face_angle must be less than drive_face_angle")

    rp = pitch_diameter / 2.0
    if tooth_height is None:
        tooth_height = pitch_diameter * 0.04  # ~4% of pitch diameter

    tip_r = rp + tooth_height / 2.0
    root_r = rp - tooth_height / 2.0

    tooth_pitch_rad = 2.0 * math.pi / teeth
    lock_rad = math.radians(locking_face_angle_deg)
    drive_rad = math.radians(drive_face_angle_deg)

    # Tooth slot geometry:
    # The locking face is nearly radial (steep), the drive face is angled.
    # Slot spans from one tooth's drive face to the next tooth's locking face.

    # Locking face of tooth 0 (right side of slot, nearly radial)
    lock_angle_offset = lock_rad * tooth_height / (2.0 * rp)
    # Drive face of tooth 1 (left side of slot, angled)
    drive_angle_offset = drive_rad * tooth_height / (2.0 * rp)

    # Positions at the tip circle
    lock_tip_angle = lock_angle_offset
    drive_tip_angle = tooth_pitch_rad - drive_angle_offset

    # Positions at the root circle
    lock_root_angle = -lock_angle_offset * 0.2
    drive_root_angle = tooth_pitch_rad + drive_angle_offset * 0.2

    elements: list[dict[str, Any]] = []

    # Locking face (tip to root) — steep, nearly radial line
    lock_tip = [
        center_x + tip_r * math.cos(lock_tip_angle),
        center_y + tip_r * math.sin(lock_tip_angle),
    ]
    lock_root = [
        center_x + root_r * math.cos(lock_root_angle),
        center_y + root_r * math.sin(lock_root_angle),
    ]
    elements.append(
        {
            "type": "line",
            "x1": lock_tip[0],
            "y1": lock_tip[1],
            "x2": lock_root[0],
            "y2": lock_root[1],
        }
    )

    # Root arc (from locking root to drive root)
    root_start_deg = math.degrees(lock_root_angle)
    root_end_deg = math.degrees(drive_root_angle)
    if root_end_deg < root_start_deg:
        root_end_deg += 360.0
    elements.append(
        {
            "type": "arc",
            "cx": center_x,
            "cy": center_y,
            "r": root_r + root_radius,
            "start_angle": root_start_deg,
            "end_angle": root_end_deg,
        }
    )

    # Drive face (root to tip) — gradual angle
    drive_root = [
        center_x + root_r * math.cos(drive_root_angle),
        center_y + root_r * math.sin(drive_root_angle),
    ]
    drive_tip = [
        center_x + tip_r * math.cos(drive_tip_angle),
        center_y + tip_r * math.sin(drive_tip_angle),
    ]
    elements.append(
        {
            "type": "line",
            "x1": drive_root[0],
            "y1": drive_root[1],
            "x2": drive_tip[0],
            "y2": drive_tip[1],
        }
    )

    # Tip arc (from drive tip back to locking tip)
    tip_start_deg = math.degrees(drive_tip_angle)
    tip_end_deg = math.degrees(lock_tip_angle)
    if tip_end_deg < tip_start_deg:
        tip_end_deg += 360.0
    elements.append(
        {
            "type": "arc",
            "cx": center_x,
            "cy": center_y,
            "r": tip_r - tip_radius,
            "start_angle": tip_start_deg,
            "end_angle": tip_end_deg,
        }
    )

    return {
        "elements": elements,
        "teeth": teeth,
        "tip_diameter": tip_r * 2.0,
        "root_diameter": root_r * 2.0,
        "pitch_diameter": pitch_diameter,
        "locking_face_angle_deg": locking_face_angle_deg,
        "drive_face_angle_deg": drive_face_angle_deg,
        "build_hint": (
            "1. Create a blank cylinder: cad.sketch (circle, r=tip_diameter/2) → cad.pad\n"
            "2. cad.sketch with these elements → cad.pocket (ThroughAll)\n"
            "3. cad.polar_pattern(features=['Pocket'], occurrences=teeth)"
        ),
    }
