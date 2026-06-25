"""End-to-end hexapod robot build through the outer orchestrator loop.

Spins up an orchestrator run, walks G0 → G5, dispatches **seven** worker
builds (1 chassis + 6 hexapod legs), then re-imports each STEP and
validates the produced geometry against the spec's frozen
interface-control documents (ICDs).

This is the v0.2.0 follow-up to ``examples/planetary_gearbox/`` — a
larger, structurally different assembly that exercises:

* The chunk-8 multi-segment ``hexapod_leg`` builder (coxa+femur+tibia
  fused into a single body, with three distinct pivot bores per leg)
* The new ``hexapod_chassis`` builder (square plate + central cable
  bore + 6 leg-mount holes on PCD)
* The orchestrator coordinating 7 worker outputs across two distinct
  build paths (``_build_envelope`` for the chassis, ``_build_leg`` for
  each leg) in a single run

Run from repo root with FreeCAD addon listening on :9876:

    PYTHONPATH=. python3 examples/hexapod_robot/run.py [--out OUTPUT_DIR]

Output: a directory containing 7 STEP files (chassis + 6 legs), one
STL per part, a metadata.json per part, and an
``orchestrator_report.txt`` summarizing the gate walk + validation.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Spec parameters — match the v3 hexapod model dimensions per CLAUDE.md
# memory ("19 composite bodies, 18 revolute joints, standing height
# 0.212m"). Our orchestrator-built version uses the coarser 7-body
# composite (one body per leg + chassis) but the same segment sizes so
# the URDF export downstream lines up with the existing v3 pipeline.
# ---------------------------------------------------------------------------

# Chassis
CHASSIS_RADIUS_MM = 75.0
CHASSIS_THICKNESS_MM = 5.0
LEG_MOUNT_PCD_MM = 110.0          # = 2 × COXA_SERVO_RADIUS=55 from v3 model
LEG_MOUNT_HOLE_DIA_MM = 4.0       # M4 clearance
CENTRAL_BORE_DIA_MM = 12.0        # cable routing

# Leg segments
COXA_LENGTH_MM = 52.0
FEMUR_LENGTH_MM = 66.0
TIBIA_LENGTH_MM = 133.0
SEGMENT_WIDTH_MM = 20.0
SEGMENT_THICKNESS_MM = 8.0

# Pivot bores — distinct sizes so _measure_bore_diameter's expected_mm
# hint can disambiguate them on the imported STEP
HIP_YAW_BORE_MM = 4.0    # M4 (matches chassis mount holes)
HIP_PITCH_BORE_MM = 5.0  # M5
KNEE_BORE_MM = 6.0       # M6

NUM_LEGS = 6
LEG_ANGLES_DEG = [0, 60, 120, 180, 240, 300]
TOTAL_LEG_LENGTH = COXA_LENGTH_MM + FEMUR_LENGTH_MM + TIBIA_LENGTH_MM


def _leg_mount_positions() -> list[tuple[float, float]]:
    r = LEG_MOUNT_PCD_MM / 2.0
    return [
        (
            r * math.cos(math.radians(a)),
            r * math.sin(math.radians(a)),
        )
        for a in LEG_ANGLES_DEG
    ]


# ---------------------------------------------------------------------------
# Spec construction
# ---------------------------------------------------------------------------


def _make_spec(run_dir: Path):
    from orchestrator.runner import init_run, save_spec, transition
    from orchestrator.spec import (
        AssemblySkeleton,
        CoordinateFrame,
        Interface,
        MatingSemantic,
        Objective,
        SpecStatus,
        Subsystem,
        SubsystemKind,
        ValidationCheckPoint,
        ValidationMethod,
    )

    run = init_run("Hexapod Robot 6-leg", run_dir=run_dir)
    transition(run, SpecStatus.NORMALIZING, reason="example start")

    run.spec.objectives = [
        Objective(name="standing_height", direction="maximize", unit="mm",
                  weight=1.0, threshold=200.0),
        Objective(name="mass", direction="minimize", unit="kg",
                  weight=0.5, threshold=2.0),
    ]
    run.spec.global_constraints = {
        "max_mass_kg": 2.0,
        "leg_count": NUM_LEGS,
    }
    side_mm = 2 * CHASSIS_RADIUS_MM
    run.spec.skeleton = AssemblySkeleton(
        datums={"body_origin": [0, 0, 0]},
        reserved_volumes={
            "body_envelope": {
                "origin": [0, 0, 0],
                "size": [side_mm + TOTAL_LEG_LENGTH * 1.5,
                         side_mm + TOTAL_LEG_LENGTH * 1.5,
                         CHASSIS_THICKNESS_MM + SEGMENT_THICKNESS_MM + 10],
            },
        },
    )

    subsystems = [
        Subsystem(
            id="s_chassis",
            name="hexapod_chassis",
            kind=SubsystemKind.GENERATED,
            envelope_mm=[side_mm + 1, side_mm + 1, CHASSIS_THICKNESS_MM + 1],
            mass_budget_kg=0.3,
            material="aluminum",
            interfaces=["ifc_chassis_central", "ifc_chassis_mounts"],
            worker_count=1,
            assembly_constraints={"datum": "body_origin"},
        ),
    ]
    interfaces = [
        Interface(
            id="ifc_chassis_central",
            name="central_cable_route",
            subsystem_a="hexapod_chassis",
            port_a="cable",
            subsystem_b="electronics",
            port_b="harness",
            geometry={"diameter_mm": CENTRAL_BORE_DIA_MM},
            frame_a=CoordinateFrame(origin_mm=[0, 0, CHASSIS_THICKNESS_MM / 2]),
            frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
            mating=MatingSemantic(type="cylindrical_fit"),
            validation=ValidationMethod(check_points=[
                ValidationCheckPoint(feature="bore_dia",
                                     expected_mm=CENTRAL_BORE_DIA_MM,
                                     tolerance_mm=0.3),
            ]),
        ),
        Interface(
            id="ifc_chassis_mounts",
            name="leg_mount_pattern",
            subsystem_a="hexapod_chassis",
            port_a="mount_holes",
            subsystem_b="legs",
            port_b="hip_yaw_bores",
            geometry={"diameter_mm": LEG_MOUNT_HOLE_DIA_MM},
            frame_a=CoordinateFrame(origin_mm=[0, 0, CHASSIS_THICKNESS_MM / 2]),
            frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
            mating=MatingSemantic(type="bolt_pattern"),
            validation=ValidationMethod(check_points=[
                ValidationCheckPoint(feature="motor_mount_pcd",
                                     expected_mm=LEG_MOUNT_PCD_MM,
                                     tolerance_mm=0.5),
            ]),
        ),
    ]

    # 6 legs as separate subsystems — each gets its own worker dispatch.
    for i, _ in enumerate(LEG_ANGLES_DEG, start=1):
        leg_name = f"leg_{i}"
        subsystems.append(Subsystem(
            id=f"s_leg_{i}",
            name=leg_name,
            kind=SubsystemKind.GENERATED,
            envelope_mm=[TOTAL_LEG_LENGTH + 1, SEGMENT_WIDTH_MM + 1,
                         SEGMENT_THICKNESS_MM + 1],
            mass_budget_kg=0.15,
            material="aluminum",
            interfaces=[
                f"ifc_leg_{i}_hip_yaw",
                f"ifc_leg_{i}_hip_pitch",
                f"ifc_leg_{i}_knee",
            ],
            worker_count=1,
            assembly_constraints={"datum": "body_origin"},
        ))
        # Three pivot bores per leg, each a distinct interface so
        # _measure_bore_diameter's expected_mm hint can disambiguate.
        for label, dia in (
            ("hip_yaw", HIP_YAW_BORE_MM),
            ("hip_pitch", HIP_PITCH_BORE_MM),
            ("knee", KNEE_BORE_MM),
        ):
            interfaces.append(Interface(
                id=f"ifc_leg_{i}_{label}",
                name=f"leg_{i}_{label}_pivot",
                subsystem_a=leg_name,
                port_a=label,
                subsystem_b="adjacent_segment" if label != "hip_yaw" else "chassis",
                port_b="pivot",
                geometry={"diameter_mm": dia},
                frame_a=CoordinateFrame(origin_mm=[0, 0, SEGMENT_THICKNESS_MM / 2]),
                frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                mating=MatingSemantic(type="cylindrical_fit"),
                validation=ValidationMethod(check_points=[
                    ValidationCheckPoint(feature="bore_dia",
                                         expected_mm=dia,
                                         tolerance_mm=0.2),
                ]),
            ))

    run.spec.subsystems = subsystems
    run.spec.interfaces = interfaces
    save_spec(run)
    return run


def _walk_to_building(run) -> None:
    from orchestrator.runner import (
        check_gate_g0,
        check_gate_g1,
        check_gate_g2,
        check_gate_g3,
        transition,
    )
    from orchestrator.spec import SpecStatus

    for label, fn, next_state in (
        ("G0", check_gate_g0, SpecStatus.COUNCIL_REVIEW),
        ("G1", check_gate_g1, None),
        ("G2", check_gate_g2, SpecStatus.LAYOUT_FROZEN),
    ):
        ok, issues = fn(run.spec)
        print(f"  {label}: {'✓' if ok else '✗'}", "  " + str(issues) if issues else "")
        if not ok:
            sys.exit(1)
        if next_state:
            transition(run, next_state, reason=f"{label} pass")
    check_gate_g3(run.spec)
    transition(run, SpecStatus.INTERFACES_FROZEN, reason="G3 pass")
    transition(run, SpecStatus.BUILDING, reason="dispatch workers")


# ---------------------------------------------------------------------------
# Worker dispatch
# ---------------------------------------------------------------------------


def _dispatch_workers(run) -> dict[str, Path]:
    """Build all 7 subsystems sequentially (FreeCAD socket is single-doc-at-a-time)."""
    from orchestrator.runner import build_worker_prompts
    from orchestrator.worker_builds import (
        hexapod_chassis as chassis_mod,
    )
    from orchestrator.worker_builds import (
        hexapod_leg as leg_mod,
    )

    prompts = {p["subsystem"]: Path(p["output_dir"]) for p in build_worker_prompts(run)}
    results: dict[str, Path] = {}

    # 1. Chassis
    print("  building hexapod_chassis...")
    results["hexapod_chassis"] = chassis_mod.build_hexapod_chassis(
        sub_spec={
            "name": "hexapod_chassis",
            "chassis_radius_mm": CHASSIS_RADIUS_MM,
            "thickness_mm": CHASSIS_THICKNESS_MM,
            "leg_count": NUM_LEGS,
            "mount_pcd_mm": LEG_MOUNT_PCD_MM,
            "mount_hole_diameter_mm": LEG_MOUNT_HOLE_DIA_MM,
            "central_bore_diameter_mm": CENTRAL_BORE_DIA_MM,
        },
        output_dir=prompts["hexapod_chassis"],
    )

    # 2. Six legs (each is identical geometry; the assembly step is
    # what places them at the 6 mount positions)
    for i, (px, py) in enumerate(_leg_mount_positions(), start=1):
        leg_name = f"leg_{i}"
        print(f"  building {leg_name} (mount at +{px:+.1f},{py:+.1f}, "
              f"angle={LEG_ANGLES_DEG[i-1]}°)...")
        results[leg_name] = leg_mod.build_hexapod_leg(
            sub_spec={
                "name": leg_name,
                "coxa_length_mm": COXA_LENGTH_MM,
                "femur_length_mm": FEMUR_LENGTH_MM,
                "tibia_length_mm": TIBIA_LENGTH_MM,
                "segment_width_mm": SEGMENT_WIDTH_MM,
                "segment_thickness_mm": SEGMENT_THICKNESS_MM,
                "hip_yaw_bore_mm": HIP_YAW_BORE_MM,
                "hip_pitch_bore_mm": HIP_PITCH_BORE_MM,
                "knee_bore_mm": KNEE_BORE_MM,
            },
            output_dir=prompts[leg_name],
        )

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--out", type=Path, default=Path("/tmp/hexapod_robot_run"),
                    help="Directory to write the run output into")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not args.quiet:
        logging.basicConfig(level=logging.WARNING,
                            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"Hexapod robot — {NUM_LEGS} legs at {LEG_ANGLES_DEG}°")
    print(f"  chassis: {2*CHASSIS_RADIUS_MM}×{2*CHASSIS_RADIUS_MM}×{CHASSIS_THICKNESS_MM} mm, "
          f"PCD={LEG_MOUNT_PCD_MM}")
    print(f"  leg: coxa={COXA_LENGTH_MM} + femur={FEMUR_LENGTH_MM} + tibia={TIBIA_LENGTH_MM} = "
          f"{TOTAL_LEG_LENGTH} mm")
    print(f"  pivot bores: hip_yaw={HIP_YAW_BORE_MM}, hip_pitch={HIP_PITCH_BORE_MM}, "
          f"knee={KNEE_BORE_MM} mm")
    print(f"Run output: {out}\n")

    from orchestrator.worker_builds import common
    if not common.freecad_ready():
        print(f"ERROR: FreeCAD addon not reachable at "
              f"{common.fc_host()}:{common.fc_port()}.", file=sys.stderr)
        return 1
    if not common.freecad_ready_with_import_step():
        print("ERROR: FreeCAD addon is running but cad_import_step is not "
              "registered. Reload the addon.", file=sys.stderr)
        return 1

    print("[1/4] G0 → G3 gate walk")
    run = _make_spec(out / "run")
    _walk_to_building(run)

    print(f"\n[2/4] Dispatch {len(run.spec.subsystems)} worker builds "
          f"(1 chassis + {NUM_LEGS} legs)")
    t0 = time.monotonic()
    step_paths = _dispatch_workers(run)
    print(f"  built {len(step_paths)} parts in {time.monotonic() - t0:.1f}s")

    print("\n[3/4] G4 (artifact check)")
    from orchestrator.runner import check_gate_g4, transition, validate_results
    from orchestrator.spec import SpecStatus
    ok_g4, issues = check_gate_g4(run)
    print(f"  G4: {'✓' if ok_g4 else '✗'}", issues if issues else "")
    if not ok_g4:
        return 1
    transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

    print("\n[4/4] G5 (verify-mode validation — orchestrator re-measures every STEP)")
    reports = validate_results(run, verify_measurements=True)
    print(f"  validated {len(reports)} parts:")
    for r in reports:
        status = "✓" if r.overall_pass else "✗"
        print(f"    {status} {r.subsystem_name} (source={r.measurement_source})")
        for dc in r.dimension_checks:
            mark = "✓" if dc.passed else "✗"
            print(f"        {mark} {dc.interface_id}.{dc.feature}: "
                  f"measured={dc.measured_mm}, expected={dc.expected_mm}±{dc.tolerance_mm}")
        if r.failure_codes:
            print(f"        failure_codes: {[fc.value for fc in r.failure_codes]}")

    all_pass = all(r.overall_pass for r in reports)
    print(f"\nResult: {'ALL PARTS PASS' if all_pass else 'AT LEAST ONE PART FAILED'}")
    print(f"Total leg DOF in this hexapod (3 per leg × {NUM_LEGS} legs): "
          f"{3 * NUM_LEGS} revolute joints when assembled")

    summary = out / "orchestrator_report.txt"
    summary.write_text(
        f"Hexapod Robot — orchestrator run summary\n"
        f"========================================\n\n"
        f"chassis: {2*CHASSIS_RADIUS_MM}×{2*CHASSIS_RADIUS_MM}×{CHASSIS_THICKNESS_MM} mm, "
        f"PCD={LEG_MOUNT_PCD_MM}\n"
        f"legs: {NUM_LEGS}× hexapod_leg "
        f"({COXA_LENGTH_MM}+{FEMUR_LENGTH_MM}+{TIBIA_LENGTH_MM}={TOTAL_LEG_LENGTH} mm)\n"
        f"pivot bores: hip_yaw={HIP_YAW_BORE_MM}, hip_pitch={HIP_PITCH_BORE_MM}, "
        f"knee={KNEE_BORE_MM}\n\n"
        f"Parts built and validated:\n"
        + "\n".join(f"  {'✓' if r.overall_pass else '✗'} {r.subsystem_name} "
                   f"(source={r.measurement_source})" for r in reports)
        + f"\n\nAll parts passing: {all_pass}\n"
        f"DOF count when assembled: {3 * NUM_LEGS}\n"
    )
    print(f"\nReport written to: {summary}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
