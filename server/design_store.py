"""Session-scoped storage for design briefs.

Module-level dict store with token_hex handles — same pattern as
motion_store.py.  Briefs persist for the lifetime of the MCP server process.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from server.design_models import DesignBrief

# Module-level store: brief_id → DesignBrief
_store: dict[str, DesignBrief] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_brief(
    name: str,
    parameters: dict[str, Any],
    status: str = "draft",
    research_notes: str = "",
) -> DesignBrief:
    """Create and store a new brief.  Returns the stored DesignBrief."""
    brief_id = f"brief_{secrets.token_hex(4)}"
    now = _now_iso()
    brief = DesignBrief(
        brief_id=brief_id,
        name=name,
        parameters=parameters,
        status=status,
        research_notes=research_notes,
        created_at=now,
        updated_at=now,
    )
    _store[brief_id] = brief
    return brief


def get_brief(brief_id: str) -> DesignBrief | None:
    """Return stored brief or None."""
    return _store.get(brief_id)


def update_brief(
    brief_id: str,
    *,
    parameters: dict[str, Any] | None = None,
    status: str | None = None,
    research_notes: str | None = None,
    name: str | None = None,
) -> DesignBrief | None:
    """Patch fields on an existing brief.  Returns updated brief or None."""
    existing = _store.get(brief_id)
    if existing is None:
        return None

    updated = DesignBrief(
        brief_id=existing.brief_id,
        name=name if name is not None else existing.name,
        parameters=parameters if parameters is not None else existing.parameters,
        status=status if status is not None else existing.status,
        research_notes=research_notes if research_notes is not None else existing.research_notes,
        created_at=existing.created_at,
        updated_at=_now_iso(),
    )
    _store[brief_id] = updated
    return updated


def list_briefs() -> list[dict[str, Any]]:
    """Return summary info for all stored briefs."""
    return [
        {
            "brief_id": b.brief_id,
            "name": b.name,
            "status": b.status,
            "param_count": len(b.parameters),
            "created_at": b.created_at,
        }
        for b in _store.values()
    ]


def clear() -> int:
    """Clear all stored briefs.  Returns the count removed."""
    count = len(_store)
    _store.clear()
    return count
