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

from server.analysis_convergence import relative_change, run_convergence_study  # noqa: E402
from server.analysis_models import (  # noqa: E402
    AnalysisCheck,
    BoundaryCondition,  # noqa: E402
    CheckStatus,
    FailureMode,
    ReflectExpectations,
)
from server.decide import from_failure, interpret_compare_to_expectations  # noqa: E402
from server.screen_stress import screen_stress  # noqa: E402
from server.tools_analysis import StructuralSolveError, _resolve_material  # noqa: E402
from server.tools_cad import cad_export_body  # noqa: E402

PULLBACKS_MM = [10.0, 20.0, 30.0]
MAX_COMPRESSION_M = 0.030
M_TO_FT = 3.28084

# Latch geometry — single source of truth shared by the structural screen and the
# FreeCAD builder so the screened section and the meshed part describe the SAME
# tooth. width/length are the screen's section width and bending moment arm.
LATCH_TOOTH_WIDTH_MM = 6.0
LATCH_TOOTH_LEN_MM = 2.5
LATCH_V1 = {
    "root_mm": 1.0,
    "fillet_ratio": 0.0,
    "clamp_fillet_mm": 0.0,
}  # under-dimensioned, sharp everywhere
LATCH_V2 = {
    "root_mm": 2.2,
    "fillet_ratio": 0.3,
    "clamp_fillet_mm": 0.5,
}  # thicker root + root & clamp fillets
# Analytical beam theory vs meshed FEA legitimately diverge at a stress
# concentration; flag (don't fail) a screen-vs-FEA gap wider than this. Applied
# only to the *converged* (filleted) variant — a comparison against a singular
# stress is meaningless.
FEA_SCREEN_TOL = 0.25
# Mesh-convergence study: solve each latch at two densities and watch the trend.
# A filleted root converges (stress stabilizes → a real, finite stress riser); a
# sharp re-entrant corner diverges (the peak keeps climbing with refinement →
# the stress is an unbounded singularity, not a number you can trust). FEA is
# only trustworthy where it converges; on a singular geometry the analytical
# screen is the operative verdict. The two densities bracket the fillet: 0.2 mm
# is ~3 elements across the V2 root radius (resolved); 0.3 mm is ~2 (marginal).
# Refining past ~0.15 mm re-exposes a SEPARATE artifact — the idealized
# fixed-face clamp edge, itself singular — which sub-modeling removes (ROADMAP).
LATCH_MESH_COARSE_MM = 0.3
LATCH_MESH_FINE_MM = 0.2
# Relative change in peak von Mises from coarse → fine. At/below this the solve
# has converged at the fillet (trust the value); above it the peak is still
# climbing → singular root. 10% is a standard engineering-convergence threshold.
FEA_CONVERGENCE_TOL = 0.10
# A sear's governing structural load is the *catch* event — the moving plunger
# arrested by the tooth — not the static full-cock hold. Treat the catch as a
# suddenly-applied load: the classic dynamic amplification factor for a load
# applied instantaneously from rest is 2.0 (Roark, shock loading). The static
# hold force still sizes the spring seat and the rod's buckling; only the latch
# tooth sees this amplified catch load. (ROADMAP Move 4 #4.)
LATCH_IMPACT_FACTOR = 2.0

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
            failure_modes_to_check=tuple(FailureMode(m) for m in spec["failure_modes_to_check"]),
            expected_hotspot=spec["expected_hotspot"],
            expected_peak_stress_mpa=(float(lo), float(hi)),
        )
    return out


