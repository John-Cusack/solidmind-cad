"""Stub command handlers for the Gazebo bridge runtime.

Phase 1 MVP: provides session tracking and stub responses.
Actual Gazebo integration (gz-sim, ros2) will be added in Phase 2.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from gazebo_bridge.models import GazeboSession

logger = logging.getLogger("solidmind.gazebo_runtime")


class GazeboRuntimeError(Exception):
    """Raised when a Gazebo runtime command fails."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class GazeboRuntime:
    """Stub Gazebo runtime with session tracking.

    Command handlers raise ``GazeboRuntimeError`` for unimplemented
    operations.  Teleop lifecycle is tracked in-memory.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, GazeboSession] = {}

    def handle_ping(self) -> dict[str, Any]:
        return {"pong": True}

    def handle_simulate(self, args: dict[str, Any]) -> dict[str, Any]:
        mechanism = args.get("mechanism", {})
        duration_s = float(args.get("duration_s", 1.0))
        part_ids = [p.get("id") for p in mechanism.get("parts", []) if p.get("id")]
        return {
            "time_series": [
                {"t": 0.0, "parts": {pid: {"omega_rpm": 0.0} for pid in part_ids}},
                {"t": duration_s, "parts": {pid: {"omega_rpm": 0.0} for pid in part_ids}},
            ],
            "summary": {
                "simulation_time_s": duration_s,
                "steady_state_speeds": {pid: 0.0 for pid in part_ids},
            },
        }

    def handle_teleop_start(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = f"gz_sess_{secrets.token_hex(4)}"
        session = GazeboSession(
            session_id=session_id,
            session_type="teleop",
            mechanism=args.get("mechanism", {}),
            profile=args.get("profile", {}),
            urdf_path=args.get("urdf_path"),
        )
        self._sessions[session_id] = session
        return {
            "session_id": session_id,
            "status": "started",
        }

    def handle_teleop_command(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._sessions.get(session_id)
        if session is None:
            raise GazeboRuntimeError(
                f"No such session: {session_id}",
                code="GAZEBO_SESSION_NOT_FOUND",
            )
        session.vx_mps = float(args.get("vx_mps", 0.0))
        session.yaw_rate_rps = float(args.get("yaw_rate_rps", 0.0))
        session.body_height_m = float(args.get("body_height_m", 0.0))
        session.vy_mps = float(args.get("vy_mps", 0.0))
        session.vz_mps = float(args.get("vz_mps", 0.0))
        session.tick_count += 1
        return {"applied": True}

    def handle_teleop_state(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._sessions.get(session_id)
        if session is None:
            raise GazeboRuntimeError(
                f"No such session: {session_id}",
                code="GAZEBO_SESSION_NOT_FOUND",
            )
        return {
            "state": {
                "vx_mps": session.vx_mps,
                "yaw_rate_rps": session.yaw_rate_rps,
                "body_height_m": session.body_height_m,
                "vy_mps": session.vy_mps,
                "vz_mps": session.vz_mps,
            },
            "tick_count": session.tick_count,
        }

    def handle_teleop_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._sessions.pop(session_id, None)
        tick_count = session.tick_count if session else 0
        return {"stopped": True, "tick_count": tick_count}
