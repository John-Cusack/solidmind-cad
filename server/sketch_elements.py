"""Sketch element parameter normalization.

Canonicalizes element parameter names before they reach the FreeCAD addon.
The addon expects strict canonical names (``cx``, ``cy``, ``w``, ``h``, etc.)
but the LLM often produces natural aliases (``center``, ``width``, ``height``).
This module bridges that gap with a declarative schema + normalize pass.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One canonical parameter with optional aliases."""

    canonical: str
    aliases: tuple[str, ...]
    required: bool = False


# ---------------------------------------------------------------------------
# Element schemas — canonical params the addon expects
# ---------------------------------------------------------------------------

ELEMENT_SCHEMAS: dict[str, tuple[ParamSpec, ...]] = {
    "rect": (
        ParamSpec("x", (), True),
        ParamSpec("y", (), True),
        ParamSpec("w", ("width",), True),
        ParamSpec("h", ("height",), True),
    ),
    "circle": (
        ParamSpec("cx", ("center_x",), True),
        ParamSpec("cy", ("center_y",), True),
        ParamSpec("r", ("radius",), True),
    ),
    "line": (
        ParamSpec("x1", ("start_x",), True),
        ParamSpec("y1", ("start_y",), True),
        ParamSpec("x2", ("end_x",), True),
        ParamSpec("y2", ("end_y",), True),
    ),
    "arc": (
        ParamSpec("cx", ("center_x",), True),
        ParamSpec("cy", ("center_y",), True),
        ParamSpec("r", ("radius",), True),
        ParamSpec("start_angle", (), True),
        ParamSpec("end_angle", (), True),
    ),
}

# Build reverse alias lookup per element type: alias → canonical
_ALIAS_MAP: dict[str, dict[str, str]] = {}
for _etype, _specs in ELEMENT_SCHEMAS.items():
    _m: dict[str, str] = {}
    for _s in _specs:
        for _a in _s.aliases:
            _m[_a] = _s.canonical
    _ALIAS_MAP[_etype] = _m


def _expand_array_shorthands(elem: dict) -> None:
    """Expand array-style shorthands into flat keys.

    - ``center: [x, y]`` → ``cx``, ``cy``  (circle, arc)
    - ``start: [x, y]``  → ``x1``, ``y1``  (line)
    - ``end: [x, y]``    → ``x2``, ``y2``  (line)
    """
    etype = elem.get("type")

    # center: [x, y] → cx, cy  (only for circle/arc)
    if etype in ("circle", "arc"):
        center = elem.pop("center", None)
        if isinstance(center, (list, tuple)) and len(center) >= 2:
            elem.setdefault("cx", center[0])
            elem.setdefault("cy", center[1])

    # start/end arrays → x1/y1/x2/y2  (only for line)
    if etype == "line":
        start = elem.pop("start", None)
        if isinstance(start, (list, tuple)) and len(start) >= 2:
            elem.setdefault("x1", start[0])
            elem.setdefault("y1", start[1])
        end = elem.pop("end", None)
        if isinstance(end, (list, tuple)) and len(end) >= 2:
            elem.setdefault("x2", end[0])
            elem.setdefault("y2", end[1])


def _apply_aliases(elem: dict) -> None:
    """Rename alias keys to their canonical names."""
    etype = elem.get("type")
    alias_map = _ALIAS_MAP.get(etype)  # type: ignore[arg-type]
    if not alias_map:
        return
    for alias, canonical in alias_map.items():
        if alias in elem and canonical not in elem:
            elem[canonical] = elem.pop(alias)


def normalize_elements(elements: list[dict]) -> list[dict]:
    """Normalize element params in-place. Returns the same list.

    Unknown element types pass through unchanged (forward-compatible).
    Unknown extra keys are preserved (addon ignores them).
    """
    for elem in elements:
        _expand_array_shorthands(elem)
        _apply_aliases(elem)
    return elements
