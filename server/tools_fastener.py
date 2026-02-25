"""MCP tool for fastener dimension lookup.

One tool: cad.fastener_spec — returns all dimensions needed to model
a metric bolt, including through-holes, counterbores, tap drills, etc.
"""
from __future__ import annotations

from typing import Any

from server.fastener_data import (
    SUPPORTED_HEAD_TYPES,
    SUPPORTED_SIZES,
    lookup,
)


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def cad_fastener_spec(
    size: str,
    length: float,
    head_type: str = "socket_head",
) -> dict[str, Any]:
    """Look up all dimensions for a metric fastener.

    Given a bolt size (e.g. "M4"), length, and head type, returns every
    dimension needed for CAD: head diameter, head height, through-hole
    sizes (close/normal/loose fit), counterbore or countersink dimensions,
    tap drill size, socket/wrench size, and washer dimensions.

    This avoids the LLM having to recall ISO tables from memory.
    """
    spec = lookup(size=size, length=length, head_type=head_type)
    if spec is None:
        return _error_result(
            "FASTENER_NOT_FOUND",
            f"No data for size='{size}' head_type='{head_type}'. "
            f"Supported sizes: {SUPPORTED_SIZES}. "
            f"Supported head types: {SUPPORTED_HEAD_TYPES}.",
        )
    return {"ok": True, "fastener": spec.to_dict()}
