"""MCP tool implementations for ME-grade design validation primitives."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from server.me_orchestrator import (
    apply_risk_gates,
    build_traceability_matrix,
    list_validators,
    run_design_loop,
    validate_constraint_sheet,
)

log = logging.getLogger("solidmind.tools_me")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def me_validate_constraints(constraint_sheet: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic proxy validators on a constraint dict."""
    if not isinstance(constraint_sheet, dict):
        return _error_result("INVALID_INPUT", "constraint_sheet must be an object")
    if _TOOL_LOG:
        log.info("CALL me_validate_constraints keys=%s", list(constraint_sheet.keys()))
    t0 = time.monotonic()
    result = validate_constraint_sheet(constraint_sheet)
    if _TOOL_LOG:
        findings = result.get("findings", [])
        log.info(
            "OK   me_validate_constraints %.3fs findings=%d", time.monotonic() - t0, len(findings)
        )
    return result


def me_build_traceability(
    constraint_sheet: dict[str, Any],
    validation_report: dict[str, Any],
) -> dict[str, Any]:
    """Build requirement-to-evidence traceability matrix."""
    if not isinstance(constraint_sheet, dict):
        return _error_result("INVALID_INPUT", "constraint_sheet must be an object")
    if not isinstance(validation_report, dict):
        return _error_result("INVALID_INPUT", "validation_report must be an object")
    if _TOOL_LOG:
        log.info("CALL me_build_traceability")
    t0 = time.monotonic()
    result = build_traceability_matrix(constraint_sheet, validation_report)
    if _TOOL_LOG:
        log.info("OK   me_build_traceability %.3fs", time.monotonic() - t0)
    return result


def me_apply_risk_gates(
    constraint_sheet: dict[str, Any],
    validation_report: dict[str, Any],
) -> dict[str, Any]:
    """Apply risk classification + signoff gates."""
    if not isinstance(constraint_sheet, dict):
        return _error_result("INVALID_INPUT", "constraint_sheet must be an object")
    if not isinstance(validation_report, dict):
        return _error_result("INVALID_INPUT", "validation_report must be an object")
    if _TOOL_LOG:
        log.info("CALL me_apply_risk_gates")
    t0 = time.monotonic()
    result = apply_risk_gates(constraint_sheet, validation_report)
    if _TOOL_LOG:
        log.info("OK   me_apply_risk_gates %.3fs", time.monotonic() - t0)
    return result


def me_design_loop(constraints: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic ME loop: validate -> trace -> risk gates.

    The LLM constructs the constraint dict from its own knowledge and
    research notes, then passes it here for deterministic checks.
    """
    if not isinstance(constraints, dict):
        return _error_result("INVALID_INPUT", "constraints must be an object")
    if _TOOL_LOG:
        log.info("CALL me_design_loop keys=%s", list(constraints.keys()))
    t0 = time.monotonic()
    result = run_design_loop(constraints)
    if _TOOL_LOG:
        log.info("OK   me_design_loop %.3fs", time.monotonic() - t0)
    return result


def me_list_validators() -> dict[str, Any]:
    """List available validators with metadata (fields read, thresholds, priority)."""
    if _TOOL_LOG:
        log.info("CALL me_list_validators")
    t0 = time.monotonic()
    result = {"ok": True, "validators": list_validators()}
    if _TOOL_LOG:
        log.info(
            "OK   me_list_validators %.3fs count=%d",
            time.monotonic() - t0,
            len(result["validators"]),
        )
    return result
