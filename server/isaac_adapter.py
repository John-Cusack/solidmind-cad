"""Adapter layer between motion tools and optional Isaac bridge client."""
from __future__ import annotations

import time
from typing import Any

from server.isaac_client import (
    IsaacCommandError,
    IsaacConnectionError,
    get_client,
)
from server.motion_models import Mechanism


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def simulate(
    mechanism: Mechanism,
    duration_s: float,
    dt_s: float,
    output_interval: float,
) -> dict[str, Any]:
    """Run batch simulation using the Isaac bridge if available."""
    client = get_client()
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            "Isaac bridge not running on localhost:9878. Start the Isaac bridge process.",
        )

    try:
        result = client.simulate(
            mechanism=mechanism.to_dict(),
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error("ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    return {"ok": True, **result}


def teleop_start(
    mechanism: Mechanism,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Start a teleop session on the Isaac bridge."""
    client = get_client()
    if client is None:
        return _error(
            "ISAAC_NOT_CONNECTED",
            "Isaac bridge not running on localhost:9878. Start the Isaac bridge process.",
        )

    try:
        result = client.teleop_start(mechanism=mechanism.to_dict(), profile=profile)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error("ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    session_id = result.get("session_id")
    if not session_id:
        session_id = f"isaac_{mechanism.name}_{int(time.time())}"
        result["session_id"] = session_id

    # Expose default keyboard controls expected by this repo contract.
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
    client = get_client()
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
        return _error("ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    return {"ok": True, **result}


def teleop_state(session_id: str) -> dict[str, Any]:
    """Read teleop state from Isaac."""
    client = get_client()
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.teleop_state(session_id=session_id)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error("ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    return {"ok": True, **result}


def teleop_stop(session_id: str) -> dict[str, Any]:
    """Stop teleop session in Isaac."""
    client = get_client()
    if client is None:
        return _error("ISAAC_NOT_CONNECTED", "Isaac bridge is not connected")

    try:
        result = client.teleop_stop(session_id=session_id)
    except IsaacConnectionError as exc:
        return _error("ISAAC_CONNECTION_LOST", str(exc))
    except IsaacCommandError as exc:
        return _error("ISAAC_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error("ISAAC_ERROR", str(exc))

    return {"ok": True, **result}
