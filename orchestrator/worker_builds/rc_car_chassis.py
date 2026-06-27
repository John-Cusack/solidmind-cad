"""RC-car chassis builder — chunk 7 of wiring the loops.

Larger envelope with more interface features than the quadrotor arm:
front + rear axle bores on centerline, plus a 4-hole motor-mount
pattern. Tests that the envelope route scales to multiple cylindrical
features at independent positions without needing a custom dispatcher.

Build path: ``_build_envelope`` via ``sub_spec["envelope_holes"]``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def _mounting_hole_positions(
    pcd_mm: float,
    hole_count: int,
    cx: float = 0.0,
    cy: float = 0.0,
    phase_deg: float = 45.0,
) -> list[tuple[float, float]]:
    """Evenly-spaced points on a circle around (cx, cy)."""
    if hole_count <= 0 or pcd_mm <= 0:
        return []
    r = pcd_mm / 2.0
    step = 360.0 / hole_count
    return [
        (
            cx + r * math.cos(math.radians(phase_deg + i * step)),
            cy + r * math.sin(math.radians(phase_deg + i * step)),
        )
        for i in range(hole_count)
    ]


def build_rc_car_chassis(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
) -> Path:
    """Build an RC-car chassis-plate STEP from a worker sub_spec.

    ``sub_spec`` fields (defaults sized for a small 1/24-scale chassis):

    =================================  =======  ====================================
    field                              dflt     meaning
    =================================  =======  ====================================
    ``name``/``subsystem``              "rc_car_chassis"
    ``length_mm``                       180.0   chassis length (X)
    ``width_mm``                         90.0   chassis width (Y)
    ``thickness_mm``                      4.0   plate thickness (Z)
    ``axle_bore_diameter_mm``             6.0   axle clearance bore
    ``wheelbase_mm``                    130.0   distance between axles (X)
    ``mounting_hole_count``                 4   center-mount holes (motor/RX)
    ``mounting_hole_diameter_mm``         3.0   mount hole dia
    ``mounting_hole_pcd_mm``             60.0   mount-hole PCD (centered)
    =================================  =======  ====================================

    Geometry: a flat plate with 2 axle bores on centerline, plus a
    4-hole mounting pattern at the center. All holes are ThroughAll.
    """
    part_name = sub_spec.get("name", sub_spec.get("subsystem", "rc_car_chassis"))
    length_mm = float(sub_spec.get("length_mm", 180.0))
    width_mm = float(sub_spec.get("width_mm", 90.0))
    thickness_mm = float(sub_spec.get("thickness_mm", 4.0))
    axle_bore_dia = float(sub_spec.get("axle_bore_diameter_mm", 6.0))
    wheelbase_mm = float(sub_spec.get("wheelbase_mm", 130.0))
    mount_count = int(sub_spec.get("mounting_hole_count", 4))
    mount_dia = float(sub_spec.get("mounting_hole_diameter_mm", 3.0))
    mount_pcd = float(sub_spec.get("mounting_hole_pcd_mm", 60.0))

    half_wb = wheelbase_mm / 2.0
    mount_positions = _mounting_hole_positions(mount_pcd, mount_count)

    envelope_holes: list[dict[str, Any]] = [
        # Front axle bore (on centerline, +X)
        {
            "cx": +half_wb,
            "cy": 0.0,
            "diameter_mm": axle_bore_dia,
            "type": "pocket",
            "depth_mm": 0.0,
        },
        # Rear axle bore (on centerline, -X)
        {
            "cx": -half_wb,
            "cy": 0.0,
            "diameter_mm": axle_bore_dia,
            "type": "pocket",
            "depth_mm": 0.0,
        },
    ]
    for cx, cy in mount_positions:
        envelope_holes.append(
            {
                "cx": cx,
                "cy": cy,
                "diameter_mm": mount_dia,
                "type": "pocket",
                "depth_mm": 0.0,
            }
        )

    build_spec: dict[str, Any] = dict(sub_spec)
    build_spec["name"] = part_name
    build_spec["build_type"] = "envelope"
    build_spec["envelope_holes"] = envelope_holes
    build_spec["envelope_mm"] = [length_mm, width_mm, thickness_mm]
    build_spec["params"] = {
        "length_mm": length_mm,
        "width_mm": width_mm,
        "thickness_mm": thickness_mm,
        "axle_bore_diameter_mm": axle_bore_dia,
        "wheelbase_mm": wheelbase_mm,
        "mounting_hole_count": mount_count,
        "mounting_hole_diameter_mm": mount_dia,
        "mounting_hole_pcd_mm": mount_pcd,
    }

    if interfaces is not None and interfaces:
        ids: list[str] = [ifc.get("id", "") for ifc in interfaces]
        front_id = ids[0] if len(ids) >= 1 and ids[0] else "ifc_axle_front"
        rear_id = ids[1] if len(ids) >= 2 and ids[1] else "ifc_axle_rear"
        mounts_id = ids[2] if len(ids) >= 3 and ids[2] else "ifc_mounts"
    else:
        front_id, rear_id, mounts_id = (
            "ifc_axle_front",
            "ifc_axle_rear",
            "ifc_mounts",
        )

    return common.dispatch_and_rewrite(
        build_spec=build_spec,
        output_dir=output_dir,
        part_name=part_name,
        interface_actuals={
            front_id: {"bore_dia": axle_bore_dia},
            rear_id: {"bore_dia": axle_bore_dia},
            mounts_id: {
                "bore_dia": mount_dia,
                "motor_mount_pcd": mount_pcd,
            },
        },
        notes=(
            f"rc_car_chassis builder: {length_mm}×{width_mm}×{thickness_mm}, "
            f"axles@{wheelbase_mm}mm WB, mounts {mount_count}×Ø{mount_dia}@PCD={mount_pcd}"
        ),
        claimed_mass_kg=0.080,
    )


__all__ = ["build_rc_car_chassis"]
