"""Quadrotor-arm builder — chunk 6 of wiring the loops.

Cross-domain part class: tests the pattern's generality on something
that isn't a gear-train component. Geometry: rectangular boom with
a single root-mount hole at one end (chassis pivot) and a 4-hole
motor-mount pattern at the other (square or circular).

Build path: ``_build_envelope`` (the default route in
``worker_entry._build_geometry``), with holes packed into
``sub_spec["envelope_holes"]`` so they land at non-origin positions.

The orchestrator-side measurement uses two strategies:
  - ``bore_dia`` for individual hole diameters (root + motor holes)
  - ``motor_mount_pcd`` for the pitch-circle of the 4-hole pattern
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def _motor_mount_positions(
    pattern: str,
    pcd_mm: float,
    hole_count: int,
    cx: float,
    cy: float,
    phase_deg: float = 45.0,
) -> list[tuple[float, float]]:
    """Compute the (x, y) centers of the motor-mount holes.

    For ``pattern == "square"`` (4 holes only): corners of a square
    inscribed in the PCD circle. For ``pattern == "circular"``:
    ``hole_count`` evenly-spaced points on the circle. Phase rotates
    the pattern (default 45° puts square corners off-axis).
    """
    r = pcd_mm / 2.0
    if pattern == "square":
        # Ignore hole_count and use 4. Square diagonal == PCD.
        return [
            (
                cx + r * math.cos(math.radians(phase_deg + i * 90)),
                cy + r * math.sin(math.radians(phase_deg + i * 90)),
            )
            for i in range(4)
        ]
    step = 360.0 / max(hole_count, 1)
    return [
        (
            cx + r * math.cos(math.radians(phase_deg + i * step)),
            cy + r * math.sin(math.radians(phase_deg + i * step)),
        )
        for i in range(hole_count)
    ]


def build_quadrotor_arm(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
) -> Path:
    """Build a quadrotor-arm STEP from a worker sub_spec.

    ``sub_spec`` fields (defaults sized for a small FPV quad arm):

    =================================  =======  ====================================
    field                              dflt     meaning
    =================================  =======  ====================================
    ``name``/``subsystem``              "quadrotor_arm"
    ``length_mm``                       120.0   boom length (X)
    ``width_mm``                         18.0   boom width (Y)
    ``height_mm``                         8.0   boom height (Z)
    ``root_mount_diameter_mm``            5.0   chassis-end pivot bore
    ``motor_mount_pattern``           "square"  "square" (4) or "circular"
    ``motor_mount_pcd_mm``               16.0   pitch circle / square diagonal
    ``motor_mount_hole_count``              4   motor hole count
    ``motor_mount_hole_diameter_mm``      3.2   motor hole diameter (M3)
    =================================  =======  ====================================

    The boom lies along +X. The root mount sits at (-length/2 + width,
    0); the motor mount sits at (+length/2 - width, 0). All holes are
    ThroughAll pockets.
    """
    part_name = sub_spec.get("name", sub_spec.get("subsystem", "quadrotor_arm"))
    length_mm = float(sub_spec.get("length_mm", 120.0))
    width_mm = float(sub_spec.get("width_mm", 18.0))
    height_mm = float(sub_spec.get("height_mm", 8.0))
    root_dia = float(sub_spec.get("root_mount_diameter_mm", 5.0))
    pattern = str(sub_spec.get("motor_mount_pattern", "square"))
    pcd_mm = float(sub_spec.get("motor_mount_pcd_mm", 16.0))
    hole_count = int(sub_spec.get("motor_mount_hole_count", 4))
    hole_dia = float(sub_spec.get("motor_mount_hole_diameter_mm", 3.2))

    # Inset both mounts by ~width from the ends so we don't break the boundary.
    root_cx = -length_mm / 2.0 + width_mm
    root_cy = 0.0
    motor_cx = length_mm / 2.0 - width_mm
    motor_cy = 0.0

    motor_positions = _motor_mount_positions(
        pattern,
        pcd_mm,
        hole_count,
        motor_cx,
        motor_cy,
    )

    envelope_holes: list[dict[str, Any]] = [
        {
            "cx": root_cx,
            "cy": root_cy,
            "diameter_mm": root_dia,
            "type": "pocket",
            "depth_mm": 0.0,  # ThroughAll
        },
    ]
    for mcx, mcy in motor_positions:
        envelope_holes.append(
            {
                "cx": mcx,
                "cy": mcy,
                "diameter_mm": hole_dia,
                "type": "pocket",
                "depth_mm": 0.0,  # ThroughAll
            }
        )

    build_spec: dict[str, Any] = dict(sub_spec)
    build_spec["name"] = part_name
    build_spec["build_type"] = "envelope"
    build_spec["envelope_holes"] = envelope_holes
    build_spec["envelope_mm"] = [length_mm, width_mm, height_mm]
    build_spec["params"] = {
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "root_mount_diameter_mm": root_dia,
        "motor_mount_pattern": pattern,
        "motor_mount_pcd_mm": pcd_mm,
        "motor_mount_hole_count": hole_count,
        "motor_mount_hole_diameter_mm": hole_dia,
    }

    if interfaces is not None and interfaces:
        root_id = interfaces[0].get("id", "ifc_root")
        motor_id = interfaces[1].get("id", "ifc_motor") if len(interfaces) > 1 else "ifc_motor"
    else:
        root_id, motor_id = "ifc_root", "ifc_motor"

    return common.dispatch_and_rewrite(
        build_spec=build_spec,
        output_dir=output_dir,
        part_name=part_name,
        interface_actuals={
            root_id: {"bore_dia": root_dia},
            motor_id: {
                "bore_dia": hole_dia,
                "motor_mount_pcd": pcd_mm,
            },
        },
        notes=(
            f"quadrotor_arm builder: {length_mm}×{width_mm}×{height_mm}, "
            f"root_dia={root_dia}, motor pattern={pattern}@PCD={pcd_mm} "
            f"({hole_count}×Ø{hole_dia})"
        ),
        claimed_mass_kg=0.012,
    )


__all__ = ["build_quadrotor_arm"]
