from __future__ import annotations

import hashlib
from typing import Any, Literal

from server.constants import (
    COVERAGE_THRESHOLDS,
    DEFAULT_PROCESS,
    HASH_ALGO,
    MATURITY_LEVELS,
    SUPPORTED_PROCESSES,
    SUPPORTED_SPEC_MAJOR,
)
from server.jcs import JcsError, canonicalize
from server.json_pointer import JsonPointerError
from server.json_pointer import get as jp_get
from server.json_pointer import remove_value as jp_remove
from server.json_pointer import set_value as jp_set
from server.models import ConversationSignals, ToolError
from server.question_bank import Question, load_question_bank
from server.spec_draft import deep_copy_spec_draft, ensure_defaults, strip_internal_fields
from server.timeutil import next_deterministic_ts
from server.validation import _parse_semver_major, run_rules, validate_all

_ApplyOp = Literal["set", "append", "remove"]
_Source = Literal["user", "llm_proposal", "default", "import", "user_skip"]
_MISSING = object()


def _tool_error(
    code: str, message: str, field: str | None = None, details: dict | None = None
) -> ToolError:
    return ToolError(code=code, message=message, field=field, details=details or {})


def _process_from_spec(spec_obj: dict[str, Any]) -> str:
    meta = spec_obj.get("meta") if isinstance(spec_obj, dict) else None
    process = meta.get("process") if isinstance(meta, dict) else None
    if process in SUPPORTED_PROCESSES:
        return process
    # Fallback keeps tools deterministic when a host sends an incomplete draft.
    return DEFAULT_PROCESS


def _is_non_blank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_non_default_print_3d_settings(settings: Any) -> bool:
    if not isinstance(settings, dict):
        return False

    if _is_non_blank_string(settings.get("notes")):
        return True
    if _is_non_blank_string(settings.get("support_policy")):
        return True

    for key in ("layer_height_mm", "nozzle_diameter_mm", "wall_count", "infill_percent"):
        value = settings.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return True
    return False


