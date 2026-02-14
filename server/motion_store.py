"""Session-scoped storage for mechanism definitions.

Module-level dict store with UUID handles — same pattern as geometry_store.py.
Mechanisms persist for the lifetime of the MCP server process.
"""
from __future__ import annotations

import secrets
from typing import Any

from server.motion_models import Mechanism

# Module-level store: handle → Mechanism
_store: dict[str, Mechanism] = {}


def store(mechanism: Mechanism) -> str:
    """Store a mechanism and return a handle like ``mech_a1b2c3d4``."""
    handle = f"mech_{secrets.token_hex(4)}"
    _store[handle] = mechanism
    return handle


def get(handle: str) -> Mechanism | None:
    """Return stored mechanism for *handle*, or ``None`` if not found."""
    return _store.get(handle)


def remove(handle: str) -> bool:
    """Remove a mechanism.  Returns ``True`` if it existed."""
    existed = handle in _store
    _store.pop(handle, None)
    return existed


def list_all() -> list[dict[str, Any]]:
    """Return summary info for all stored mechanisms."""
    result = []
    for handle, mech in _store.items():
        result.append({
            "id": handle,
            "name": mech.name,
            "part_count": len(mech.parts),
            "joint_count": len(mech.joints),
        })
    return result


def clear() -> int:
    """Clear all stored mechanisms.  Returns the count removed."""
    count = len(_store)
    _store.clear()
    return count
