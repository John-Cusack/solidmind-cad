"""Socket client for optional Isaac simulation bridge.

Mirrors the Chrono client pattern so motion tools can route dynamic simulation
requests to an Isaac sidecar when available.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from typing import Any

logger = logging.getLogger("solidmind.isaac_client")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Non-positive %s=%r, using default %d", name, raw, default)
        return default
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %.3f", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Non-positive %s=%r, using default %.3f", name, raw, default)
        return default
    return value


DEFAULT_HOST = os.environ.get("SOLIDMIND_ISAAC_HOST", "127.0.0.1")
DEFAULT_PORT = _env_int("SOLIDMIND_ISAAC_PORT", 9878)
CONNECT_TIMEOUT = _env_float("SOLIDMIND_ISAAC_CONNECT_TIMEOUT_S", 5.0)
READ_TIMEOUT = _env_float("SOLIDMIND_ISAAC_READ_TIMEOUT_S", 120.0)
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class IsaacConnectionError(Exception):
    """Raised when the client cannot connect to the Isaac bridge."""


class IsaacCommandError(Exception):
    """Raised when a command fails on the Isaac bridge."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class IsaacClient:
    """TCP client that sends commands to the Isaac bridge socket server."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self._host = host or os.environ.get("SOLIDMIND_ISAAC_HOST", DEFAULT_HOST)
        self._port = port if port is not None else _env_int("SOLIDMIND_ISAAC_PORT", DEFAULT_PORT)
        self._connect_timeout = _env_float("SOLIDMIND_ISAAC_CONNECT_TIMEOUT_S", CONNECT_TIMEOUT)
        self._read_timeout = _env_float("SOLIDMIND_ISAAC_READ_TIMEOUT_S", READ_TIMEOUT)
        self._sock: socket.socket | None = None
        self._buffer = b""

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self, timeout: float | None = None) -> None:
        """Connect to the Isaac bridge socket server."""
        if self._sock is not None:
            return
        effective_timeout = self._connect_timeout if timeout is None else timeout

        logger.debug(
            "Connecting to Isaac bridge at %s:%d (timeout=%.1fs)",
            self._host,
            self._port,
            effective_timeout,
        )
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(effective_timeout)
            sock.connect((self._host, self._port))
        except (ConnectionRefusedError, OSError) as exc:
            if "sock" in locals():
                try:
                    sock.close()
                except OSError:
                    pass
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
        timeout: float | None = None,
        **args: Any,
    ) -> Any:
        """Send a command and return result data."""
        self._ensure_connected()
        assert self._sock is not None
        effective_timeout = self._read_timeout if timeout is None else timeout

        message = json.dumps({"cmd": cmd, "args": args}, separators=(",", ":")) + "\n"
        logger.debug("Sending command: %s (payload %d bytes)", cmd, len(message))
        try:
            self._sock.sendall(message.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.error("Connection lost while sending '%s': %s", cmd, exc)
            self._sock = None
            raise IsaacConnectionError(f"Connection lost while sending: {exc}") from exc

        response = self._read_response(effective_timeout)
        if not isinstance(response, dict):
            raise IsaacCommandError(
                f"Malformed response type {type(response).__name__} for command '{cmd}'",
                code="ISAAC_PROTOCOL_ERROR",
            )
        logger.debug(
            "Response for '%s': ok=%s keys=%s",
            cmd,
            response.get("ok"),
            list(response.keys()),
        )

        if not response.get("ok", False):
            err = response.get("error", "Unknown error")
            code: str | None = None
            if isinstance(err, dict):
                code_val = err.get("code")
                if isinstance(code_val, str) and code_val:
                    code = code_val
                msg_val = err.get("message")
                if isinstance(msg_val, str) and msg_val.strip():
                    error_msg = msg_val.strip()
                else:
                    error_msg = json.dumps(err, sort_keys=True)
            elif isinstance(err, str):
                error_msg = err
            else:
                error_msg = str(err)
            logger.error("Command '%s' failed: %s (code=%s)", cmd, error_msg, code)
            raise IsaacCommandError(error_msg, code=code)

        return response.get("result")

    def import_urdf(
        self,
        urdf_path: str,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import a URDF file into the Isaac scene."""
        kwargs: dict[str, Any] = {"urdf_path": urdf_path}
        if import_config:
            kwargs["import_config"] = import_config
        return self.send_command("import_urdf", **kwargs)

    def simulate(
        self,
        mechanism: dict[str, Any],
        duration_s: float = 1.0,
        dt_s: float = 0.001,
        output_interval: float = 0.01,
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run batch simulation and return summary/time-series data."""
        kwargs: dict[str, Any] = {
            "mechanism": mechanism,
            "duration_s": duration_s,
            "dt_s": dt_s,
            "output_interval": output_interval,
            "profile": profile or {},
        }
        if urdf_path is not None:
            kwargs["urdf_path"] = urdf_path
        if import_config is not None:
            kwargs["import_config"] = import_config
        return self.send_command(
            "simulate",
            timeout=max(self._read_timeout, duration_s * 100),
            **kwargs,
        )

    def simulate_start(
        self,
        mechanism: dict[str, Any],
        duration_s: float = 1.0,
        dt_s: float = 0.001,
        output_interval: float = 0.01,
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Start a simulation session (non-blocking)."""
        kwargs: dict[str, Any] = {
            "mechanism": mechanism,
            "duration_s": duration_s,
            "dt_s": dt_s,
            "output_interval": output_interval,
            "profile": profile or {},
            "verify": verify,
        }
        if urdf_path is not None:
            kwargs["urdf_path"] = urdf_path
        if import_config is not None:
            kwargs["import_config"] = import_config
        return self.send_command("simulate_start", **kwargs)

    def simulate_status(self, session_id: str) -> dict[str, Any]:
        """Poll simulation session progress."""
        return self.send_command("simulate_status", session_id=session_id)

    def simulate_stop(self, session_id: str) -> dict[str, Any]:
        """Stop simulation session and return final samples."""
        return self.send_command("simulate_stop", session_id=session_id)

    def teleop_start(
        self,
        mechanism: dict[str, Any],
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Start a teleop session in Isaac."""
        kwargs: dict[str, Any] = {
            "mechanism": mechanism,
            "profile": profile or {},
            "verify": verify,
        }
        if urdf_path is not None:
            kwargs["urdf_path"] = urdf_path
        if import_config is not None:
            kwargs["import_config"] = import_config
        return self.send_command("teleop_start", **kwargs)

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

    def screenshot(
        self,
        width: int = 1280,
        height: int = 720,
        camera_position: list[float] | None = None,
        camera_target: list[float] | None = None,
        preset: str | None = None,
    ) -> dict[str, Any]:
        """Capture the Isaac Sim viewport as a base64-encoded PNG."""
        kwargs: dict[str, Any] = {"width": width, "height": height}
        if camera_position is not None:
            kwargs["camera_position"] = camera_position
        if camera_target is not None:
            kwargs["camera_target"] = camera_target
        if preset is not None:
            kwargs["preset"] = preset
        return self.send_command("screenshot", **kwargs)

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
    except (IsaacConnectionError, OSError) as exc:
        logger.warning("Isaac bridge not available: %s", exc)
        return None
    return _client


def reset_client() -> None:
    """Disconnect and reset the global client."""
    global _client  # noqa: PLW0603
    if _client is not None:
        _client.disconnect()
    _client = None