def _assess_design_path(spec_obj: dict[str, Any]) -> dict[str, Any]:
    """Classify design path as basic_box vs spec_driven for deterministic gating."""
    process = _process_from_spec(spec_obj)
    if process != "print_3d":
        return {
            "process": process,
            "design_path": "spec_driven",
            "requires_full_spec": True,
            "reason_codes": ["non_print_3d_process"],
            "reasons": [f"Process {process!r} uses full specification gating."],
        }

    meta = spec_obj.get("meta", {}) if isinstance(spec_obj, dict) else {}
    part = spec_obj.get("part", {}) if isinstance(spec_obj, dict) else {}
    mfg = spec_obj.get("manufacturing", {}) if isinstance(spec_obj, dict) else {}
    inspection = spec_obj.get("inspection", {}) if isinstance(spec_obj, dict) else {}

    maturity = meta.get("maturity_level", "L1") if isinstance(meta, dict) else "L1"

    blockers: list[tuple[str, str]] = []

    if maturity != "L1":
        blockers.append(
            (
                "maturity_not_l1",
                f"Maturity {maturity!r} requires full specification gating.",
            )
        )

    interfaces = part.get("interfaces", []) if isinstance(part, dict) else []
    if isinstance(interfaces, list):
        if interfaces:
            blockers.append(("interfaces_present", "Part interfaces are specified."))
    elif interfaces is not None:
        blockers.append(("interfaces_malformed", "Part interfaces field is not a list."))

    critical_features = part.get("critical_features", []) if isinstance(part, dict) else []
    if isinstance(critical_features, list):
        if critical_features:
            blockers.append(("critical_features_present", "Critical features are specified."))
    elif critical_features is not None:
        blockers.append(
            ("critical_features_malformed", "Part critical_features field is not a list.")
        )

    output_target = mfg.get("output_target", "vendor") if isinstance(mfg, dict) else "vendor"
    if output_target != "vendor":
        blockers.append(
            (
                "non_vendor_output_target",
                f"Output target {output_target!r} requires explicit print settings/spec detail.",
            )
        )

    tolerances = mfg.get("tolerances", {}) if isinstance(mfg, dict) else {}
    if isinstance(tolerances, dict):
        if _is_non_blank_string(tolerances.get("general")):
            blockers.append(
                ("tolerances_general_present", "General tolerance notes are specified.")
            )
        critical_tolerances = tolerances.get("critical", [])
        if isinstance(critical_tolerances, list):
            if critical_tolerances:
                blockers.append(
                    ("tolerances_critical_present", "Critical tolerance notes are specified.")
                )
        elif critical_tolerances is not None:
            blockers.append(
                ("tolerances_critical_malformed", "Critical tolerance field is not a list.")
            )
    elif tolerances is not None:
        blockers.append(("tolerances_malformed", "Tolerance section is malformed."))

    appearance = mfg.get("appearance", {}) if isinstance(mfg, dict) else {}
    if isinstance(appearance, dict):
        if _is_non_blank_string(appearance.get("color")):
            blockers.append(
                ("appearance_color_present", "Appearance color requirement is specified.")
            )
        if _is_non_blank_string(appearance.get("finish")):
            blockers.append(
                ("appearance_finish_present", "Appearance finish requirement is specified.")
            )
        if appearance.get("support_marks_ok") is False:
            blockers.append(
                ("appearance_support_marks_strict", "Support-mark restriction is specified.")
            )
        cosmetic_surfaces = appearance.get("cosmetic_surfaces", [])
        if isinstance(cosmetic_surfaces, list):
            if cosmetic_surfaces:
                blockers.append(
                    ("appearance_cosmetic_surfaces_present", "Cosmetic surfaces are specified.")
                )
        elif cosmetic_surfaces is not None:
            blockers.append(
                ("appearance_cosmetic_surfaces_malformed", "Cosmetic surfaces field is not a list.")
            )
    elif appearance is not None:
        blockers.append(("appearance_malformed", "Appearance section is malformed."))

    post_processing = mfg.get("post_processing", []) if isinstance(mfg, dict) else []
    if isinstance(post_processing, list):
        if post_processing:
            blockers.append(
                ("post_processing_present", "Post-processing requirements are specified.")
            )
    elif post_processing is not None:
        blockers.append(("post_processing_malformed", "Post-processing field is not a list."))

    in_house_settings = mfg.get("in_house_settings", {}) if isinstance(mfg, dict) else {}
    if _has_non_default_print_3d_settings(in_house_settings):
        blockers.append(("in_house_settings_present", "In-house print settings are specified."))

    if isinstance(inspection, dict):
        if _is_non_blank_string(inspection.get("method")):
            blockers.append(("inspection_method_present", "Inspection method is specified."))
        ctq = inspection.get("ctq", [])
        if isinstance(ctq, list):
            if ctq:
                blockers.append(("inspection_ctq_present", "CTQ requirements are specified."))
        elif ctq is not None:
            blockers.append(("inspection_ctq_malformed", "Inspection ctq field is not a list."))
        reqs = inspection.get("requirements", [])
        if isinstance(reqs, list):
            if reqs:
                blockers.append(
                    ("inspection_requirements_present", "Inspection requirements are specified.")
                )
        elif reqs is not None:
            blockers.append(
                (
                    "inspection_requirements_malformed",
                    "Inspection requirements field is not a list.",
                )
            )
    elif inspection is not None:
        blockers.append(("inspection_malformed", "Inspection section is malformed."))

    if blockers:
        return {
            "process": process,
            "design_path": "spec_driven",
            "requires_full_spec": True,
            "reason_codes": [code for code, _ in blockers],
            "reasons": [message for _, message in blockers],
        }

    return {
        "process": process,
        "design_path": "basic_box",
        "requires_full_spec": False,
        "reason_codes": [],
        "reasons": ["Eligible for basic envelope-box generation path."],
    }


def spec_assess_design_path(*, spec_draft: dict) -> dict[str, Any]:
    """Return deterministic design-path classification for downstream gating."""
    return _assess_design_path(spec_draft)


def spec_select_schema(*, process: str, maturity_level: str, spec_version: str) -> dict[str, Any]:
    major = _parse_semver_major(spec_version) if isinstance(spec_version, str) else None
    if major != SUPPORTED_SPEC_MAJOR:
        return {
            "schema_id": None,
            "question_bank_id": None,
            "coverage_threshold": None,
            "errors": [
                _tool_error(
                    "UNSUPPORTED_SPEC_VERSION",
                    f"Unsupported spec major version: {major}",
                    field="/spec_version",
                    details={"supported_major": SUPPORTED_SPEC_MAJOR},
                ).to_dict()
            ],
        }

    if process not in SUPPORTED_PROCESSES:
        return {
            "schema_id": None,
            "question_bank_id": None,
            "coverage_threshold": None,
            "errors": [
                _tool_error(
                    "UNSUPPORTED_PROCESS",
                    f"Unsupported process: {process!r}",
                    field="/process",
                    details={"supported_processes": list(SUPPORTED_PROCESSES)},
                ).to_dict()
            ],
        }

    if maturity_level not in MATURITY_LEVELS:
        return {
            "schema_id": None,
            "question_bank_id": None,
            "coverage_threshold": None,
            "errors": [
                _tool_error(
                    "UNSUPPORTED_MATURITY_LEVEL",
                    f"Unsupported maturity level: {maturity_level!r}",
                    field="/maturity_level",
                    details={"supported": list(MATURITY_LEVELS)},
                ).to_dict()
            ],
        }

    qb = load_question_bank(process)
    return {
        "schema_id": f"{process}_v1",
        "question_bank_id": qb.question_bank_id,
        "coverage_threshold": float(COVERAGE_THRESHOLDS[maturity_level]),
        "errors": [],
    }


