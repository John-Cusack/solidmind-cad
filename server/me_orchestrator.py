from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.models import Finding, Severity, ValidatorInfo, ValidatorResult


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.upper() == "TBD" or text == "":
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    if f is None:
        return None
    return int(round(f))


def _geom_float(geom: dict[str, Any], *keys: str) -> float | None:
    """Try multiple field names in order, return the first valid float."""
    for key in keys:
        val = _as_float(geom.get(key))
        if val is not None:
            return val
    return None


def _safe_section(sheet: dict[str, Any], key: str) -> dict[str, Any]:
    """Extract a dict section from the constraint sheet, defaulting to {}."""
    val = sheet.get(key, {})
    return val if isinstance(val, dict) else {}


# ---------------------------------------------------------------------------
# Individual validator functions
# ---------------------------------------------------------------------------

def _check_min_thickness(sheet: dict[str, Any]) -> ValidatorResult:
    """Compare thinnest wall/blade against process minimum feature size."""
    geom = _safe_section(sheet, "geometry_interfaces")
    mfg = _safe_section(sheet, "manufacturing")
    min_wall = _geom_float(geom, "min_wall_thickness_mm", "min_blade_thickness_mm")
    min_feature = _as_float(mfg.get("min_feature_size_mm"))

    if min_wall is None or min_feature is None:
        return ValidatorResult(
            name="min_thickness_check", status="warn",
            message="Cannot evaluate min_thickness_check: wall thickness or process feature size is missing.",
            measured={"min_wall_thickness_mm": min_wall, "min_feature_size_mm": min_feature},
            priority=910,
        )
    if min_wall < min_feature:
        return ValidatorResult(
            name="min_thickness_check", status="fail",
            message=(
                f"Minimum wall thickness {min_wall:.3f} mm is below "
                f"process minimum feature size {min_feature:.3f} mm."
            ),
            measured={"min_wall_thickness_mm": min_wall, "min_feature_size_mm": min_feature},
            priority=910,
        )
    if min_wall < (min_feature * 1.1):
        return ValidatorResult(
            name="min_thickness_check", status="warn",
            message=(
                f"Minimum wall thickness {min_wall:.3f} mm is close to "
                f"the process floor {min_feature:.3f} mm."
            ),
            measured={"min_wall_thickness_mm": min_wall, "min_feature_size_mm": min_feature},
            priority=910,
        )
    return ValidatorResult(
        name="min_thickness_check", status="pass",
        message="Minimum wall thickness is above process minimum feature size.",
        measured={"min_wall_thickness_mm": min_wall, "min_feature_size_mm": min_feature},
        priority=910,
    )


def _check_sharp_edge(sheet: dict[str, Any]) -> ValidatorResult:
    """Check fillet radii against stress-riser threshold."""
    geom = _safe_section(sheet, "geometry_interfaces")
    mfg = _safe_section(sheet, "manufacturing")
    thresholds = _safe_section(sheet, "thresholds")
    min_fillet = _as_float(geom.get("min_fillet_radius_mm"))
    min_feature = _as_float(mfg.get("min_feature_size_mm"))

    fillet_threshold = _as_float(thresholds.get("min_fillet_threshold_mm"))
    if fillet_threshold is None:
        fillet_threshold = None if min_feature is None else max(0.5, min_feature * 0.5)

    measured = {"min_fillet_radius_mm": min_fillet, "threshold_mm": fillet_threshold}

    if min_fillet is None or fillet_threshold is None:
        return ValidatorResult(
            name="sharp_edge_check", status="warn",
            message="Cannot evaluate sharp_edge_check: fillet radius or threshold is missing.",
            measured=measured, priority=900,
        )
    if min_fillet < fillet_threshold:
        return ValidatorResult(
            name="sharp_edge_check", status="fail",
            message=(
                f"Minimum fillet radius {min_fillet:.3f} mm is below "
                f"threshold {fillet_threshold:.3f} mm."
            ),
            measured=measured, priority=900,
        )
    return ValidatorResult(
        name="sharp_edge_check", status="pass",
        message="Minimum fillet radius satisfies stress-riser threshold.",
        measured=measured, priority=900,
    )


