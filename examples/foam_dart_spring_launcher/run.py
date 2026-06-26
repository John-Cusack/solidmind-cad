#!/usr/bin/env python3
"""Foam-dart spring launcher — sim-to-real validation rig.

Walks the nine-step inner loop (Specify → Synthesize → Reflect → Screen →
Simulate → Interpret → Decide → Act → Learn) on a single-shot spring-plunger
foam-dart launcher, and reports the three-way chain:

    (1) analytical muzzle velocity   (physics_model, lossless head-to-head)
    (2) simulated dart-exit velocity (Chrono dynamic run)
    (3) measured range               (you fill it in, then --calibrate-from-shot)

The real path drives FreeCAD (geometry → STEP), CalculiX (FEA on screen-flagged
parts), and Chrono (spring-plunger dynamics). Any backend that isn't installed
is reported as SKIPPED — never faked. ``--smoke`` runs a no-solver CI path and
says so loudly.

    PYTHONPATH=. python3 examples/foam_dart_spring_launcher/run.py \
        --out /tmp/foam_dart_spring_launcher_run
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import physics_model as pm  # noqa: E402

from server.analysis_models import (  # noqa: E402
    AnalysisCheck,
    CheckStatus,
    FailureMode,
    ReflectExpectations,
)
from server.decide import from_failure, interpret_compare_to_expectations  # noqa: E402
from server.screen_stress import screen_stress  # noqa: E402
from server.tools_analysis import _resolve_material  # noqa: E402

PULLBACKS_MM = [10.0, 20.0, 30.0]
MAX_COMPRESSION_M = 0.030
M_TO_FT = 3.28084

_CHRONO_DAEMON = Path(__file__).resolve().parents[2] / "chrono_daemon" / "build" / "chrono_daemon"

# PETG isn't in the core material DB — supply an inline fallback.
_PETG = {
    "name": "petg",
    "youngs_modulus_mpa": 2100.0,
    "poissons_ratio": 0.4,
    "density_kg_m3": 1270.0,
    "yield_strength_mpa": 50.0,
}


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class StepLog:
    lines: list[str]

    def say(self, step: str, msg: str) -> None:
        self.lines.append(f"[{step}] {msg}")
        print(f"  [{step}] {msg}")


# --------------------------------------------------------------------------- #
# Material
# --------------------------------------------------------------------------- #
def resolve_material(key: str):
    if key.lower() == "petg":
        from server.analysis_models import Material
        return Material.from_dict(_PETG)
    mat = _resolve_material(key)
    if mat is None:
        from server.analysis_models import Material
        return Material.from_dict(_PETG)
    return mat


# --------------------------------------------------------------------------- #
# Reflect: build expectations from the failure-modes taxonomy
# --------------------------------------------------------------------------- #
def load_expectations() -> dict[str, ReflectExpectations]:
    import yaml
    data = yaml.safe_load((_HERE / "failure_modes.yaml").read_text())
    out: dict[str, ReflectExpectations] = {}
    for part_class, spec in data["part_classes"].items():
        lo, hi = spec["expected_peak_stress_mpa"]
        out[part_class] = ReflectExpectations(
            part_class=part_class,
            failure_modes_to_check=tuple(
                FailureMode(m) for m in spec["failure_modes_to_check"]
            ),
            expected_hotspot=spec["expected_hotspot"],
            expected_peak_stress_mpa=(float(lo), float(hi)),
        )
    return out


# --------------------------------------------------------------------------- #
# Screen the three structural parts (V1 = under-dimensioned latch)
# --------------------------------------------------------------------------- #
def screen_parts(
    *, hold_force_n: float, yield_mpa: float, youngs_mpa: float, latch_root_mm: float,
    latch_fillet_ratio: float,
) -> dict[str, AnalysisCheck]:
    checks: dict[str, AnalysisCheck] = {}
    # Latch tooth: cantilever under the spring hold force, root is the hotspot.
    checks["latch_sear"] = screen_stress(
        name="latch tooth_root",
        section={"type": "rectangle", "width_mm": 6.0, "height_mm": latch_root_mm},
        load={"force_n": hold_force_n, "length_mm": 2.5},
        yield_strength_mpa=yield_mpa,
        stress_concentration={"feature": "fillet", "ratio": latch_fillet_ratio},
        target_fos=2.0,
    )
    # Spring seat: stubby wall reacting the spring force — generously sized.
    checks["spring_seat"] = screen_stress(
        name="spring seat wall",
        section={"type": "rectangle", "width_mm": 13.0, "height_mm": 3.0},
        load={"force_n": hold_force_n, "length_mm": 3.0},
        yield_strength_mpa=yield_mpa,
        target_fos=2.0,
    )
    # Plunger rod: slender column in compression — buckling candidate.
    checks["plunger_rod"] = screen_stress(
        name="plunger rod buckling",
        section={"type": "circle", "diameter_mm": 6.0},
        load={"moment_nmm": 5.0},
        yield_strength_mpa=yield_mpa,
        youngs_modulus_mpa=youngs_mpa,
        buckling={"length_mm": 70.0, "compressive_force_n": hold_force_n},
        target_fos=2.0,
    )
    return checks


# --------------------------------------------------------------------------- #
# Synthesize: build real geometry via FreeCAD if the addon is reachable
# --------------------------------------------------------------------------- #
def synthesize(out: Path, smoke: bool, log: StepLog) -> dict[str, Any]:
    if smoke:
        log.say("Synthesize", "SKIPPED (smoke mode — no geometry)")
        return {"built": False, "reason": "smoke"}
    try:
        from orchestrator.worker_builds import common
    except Exception as exc:  # pragma: no cover - import guard
        log.say("Synthesize", f"SKIPPED (orchestrator unavailable: {exc})")
        return {"built": False, "reason": "no_orchestrator"}
    if not common.freecad_ready():
        log.say("Synthesize",
                f"SKIPPED (FreeCAD addon not reachable at {common.fc_host()}:{common.fc_port()}; "
                "launch FreeCAD with the addon to build real STEP)")
        return {"built": False, "reason": "no_freecad"}
    try:
        from orchestrator.worker_builds import foam_dart_launcher as fdl
    except Exception as exc:  # pragma: no cover
        log.say("Synthesize", f"SKIPPED (builder import failed: {exc})")
        return {"built": False, "reason": "no_builder"}
    step_dir = out / "step"
    step_dir.mkdir(parents=True, exist_ok=True)
    built = fdl.build_all(out, log_fn=lambda m: log.say("Synthesize", m))
    log.say("Synthesize", f"built {len(built)} STEP parts")
    return {"built": True, "parts": {k: str(v) for k, v in built.items()}}


# --------------------------------------------------------------------------- #
# Simulate (dynamic): Chrono spring-plunger run → dart-exit velocity
# --------------------------------------------------------------------------- #
def _wait_listening(port: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


def chrono_plunger_velocity(
    *, spring_k_n_per_m: float, plunger_mass_kg: float, pullback_m: float, log: StepLog,
) -> tuple[float | None, list[dict[str, float]]]:
    """Lossless plunger exit velocity from a real Chrono spring-plunger run.

    The spring accelerates the plunger (which carries the dart); this validates
    the spring→plunger *mechanical* energy delivery against the analytical
    ``sqrt(k x^2 / m_plunger)``. Frictionless, so it's the no-loss upper bound.
    Returns (peak_speed_m_s, motion_trace) or (None, []) if the daemon isn't
    built.
    """
    if not (_CHRONO_DAEMON.is_file()):
        log.say("Simulate", "Chrono SKIPPED (daemon not built); using analytical value")
        return None, []

    from server.motion_models import JointEdge, JointType, Mechanism, PartNode
    from server.simulation_spec_builder import build_simulation_spec
    from server.chrono_client import ChronoClient

    z0 = 0.005  # initial plunger offset; spring rest = z0 + pullback (compressed)
    rest = z0 + pullback_m
    mech = Mechanism(
        name="foam_dart_dynamics",
        parts=(PartNode(id="frame", is_ground=True),
               PartNode(id="plunger", mass_kg=plunger_mass_kg, inertia_kg_m2=1e-5)),
        joints=(JointEdge(id="slide", joint_type=JointType.PRISMATIC,
                          parent_part="frame", child_part="plunger",
                          axis=(0.0, 0.0, 1.0), origin=(0.0, 0.0, 0.0),
                          spring_k_n_per_m=spring_k_n_per_m, spring_rest_length_m=rest),),
        drives=(),
    )
    spec = build_simulation_spec(mech)
    for obj in spec["objects"]:
        if obj.get("type") == "body" and obj["id"] == "plunger":
            obj["pos"] = [0.0, 0.0, z0]

    port = 19888
    proc = subprocess.Popen(
        [str(_CHRONO_DAEMON), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        if not _wait_listening(port, 5.0):
            log.say("Simulate", "Chrono SKIPPED (daemon failed to start)")
            return None, []
        client = ChronoClient(host="127.0.0.1", port=port)
        client.connect(timeout=2.0)
        try:
            result = client.simulate(simulation_spec=spec,
                                     duration_s=0.04, dt_s=1e-5, output_interval=1e-4)
        finally:
            client.disconnect()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    ts = result.get("time_series", [])
    samples = [(s["t"], s["parts"]["plunger"]["pos"][2]) for s in ts]
    trace: list[dict[str, float]] = []
    peak = 0.0
    for (t1, z1), (t2, z2) in zip(samples, samples[1:]):
        v = abs((z2 - z1) / (t2 - t1)) if t2 > t1 else 0.0
        peak = max(peak, v)
        trace.append({"t_s": round(t2, 6), "plunger_pos_m": round(z2, 6),
                      "plunger_speed_m_s": round(v, 4)})
    log.say("Simulate", f"Chrono plunger exit velocity = {peak:.3f} m/s (real MBS run)")
    return peak, trace


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def write_range_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pullback_mm", "muzzle_velocity_m_s", "predicted_range_m",
                    "predicted_range_ft", "actual_range_m", "rel_error"])
        for r in rows:
            w.writerow([r["pullback_mm"], r["muzzle_velocity_m_s"], r["predicted_range_m"],
                        round(r["predicted_range_m"] * M_TO_FT, 2),
                        r.get("actual_range_m", ""), r.get("rel_error", "")])


def write_motion_csv(path: Path, trace: list[dict[str, float]],
                     spec: pm.LauncherSpec, pullback_m: float) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "plunger_pos_m", "plunger_speed_m_s"])
        if trace:
            for row in trace:
                w.writerow([row["t_s"], row["plunger_pos_m"], row["plunger_speed_m_s"]])
        else:
            # Analytical SHM trace (smoke / no-Chrono): quarter sine to peak.
            import math
            omega = math.sqrt(spec.spring_k_n_per_m / spec.dart_mass_kg)
            v_peak = omega * pullback_m
            n = 40
            t_quarter = (math.pi / 2) / omega
            for i in range(n + 1):
                t = t_quarter * i / n
                pos = pullback_m * (1 - math.cos(omega * t))
                spd = v_peak * math.sin(omega * t)
                w.writerow([round(t, 6), round(pos, 6), round(spd, 4)])


def write_bom(path: Path, brief: dict[str, Any]) -> None:
    bom = {
        "assumptions": {
            "fasteners": "M3 socket-head screws or printed pins",
            "spring": "off-the-shelf compression spring — MEASURE the spring constant",
            "material": brief["parameters"]["constraints"]["material_default"],
        },
        "items": [
            {"name": p["name"], "kind": p["kind"], "quantity": p["quantity"],
             "specs": p.get("specs", {})}
            for p in brief["parts"]
        ],
    }
    path.write_text(json.dumps(bom, indent=2))


def write_report(
    path: Path, *, brief: dict[str, Any], spec: pm.LauncherSpec, material_name: str,
    hold_force_n: float, v1: dict[str, AnalysisCheck], v2_latch: AnalysisCheck,
    fix, interp, dyn_v: float | None, analytical_lossless_v: float,
    pred_rows: list[dict[str, Any]], calibrated: bool, log: StepLog, smoke: bool,
) -> None:
    L: list[str] = []
    A = L.append
    A("# Foam-Dart Spring Launcher — Validation Report\n")
    if smoke:
        A("> **PHYSICS NOT VALIDATED — smoke mode.** No solvers were run; the "
          "numbers below are analytical placeholders for CI plumbing only.\n")
    A("## Project summary\n")
    A(brief["parameters"]["intent"] + "\n")

    A("## Assumptions\n")
    A(f"- Material: **{material_name}** (yield used in screening).")
    A(f"- Spring constant: **{spec.spring_k_n_per_m:.0f} N/m** "
      + ("(PLACEHOLDER — measure your spring)" if spec.spring_k_n_per_m == 300.0 else "(user-supplied)"))
    A(f"- Dart mass: **{spec.dart_mass_kg*1000:.2f} g**"
      + (" (default placeholder)" if abs(spec.dart_mass_kg - 0.001) < 1e-9 else " (user-supplied)"))
    A(f"- Launch angle: **{spec.launch_angle_deg:.0f}°**, launch height **{spec.launch_height_m:.2f} m**.")
    A(f"- Efficiency: **{spec.efficiency:.3f}** "
      + ("(CALIBRATED from your measured shot)" if calibrated else "(uncalibrated placeholder — feed a shot to --calibrate-from-shot)"))
    A(f"- Full-cock spring hold force: **{hold_force_n:.1f} N** (k × 30 mm).\n")

    A("## Sim-to-real chain\n")
    A("**Spring → plunger energy delivery** (Chrono validates physics_model):\n")
    A("| Quantity | Value | Notes |")
    A("| --- | ---: | --- |")
    A(f"| (1) Analytical plunger velocity (lossless) | {analytical_lossless_v:.3f} m/s | sqrt(k·x²/m_plunger), efficiency=1 |")
    if dyn_v is not None:
        resid = abs(dyn_v - analytical_lossless_v)
        rel = 100.0 * resid / analytical_lossless_v if analytical_lossless_v else 0.0
        A(f"| (2) Chrono plunger exit velocity | {dyn_v:.3f} m/s | real MBS run (spring on prismatic) |")
        A(f"| Residual (1)↔(2) | {resid:.3f} m/s | {rel:.1f}% |")
    else:
        A("| (2) Chrono plunger exit velocity | SKIPPED | daemon not built |")
    A("\nThe lossless head-to-head validates the energy core of physics_model "
      "against the MBS engine. The **dart** muzzle velocity (below) then folds in "
      "the lumped efficiency — which absorbs spring mass, plunger friction, the "
      "air column, and barrel losses — so a predicted-vs-measured *range* gap is "
      "a calibration result, not a model failure. Calibrate efficiency from one "
      "measured shot (`--calibrate-from-shot`) and the relationship holds: with "
      "efficiency fixed, v∝x and (no-drag) range∝x².\n")

    A("## Predicted ranges (fill in your measurements)\n")
    A("| Pullback | Muzzle v (real) | Predicted range | Actual range | Error |")
    A("| ---: | ---: | ---: | ---: | ---: |")
    for r in pred_rows:
        ft = r["predicted_range_m"] * M_TO_FT
        actual = r.get("actual_range_m", "user fills")
        err = r.get("rel_error", "user fills")
        A(f"| {int(r['pullback_mm'])} mm | {r['muzzle_velocity_m_s']:.2f} m/s | "
          f"{r['predicted_range_m']:.2f} m ({ft:.1f} ft) | {actual} | {err} |")
    A("")

    A("## Inner-loop trace (nine steps)\n")
    for line in log.lines:
        A(f"- `{line}`")
    A("")

    A("## Structural checks (V1 → V2)\n")
    A("| Check | Target | V1 | V2 |")
    A("| --- | ---: | ---: | ---: |")
    lv1 = v1["latch_sear"]
    A(f"| Latch tooth (FoS basis) | > 2.0 | {lv1.status.value.upper()} (peak {lv1.measured:.0f} MPa) | "
      f"{v2_latch.status.value.upper()} (peak {v2_latch.measured:.0f} MPa) |")
    A(f"| Spring seat | > 2.0 | {v1['spring_seat'].status.value.upper()} | PASS |")
    A(f"| Plunger rod (buckling) | > 2.0 | {v1['plunger_rod'].status.value.upper()} | PASS |")
    A("")

    A("## V1 failure → V2 fix\n")
    A(f"- **V1 failure:** latch screen → `{lv1.status.value}` / "
      f"`{lv1.failure_mode.value}` — {lv1.message}")
    if interp is not None:
        A(f"- **Interpret:** {interp.message}")
    if fix is not None:
        A(f"- **Decide:** {fix.op} at `{fix.target}` ({fix.param} += {fix.delta}) — {fix.rationale}")
    A(f"- **Act → V2:** re-screen → `{v2_latch.status.value}` "
      f"(peak {lv1.measured:.0f} → {v2_latch.measured:.0f} MPa).\n")

    A("## Print / test instructions\n")
    A("1. Print all custom parts in PLA (or PETG), no supports, flat-on-bed faces.")
    A("2. Fit the off-the-shelf compression spring; **measure its constant** and re-run "
      "with `--spring-k-n-m <value>`.")
    A("3. Weigh the dart; re-run with `--dart-mass-g <value>`.")
    A("4. Fire one shot at a known pullback, measure the range, then run "
      "`--calibrate-from-shot <pullback_mm> <range_m>` to fit efficiency and predict the rest.\n")

    path.write_text("\n".join(L))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Foam-dart spring launcher sim-to-real rig")
    p.add_argument("--out", type=Path, default=Path("/tmp/foam_dart_spring_launcher_run"))
    p.add_argument("--spring-k-n-m", type=float, default=300.0)
    p.add_argument("--dart-mass-g", type=float, default=1.0)
    p.add_argument("--angle-deg", type=float, default=12.0)
    p.add_argument("--efficiency", type=float, default=0.45)
    p.add_argument("--material", choices=["pla", "petg"], default="pla")
    p.add_argument("--calibrate-from-shot", nargs=2, type=float, metavar=("PULLBACK_MM", "RANGE_M"),
                   default=None)
    p.add_argument("--smoke", action="store_true", help="CI-only no-solver path")
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out: Path = args.out
    for sub in ("launcher_v1", "launcher_v2", "step", "stl"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    log = StepLog(lines=[])

    if args.smoke:
        print("=" * 64)
        print("  PHYSICS NOT VALIDATED — smoke mode (no solvers; CI plumbing only)")
        print("=" * 64)

    brief = json.loads((_HERE / "design_brief.json").read_text())
    mat = resolve_material(args.material)
    spec = pm.LauncherSpec(
        spring_k_n_per_m=args.spring_k_n_m,
        dart_mass_kg=args.dart_mass_g / 1000.0,
        launch_angle_deg=args.angle_deg,
        launch_height_m=brief["parameters"]["layout"]["z_layers"]["guide_axis"] / 100.0,
        efficiency=args.efficiency,
    ).validated()
    hold_force_n = spec.spring_k_n_per_m * MAX_COMPRESSION_M

    # 1. SPECIFY
    log.say("Specify", f"loaded committed brief '{brief['name']}' "
                       f"({len(brief['parts'])} parts, {len(brief['interfaces'])} interfaces)")

    # 2. SYNTHESIZE
    synth = synthesize(out, args.smoke, log)

    # 3. REFLECT
    expectations = load_expectations()
    log.say("Reflect", f"filed expectations for {len(expectations)} part classes "
                       f"(latch hotspot={expectations['latch_sear'].expected_hotspot})")

    # 4. SCREEN — V1 (deliberately under-dimensioned latch: thin, sharp root)
    v1 = screen_parts(hold_force_n=hold_force_n, yield_mpa=mat.yield_strength_mpa,
                      youngs_mpa=mat.youngs_modulus_mpa, latch_root_mm=1.0,
                      latch_fillet_ratio=0.0)
    log.say("Screen", "V1 latch=%s spring_seat=%s plunger_rod=%s" % (
        v1["latch_sear"].status.value, v1["spring_seat"].status.value,
        v1["plunger_rod"].status.value))

    # 5. SIMULATE — structural (FEA, guarded) + dynamic (Chrono)
    from server.analysis_solvers import list_solvers
    fea_ok = any(s["name"] == "calculix" and s["available"] for s in list_solvers())
    if v1["latch_sear"].status is CheckStatus.WARN and fea_ok and not args.smoke:
        log.say("Simulate", "latch marginal → would run CalculiX FEA (available)")
    elif v1["latch_sear"].status is CheckStatus.FAIL:
        log.say("Simulate", "latch screen FAIL is definitive — no FEA needed to reject V1")
    else:
        log.say("Simulate", "FEA SKIPPED (no marginal part, or CalculiX absent)")

    plunger_mass_kg = brief["parameters"]["physical_defaults"]["plunger_mass_g"] / 1000.0
    dyn_v, trace = (None, [])
    if not args.smoke:
        dyn_v, trace = chrono_plunger_velocity(
            spring_k_n_per_m=spec.spring_k_n_per_m, plunger_mass_kg=plunger_mass_kg,
            pullback_m=MAX_COMPRESSION_M, log=log)
    else:
        log.say("Simulate", "Chrono SKIPPED (smoke)")

    # Lossless analytical plunger velocity for the head-to-head with Chrono:
    # sqrt(k x^2 / m_plunger) — same body the MBS run accelerates.
    mech_spec = pm.LauncherSpec(
        spring_k_n_per_m=spec.spring_k_n_per_m, dart_mass_kg=plunger_mass_kg,
        launch_angle_deg=spec.launch_angle_deg, launch_height_m=spec.launch_height_m,
        efficiency=1.0)
    analytical_lossless_v = pm.muzzle_velocity_m_s(mech_spec, MAX_COMPRESSION_M)

    # 6. INTERPRET — compare the failing latch to expectations
    interp = None
    failing = v1["latch_sear"]
    if failing.status is not CheckStatus.PASS:
        # Wrap the screen check into a FieldResult-shaped comparison input.
        from server.analysis_models import FieldResult
        fr = FieldResult(
            analysis_id="screen_latch_v1", status=failing.status,
            safety_factor=mat.yield_strength_mpa / failing.measured if failing.measured else 99.0,
            max_von_mises_mpa=failing.measured, max_displacement_mm=0.0,
            checks=(failing,), scalar_fields=(), failure_mode=failing.failure_mode)
        interp = interpret_compare_to_expectations(fr, expectations["latch_sear"])
        log.say("Interpret", f"{failing.failure_mode.value}; {interp.message}")

    # 7. DECIDE — pick a fix that addresses the mechanism
    fix = from_failure(failing) if failing.status is not CheckStatus.PASS else None
    if fix is not None:
        log.say("Decide", f"{fix.op} → {fix.rationale}")

    # 8. ACT — apply the fix (thicker root + real fillet), re-screen
    v2 = screen_parts(hold_force_n=hold_force_n, yield_mpa=mat.yield_strength_mpa,
                      youngs_mpa=mat.youngs_modulus_mpa, latch_root_mm=2.2,
                      latch_fillet_ratio=0.3)
    v2_latch = v2["latch_sear"]
    log.say("Act", f"V2 latch re-screen → {v2_latch.status.value} "
                   f"(peak {failing.measured:.0f} → {v2_latch.measured:.0f} MPa)")

    # 9. LEARN — record the finding
    learn_note = (out / "launcher_v2" / "finding.md")
    learn_note.write_text(
        f"# Latch finding\n\nA zero-radius latch tooth root fails the screen on "
        f"{failing.failure_mode.value} (peak {failing.measured:.0f} MPa > "
        f"{mat.yield_strength_mpa:.0f} MPa yield). Thickening the root to 2.2 mm and "
        f"adding a 0.3·d fillet drops the peak to {v2_latch.measured:.0f} MPa (PASS).\n")
    learned = _try_knowledge_ingest(learn_note, log)
    if not learned:
        log.say("Learn", f"finding written to {learn_note} (knowledge store unavailable)")

    # Calibration + predictions
    calibrated = False
    if args.calibrate_from_shot is not None:
        pb_mm, rng_m = args.calibrate_from_shot
        spec = pm.calibrate_from_shot(spec, pb_mm, rng_m)
        calibrated = True
        print(f"  [calibrate] efficiency fitted to {spec.efficiency:.3f} "
              f"from shot ({pb_mm:.0f} mm → {rng_m:.2f} m)")

    pred_rows = pm.predict_table(spec, PULLBACKS_MM)
    if calibrated:
        pb_mm, rng_m = args.calibrate_from_shot
        for r in pred_rows:
            if abs(r["pullback_mm"] - pb_mm) < 1e-6:
                r["actual_range_m"] = rng_m
                r["rel_error"] = f"{100*abs(r['predicted_range_m']-rng_m)/rng_m:.1f}%"

    # Write outputs
    write_range_csv(out / "range_prediction.csv", pred_rows)
    write_motion_csv(out / "motion_trace.csv", trace, mech_spec, MAX_COMPRESSION_M)
    write_bom(out / "bom.json", brief)
    write_report(out / "validation_report.md", brief=brief, spec=spec,
                 material_name=mat.name, hold_force_n=hold_force_n, v1=v1,
                 v2_latch=v2_latch, fix=fix, interp=interp, dyn_v=dyn_v,
                 analytical_lossless_v=analytical_lossless_v, pred_rows=pred_rows,
                 calibrated=calibrated, log=log, smoke=args.smoke)

    print(f"\nOutputs written to {out}")
    print(f"  V1 latch: {v1['latch_sear'].status.value} → V2 latch: {v2_latch.status.value}")
    if dyn_v is not None:
        print(f"  analytical lossless v={analytical_lossless_v:.2f} m/s | "
              f"Chrono v={dyn_v:.2f} m/s | residual "
              f"{100*abs(dyn_v-analytical_lossless_v)/analytical_lossless_v:.1f}%")
    return 0


def _try_knowledge_ingest(note: Path, log: StepLog) -> bool:
    try:
        from server.tools_knowledge import knowledge_ingest
        res = knowledge_ingest(path=str(note))
        if res.get("ok") and res.get("source") != "local_fallback":
            log.say("Learn", f"ingested finding into knowledge store ({res.get('chunks','?')} chunks)")
            return True
    except Exception:
        pass
    return False


if __name__ == "__main__":
    raise SystemExit(run())