def spec_apply_answer(
    *,
    spec_draft: dict,
    op: _ApplyOp,
    path: str,
    value: Any = _MISSING,
    question_id: str | None = None,
    source: _Source,
) -> dict[str, Any]:
    try:
        updated = deep_copy_spec_draft(spec_draft)
        ensure_defaults(updated)
    except Exception as e:
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [_tool_error("TYPE_ERROR", f"Invalid spec_draft: {e}").to_dict()],
            "warnings": [],
        }

    if op not in ("set", "append", "remove"):
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [_tool_error("INVALID_OP", f"Invalid op: {op!r}", field="/op").to_dict()],
            "warnings": [],
        }

    if not isinstance(path, str) or not path:
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [
                _tool_error(
                    "INVALID_JSON_POINTER", "path must be a non-empty string", field="/path"
                ).to_dict()
            ],
            "warnings": [],
        }

    if source not in ("user", "llm_proposal", "default", "import", "user_skip"):
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [
                _tool_error("TYPE_ERROR", f"Invalid source: {source!r}", field="/source").to_dict()
            ],
            "warnings": [],
        }

    if op in ("set", "append") and value is _MISSING:
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [
                _tool_error(
                    "TYPE_ERROR", f"value is required for op={op!r}", field="/value"
                ).to_dict()
            ],
            "warnings": [],
        }

    try:
        if op == "set":
            jp_set(updated, path, value, create_missing=True)
        elif op == "append":
            target = jp_get(updated, path)
            if not isinstance(target, list):
                raise JsonPointerError("append target is not a list")
            target.append(value)
        else:
            jp_remove(updated, path)
    except JsonPointerError as e:
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [_tool_error("INVALID_JSON_POINTER", str(e), field="/path").to_dict()],
            "warnings": [],
        }
    except Exception as e:
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [_tool_error("INTERNAL_ERROR", f"Failed to apply mutation: {e}").to_dict()],
            "warnings": [],
        }

    ts = next_deterministic_ts(updated)
    interview = updated.setdefault("_interview", {})
    answered = interview.setdefault("answered", {})
    skipped = interview.setdefault("skipped", {})

    if question_id:
        if source == "user_skip":
            if isinstance(skipped, dict):
                skipped[question_id] = ts
        else:
            if isinstance(answered, dict):
                answered[question_id] = ts
            if isinstance(skipped, dict) and question_id in skipped:
                del skipped[question_id]

    audit = updated.setdefault("_audit", [])
    if isinstance(audit, list):
        audit_entry: dict[str, Any] = {"ts": ts, "op": op, "path": path, "source": source}
        if question_id:
            audit_entry["question_id"] = question_id
        if op != "remove":
            audit_entry["value"] = value
        audit.append(audit_entry)

    return {
        "spec_draft_updated": updated,
        "applied": True,
        "errors": [],
        "warnings": [],
    }


def spec_validate(*, spec_draft: dict) -> dict[str, Any]:
    res = validate_all(spec_draft)
    return {
        "shape_valid": bool(res.shape_valid),
        "errors": [e.to_dict() for e in res.errors],
        "coverage_score": float(res.coverage_score),
        "coverage_threshold": float(res.coverage_threshold),
        "blockers": [b.to_dict() for b in res.blockers],
        "warnings": [w.to_dict() for w in res.warnings],
    }


def _parse_conversation_signals(raw: Any) -> ConversationSignals:
    if not isinstance(raw, dict):
        return ConversationSignals()

    user_expertise = raw.get("user_expertise", "unknown")
    language_preference = raw.get("language_preference", "auto")
    previous_question_id = raw.get("previous_question_id")
    allow_revisit_skipped = bool(raw.get("allow_revisit_skipped", False))

    if user_expertise not in ("novice", "intermediate", "expert", "unknown"):
        user_expertise = "unknown"
    if language_preference not in ("plain", "technical", "auto"):
        language_preference = "auto"
    if previous_question_id is not None and not isinstance(previous_question_id, str):
        previous_question_id = None

    return ConversationSignals(
        user_expertise=user_expertise,
        language_preference=language_preference,
        previous_question_id=previous_question_id,
        allow_revisit_skipped=allow_revisit_skipped,
    )


def _choose_question_text(q: Question, signals: ConversationSignals) -> str:
    pref = signals.language_preference
    if pref == "plain":
        return q.plain
    if pref == "technical":
        return q.technical

    # auto
    if signals.user_expertise == "novice":
        return q.plain
    if signals.user_expertise == "expert":
        return q.technical
    return q.plain


