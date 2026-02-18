"""JSON line protocol helpers for the Isaac bridge sidecar."""
from __future__ import annotations

import json
from typing import Any


class ProtocolError(Exception):
    """Raised when request framing or payload shape is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_request_line(line: bytes | str) -> tuple[str, dict[str, Any]]:
    """Decode one newline-delimited request into ``(cmd, args)``."""
    if isinstance(line, bytes):
        payload = line.decode("utf-8")
    else:
        payload = line
    payload = payload.strip()
    if not payload:
        raise ProtocolError("INVALID_REQUEST", "Empty request line")
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError("INVALID_JSON", f"Malformed JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("INVALID_REQUEST", "Request must be a JSON object")
    cmd = obj.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ProtocolError("INVALID_REQUEST", "Request field 'cmd' must be a non-empty string")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError("INVALID_REQUEST", "Request field 'args' must be an object")
    return cmd.strip(), args


def ok_response(result: Any) -> dict[str, Any]:
    """Build a success envelope."""
    return {"ok": True, "result": result}


def error_response(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an error envelope."""
    err: dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"ok": False, "error": err}


def encode_response(response: dict[str, Any]) -> bytes:
    """Serialize a response envelope to one newline-delimited JSON line."""
    return (json.dumps(response, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
