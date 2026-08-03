"""NDJSON client for the C++ Chrono daemon.

The shim is the contract face of engine-chrono; the daemon behind it speaks
the same framing but a Chrono-native vocabulary (``simulation_spec``).  This
client is the hop between them, and stays inside the engine — core never
speaks Chrono spec.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

logger = logging.getLogger("solidmind.chrono_bridge.daemon")

DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 9977


class DaemonError(Exception):
    """Raised when the daemon is unreachable or returns an error."""

    def __init__(self, message: str, *, code: str = "ENGINE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class DaemonClient:
    """One short-lived connection per request — the daemon accepts serially."""

    def __init__(
        self,
        host: str = DEFAULT_DAEMON_HOST,
        port: int = DEFAULT_DAEMON_PORT,
        *,
        timeout: float = 300.0,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    def send(self, cmd: str, args: dict[str, Any] | None = None) -> Any:
        """Send one command and return its ``result`` payload."""
        payload = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
        try:
            with socket.create_connection((self._host, self._port), timeout=10.0) as sock:
                sock.settimeout(self._timeout)
                sock.sendall(payload.encode("utf-8"))
                buffer = b""
                while b"\n" not in buffer:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise DaemonError(
                            f"Chrono daemon at {self.address} closed the connection",
                            code="ENGINE_ERROR",
                        )
                    buffer += chunk
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            raise DaemonError(
                f"Cannot reach the Chrono daemon at {self.address}: {exc}. "
                "Start it with chrono_daemon/run.sh.",
                code="ENGINE_ERROR",
            ) from exc

        try:
            response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DaemonError(f"Malformed daemon response: {exc}") from exc

        if not isinstance(response, dict):
            raise DaemonError("Daemon response was not an object")
        if not response.get("ok", False):
            error = response.get("error", "unknown error")
            # Contract-shaped daemons send {code, message}; older ones a string.
            if isinstance(error, dict):
                raise DaemonError(
                    str(error.get("message", "")), code=str(error.get("code", "ENGINE_ERROR"))
                )
            raise DaemonError(str(error))
        return response.get("result")

    def ping(self) -> bool:
        try:
            result = self.send("ping")
        except DaemonError:
            return False
        return bool(isinstance(result, dict) and result.get("pong"))

    def version(self) -> str | None:
        """Daemon version from its handshake, when it speaks one."""
        try:
            result = self.send("hello")
        except DaemonError:
            return None
        if isinstance(result, dict):
            return str(result.get("engine_version", "")) or None
        return None
