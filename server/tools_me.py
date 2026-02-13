"""MCP tool implementations for ME-grade design validation primitives."""
from __future__ import annotations

from typing import Any

from server.me_orchestrator import (
    apply_risk_gates,
    build_traceability_matrix,
    list_validators,
    run_design_loop,
    validate_constraint_sheet,
)


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def me_validate_constraints(constraint_sheet: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic proxy validators on a constraint dict."""
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


def me_design_loop(constraints: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic ME loop: validate -> trace -> risk gates.

    The LLM constructs the constraint dict from its own knowledge and
    research notes, then passes it here for deterministic checks.
    """
    if not isinstance(constraints, dict):
        return _error_result("INVALID_INPUT", "constraints must be an object")
    return run_design_loop(constraints)


def me_list_validators() -> dict[str, Any]:
    """List available validators with metadata (fields read, thresholds, priority)."""
    return {"ok": True, "validators": list_validators()}