def _check_symmetry(sheet: dict[str, Any]) -> ValidatorResult:
    """Check polar symmetry and blade/feature count against LLM-provided range."""
    geom = _safe_section(sheet, "geometry_interfaces")
    bal = _safe_section(sheet, "balance_rotor")
    thresholds = _safe_section(sheet, "thresholds")

    symmetry_required = bool(bal.get("symmetry_required", False))
    blade_count = _as_int(geom.get("blade_count"))
    min_blade_count = _as_int(thresholds.get("min_blade_count"))
    if min_blade_count is None:
        min_blade_count = 2  # safe default for any rotating part

    measured = {"symmetry_required": symmetry_required, "blade_count": blade_count, "min_blade_count": min_blade_count}

    if not symmetry_required:
        return ValidatorResult(
            name="symmetry_check", status="warn",
            message="symmetry_required not set; rotating parts typically need polar symmetry.",
            measured=measured, priority=870,
        )
    if blade_count is None:
        return ValidatorResult(
            name="symmetry_check", status="warn",
            message="Cannot verify symmetry: blade_count is missing.",
            measured=measured, priority=870,
        )
    if blade_count < min_blade_count:
        return ValidatorResult(
            name="symmetry_check", status="warn",
            message=(
                f"Blade count {blade_count} is below expected minimum "
                f"of {min_blade_count} for this part type."
            ),
            measured=measured, priority=870,
        )
    return ValidatorResult(
        name="symmetry_check", status="pass",
        message=(
            f"Symmetry requirement present, blade count {blade_count} "
            f"is within expected range (>= {min_blade_count})."
        ),
        measured=measured, priority=870,
    )


def _check_centrifugal_stress(sheet: dict[str, Any]) -> ValidatorResult:
    """Hoop stress estimate for rotating parts: sigma = rho*omega^2*r^2/3 (solid disc proxy)."""
    geom = _safe_section(sheet, "geometry_interfaces")
    env = _safe_section(sheet, "operating_envelope")
    mat = _safe_section(sheet, "material")
    thresholds = _safe_section(sheet, "thresholds")

    outer_diam_mm = _geom_float(geom, "outer_diameter_mm", "exducer_diameter_mm")
    max_rpm = _as_float(env.get("max_rpm"))
    density = _as_float(mat.get("density_kg_m3"))
    yield_mpa = _as_float(mat.get("yield_strength_at_temp_mpa"))
    creep_mpa = _as_float(mat.get("creep_limit_at_temp_mpa"))

    sf_fail = _as_float(thresholds.get("min_safety_factor")) or 1.5
    sf_warn = _as_float(thresholds.get("preferred_safety_factor")) or max(sf_fail, 2.0)

    sigma_mpa = None
    safety_factor = None

    if max_rpm is None or density is None or outer_diam_mm is None:
        return ValidatorResult(
            name="centrifugal_stress_proxy", status="warn",
            message="Cannot compute centrifugal stress: rpm, density, or diameter is missing.",
            measured={
                "max_rpm": max_rpm, "outer_diameter_mm": outer_diam_mm,
                "density_kg_m3": density, "sigma_mpa": None,
                "yield_strength_at_temp_mpa": yield_mpa, "creep_limit_at_temp_mpa": creep_mpa,
                "safety_factor": None, "min_safety_factor": sf_fail, "preferred_safety_factor": sf_warn,
            },
            priority=950,
        )

    omega = max_rpm * (2.0 * math.pi / 60.0)
    radius_m = outer_diam_mm / 2000.0
    sigma_pa = density * (omega ** 2) * (radius_m ** 2) / 3.0
    sigma_mpa = sigma_pa / 1_000_000.0

    if yield_mpa is not None and sigma_mpa > 0:
        safety_factor = yield_mpa / sigma_mpa

    measured = {
        "max_rpm": max_rpm, "outer_diameter_mm": outer_diam_mm,
        "density_kg_m3": density, "sigma_mpa": sigma_mpa,
        "yield_strength_at_temp_mpa": yield_mpa, "creep_limit_at_temp_mpa": creep_mpa,
        "safety_factor": safety_factor, "min_safety_factor": sf_fail, "preferred_safety_factor": sf_warn,
    }

    if creep_mpa is not None and sigma_mpa > creep_mpa:
        return ValidatorResult(
            name="centrifugal_stress_proxy", status="fail",
            message=(
                f"Centrifugal stress proxy {sigma_mpa:.1f} MPa exceeds "
                f"creep limit {creep_mpa:.1f} MPa."
            ),
            measured=measured, priority=950,
        )
    if safety_factor is None:
        return ValidatorResult(
            name="centrifugal_stress_proxy", status="warn",
            message=(
                f"Centrifugal stress proxy {sigma_mpa:.1f} MPa computed, "
                f"but yield strength is missing for safety factor check."
            ),
            measured=measured, priority=950,
        )
    if safety_factor < sf_fail:
        return ValidatorResult(
            name="centrifugal_stress_proxy", status="fail",
            message=(
                f"Centrifugal safety factor {safety_factor:.2f} is below "
                f"minimum {sf_fail:.2f} at {max_rpm:.0f} rpm."
            ),
            measured=measured, priority=950,
        )
    if safety_factor < sf_warn:
        return ValidatorResult(
            name="centrifugal_stress_proxy", status="warn",
            message=(
                f"Centrifugal safety factor {safety_factor:.2f} is below "
                f"preferred {sf_warn:.2f}; consider adding margin."
            ),
            measured=measured, priority=950,
        )
    return ValidatorResult(
        name="centrifugal_stress_proxy", status="pass",
        message=f"Centrifugal stress proxy gives safety factor {safety_factor:.2f}.",
        measured=measured, priority=950,
    )


