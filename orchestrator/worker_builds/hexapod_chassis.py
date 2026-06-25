"""Hexapod chassis builder — flat plate with 6 leg-mount through-holes.

Companion to ``hexapod_leg`` (chunk 8). Together they're the seven
worker outputs the orchestrator dispatches when building a complete
six-legged robot from spec.

Geometry: a square plate (side = 2 × chassis_radius_mm), `hexapod_leg_count`
through-holes evenly spaced on a pitch circle at `mount_pcd_mm`, and one
optional central cable-routing bore. Routes through ``_build_envelope``
via ``sub_spec["envelope_holes"]`` so all the leg-mount positions are
carved as ThroughAll pockets in a single pass.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def _leg_mount_positions(
    leg_count: int,
    mount_pcd_mm: float,
    phase_deg: float = 0.0,
) -> list[tuple[float, float]]:
    """Evenly-spaced leg-mount centers on the chassis PCD."""
    if leg_count <= 0 or mount_pcd_mm <= 0:
        return []
    r = mount_pcd_mm / 2.0
    step = 360.0 / leg_count
    return [
        (
            r * math.cos(math.radians(phase_deg + i * step)),
            r * math.sin(math.radians(phase_deg + i * step)),
        )
        for i in range(leg_count)
    ]


def build_hexapod_chassis(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
) -> Path:
    """Build a hexapod-chassis STEP from a worker sub_spec.

    ``sub_spec`` fields (defaults match v3 hexapod model dimensions):

    =================================  ======  ==========================================
    field                              dflt    meaning
    =================================  ======  ==========================================
    ``name``/``subsystem``              "hexapod_chassis"
    ``chassis_radius_mm``                75.0  half-side of the square footprint
    ``thickness_mm``                      5.0  plate thickness (Z)
    ``leg_count``                            6  number of legs
    ``mount_pcd_mm``                    110.0  pitch circle on which leg mounts sit
                                                (default = 2 × COXA_SERVO_RADIUS=55)
    ``mount_hole_diameter_mm``            4.0  M4 clearance for leg pivots
    ``central_bore_diameter_mm``         12.0  cable routing (0 = skip)
    =================================  ======  ==========================================
    """
    part_name = sub_spec.get("name", sub_spec.get("subsystem", "hexapod_chassis"))
    chassis_radius_mm = float(sub_spec.get("chassis_radius_mm", 75.0))
    thickness_mm = float(sub_spec.get("thickness_mm", 5.0))
    leg_count = int(sub_spec.get("leg_count", 6))
    mount_pcd_mm = float(sub_spec.get("mount_pcd_mm", 110.0))
    mount_hole_dia = float(sub_spec.get("mount_hole_diameter_mm", 4.0))
    central_bore_dia = float(sub_spec.get("central_bore_diameter_mm", 12.0))

    side_mm = 2 * chassis_radius_mm

    envelope_holes: list[dict[str, Any]] = []
    if central_bore_dia > 0:
        envelope_holes.append({
            "cx": 0.0, "cy": 0.0,
            "diameter_mm": central_bore_dia,
            "type": "pocket", "depth_mm": 0.0,
        })
    for cx, cy in _leg_mount_positions(leg_count, mount_pcd_mm):
        envelope_holes.append({
            "cx": cx, "cy": cy,
            "diameter_mm": mount_hole_dia,
            "type": "pocket", "depth_mm": 0.0,
        })

    build_spec: dict[str, Any] = dict(sub_spec)
    build_spec["name"] = part_name
    build_spec["build_type"] = "envelope"
    build_spec["envelope_holes"] = envelope_holes
    build_spec["envelope_mm"] = [side_mm, side_mm, thickness_mm]
    build_spec["params"] = {
        "chassis_radius_mm": chassis_radius_mm,
        "thickness_mm": thickness_mm,
        "leg_count": leg_count,
        "mount_pcd_mm": mount_pcd_mm,
        "mount_hole_diameter_mm": mount_hole_dia,
        "central_bore_diameter_mm": central_bore_dia,
    }

    if interfaces is not None and interfaces:
        ids = [ifc.get("id", "") for ifc in interfaces]
        central_id = ids[0] if len(ids) >= 1 and ids[0] else "ifc_central"
        mounts_id = ids[1] if len(ids) >= 2 and ids[1] else "ifc_mounts"
    else:
        central_id, mounts_id = "ifc_central", "ifc_mounts"

    interface_actuals: dict[str, dict[str, float]] = {
        mounts_id: {
            "bore_dia": mount_hole_dia,
            "motor_mount_pcd": mount_pcd_mm,
        },
    }
    if central_bore_dia > 0:
        interface_actuals[central_id] = {"bore_dia": central_bore_dia}

    return common.dispatch_and_rewrite(
        build_spec=build_spec,
        output_dir=output_dir,
        part_name=part_name,
        interface_actuals=interface_actuals,
        notes=(
            f"hexapod_chassis builder: {side_mm}×{side_mm}×{thickness_mm}, "
            f"{leg_count} mounts @ PCD={mount_pcd_mm}, "
            f"central_bore={central_bore_dia}"
        ),
        claimed_mass_kg=0.150,
    )


__all__ = ["build_hexapod_chassis"]
