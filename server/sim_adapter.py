"""One adapter between the tool façade and any engine.

``isaac_adapter`` and ``gazebo_adapter`` were the same forty lines repeated per
verb per engine: fetch a client, send a command, translate three exception
types into a result dict.  With one client and one contract there is one
adapter, and adding an engine adds none of this.

Everything here is engine-agnostic. Where behaviour differs between engines it
is chosen from the ``hello`` handshake (does it advertise sessions? teleop?),
never from the engine's name.
"""

from __future__ import annotations

import logging
from typing import Any

from server.engine_client import (
    EngineCommandError,
    EngineConnectionError,
    get_client,
)
from server.engine_registry import install_hint, resolve_host, resolve_port

logger = logging.getLogger("solidmind.sim_adapter")

#: Returned when the engine isn't running.  Callers turn this into guidance.
NOT_CONNECTED = "ENGINE_NOT_CONNECTED"
CONNECTION_LOST = "ENGINE_CONNECTION_LOST"
COMMAND_ERROR = "ENGINE_COMMAND_ERROR"
PROTOCOL_ERROR = "ENGINE_PROTOCOL_ERROR"


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def bridge_location(engine: str) -> str:
    return f"{resolve_host(engine)}:{resolve_port(engine)}"


def not_connected_message(engine: str) -> str:
    hint = install_hint(engine)
    message = (
        f"The {engine} engine is not running on {bridge_location(engine)}. "
        f"Start it with sim.start_engine('{engine}')."
    )
    return f"{message} {hint}" if hint else message


def call(
    engine: str,
    cmd: str,
    *,
    timeout: float | None = None,
    **args: Any,
) -> dict[str, Any]:
    """Send *cmd* to *engine* and return ``{"ok": True, **result}`` or an error.

    ``None`` arguments are dropped rather than sent, so an engine only ever
    sees the options a caller actually set.
    """
    payload = {k: v for k, v in args.items() if v is not None}

    try:
        client = get_client(engine)
    except Exception as exc:  # noqa: BLE001 — registry/socket setup
        return _error(CONNECTION_LOST, str(exc))
    if client is None:
        return _error(NOT_CONNECTED, not_connected_message(engine))

    try:
        result = client.send_command(cmd, timeout=timeout, **payload)
    except EngineConnectionError as exc:
        return _error(CONNECTION_LOST, str(exc))
    except EngineCommandError as exc:
        return _error(exc.code or COMMAND_ERROR, str(exc))
    except Exception as exc:  # noqa: BLE001 — never leak a raw traceback
        logger.exception("Unhandled error calling %s.%s", engine, cmd)
        return _error(COMMAND_ERROR, str(exc))

    if result is None:
        return {"ok": True}
    if not isinstance(result, dict):
        return _error(
            PROTOCOL_ERROR,
            f"The {engine} engine returned a non-object {cmd} result ({type(result).__name__})",
        )
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Capability queries
# ---------------------------------------------------------------------------


def capabilities(engine: str) -> dict[str, Any]:
    """What *engine* says it can do, or ``{}`` when it can't be asked."""
    client = get_client(engine)
    if client is None:
        return {}
    return client.capabilities()


def supports_mode(engine: str, mode: str) -> bool | None:
    """True/False when the engine answers, None when it is unreachable."""
    client = get_client(engine)
    if client is None:
        return None
    caps = client.capabilities()
    if not caps:
        return None
    return mode in (caps.get("modes") or [])


def supports_feature(engine: str, feature: str) -> bool | None:
    client = get_client(engine)
    if client is None:
        return None
    caps = client.capabilities()
    if not caps:
        return None
    return feature in (caps.get("features") or [])


def teleop_dofs(engine: str) -> list[str] | None:
    """Teleop axes the engine accepts, or None when it doesn't say.

    Engines that implement teleop declare the command axes they honour, so
    core can reject a lateral-velocity command to a robot that only walks
    forward instead of silently dropping it.
    """
    caps = capabilities(engine)
    dofs = caps.get("teleop_dofs")
    if isinstance(dofs, list) and all(isinstance(d, str) for d in dofs):
        return list(dofs)
    return None


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def normalize_simulate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Fill in the result fields the schema wants but an engine may omit."""
    normalized = dict(result)
    if "time_series" not in normalized and isinstance(normalized.get("samples"), list):
        normalized["time_series"] = normalized["samples"]
    summary = normalized.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    if "simulation_time_s" not in summary and normalized.get("time_series"):
        try:
            summary["simulation_time_s"] = float(normalized["time_series"][-1].get("t", 0.0))
        except (AttributeError, TypeError, ValueError):
            pass
    normalized["summary"] = summary
    return normalized
