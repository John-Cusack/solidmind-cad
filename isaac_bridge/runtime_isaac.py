"""Isaac bridge runtime.

The runtime exposes deterministic command handlers for the bridge protocol.
When Omniverse Isaac APIs are available, a minimal physics stepping path is used.
Otherwise, a deterministic analytical fallback is used for local/CI execution.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from isaac_bridge.models import SUPPORTED_JOINT_TYPES, TeleopSession


class IsaacRuntimeError(Exception):
    """Runtime-level error with structured code/message/details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class _EngineResult:
    mode: str
    steps: int
    warning: str | None = None


class _IsaacWorldEngine:
    """Optional minimal integration with Isaac Sim APIs."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._available = False
        self._world_type: Any = None
        self._detect()

    @property
    def available(self) -> bool:
        return self._available

    def _detect(self) -> None:
        try:
            from omni.isaac.core import World  # type: ignore[import-not-found]
        except Exception:
            self._available = False
            return
        self._world_type = World
        self._available = True

    def run(self, *, duration_s: float, dt_s: float) -> _EngineResult:
        if not self._available:
            return _EngineResult(mode="reference", steps=max(1, int(math.ceil(duration_s / dt_s))))
        try:
            world = self._world_type(  # type: ignore[operator]
                stage_units_in_meters=1.0,
                physics_dt=dt_s,
                rendering_dt=dt_s,
            )
            n_steps = max(1, int(math.ceil(duration_s / dt_s)))
            for _ in range(n_steps):
                world.step(render=False)
            return _EngineResult(mode="isaac", steps=n_steps)
        except Exception as exc:
            return _EngineResult(
                mode="reference",
                steps=max(1, int(math.ceil(duration_s / dt_s))),
                warning=f"Isaac runtime stepping failed, fell back to reference mode: {exc}",
            )


class IsaacRuntime:
    """Command runtime used by the bridge server."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._sessions: dict[str, TeleopSession] = {}
        self._lock = threading.RLock()
        self._engine = _IsaacWorldEngine(headless=headless)

    def ping(self) -> dict[str, Any]:
        return {
            "pong": True,
            "bridge_version": "1.0.0",
            "capabilities": {
                "commands": [
                    "ping",
                    "simulate",
                    "teleop_start",
                    "teleop_command",
                    "teleop_state",
                    "teleop_stop",
                ],
                "supported_joint_types": sorted(SUPPORTED_JOINT_TYPES),
                "headless_default": self._headless,
                "isaac_available": self._engine.available,
            },
        }

    def simulate(
        self,
        *,
        mechanism: dict[str, Any],
        duration_s: float,
        dt_s: float,
        output_interval: float,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mech = _validate_mechanism(mechanism)
        _validate_sim_args(
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
        unsupported = _unsupported_joints(mech)
        if unsupported:
            raise IsaacRuntimeError(
                "UNSUPPORTED_JOINT_TYPE",
                "Mechanism contains unsupported joint types for Isaac bridge v1",
                details={
                    "unsupported_joints": unsupported,
                    "supported_joint_types": sorted(SUPPORTED_JOINT_TYPES),
                },
            )

        part_ids = [
            p["id"]
            for p in mech.get("parts", [])
            if isinstance(p, dict) and isinstance(p.get("id"), str)
        ]
        speeds = _steady_state_speeds(mech)
        sample_times = _sample_times(duration_s=duration_s, output_interval=output_interval)
        time_series = [
            {
                "t": t,
                "parts": {pid: {"omega_rpm": float(speeds.get(pid, 0.0))} for pid in part_ids},
            }
            for t in sample_times
        ]
        engine_result = self._engine.run(duration_s=duration_s, dt_s=dt_s)
        result: dict[str, Any] = {
            "time_series": time_series,
            "summary": {
                "simulation_time_s": duration_s,
                "time_steps": engine_result.steps,
                "output_samples": len(sample_times),
                "steady_state_speeds": {pid: float(speeds.get(pid, 0.0)) for pid in part_ids},
                "engine_mode": engine_result.mode,
            },
            "profile_used": dict(profile or {}),
        }
        if engine_result.warning:
            result["warnings"] = [engine_result.warning]
        return result

    def teleop_start(
        self,
        *,
        mechanism: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mech = _validate_mechanism(mechanism)
        unsupported = _unsupported_joints(mech)
        if unsupported:
            raise IsaacRuntimeError(
                "UNSUPPORTED_JOINT_TYPE",
                "Mechanism contains unsupported joint types for Isaac bridge v1",
                details={
                    "unsupported_joints": unsupported,
                    "supported_joint_types": sorted(SUPPORTED_JOINT_TYPES),
                },
            )
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = time.time()
        session = TeleopSession(
            session_id=session_id,
            mechanism=mech,
            profile=dict(profile or {}),
            started_at_s=now,
        )
        with self._lock:
            self._sessions[session_id] = session
        return {
            "session_id": session_id,
            "status": "started",
            "keyboard_bindings": {
                "forward_back": "W/S",
                "turn": "A/D",
                "body_height": "Q/E",
            },
            "state": session.state.to_dict(),
            "profile_used": dict(profile or {}),
        }

    def teleop_command(
        self,
        *,
        session_id: str,
        vx_mps: float,
        yaw_rate_rps: float,
        body_height_m: float,
    ) -> dict[str, Any]:
        _validate_finite("vx_mps", vx_mps)
        _validate_finite("yaw_rate_rps", yaw_rate_rps)
        _validate_finite("body_height_m", body_height_m)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise IsaacRuntimeError(
                    "ISAAC_UNKNOWN_SESSION",
                    f"unknown session {session_id}",
                )
            session.state.vx_mps = float(vx_mps)
            session.state.yaw_rate_rps = float(yaw_rate_rps)
            session.state.body_height_m = float(body_height_m)
            state = session.state.to_dict()
        return {"applied": True, "state": state}

    def teleop_state(self, *, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise IsaacRuntimeError(
                    "ISAAC_UNKNOWN_SESSION",
                    f"unknown session {session_id}",
                )
            state = session.state.to_dict()
            uptime_s = max(0.0, time.time() - session.started_at_s)
        return {"state": state, "uptime_s": uptime_s}

    def teleop_stop(self, *, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return {"stopped": True, "already_stopped": True}
        return {"stopped": True}


def _validate_mechanism(mechanism: dict[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(mechanism, dict):
        raise IsaacRuntimeError("INVALID_MECHANISM", "mechanism must be an object")
    parts = mechanism.get("parts")
    joints = mechanism.get("joints")
    if not isinstance(parts, list):
        raise IsaacRuntimeError("INVALID_MECHANISM", "mechanism.parts must be an array")
    if not isinstance(joints, list):
        raise IsaacRuntimeError("INVALID_MECHANISM", "mechanism.joints must be an array")
    return mechanism


def _unsupported_joints(mechanism: dict[str, Any]) -> list[dict[str, str]]:
    unsupported: list[dict[str, str]] = []
    joints = mechanism.get("joints", [])
    if not isinstance(joints, list):
        return unsupported
    for index, joint in enumerate(joints):
        if not isinstance(joint, dict):
            unsupported.append({"id": f"index_{index}", "joint_type": "unknown"})
            continue
        joint_type = str(joint.get("joint_type", "")).strip().lower()
        if joint_type not in SUPPORTED_JOINT_TYPES:
            joint_id = str(joint.get("id", f"index_{index}"))
            unsupported.append({"id": joint_id, "joint_type": joint_type or "unknown"})
    return unsupported


def _validate_finite(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise IsaacRuntimeError("INVALID_INPUT", f"{name} must be a finite number")
    return float(value)


def _validate_sim_args(*, duration_s: float, dt_s: float, output_interval: float) -> None:
    duration = _validate_finite("duration_s", duration_s)
    dt = _validate_finite("dt_s", dt_s)
    out = _validate_finite("output_interval", output_interval)
    if duration <= 0:
        raise IsaacRuntimeError("INVALID_INPUT", "duration_s must be > 0")
    if dt <= 0:
        raise IsaacRuntimeError("INVALID_INPUT", "dt_s must be > 0")
    if out <= 0:
        raise IsaacRuntimeError("INVALID_INPUT", "output_interval must be > 0")
    if out < dt:
        raise IsaacRuntimeError("INVALID_INPUT", "output_interval must be >= dt_s")
    if out > duration:
        raise IsaacRuntimeError("INVALID_INPUT", "output_interval must be <= duration_s")


def _sample_times(*, duration_s: float, output_interval: float) -> list[float]:
    n = max(1, int(math.floor(duration_s / output_interval)))
    out = [round(i * output_interval, 9) for i in range(n + 1)]
    if out[-1] < duration_s:
        out.append(round(duration_s, 9))
    else:
        out[-1] = round(duration_s, 9)
    return out


def _steady_state_speeds(mechanism: dict[str, Any]) -> dict[str, float]:
    part_ids = [
        p.get("id")
        for p in mechanism.get("parts", [])
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    ]
    speeds = {pid: 0.0 for pid in part_ids if isinstance(pid, str)}
    drives = mechanism.get("drives", [])
    joints = mechanism.get("joints", [])
    if not isinstance(drives, list) or not isinstance(joints, list):
        return speeds
    joint_by_id = {
        str(j.get("id")): j
        for j in joints
        if isinstance(j, dict) and isinstance(j.get("id"), str)
    }
    for drive in drives:
        if not isinstance(drive, dict):
            continue
        speed = drive.get("speed_rpm")
        if not isinstance(speed, (int, float)) or not math.isfinite(float(speed)):
            continue
        joint_id = drive.get("joint_id")
        if not isinstance(joint_id, str):
            continue
        joint = joint_by_id.get(joint_id)
        if not isinstance(joint, dict):
            continue
        child = joint.get("child_part")
        parent = joint.get("parent_part")
        if isinstance(child, str):
            speeds[child] = float(speed)
        if isinstance(parent, str) and parent not in speeds:
            speeds[parent] = float(speed)
    return speeds
