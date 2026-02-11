"""Newline-delimited JSON protocol for FreeCAD addon socket communication.

Commands are JSON objects with ``cmd`` and ``args`` fields.
Responses are JSON objects with ``ok``, and either ``result`` or ``error``.
Each message is a single line terminated by ``\\n``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field as dc_field
from typing import Any


@dataclass(frozen=True, slots=True)
class Command:
    """A command sent from the MCP bridge to the FreeCAD addon."""

    cmd: str
    args: dict[str, Any] = dc_field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"cmd": self.cmd, "args": self.args}, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Command:
        data = json.loads(line)
        return cls(cmd=data["cmd"], args=data.get("args", {}))


@dataclass(frozen=True, slots=True)
class Response:
    """A response from the FreeCAD addon back to the MCP bridge."""

    ok: bool
    result: Any = None
    error: str | None = None

    def to_json(self) -> str:
        d: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            d["result"] = self.result
        else:
            d["error"] = self.error
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Response:
        data = json.loads(line)
        return cls(
            ok=data["ok"],
            result=data.get("result"),
            error=data.get("error"),
        )

    @classmethod
    def success(cls, result: Any = None) -> Response:
        return cls(ok=True, result=result)

    @classmethod
    def failure(cls, error: str) -> Response:
        return cls(ok=False, error=error)


def encode_message(msg: Command | Response) -> bytes:
    """Encode a protocol message as newline-terminated bytes."""
    return (msg.to_json() + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict[str, Any]:
    """Decode a single newline-terminated JSON line."""
    return json.loads(line.decode("utf-8").strip())
