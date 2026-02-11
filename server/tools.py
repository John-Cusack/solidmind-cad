from __future__ import annotations

import hashlib
from typing import Any, Literal

from server.constants import COVERAGE_THRESHOLDS, HASH_ALGO, MATURITY_LEVELS, SUPPORTED_PROCESS, SUPPORTED_SPEC_MAJOR
from server.jcs import JcsError, canonicalize
from server.json_pointer import JsonPointerError, get as jp_get, remove_value as jp_remove, set_value as jp_set
from server.models import ConversationSignals, ToolError
from server.question_bank import Question, load_question_bank
from server.spec_draft import deep_copy_spec_draft, ensure_defaults, strip_internal_fields
from server.timeutil import next_deterministic_ts
from server.validation import _parse_semver_major, run_rules, validate_all


_ApplyOp = Literal["set", "append", "remove"]
_Source = Literal["user", "llm_proposal", "default", "import", "user_skip"]
_MISSING = object()


def _tool_error(code: str, message: str, field: str | None = None, details: dict | None = None) -> ToolError:
    return ToolError(code=code, message=message, field=field, details=details or {})


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

    if process != SUPPORTED_PROCESS:
        return {
            "schema_id": None,
            "question_bank_id": None,
            "coverage_threshold": None,
            "errors": [
                _tool_error(
                    "UNSUPPORTED_PROCESS",
                    f"Unsupported process: {process!r}",
                    field="/process",
                    details={"supported_process": SUPPORTED_PROCESS},
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
            "errors": [_tool_error("INVALID_JSON_POINTER", "path must be a non-empty string", field="/path").to_dict()],
            "warnings": [],
        }

    if source not in ("user", "llm_proposal", "default", "import", "user_skip"):
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [_tool_error("TYPE_ERROR", f"Invalid source: {source!r}", field="/source").to_dict()],
            "warnings": [],
        }

    if op in ("set", "append") and value is _MISSING:
        return {
            "spec_draft_updated": spec_draft,
            "applied": False,
            "errors": [_tool_error("TYPE_ERROR", f"value is required for op={op!r}", field="/value").to_dict()],
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


def spec_next_question(*, spec_draft: dict, conversation_signals: dict | None = None) -> dict[str, Any]:
    meta = spec_draft.get("meta") if isinstance(spec_draft, dict) else None
    maturity = meta.get("maturity_level") if isinstance(meta, dict) else None
    maturity_level = maturity if maturity in MATURITY_LEVELS else "L1"

    qb = load_question_bank(SUPPORTED_PROCESS)
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
    except JcsError as e:
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


def spec_export_brief(*, spec: dict) -> dict[str, Any]:
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
    lines.append(f"- Envelope: x={env.get('x','')}, y={env.get('y','')}, z={env.get('z','')}")
    lines.append("")
    lines.append("## Manufacturing")
    lines.append(f"- Material: {material.get('grade','')} ({material.get('family','')})")
    lines.append(f"- General tolerance: {tolerances.get('general','')}")
    lines.append(f"- Surface finish (Ra um): {sf.get('ra_um','')}")
    lines.append(f"- Coating: {sf.get('coating','')}")
    lines.append("")
    lines.append("## Inspection")
    lines.append(f"- Method: {insp.get('method','')}")
    lines.append("")
    lines.append("## Deliverables")
    lines.append(f"- CAD formats: {', '.join(deliv.get('cad_formats', []) or [])}")
    lines.append(f"- Drawing required: {deliv.get('drawing_required','')}")
    lines.append("")

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

    return {"markdown": "\n".join(lines).rstrip() + "\n"}


def spec_export_rfq_summary(*, spec: dict) -> dict[str, Any]:
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
    lines.append(f"- Envelope: x={env.get('x','')}, y={env.get('y','')}, z={env.get('z','')}")
    lines.append(f"- Material: {material.get('grade','')}")
    lines.append(f"- General tolerance: {tolerances.get('general','')}")
    lines.append(f"- Surface finish (Ra um): {sf.get('ra_um','')}")
    lines.append("")
    lines.append("## Files Requested")
    lines.append(f"- CAD: {', '.join(deliv.get('cad_formats', []) or [])}")
    lines.append(f"- Drawing required: {deliv.get('drawing_required','')}")
    lines.append("")
    return {"markdown": "\n".join(lines).rstrip() + "\n"}