def _check_mass_properties(sheet: dict[str, Any]) -> ValidatorResult:
    """Rough mass estimate from annular disc proxy."""
    geom = _safe_section(sheet, "geometry_interfaces")
    mat = _safe_section(sheet, "material")
    thresholds = _safe_section(sheet, "thresholds")

    min_wall = _geom_float(geom, "min_wall_thickness_mm", "min_blade_thickness_mm")
    outer_diam_mm = _geom_float(geom, "outer_diameter_mm", "exducer_diameter_mm")
    hub_diam_mm = _geom_float(geom, "hub_diameter_mm", "inner_diameter_mm")
    density = _as_float(mat.get("density_kg_m3"))
    max_mass = _as_float(thresholds.get("max_mass_kg"))

    mass_kg = None
    polar_inertia_kg_m2 = None

    if density is None or outer_diam_mm is None or hub_diam_mm is None or min_wall is None:
        return ValidatorResult(
            name="mass_properties", status="warn",
            message="Cannot compute mass proxy: density, diameters, or wall thickness is incomplete.",
            measured={"mass_kg": None, "polar_inertia_kg_m2": None, "max_mass_kg": max_mass},
            priority=740,
        )

    r_out = outer_diam_mm / 2000.0
    r_in = max(0.0, hub_diam_mm / 2000.0)
    thickness_m = max(0.004, (min_wall / 1000.0) * 6.0)
    volume_m3 = math.pi * max(0.0, (r_out ** 2) - (r_in ** 2)) * thickness_m
    mass_kg = density * volume_m3
    polar_inertia_kg_m2 = 0.5 * mass_kg * ((r_out ** 2) + (r_in ** 2))
    measured = {"mass_kg": mass_kg, "polar_inertia_kg_m2": polar_inertia_kg_m2, "max_mass_kg": max_mass}

    if mass_kg <= 0:
        return ValidatorResult(
            name="mass_properties", status="fail",
            message="Mass proxy produced non-positive mass; check geometry/material inputs.",
            measured=measured, priority=740,
        )
    if max_mass is not None and mass_kg > max_mass:
        return ValidatorResult(
            name="mass_properties", status="warn",
            message=f"Mass proxy {mass_kg:.3f} kg exceeds target maximum {max_mass:.3f} kg.",
            measured=measured, priority=740,
        )
    return ValidatorResult(
        name="mass_properties", status="pass",
        message=f"Mass proxy computed at {mass_kg:.3f} kg.",
        measured=measured, priority=740,
    )


