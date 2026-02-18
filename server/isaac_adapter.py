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


def simulate(
    mechanism: Mechanism,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any] | None = None,
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


def teleop_start(
    mechanism: Mechanism,
    profile: dict[str, Any],
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
        result = client.teleop_start(mechanism=mechanism.to_dict(), profile=profile)
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
