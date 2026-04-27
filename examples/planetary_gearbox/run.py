"""End-to-end planetary gearbox build through the outer orchestrator loop.

Spins up an orchestrator run, walks G0 → G5, dispatches **five** worker
builds in parallel (1 sun gear, 3 planets, 1 carrier, 1 ring gear), then
re-imports each STEP and validates that the produced geometry matches
the spec's frozen interface-control documents (ICDs).

This is the flagship demo of v0.2.0: the same outer loop closed on
``sun_gear`` / ``planet_carrier`` / ``quadrotor_arm`` / ``rc_car_chassis``
/ ``hexapod_leg`` standalone, now exercised together to produce a
real, multi-part **assembly** — the kind of design the orchestrator is
ultimately for.

Run from repo root with FreeCAD addon listening on :9876:

    python3 examples/planetary_gearbox/run.py [--out OUTPUT_DIR]

Output: a directory containing 5 STEP files (sun, planet × 3, carrier,
ring), one STL per part, a metadata.json per part, and an
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
# Spec parameters — picked so the planetary assembly condition holds:
#   (sun_teeth + ring_teeth) % num_planets == 0
# Module-1 gears with sun=12, planet=18, ring=48 → 5:1 ratio (ring fixed).
# ---------------------------------------------------------------------------

MODULE_MM = 1.0
SUN_TEETH = 12
PLANET_TEETH = 18
NUM_PLANETS = 3
RING_TEETH = 48              # = sun + 2*planet
GEAR_THICKNESS_MM = 8.0
RING_THICKNESS_MM = 10.0
SHAFT_BORE_MM = 6.0
PIN_BORE_MM = 4.0
CARRIER_OUTER_DIA_MM = 36.0
CARRIER_THICKNESS_MM = 5.0
PIN_HEIGHT_MM = 6.0

PITCH_DIAMETER_SUN_MM = SUN_TEETH * MODULE_MM            # 12
PITCH_DIAMETER_PLANET_MM = PLANET_TEETH * MODULE_MM       # 18
PITCH_DIAMETER_RING_MM = RING_TEETH * MODULE_MM           # 48
ORBIT_RADIUS_MM = (PITCH_DIAMETER_SUN_MM + PITCH_DIAMETER_PLANET_MM) / 2  # 15

# Closed-form planetary speed ratio with ring fixed, sun = input,
# carrier = output: ratio = 1 + ring_teeth / sun_teeth = 1 + 48/12 = 5.
SPEED_RATIO = 1.0 + RING_TEETH / SUN_TEETH


def _planet_positions() -> list[tuple[float, float]]:
    """3 planet centers evenly spaced on the orbit circle."""
    step = 360.0 / NUM_PLANETS
    return [
        (
            ORBIT_RADIUS_MM * math.cos(math.radians(i * step)),
            ORBIT_RADIUS_MM * math.sin(math.radians(i * step)),
        )
        for i in range(NUM_PLANETS)
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

    run = init_run("Planetary Gearbox 5:1", run_dir=run_dir)
    transition(run, SpecStatus.NORMALIZING, reason="example start")

    run.spec.objectives = [
        Objective(name="ratio_match", direction="maximize", unit="ratio",
                  weight=1.0, threshold=0.99),
        Objective(name="mass", direction="minimize", unit="kg",
                  weight=0.5, threshold=0.5),
    ]
    run.spec.global_constraints = {
        "max_mass_kg": 0.5,
        "target_speed_ratio": SPEED_RATIO,
        "module_mm": MODULE_MM,
    }
    run.spec.skeleton = AssemblySkeleton(
        datums={"central_axis": [0, 0, 0]},
        reserved_volumes={
            "gearbox_envelope": {
                "origin": [0, 0, 0],
                "size": [PITCH_DIAMETER_RING_MM + 8, PITCH_DIAMETER_RING_MM + 8,
                         max(GEAR_THICKNESS_MM, RING_THICKNESS_MM, CARRIER_THICKNESS_MM + PIN_HEIGHT_MM) + 2],
            },
        },
    )

    subsystems = [
        Subsystem(
            id="s_sun",
            name="sun_gear",
            kind=SubsystemKind.GENERATED,
            envelope_mm=[PITCH_DIAMETER_SUN_MM + 4, PITCH_DIAMETER_SUN_MM + 4, GEAR_THICKNESS_MM],
            mass_budget_kg=0.05,
            material="steel",
            interfaces=["ifc_sun_bore"],
            worker_count=1,
            assembly_constraints={"datum": "central_axis"},
        ),
    ]
    interfaces = [
        Interface(
            id="ifc_sun_bore",
            name="sun_input_shaft",
            subsystem_a="sun_gear",
            port_a="bore",
            subsystem_b="input_shaft",
            port_b="spline",
            geometry={"diameter_mm": SHAFT_BORE_MM},
            frame_a=CoordinateFrame(origin_mm=[0, 0, GEAR_THICKNESS_MM / 2]),
            frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
            mating=MatingSemantic(type="cylindrical_fit"),
            validation=ValidationMethod(check_points=[
                ValidationCheckPoint(feature="bore_dia",
                                     expected_mm=SHAFT_BORE_MM, tolerance_mm=0.2),
            ]),
        ),
    ]

    # Three planet gears as separate subsystems (each gets its own
    # worker dispatch under the outer-loop machinery).
    for i, _ in enumerate(_planet_positions(), start=1):
        subsystems.append(Subsystem(
            id=f"s_planet_{i}",
            name=f"planet_{i}",
            kind=SubsystemKind.GENERATED,
            envelope_mm=[PITCH_DIAMETER_PLANET_MM + 4, PITCH_DIAMETER_PLANET_MM + 4,
                         GEAR_THICKNESS_MM],
            mass_budget_kg=0.04,
            material="steel",
            interfaces=[f"ifc_planet_{i}_pin"],
            worker_count=1,
            assembly_constraints={"datum": "central_axis"},
        ))
        interfaces.append(Interface(
            id=f"ifc_planet_{i}_pin",
            name=f"planet_{i}_pin_bore",
            subsystem_a=f"planet_{i}",
            port_a="bore",
            subsystem_b="planet_carrier",
            port_b=f"pin_{i}",
            geometry={"diameter_mm": PIN_BORE_MM},
            frame_a=CoordinateFrame(origin_mm=[0, 0, GEAR_THICKNESS_MM / 2]),
            frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
            mating=MatingSemantic(type="cylindrical_fit"),
            validation=ValidationMethod(check_points=[
                ValidationCheckPoint(feature="bore_dia",
                                     expected_mm=PIN_BORE_MM, tolerance_mm=0.2),
            ]),
        ))

    subsystems.append(Subsystem(
        id="s_carrier",
        name="planet_carrier",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[CARRIER_OUTER_DIA_MM, CARRIER_OUTER_DIA_MM,
                     CARRIER_THICKNESS_MM + PIN_HEIGHT_MM],
        mass_budget_kg=0.06,
        material="aluminum",
        interfaces=["ifc_carrier"],
        worker_count=1,
        assembly_constraints={"datum": "central_axis"},
    ))
    interfaces.append(Interface(
        id="ifc_carrier",
        name="carrier_pins_to_planets",
        subsystem_a="planet_carrier",
        port_a="pins",
        subsystem_b="planets",
        port_b="bores",
        geometry={"diameter_mm": SHAFT_BORE_MM},
        frame_a=CoordinateFrame(origin_mm=[0, 0, CARRIER_THICKNESS_MM / 2]),
        frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
        mating=MatingSemantic(type="cylindrical_fit"),
        validation=ValidationMethod(check_points=[
            ValidationCheckPoint(feature="bore_dia",
                                 expected_mm=SHAFT_BORE_MM, tolerance_mm=0.2),
            ValidationCheckPoint(feature="pin_circle_dia",
                                 expected_mm=2 * ORBIT_RADIUS_MM, tolerance_mm=0.5),
        ]),
    ))

    subsystems.append(Subsystem(
        id="s_ring",
        name="ring_gear",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[PITCH_DIAMETER_RING_MM + 6, PITCH_DIAMETER_RING_MM + 6,
                     RING_THICKNESS_MM],
        mass_budget_kg=0.20,
        material="steel",
        interfaces=["ifc_ring"],
        worker_count=1,
        assembly_constraints={"datum": "central_axis"},
    ))
    # Internal-gear geometry note: no physical cylinder sits at the
    # pitch diameter (it's a virtual rolling circle). What's measurable
    # is the root circle, sitting OUTWARD of the pitch in the internal-
    # tooth case: ring_root_dia = pitch_dia + 2 * dedendum_coeff * module
    # = ring_teeth * module + 2 * (1 + clearance_coeff) * module
    # = 48 + 2 * 1.25 = 50.5 for clearance_coeff=0.25.
    ring_root_dia_mm = PITCH_DIAMETER_RING_MM + 2 * 1.25 * MODULE_MM
    interfaces.append(Interface(
        id="ifc_ring",
        name="ring_root_circle",
        subsystem_a="ring_gear",
        port_a="root_cylinder",
        subsystem_b="planets",
        port_b="mesh",
        geometry={"diameter_mm": ring_root_dia_mm},
        frame_a=CoordinateFrame(origin_mm=[0, 0, RING_THICKNESS_MM / 2]),
        frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
        mating=MatingSemantic(type="gear_mesh"),
        validation=ValidationMethod(check_points=[
            ValidationCheckPoint(feature="bore_dia",
                                 expected_mm=ring_root_dia_mm,
                                 tolerance_mm=0.5),
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


def _dispatch_workers(run, output_root: Path) -> dict[str, Path]:
    """Build all 5+ subsystems, return {subsystem_name: step_path}.

    Sequential for now (FreeCAD socket is single-doc-at-a-time inside
    a session); each builder produces an isolated STEP that the
    orchestrator re-imports independently in the validation step.
    """
    from orchestrator.runner import build_worker_prompts
    from orchestrator.worker_builds import (
        planet_carrier as carrier_mod,
    )
    from orchestrator.worker_builds import (
        ring_gear as ring_mod,
    )
    from orchestrator.worker_builds import (
        sun_gear as sun_mod,
    )

    prompts = {p["subsystem"]: Path(p["output_dir"]) for p in build_worker_prompts(run)}
    results: dict[str, Path] = {}

    # 1. Sun gear
    print("  building sun_gear...")
    results["sun_gear"] = sun_mod.build_sun_gear(
        sub_spec={
            "name": "sun_gear",
            "module": MODULE_MM,
            "teeth": SUN_TEETH,
            "thickness_mm": GEAR_THICKNESS_MM,
            "bore_diameter_mm": SHAFT_BORE_MM,
        },
        output_dir=prompts["sun_gear"],
    )

    # 2. Planet gears (3) — same builder, different teeth count + bore
    for i, (px, py) in enumerate(_planet_positions(), start=1):
        name = f"planet_{i}"
        print(f"  building {name} (orbit position {px:+.2f}, {py:+.2f})...")
        results[name] = sun_mod.build_sun_gear(
            sub_spec={
                "name": name,
                "module": MODULE_MM,
                "teeth": PLANET_TEETH,
                "thickness_mm": GEAR_THICKNESS_MM,
                "bore_diameter_mm": PIN_BORE_MM,
            },
            output_dir=prompts[name],
        )

    # 3. Planet carrier — central bore + 3 pin bosses on PCD
    print("  building planet_carrier...")
    results["planet_carrier"] = carrier_mod.build_planet_carrier(
        sub_spec={
            "name": "planet_carrier",
            "outer_diameter_mm": CARRIER_OUTER_DIA_MM,
            "thickness_mm": CARRIER_THICKNESS_MM,
            "bore_diameter_mm": SHAFT_BORE_MM,
            "pin_count": NUM_PLANETS,
            "pin_circle_diameter_mm": 2 * ORBIT_RADIUS_MM,
            "pin_diameter_mm": PIN_BORE_MM,
            "pin_height_mm": PIN_HEIGHT_MM,
        },
        output_dir=prompts["planet_carrier"],
    )

    # 4. Ring gear — internal teeth via polar pattern of one slot
    print("  building ring_gear...")
    results["ring_gear"] = ring_mod.build_ring_gear(
        sub_spec={
            "name": "ring_gear",
            "module": MODULE_MM,
            "sun_teeth": SUN_TEETH,
            "planet_teeth": PLANET_TEETH,
            "num_planets": NUM_PLANETS,
            "thickness_mm": RING_THICKNESS_MM,
        },
        output_dir=prompts["ring_gear"],
    )
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--out", type=Path, default=Path("/tmp/planetary_gearbox_run"),
                    help="Directory to write the run output into")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not args.quiet:
        logging.basicConfig(level=logging.WARNING,
                            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"Planetary gearbox 5:1 — module={MODULE_MM}, sun={SUN_TEETH}, "
          f"planet={PLANET_TEETH}, ring={RING_TEETH}, planets={NUM_PLANETS}")
    print(f"Run output: {out}")

    # Reachability probe — fail early with a clear message if the addon's down.
    from orchestrator.worker_builds import common
    if not common.freecad_ready():
        print(f"ERROR: FreeCAD addon not reachable at "
              f"{common.fc_host()}:{common.fc_port()}. "
              "Start FreeCAD with the addon installed (scripts/install_freecad_addon.sh).",
              file=sys.stderr)
        return 1
    if not common.freecad_ready_with_import_step():
        print("ERROR: FreeCAD addon is running but cad_import_step is not "
              "registered. Reload the addon (commit 36bd03e or later required).",
              file=sys.stderr)
        return 1

    print("\n[1/4] G0 → G3 gate walk")
    run = _make_spec(out / "run")
    _walk_to_building(run)

    print(f"\n[2/4] Dispatch {len(run.spec.subsystems)} worker builds")
    t0 = time.monotonic()
    step_paths = _dispatch_workers(run, out)
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
        details = f" (source={r.measurement_source})"
        print(f"    {status} {r.subsystem_name}{details}")
        for dc in r.dimension_checks:
            mark = "✓" if dc.passed else "✗"
            print(f"        {mark} {dc.feature}: measured={dc.measured_mm}, "
                  f"expected={dc.expected_mm}±{dc.tolerance_mm}")
        if r.failure_codes:
            print(f"        failure_codes: {[fc.value for fc in r.failure_codes]}")

    all_pass = all(r.overall_pass for r in reports)
    print(f"\nResult: {'ALL PARTS PASS' if all_pass else 'AT LEAST ONE PART FAILED'}")
    print(f"Speed ratio (closed-form, ring fixed): {SPEED_RATIO:.2f}:1")

    summary = out / "orchestrator_report.txt"
    summary.write_text(
        f"Planetary Gearbox 5:1 — orchestrator run summary\n"
        f"================================================\n\n"
        f"module={MODULE_MM}, sun_teeth={SUN_TEETH}, planet_teeth={PLANET_TEETH}, "
        f"ring_teeth={RING_TEETH}, num_planets={NUM_PLANETS}\n"
        f"target speed ratio = {SPEED_RATIO:.2f}:1\n\n"
        f"Parts built and validated:\n"
        + "\n".join(f"  {'✓' if r.overall_pass else '✗'} {r.subsystem_name} "
                   f"(source={r.measurement_source})" for r in reports)
        + f"\n\nAll parts passing: {all_pass}\n"
    )
    print(f"\nReport written to: {summary}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
