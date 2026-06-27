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
    nut_lookup,
)

# Common shorthand → canonical head_type mapping
_HEAD_TYPE_ALIASES: dict[str, str] = {
    "socket": "socket_head",
    "shcs": "socket_head",
    "cap": "socket_head",
    "cap_screw": "socket_head",
    "hex_bolt": "hex",
    "bhcs": "button_head",
    "button": "button_head",
    "csk": "countersunk",
    "flat_head": "countersunk",
    "flat": "countersunk",
    "grub": "set_screw",
}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def cad_fastener_spec(
    size: str,
    length: float = 0.0,
    head_type: str = "socket_head",
) -> dict[str, Any]:
    """Look up all dimensions for a metric fastener.

    Given a bolt size (e.g. "M4"), length, and head type, returns every
    dimension needed for CAD: head diameter, head height, through-hole
    sizes (close/normal/loose fit), counterbore or countersink dimensions,
    tap drill size, socket/wrench size, and washer dimensions.

    ``length`` defaults to 0 when only head/hole dimensions are needed.
    ``head_type`` accepts common aliases: 'socket' → 'socket_head',
    'button' → 'button_head', 'csk'/'flat' → 'countersunk', etc.

    This avoids the LLM having to recall ISO tables from memory.
    """
    # Resolve head_type aliases
    canonical = _HEAD_TYPE_ALIASES.get(head_type.lower(), head_type)

    spec = lookup(size=size, length=length, head_type=canonical)
    if spec is None:
        return _error_result(
            "FASTENER_NOT_FOUND",
            f"No data for size='{size}' head_type='{head_type}' "
            f"(resolved to '{canonical}'). "
            f"Supported sizes: {SUPPORTED_SIZES}. "
            f"Supported head types: {SUPPORTED_HEAD_TYPES}.",
        )

    result: dict[str, Any] = {"ok": True, "fastener": spec.to_dict()}

    # Also include nut dimensions for convenience
    nut = nut_lookup(size=size, nut_type="hex")
    if nut is not None:
        result["nut"] = nut.to_dict()

    return result
