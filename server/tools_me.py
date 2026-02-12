"""MCP tool implementations for ME-grade design orchestration primitives."""
from __future__ import annotations

from typing import Any

from server.me_orchestrator import (
    apply_risk_gates,
    build_traceability_matrix,
    instantiate_constraint_sheet,
    route_request,
    run_design_loop,
    validate_constraint_sheet,
)
from server.me_registry import (
    domain_tags_by_id,
    list_archetype_ids,
    list_domain_tags,
    load_archetype_card,
    load_standards_sources,
)


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def me_list_domain_tags() -> dict[str, Any]:
    """List all ME domain tags in the controlled vocabulary."""
    tags = list_domain_tags()
    return {"ok": True, "count": len(tags), "domain_tags": tags}


def me_list_archetypes() -> dict[str, Any]:
    """List available archetype IDs."""
    ids = list_archetype_ids()
    return {"ok": True, "count": len(ids), "archetype_ids": ids}


def me_get_archetype_card(archetype_id: str) -> dict[str, Any]:
    """Load a specific archetype card by id."""
    try:
        card = load_archetype_card(archetype_id)
    except KeyError:
        return _error_result("UNKNOWN_ARCHETYPE", f"Unknown archetype_id: {archetype_id}")
    return {"ok": True, "archetype_card": card}


def me_route_request(request_text: str) -> dict[str, Any]:
    """Route user request text to the best matching archetype + domain tags."""
    if not isinstance(request_text, str) or not request_text.strip():
        return _error_result("INVALID_INPUT", "request_text must be a non-empty string")
    return route_request(request_text)


def me_instantiate_constraint_sheet(
    archetype_id: str,
    overrides: dict[str, Any] | None = None,
    assumptions: list[str] | None = None,
) -> dict[str, Any]:
    """Instantiate archetype constraint sheet with optional overrides/assumptions."""
    if not isinstance(archetype_id, str) or not archetype_id:
        return _error_result("INVALID_INPUT", "archetype_id must be a non-empty string")
    if overrides is not None and not isinstance(overrides, dict):
        return _error_result("INVALID_INPUT", "overrides must be an object")
    if assumptions is not None and not isinstance(assumptions, list):
        return _error_result("INVALID_INPUT", "assumptions must be an array of strings")

    try:
        return instantiate_constraint_sheet(archetype_id, overrides=overrides, assumptions=assumptions)
    except KeyError:
        return _error_result("UNKNOWN_ARCHETYPE", f"Unknown archetype_id: {archetype_id}")
    except Exception as e:
        return _error_result("INSTANTIATE_FAILED", str(e))


def me_validate_constraint_sheet(constraint_sheet: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic proxy validators on a constraint sheet."""
    if not isinstance(constraint_sheet, dict):
        return _error_result("INVALID_INPUT", "constraint_sheet must be an object")
    return validate_constraint_sheet(constraint_sheet)


def me_build_traceability(
    constraint_sheet: dict[str, Any],
    validation_report: dict[str, Any],
) -> dict[str, Any]:
    """Build requirement-to-evidence traceability matrix."""
    if not isinstance(constraint_sheet, dict):
        return _error_result("INVALID_INPUT", "constraint_sheet must be an object")
    if not isinstance(validation_report, dict):
        return _error_result("INVALID_INPUT", "validation_report must be an object")
    return build_traceability_matrix(constraint_sheet, validation_report)


def me_apply_risk_gates(
    constraint_sheet: dict[str, Any],
    validation_report: dict[str, Any],
) -> dict[str, Any]:
    """Apply risk classification + signoff gates."""
    if not isinstance(constraint_sheet, dict):
        return _error_result("INVALID_INPUT", "constraint_sheet must be an object")
    if not isinstance(validation_report, dict):
        return _error_result("INVALID_INPUT", "validation_report must be an object")
    return apply_risk_gates(constraint_sheet, validation_report)


def me_design_loop(
    request_text: str,
    overrides: dict[str, Any] | None = None,
    assumptions: list[str] | None = None,
) -> dict[str, Any]:
    """Run full deterministic ME loop: route -> constrain -> validate -> trace -> gates."""
    if not isinstance(request_text, str) or not request_text.strip():
        return _error_result("INVALID_INPUT", "request_text must be a non-empty string")
    if overrides is not None and not isinstance(overrides, dict):
        return _error_result("INVALID_INPUT", "overrides must be an object")
    if assumptions is not None and not isinstance(assumptions, list):
        return _error_result("INVALID_INPUT", "assumptions must be an array of strings")
    return run_design_loop(request_text, overrides=overrides, assumptions=assumptions)


def me_get_knowledge_policy() -> dict[str, Any]:
    """Return standards/material source policy and authority ordering."""
    policy = load_standards_sources()
    return {"ok": True, "knowledge_policy": policy, "domain_tag_index": list(domain_tags_by_id())}