def spec_next_question(
    *, spec_draft: dict, conversation_signals: dict | None = None
) -> dict[str, Any]:
    meta = spec_draft.get("meta") if isinstance(spec_draft, dict) else None
    maturity = meta.get("maturity_level") if isinstance(meta, dict) else None
    maturity_level = maturity if maturity in MATURITY_LEVELS else "L1"
    process = _process_from_spec(spec_draft)

    qb = load_question_bank(process)
    q_by_id = qb.by_id()

    interview = spec_draft.get("_interview", {}) if isinstance(spec_draft, dict) else {}
    answered = interview.get("answered", {}) if isinstance(interview, dict) else {}
    skipped = interview.get("skipped", {}) if isinstance(interview, dict) else {}
    answered_ids = set(answered.keys()) if isinstance(answered, dict) else set()
    skipped_ids = set(skipped.keys()) if isinstance(skipped, dict) else set()

    signals = _parse_conversation_signals(conversation_signals)
    include_skipped = bool(signals.allow_revisit_skipped)

    blockers, _warnings = run_rules(spec_draft)
    for b in blockers:
        if not b.question_id:
            continue
        if b.question_id in answered_ids:
            continue
        if (not include_skipped) and (b.question_id in skipped_ids):
            continue
        q = q_by_id.get(b.question_id)
        if q is None:
            return {
                "question_id": b.question_id,
                "question_text": b.message,
                "field_paths": [b.field] if b.field else [],
                "rationale": f"Blocker: {b.rule_id}",
            }
        return {
            "question_id": q.id,
            "question_text": _choose_question_text(q, signals),
            "field_paths": list(q.field_paths),
            "rationale": f"Blocker: {b.rule_id}",
        }

    required_unanswered: list[Question] = []
    for q in qb.questions:
        if not q.required_for(maturity_level):
            continue
        if q.id in answered_ids:
            continue
        if (not include_skipped) and (q.id in skipped_ids):
            continue
        required_unanswered.append(q)

    required_unanswered.sort(key=lambda q: (-int(q.priority), q.id))
    if required_unanswered:
        q = required_unanswered[0]
        return {
            "question_id": q.id,
            "question_text": _choose_question_text(q, signals),
            "field_paths": list(q.field_paths),
            "rationale": f"Required at {maturity_level}",
        }

    weighted: list[tuple[float, int, str, Question]] = []
    for q in qb.questions:
        if q.id in answered_ids:
            continue
        if (not include_skipped) and (q.id in skipped_ids):
            continue
        w = q.weight_for(maturity_level)
        if w <= 0:
            continue
        weighted.append((w, q.priority, q.id, q))

    weighted.sort(key=lambda t: (-t[0], -int(t[1]), t[2]))
    if weighted:
        q = weighted[0][3]
        return {
            "question_id": q.id,
            "question_text": _choose_question_text(q, signals),
            "field_paths": list(q.field_paths),
            "rationale": f"Highest weight unanswered at {maturity_level}",
        }

    return {
        "question_id": None,
        "question_text": "",
        "field_paths": [],
        "rationale": "No eligible questions remaining (answered or skipped).",
    }


def spec_finalize(*, spec_draft: dict) -> dict[str, Any]:
    res = validate_all(spec_draft)
    cleaned = strip_internal_fields(spec_draft)
    meta = cleaned.setdefault("meta", {})
    if isinstance(meta, dict):
        meta["coverage_score"] = float(res.coverage_score)

    try:
        canonical = canonicalize(cleaned).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
    except JcsError:
        # Fall back to a stable-but-non-JCS hash if canonicalization fails.
        digest = hashlib.sha256(repr(cleaned).encode("utf-8")).hexdigest()

    changelog = spec_draft.get("_audit", [])
    if not isinstance(changelog, list):
        changelog = []

    provenance: dict[str, dict[str, Any]] = {}
    for entry in changelog:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        provenance[path] = {
            "ts": entry.get("ts"),
            "source": entry.get("source"),
            "question_id": entry.get("question_id"),
            "op": entry.get("op"),
        }

    return {
        "spec": cleaned,
        "hash": digest,
        "hash_algo": HASH_ALGO,
        "changelog": changelog,
        "provenance": provenance,
    }


def _append_common_tail(lines: list[str], spec: dict) -> None:
    open_q = spec.get("open_questions", []) if isinstance(spec, dict) else []
    assumptions = spec.get("assumptions", []) if isinstance(spec, dict) else []
    if open_q:
        lines.append("## Open Questions")
        for q in open_q:
            lines.append(f"- {q}")
        lines.append("")
    if assumptions:
        lines.append("## Assumptions")
        for a in assumptions:
            lines.append(f"- {a}")
        lines.append("")


