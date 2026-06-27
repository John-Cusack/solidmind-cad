"""Adapter layer between motion tools and optional Isaac bridge client."""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from server.isaac_client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    IsaacClient,
    IsaacCommandError,
    IsaacConnectionError,
    get_client,
    reset_client,
)
from server.motion_models import Mechanism

logger = logging.getLogger("solidmind.isaac_adapter")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Subprocess handle for the Isaac bridge (managed by launch_bridge/stop_bridge)
_bridge_process: subprocess.Popen[bytes] | None = None


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _bridge_location() -> str:
    return f"{DEFAULT_HOST}:{DEFAULT_PORT}"


def import_urdf(
    urdf_path: str,
    import_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Import a URDF file via the Isaac bridge."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            f"Isaac bridge not running on {_bridge_location()}. Start the Isaac bridge process.",
        )

    try:
        result = client.import_urdf(urdf_path=urdf_path, import_config=import_config)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object import_urdf result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def simulate(
    mechanism: Mechanism,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any] | None = None,
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run batch simulation using the Isaac bridge if available."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            f"Isaac bridge not running on {_bridge_location()}. Start the Isaac bridge process.",
        )

    try:
        result = client.simulate(
            mechanism=mechanism.to_dict(),
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
            profile=profile or {},
            urdf_path=urdf_path,
            import_config=import_config,
        )
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object simulate result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def simulate_start(
    mechanism: Mechanism,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any] | None = None,
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Start a simulation session via the Isaac bridge (non-blocking)."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            f"Isaac bridge not running on {_bridge_location()}. Start the Isaac bridge process.",
        )

    try:
        result = client.simulate_start(
            mechanism=mechanism.to_dict(),
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
            profile=profile or {},
            urdf_path=urdf_path,
            import_config=import_config,
            verify=verify,
        )
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object simulate_start result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def simulate_status(session_id: str) -> dict[str, Any]:
    """Poll simulation session progress from Isaac."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.simulate_status(session_id=session_id)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object simulate_status result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def simulate_stop(session_id: str) -> dict[str, Any]:
    """Stop simulation session and return final samples from Isaac."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.simulate_stop(session_id=session_id)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object simulate_stop result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def teleop_start(
    mechanism: Mechanism,
    profile: dict[str, Any],
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
    verify: bool = True,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Start a teleop session on the Isaac bridge."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            f"Isaac bridge not running on {_bridge_location()}. Start the Isaac bridge process.",
        )

    try:
        result = client.teleop_start(
            mechanism=mechanism.to_dict(),
            profile=profile,
            urdf_path=urdf_path,
            import_config=import_config,
            verify=verify,
            allow_partial=allow_partial,
        )
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object teleop_start result ({type(result).__name__})",
        )

    session_id = str(result.get("session_id", "")).strip()
    if not session_id:
        return _error(
            "ISAAC_PROTOCOL_ERROR", "Isaac teleop_start response missing required session_id"
        )

    result["session_id"] = session_id
    result.setdefault(
        "keyboard_bindings",
        {
            "forward_back": "W/S",
            "turn": "A/D",
            "body_height": "Q/E",
        },
    )
    return {"ok": True, **result}


