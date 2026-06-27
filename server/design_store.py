"""Session-scoped storage for design briefs.

Module-level dict store with token_hex handles — same pattern as
motion_store.py.  Briefs persist for the lifetime of the MCP server process.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from server.design_models import DesignBrief, InterfaceEntry, PartEntry

# Module-level store: brief_id → DesignBrief
_store: dict[str, DesignBrief] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def store_brief(
    name: str,
    parameters: dict[str, Any],
    status: str = "intent",
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
        parts=existing.parts,
        interfaces=existing.interfaces,
        created_at=existing.created_at,
        updated_at=_now_iso(),
    )
    _store[brief_id] = updated
    return updated


def add_part(brief_id: str, part: PartEntry) -> DesignBrief | None:
    """Append a part to a brief's parts list.  Returns updated brief or None."""
    existing = _store.get(brief_id)
    if existing is None:
        return None

    updated = DesignBrief(
        brief_id=existing.brief_id,
        name=existing.name,
        parameters=existing.parameters,
        status=existing.status,
        research_notes=existing.research_notes,
        parts=[*existing.parts, part],
        interfaces=existing.interfaces,
        created_at=existing.created_at,
        updated_at=_now_iso(),
    )
    _store[brief_id] = updated
    return updated


def update_part(
    brief_id: str,
    part_name: str,
    **fields: Any,
) -> DesignBrief | None:
    """Patch fields on a named part.  Returns updated brief or None.

    Accepted fields: kind, quantity, specs, status, body_label.
    Unknown fields are silently ignored.
    """
    existing = _store.get(brief_id)
    if existing is None:
        return None

    _ALLOWED = {"kind", "quantity", "specs", "status", "body_label"}
    patched = {k: v for k, v in fields.items() if k in _ALLOWED}
    if not patched:
        return existing

    new_parts: list[PartEntry] = []
    found = False
    for p in existing.parts:
        if p.name == part_name:
            found = True
            new_parts.append(PartEntry(
                name=p.name,
                kind=patched.get("kind", p.kind),
                quantity=patched.get("quantity", p.quantity),
                specs=dict(patched.get("specs", p.specs)),
                status=patched.get("status", p.status),
                body_label=patched.get("body_label", p.body_label),
            ))
        else:
            new_parts.append(p)

    if not found:
        return None

    updated = DesignBrief(
        brief_id=existing.brief_id,
        name=existing.name,
        parameters=existing.parameters,
        status=existing.status,
        research_notes=existing.research_notes,
        parts=new_parts,
        interfaces=existing.interfaces,
        created_at=existing.created_at,
        updated_at=_now_iso(),
    )
    _store[brief_id] = updated
    return updated


def add_interface(brief_id: str, iface: InterfaceEntry) -> DesignBrief | None:
    """Append an interface to a brief.  Returns updated brief or None."""
    existing = _store.get(brief_id)
    if existing is None:
        return None

    updated = DesignBrief(
        brief_id=existing.brief_id,
        name=existing.name,
        parameters=existing.parameters,
        status=existing.status,
        research_notes=existing.research_notes,
        parts=existing.parts,
        interfaces=[*existing.interfaces, iface],
        created_at=existing.created_at,
        updated_at=_now_iso(),
    )
    _store[brief_id] = updated
    return updated


def get_part(brief_id: str, part_name: str) -> tuple[PartEntry | None, list[InterfaceEntry]]:
    """Return a part and its interfaces.  (None, []) if brief or part not found."""
    existing = _store.get(brief_id)
    if existing is None:
        return None, []
    part = existing.get_part(part_name)
    if part is None:
        return None, []
    return part, existing.get_interfaces_for(part_name)


def list_briefs() -> list[dict[str, Any]]:
    """Return summary info for all stored briefs."""
    return [
        {
            "brief_id": b.brief_id,
            "name": b.name,
            "status": b.status,
            "param_count": len(b.parameters),
            "part_count": len(b.parts),
            "interface_count": len(b.interfaces),
            "created_at": b.created_at,
        }
        for b in _store.values()
    ]


def clear() -> int:
    """Clear all stored briefs.  Returns the count removed."""
    count = len(_store)
    _store.clear()
    return count