def _export_brief_cnc(spec: dict) -> dict[str, Any]:
    meta = spec.get("meta", {}) if isinstance(spec, dict) else {}
    part = spec.get("part", {}) if isinstance(spec, dict) else {}
    mfg = spec.get("manufacturing", {}) if isinstance(spec, dict) else {}
    insp = spec.get("inspection", {}) if isinstance(spec, dict) else {}
    deliv = spec.get("deliverables", {}) if isinstance(spec, dict) else {}

    env = part.get("envelope", {}) if isinstance(part, dict) else {}
    material = mfg.get("material", {}) if isinstance(mfg, dict) else {}
    tolerances = mfg.get("tolerances", {}) if isinstance(mfg, dict) else {}
    sf = mfg.get("surface_finish", {}) if isinstance(mfg, dict) else {}

    lines: list[str] = []
    lines.append("# Design Brief")
    lines.append("")
    lines.append(f"- Process: {meta.get('process', '')}")
    lines.append(f"- Maturity: {meta.get('maturity_level', '')}")
    lines.append(f"- Units: {meta.get('units', '')}")
    lines.append(f"- Quantity: {part.get('quantity', '')}")
    lines.append("")
    lines.append("## Part")
    lines.append(f"- Name: {part.get('name', '')}")
    lines.append(f"- Description: {part.get('description', '')}")
    lines.append(f"- Envelope: x={env.get('x', '')}, y={env.get('y', '')}, z={env.get('z', '')}")
    lines.append("")
    lines.append("## Manufacturing")
    lines.append(f"- Material: {material.get('grade', '')} ({material.get('family', '')})")
    lines.append(f"- General tolerance: {tolerances.get('general', '')}")
    lines.append(f"- Surface finish (Ra um): {sf.get('ra_um', '')}")
    lines.append(f"- Coating: {sf.get('coating', '')}")
    lines.append("")
    lines.append("## Inspection")
    lines.append(f"- Method: {insp.get('method', '')}")
    lines.append("")
    lines.append("## Deliverables")
    lines.append(f"- CAD formats: {', '.join(deliv.get('cad_formats', []) or [])}")
    lines.append(f"- Drawing required: {deliv.get('drawing_required', '')}")
    lines.append("")
    _append_common_tail(lines, spec)
    return {"markdown": "\n".join(lines).rstrip() + "\n"}


def _export_brief_print_3d(spec: dict) -> dict[str, Any]:
    meta = spec.get("meta", {}) if isinstance(spec, dict) else {}
    part = spec.get("part", {}) if isinstance(spec, dict) else {}
    mfg = spec.get("manufacturing", {}) if isinstance(spec, dict) else {}
    insp = spec.get("inspection", {}) if isinstance(spec, dict) else {}
    deliv = spec.get("deliverables", {}) if isinstance(spec, dict) else {}

    env = part.get("envelope", {}) if isinstance(part, dict) else {}
    material = mfg.get("material", {}) if isinstance(mfg, dict) else {}
    tolerances = mfg.get("tolerances", {}) if isinstance(mfg, dict) else {}
    appearance = mfg.get("appearance", {}) if isinstance(mfg, dict) else {}
    in_house_settings = mfg.get("in_house_settings", {}) if isinstance(mfg, dict) else {}

    lines: list[str] = []
    lines.append("# Design Brief")
    lines.append("")
    lines.append(f"- Process: {meta.get('process', '')}")
    lines.append(f"- Maturity: {meta.get('maturity_level', '')}")
    lines.append(f"- Units: {meta.get('units', '')}")
    lines.append(f"- Quantity: {part.get('quantity', '')}")
    lines.append("")
    lines.append("## Part")
    lines.append(f"- Name: {part.get('name', '')}")
    lines.append(f"- Description: {part.get('description', '')}")
    lines.append(f"- Envelope: x={env.get('x', '')}, y={env.get('y', '')}, z={env.get('z', '')}")
    lines.append("")
    lines.append("## Manufacturing")
    lines.append(f"- Technology: {mfg.get('technology', '')}")
    lines.append(f"- Output target: {mfg.get('output_target', '')}")
    lines.append(f"- Material: {material.get('grade', '')} ({material.get('family', '')})")
    lines.append(f"- Fit tolerance notes: {tolerances.get('general', '')}")
    lines.append(f"- Appearance color: {appearance.get('color', '')}")
    lines.append(f"- Appearance finish: {appearance.get('finish', '')}")
    lines.append(f"- Support marks acceptable: {appearance.get('support_marks_ok', '')}")
    cosmetic_surfaces = (
        appearance.get("cosmetic_surfaces", []) if isinstance(appearance, dict) else []
    )
    lines.append(f"- Cosmetic surfaces: {', '.join(cosmetic_surfaces or [])}")
    post_processing = mfg.get("post_processing", []) if isinstance(mfg, dict) else []
    lines.append(f"- Post processing: {', '.join(post_processing or [])}")
    lines.append("")
    if mfg.get("output_target") in ("in_house", "both"):
        lines.append("## In-House Print Settings")
        lines.append(f"- Notes: {in_house_settings.get('notes', '')}")
        lines.append(f"- Layer height (mm): {in_house_settings.get('layer_height_mm', '')}")
        lines.append(f"- Nozzle diameter (mm): {in_house_settings.get('nozzle_diameter_mm', '')}")
        lines.append(f"- Wall count: {in_house_settings.get('wall_count', '')}")
        lines.append(f"- Infill (%): {in_house_settings.get('infill_percent', '')}")
        lines.append(f"- Support policy: {in_house_settings.get('support_policy', '')}")
        lines.append("")
    lines.append("## Inspection")
    lines.append(f"- Method: {insp.get('method', '')}")
    lines.append("")
    lines.append("## Deliverables")
    lines.append(f"- CAD formats: {', '.join(deliv.get('cad_formats', []) or [])}")
    lines.append(f"- Drawing required: {deliv.get('drawing_required', '')}")
    lines.append("")
    _append_common_tail(lines, spec)
    return {"markdown": "\n".join(lines).rstrip() + "\n"}


