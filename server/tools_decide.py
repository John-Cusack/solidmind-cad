"""MCP tool wrappers for the Decide/Interpret helpers (decide.* tools)."""
from __future__ import annotations

from typing import Any

from server.analysis_models import AnalysisCheck, FieldResult, ReflectExpectations
from server.decide import (
    from_failure,
    interpret_compare_to_expectations,
)


def decide_from_failure(*, check: dict[str, Any]) -> dict[str, Any]:
    """Propose a concrete geometry fix for a failing AnalysisCheck."""
    try:
        chk = AnalysisCheck.from_dict(check)
    except (KeyError, ValueError) as exc:
        return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(exc)}}
    proposal = from_failure(chk)
    if proposal is None:
        return {
            "ok": True,
            "proposal": None,
            "message": "check has no typed failure_mode; nothing to decide",
        }
    return {"ok": True, "proposal": proposal.to_dict()}


def decide_interpret(
    *, result: dict[str, Any], expectations: dict[str, Any]
) -> dict[str, Any]:
    """Compare a FieldResult against Reflect-step expectations."""
    try:
        res = FieldResult.from_dict(result)
        exp = ReflectExpectations.from_dict(expectations)
    except (KeyError, ValueError) as exc:
        return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(exc)}}
    return {"ok": True, **interpret_compare_to_expectations(res, exp).to_dict()}
