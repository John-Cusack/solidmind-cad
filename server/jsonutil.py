from __future__ import annotations

from typing import Any


try:
    import orjson  # type: ignore

    def dumps(obj: Any) -> bytes:
        # orjson emits UTF-8 bytes and uses a compact representation by default.
        return orjson.dumps(obj)

    def loads(data: bytes | bytearray | memoryview | str) -> Any:
        if isinstance(data, str):
            return orjson.loads(data.encode("utf-8"))
        return orjson.loads(data)

except ModuleNotFoundError:
    import json

    def dumps(obj: Any) -> bytes:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")

    def loads(data: bytes | bytearray | memoryview | str) -> Any:
        if isinstance(data, (bytes, bytearray, memoryview)):
            return json.loads(bytes(data).decode("utf-8"))
        return json.loads(data)


def dumps_str(obj: Any) -> str:
    """Return compact JSON as a ``str`` (convenience for text content fields)."""
    return dumps(obj).decode("utf-8")

