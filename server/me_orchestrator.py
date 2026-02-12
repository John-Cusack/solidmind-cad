from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from server.me_registry import domain_tags_by_id, list_archetype_ids, load_archetype_card, load_constraint_template
from server.models import Finding, Severity


def _normalize_text(s: str) -> str:
    return " ".join(s.lower().replace("_", " ").replace("-", " ").split())


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


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


def _collect_tbd_fields(obj: Any, pointer_prefix: str = "") -> list[str]:
    tbd: list[str] = []
    if isinstance(obj, dict):
        for key in sorted(obj.keys()):
            token = key.replace("~", "~0").replace("/", "~1")
            child_ptr = f"{pointer_prefix}/{token}" if pointer_prefix else f"/{token}"
            tbd.extend(_collect_tbd_fields(obj[key], child_ptr))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            child_ptr = f"{pointer_prefix}/{idx}" if pointer_prefix else f"/{idx}"
            tbd.extend(_collect_tbd_fields(value, child_ptr))
    elif isinstance(obj, str) and obj.strip().upper() == "TBD":
        tbd.append(pointer_prefix or "/")
    return tbd


def route_request(request_text: str) -> dict[str, Any]:
    """Route a free-text request to the best matching archetype and ME domain tags."""
    normalized = _normalize_text(request_text)
    tags = domain_tags_by_id()

    best: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []

    for archetype_id in list_archetype_ids():
        card = load_archetype_card(archetype_id)
        routing = card.get("routing_signals", {}) if isinstance(card, dict) else {}
        keywords = routing.get("keywords", []) if isinstance(routing, dict) else []
        phrases = routing.get("phrases", []) if isinstance(routing, dict) else []

        matched_signals: list[str] = []
        score = 0.0

        for phrase in phrases:
            if isinstance(phrase, str) and _normalize_text(phrase) in normalized:
                matched_signals.append(phrase)
                score += 2.5

        for keyword in keywords:
            if isinstance(keyword, str) and _normalize_text(keyword) in normalized:
                matched_signals.append(keyword)
                score += 1.0

        for tag_id in card.get("domain_tags", []):
            tag = tags.get(str(tag_id), {})
            signals = tag.get("signals", {}) if isinstance(tag, dict) else {}
            kw_list = signals.get("keywords", []) if isinstance(signals, dict) else []
            for kw in kw_list:
                if isinstance(kw, str) and _normalize_text(kw) in normalized:
                    score += 0.25

        confidence = 0.0
        if keywords:
            confidence = min(0.99, score / (len(keywords) + 2.0))
        elif score > 0:
            confidence = min(0.99, score / 4.0)

        candidate = {
            "archetype_id": archetype_id,
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "matched_signals": sorted(set(matched_signals)),
            "domain_tags": sorted(card.get("domain_tags", [])),
        }
        candidates.append(candidate)
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    if best is None or best["score"] <= 0:
        return {
            "ok": True,
            "archetype_id": None,
            "confidence": 0.0,
            "domain_tags": [],
            "matched_signals": [],
            "assumptions": [
                "No known archetype matched the request text.",
                "Proceeding requires manual archetype selection or adding a new archetype card.",
            ],
            "candidates": sorted(candidates, key=lambda c: (-float(c["score"]), str(c["archetype_id"]))),
        }

    return {
        "ok": True,
        "archetype_id": best["archetype_id"],
        "confidence": best["confidence"],
        "domain_tags": best["domain_tags"],
        "matched_signals": best["matched_signals"],
        "assumptions": [
            "Routing used lexical signal matching against archetype card and domain tag metadata.",
            "Confidence reflects signal overlap, not physical feasibility.",
        ],
        "candidates": sorted(candidates, key=lambda c: (-float(c["score"]), str(c["archetype_id"]))),
    }


def instantiate_constraint_sheet(
    archetype_id: str,
    overrides: dict[str, Any] | None = None,
    assumptions: list[str] | None = None,
) -> dict[str, Any]:
    """Instantiate a constraint sheet from an archetype card and template."""
    card = load_archetype_card(archetype_id)
    template_id = card.get("constraint_template_id")
    if not isinstance(template_id, str) or not template_id:
        raise ValueError(f"Archetype {archetype_id!r} missing constraint_template_id")

    sheet = load_constraint_template(template_id)
    metadata = sheet.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Constraint template metadata must be a mapping")

    metadata["archetype_id"] = archetype_id
    metadata.setdefault("template_version", sheet.get("version", 1))

    if assumptions:
        existing = metadata.get("assumptions")
        if not isinstance(existing, list):
            existing = []
        metadata["assumptions"] = list(existing) + [str(a) for a in assumptions]

    if overrides:
        _deep_merge(sheet, overrides)

    tbd_fields = _collect_tbd_fields(sheet)

    return {
        "ok": True,
        "archetype_id": archetype_id,
        "constraint_sheet": sheet,
        "tbd_fields": sorted(set(tbd_fields)),
        "assumption_impact_notes": list(metadata.get("assumption_impact_notes", []) or []),
    }


