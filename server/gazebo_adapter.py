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


def _normalize_simulate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize batch simulation result keys from the bridge contract."""
    normalized = dict(result)
    if "time_series" not in normalized and isinstance(normalized.get("samples"), list):
        normalized["time_series"] = normalized["samples"]
    summary = normalized.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    if "simulation_time_s" not in summary:
        if normalized.get("time_series"):
            try:
                summary["simulation_time_s"] = float(normalized["time_series"][-1].get("t", 0.0))
            except Exception:
                pass
    normalized["summary"] = summary
    return normalized


def simulate(
    mechanism: Mechanism,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any] | None = None,
    urdf_path: str | None = None,
    sdf_path: str | None = None,
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
            sdf_path=sdf_path,
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
    return {"ok": True, **_normalize_simulate_result(result)}


def teleop_start(
    mechanism: Mechanism,
    profile: dict[str, Any],
    urdf_path: str | None = None,
    sdf_path: str | None = None,
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
            sdf_path=sdf_path,
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


def spawn_model(
    *,
    model_name: str | None = None,
    urdf_path: str | None = None,
    sdf_path: str | None = None,
    world_name: str | None = None,
) -> dict[str, Any]:
    """Spawn a model through the Gazebo bridge."""
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
        result = client.spawn_model(
            model_name=model_name,
            urdf_path=urdf_path,
            sdf_path=sdf_path,
            world_name=world_name,
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
            f"Gazebo bridge returned non-object spawn_model result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def diagnose(world_name: str | None = None) -> dict[str, Any]:
    """Get runtime diagnostics from the Gazebo bridge."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.diagnose(world_name=world_name)
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object diagnose result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def px4_start(
    *,
    binary: str | None = None,
    args: list[str] | None = None,
    system_address: str | None = None,
) -> dict[str, Any]:
    """Start PX4 SITL through the Gazebo bridge runtime."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.px4_start(binary=binary, args=args, system_address=system_address)
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object px4_start result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def px4_status() -> dict[str, Any]:
    """Read PX4 lifecycle status from Gazebo bridge."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.px4_status()
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object px4_status result ({type(result).__name__})",
        )
    return {"ok": True, **result}


def px4_stop() -> dict[str, Any]:
    """Stop PX4 lifecycle through Gazebo bridge."""
    try:
        client = get_client()
    except Exception as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    if client is None:
        return _error("GAZEBO_NOT_CONNECTED", "Gazebo bridge is not connected")

    try:
        result = client.px4_stop()
    except GazeboConnectionError as exc:
        return _error("GAZEBO_CONNECTION_LOST", str(exc))
    except GazeboCommandError as exc:
        return _error(exc.code or "GAZEBO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("GAZEBO_COMMAND_ERROR", str(exc))

    if not isinstance(result, dict):
        return _error(
            "GAZEBO_PROTOCOL_ERROR",
            f"Gazebo bridge returned non-object px4_stop result ({type(result).__name__})",
        )
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
