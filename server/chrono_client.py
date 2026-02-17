"""Socket client for communicating with the Chrono daemon.

Same architecture as freecad_client.py — TCP socket client with retry/reconnect.
The Chrono daemon is a standalone C++ executable that listens on localhost:9877
and runs multibody dynamics simulations via Project Chrono.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

logger = logging.getLogger("solidmind.chrono_client")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 120.0  # Simulations can take longer than CAD commands
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class ChronoConnectionError(Exception):
    """Raised when the client cannot connect to the Chrono daemon."""


class ChronoCommandError(Exception):
    """Raised when a command fails on the Chrono side."""


class ChronoClient:
    """TCP client that sends commands to the Chrono daemon socket server."""

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
        """Connect to the Chrono daemon socket server."""
        if self._sock is not None:
            return

        logger.debug("Connecting to Chrono daemon at %s:%d (timeout=%.1fs)",
                      self._host, self._port, timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self._host, self._port))
        except (ConnectionRefusedError, OSError) as e:
            sock.close()
            logger.warning("Connection to Chrono daemon at %s:%d failed: %s",
                           self._host, self._port, e)
            raise ChronoConnectionError(
                f"Cannot connect to Chrono daemon at {self._host}:{self._port}: {e}. "
                "Please start the chrono_daemon binary."
            ) from e

        self._sock = sock
        self._buffer = b""
        logger.info("Connected to Chrono daemon at %s:%d", self._host, self._port)

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
            except ChronoConnectionError as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        "Chrono connection attempt %d/%d failed, retrying in %.1fs",
                        attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)

        raise ChronoConnectionError(
            f"Failed to connect to Chrono daemon after {max_retries} attempts. "
            "Please start the chrono_daemon binary."
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
            logger.info("Disconnected from Chrono daemon")

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

        Raises ``ChronoCommandError`` if the command fails on the Chrono side,
        or ``ChronoConnectionError`` if the connection is lost.
        """
        self._ensure_connected()
        assert self._sock is not None

        message = json.dumps({"cmd": cmd, "args": args}, separators=(",", ":")) + "\n"
        logger.debug("Sending command: %s (payload %d bytes)", cmd, len(message))
        try:
            self._sock.sendall(message.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error("Connection lost while sending '%s': %s", cmd, e)
            self._sock = None
            raise ChronoConnectionError(f"Connection lost while sending: {e}") from e

        response = self._read_response(timeout)
        logger.debug("Response for '%s': ok=%s keys=%s",
                      cmd, response.get("ok"), list(response.keys()))

        if not response.get("ok", False):
            error_msg = response.get("error", "Unknown error")
            logger.error("Command '%s' failed: %s", cmd, error_msg)
            raise ChronoCommandError(error_msg)

        # Surface any warnings from the daemon
        warnings = response.get("warnings")
        if warnings:
            for w in warnings:
                logger.warning("Chrono daemon warning: %s", w)

        return response.get("result")

    def simulate(
        self,
        mechanism: dict[str, Any] | None = None,
        duration_s: float = 1.0,
        dt_s: float = 0.001,
        output_interval: float = 0.01,
        simulation_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a dynamic simulation and return results.

        This is a convenience wrapper around send_command("simulate", ...).
        When *simulation_spec* is provided, it is sent instead of *mechanism*
        (the new Python-planner path using shaft-based Chrono elements).
        """
        kwargs: dict[str, Any] = {
            "duration_s": duration_s,
            "dt_s": dt_s,
            "output_interval": output_interval,
        }
        if simulation_spec is not None:
            n_objects = len(simulation_spec.get("objects", []))
            obj_types = [o.get("type") for o in simulation_spec.get("objects", [])]
            logger.info(
                "simulate: spec with %d objects (%s), duration=%.3fs dt=%.4fs",
                n_objects, ", ".join(obj_types), duration_s, dt_s,
            )
            kwargs["simulation_spec"] = simulation_spec
        elif mechanism is not None:
            logger.info("simulate: legacy mechanism path, duration=%.3fs", duration_s)
            kwargs["mechanism"] = mechanism
        else:
            raise ValueError("Either mechanism or simulation_spec must be provided")

        t0 = time.monotonic()
        result = self.send_command(
            "simulate",
            timeout=max(READ_TIMEOUT, duration_s * 100),
            **kwargs,
        )
        elapsed = time.monotonic() - t0
        logger.info("simulate: completed in %.3fs", elapsed)
        return result

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
                data = self._sock.recv(65536)  # Larger buffer for simulation results
            except socket.timeout as e:
                raise ChronoConnectionError(
                    f"Timed out waiting for Chrono response ({timeout}s)"
                ) from e
            except (ConnectionResetError, OSError) as e:
                self._sock = None
                raise ChronoConnectionError(
                    f"Connection lost while reading: {e}"
                ) from e

            if not data:
                logger.error("Chrono daemon closed the connection (recv returned empty)")
                self._sock = None
                raise ChronoConnectionError("Connection closed by Chrono daemon")

            self._buffer += data
            logger.debug("Received %d bytes (buffer total: %d)", len(data), len(self._buffer))

        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


# Module-level singleton
_client: ChronoClient | None = None


def get_client() -> ChronoClient | None:
    """Get or create the global Chrono client.

    Returns None if the daemon is not running (graceful degradation).
    Unlike FreeCAD client, Chrono is optional — Tier 1 and 2 work without it.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        _client = ChronoClient()
    try:
        if not _client.is_connected:
            _client.connect(timeout=2.0)
    except ChronoConnectionError as exc:
        logger.warning("Chrono daemon not available: %s", exc)
        return None
    return _client


def reset_client() -> None:
    """Disconnect and reset the global client."""
    global _client  # noqa: PLW0603
    if _client is not None:
        _client.disconnect()
        _client = None