def _check_manufacturability(sheet: dict[str, Any]) -> ValidatorResult:
    """Process-specific manufacturability heuristics."""
    geom = _safe_section(sheet, "geometry_interfaces")
    mfg = _safe_section(sheet, "manufacturing")

    min_wall = _geom_float(geom, "min_wall_thickness_mm", "min_blade_thickness_mm")
    min_fillet = _as_float(geom.get("min_fillet_radius_mm"))
    process = str(mfg.get("process", "unknown"))
    draft_angle = _as_float(mfg.get("draft_angle_min_deg"))
    measured = {"process": process, "draft_angle_min_deg": draft_angle}

    if process == "casting":
        if draft_angle is None:
            return ValidatorResult(
                name="manufacturability_heuristics", status="warn",
                message="Casting process selected but draft_angle_min_deg is TBD.",
                measured=measured, priority=780,
            )
        if draft_angle < 1.0:
            return ValidatorResult(
                name="manufacturability_heuristics", status="fail",
                message=f"Draft angle {draft_angle:.2f} deg is below castability floor (1.0 deg).",
                measured=measured, priority=780,
            )
        if draft_angle < 2.0:
            return ValidatorResult(
                name="manufacturability_heuristics", status="warn",
                message=f"Draft angle {draft_angle:.2f} deg is manufacturable but tight for casting.",
                measured=measured, priority=780,
            )
        return ValidatorResult(
            name="manufacturability_heuristics", status="pass",
            message="Casting draft-angle is within recommended range.",
            measured=measured, priority=780,
        )

    if process in ("5axis_machining", "machining"):
        if min_fillet is not None and min_fillet < 0.5:
            return ValidatorResult(
                name="manufacturability_heuristics", status="warn",
                message="Small internal radii may require non-standard tooling.",
                measured=measured, priority=780,
            )
        return ValidatorResult(
            name="manufacturability_heuristics", status="pass",
            message="Machining assumptions appear reasonable.",
            measured=measured, priority=780,
        )

    if process in ("fdm", "sla", "sls"):
        min_wall_print = _as_float(mfg.get("min_wall_thickness_mm"))
        if min_wall_print is None:
            min_wall_print = {"fdm": 0.8, "sla": 0.5, "sls": 0.7}.get(process)
        issues: list[str] = []
        if min_wall is not None and min_wall_print is not None and min_wall < min_wall_print:
            issues.append(
                f"Wall thickness {min_wall:.2f} mm below {process.upper()} "
                f"minimum {min_wall_print:.2f} mm."
            )
        max_overhang = _as_float(mfg.get("max_overhang_angle_deg"))
        if process == "fdm" and max_overhang is not None and max_overhang > 45:
            issues.append(
                f"Overhang angle {max_overhang:.0f} deg exceeds 45 deg; "
                f"supports likely needed."
            )
        if issues:
            return ValidatorResult(
                name="manufacturability_heuristics", status="warn",
                message=" ".join(issues),
                measured=measured, priority=780,
            )
        return ValidatorResult(
            name="manufacturability_heuristics", status="pass",
            message=f"{process.upper()} manufacturing assumptions appear reasonable.",
            measured=measured, priority=780,
        )

    return ValidatorResult(
        name="manufacturability_heuristics", status="pass",
        message=f"No process-specific heuristics for {process!r}; skipping.",
        measured=measured, priority=780,
    )


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

