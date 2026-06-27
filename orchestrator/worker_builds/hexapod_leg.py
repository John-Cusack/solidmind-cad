"""Hexapod-leg builder — chunk 8 of wiring the loops.

The most complex builder of the chunk-5–8 set. Single body containing
three composite segments (coxa, femur, tibia) laid end-to-end in +X,
fused into one continuous bar via shared sketch edges. Three pivot
bores at segment junctions are deliberately given distinct diameters
so the orchestrator's ``_measure_bore_diameter`` strategy can pick
each one out via the ``expected_mm`` hint registered on the matching
ValidationCheckPoint.

This is the only chunk that adds a new dispatch route to
``orchestrator.worker_entry`` (``build_type="leg"`` → ``_build_leg``).
The other three chunks reuse existing routes.

NOTE: servo pockets (rectangular depressions in the top face per
segment) are deferred. The addon's ``new_sketch`` route either uses
a plane (XY/XZ/YZ) or a face name, and the top-face name is not
predictable across multi-pad bodies. Adding a face-finder helper or
a datum-plane API to the addon is out of scope for this chunk; the
multi-segment composite + 3 distinct pivot bores already proves the
pattern handles complex geometry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def build_hexapod_leg(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
) -> Path:
    """Build a hexapod-leg STEP from a worker sub_spec.

    ``sub_spec`` fields (defaults sized for v3 hexapod-style legs):

    =================================  =======  ====================================
    field                              dflt     meaning
    =================================  =======  ====================================
    ``name``/``subsystem``              "hexapod_leg"
    ``coxa_length_mm``                   52.0   coxa segment length
    ``femur_length_mm``                  66.0   femur segment length
    ``tibia_length_mm``                 133.0   tibia segment length
    ``segment_width_mm``                 20.0   leg cross-section Y
    ``segment_thickness_mm``              8.0   leg cross-section Z
    ``hip_yaw_bore_mm``                   4.0   chassis-pivot bore (M4)
    ``hip_pitch_bore_mm``                 5.0   coxa-femur pivot (M5)
    ``knee_bore_mm``                      6.0   femur-tibia pivot (M6)
    =================================  =======  ====================================

    Distinct bore diameters let ``_measure_bore_diameter`` pick each
    one out of ``cad_find_holes`` results when called with the matching
    ``expected_mm`` hint.
    """
    part_name = sub_spec.get("name", sub_spec.get("subsystem", "hexapod_leg"))
    coxa_len = float(sub_spec.get("coxa_length_mm", 52.0))
    femur_len = float(sub_spec.get("femur_length_mm", 66.0))
    tibia_len = float(sub_spec.get("tibia_length_mm", 133.0))
    width = float(sub_spec.get("segment_width_mm", 20.0))
    thickness = float(sub_spec.get("segment_thickness_mm", 8.0))
    hip_yaw_bore = float(sub_spec.get("hip_yaw_bore_mm", 4.0))
    hip_pitch_bore = float(sub_spec.get("hip_pitch_bore_mm", 5.0))
    knee_bore = float(sub_spec.get("knee_bore_mm", 6.0))

    total_length = coxa_len + femur_len + tibia_len

    build_spec: dict[str, Any] = dict(sub_spec)
    build_spec["name"] = part_name
    build_spec["build_type"] = "leg"
    build_spec["coxa_length_mm"] = coxa_len
    build_spec["femur_length_mm"] = femur_len
    build_spec["tibia_length_mm"] = tibia_len
    build_spec["segment_width_mm"] = width
    build_spec["segment_thickness_mm"] = thickness
    build_spec["hip_yaw_bore_mm"] = hip_yaw_bore
    build_spec["hip_pitch_bore_mm"] = hip_pitch_bore
    build_spec["knee_bore_mm"] = knee_bore
    build_spec["envelope_mm"] = [total_length, width, thickness]
    build_spec["params"] = {
        "coxa_length_mm": coxa_len,
        "femur_length_mm": femur_len,
        "tibia_length_mm": tibia_len,
        "total_length_mm": total_length,
        "segment_width_mm": width,
        "segment_thickness_mm": thickness,
        "hip_yaw_bore_mm": hip_yaw_bore,
        "hip_pitch_bore_mm": hip_pitch_bore,
        "knee_bore_mm": knee_bore,
    }

    if interfaces is not None and interfaces:
        ids: list[str] = [ifc.get("id", "") for ifc in interfaces]
        hip_yaw_id = ids[0] if len(ids) >= 1 and ids[0] else "ifc_hip_yaw"
        hip_pitch_id = ids[1] if len(ids) >= 2 and ids[1] else "ifc_hip_pitch"
        knee_id = ids[2] if len(ids) >= 3 and ids[2] else "ifc_knee"
        segments_id = ids[3] if len(ids) >= 4 and ids[3] else "ifc_segments"
    else:
        hip_yaw_id, hip_pitch_id = "ifc_hip_yaw", "ifc_hip_pitch"
        knee_id, segments_id = "ifc_knee", "ifc_segments"

    return common.dispatch_and_rewrite(
        build_spec=build_spec,
        output_dir=output_dir,
        part_name=part_name,
        interface_actuals={
            hip_yaw_id: {"bore_dia": hip_yaw_bore},
            hip_pitch_id: {"bore_dia": hip_pitch_bore},
            knee_id: {"bore_dia": knee_bore},
            segments_id: {"segment_length": total_length},
        },
        notes=(
            f"hexapod_leg builder: coxa={coxa_len}, femur={femur_len}, "
            f"tibia={tibia_len}, bores=({hip_yaw_bore},{hip_pitch_bore},{knee_bore})"
        ),
        claimed_mass_kg=0.060,
    )


__all__ = ["build_hexapod_leg"]
