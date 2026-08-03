"""Minimal NDJSON client for the TCK.

Deliberately not core's client: the kit must run inside an engine repository
with nothing installed, and it needs to see raw envelopes (including malformed
ones) that a well-behaved client would hide.
"""

from __future__ import annotations

import json
import socket
from typing import Any


class TckConnectionError(Exception):
    """The engine could not be reached or dropped the connection."""


class TckClient:
    """One long-lived connection, raw envelopes in and out."""

    def __init__(self, host: str, port: int, *, timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = b""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def connect(self, timeout: float = 5.0) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise TckConnectionError(f"Cannot connect to {self.address}: {exc}") from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buffer = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> TckClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- raw I/O ---------------------------------------------------------

    def send_raw(self, line: str) -> dict[str, Any]:
        """Send *line* verbatim (no framing help) and read one response."""
        if self._sock is None:
            raise TckConnectionError("Not connected")
        try:
            self._sock.sendall(line.encode("utf-8"))
        except OSError as exc:
            raise TckConnectionError(f"Send failed: {exc}") from exc

        while b"\n" not in self._buffer:
            try:
                data = self._sock.recv(65536)
            except TimeoutError as exc:
                raise TckConnectionError(f"Timed out after {self.timeout}s") from exc
            except OSError as exc:
                raise TckConnectionError(f"Read failed: {exc}") from exc
            if not data:
                raise TckConnectionError("Engine closed the connection")
            self._buffer += data

        raw, self._buffer = self._buffer.split(b"\n", 1)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TckConnectionError(f"Engine sent unparseable JSON: {exc}") from exc

    def request(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a well-formed request; return the whole response envelope."""
        envelope: dict[str, Any] = {"cmd": cmd, "args": args or {}}
        if request_id is not None:
            envelope["request_id"] = request_id
        return self.send_raw(json.dumps(envelope) + "\n")

    def result(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its ``result``; raise on ``ok: false``."""
        response = self.request(cmd, args)
        if not response.get("ok"):
            error = response.get("error", {})
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise TckConnectionError(f"{cmd} failed [{code}]: {message}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}
