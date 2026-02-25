"""Adapter layer between motion tools and optional Gazebo bridge client."""
from __future__ import annotations

from typing import Any

from server.gazebo_client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    GazeboCommandError,
    GazeboConnectionError,
    get_client,
)
from server.motion_models import Mechanism


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _bridge_location() -> str:
    return f"{DEFAULT_HOST}:{DEFAULT_PORT}"


def simulate(
    mechanism: Mechanism,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any] | None = None,
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run batch simulation using the Gazebo bridge if available.

    Single-call synchronous — no session lifecycle polling needed.
    """
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "GAZEBO_NOT_CONNECTED",
            f"Gazebo bridge not running on {_bridge_location()}. Start the Gazebo bridge process.",
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
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object simulate result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def teleop_start(
    mechanism: Mechanism,
    profile: dict[str, Any],
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Start a teleop session on the Gazebo bridge."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error(
            "GAZEBO_NOT_CONNECTED",
            f"Gazebo bridge not running on {_bridge_location()}. Start the Gazebo bridge process.",
        )

    try:
        result = client.teleop_start(
            mechanism=mechanism.to_dict(),
            profile=profile,
            urdf_path=urdf_path,
            import_config=import_config,
            verify=verify,
        )
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object teleop_start result ({type(result).__name__})",
        )

    session_id = str(result.get("session_id", "")).strip()
    if not session_id:
        return _error("GAZEBO_PROTOCOL_ERROR", "Gazebo teleop_start response missing required session_id")

    result["session_id"] = session_id
    return {"ok": True, **result}


def teleop_command(
    session_id: str,
    vx_mps: float,
    yaw_rate_rps: float,
    body_height_m: float,
    vy_mps: float = 0.0,
    vz_mps: float = 0.0,
) -> dict[str, Any]:
    """Send one teleop command to Gazebo (includes vy_mps and vz_mps)."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.teleop_command(
            session_id=session_id,
            vx_mps=vx_mps,
            yaw_rate_rps=yaw_rate_rps,
            body_height_m=body_height_m,
            vy_mps=vy_mps,
            vz_mps=vz_mps,
        )
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object teleop_command result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def teleop_state(session_id: str) -> dict[str, Any]:
    """Read teleop state from Gazebo."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.teleop_state(session_id=session_id)
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object teleop_state result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def teleop_stop(session_id: str) -> dict[str, Any]:
    """Stop teleop session in Gazebo."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.teleop_stop(session_id=session_id)
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object teleop_stop result ({type(result).__name__})",
        )
    return {"ok": True, **result}
