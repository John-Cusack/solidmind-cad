from __future__ import annotations

import math
from typing import Any


class JcsError(ValueError):
    pass


def _escape_string(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _format_float(x: float) -> str:
    if not math.isfinite(x):
        raise JcsError("Non-finite numbers are not permitted in JCS")

    # ECMAScript / JSON.stringify serializes -0 as "0".
    if x == 0.0:
        return "0"

    s = repr(x)
    if "e" in s:
        mant, exp = s.split("e", 1)
        if mant.endswith(".0"):
            mant = mant[:-2]

        sign = ""
        digits = exp
        if digits and digits[0] in "+-":
            sign = digits[0]
            digits = digits[1:]
        digits = digits.lstrip("0") or "0"
        return f"{mant}e{sign}{digits}"

    if s.endswith(".0"):
        return s[:-2]
    return s


def normalize_float(value: float, precision: int = 10) -> float:
    """Normalize a float to a fixed precision for consistent hashing.

    Args:
        value: The float value to normalize
        precision: Decimal places to round to

    Returns:
        Normalized float value
    """
    if not math.isfinite(value):
        raise JcsError("Cannot normalize non-finite float")
    return round(value, precision)


def normalize_dict_keys(obj: dict[str, Any]) -> dict[str, Any]:
    """Recursively normalize all dictionary keys to sorted order.

    Args:
        obj: Dictionary to normalize

    Returns:
        Dictionary with sorted keys at all levels
    """
    if not isinstance(obj, dict):
        return obj

    result: dict[str, Any] = {}
    for key in sorted(obj.keys()):
        if isinstance(obj[key], dict):
            result[key] = normalize_dict_keys(obj[key])
        elif isinstance(obj[key], list):
            result[key] = [
                normalize_dict_keys(item) if isinstance(item, dict) else item
                for item in obj[key]
            ]
        else:
            result[key] = obj[key]
    return result


def sort_arrays_canonicalically(obj: Any) -> Any:
    """Sort arrays deterministically for canonicalization.

    Args:
        obj: Object to process

    Returns:
        Object with arrays sorted where appropriate
    """
    if isinstance(obj, dict):
        return {k: sort_arrays_canonicalically(v) for k, v in obj.items()}
    if isinstance(obj, list):
        if obj and all(isinstance(item, (str, int, float)) for item in obj):
            return sorted(obj)
        return [sort_arrays_canonicalically(item) for item in obj]
    return obj


def canonicalize(obj: Any) -> str:
    """RFC 8785-style canonical JSON serialization (JCS).

    This implementation is intentionally strict:
    - dict keys must be strings
    - only JSON types are allowed (dict, list, str, int/float, bool, None)
    """
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"

    if isinstance(obj, str):
        return _escape_string(obj)

    # bool is a subclass of int; keep this after bool handling.
    if isinstance(obj, int):
        return str(obj)

    if isinstance(obj, float):
        return _format_float(obj)

    if isinstance(obj, list):
        return "[" + ",".join(canonicalize(v) for v in obj) + "]"

    if isinstance(obj, dict):
        items: list[str] = []
        for k in sorted(obj.keys()):
            if not isinstance(k, str):
                raise JcsError("JCS requires object keys to be strings")
            items.append(_escape_string(k) + ":" + canonicalize(obj[k]))
        return "{" + ",".join(items) + "}"

    raise JcsError(f"Unsupported type for JCS: {type(obj).__name__}")