def _result_entry(
    validator: str,
    status: str,
    message: str,
    measured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "validator": validator,
        "status": status,
        "message": message,
        "measured": measured or {},
    }


def validate_constraint_sheet(constraint_sheet: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic Tier 0/1 proxy validators against a constraint sheet."""
    validators = constraint_sheet.get("validators", [])
    if not isinstance(validators, list) or not validators:
        validators = [
            "mass_properties",
            "min_thickness_check",
            "sharp_edge_check",
            "symmetry_check",
            "centrifugal_stress_proxy",
            "manufacturability_heuristics",
        ]

    geom = constraint_sheet.get("geometry_interfaces", {}) if isinstance(constraint_sheet, dict) else {}
    mfg = constraint_sheet.get("manufacturing", {}) if isinstance(constraint_sheet, dict) else {}
    env = constraint_sheet.get("operating_envelope", {}) if isinstance(constraint_sheet, dict) else {}
    mat = constraint_sheet.get("material", {}) if isinstance(constraint_sheet, dict) else {}
    bal = constraint_sheet.get("balance_rotor", {}) if isinstance(constraint_sheet, dict) else {}

    min_blade = _as_float(geom.get("min_blade_thickness_mm")) if isinstance(geom, dict) else None
    min_feature = _as_float(mfg.get("min_feature_size_mm")) if isinstance(mfg, dict) else None
    min_fillet = _as_float(geom.get("min_fillet_radius_mm")) if isinstance(geom, dict) else None
    blade_count = _as_int(geom.get("blade_count")) if isinstance(geom, dict) else None

    exducer_diam_mm = _as_float(geom.get("exducer_diameter_mm")) if isinstance(geom, dict) else None
    hub_diam_mm = _as_float(geom.get("hub_diameter_mm")) if isinstance(geom, dict) else None
    max_rpm = _as_float(env.get("max_rpm")) if isinstance(env, dict) else None
    density = _as_float(mat.get("density_kg_m3")) if isinstance(mat, dict) else None
    yield_mpa = _as_float(mat.get("yield_strength_at_temp_mpa")) if isinstance(mat, dict) else None
    creep_mpa = _as_float(mat.get("creep_limit_at_temp_mpa")) if isinstance(mat, dict) else None

    process = str(mfg.get("process", "unknown")) if isinstance(mfg, dict) else "unknown"
    draft_angle = _as_float(mfg.get("draft_angle_min_deg")) if isinstance(mfg, dict) else None

    results: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []

    def push_finding(validator: str, status: str, message: str, priority: int = 500) -> None:
        if status == "fail":
            blockers.append(
                Finding(
                    rule_id=f"me.{validator}",
                    severity=Severity.BLOCK,
                    message=message,
                    priority=priority,
                ).to_dict()
            )
        elif status == "warn":
            warnings.append(
                Finding(
                    rule_id=f"me.{validator}",
                    severity=Severity.WARN,
                    message=message,
                    priority=priority,
                ).to_dict()
            )
        else:
            notes.append(
                Finding(
                    rule_id=f"me.{validator}",
                    severity=Severity.NOTE,
                    message=message,
                    priority=priority,
                ).to_dict()
            )

    if "min_thickness_check" in validators:
        if min_blade is None or min_feature is None:
            status = "warn"
            message = "Cannot fully evaluate min_thickness_check because min_blade_thickness or min_feature_size is TBD."
        elif min_blade < min_feature:
            status = "fail"
            message = (
                f"Minimum blade thickness {min_blade:.3f} mm is below process minimum feature size {min_feature:.3f} mm."
            )
        elif min_blade < (min_feature * 1.1):
            status = "warn"
            message = (
                f"Minimum blade thickness {min_blade:.3f} mm is close to the process floor {min_feature:.3f} mm."
            )
        else:
            status = "pass"
            message = "Minimum blade thickness is above process minimum feature size."

        results.append(_result_entry(
            "min_thickness_check",
            status,
            message,
            {"min_blade_thickness_mm": min_blade, "min_feature_size_mm": min_feature},
        ))
        push_finding("min_thickness_check", status, message, priority=910)

    if "sharp_edge_check" in validators:
        threshold = None if min_feature is None else max(0.5, min_feature * 0.5)
        if min_fillet is None or threshold is None:
            status = "warn"
            message = "Cannot fully evaluate sharp_edge_check because fillet radius or process feature floor is TBD."
        elif min_fillet < threshold:
            status = "fail"
            message = (
                f"Minimum fillet radius {min_fillet:.3f} mm is below threshold {threshold:.3f} mm for rotating service."
            )
        else:
            status = "pass"
            message = "Minimum fillet radius satisfies sharp-edge stress-riser threshold."

        results.append(_result_entry(
            "sharp_edge_check",
            status,
            message,
            {"min_fillet_radius_mm": min_fillet, "threshold_mm": threshold},
        ))
        push_finding("sharp_edge_check", status, message, priority=900)

    if "symmetry_check" in validators:
        symmetry_required = bool(bal.get("symmetry_required", False)) if isinstance(bal, dict) else False
        if not symmetry_required:
            status = "fail"
            message = "symmetry_required is false; rotating wheel should enforce polar symmetry."
        elif blade_count is None:
            status = "warn"
            message = "Cannot verify balance proxy because blade_count is TBD."
        elif blade_count < 8:
            status = "warn"
            message = f"Blade count {blade_count} is unusually low for a turbocharger turbine wheel."
        else:
            status = "pass"
            message = "Polar symmetry requirement is present and blade count is in expected range."

        results.append(_result_entry(
            "symmetry_check",
            status,
            message,
            {"symmetry_required": symmetry_required, "blade_count": blade_count},
        ))
        push_finding("symmetry_check", status, message, priority=870)

    if "centrifugal_stress_proxy" in validators:
        sigma_mpa = None
        safety_factor = None
        if max_rpm is None or density is None or exducer_diam_mm is None:
            status = "warn"
            message = "Cannot compute centrifugal stress proxy because rpm, density, or exducer diameter is TBD."
        else:
            omega = max_rpm * (2.0 * math.pi / 60.0)
            radius_m = exducer_diam_mm / 2000.0
            sigma_pa = density * (omega ** 2) * (radius_m ** 2) / 3.0
            sigma_mpa = sigma_pa / 1_000_000.0

            if yield_mpa is not None and sigma_mpa > 0:
                safety_factor = yield_mpa / sigma_mpa

            if safety_factor is None:
                status = "warn"
                message = (
                    f"Computed centrifugal stress proxy {sigma_mpa:.1f} MPa, but yield_strength_at_temp_mpa is missing."
                )
            elif safety_factor < 1.5:
                status = "fail"
                message = (
                    f"Centrifugal safety factor {safety_factor:.2f} is below minimum target 1.50 at {max_rpm:.0f} rpm."
                )
            elif safety_factor < 2.0:
                status = "warn"
                message = (
                    f"Centrifugal safety factor {safety_factor:.2f} is acceptable but low; consider adding margin."
                )
            else:
                status = "pass"
                message = f"Centrifugal stress proxy gives safety factor {safety_factor:.2f}."

            if creep_mpa is not None and sigma_mpa is not None and sigma_mpa > creep_mpa:
                status = "fail"
                message = (
                    f"Centrifugal stress proxy {sigma_mpa:.1f} MPa exceeds creep limit {creep_mpa:.1f} MPa."
                )

        results.append(_result_entry(
            "centrifugal_stress_proxy",
            status,
            message,
            {
                "max_rpm": max_rpm,
                "exducer_diameter_mm": exducer_diam_mm,
                "density_kg_m3": density,
                "sigma_mpa": sigma_mpa,
                "yield_strength_at_temp_mpa": yield_mpa,
                "creep_limit_at_temp_mpa": creep_mpa,
                "safety_factor": safety_factor,
            },
        ))
        push_finding("centrifugal_stress_proxy", status, message, priority=950)

    if "mass_properties" in validators:
        mass_kg = None
        polar_inertia_kg_m2 = None
        if density is None or exducer_diam_mm is None or hub_diam_mm is None or min_blade is None:
            status = "warn"
            message = "Cannot compute mass properties proxy because density/diameter/thickness inputs are incomplete."
        else:
            r_out = exducer_diam_mm / 2000.0
            r_in = max(0.0, hub_diam_mm / 2000.0)
            thickness_m = max(0.004, (min_blade / 1000.0) * 6.0)
            volume_m3 = math.pi * max(0.0, (r_out ** 2) - (r_in ** 2)) * thickness_m
            mass_kg = density * volume_m3
            polar_inertia_kg_m2 = 0.5 * mass_kg * ((r_out ** 2) + (r_in ** 2))
            if mass_kg <= 0:
                status = "fail"
                message = "Mass properties proxy produced non-positive mass; check geometry/material inputs."
            elif mass_kg > 2.0:
                status = "warn"
                message = f"Mass proxy {mass_kg:.3f} kg is high for a typical turbocharger wheel envelope."
            else:
                status = "pass"
                message = f"Mass proxy computed at {mass_kg:.3f} kg with polar inertia estimate available."

        results.append(_result_entry(
            "mass_properties",
            status,
            message,
            {"mass_kg": mass_kg, "polar_inertia_kg_m2": polar_inertia_kg_m2},
        ))
        push_finding("mass_properties", status, message, priority=740)

    if "manufacturability_heuristics" in validators:
        if process == "casting":
            if draft_angle is None:
                status = "warn"
                message = "Casting process selected but draft_angle_min_deg is TBD."
            elif draft_angle < 1.0:
                status = "fail"
                message = f"Draft angle {draft_angle:.2f} deg is below castability floor (1.0 deg)."
            elif draft_angle < 2.0:
                status = "warn"
                message = f"Draft angle {draft_angle:.2f} deg is manufacturable but tight for casting robustness."
            else:
                status = "pass"
                message = "Casting draft-angle assumption is within recommended range."
        elif process in ("5axis_machining", "machining"):
            if min_fillet is not None and min_fillet < 0.5:
                status = "warn"
                message = "Small internal radii may require non-standard tooling in machining route."
            else:
                status = "pass"
                message = "Machining route assumptions appear reasonable at proxy level."
        else:
            status = "warn"
            message = f"Manufacturability heuristics for process {process!r} are limited in this version."

        results.append(_result_entry(
            "manufacturability_heuristics",
            status,
            message,
            {"process": process, "draft_angle_min_deg": draft_angle},
        ))
        push_finding("manufacturability_heuristics", status, message, priority=780)

    blockers.sort(key=lambda f: (-int(f.get("priority", 0)), str(f.get("rule_id", ""))))
    warnings.sort(key=lambda f: (-int(f.get("priority", 0)), str(f.get("rule_id", ""))))
    notes.sort(key=lambda f: (-int(f.get("priority", 0)), str(f.get("rule_id", ""))))

    return {
        "ok": True,
        "validators_run": [str(v) for v in validators],
        "results": results,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "summary": (
            f"Validation complete: {len(blockers)} blockers, {len(warnings)} warnings, "
            f"{len(notes)} notes across {len(validators)} validators."
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


def suggest_high_value_questions(tbd_fields: list[str]) -> list[str]:
    """Return up to two targeted clarification questions from TBD fields."""
    questions: list[str] = []

    mapping = {
        "/operating_envelope/max_rpm": "What is the confirmed maximum operating RPM?",
        "/operating_envelope/gas_temp_max_c": "What is the maximum gas-side operating temperature in degC?",
        "/manufacturing/process": "Should manufacturing be assumed as casting, machining, or additive metal?",
        "/material/yield_strength_at_temp_mpa": "Do you have certified yield strength at operating temperature for the selected material?",
        "/balance_rotor/balance_grade_target": "What rotor balance grade target should be enforced?",
    }

    for field in sorted(set(tbd_fields)):
        q = mapping.get(field)
        if q and q not in questions:
            questions.append(q)
        if len(questions) >= 2:
            break

    return questions


def run_design_loop(
    request_text: str,
    overrides: dict[str, Any] | None = None,
    assumptions: list[str] | None = None,
) -> dict[str, Any]:
    """Route -> instantiate -> validate -> trace -> risk gates (deterministic ME loop)."""
    route = route_request(request_text)
    archetype_id = route.get("archetype_id")
    if not isinstance(archetype_id, str) or not archetype_id:
        return {
            "ok": False,
            "error": {
                "code": "NO_ARCHETYPE_MATCH",
                "message": "No archetype match found for request. Add or select an archetype card.",
            },
            "routing": route,
        }

    inst = instantiate_constraint_sheet(archetype_id, overrides=overrides, assumptions=assumptions)
    constraint_sheet = inst["constraint_sheet"]

    validation = validate_constraint_sheet(constraint_sheet)
    traceability = build_traceability_matrix(constraint_sheet, validation)
    risk = apply_risk_gates(constraint_sheet, validation)
    questions = suggest_high_value_questions(inst.get("tbd_fields", []))

    return {
        "ok": True,
        "routing": route,
        "constraint_instantiation": inst,
        "validation": validation,
        "traceability": traceability,
        "risk_gates": risk,
        "next_questions": questions,
        "summary": {
            "archetype_id": archetype_id,
            "risk_class": risk["risk_class"],
            "gate_decision": risk["gate_decision"],
            "blocker_count": len(validation.get("blockers", [])),
            "warning_count": len(validation.get("warnings", [])),
            "tbd_count": len(inst.get("tbd_fields", [])),
        },
    }