def teleop_command(
    session_id: str,
    vx_mps: float,
    yaw_rate_rps: float,
    body_height_m: float,
) -> dict[str, Any]:
    """Send one teleop command to Isaac."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.teleop_command(
            session_id=session_id,
            vx_mps=vx_mps,
            yaw_rate_rps=yaw_rate_rps,
            body_height_m=body_height_m,
        )
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object teleop_command result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def teleop_state(session_id: str) -> dict[str, Any]:
    """Read teleop state from Isaac."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.teleop_state(session_id=session_id)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object teleop_state result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def teleop_stop(session_id: str) -> dict[str, Any]:
    """Stop teleop session in Isaac."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.teleop_stop(session_id=session_id)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object teleop_stop result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def isaac_screenshot(
    width: int = 1280,
    height: int = 720,
    camera_position: list[float] | None = None,
    camera_target: list[float] | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    """Capture the Isaac Sim viewport as a base64-encoded PNG."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            f"Isaac bridge not running on {_bridge_location()}. Start the Isaac bridge process.",
        )

    try:
        result = client.screenshot(
            width=width,
            height=height,
            camera_position=camera_position,
            camera_target=camera_target,
            preset=preset,
        )
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error(exc.code or "ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "ISAAC_PROTOCOL_ERROR",
            f"Isaac bridge returned non-object screenshot result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


def _ping_bridge(host: str, port: int) -> bool:
    """Send a ping command to the Isaac bridge and check for pong."""
    try:
        client = IsaacClient(host=host, port=port)
        client.connect(timeout=2.0)
        result = client.ping()
        client.disconnect()
        return result
    except Exception:
        return False


def launch_bridge(
    *,
    headless: bool = False,
    port: int = DEFAULT_PORT,
    environment: str = "full_warehouse.usd",
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Spawn the Isaac bridge as a subprocess and wait for it to accept connections.

    If the bridge is already running (responds to ping), returns early.
    """
    global _bridge_process  # noqa: PLW0603

    host = DEFAULT_HOST

    # Check if bridge is already running
    if _ping_bridge(host, port):
        logger.info("Isaac bridge already running on %s:%d", host, port)
        pid = _bridge_process.pid if _bridge_process and _bridge_process.poll() is None else None
        # Reset client so it reconnects to the (possibly new) bridge
        reset_client()
        return {
            "ok": True,
            "status": "already_running",
            "host": host,
            "port": port,
            "pid": pid,
            "headless": headless,
        }

    # Build launch command
    isaac_python = os.environ.get("ISAAC_PYTHON", "")
    cmd: list[str]
    if isaac_python and os.path.isfile(isaac_python):
        cmd = [isaac_python, "-m", "isaac_bridge.bridge_server"]
    else:
        # Use the shell script which handles ISAAC_PYTHON itself
        script = _PROJECT_ROOT / "scripts" / "run_isaac_bridge.sh"
        if script.is_file():
            cmd = ["bash", str(script)]
        else:
            return _error(
                "ISAAC_LAUNCH_FAILED",
                "Cannot find Isaac bridge launcher. Set ISAAC_PYTHON or ensure "
                "scripts/run_isaac_bridge.sh exists.",
            )

    # Add arguments
    cmd.extend(["--port", str(port)])
    if headless:
        cmd.append("--headless")
    if environment:
        cmd.extend(["--environment", environment])

    logger.info("Launching Isaac bridge: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_PROJECT_ROOT),
        )
    except Exception as exc:
        return _error("ISAAC_LAUNCH_FAILED", f"Failed to spawn Isaac bridge: {exc}")

    _bridge_process = proc

    # Poll until the bridge responds to ping or timeout
    start = time.monotonic()
    poll_interval = 2.0
    while time.monotonic() - start < timeout_s:
        # Check if process died
        if proc.poll() is not None:
            stderr_out = ""
            if proc.stderr:
                try:
                    stderr_out = proc.stderr.read().decode("utf-8", errors="replace")[-500:]
                except Exception:
                    pass
            return _error(
                "ISAAC_LAUNCH_FAILED",
                f"Isaac bridge process exited with code {proc.returncode}. "
                f"Last stderr: {stderr_out}",
            )

        if _ping_bridge(host, port):
            logger.info(
                "Isaac bridge ready on %s:%d (pid=%d, %.1fs)",
                host,
                port,
                proc.pid,
                time.monotonic() - start,
            )
            # Reset client so subsequent calls connect to the new bridge
            reset_client()
            return {
                "ok": True,
                "status": "launched",
                "host": host,
                "port": port,
                "pid": proc.pid,
                "headless": headless,
                "startup_time_s": round(time.monotonic() - start, 1),
            }

        time.sleep(poll_interval)

    # Timeout — kill the process
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except Exception:
        proc.kill()

    return _error(
        "ISAAC_LAUNCH_TIMEOUT",
        f"Isaac bridge did not respond to ping within {timeout_s}s. "
        "Check that Isaac Sim is properly installed and ISAAC_PYTHON is set.",
    )


def stop_bridge() -> dict[str, Any]:
    """Stop the managed Isaac bridge subprocess."""
    global _bridge_process  # noqa: PLW0603

    if _bridge_process is None:
        return {"ok": True, "status": "not_managed", "message": "No managed bridge process"}

    pid = _bridge_process.pid
    if _bridge_process.poll() is not None:
        rc = _bridge_process.returncode
        _bridge_process = None
        reset_client()
        return {"ok": True, "status": "already_exited", "pid": pid, "return_code": rc}

    logger.info("Stopping Isaac bridge (pid=%d)", pid)
    try:
        _bridge_process.send_signal(signal.SIGTERM)
        _bridge_process.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        logger.warning("Isaac bridge did not exit after SIGTERM, sending SIGKILL")
        _bridge_process.kill()
        _bridge_process.wait(timeout=5.0)
    except Exception as exc:
        return _error("ISAAC_STOP_FAILED", f"Failed to stop Isaac bridge: {exc}")

    rc = _bridge_process.returncode
    _bridge_process = None
    reset_client()

    return {"ok": True, "status": "stopped", "pid": pid, "return_code": rc}
