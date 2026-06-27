"""Stage 6: Verification ladder + SBCE convergence.

Runs verification in increasing cost order (L1 analytic → L2 coarse FEA → L3 high-fidelity),
then uses SBCE to rank assembly-level candidates.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.sbce import (
    AssemblyCandidate,
    Variant,
    beam_search,
    enumerate_candidates,
    filter_feasible,
    intersect_feasible_sets,
    pareto_frontier,
    rank_candidates,
)
from orchestrator.spec import MasterSpec
from orchestrator.validator import ValidationReport

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verification levels
# ---------------------------------------------------------------------------


class VerificationLevel:
    L0_DIMENSIONAL = "L0"  # geometry measurement (always runs)
    L1_ANALYTIC = "L1"     # handbook equations
    L2_COARSE_FEA = "L2"   # auto-meshed CalculiX
    L3_HIGH_FIDELITY = "L3"  # refined mesh, nonlinear, MBS


@dataclass(slots=True)
class VerificationResult:
    """Result from a single verification check."""

    level: str  # L0, L1, L2, L3
    check_name: str
    subsystem_name: str = ""
    value: float = 0.0
    unit: str = ""
    threshold: float | None = None
    passed: bool = True
    notes: str = ""


@dataclass(slots=True)
class ScoringReport:
    """Complete scoring report for a run."""

    variants: list[Variant] = field(default_factory=list)
    candidates: list[AssemblyCandidate] = field(default_factory=list)
    frontier: list[AssemblyCandidate] = field(default_factory=list)
    verification_results: list[VerificationResult] = field(default_factory=list)
    winner_index: int | None = None


# ---------------------------------------------------------------------------
# Build variants from validation reports
# ---------------------------------------------------------------------------


def build_variants(
    spec: MasterSpec,
    validation_reports: list[ValidationReport],
) -> dict[str, list[Variant]]:
    """Convert validation reports into SBCE variants grouped by subsystem."""
    by_subsystem: dict[str, list[Variant]] = {}

    for report in validation_reports:
        variant = Variant(
            subsystem_name=report.subsystem_name,
            variant_index=_extract_variant_index(report.worker_id),
            feasible=report.overall_pass,
        )
        # Copy measured values
        if report.mass_kg is not None:
            variant.measured["mass_kg"] = report.mass_kg
            variant.scores["mass"] = report.mass_kg
        for dc in report.dimension_checks:
            if dc.measured_mm is not None:
                variant.measured[f"{dc.interface_id}/{dc.feature}"] = dc.measured_mm
        if report.envelope_check and report.envelope_check.actual_bbox_mm:
            bbox = report.envelope_check.actual_bbox_mm
            if len(bbox) >= 3:
                vol = bbox[0] * bbox[1] * bbox[2]
                variant.measured["volume_mm3"] = vol
                variant.scores["volume_mm3"] = vol

        if not report.overall_pass:
            codes = ", ".join(fc.value for fc in report.failure_codes)
            variant.elimination_reason = f"Validation failed: {codes}"

        by_subsystem.setdefault(report.subsystem_name, []).append(variant)

    return by_subsystem


def _extract_variant_index(worker_id: str) -> int:
    """Extract variant index from worker_id like 'sun_gear_0'."""
    parts = worker_id.rsplit("_", 1)
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------------------
# L1 analytic checks (deterministic, no FEA)
# ---------------------------------------------------------------------------


def run_l1_checks(
    spec: MasterSpec,
    variants_by_subsystem: dict[str, list[Variant]],
) -> list[VerificationResult]:
    """Run L1 analytic verification checks.

    These are deterministic engineering formulas — no FEA needed.
    Checks mass budgets, basic stress estimates from interface loads,
    and objective thresholds.
    """
    results: list[VerificationResult] = []

    for sub_name, variants in variants_by_subsystem.items():
        sub = spec.get_subsystem(sub_name)
        if sub is None:
            continue

        for v in variants:
            if not v.feasible:
                continue

            # Mass check
            mass = v.measured.get("mass_kg")
            if mass is not None and sub.mass_budget_kg is not None:
                passed = mass <= sub.mass_budget_kg
                results.append(VerificationResult(
                    level=VerificationLevel.L1_ANALYTIC,
                    check_name="mass_budget",
                    subsystem_name=sub_name,
                    value=mass,
                    unit="kg",
                    threshold=sub.mass_budget_kg,
                    passed=passed,
                    notes=f"variant_{v.variant_index}",
                ))
                if not passed:
                    v.feasible = False
                    v.elimination_reason = (
                        f"Mass {mass:.4f} > budget {sub.mass_budget_kg:.4f} kg"
                    )

    return results


# ---------------------------------------------------------------------------
# Scoring pipeline
# ---------------------------------------------------------------------------


def score_run(
    spec: MasterSpec,
    validation_reports: list[ValidationReport],
    *,
    beam_width: int = 5,
    max_candidates: int = 50,
    run_dir: Path | None = None,
) -> ScoringReport:
    """Full scoring pipeline: variants → filter → SBCE → rank → Pareto.

    Returns a ScoringReport with ranked candidates and the Pareto frontier.
    """
    report = ScoringReport()

    # Build variants from validation
    variants_by_sub = build_variants(spec, validation_reports)

    # Filter by hard thresholds
    for sub_name in list(variants_by_sub.keys()):
        variants_by_sub[sub_name] = filter_feasible(variants_by_sub[sub_name], spec)

    # L1 verification
    report.verification_results = run_l1_checks(spec, variants_by_sub)

    # Re-filter after L1
    for sub_name in list(variants_by_sub.keys()):
        variants_by_sub[sub_name] = [
            v for v in variants_by_sub[sub_name] if v.feasible
        ]

    # L2: Coarse FEA (only if objectives require stress/deflection)
    if _needs_fea(spec) and run_dir is not None:
        l2_results = _run_l2_checks(spec, variants_by_sub, run_dir)
        report.verification_results.extend(l2_results)
        for sub_name in list(variants_by_sub.keys()):
            variants_by_sub[sub_name] = [
                v for v in variants_by_sub[sub_name] if v.feasible
            ]

    # Flatten variants for report
    for variants in variants_by_sub.values():
        report.variants.extend(variants)

    # SBCE intersection
    narrowed = intersect_feasible_sets(variants_by_sub, spec)

    # Enumerate or beam search
    total_combos = 1
    for variants in narrowed.values():
        total_combos *= max(len(variants), 1)

    if total_combos <= max_candidates:
        candidates = enumerate_candidates(narrowed, max_candidates=max_candidates)
    else:
        candidates = beam_search(narrowed, spec, beam_width=beam_width)

    # Rank and find Pareto frontier
    report.candidates = rank_candidates(candidates, spec)
    report.frontier = pareto_frontier(report.candidates, spec)

    if report.candidates:
        report.winner_index = 0  # best-ranked candidate

    return report


# ---------------------------------------------------------------------------
# L2 FEA checks
# ---------------------------------------------------------------------------

_FEA_KEYWORDS = {"stress", "von_mises", "displacement", "deflection", "safety_factor"}


def _needs_fea(spec: MasterSpec) -> bool:
    """True if objectives mention stress/deflection or interfaces carry loads."""
    for obj in spec.objectives:
        if any(kw in obj.name.lower() for kw in _FEA_KEYWORDS):
            return True
    for ifc in spec.interfaces:
        for lc in ifc.loads:
            total = (
                abs(lc.torque_nm)
                + abs(lc.axial_force_n)
                + abs(lc.radial_force_n)
                + abs(lc.bending_moment_nm)
            )
            if total > 1e-9:
                return True
    return False


def _run_l2_checks(
    spec: MasterSpec,
    variants_by_sub: dict[str, list],
    run_dir: Path,
) -> list[VerificationResult]:
    """Run L2 coarse FEA on all feasible variants that have STEP files."""
    from orchestrator.fea import FEAReport, run_l2_fea
    from orchestrator.materials import resolve_material

    results: list[VerificationResult] = []

    for sub_name, variants in variants_by_sub.items():
        sub = spec.get_subsystem(sub_name)
        if sub is None:
            continue

        material = resolve_material(sub.material)
        if material is None:
            log.warning("Unknown material '%s' for %s — skipping L2", sub.material, sub_name)
            continue

        interfaces = spec.interfaces_for(sub_name)

        for v in variants:
            if not v.feasible:
                continue

            # Find STEP file for this variant
            variant_dir = run_dir / f"{sub_name}_{v.variant_index}" / "output"
            step_files = list(variant_dir.glob("*.step")) if variant_dir.exists() else []
            if not step_files:
                step_files = list(variant_dir.glob("*.stp")) if variant_dir.exists() else []
            if not step_files:
                log.debug("No STEP file for %s variant %d", sub_name, v.variant_index)
                continue

            step_path = step_files[0]
            work_dir = run_dir / f"{sub_name}_{v.variant_index}" / "fea"

            try:
                report: FEAReport = run_l2_fea(
                    step_path, sub, interfaces, material, work_dir,
                )
            except Exception:
                log.exception("L2 FEA failed for %s variant %d", sub_name, v.variant_index)
                results.append(VerificationResult(
                    level=VerificationLevel.L2_COARSE_FEA,
                    check_name="fea_error",
                    subsystem_name=sub_name,
                    passed=False,
                    notes=f"variant_{v.variant_index}: FEA pipeline error",
                ))
                continue

            # Record results
            variant_tag = f"variant_{v.variant_index}"

            results.append(VerificationResult(
                level=VerificationLevel.L2_COARSE_FEA,
                check_name="max_von_mises",
                subsystem_name=sub_name,
                value=report.filtered_max_stress_mpa,
                unit="MPa",
                threshold=material.yield_strength_mpa,
                passed=report.safety_factor >= 1.0,
                notes=variant_tag,
            ))

            if report.fine:
                results.append(VerificationResult(
                    level=VerificationLevel.L2_COARSE_FEA,
                    check_name="max_displacement",
                    subsystem_name=sub_name,
                    value=report.fine.max_displacement_mm,
                    unit="mm",
                    passed=True,  # informational unless objective sets threshold
                    notes=variant_tag,
                ))

            results.append(VerificationResult(
                level=VerificationLevel.L2_COARSE_FEA,
                check_name="convergence",
                subsystem_name=sub_name,
                value=report.convergence_pct,
                unit="%",
                threshold=10.0,
                passed=report.converged,
                notes=variant_tag,
            ))

            results.append(VerificationResult(
                level=VerificationLevel.L2_COARSE_FEA,
                check_name="safety_factor",
                subsystem_name=sub_name,
                value=report.safety_factor,
                unit="",
                threshold=1.0,
                passed=report.safety_factor >= 1.0,
                notes=variant_tag,
            ))

            # Store in variant measured/scores
            v.measured["max_von_mises_mpa"] = report.filtered_max_stress_mpa
            v.measured["max_displacement_mm"] = (
                report.fine.max_displacement_mm if report.fine else 0.0
            )
            v.scores["max_von_mises"] = report.filtered_max_stress_mpa
            v.scores["safety_factor"] = report.safety_factor

            # Mark infeasible if failed
            if not report.passed:
                v.feasible = False
                reasons = []
                if report.safety_factor < 1.0:
                    reasons.append(f"SF={report.safety_factor:.2f} < 1.0")
                if not report.converged:
                    reasons.append(f"convergence={report.convergence_pct:.1f}% > 10%")
                v.elimination_reason = f"L2 FEA: {', '.join(reasons)}"

    return results


# ---------------------------------------------------------------------------
# Gate G6
# ---------------------------------------------------------------------------


def check_gate_g6(
    spec: MasterSpec,
    scoring_report: ScoringReport,
) -> tuple[bool, list[str]]:
    """G6: At least one assembly candidate satisfies all hard thresholds."""
    issues: list[str] = []

    if not scoring_report.candidates:
        issues.append("No assembly candidates survived filtering")
        return False, issues

    if not scoring_report.frontier:
        issues.append("Pareto frontier is empty")
        return False, issues

    # Check that at least one candidate passes all hard thresholds
    for obj in spec.objectives:
        if obj.threshold is None:
            continue
        any_passes = False
        for candidate in scoring_report.frontier:
            values: list[float] = []
            for v in candidate.variants.values():
                score = v.scores.get(obj.name)
                if score is None:
                    score = v.measured.get(obj.name)
                if score is not None:
                    values.append(score)
            if values:
                avg = sum(values) / len(values)
                if obj.direction == "minimize" and avg <= obj.threshold:
                    any_passes = True
                    break
                elif obj.direction == "maximize" and avg >= obj.threshold:
                    any_passes = True
                    break
        # If no variant has data for this objective, skip the check
        if not any_passes and any(
            obj.name in v.scores or obj.name in v.measured
            for c in scoring_report.frontier
            for v in c.variants.values()
        ):
            issues.append(
                f"No Pareto candidate meets threshold for '{obj.name}' "
                f"({obj.direction} {obj.threshold} {obj.unit})"
            )

    return len(issues) == 0, issues


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def save_scoring_report(report: ScoringReport, path: Path) -> None:
    """Save scoring report to JSON."""
    data: dict[str, Any] = {
        "variant_count": len(report.variants),
        "candidate_count": len(report.candidates),
        "frontier_count": len(report.frontier),
        "winner_index": report.winner_index,
        "verification_results": [
            {
                "level": vr.level,
                "check_name": vr.check_name,
                "subsystem_name": vr.subsystem_name,
                "value": vr.value,
                "unit": vr.unit,
                "threshold": vr.threshold,
                "passed": vr.passed,
                "notes": vr.notes,
            }
            for vr in report.verification_results
        ],
        "candidates": [
            {
                "assembly_score": c.assembly_score,
                "feasible": c.feasible,
                "variants": {
                    name: {
                        "variant_index": v.variant_index,
                        "feasible": v.feasible,
                        "scores": v.scores,
                    }
                    for name, v in c.variants.items()
                },
            }
            for c in report.candidates
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