VALIDATORS: dict[str, tuple[Callable[[dict[str, Any]], ValidatorResult], ValidatorInfo]] = {
    "min_thickness_check": (_check_min_thickness, ValidatorInfo(
        name="min_thickness_check",
        description="Compares thinnest wall/blade against process minimum feature size.",
        reads=(
            "geometry_interfaces.min_wall_thickness_mm",
            "geometry_interfaces.min_blade_thickness_mm",
            "manufacturing.min_feature_size_mm",
        ),
        thresholds={},
        priority=910,
    )),
    "sharp_edge_check": (_check_sharp_edge, ValidatorInfo(
        name="sharp_edge_check",
        description="Checks fillet radii against stress-riser threshold.",
        reads=(
            "geometry_interfaces.min_fillet_radius_mm",
            "manufacturing.min_feature_size_mm",
        ),
        thresholds={"min_fillet_threshold_mm": "max(0.5, min_feature_size * 0.5)"},
        priority=900,
    )),
    "symmetry_check": (_check_symmetry, ValidatorInfo(
        name="symmetry_check",
        description="Checks polar symmetry and blade/feature count against LLM-provided range.",
        reads=(
            "geometry_interfaces.blade_count",
            "balance_rotor.symmetry_required",
        ),
        thresholds={"min_blade_count": 2},
        priority=870,
    )),
    "centrifugal_stress_proxy": (_check_centrifugal_stress, ValidatorInfo(
        name="centrifugal_stress_proxy",
        description=(
            "Hoop stress estimate for rotating parts (solid disc proxy). "
            "Computes safety factor against yield and checks creep limit."
        ),
        reads=(
            "geometry_interfaces.outer_diameter_mm",
            "geometry_interfaces.exducer_diameter_mm",
            "operating_envelope.max_rpm",
            "material.density_kg_m3",
            "material.yield_strength_at_temp_mpa",
            "material.creep_limit_at_temp_mpa",
        ),
        thresholds={"min_safety_factor": 1.5, "preferred_safety_factor": 2.0},
        priority=950,
    )),
    "mass_properties": (_check_mass_properties, ValidatorInfo(
        name="mass_properties",
        description="Rough mass estimate from annular disc proxy with optional max-mass threshold.",
        reads=(
            "geometry_interfaces.outer_diameter_mm",
            "geometry_interfaces.hub_diameter_mm",
            "geometry_interfaces.min_wall_thickness_mm",
            "material.density_kg_m3",
        ),
        thresholds={"max_mass_kg": "None (optional upper bound)"},
        priority=740,
    )),
    "manufacturability_heuristics": (_check_manufacturability, ValidatorInfo(
        name="manufacturability_heuristics",
        description=(
            "Process-specific checks for casting (draft angle), "
            "machining (internal radii), and 3D printing (wall thickness, overhangs)."
        ),
        reads=(
            "manufacturing.process",
            "manufacturing.draft_angle_min_deg",
            "manufacturing.min_wall_thickness_mm",
            "manufacturing.max_overhang_angle_deg",
            "geometry_interfaces.min_fillet_radius_mm",
        ),
        thresholds={},
        priority=780,
    )),
}


def list_validators() -> list[dict[str, Any]]:
    """Return metadata for all registered validators."""
    return [
        {
            "name": info.name,
            "description": info.description,
            "reads": list(info.reads),
            "thresholds": info.thresholds,
            "priority": info.priority,
        }
        for _, info in VALIDATORS.values()
    ]


# ---------------------------------------------------------------------------
# validate_constraint_sheet — runs selected validators from the registry
# ---------------------------------------------------------------------------

