"""Socket client for optional Isaac simulation bridge.

Mirrors the Chrono client pattern so motion tools can route dynamic simulation
requests to an Isaac sidecar when available.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

logger = logging.getLogger("solidmind.isaac_client")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9878
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 120.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class IsaacConnectionError(Exception):
    """Raised when the client cannot connect to the Isaac bridge."""


class IsaacCommandError(Exception):
    """Raised when a command fails on the Isaac bridge."""


class IsaacClient:
    """TCP client that sends commands to the Isaac bridge socket server."""

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
        """Connect to the Isaac bridge socket server."""
        if self._sock is not None:
            return

        logger.debug(
            "Connecting to Isaac bridge at %s:%d (timeout=%.1fs)",
            self._host,
            self._port,
            timeout,
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self._host, self._port))
        except (ConnectionRefusedError, OSError) as exc:
            sock.close()
            logger.warning(
                "Connection to Isaac bridge at %s:%d failed: %s",
                self._host,
                self._port,
                exc,
            )
            raise IsaacConnectionError(
                f"Cannot connect to Isaac bridge at {self._host}:{self._port}: {exc}. "
                "Please start the Isaac bridge process."
            ) from exc

        self._sock = sock
        self._buffer = b""
        logger.info("Connected to Isaac bridge at %s:%d", self._host, self._port)

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
            except IsaacConnectionError as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        "Isaac bridge connection attempt %d/%d failed, retrying in %.1fs",
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)

        raise IsaacConnectionError(
            f"Failed to connect to Isaac bridge after {max_retries} attempts. "
            "Please start the Isaac bridge process."
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
            logger.info("Disconnected from Isaac bridge")

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
        """Send a command and return result data."""
        self._ensure_connected()
        assert self._sock is not None

        message = json.dumps({"cmd": cmd, "args": args}, separators=(",", ":")) + "\n"
        logger.debug("Sending command: %s (payload %d bytes)", cmd, len(message))
        try:
            self._sock.sendall(message.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.error("Connection lost while sending '%s': %s", cmd, exc)
            self._sock = None
            raise IsaacConnectionError(f"Connection lost while sending: {exc}") from exc

        response = self._read_response(timeout)
        logger.debug(
            "Response for '%s': ok=%s keys=%s",
            cmd,
            response.get("ok"),
            list(response.keys()),
        )

        if not response.get("ok", False):
            error_msg = response.get("error", "Unknown error")
            logger.error("Command '%s' failed: %s", cmd, error_msg)
            raise IsaacCommandError(error_msg)

        return response.get("result")

    def simulate(
        self,
        mechanism: dict[str, Any],
        duration_s: float = 1.0,
        dt_s: float = 0.001,
        output_interval: float = 0.01,
    ) -> dict[str, Any]:
        """Run batch simulation and return summary/time-series data."""
        return self.send_command(
            "simulate",
            timeout=max(READ_TIMEOUT, duration_s * 100),
            mechanism=mechanism,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )

    def teleop_start(
        self,
        mechanism: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a teleop session in Isaac."""
        return self.send_command(
            "teleop_start",
            mechanism=mechanism,
            profile=profile or {},
        )

    def teleop_command(
        self,
        session_id: str,
        vx_mps: float,
        yaw_rate_rps: float,
        body_height_m: float,
    ) -> dict[str, Any]:
        """Send one teleop command sample."""
        return self.send_command(
            "teleop_command",
            session_id=session_id,
            vx_mps=vx_mps,
            yaw_rate_rps=yaw_rate_rps,
            body_height_m=body_height_m,
        )

    def teleop_state(self, session_id: str) -> dict[str, Any]:
        """Read teleop state from Isaac."""
        return self.send_command("teleop_state", session_id=session_id)

    def teleop_stop(self, session_id: str) -> dict[str, Any]:
        """Stop a teleop session."""
        return self.send_command("teleop_stop", session_id=session_id)

    def _ensure_connected(self) -> None:
        if self._sock is None:
            self.connect_with_retry()

    def _read_response(self, timeout: float) -> dict[str, Any]:
        assert self._sock is not None
        self._sock.settimeout(timeout)

        while b"\n" not in self._buffer:
            try:
                data = self._sock.recv(65536)
            except socket.timeout as exc:
                raise IsaacConnectionError(
                    f"Timed out waiting for Isaac bridge response ({timeout}s)"
                ) from exc
            except (ConnectionResetError, OSError) as exc:
                self._sock = None
                raise IsaacConnectionError(
                    f"Connection lost while reading: {exc}"
                ) from exc

            if not data:
                logger.error("Isaac bridge closed the connection (recv returned empty)")
                self._sock = None
                raise IsaacConnectionError("Connection closed by Isaac bridge")

            self._buffer += data

        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


_client: IsaacClient | None = None


def get_client() -> IsaacClient | None:
    """Get or create the global Isaac client.

    Returns None if the bridge is unavailable (graceful degradation).
    """
    global _client  # noqa: PLW0603
    if _client is None:
        _client = IsaacClient()
    try:
        if not _client.is_connected:
            _client.connect(timeout=2.0)
    except IsaacConnectionError as exc:
        logger.warning("Isaac bridge not available: %s", exc)
        return None
    return _client


def reset_client() -> None:
    """Disconnect and reset the global client."""
    global _client  # noqa: PLW0603
    if _client is not None:
        _client.disconnect()
    _client = None
