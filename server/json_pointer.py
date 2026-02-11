from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class JsonPointerError(ValueError):
    pass


def _unescape(token: str) -> str:
    # RFC 6901: "~1" -> "/", "~0" -> "~"
    if "~" not in token:
        return token
    out = []
    i = 0
    while i < len(token):
        ch = token[i]
        if ch != "~":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(token):
            raise JsonPointerError("Invalid escape in JSON Pointer token")
        nxt = token[i + 1]
        if nxt == "0":
            out.append("~")
        elif nxt == "1":
            out.append("/")
        else:
            raise JsonPointerError("Invalid escape in JSON Pointer token")
        i += 2
    return "".join(out)


def parse(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise JsonPointerError("JSON Pointer must be empty or start with '/'")
    # Split keeps leading empty; skip first.
    raw_tokens = pointer.split("/")[1:]
    return [_unescape(t) for t in raw_tokens]


def _parse_index(token: str) -> int:
    if token == "":
        raise JsonPointerError("Empty array index in JSON Pointer")
    if token.startswith("+") or (token.startswith("0") and token != "0"):
        # Disallow "+1" and leading zeros for indices for consistency.
        raise JsonPointerError("Invalid array index in JSON Pointer")
    try:
        idx = int(token, 10)
    except ValueError as e:
        raise JsonPointerError("Invalid array index in JSON Pointer") from e
    if idx < 0:
        raise JsonPointerError("Negative array index in JSON Pointer")
    return idx


@dataclass(frozen=True, slots=True)
class _ParentRef:
    parent: Any
    token: str


def _get_parent(doc: Any, tokens: list[str], *, create_missing: bool) -> _ParentRef:
    if not tokens:
        raise JsonPointerError("Pointer refers to the document root; not allowed here")

    cur = doc
    for token in tokens[:-1]:
        if isinstance(cur, dict):
            if token not in cur:
                if not create_missing:
                    raise JsonPointerError("Missing object key while resolving JSON Pointer")
                cur[token] = {}
            cur = cur[token]
            continue

        if isinstance(cur, list):
            idx = _parse_index(token)
            if idx >= len(cur):
                raise JsonPointerError("Array index out of range while resolving JSON Pointer")
            cur = cur[idx]
            continue

        raise JsonPointerError("Cannot traverse non-container while resolving JSON Pointer")

    return _ParentRef(parent=cur, token=tokens[-1])


def get(doc: Any, pointer: str) -> Any:
    tokens = parse(pointer)
    if not tokens:
        return doc
    cur = doc
    for token in tokens:
        if isinstance(cur, dict):
            if token not in cur:
                raise JsonPointerError("Missing object key while resolving JSON Pointer")
            cur = cur[token]
        elif isinstance(cur, list):
            idx = _parse_index(token)
            if idx >= len(cur):
                raise JsonPointerError("Array index out of range while resolving JSON Pointer")
            cur = cur[idx]
        else:
            raise JsonPointerError("Cannot traverse non-container while resolving JSON Pointer")
    return cur


def set_value(doc: Any, pointer: str, value: Any, *, create_missing: bool) -> None:
    tokens = parse(pointer)
    ref = _get_parent(doc, tokens, create_missing=create_missing)
    parent = ref.parent
    token = ref.token

    if isinstance(parent, dict):
        parent[token] = value
        return

    if isinstance(parent, list):
        idx = _parse_index(token)
        if idx >= len(parent):
            raise JsonPointerError("Array index out of range while setting JSON Pointer")
        parent[idx] = value
        return

    raise JsonPointerError("Cannot set value on non-container parent")


def remove_value(doc: Any, pointer: str) -> None:
    tokens = parse(pointer)
    ref = _get_parent(doc, tokens, create_missing=False)
    parent = ref.parent
    token = ref.token

    if isinstance(parent, dict):
        if token not in parent:
            raise JsonPointerError("Missing object key while removing JSON Pointer")
        del parent[token]
        return

    if isinstance(parent, list):
        idx = _parse_index(token)
        if idx >= len(parent):
            raise JsonPointerError("Array index out of range while removing JSON Pointer")
        parent.pop(idx)
        return

    raise JsonPointerError("Cannot remove value on non-container parent")