def spec_export_brief(*, spec: dict) -> dict[str, Any]:
    process = _process_from_spec(spec)
    if process == "cnc":
        return _export_brief_cnc(spec)
    return _export_brief_print_3d(spec)


def _export_rfq_summary_cnc(spec: dict) -> dict[str, Any]:
    meta = spec.get("meta", {}) if isinstance(spec, dict) else {}
    part = spec.get("part", {}) if isinstance(spec, dict) else {}
    mfg = spec.get("manufacturing", {}) if isinstance(spec, dict) else {}
    deliv = spec.get("deliverables", {}) if isinstance(spec, dict) else {}

    env = part.get("envelope", {}) if isinstance(part, dict) else {}
    material = mfg.get("material", {}) if isinstance(mfg, dict) else {}
    tolerances = mfg.get("tolerances", {}) if isinstance(mfg, dict) else {}
    sf = mfg.get("surface_finish", {}) if isinstance(mfg, dict) else {}

    lines: list[str] = []
    lines.append("# RFQ Summary (CNC)")
    lines.append("")
    lines.append(f"- Quantity: {part.get('quantity', '')}")
    lines.append(f"- Units: {meta.get('units', '')}")
    lines.append(f"- Envelope: x={env.get('x', '')}, y={env.get('y', '')}, z={env.get('z', '')}")
    lines.append(f"- Material: {material.get('grade', '')}")
    lines.append(f"- General tolerance: {tolerances.get('general', '')}")
    lines.append(f"- Surface finish (Ra um): {sf.get('ra_um', '')}")
    lines.append("")
    lines.append("## Files Requested")
    lines.append(f"- CAD: {', '.join(deliv.get('cad_formats', []) or [])}")
    lines.append(f"- Drawing required: {deliv.get('drawing_required', '')}")
    lines.append("")
    return {"markdown": "\n".join(lines).rstrip() + "\n"}


def _export_rfq_summary_print_3d(spec: dict) -> dict[str, Any]:
    meta = spec.get("meta", {}) if isinstance(spec, dict) else {}
    part = spec.get("part", {}) if isinstance(spec, dict) else {}
    mfg = spec.get("manufacturing", {}) if isinstance(spec, dict) else {}
    deliv = spec.get("deliverables", {}) if isinstance(spec, dict) else {}

    env = part.get("envelope", {}) if isinstance(part, dict) else {}
    material = mfg.get("material", {}) if isinstance(mfg, dict) else {}
    tolerances = mfg.get("tolerances", {}) if isinstance(mfg, dict) else {}
    appearance = mfg.get("appearance", {}) if isinstance(mfg, dict) else {}
    in_house_settings = mfg.get("in_house_settings", {}) if isinstance(mfg, dict) else {}

    lines: list[str] = []
    lines.append("# RFQ Summary (3D Printing)")
    lines.append("")
    lines.append(f"- Quantity: {part.get('quantity', '')}")
    lines.append(f"- Units: {meta.get('units', '')}")
    lines.append(f"- Envelope: x={env.get('x', '')}, y={env.get('y', '')}, z={env.get('z', '')}")
    lines.append(f"- Technology: {mfg.get('technology', '')}")
    lines.append(f"- Output target: {mfg.get('output_target', '')}")
    lines.append(f"- Material: {material.get('grade', '')} ({material.get('family', '')})")
    lines.append(f"- Fit tolerance notes: {tolerances.get('general', '')}")
    lines.append(f"- Appearance color: {appearance.get('color', '')}")
    lines.append(f"- Appearance finish: {appearance.get('finish', '')}")
    lines.append(f"- Support marks acceptable: {appearance.get('support_marks_ok', '')}")
    lines.append(f"- Post processing: {', '.join(mfg.get('post_processing', []) or [])}")
    lines.append("")
    if mfg.get("output_target") in ("in_house", "both"):
        lines.append("## In-House Settings")
        lines.append(f"- Notes: {in_house_settings.get('notes', '')}")
        lines.append(f"- Layer height (mm): {in_house_settings.get('layer_height_mm', '')}")
        lines.append(f"- Nozzle diameter (mm): {in_house_settings.get('nozzle_diameter_mm', '')}")
        lines.append(f"- Wall count: {in_house_settings.get('wall_count', '')}")
        lines.append(f"- Infill (%): {in_house_settings.get('infill_percent', '')}")
        lines.append(f"- Support policy: {in_house_settings.get('support_policy', '')}")
        lines.append("")
    lines.append("## Files Requested")
    lines.append(f"- CAD: {', '.join(deliv.get('cad_formats', []) or [])}")
    lines.append(f"- Drawing required: {deliv.get('drawing_required', '')}")
    lines.append("")
    return {"markdown": "\n".join(lines).rstrip() + "\n"}


