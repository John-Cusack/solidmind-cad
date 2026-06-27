"""Server-side geometry store for ref-based data flow.

Geometry tools store bulk element arrays here and return lightweight handles.
``cad.sketch`` resolves handles back to elements without the LLM ever seeing
the raw data.  Session-scoped (dies with the MCP server process).
"""

from __future__ import annotations

import secrets
from typing import Any

# Module-level store: handle → elements list
_store: dict[str, list[dict[str, Any]]] = {}
# Optional metadata per handle
_metadata: dict[str, dict[str, Any]] = {}


def store(elements: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> str:
    """Store elements and return a handle like ``geo_a1b2c3d4``."""
    handle = f"geo_{secrets.token_hex(4)}"
    _store[handle] = elements
    if metadata:
        _metadata[handle] = metadata
    return handle


def retrieve(handle: str) -> list[dict[str, Any]] | None:
    """Return stored elements for *handle*, or ``None`` if not found.

    Does **not** remove the entry (same ref can be reused, e.g. planet gears).
    """
    return _store.get(handle)


def remove(handle: str) -> bool:
    """Explicitly remove a handle.  Returns ``True`` if it existed."""
    existed = handle in _store
    _store.pop(handle, None)
    _metadata.pop(handle, None)
    return existed


def clear() -> int:
    """Clear all stored geometry.  Returns the number of handles removed."""
    count = len(_store)
    _store.clear()
    _metadata.clear()
    return count


def stats() -> dict[str, Any]:
    """Return diagnostics about the store."""
    total_elements = sum(len(elems) for elems in _store.values())
    return {
        "handle_count": len(_store),
        "total_elements": total_elements,
    }