def validate_constraint_sheet(constraint_sheet: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic proxy validators against a constraint dict.

    All thresholds (min_blade_count, max_mass_kg, min_safety_factor, etc.)
    are read from the constraint dict itself — the LLM sets them based on
    its engineering knowledge and research for the specific part type.
    """
    requested = constraint_sheet.get("validators", [])
    if not isinstance(requested, list) or not requested:
        requested = list(VALIDATORS.keys())

    results: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []

    for name in requested:
        entry = VALIDATORS.get(name)
        if entry is None:
            continue
        fn, _info = entry
        vr = fn(constraint_sheet)

        results.append({
            "validator": vr.name,
            "status": vr.status,
            "message": vr.message,
            "measured": vr.measured,
        })

        finding = Finding(
            rule_id=f"me.{vr.name}",
            severity=(
                Severity.BLOCK if vr.status == "fail"
                else Severity.WARN if vr.status == "warn"
                else Severity.NOTE
            ),
            message=vr.message,
            priority=vr.priority,
        ).to_dict()

        if vr.status == "fail":
            blockers.append(finding)
        elif vr.status == "warn":
            warnings.append(finding)
        else:
            notes.append(finding)

    blockers.sort(key=lambda f: (-int(f.get("priority", 0)), str(f.get("rule_id", ""))))
    warnings.sort(key=lambda f: (-int(f.get("priority", 0)), str(f.get("rule_id", ""))))
    notes.sort(key=lambda f: (-int(f.get("priority", 0)), str(f.get("rule_id", ""))))

    return {
        "ok": True,
        "validators_run": [str(v) for v in requested],
        "results": results,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "summary": (
            f"Validation complete: {len(blockers)} blockers, {len(warnings)} warnings, "
            f"{len(notes)} notes across {len(requested)} validators."
        ),
    }


def build_traceability_matrix(
    constraint_sheet: dict[str, Any],
    validation_report: dict[str, Any],
) -> dict[str, Any]:
    """Build requirement-to-evidence traceability matrix."""
    requirements = constraint_sheet.get("requirements", [])
    if not isinstance(requirements, list):
        requirements = []

    waivers = constraint_sheet.get("waivers", [])
    waiver_ids = {
        str(w.get("requirement_id"))
        for w in waivers
        if isinstance(w, dict) and isinstance(w.get("requirement_id"), str)
    }

    result_by_validator: dict[str, dict[str, Any]] = {}
    for result in validation_report.get("results", []) if isinstance(validation_report, dict) else []:
        if not isinstance(result, dict):
            continue
        validator = result.get("validator")
        if isinstance(validator, str):
            result_by_validator[validator] = result

    matrix: list[dict[str, Any]] = []
    for req in requirements:
        if not isinstance(req, dict):
            continue
        requirement_id = req.get("requirement_id")
        validator = req.get("validator")
        if not isinstance(requirement_id, str) or not requirement_id:
            continue

        status = "tbd"
        evidence: dict[str, Any] = {}

        if requirement_id in waiver_ids:
            status = "waived"
            evidence = {"reason": "Waiver present in constraint sheet."}
        elif isinstance(validator, str) and validator in result_by_validator:
            vr = result_by_validator[validator]
            vr_status = vr.get("status")
            if isinstance(vr_status, str):
                if vr_status == "pass":
                    status = "pass"
                elif vr_status == "fail":
                    status = "fail"
                elif vr_status == "warn":
                    status = "tbd"
            evidence = {
                "validator": validator,
                "message": vr.get("message"),
                "measured": vr.get("measured", {}),
            }

        matrix.append(
            {
                "requirement_id": requirement_id,
                "statement": req.get("statement", ""),
                "source": req.get("source", "unknown"),
                "linked_constraints": req.get("linked_constraints", []),
                "validator": validator,
                "status": status,
                "evidence": evidence,
            }
        )

    matrix.sort(key=lambda row: str(row.get("requirement_id", "")))

    status_counts = {"pass": 0, "fail": 0, "tbd": 0, "waived": 0}
    for row in matrix:
        s = row.get("status")
        if isinstance(s, str) and s in status_counts:
            status_counts[s] += 1

    return {
        "ok": True,
        "traceability_matrix": matrix,
        "status_counts": status_counts,
    }


def apply_risk_gates(constraint_sheet: dict[str, Any], validation_report: dict[str, Any]) -> dict[str, Any]:
    """Assign risk class and emit notify-only gate guidance."""
    env = constraint_sheet.get("operating_envelope", {}) if isinstance(constraint_sheet, dict) else {}
    max_rpm = _as_float(env.get("max_rpm")) if isinstance(env, dict) else None
    gas_temp = _as_float(env.get("gas_temp_max_c")) if isinstance(env, dict) else None

    blockers = validation_report.get("blockers", []) if isinstance(validation_report, dict) else []
    warnings = validation_report.get("warnings", []) if isinstance(validation_report, dict) else []

    score = 0
    reasons: list[str] = []

    if max_rpm is not None:
        if max_rpm >= 120000:
            score += 3
            reasons.append("max_rpm >= 120000")
        elif max_rpm >= 80000:
            score += 2
            reasons.append("max_rpm >= 80000")
        elif max_rpm >= 50000:
            score += 1
            reasons.append("max_rpm >= 50000")

    if gas_temp is not None:
        if gas_temp >= 1000:
            score += 3
            reasons.append("gas_temp_max_c >= 1000")
        elif gas_temp >= 850:
            score += 2
            reasons.append("gas_temp_max_c >= 850")
        elif gas_temp >= 650:
            score += 1
            reasons.append("gas_temp_max_c >= 650")

    if blockers:
        score += 2
        reasons.append(f"{len(blockers)} validation blocker(s)")

    if warnings:
        score += 1
        reasons.append(f"{len(warnings)} validation warning(s)")

    if score <= 2:
        risk_class = "low"
    elif score <= 4:
        risk_class = "medium"
    elif score <= 6:
        risk_class = "high"
    else:
        risk_class = "critical"

    requires_signoff = risk_class in ("high", "critical")
    has_blockers = len(blockers) > 0
    has_warnings = len(warnings) > 0
    blocked = False

    gate_decision = "proceed"
    if has_blockers or has_warnings or requires_signoff:
        gate_decision = "proceed_with_notices"

    required_actions: list[str] = []
    if has_blockers:
        required_actions.append("Release risk is elevated: resolve validation blockers as soon as possible.")
    if requires_signoff:
        required_actions.append("Obtain human engineering signoff before release.")

    return {
        "ok": True,
        "risk_class": risk_class,
        "risk_score": score,
        "risk_reasons": reasons,
        "requires_signoff": requires_signoff,
        "blocked": blocked,
        "gate_decision": gate_decision,
        "required_actions": required_actions,
    }


def _find_local_notes() -> list[str]:
    """List existing research notes in me_knowledge/notes/."""
    notes_dir = Path(__file__).resolve().parent.parent / "me_knowledge" / "notes"
    if not notes_dir.is_dir():
        return []
    return sorted(p.name for p in notes_dir.glob("*.md"))


# Keep old name as alias for backward compatibility
_find_relevant_notes = _find_local_notes


def _search_knowledge(query: str) -> dict[str, Any] | None:
    """Search the knowledge store if available.

    Returns search results dict or ``None`` if the store is not configured.
    """
    try:
        from server.knowledge_store import get_knowledge_store
        store = get_knowledge_store()
        if store is None:
            return None
        results = store.search(query, top_k=3)
        return {
            "source": "lancedb",
            "result_count": len(results),
            "results": [
                {"content": r.content, "source": r.source, "score": r.score}
                for r in results
            ],
        }
    except Exception:
        return None


def _derive_search_query(constraints: dict[str, Any]) -> str:
    """Build a suggested knowledge search query from constraint domain tags."""
    parts: list[str] = []
    geom = constraints.get("geometry_interfaces", {})
    env = constraints.get("operating_envelope", {})
    mat = constraints.get("material", {})
    mfg = constraints.get("manufacturing", {})

    # Part type hints
    if geom.get("blade_count"):
        parts.append("turbine blade")
    if env.get("max_rpm"):
        parts.append("rotating")
    if mat.get("material_grade"):
        parts.append(str(mat["material_grade"]))
    if mfg.get("process"):
        parts.append(str(mfg["process"]))

    return " ".join(parts) if parts else "engineering design"


def run_design_loop(constraints: dict[str, Any]) -> dict[str, Any]:
    """Validate constraints, build traceability, apply risk gates (deterministic ME loop).

    The LLM constructs the constraint dict from its own engineering knowledge
    and research notes, then passes it here for deterministic validation.
    """
    validation = validate_constraint_sheet(constraints)

    requirements = constraints.get("requirements", [])
    if isinstance(requirements, list) and requirements:
        traceability = build_traceability_matrix(constraints, validation)
    else:
        traceability = {"ok": True, "traceability_matrix": [], "status_counts": {"pass": 0, "fail": 0, "tbd": 0, "waived": 0}}

    risk = apply_risk_gates(constraints, validation)
    notes_available = _find_local_notes()

    # Suggest a knowledge search query based on constraint domain
    search_query = _derive_search_query(constraints)
    knowledge_search_result = _search_knowledge(search_query)

    result: dict[str, Any] = {
        "ok": True,
        "validation": validation,
        "traceability": traceability,
        "risk_gates": risk,
        "notes_available": notes_available,
        "knowledge_search_hint": search_query,
        "summary": {
            "risk_class": risk["risk_class"],
            "gate_decision": risk["gate_decision"],
            "blocker_count": len(validation.get("blockers", [])),
            "warning_count": len(validation.get("warnings", [])),
        },
    }

    if knowledge_search_result is not None:
        result["knowledge_search"] = knowledge_search_result

    return result