def spec_export_rfq_summary(*, spec: dict) -> dict[str, Any]:
    process = _process_from_spec(spec)
    if process == "cnc":
        return _export_rfq_summary_cnc(spec)
    return _export_rfq_summary_print_3d(spec)


def spec_generate_cad(
    *,
    spec: dict[str, Any],
    output_format: str,
    output_path: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate CAD geometry from a finalized spec."""
    opts = options or {}
    errors: list[dict[str, Any]] = []
    precondition_warnings: list[str] = []

    # Precondition 1: must be finalized (no internal fields)
    if "_interview" in spec or "_audit" in spec:
        errors.append(
            _tool_error(
                "NOT_FINALIZED",
                "Spec contains internal fields (_interview/_audit). Run spec.finalize first.",
            ).to_dict()
        )
        return {
            "file_path": None,
            "cad_data": None,
            "metadata": {},
            "warnings": [],
            "errors": errors,
        }

    meta = spec.get("meta", {}) if isinstance(spec, dict) else {}
    maturity = meta.get("maturity_level", "L1")
    coverage = float(meta.get("coverage_score", 0.0))
    threshold = float(COVERAGE_THRESHOLDS.get(maturity, 1.0))
    design_gate = _assess_design_path(spec)

    # Precondition 2: coverage threshold is notify-only (never blocks generation).
    if coverage < threshold:
        mode = "spec_driven" if bool(design_gate.get("requires_full_spec", True)) else "basic_box"
        reasons = list(design_gate.get("reason_codes", []))
        reason_text = f" reason_codes={','.join(reasons)}" if reasons else ""
        precondition_warnings.append(
            f"Coverage {coverage:.0%} is below {maturity} threshold {threshold:.0%}; "
            f"proceeding in notify-only mode on design_path={mode}.{reason_text}"
        )

    # Precondition 3: optional hash verification
    expected_hash = opts.get("spec_hash")
    if expected_hash is not None:
        try:
            canonical = canonicalize(spec).encode("utf-8")
            actual_hash = hashlib.sha256(canonical).hexdigest()
        except JcsError:
            actual_hash = hashlib.sha256(repr(spec).encode("utf-8")).hexdigest()

        if actual_hash != expected_hash:
            errors.append(
                _tool_error(
                    "HASH_MISMATCH",
                    "Spec hash does not match expected value.",
                    details={"expected": expected_hash, "actual": actual_hash},
                ).to_dict()
            )
            return {
                "file_path": None,
                "cad_data": None,
                "metadata": {},
                "warnings": precondition_warnings,
                "errors": errors,
            }

    return _generate_cad_generic_v1(spec, precondition_warnings, design_gate, opts)


def _generate_cad_generic_v1(
    spec: dict[str, Any],
    precondition_warnings: list[str],
    design_gate: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Generate CAD using the generic_v1 geometry engine pipeline."""
    from server.geometry_compiler_freecad import FreeCADCompiler
    from server.geometry_executor import Executor, compute_execution_trace_hash
    from server.geometry_planning import plan_geometry
    from server.geometry_repair import recommend_repairs
    from server.geometry_verify import VerificationEngine

    errors: list[dict[str, Any]] = []
    planning_opts = {
        "planning_mode": str(options.get("planning_mode", "legacy")),
        "strict_mode": bool(options.get("strict_mode", False)),
    }
    if isinstance(options.get("question_budget_override"), int):
        planning_opts["question_budget_override"] = int(options["question_budget_override"])

    # Step 1: Plan geometry (spec -> GIR -> EIR)
    try:
        plan_result = plan_geometry(spec, options=planning_opts)
    except Exception as e:
        errors.append(_tool_error("PLANNING_ERROR", f"Geometry planning failed: {e}").to_dict())
        return {
            "file_path": None,
            "cad_data": None,
            "metadata": {"engine_mode": "generic_v1"},
            "warnings": precondition_warnings,
            "errors": errors,
            "notices": [],
        }

    gir_dict = plan_result["gir"]
    eir_dict = plan_result["eir"]
    plan_notices = plan_result.get("notices", [])
    plan_metadata = plan_result.get("metadata", {})
    planning_plan = plan_result.get("planning_plan")
    planning_plan_hash = plan_result.get("planning_plan_hash")
    policy_key = plan_result.get("policy_key")

    # Step 2: Compile EIR operations via FreeCAD compiler
    from server.geometry_ir import EIRBuilder, Invariant

    eir_builder = EIRBuilder()
    for op_data in eir_dict.get("operations", []):
        invariants = [
            Invariant(
                type=inv.get("type", ""),
                threshold=inv.get("threshold"),
                scope=inv.get("scope"),
            )
            for inv in op_data.get("invariants", [])
        ]
        eir_builder.add_operation(
            op_type=op_data["op_type"],
            inputs=op_data.get("inputs", {}),
            depends_on=op_data.get("depends_on", []),
            invariants=invariants,
            feature_provenance_id=op_data.get("feature_provenance_id"),
            phase_id=op_data.get("phase_id"),
            reference_support_type=op_data.get("reference_support_type"),
            topology_sensitive=op_data.get("topology_sensitive"),
        )
    eir = eir_builder.build()

    compiler = FreeCADCompiler()
    compiler_result = compiler.compile_eir(eir)

    # Step 3: Execute (mock for now — no live FreeCAD client in this path)
    executor = Executor()
    compiled_ops = compiler_result.ops or []
    execution_trace = executor.execute_plan(compiled_ops, backend="mock")
    trace_hash = compute_execution_trace_hash(execution_trace)
    checkpoint_summary = execution_trace.metadata.get("checkpoint_summary", {})

    # Step 4: Verify
    # Reconstruct GIR object for verification
    from server.geometry_ir import GIRBuilder, Quantity

    gir_builder = GIRBuilder()
    gir_builder.add_global_frame()
    for feat in gir_dict.get("features", []):
        if feat.get("type") == "primitive":
            dims = {
                k: Quantity(value=v["value"], unit=v.get("unit", "mm"))
                for k, v in feat.get("dimensions", {}).items()
            }
            gir_builder.add_primitive(feat.get("primitive_type", "box"), dims)
    gir_obj = gir_builder.build()

    verifier = VerificationEngine()
    verification_report = verifier.verify(execution_trace, gir_obj, spec)
    repair_recommendations = recommend_repairs(
        execution_trace=execution_trace,
        verification_report=verification_report,
        planning_plan=planning_plan if isinstance(planning_plan, dict) else None,
    )

    # Build Section 4.8 response
    notices = (
        plan_notices
        + [
            {"code": n.code, "severity": n.severity, "message": n.message, "context": n.context}
            for n in verification_report.notices
        ]
        + [
            {"code": n.code, "severity": n.severity, "message": n.message, "context": n.context}
            for n in compiler_result.notices
        ]
        + [rec.to_notice() for rec in repair_recommendations]
    )

    metadata = {
        "engine_mode": "generic_v1",
        "design_path": design_gate.get("design_path"),
        "coverage_status": "complete",
        "risk_class": design_gate.get("risk_class", "standard"),
        "gir_hash": plan_metadata.get("gir_hash"),
        "eir_hash": plan_metadata.get("eir_hash"),
        "execution_trace_hash": trace_hash,
        "verification_report_hash": verification_report.report_hash,
        "strategy": plan_metadata.get("strategy"),
        "compiler_status": compiler_result.status,
        "verification_passed": verification_report.passed,
        "planning_plan_hash": planning_plan_hash,
        "policy_key": policy_key,
        "checkpoint_summary": checkpoint_summary,
        "repair_recommendations_present": bool(repair_recommendations),
    }

    return {
        "file_path": None,
        "cad_data": None,
        "metadata": metadata,
        "warnings": precondition_warnings,
        "errors": errors,
        "notices": notices,
        "gir": gir_dict,
        "eir": eir_dict,
    }


def spec_plan_geometry(
    *, spec: dict[str, Any], options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read-only observability: returns GIR/EIR + notices without executing."""
    from server.geometry_planning import plan_geometry

    return plan_geometry(spec, options=options)
