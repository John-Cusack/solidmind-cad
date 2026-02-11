"""Socket client for communicating with the FreeCAD addon.

The MCP bridge server uses this client to send commands to the FreeCAD addon
running inside the FreeCAD GUI process over a TCP socket.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

logger = logging.getLogger("solidmind.freecad_client")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class FreeCADConnectionError(Exception):
    """Raised when the client cannot connect to the FreeCAD addon."""


class FreeCADCommandError(Exception):
    """Raised when a command fails on the FreeCAD side."""


class FreeCADClient:
    """TCP client that sends commands to the FreeCAD addon socket server."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._buffer = b""

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        """Connect to the FreeCAD addon socket server."""
        if self._sock is not None:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self._host, self._port))
        except (ConnectionRefusedError, OSError) as e:
            sock.close()
            raise FreeCADConnectionError(
                f"Cannot connect to FreeCAD addon at {self._host}:{self._port}. "
                "Please start FreeCAD with the SolidMind addon loaded."
            ) from e

        self._sock = sock
        self._buffer = b""
        logger.info("Connected to FreeCAD addon at %s:%d", self._host, self._port)

    def connect_with_retry(
        self,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY,
    ) -> None:
        """Connect with retries and exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                self.connect()
                return
            except FreeCADConnectionError as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        "Connection attempt %d/%d failed, retrying in %.1fs",
                        attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)

        raise FreeCADConnectionError(
            f"Failed to connect after {max_retries} attempts. "
            "Please start FreeCAD with the SolidMind addon loaded."
        ) from last_error

    def disconnect(self) -> None:
        """Close the connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buffer = b""
            logger.info("Disconnected from FreeCAD addon")

    def ping(self) -> bool:
        """Check if the connection is alive."""
        try:
            result = self.send_command("ping")
            return result.get("pong", False) is True
        except Exception:
            return False

    def send_command(
        self,
        cmd: str,
        timeout: float = READ_TIMEOUT,
        **args: Any,
    ) -> Any:
        """Send a command and return the result.

        Raises ``FreeCADCommandError`` if the command fails on the FreeCAD
        side, or ``FreeCADConnectionError`` if the connection is lost.
        """
        self._ensure_connected()
        assert self._sock is not None

        # Encode and send
        message = json.dumps({"cmd": cmd, "args": args}, separators=(",", ":")) + "\n"
        try:
            self._sock.sendall(message.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._sock = None
            raise FreeCADConnectionError(f"Connection lost while sending: {e}") from e

        # Read response
        response = self._read_response(timeout)

        if not response.get("ok", False):
            error_msg = response.get("error", "Unknown error")
            raise FreeCADCommandError(error_msg)

        return response.get("result")

    def _ensure_connected(self) -> None:
        """Ensure we have an active connection, reconnecting if needed."""
        if self._sock is None:
            self.connect_with_retry()

    def _read_response(self, timeout: float) -> dict[str, Any]:
        """Read a newline-delimited JSON response."""
        assert self._sock is not None
        self._sock.settimeout(timeout)

        while b"\n" not in self._buffer:
            try:
                data = self._sock.recv(4096)
            except socket.timeout as e:
                raise FreeCADConnectionError(
                    f"Timed out waiting for response ({timeout}s)"
                ) from e
            except (ConnectionResetError, OSError) as e:
                self._sock = None
                raise FreeCADConnectionError(
                    f"Connection lost while reading: {e}"
                ) from e

            if not data:
                self._sock = None
                raise FreeCADConnectionError("Connection closed by FreeCAD addon")

            self._buffer += data

        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


# Module-level singleton
_client: FreeCADClient | None = None


def get_client() -> FreeCADClient:
    """Get or create the global FreeCAD client."""
    global _client
    if _client is None:
        _client = FreeCADClient()
    return _client


def reset_client() -> None:
    """Disconnect and reset the global client."""
    global _client
    if _client is not None:
        _client.disconnect()
        _client = None
