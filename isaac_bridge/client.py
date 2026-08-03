"""Bridge-side client for the Isaac sidecar.

The bridge ships its own client so nothing in this package depends on core.
That is deliberate duplication: the Engine Integration Contract is data, not
a shared library (``docs/engine-contract.md``; architecture doc, Principle 2),
and an engine repo must be usable — and testable — on its own.

Core keeps its own client for driving engines; this one exists for the
bridge's in-process tooling (``isaac_bridge.lifecycle``, scripts, tests).
Both speak the same NDJSON envelope, so they stay interchangeable.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from typing import Any

logger = logging.getLogger("solidmind.isaac_bridge.client")

DEFAULT_HOST = os.environ.get("SOLIDMIND_ISAAC_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SOLIDMIND_ISAAC_PORT", "9878"))
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 120.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class BridgeConnectionError(Exception):
    """Raised when the client cannot reach the bridge."""


class BridgeCommandError(Exception):
    """Raised when a command fails on the bridge side."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class BridgeClient:
    """Minimal NDJSON/TCP client for the contract verbs."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self._host = host or DEFAULT_HOST
        self._port = DEFAULT_PORT if port is None else port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sock: socket.socket | None = None
        self._buffer = b""

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    @property
    def port(self) -> int:
        return self._port

    def connect(self, timeout: float | None = None) -> None:
        """Open the connection (no-op when already connected)."""
        if self._sock is not None:
            return
        effective = self._connect_timeout if timeout is None else timeout
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(effective)
        try:
            sock.connect((self._host, self._port))
        except (ConnectionRefusedError, OSError) as exc:
            try:
                sock.close()
            except OSError:
                pass
            raise BridgeConnectionError(
                f"Cannot connect to the Isaac bridge at {self._host}:{self._port}: {exc}"
            ) from exc
        self._sock = sock
        self._buffer = b""
        logger.info("Connected to Isaac bridge at %s:%d", self._host, self._port)

    def connect_with_retry(
        self,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY,
    ) -> None:
        """Connect with exponential backoff between attempts."""
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                self.connect()
                return
            except BridgeConnectionError as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (2**attempt))
        raise BridgeConnectionError(
            f"Failed to connect to the Isaac bridge after {max_retries} attempts"
        ) from last_error

    def disconnect(self) -> None:
        """Close the connection if one is open."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buffer = b""

    # -- contract verbs -------------------------------------------------

    def hello(self) -> dict[str, Any]:
        """Capability handshake (contract §2)."""
        return self.send_command("hello")

    def ping(self) -> bool:
        """Liveness check — never raises."""
        try:
            result = self.send_command("ping", timeout=5.0)
        except Exception:
            return False
        return bool(result.get("pong", False))

    def send_command(
        self,
        cmd: str,
        timeout: float | None = None,
        request_id: str | None = None,
        **args: Any,
    ) -> Any:
        """Send one request and return its ``result`` payload.

        Raises ``BridgeCommandError`` when the bridge answers ``ok: false``,
        ``BridgeConnectionError`` when the socket fails.
        """
        if self._sock is None:
            raise BridgeConnectionError("Not connected — call connect() first")
        effective = self._read_timeout if timeout is None else timeout

        envelope: dict[str, Any] = {"cmd": cmd, "args": args}
        if request_id is not None:
            envelope["request_id"] = request_id
        raw = (json.dumps(envelope) + "\n").encode("utf-8")
        try:
            self._sock.sendall(raw)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._sock = None
            raise BridgeConnectionError(f"Connection lost while sending: {exc}") from exc

        response = self._read_response(effective)
        if not isinstance(response, dict):
            raise BridgeCommandError(
                f"Malformed response for '{cmd}': expected an object",
                code="INVALID_REQUEST",
            )

        if not response.get("ok", False):
            err = response.get("error", "Unknown error")
            if isinstance(err, dict):
                code = err.get("code") if isinstance(err.get("code"), str) else None
                message = err.get("message") or json.dumps(err)
            else:
                code, message = None, str(err)
            raise BridgeCommandError(str(message), code=code)

        return response.get("result")

    def import_urdf(
        self,
        urdf_path: str,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import a URDF into the Isaac scene."""
        kwargs: dict[str, Any] = {"urdf_path": urdf_path}
        if import_config:
            kwargs["import_config"] = import_config
        return self.send_command("import_urdf", **kwargs)

    def screenshot(self, **kwargs: Any) -> dict[str, Any]:
        """Capture the viewport."""
        return self.send_command("screenshot", **kwargs)

    # -- framing --------------------------------------------------------

    def _read_response(self, timeout: float) -> Any:
        assert self._sock is not None
        self._sock.settimeout(timeout)
        while b"\n" not in self._buffer:
            try:
                data = self._sock.recv(65536)
            except TimeoutError as exc:
                raise BridgeConnectionError(
                    f"Timed out waiting for a bridge response ({timeout}s)"
                ) from exc
            except (ConnectionResetError, OSError) as exc:
                self._sock = None
                raise BridgeConnectionError(f"Connection lost while reading: {exc}") from exc
            if not data:
                self._sock = None
                raise BridgeConnectionError("Bridge closed the connection")
            self._buffer += data

        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            return json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BridgeCommandError(
                f"Malformed response line: {exc}", code="INVALID_JSON"
            ) from exc
