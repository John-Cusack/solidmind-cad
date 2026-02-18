"""Adapter layer between motion tools and optional Isaac bridge client."""
from __future__ import annotations

from typing import Any

from server.isaac_client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    IsaacCommandError,
    IsaacConnectionError,
    get_client,
)
from server.motion_models import Mechanism


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
        return _error("ISAAC_PROTOCOL_ERROR", "Isaac teleop_start response missing required session_id")

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