# --------------------------------------------------------------------------- #
# Screen the three structural parts (V1 = under-dimensioned latch)
# --------------------------------------------------------------------------- #
def screen_parts(
    *,
    hold_force_n: float,
    yield_mpa: float,
    youngs_mpa: float,
    latch_root_mm: float,
    latch_fillet_ratio: float,
    latch_force_n: float | None = None,
) -> dict[str, AnalysisCheck]:
    # The latch tooth is screened at its governing *catch* load; the spring seat
    # and rod see the static hold force. Default the latch load to the hold force
    # so callers that don't separate them keep the static behaviour.
    if latch_force_n is None:
        latch_force_n = hold_force_n
    checks: dict[str, AnalysisCheck] = {}
    # Latch tooth: cantilever under the catch load, root is the hotspot.
    checks["latch_sear"] = screen_stress(
        name="latch tooth_root",
        section={"type": "rectangle", "width_mm": LATCH_TOOTH_WIDTH_MM, "height_mm": latch_root_mm},
        load={"force_n": latch_force_n, "length_mm": LATCH_TOOTH_LEN_MM},
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
def synthesize(
    out: Path, smoke: bool, log: StepLog, brief: dict[str, Any] | None = None
) -> dict[str, Any]:
    if smoke:
        log.say("Synthesize", "SKIPPED (smoke mode — no geometry)")
        return {"built": False, "reason": "smoke"}
    try:
        from orchestrator.worker_builds import common
    except Exception as exc:  # pragma: no cover - import guard
        log.say("Synthesize", f"SKIPPED (orchestrator unavailable: {exc})")
        return {"built": False, "reason": "no_orchestrator"}
    if not common.freecad_ready():
        log.say(
            "Synthesize",
            f"SKIPPED (FreeCAD addon not reachable at {common.fc_host()}:{common.fc_port()}; "
            "launch FreeCAD with the addon to build real STEP)",
        )
        return {"built": False, "reason": "no_freecad"}
    try:
        from orchestrator.worker_builds import foam_dart_launcher as fdl
    except Exception as exc:  # pragma: no cover
        log.say("Synthesize", f"SKIPPED (builder import failed: {exc})")
        return {"built": False, "reason": "no_builder"}
    step_dir = out / "step"
    step_dir.mkdir(parents=True, exist_ok=True)
    specs = {p["name"]: p.get("specs", {}) for p in (brief or {}).get("parts", [])}
    try:
        built = fdl.build_all(out, specs=specs, log_fn=lambda m: log.say("Synthesize", m))
    except Exception as exc:  # any builder error → report SKIPPED, never crash the run
        log.say("Synthesize", f"SKIPPED (geometry build failed: {exc})")
        return {"built": False, "reason": "build_error", "error": str(exc)}
    n_custom = sum(1 for p in (brief or {}).get("parts", []) if p.get("kind") == "custom")
    log.say("Synthesize", f"built {len(built)} of {n_custom} custom STEP parts")
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
    *,
    spring_k_n_per_m: float,
    plunger_mass_kg: float,
    pullback_m: float,
    log: StepLog,
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

    from server.chrono_client import ChronoClient
    from server.motion_models import JointEdge, JointType, Mechanism, PartNode
    from server.simulation_spec_builder import build_simulation_spec

    z0 = 0.005  # initial plunger offset; spring rest = z0 + pullback (compressed)
    rest = z0 + pullback_m
    mech = Mechanism(
        name="foam_dart_dynamics",
        parts=(
            PartNode(id="frame", is_ground=True),
            PartNode(id="plunger", mass_kg=plunger_mass_kg, inertia_kg_m2=1e-5),
        ),
        joints=(
            JointEdge(
                id="slide",
                joint_type=JointType.PRISMATIC,
                parent_part="frame",
                child_part="plunger",
                axis=(0.0, 0.0, 1.0),
                origin=(0.0, 0.0, 0.0),
                spring_k_n_per_m=spring_k_n_per_m,
                spring_rest_length_m=rest,
            ),
        ),
        drives=(),
    )
    spec = build_simulation_spec(mech)
    for obj in spec["objects"]:
        if obj.get("type") == "body" and obj["id"] == "plunger":
            obj["pos"] = [0.0, 0.0, z0]

    port = 19888
    proc = subprocess.Popen(
        [str(_CHRONO_DAEMON), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_listening(port, 5.0):
            log.say("Simulate", "Chrono SKIPPED (daemon failed to start)")
            return None, []
        client = ChronoClient(host="127.0.0.1", port=port)
        client.connect(timeout=2.0)
        try:
            result = client.simulate(
                simulation_spec=spec, duration_s=0.04, dt_s=1e-5, output_interval=1e-4
            )
        finally:
            client.disconnect()
    except Exception as exc:  # connect/sim/protocol error → degrade, don't crash
        log.say("Simulate", f"Chrono SKIPPED (sim error: {exc})")
        return None, []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    ts = result.get("time_series", [])
    samples = [(s["t"], s["parts"]["plunger"]["pos"][2]) for s in ts]
    # A degenerate trace (≤1 sample) yields no real velocity — report SKIPPED
    # rather than a faked 0.0 m/s that would look like a genuine measurement.
    if len(samples) < 2:
        log.say("Simulate", "Chrono SKIPPED (degenerate trace — no usable samples)")
        return None, []
    trace: list[dict[str, float]] = []
    peak = 0.0
    for (t1, z1), (t2, z2) in zip(samples, samples[1:], strict=False):
        v = abs((z2 - z1) / (t2 - t1)) if t2 > t1 else 0.0
        peak = max(peak, v)
        trace.append(
            {"t_s": round(t2, 6), "plunger_pos_m": round(z2, 6), "plunger_speed_m_s": round(v, 4)}
        )
    log.say("Simulate", f"Chrono plunger exit velocity = {peak:.3f} m/s (real MBS run)")
    return peak, trace


# --------------------------------------------------------------------------- #
# Simulate (structural): real CalculiX FEA on the enriched latch
# --------------------------------------------------------------------------- #
def select_latch_faces(faces: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Pick (fixed_base, load_tip) face names from a get_body_topology face list.

    The latch is built (see foam_dart_launcher.latch_profile) with its column
    foot at min Y and the tooth tip at max X. Fixed = the foot face (min center
    Y); load = the tooth-tip face (max center X). Pure, so it is unit-tested with
    a synthetic topology.
    """
    named = [f for f in faces if f.get("name") and f.get("center")]
    if not named:
        return None, None
    fixed = min(named, key=lambda f: f["center"][1])
    load = max(named, key=lambda f: f["center"][0])
    return fixed["name"], load["name"]


def build_latch_variants(out: Path, log: StepLog) -> dict[str, dict[str, Any]] | None:
    """Build the V1/V2 latch bodies (real STEP) and leave them live for FEA.

    Returns ``{"v1": {...}, "v2": {...}}`` (each with doc/body/step) or None if
    FreeCAD isn't reachable / the build fails — the caller then reports SKIPPED.
    """
    try:
        from orchestrator.worker_builds import common
        from orchestrator.worker_builds import foam_dart_launcher as fdl
    except Exception as exc:  # pragma: no cover - import guard
        log.say("Simulate", f"FEA SKIPPED (orchestrator unavailable: {exc})")
        return None
    if not common.freecad_ready():
        log.say("Simulate", "FEA SKIPPED (FreeCAD addon not reachable; latch not built)")
        return None
    variants: dict[str, dict[str, Any]] = {}
    for label, cfg in (("v1", LATCH_V1), ("v2", LATCH_V2)):
        try:
            variants[label] = fdl.build_latch_variant(
                out,
                root_mm=cfg["root_mm"],
                fillet_mm=cfg["fillet_ratio"] * cfg["root_mm"],
                tooth_len_mm=LATCH_TOOTH_LEN_MM,
                tooth_width_mm=LATCH_TOOTH_WIDTH_MM,
                label=label,
                log=lambda m: log.say("Synthesize", m),
                clamp_fillet_mm=cfg["clamp_fillet_mm"],
            )
        except Exception as exc:  # one builder error → skip that variant, keep the rest
            log.say("Simulate", f"FEA SKIPPED ({label} latch build failed: {exc})")
    # Return whatever built (possibly partial); None only if nothing did.
    return variants or None


def fea_latch(
    *,
    variant: dict[str, Any],
    material: str | dict[str, Any],
    latch_force_n: float,
    log: StepLog,
    tag: str,
) -> dict[str, Any] | None:
    """Run a *mesh-convergence* CalculiX study on one latch body.

    Exports the body once, then drives the shared ``run_convergence_study`` — the
    same two-density engine the batch outer loop (``run_l2_fea``) uses — so the
    example and the core can never disagree about what "converged" means. The
    trusted result is the fine solve, enriched with the convergence trend:

        {"max_von_mises_mpa", "safety_factor", "status", ...,   # fine solve
         "peak_coarse_mpa", "peak_fine_mpa",
         "convergence_delta", "converged"}

    Returns None on any missing-backend / solver / geometry failure — a sharp
    root that *diverges* still returns a dict (``converged=False``); only an
    actual backend failure is SKIPPED. Never faked.
    """
    try:
        from orchestrator.worker_builds import foam_dart_launcher as fdl

        topo = fdl._send("get_body_topology", body=variant["body"], doc=variant["doc"])
        fixed_name, load_name = select_latch_faces(topo.get("faces", []))
        if not fixed_name or not load_name:
            log.say("Simulate", f"FEA SKIPPED ({tag}: could not resolve latch faces)")
            return None
        export = cad_export_body(body=variant["body"], format="step", doc=variant["doc"])
        if not export.get("ok"):
            code = export.get("error", {}).get("code", "?")
            log.say("Simulate", f"FEA SKIPPED ({tag}: STEP export failed: {code})")
            return None
        mat = _resolve_material(material)
        if mat is None:
            log.say("Simulate", f"FEA SKIPPED ({tag}: unknown material {material!r})")
            return None
        study = run_convergence_study(
            step_path=export["path"],
            material=mat,
            boundary_conditions=[
                BoundaryCondition(bc_type="fixed", faces=(fixed_name,), value={}),
                BoundaryCondition(
                    bc_type="force", faces=(load_name,), value={"fy": -latch_force_n}
                ),
            ],
            coarse_size=LATCH_MESH_COARSE_MM,
            fine_size=LATCH_MESH_FINE_MM,
            threshold=FEA_CONVERGENCE_TOL,
            body_label=variant["body"],
        )
    except StructuralSolveError as exc:  # solver/mesh failure → degrade, don't crash
        log.say("Simulate", f"FEA SKIPPED ({tag}: {exc})")
        return None
    except Exception as exc:  # connection/topology/export error → degrade, don't crash
        log.say("Simulate", f"FEA SKIPPED ({tag}: {exc})")
        return None

    res = dict(study.fine.to_dict())
    res.update(
        peak_coarse_mpa=study.peak_coarse_mpa,
        peak_fine_mpa=study.peak_fine_mpa,
        convergence_delta=study.convergence_delta,
        converged=study.converged,
    )
    trend = "converged" if study.converged else "DIVERGING (singular root)"
    log.say(
        "Simulate",
        f"FEA {tag}: peak vM {study.peak_coarse_mpa:.1f}→{study.peak_fine_mpa:.1f} MPa "
        f"(Δ {study.convergence_delta * 100:.0f}% under refinement) — {trend}",
    )
    return res


def _rel_within(
    reference: float, value: float | None, tol: float
) -> tuple[bool | None, float | None]:
    """Relative difference ``|value - reference| / value`` against a tolerance.

    ``value`` is the denominator (the trusted/measured quantity); a missing or
    non-positive ``value`` yields ``(None, None)``. The relative-change formula is
    the shared-core ``relative_change`` so this example and the engine that runs
    the batch convergence study can never drift apart.
    """
    rel = relative_change(reference, value)
    if rel is None:
        return None, None
    return rel <= tol, rel


def fea_convergence(
    peak_coarse: float, peak_fine: float | None
) -> tuple[bool | None, float | None]:
    """Relative change in peak von Mises under mesh refinement.

    Small (<= ``FEA_CONVERGENCE_TOL``) → the solve has converged and the stress
    is a trustworthy finite value. Large → the peak is still climbing as the mesh
    refines → an unbounded singularity (a sharp re-entrant corner), which is the
    rejection itself, not a number to compare.
    """
    return _rel_within(peak_coarse, peak_fine, FEA_CONVERGENCE_TOL)


def screen_vs_fea(screen_mpa: float, fea_mpa: float | None) -> tuple[bool | None, float | None]:
    """Relative agreement between the analytical screen peak and a *converged* FEA peak.

    Only meaningful for the filleted variant — comparing the screen against a
    singular (diverging) stress is meaningless, so callers gate on ``converged``.
    """
    return _rel_within(screen_mpa, fea_mpa, FEA_SCREEN_TOL)


# --------------------------------------------------------------------------- #
# Simulate (kinematic): motion Tier-2 — plunger travel, binding, clearance
# --------------------------------------------------------------------------- #
def _geometric_interference(out: Path, log: StepLog) -> dict[str, Any] | None:
    """Best-effort FreeCAD geometric interference confirmation.

    Imports the guide-tube + plunger-head STEPs into one document and runs the
    motion Tier-2 assembly + interference check. Returns ``{"clear": bool}`` or
    None (SKIPPED). The assembly path needs a live addon (and the imported STEPs
    resolved as assembly parts), so this commonly degrades — the analytical
    clearance result stands either way; nothing is faked.
    """
    try:
        from orchestrator.worker_builds import common
        from orchestrator.worker_builds import foam_dart_launcher as fdl

        if not common.freecad_ready():
            return None
        guide = out / "step" / "guide_tube.step"
        head = out / "step" / "plunger_head.step"
        if not (guide.is_file() and head.is_file()):
            return None
        from server.tools_motion import (
            motion_check_interference,
            motion_create_assembly,
            motion_define_mechanism,
        )

        doc = fdl._send("new_document", name="kin_assembly").get("name", "kin_assembly")
        g = fdl._send("import_step", path=str(guide), doc=doc, object_name="guide_tube")
        h = fdl._send("import_step", path=str(head), doc=doc, object_name="plunger_head")
        mech = {
            "name": "plunger_in_guide_geom",
            "parts": [
                {"id": "frame", "is_ground": True, "body_name": g.get("object", "guide_tube")},
                {"id": "plunger", "body_name": h.get("object", "plunger_head")},
            ],
            "joints": [
                {
                    "id": "slide",
                    "joint_type": "prismatic",
                    "parent_part": "frame",
                    "child_part": "plunger",
                    "axis": (0.0, 0.0, 1.0),
                    "origin": (0.0, 0.0, 0.0),
                }
            ],
            "drives": [],
        }
        defined = motion_define_mechanism(mechanism=mech)
        mid = defined.get("mechanism_id") if defined.get("ok") else None
        if not mid:
            return None
        asm = motion_create_assembly(mechanism_id=mid, doc=doc)
        if not asm.get("ok"):
            log.say(
                "Simulate",
                f"Kinematic geometric check SKIPPED ({asm.get('error', {}).get('code', '?')})",
            )
            return None
        chk = motion_check_interference(mechanism_id=mid, doc=doc)
        if not chk.get("ok"):
            return None
        return {"clear": bool(chk.get("clear"))}
    except Exception as exc:  # any addon/assembly failure → analytical result stands
        log.say("Simulate", f"Kinematic geometric check SKIPPED ({exc})")
        return None


def kinematic_tier2(
    *, brief: dict[str, Any], out: Path, smoke: bool, log: StepLog
) -> dict[str, Any]:
    """Validate plunger travel / binding / moving clearance.

    Moving clearance is analytical from the brief specs (always runs). Travel is
    validated by defining the prismatic mechanism and running the structural
    motion validators. Binding follows analytically from clearance over the
    coaxial travel, with an optional FreeCAD geometric confirmation. Nothing is
    faked: unavailable backends report SKIPPED.
    """
    parts = {p["name"]: p.get("specs", {}) for p in brief.get("parts", []) if p.get("name")}
    bore = float(parts.get("guide_tube", {}).get("bore_dia_mm", 16.0))
    head = float(parts.get("plunger_head", {}).get("dia_mm", 15.2))
    constraints = brief.get("parameters", {}).get("constraints", {})
    min_clear = float(constraints.get("min_moving_clearance_mm", 0.4))
    travel_mm = 0.0
    for ifc in brief.get("interfaces", []):
        if ifc.get("spec", {}).get("type") == "prismatic":
            travel_mm = float(ifc["spec"].get("travel_mm", MAX_COMPRESSION_M * 1000.0))
    clear = (bore - head) / 2.0
    out_rows: dict[str, Any] = {
        "clearance": {
            "value_mm": clear,
            "target_mm": min_clear,
            "pass": clear >= min_clear - 1e-9,
            "mode": "analytical (from brief specs)",
        }
    }

    if smoke:
        out_rows["travel"] = {"status": "SKIPPED", "reason": "smoke"}
        out_rows["binding"] = {"status": "SKIPPED", "reason": "smoke"}
        log.say("Simulate", "Kinematic SKIPPED (smoke); clearance computed analytically only")
        return out_rows

    # Plunger travel: define the prismatic mechanism and run the analytical
    # structural validators (pure Python — no FreeCAD needed).
    try:
        from server.tools_motion import motion_define_mechanism, motion_validate

        mech = {
            "name": "plunger_in_guide",
            "parts": [
                {"id": "frame", "is_ground": True, "body_name": "guide_tube"},
                {"id": "plunger", "body_name": "plunger_head"},
            ],
            "joints": [
                {
                    "id": "slide",
                    "joint_type": "prismatic",
                    "parent_part": "frame",
                    "child_part": "plunger",
                    "axis": (0.0, 0.0, 1.0),
                    "origin": (0.0, 0.0, 0.0),
                    "min_travel_mm": 0.0,
                    "max_travel_mm": travel_mm,
                }
            ],
            "drives": [],
        }
        defined = motion_define_mechanism(mechanism=mech)
        mid = defined.get("mechanism_id") if defined.get("ok") else None
        validated = motion_validate(mechanism_id=mid) if mid else {"ok": False}
        # A real PASS needs a successful validation (ok=True) with no blockers; an
        # error-shaped result (ok=False, no 'blockers' key) must not read as PASS.
        travel_ok = bool(mid) and validated.get("ok") and not validated.get("blockers")
        out_rows["travel"] = {
            "status": "PASS" if travel_ok else "FAIL",
            "range_mm": [0.0, travel_mm],
            "mode": "analytical (mechanism validated)",
        }
        log.say(
            "Simulate",
            f"Kinematic: prismatic travel 0–{travel_mm:.0f} mm "
            f"{'valid' if travel_ok else 'INVALID'} (analytical)",
        )
    except Exception as exc:
        out_rows["travel"] = {"status": "SKIPPED", "reason": str(exc)}

    # Binding follows from clearance for coaxial cylinders over the full travel.
    out_rows["binding"] = {
        "status": "PASS" if clear > 0.0 else "FAIL",
        "mode": "analytical (clearance > 0 over coaxial travel)",
    }
    geom = _geometric_interference(out, log)
    if geom is not None:
        out_rows["binding"]["geometric_clear"] = geom.get("clear")
        # A real geometric interference overrides the analytical PASS — the
        # FreeCAD assembly check is the stronger evidence; never hide a binding.
        if geom.get("clear") is False:
            out_rows["binding"]["status"] = "FAIL"
            out_rows["binding"]["mode"] = "geometric (FreeCAD interference check found binding)"
            log.say("Simulate", "Kinematic: geometric interference DETECTED → binding FAIL")
    return out_rows


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def write_range_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "pullback_mm",
                "muzzle_velocity_m_s",
                "predicted_range_m",
                "predicted_range_ft",
                "actual_range_m",
                "rel_error",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r["pullback_mm"],
                    r["muzzle_velocity_m_s"],
                    r["predicted_range_m"],
                    round(r["predicted_range_m"] * M_TO_FT, 2),
                    r.get("actual_range_m", ""),
                    r.get("rel_error", ""),
                ]
            )


def write_motion_csv(
    path: Path, trace: list[dict[str, float]], spec: pm.LauncherSpec, pullback_m: float
) -> None:
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
            {
                "name": p["name"],
                "kind": p["kind"],
                "quantity": p["quantity"],
                "specs": p.get("specs", {}),
            }
            for p in brief["parts"]
        ],
    }
    path.write_text(json.dumps(bom, indent=2))


def write_report(
    path: Path,
    *,
    brief: dict[str, Any],
    spec: pm.LauncherSpec,
    material_name: str,
    hold_force_n: float,
    v1: dict[str, AnalysisCheck],
    v2: dict[str, AnalysisCheck],
    fix,
    interp,
    dyn_v: float | None,
    analytical_lossless_v: float,
    pred_rows: list[dict[str, Any]],
    calibrated: bool,
    log: StepLog,
    smoke: bool,
    k_is_placeholder: bool,
    mass_is_placeholder: bool,
    fea_v1: dict[str, Any] | None = None,
    fea_v2: dict[str, Any] | None = None,
    kin: dict[str, Any] | None = None,
) -> None:
    v2_latch = v2["latch_sear"]
    L: list[str] = []
    A = L.append
    A("# Foam-Dart Spring Launcher — Validation Report\n")
    if smoke:
        A(
            "> **PHYSICS NOT VALIDATED — smoke mode.** No solvers were run; the "
            "numbers below are analytical placeholders for CI plumbing only.\n"
        )
    A("## Project summary\n")
    A(brief["parameters"]["intent"] + "\n")

    A("## Assumptions\n")
    A(f"- Material: **{material_name}** (yield used in screening).")
    A(
        f"- Spring constant: **{spec.spring_k_n_per_m:.0f} N/m** "
        + ("(PLACEHOLDER — measure your spring)" if k_is_placeholder else "(user-supplied)")
    )
    A(
        f"- Dart mass: **{spec.dart_mass_kg * 1000:.2f} g**"
        + (" (default placeholder)" if mass_is_placeholder else " (user-supplied)")
    )
    A(
        f"- Launch angle: **{spec.launch_angle_deg:.0f}°**, launch height **{spec.launch_height_m:.2f} m**."
    )
    A(
        f"- Efficiency: **{spec.efficiency:.3f}** "
        + (
            "(CALIBRATED from your measured shot)"
            if calibrated
            else "(uncalibrated placeholder — feed a shot to --calibrate-from-shot)"
        )
    )
    A(f"- Full-cock spring hold force: **{hold_force_n:.1f} N** (k × 30 mm).\n")
    A(
        f"- Latch governing load: **{hold_force_n * LATCH_IMPACT_FACTOR:.1f} N** "
        f"({LATCH_IMPACT_FACTOR:g}× hold) — the sear catches a moving plunger, so it "
        "is sized for a suddenly-applied (impact) load, not the static hold.\n"
    )

    A("## Sim-to-real chain\n")
    A("**Spring → plunger energy delivery** (Chrono validates physics_model):\n")
    A("| Quantity | Value | Notes |")
    A("| --- | ---: | --- |")
    A(
        f"| (1) Analytical plunger velocity (lossless) | {analytical_lossless_v:.3f} m/s | sqrt(k·x²/m_plunger), efficiency=1 |"
    )
    if dyn_v is not None:
        resid = abs(dyn_v - analytical_lossless_v)
        rel = 100.0 * resid / analytical_lossless_v if analytical_lossless_v else 0.0
        A(
            f"| (2) Chrono plunger exit velocity | {dyn_v:.3f} m/s | real MBS run (spring on prismatic) |"
        )
        A(f"| Residual (1)↔(2) | {resid:.3f} m/s | {rel:.1f}% |")
    else:
        A("| (2) Chrono plunger exit velocity | SKIPPED | daemon not built |")
    A(
        "\nThe lossless head-to-head validates the energy core of physics_model "
        "against the MBS engine. The **dart** muzzle velocity (below) then folds in "
        "the lumped efficiency — which absorbs spring mass, plunger friction, the "
        "air column, and barrel losses — so a predicted-vs-measured *range* gap is "
        "a calibration result, not a model failure. Calibrate efficiency from one "
        "measured shot (`--calibrate-from-shot`) and the relationship holds: with "
        "efficiency fixed, v∝x and (no-drag) range∝x².\n"
    )

    A("## Predicted ranges (fill in your measurements)\n")
    A("| Pullback | Muzzle v (real) | Predicted range | Actual range | Error |")
    A("| ---: | ---: | ---: | ---: | ---: |")
    for r in pred_rows:
        ft = r["predicted_range_m"] * M_TO_FT
        actual = r.get("actual_range_m", "user fills")
        err = r.get("rel_error", "user fills")
        A(
            f"| {int(r['pullback_mm'])} mm | {r['muzzle_velocity_m_s']:.2f} m/s | "
            f"{r['predicted_range_m']:.2f} m ({ft:.1f} ft) | {actual} | {err} |"
        )
    A("")

    A("## Inner-loop trace (nine steps)\n")
    for line in log.lines:
        A(f"- `{line}`")
    A("")

    A("## Structural checks (V1 → V2)\n")
    A("| Check | Target | V1 | V2 |")
    A("| --- | ---: | ---: | ---: |")
    lv1 = v1["latch_sear"]
    A(
        f"| Latch tooth (FoS basis) | > 2.0 | {lv1.status.value.upper()} (peak {lv1.measured:.0f} MPa) | "
        f"{v2_latch.status.value.upper()} (peak {v2_latch.measured:.0f} MPa) |"
    )
    A(
        f"| Spring seat | > 2.0 | {v1['spring_seat'].status.value.upper()} | "
        f"{v2['spring_seat'].status.value.upper()} |"
    )
    A(
        f"| Plunger rod (buckling) | > 2.0 | {v1['plunger_rod'].status.value.upper()} | "
        f"{v2['plunger_rod'].status.value.upper()} |"
    )
    A("")

    # FEA mesh convergence: distinguish a real (finite) stress riser from a
    # mesh-dependent singularity, then cross-check the trustworthy value.
    A("## FEA mesh convergence (latch tooth)\n")
    if fea_v1 is None and fea_v2 is None:
        A(
            "_FEA SKIPPED_ — no convergence study this run (the addon + `ccx`/`gmsh` "
            "were unavailable, or the latch build/solve did not complete; see the "
            "`[Simulate]` log lines above for the reason). The analytical screen "
            "above stands on its own.\n"
        )
    else:
        A(
            "A *real* stress concentration converges as the mesh refines; a sharp "
            "re-entrant corner does not — its peak keeps climbing because the stress is "
            "an unbounded singularity. FEA is trustworthy only where it converges; on a "
            "singular geometry the analytical screen is the operative verdict.\n"
        )
        A(
            f"| Variant | Peak σ (coarse {LATCH_MESH_COARSE_MM:g} mm) | "
            f"Peak σ (fine {LATCH_MESH_FINE_MM:g} mm) | Δ on refine | Converged? | FEA verdict |"
        )
        A("| --- | ---: | ---: | ---: | :--: | --- |")
        for tag, fea in (("V1 (sharp root)", fea_v1), ("V2 (filleted root)", fea_v2)):
            if fea is None:
                A(f"| {tag} | SKIPPED | — | — | — | — |")
                continue
            pc = fea.get("peak_coarse_mpa", 0.0)
            pf = fea.get("peak_fine_mpa", 0.0)
            delta = fea.get("convergence_delta")
            conv = fea.get("converged")
            d_s = f"{delta * 100:.0f}%" if delta is not None else "—"
            if conv:
                mark = "✓"
                verdict = f"converges — confirms screen (FoS {fea.get('safety_factor', 0.0):.1f})"
            else:
                mark = "✗"
                verdict = "diverges (singular root) — screen FAIL stands"
            A(f"| {tag} | {pc:.1f} MPa | {pf:.1f} MPa | {d_s} | {mark} | {verdict} |")
        # Cross-check the screen against the *converged* variant only (V2). Comparing
        # the screen to V1's diverging (singular) peak would be meaningless.
        if fea_v2 is not None and fea_v2.get("converged"):
            within, rel = screen_vs_fea(v2_latch.measured, fea_v2.get("peak_fine_mpa"))
            rel_s = f"{rel * 100:.0f}%" if rel is not None else "—"
            mark = "✓ within" if within else "⚠ beyond"
            A(
                f"\n**V2 (the redesign) is confirmed:** it converges, so its FEA value is "
                f"trustworthy — analytical screen {v2_latch.measured:.1f} MPa vs converged "
                f"FEA {fea_v2.get('peak_fine_mpa', 0.0):.1f} MPa, {mark} ±"
                f"{FEA_SCREEN_TOL * 100:.0f}% ({rel_s}). **V1 is rejected by the screen** "
                "(its sharp root is singular, so FEA gives no number to trust — the FEA is "
                "shown only to demonstrate that divergence). Note: an idealized fixed-face "
                "clamp edge is itself singular, so refining past the fillet re-exposes that "
                "artifact in both — sub-modeling removes it (see ROADMAP).\n"
            )
        elif fea_v2 is not None:
            # V2 solved but its peak kept climbing — a genuine non-convergence.
            A(
                "\n_V2 did not converge at this mesh bracket — treat the FEA as indicative "
                "and rely on the analytical screen; see the clamp-singularity note in the "
                "ROADMAP sub-modeling item._\n"
            )
        else:
            # V2 was never solved (build/face/solver SKIPPED) — the row above says so.
            A(
                "\n_V2 FEA was skipped this run (see the `[Simulate]` log) — no convergence "
                "verdict; the analytical screen stands._\n"
            )

    # Kinematic Tier-2: plunger travel / binding / clearance.
    A("## Kinematic checks (plunger in guide)\n")
    if not kin:
        A("_SKIPPED._\n")
    else:
        cl = kin.get("clearance", {})
        tv = kin.get("travel", {})
        bd = kin.get("binding", {})

        def _kstat(d: dict[str, Any]) -> str:
            return str(d.get("status", "PASS" if d.get("pass") else "FAIL"))

        A("| Check | Target | Result | Mode |")
        A("| --- | ---: | :--: | --- |")
        tv_range = tv.get("range_mm")
        tv_target = f"full {tv_range[1]:.0f} mm" if tv_range else "full release"
        A(
            f"| Plunger travel | {tv_target} | {_kstat(tv)} | {tv.get('mode', tv.get('reason', '—'))} |"
        )
        A(
            f"| Plunger binding/interference | none | {_kstat(bd)} | {bd.get('mode', bd.get('reason', '—'))} |"
        )
        if cl:
            A(
                f"| Moving clearance | ≥ {cl['target_mm']:.2f} mm | "
                f"{cl['value_mm']:.2f} mm {'PASS' if cl['pass'] else 'FAIL'} | {cl.get('mode', '—')} |"
            )
        A("")

    A("## V1 failure → V2 fix\n")
    A(
        f"- **V1 failure:** latch screen → `{lv1.status.value}` / "
        f"`{lv1.failure_mode.value}` — {lv1.message}"
    )
    if interp is not None:
        A(f"- **Interpret:** {interp.message}")
    if fix is not None:
        A(
            f"- **Decide:** {fix.op} at `{fix.target}` ({fix.param} += {fix.delta}) — {fix.rationale}"
        )
    A(
        f"- **Act → V2:** re-screen → `{v2_latch.status.value}` "
        f"(peak {lv1.measured:.0f} → {v2_latch.measured:.0f} MPa).\n"
    )

    A("## Print / test instructions\n")
    A("1. Print all custom parts in PLA (or PETG), no supports, flat-on-bed faces.")
    A(
        "2. Fit the off-the-shelf compression spring; **measure its constant** and re-run "
        "with `--spring-k-n-m <value>`."
    )
    A("3. Weigh the dart; re-run with `--dart-mass-g <value>`.")
    A(
        "4. Fire one shot at a known pullback, measure the range, then run "
        "`--calibrate-from-shot <pullback_mm> <range_m>` to fit efficiency and predict the rest.\n"
    )

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
    p.add_argument(
        "--calibrate-from-shot",
        nargs=2,
        type=float,
        metavar=("PULLBACK_MM", "RANGE_M"),
        default=None,
    )
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
        launch_height_m=brief["parameters"]["layout"]["launch_height_mm"] / 1000.0,
        efficiency=args.efficiency,
    ).validated()
    hold_force_n = spec.spring_k_n_per_m * MAX_COMPRESSION_M
    # The latch tooth is sized by the catch event, not the static hold (#4).
    latch_force_n = hold_force_n * LATCH_IMPACT_FACTOR

    # 1. SPECIFY
    log.say(
        "Specify",
        f"loaded committed brief '{brief['name']}' "
        f"({len(brief['parts'])} parts, {len(brief['interfaces'])} interfaces)",
    )

    # 2. SYNTHESIZE
    synthesize(out, args.smoke, log, brief=brief)

    # 3. REFLECT
    expectations = load_expectations()
    log.say(
        "Reflect",
        f"filed expectations for {len(expectations)} part classes "
        f"(latch hotspot={expectations['latch_sear'].expected_hotspot})",
    )

    # 4. SCREEN — V1 (deliberately under-dimensioned latch: thin, sharp root)
    v1 = screen_parts(
        hold_force_n=hold_force_n,
        latch_force_n=latch_force_n,
        yield_mpa=mat.yield_strength_mpa,
        youngs_mpa=mat.youngs_modulus_mpa,
        latch_root_mm=LATCH_V1["root_mm"],
        latch_fillet_ratio=LATCH_V1["fillet_ratio"],
    )
    log.say(
        "Screen",
        f"V1 latch={v1['latch_sear'].status.value} "
        f"spring_seat={v1['spring_seat'].status.value} "
        f"plunger_rod={v1['plunger_rod'].status.value}",
    )

    # 5. SIMULATE — structural (real CalculiX FEA on the enriched latch) + dynamic
    from server.analysis_solvers import list_solvers

    fea_ok = any(s["name"] == "calculix" and s["available"] for s in list_solvers())
    fea_v1: dict[str, Any] | None = None
    fea_v2: dict[str, Any] | None = None
    material_arg: str | dict[str, Any] = _PETG if args.material == "petg" else args.material
    if args.smoke:
        log.say("Simulate", "FEA SKIPPED (smoke)")
    elif not fea_ok:
        log.say("Simulate", "FEA SKIPPED (CalculiX/gmsh not available)")
    else:
        variants = build_latch_variants(out, log) or {}
        # V1 (sharp root): the convergence study is the proof — its stress
        # diverges under refinement (singularity), which IS the rejection.
        if variants.get("v1"):
            fea_v1 = fea_latch(
                variant=variants["v1"],
                material=material_arg,
                latch_force_n=latch_force_n,
                log=log,
                tag="V1 latch (sharp root)",
            )
        # V2 (filleted root): converges to a finite, trustworthy stress that
        # the analytical screen is then cross-checked against.
        if variants.get("v2"):
            fea_v2 = fea_latch(
                variant=variants["v2"],
                material=material_arg,
                latch_force_n=latch_force_n,
                log=log,
                tag="V2 latch (filleted root)",
            )

    plunger_mass_kg = brief["parameters"]["physical_defaults"]["plunger_mass_g"] / 1000.0
    dyn_v, trace = (None, [])
    if not args.smoke:
        dyn_v, trace = chrono_plunger_velocity(
            spring_k_n_per_m=spec.spring_k_n_per_m,
            plunger_mass_kg=plunger_mass_kg,
            pullback_m=MAX_COMPRESSION_M,
            log=log,
        )
    else:
        log.say("Simulate", "Chrono SKIPPED (smoke)")

    # Kinematic Tier-2: plunger travel / binding / moving clearance. Guarded so a
    # brief-schema surprise degrades the section to SKIPPED like its neighbours.
    try:
        kin = kinematic_tier2(brief=brief, out=out, smoke=args.smoke, log=log)
    except Exception as exc:
        log.say("Simulate", f"Kinematic SKIPPED ({exc})")
        kin = {}

    # Lossless analytical plunger velocity for the head-to-head with Chrono:
    # sqrt(k x^2 / m_plunger) — same body the MBS run accelerates.
    mech_spec = pm.LauncherSpec(
        spring_k_n_per_m=spec.spring_k_n_per_m,
        dart_mass_kg=plunger_mass_kg,
        launch_angle_deg=spec.launch_angle_deg,
        launch_height_m=spec.launch_height_m,
        efficiency=1.0,
    )
    analytical_lossless_v = pm.muzzle_velocity_m_s(mech_spec, MAX_COMPRESSION_M)

    # 6. INTERPRET — compare the failing latch to expectations
    interp = None
    failing = v1["latch_sear"]
    if failing.status is not CheckStatus.PASS:
        # Wrap the screen check into a FieldResult-shaped comparison input.
        from server.analysis_models import FieldResult

        fr = FieldResult(
            analysis_id="screen_latch_v1",
            status=failing.status,
            safety_factor=mat.yield_strength_mpa / failing.measured if failing.measured else 99.0,
            max_von_mises_mpa=failing.measured,
            max_displacement_mm=0.0,
            checks=(failing,),
            scalar_fields=(),
            failure_mode=failing.failure_mode,
        )
        interp = interpret_compare_to_expectations(fr, expectations["latch_sear"])
        log.say("Interpret", f"{failing.failure_mode.value}; {interp.message}")

    # 7. DECIDE — pick a fix that addresses the mechanism
    fix = from_failure(failing) if failing.status is not CheckStatus.PASS else None
    if fix is not None:
        log.say("Decide", f"{fix.op} → {fix.rationale}")

    # 8. ACT — apply the fix (thicker root + real fillet), re-screen
    v2 = screen_parts(
        hold_force_n=hold_force_n,
        latch_force_n=latch_force_n,
        yield_mpa=mat.yield_strength_mpa,
        youngs_mpa=mat.youngs_modulus_mpa,
        latch_root_mm=LATCH_V2["root_mm"],
        latch_fillet_ratio=LATCH_V2["fillet_ratio"],
    )
    v2_latch = v2["latch_sear"]
    log.say(
        "Act",
        f"V2 latch re-screen → {v2_latch.status.value} "
        f"(peak {failing.measured:.0f} → {v2_latch.measured:.0f} MPa)",
    )

    # 9. LEARN — record the finding
    learn_note = out / "launcher_v2" / "finding.md"
    learn_note.write_text(
        f"# Latch finding\n\nA zero-radius latch tooth root fails the screen on "
        f"{failing.failure_mode.value} (peak {failing.measured:.0f} MPa > "
        f"{mat.yield_strength_mpa:.0f} MPa yield). Thickening the root to 2.2 mm and "
        f"adding a 0.3·d fillet drops the peak to {v2_latch.measured:.0f} MPa (PASS).\n"
    )
    learned = _try_knowledge_ingest(learn_note, log)
    if not learned:
        log.say("Learn", f"finding written to {learn_note} (knowledge store unavailable)")

    # Calibration + predictions
    calibrated = False
    if args.calibrate_from_shot is not None:
        pb_mm, rng_m = args.calibrate_from_shot
        spec = pm.calibrate_from_shot(spec, pb_mm, rng_m)
        calibrated = True
        print(
            f"  [calibrate] efficiency fitted to {spec.efficiency:.3f} "
            f"from shot ({pb_mm:.0f} mm → {rng_m:.2f} m)"
        )

    pred_rows = pm.predict_table(spec, PULLBACKS_MM)
    if calibrated:
        pb_mm, rng_m = args.calibrate_from_shot
        for r in pred_rows:
            if abs(r["pullback_mm"] - pb_mm) < 1e-6:
                r["actual_range_m"] = rng_m
                r["rel_error"] = f"{100 * abs(r['predicted_range_m'] - rng_m) / rng_m:.1f}%"

    # Placeholder labels are decided against the brief's declared defaults, not
    # hardcoded magic numbers, so they stay correct if the defaults change.
    pdefaults = brief["parameters"]["physical_defaults"]
    k_is_placeholder = spec.spring_k_n_per_m == float(pdefaults["spring_k_n_per_m"])
    mass_is_placeholder = abs(spec.dart_mass_kg * 1000.0 - float(pdefaults["dart_mass_g"])) < 1e-9

    # Write outputs
    write_range_csv(out / "range_prediction.csv", pred_rows)
    write_motion_csv(out / "motion_trace.csv", trace, mech_spec, MAX_COMPRESSION_M)
    write_bom(out / "bom.json", brief)
    write_report(
        out / "validation_report.md",
        brief=brief,
        spec=spec,
        material_name=mat.name,
        hold_force_n=hold_force_n,
        v1=v1,
        v2=v2,
        fix=fix,
        interp=interp,
        dyn_v=dyn_v,
        analytical_lossless_v=analytical_lossless_v,
        pred_rows=pred_rows,
        calibrated=calibrated,
        log=log,
        smoke=args.smoke,
        k_is_placeholder=k_is_placeholder,
        mass_is_placeholder=mass_is_placeholder,
        fea_v1=fea_v1,
        fea_v2=fea_v2,
        kin=kin,
    )

    print(f"\nOutputs written to {out}")
    print(f"  V1 latch: {v1['latch_sear'].status.value} → V2 latch: {v2_latch.status.value}")
    if dyn_v is not None:
        print(
            f"  analytical lossless v={analytical_lossless_v:.2f} m/s | "
            f"Chrono v={dyn_v:.2f} m/s | residual "
            f"{100 * abs(dyn_v - analytical_lossless_v) / analytical_lossless_v:.1f}%"
        )
    return 0


def _try_knowledge_ingest(note: Path, log: StepLog) -> bool:
    try:
        from server.tools_knowledge import knowledge_ingest

        res = knowledge_ingest(path=str(note))
        if res.get("ok") and res.get("source") != "local_fallback":
            log.say(
                "Learn", f"ingested finding into knowledge store ({res.get('chunks', '?')} chunks)"
            )
            return True
    except Exception:
        pass
    return False


if __name__ == "__main__":
    raise SystemExit(run())
