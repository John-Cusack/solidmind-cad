"""Lightweight dataclass <-> dict helpers (replaces hand-rolled serialization)."""
from __future__ import annotations

import dataclasses
import types
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints


def dc_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass instance to a JSON-compatible dict.

    - Enums -> ``.value``
    - Nested dataclasses -> recurse
    - Paths -> ``str``
    - Everything else passed through
    """
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"Expected a dataclass instance, got {type(obj)}")
    result: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        result[f.name] = _to_value(getattr(obj, f.name))
    return result


def _to_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, Enum):
        return val.value
    if dataclasses.is_dataclass(val) and not isinstance(val, type):
        return dc_to_dict(val)
    if isinstance(val, list):
        return [_to_value(v) for v in val]
    if isinstance(val, dict):
        return {k: _to_value(v) for k, v in val.items()}
    if isinstance(val, Path):
        return str(val)
    return val


def dc_from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Construct a dataclass from a dict, recursing into nested types.

    - Enum fields -> constructed from value string
    - Nested dataclass fields -> recursed
    - ``list[SomeDataclass]`` -> mapped
    - Missing keys -> use dataclass defaults
    """
    if not isinstance(data, dict):
        return data
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    field_names = {f.name for f in dataclasses.fields(cls)}
    for key, val in data.items():
        if key not in field_names:
            continue
        ftype = hints.get(key)
        if ftype is not None:
            kwargs[key] = _from_value(ftype, val)
        else:
            kwargs[key] = val
    return cls(**kwargs)


def _resolve_optional(ftype: Any) -> tuple[bool, Any]:
    """If *ftype* is ``X | None``, return ``(True, X)``.  Otherwise ``(False, ftype)``."""
    origin = get_origin(ftype)
    if origin is types.UnionType or origin is Union:
        args = get_args(ftype)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return True, non_none[0]
    return False, ftype


def _from_value(ftype: Any, val: Any) -> Any:
    if val is None:
        return None

    is_opt, inner = _resolve_optional(ftype)
    if is_opt:
        return _from_value(inner, val)

    if isinstance(ftype, type):
        if issubclass(ftype, Enum) and not isinstance(val, ftype):
            return ftype(val)
        if dataclasses.is_dataclass(ftype) and isinstance(val, dict):
            return dc_from_dict(ftype, val)
        if issubclass(ftype, Path) and isinstance(val, str):
            return Path(val)

    origin = get_origin(ftype)
    args = get_args(ftype)
    if origin is list and args:
        elem_type = args[0]
        if isinstance(val, list):
            return [_from_value(elem_type, v) for v in val]

    return val
