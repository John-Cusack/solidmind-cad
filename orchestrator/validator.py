"""Stage 5: Geometry + Assembly Validation.

Reimport STEP files, recompute authoritative measurements, and validate
against frozen contracts. All dimensional truth comes from geometry
measurement — worker-claimed values are advisory only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.spec import (
    FailureCode,
    MasterSpec,
    Subsystem,
    WorkerResult,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DimensionCheck:
    """Result of a single dimensional check."""

    interface_id: str
    feature: str
    expected_mm: float
    measured_mm: float | None = None
    tolerance_mm: float = 0.01
    passed: bool = False
    error: str = ""
    source: str = "unknown"  # "orchestrator" | "claimed" | "unknown"


@dataclass(slots=True)
class EnvelopeCheck:
    """Result of envelope compliance check."""

    subsystem_name: str
    spec_envelope_mm: list[float] = field(default_factory=list)
    actual_bbox_mm: list[float] = field(default_factory=list)
    passed: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class ClearanceCheck:
    """Result of a clearance/collision check between two parts."""

    part_a: str
    part_b: str
    min_clearance_mm: float = 0.0
    required_clearance_mm: float = 0.0
    passed: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class SkeletonCheck:
    """Result of a skeleton constraint check (reserved volume or keepout)."""

    check: str  # "reserved_volume" | "keepout_zone"
    subsystem: str = ""
    passed: bool = False
    error: str = ""
    keepout: str = ""  # name of keepout zone, if applicable


@dataclass(slots=True)
class ValidationReport:
    """Complete validation report for a build."""

    subsystem_name: str
    worker_id: str = ""
    dimension_checks: list[DimensionCheck] = field(default_factory=list)
    envelope_check: EnvelopeCheck | None = None
    clearance_checks: list[ClearanceCheck] = field(default_factory=list)
    mass_kg: float | None = None
    mass_budget_kg: float | None = None
    mass_ok: bool = True
    me_checks: list[dict[str, Any]] = field(default_factory=list)
    overall_pass: bool = False
    failure_codes: list[FailureCode] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    measurement_source: str = "unknown"  # "orchestrator" | "claimed" | "unknown"
    skeleton_checks: list[SkeletonCheck] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation functions (work with measurements dict, no FreeCAD dependency)
# ---------------------------------------------------------------------------


def validate_dimensions(
    spec: MasterSpec,
    subsystem: Subsystem,
    measurements: dict[str, dict[str, float]],
) -> list[DimensionCheck]:
    """Validate interface dimensions against frozen contracts.

    Args:
        spec: The master spec with frozen interfaces.
        subsystem: The subsystem being validated.
        measurements: Dict of {interface_id: {feature: measured_value_mm}}.

    Returns:
        List of DimensionCheck results.
    """
    checks: list[DimensionCheck] = []
    for ifc in spec.interfaces_for(subsystem.name):
        ifc_measurements = measurements.get(ifc.id, {})
        for cp in ifc.validation.check_points:
            measured = ifc_measurements.get(cp.feature)
            check = DimensionCheck(
                interface_id=ifc.id,
                feature=cp.feature,
                expected_mm=cp.expected_mm,
                measured_mm=measured,
                tolerance_mm=cp.tolerance_mm,
            )
            if measured is not None:
                check.passed = abs(measured - cp.expected_mm) <= cp.tolerance_mm
            else:
                check.error = f"Missing measurement for {cp.feature}"
                check.passed = False
            checks.append(check)
    return checks


def validate_envelope(
    subsystem: Subsystem,
    actual_bbox_mm: list[float],
) -> EnvelopeCheck:
    """Check if actual bounding box fits within spec envelope."""
    check = EnvelopeCheck(
        subsystem_name=subsystem.name,
        spec_envelope_mm=subsystem.envelope_mm,
        actual_bbox_mm=actual_bbox_mm,
    )
    if not subsystem.envelope_mm:
        check.passed = True
        return check
    if len(actual_bbox_mm) < 3 or len(subsystem.envelope_mm) < 3:
        check.error = "Incomplete bounding box data"
        return check

    # Sort both to allow any orientation
    spec_sorted = sorted(subsystem.envelope_mm)
    actual_sorted = sorted(actual_bbox_mm)
    check.passed = all(
        a <= s * 1.001  # 0.1% tolerance for floating point
        for a, s in zip(actual_sorted, spec_sorted, strict=False)
    )
    if not check.passed:
        check.error = (
            f"Envelope exceeded: actual {actual_sorted} > spec {spec_sorted}"
        )
    return check


def validate_mass(
    subsystem: Subsystem,
    actual_mass_kg: float | None,
) -> tuple[bool, str]:
    """Check if actual mass is within budget."""
    if subsystem.mass_budget_kg is None:
        return True, "no mass budget"
    if actual_mass_kg is None:
        return False, "mass not measured"
    ok = actual_mass_kg <= subsystem.mass_budget_kg * 1.05  # 5% margin
    msg = f"mass {actual_mass_kg:.4f} kg vs budget {subsystem.mass_budget_kg:.4f} kg"
    return ok, msg


# ---------------------------------------------------------------------------
# Full validation pipeline
# ---------------------------------------------------------------------------


def validate_worker_result(
    spec: MasterSpec,
    result: WorkerResult,
    *,
    measurements: dict[str, dict[str, float]] | None = None,
    actual_bbox_mm: list[float] | None = None,
    actual_mass_kg: float | None = None,
    measurement_source: str = "unknown",
) -> ValidationReport:
    """Run full validation on a worker result.

    This function works with pre-collected measurements (e.g., from
    cad_measure_between calls). It does NOT import STEP files itself —
    that is the orchestrator's responsibility before calling this.

    Args:
        measurement_source: "orchestrator" if measurements come from
            orchestrator-side cad_measure_between, "claimed" if from
            worker metadata.json, "unknown" otherwise.
    """
    sub = spec.get_subsystem(result.subsystem_name)
    if sub is None:
        return ValidationReport(
            subsystem_name=result.subsystem_name,
            worker_id=result.worker_id,
            notes=[f"Unknown subsystem: {result.subsystem_name}"],
        )

    report = ValidationReport(
        subsystem_name=result.subsystem_name,
        worker_id=result.worker_id,
        measurement_source=measurement_source,
    )

    # Dimension checks
    if measurements:
        report.dimension_checks = validate_dimensions(spec, sub, measurements)
        for dc in report.dimension_checks:
            dc.source = measurement_source
        if measurement_source == "claimed":
            report.notes.append(
                "WARNING: measurements are worker-claimed, not orchestrator-verified"
            )

    # Envelope check
    if actual_bbox_mm:
        report.envelope_check = validate_envelope(sub, actual_bbox_mm)

    # Mass check
    report.mass_kg = actual_mass_kg
    report.mass_budget_kg = sub.mass_budget_kg
    if actual_mass_kg is not None:
        report.mass_ok, _ = validate_mass(sub, actual_mass_kg)

    # Skeleton constraint checks
    skeleton_checks = validate_skeleton_constraints(spec, sub, actual_bbox_mm)
    report.skeleton_checks = skeleton_checks

    # Determine overall pass/fail and failure codes
    _compute_overall(report)
    return report


def _compute_overall(report: ValidationReport) -> None:
    """Set overall_pass and failure_codes from individual checks."""
    report.overall_pass = True

    # If no checks were performed at all, we cannot verify compliance
    has_any_check = (
        bool(report.dimension_checks)
        or report.envelope_check is not None
        or report.mass_kg is not None
    )
    if not has_any_check:
        report.overall_pass = False
        report.notes.append("No checks performed — cannot verify compliance")
        return

    for dc in report.dimension_checks:
        if not dc.passed:
            report.overall_pass = False
            if FailureCode.INTERFACE_DIM_MISMATCH not in report.failure_codes:
                report.failure_codes.append(FailureCode.INTERFACE_DIM_MISMATCH)

    if report.envelope_check and not report.envelope_check.passed:
        report.overall_pass = False
        report.failure_codes.append(FailureCode.ENVELOPE_VIOLATION)

    if not report.mass_ok:
        report.overall_pass = False
        if FailureCode.MASS_OVER_BUDGET not in report.failure_codes:
            report.failure_codes.append(FailureCode.MASS_OVER_BUDGET)

    for cc in report.clearance_checks:
        if not cc.passed:
            report.overall_pass = False
            if FailureCode.CLEARANCE_COLLISION not in report.failure_codes:
                report.failure_codes.append(FailureCode.CLEARANCE_COLLISION)

    for sc in report.skeleton_checks:
        if not sc.passed:
            report.overall_pass = False
            if FailureCode.SKELETON_CONFLICT not in report.failure_codes:
                report.failure_codes.append(FailureCode.SKELETON_CONFLICT)


# ---------------------------------------------------------------------------
# Skeleton constraint validation (Phase 4)
# ---------------------------------------------------------------------------


def validate_skeleton_constraints(
    spec: MasterSpec,
    subsystem: Subsystem,
    actual_bbox_mm: list[float] | None,
) -> list[SkeletonCheck]:
    """Check part bbox fits within reserved volume and avoids keepout zones."""
    from orchestrator.skeleton import aabb_bounds, aabb_overlap

    checks: list[SkeletonCheck] = []
    sk = spec.skeleton

    # Check reserved volume
    reserved = sk.reserved_volumes.get(subsystem.name)
    if reserved and actual_bbox_mm and len(actual_bbox_mm) >= 3:
        rv_min, rv_max = aabb_bounds(reserved)
        if rv_min is not None and rv_max is not None:
            rv_size = [rv_max[i] - rv_min[i] for i in range(3)]
            actual_sorted = sorted(actual_bbox_mm)
            rv_sorted = sorted(rv_size)
            fits = all(
                a <= r * 1.001
                for a, r in zip(actual_sorted, rv_sorted, strict=False)
            )
            checks.append(SkeletonCheck(
                check="reserved_volume",
                subsystem=subsystem.name,
                passed=fits,
                error="" if fits else (
                    f"Part bbox {actual_sorted} exceeds reserved volume {rv_sorted}"
                ),
            ))

    # Check keepout zones
    if actual_bbox_mm and len(actual_bbox_mm) >= 3:
        part_pos = None
        if reserved:
            p_min, _ = aabb_bounds(reserved)
            if p_min is not None:
                part_pos = p_min

        if part_pos is not None:
            part_vol = {
                "origin": part_pos,
                "size": actual_bbox_mm,
            }
            for ki, keepout in enumerate(sk.keepout_zones):
                kname = keepout.get("name", f"keepout_{ki}")
                if aabb_overlap(part_vol, keepout):
                    checks.append(SkeletonCheck(
                        check="keepout_zone",
                        subsystem=subsystem.name,
                        passed=False,
                        error=f"Part '{subsystem.name}' violates keepout zone '{kname}'",
                        keepout=kname,
                    ))

    return checks


# ---------------------------------------------------------------------------
# Gate G5
# ---------------------------------------------------------------------------


def check_gate_g5(
    spec: MasterSpec,
    reports: list[ValidationReport],
) -> tuple[bool, list[str]]:
    """G5: All subsystems pass geometry + assembly validation."""
    issues: list[str] = []
    for r in reports:
        if not r.overall_pass:
            codes = ", ".join(fc.value for fc in r.failure_codes)
            issues.append(f"{r.subsystem_name} ({r.worker_id}): FAIL [{codes}]")
            for dc in r.dimension_checks:
                if not dc.passed:
                    issues.append(
                        f"  {dc.interface_id}/{dc.feature}: "
                        f"expected={dc.expected_mm} measured={dc.measured_mm} "
                        f"tol={dc.tolerance_mm}"
                    )
    return len(issues) == 0, issues


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def report_to_dict(report: ValidationReport) -> dict[str, Any]:
    """Serialize a ValidationReport to a JSON-compatible dict."""
    from orchestrator._serde import dc_to_dict
    return dc_to_dict(report)


def save_validation_report(
    reports: list[ValidationReport],
    path: Path,
) -> None:
    """Save validation reports to JSON."""
    data = {
        "reports": [report_to_dict(r) for r in reports],
        "all_pass": all(r.overall_pass for r in reports),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
