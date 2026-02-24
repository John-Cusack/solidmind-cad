"""MCP tool implementations for the design brief pipeline.

Three tools: save_brief, get_brief, update_brief.  The LLM extracts
parameters from user input (specs, conversation, research) and stores
them in a brief for user confirmation before building.
"""
from __future__ import annotations

import logging
from typing import Any

from server.design_store import (
    get_brief,
    list_briefs,
    store_brief,
    update_brief,
)

log = logging.getLogger("solidmind.tools_design")

_VALID_STATUSES = {"draft", "proposed", "approved", "building", "done"}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def design_save_brief(
    name: str,
    parameters: dict[str, Any],
    status: str = "draft",
    research_notes: str = "",
) -> dict[str, Any]:
    """Save a design brief.  Accepts any parameters dict.

    The LLM extracts parameters from user specs, research, or conversation
    and stores them here.  The user reviews and approves before building.
    """
    if not name:
        return _error_result("INVALID_INPUT", "Brief name is required")
    if not isinstance(parameters, dict):
        return _error_result("INVALID_INPUT", "Parameters must be a dict")
    if status not in _VALID_STATUSES:
        return _error_result("INVALID_INPUT", f"Invalid status '{status}'. Must be one of: {sorted(_VALID_STATUSES)}")

    brief = store_brief(
        name=name,
        parameters=parameters,
        status=status,
        research_notes=research_notes,
    )
    return {"ok": True, "brief": brief.to_dict()}


def design_get_brief(brief_id: str) -> dict[str, Any]:
    """Retrieve a saved brief by ID."""
    brief = get_brief(brief_id)
    if brief is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")
    return {"ok": True, "brief": brief.to_dict()}


def design_update_brief(
    brief_id: str,
    parameters: dict[str, Any] | None = None,
    status: str | None = None,
    research_notes: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Patch parameters, status, or notes on a brief."""
    if status is not None and status not in _VALID_STATUSES:
        return _error_result(
            "INVALID_INPUT",
            f"Invalid status '{status}'. Must be one of: {sorted(_VALID_STATUSES)}",
        )

    updated = update_brief(
        brief_id,
        parameters=parameters,
        status=status,
        research_notes=research_notes,
        name=name,
    )
    if updated is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    return {"ok": True, "brief": updated.to_dict()}
