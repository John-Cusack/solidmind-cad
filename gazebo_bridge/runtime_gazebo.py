"""Gazebo bridge runtime handlers.

Supports two runtime modes:
- ``real`` (default): uses Gazebo CLI/service calls when available.
- ``stub``: deterministic in-memory fallback for tests and local dev.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from gazebo_bridge.controllers import (
    ControllerError,
    MultirotorDirectController,
    Px4OffboardController,
    create_controller,
)
from gazebo_bridge.models import GazeboSession
from gazebo_bridge.px4_integration import Px4Error, Px4Manager

logger = logging.getLogger("solidmind.gazebo_runtime")


class GazeboRuntimeError(Exception):
    """Raised when a Gazebo runtime command fails."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class _SupportsRun(Protocol):
    def __call__(self, cmd: list[str]) -> tuple[int, str, str]:
        ...


def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _normalize_world_name(args: dict[str, Any], default: str) -> str:
    world_name = str(args.get("world_name", "")).strip()
    if not world_name:
        return default
    return world_name


def _resolve_model_path(args: dict[str, Any]) -> tuple[str, str]:
    sdf_path = str(args.get("sdf_path", "")).strip()
    urdf_path = str(args.get("urdf_path", "")).strip()
    explicit = str(args.get("path", "")).strip()

    if sdf_path:
        return sdf_path, "sdf"
    if urdf_path:
        return urdf_path, "urdf"
    if explicit:
        fmt = str(args.get("format", "")).strip().lower() or (
            "sdf" if explicit.endswith(".sdf") else "urdf"
        )
        return explicit, fmt
    raise GazeboRuntimeError(
        "spawn_model requires sdf_path, urdf_path, or path.",
        code="GAZEBO_SPAWN_FAILED",
    )


class StubGazeboRuntime:
    """Deterministic in-memory runtime implementation."""

    def __init__(self, *, world_name: str = "default", enable_px4: bool = False) -> None:
        self._world_name = world_name
        self._sessions: dict[str, GazeboSession] = {}
        self._session_controllers: dict[str, MultirotorDirectController | Px4OffboardController] = {}
        self._entity_counter = 0
        self._px4 = Px4Manager(enabled=enable_px4)

    def handle_ping(self) -> dict[str, Any]:
        return {"pong": True}

    def handle_diagnose(self, args: dict[str, Any]) -> dict[str, Any]:
        world_name = _normalize_world_name(args, self._world_name)
        return {
            "runtime_mode": "stub",
            "connected": True,
            "world_name": world_name,
            "active_sessions": len(self._sessions),
            "sessions": sorted(self._sessions.keys()),
            "px4": self._px4.status(),
        }

    def handle_spawn_model(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str, fmt = _resolve_model_path(args)
        path = Path(path_str)
        if not path.exists():
            raise GazeboRuntimeError(
                f"Model path does not exist: {path_str}",
                code="GAZEBO_SPAWN_FAILED",
            )
        self._entity_counter += 1
        model_name = str(args.get("model_name", "")).strip() or path.stem
        world_name = _normalize_world_name(args, self._world_name)
        return {
            "spawned": True,
            "entity_id": self._entity_counter,
            "model_name": model_name,
            "world_name": world_name,
            "source_path": str(path),
            "source_format": fmt,
        }

    def handle_simulate(self, args: dict[str, Any]) -> dict[str, Any]:
        duration_s = float(args.get("duration_s", 1.0))
        dt_s = float(args.get("dt_s", 0.01))
        output_interval = float(args.get("output_interval", 0.05))
        mechanism = args.get("mechanism", {}) if isinstance(args.get("mechanism"), dict) else {}
        world_name = _normalize_world_name(args, self._world_name)
        part_ids = [p.get("id") for p in mechanism.get("parts", []) if isinstance(p, dict) and p.get("id")]

        # Extract joint info for force/torque estimation
        joints = mechanism.get("joints", []) if isinstance(mechanism.get("joints"), list) else []
        joint_ids = [j.get("id", "") for j in joints if isinstance(j, dict)]

        samples_count = max(2, int(duration_s / max(output_interval, dt_s)) + 1)
        time_series: list[dict[str, Any]] = []
        for idx in range(samples_count):
            t = min(duration_s, idx * output_interval)
            scale = 0.0 if duration_s <= 0 else min(1.0, t / duration_s)
            part_state = {pid: {"omega_rpm": round(120.0 * scale, 6)} for pid in part_ids}
            entry: dict[str, Any] = {"t": round(t, 6), "parts": part_state}
            # Stub joint efforts — deterministic ramp for testing
            if joint_ids:
                entry["joint_efforts"] = [round(5.0 * scale, 6) for _ in joint_ids]
            time_series.append(entry)

        # Build peak joint forces from final sample
        peak_joint_forces: dict[str, float] = {}
        for i, jid in enumerate(joint_ids):
            peak_joint_forces[jid] = 5.0  # stub steady-state effort

        return {
            "time_series": time_series,
            "summary": {
                "simulation_time_s": duration_s,
                "dt_s": dt_s,
                "output_interval": output_interval,
                "steady_state_speeds": {pid: 120.0 for pid in part_ids},
                "peak_joint_forces": peak_joint_forces,
                "engine_mode": "stub",
                "world_name": world_name,
            },
        }

    def handle_teleop_start(self, args: dict[str, Any]) -> dict[str, Any]:
        profile = args.get("profile", {})
        if profile is None:
            profile = {}
        if not isinstance(profile, dict):
            raise GazeboRuntimeError("profile must be an object", code="INVALID_INPUT")

        controller_type = str(profile.get("controller_type", "multirotor_direct")).strip().lower()
        try:
            controller = create_controller(controller_type, profile)
        except ControllerError as exc:
            raise GazeboRuntimeError(str(exc), code=exc.code) from exc

        if controller_type == "px4_offboard" and not self._px4.status().get("running", False):
            raise GazeboRuntimeError(
                "PX4 offboard controller selected but PX4 is not running.",
                code="GAZEBO_PX4_NOT_READY",
            )

        session_id = f"gz_sess_{secrets.token_hex(4)}"
        model_name = str(args.get("model_name", "")).strip() or f"model_{session_id[-4:]}"
        world_name = _normalize_world_name(args, self._world_name)
        session = GazeboSession(
            session_id=session_id,
            session_type="teleop",
            mechanism=args.get("mechanism", {}),
            profile=profile,
            world_name=world_name,
            model_name=model_name,
            entity_id=int(args.get("entity_id", 0)) or None,
            urdf_path=(str(args.get("urdf_path")) if args.get("urdf_path") is not None else None),
            sdf_path=(str(args.get("sdf_path")) if args.get("sdf_path") is not None else None),
            controller_type=controller_type,
            status="running",
        )
        self._sessions[session_id] = session
        self._session_controllers[session_id] = controller
        return {
            "session_id": session_id,
            "status": "started",
            "controller_type": controller_type,
            "world_name": world_name,
            "model_name": model_name,
            "profile_used": dict(profile),
        }

    def handle_teleop_command(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        session = self._sessions.get(session_id)
        if session is None:
            raise GazeboRuntimeError(
                f"No such session: {session_id}",
                code="GAZEBO_SESSION_NOT_FOUND",
            )
        controller = self._session_controllers.get(session_id)
        if controller is None:
            raise GazeboRuntimeError(
                f"No controller for session: {session_id}",
                code="GAZEBO_PROTOCOL_ERROR",
            )

        vx_mps = float(args.get("vx_mps", 0.0))
        vy_mps = float(args.get("vy_mps", 0.0))
        vz_mps = float(args.get("vz_mps", 0.0))
        yaw_rate_rps = float(args.get("yaw_rate_rps", 0.0))
        body_height_m = float(args.get("body_height_m", 0.0))
        dt_s = float(args.get("dt_s", 0.02))

        session.vx_mps = vx_mps
        session.vy_mps = vy_mps
        session.vz_mps = vz_mps
        session.yaw_rate_rps = yaw_rate_rps
        session.body_height_m = body_height_m
        session.tick_count += 1
        session.sim_time_s += dt_s
        x_m, y_m, z_m = session.position_xyz_m
        x_m += vx_mps * dt_s
        y_m += vy_mps * dt_s
        z_m = max(0.0, z_m + vz_mps * dt_s)
        session.position_xyz_m = (x_m, y_m, z_m)
        session.yaw_rad += yaw_rate_rps * dt_s

        if isinstance(controller, MultirotorDirectController):
            session.rotor_setpoints = controller.command_to_rotors(
                vx_mps=vx_mps,
                vy_mps=vy_mps,
                vz_mps=vz_mps,
                yaw_rate_rps=yaw_rate_rps,
            )
        else:
            if not self._px4.status().get("running", False):
                raise GazeboRuntimeError(
                    "PX4 session is not running for px4_offboard teleop command.",
                    code="GAZEBO_PX4_NOT_READY",
                )
            session.rotor_setpoints = controller.command_to_setpoint(
                vx_mps=vx_mps,
                vy_mps=vy_mps,
                vz_mps=vz_mps,
                yaw_rate_rps=yaw_rate_rps,
            )

        session.mark_updated()
        return {
            "applied": True,
            "tick_count": session.tick_count,
            "state": session.telemetry()["state"],
        }

    def handle_teleop_state(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        session = self._sessions.get(session_id)
        if session is None:
            raise GazeboRuntimeError(
                f"No such session: {session_id}",
                code="GAZEBO_SESSION_NOT_FOUND",
            )
        return session.telemetry()

    def handle_teleop_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        session = self._sessions.pop(session_id, None)
        self._session_controllers.pop(session_id, None)
        if session is None:
            return {"stopped": False, "tick_count": 0}
        session.status = "stopped"
        return {
            "stopped": True,
            "tick_count": session.tick_count,
            "final_state": session.telemetry().get("state", {}),
        }

    def handle_px4_start(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._px4.start(
                binary=(str(args.get("binary")) if args.get("binary") else None),
                args=(list(args.get("args")) if isinstance(args.get("args"), list) else None),
                system_address=(str(args.get("system_address")) if args.get("system_address") else None),
            )
        except Px4Error as exc:
            raise GazeboRuntimeError(str(exc), code=exc.code) from exc

    def handle_px4_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._px4.status()

    def handle_px4_stop(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._px4.stop()


class RealGazeboRuntime(StubGazeboRuntime):
    """Runtime that talks to Gazebo services via ``gz`` CLI when available."""

    def __init__(
        self,
        *,
        world_name: str = "default",
        enable_px4: bool = False,
        command_runner: _SupportsRun | None = None,
    ) -> None:
        super().__init__(world_name=world_name, enable_px4=enable_px4)
        self._runner = command_runner or _default_runner
        self._gz_available = shutil.which("gz") is not None

    def handle_diagnose(self, args: dict[str, Any]) -> dict[str, Any]:
        out = super().handle_diagnose(args)
        out["runtime_mode"] = "real"
        out["gz_available"] = self._gz_available
        out["connected"] = bool(self._gz_available)
        if self._gz_available:
            try:
                out["worlds"] = self._list_worlds()
            except GazeboRuntimeError as exc:
                out["worlds"] = []
                out["warning"] = str(exc)
        return out

    def handle_spawn_model(self, args: dict[str, Any]) -> dict[str, Any]:
        result = super().handle_spawn_model(args)
        if not self._gz_available:
            raise GazeboRuntimeError(
                "Gazebo CLI 'gz' is not available on PATH.",
                code="GAZEBO_NOT_CONNECTED",
            )
        try:
            spawn_warnings = self._spawn_via_service(
                world_name=result["world_name"],
                model_name=result["model_name"],
                source_path=result["source_path"],
            )
        except GazeboRuntimeError:
            raise
        except Exception as exc:
            raise GazeboRuntimeError(
                f"Failed to spawn model '{result['model_name']}': {exc}",
                code="GAZEBO_SPAWN_FAILED",
            ) from exc
        if spawn_warnings:
            result["mesh_warnings"] = spawn_warnings
        return result

    def handle_simulate(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._gz_available:
            raise GazeboRuntimeError(
                "Gazebo CLI 'gz' is not available on PATH.",
                code="GAZEBO_NOT_CONNECTED",
            )

        source_path = str(args.get("sdf_path", "") or args.get("urdf_path", "")).strip()
        if source_path:
            spawn_args: dict[str, Any] = {
                "world_name": _normalize_world_name(args, self._world_name),
                "model_name": str(args.get("model_name", "")).strip() or Path(source_path).stem,
            }
            if source_path.endswith(".sdf"):
                spawn_args["sdf_path"] = source_path
            else:
                spawn_args["urdf_path"] = source_path
            spawned = self.handle_spawn_model(spawn_args)
        else:
            spawned = None

        duration_s = float(args.get("duration_s", 1.0))
        dt_s = float(args.get("dt_s", 0.01))
        world_name = _normalize_world_name(args, self._world_name)
        steps = max(1, int(round(duration_s / max(dt_s, 1e-6))))
        self._step_world(world_name=world_name, steps=steps)

        result = super().handle_simulate(args)
        summary = result.setdefault("summary", {})
        summary["engine_mode"] = "gazebo_real"
        if spawned:
            summary["spawn"] = spawned
        return result

    def _list_worlds(self) -> list[str]:
        code, stdout, stderr = self._runner(["gz", "service", "-l"])
        if code != 0:
            raise GazeboRuntimeError(
                f"Unable to list Gazebo services: {stderr.strip() or stdout.strip()}",
                code="GAZEBO_COMMAND_ERROR",
            )
        worlds: set[str] = set()
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("/world/"):
                continue
            # /world/default/control -> default
            parts = line.split("/")
            if len(parts) >= 3:
                worlds.add(parts[2])
        if not worlds:
            worlds.add(self._world_name)
        return sorted(worlds)

    def _spawn_via_service(
        self,
        *,
        world_name: str,
        model_name: str,
        source_path: str,
    ) -> list[str]:
        """Spawn a model via Gazebo service.

        Returns a list of warning strings extracted from Gazebo output
        (e.g. mesh loading errors, resource resolution failures).
        """
        req = f'sdf_filename: "{source_path}", name: "{model_name}"'
        code, stdout, stderr = self._runner([
            "gz",
            "service",
            "-s",
            f"/world/{world_name}/create",
            "--reqtype",
            "gz.msgs.EntityFactory",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "4000",
            "--req",
            req,
        ])
        out = f"{stdout}\n{stderr}".lower()
        if code != 0 or "data: true" not in out:
            raise GazeboRuntimeError(
                (
                    f"Gazebo spawn service failed for model '{model_name}' "
                    f"in world '{world_name}': {(stderr or stdout).strip()}"
                ),
                code="GAZEBO_SPAWN_FAILED",
            )

        # Surface warnings from Gazebo output (mesh loading errors, etc.)
        spawn_warnings: list[str] = []
        for line in (stderr or "").splitlines():
            line_lower = line.strip().lower()
            if not line_lower:
                continue
            # Detect mesh loading failures and resource resolution issues
            if any(kw in line_lower for kw in (
                "unable to find",
                "mesh not found",
                "failed to load",
                "resource not found",
                "invalid mesh",
                "missing mesh",
                "could not load",
            )):
                spawn_warnings.append(line.strip())
                logger.warning("Gazebo spawn warning: %s", line.strip())
        return spawn_warnings

    def _step_world(self, *, world_name: str, steps: int) -> None:
        req = f"multi_step: {int(max(1, steps))}, pause: false"
        code, stdout, stderr = self._runner([
            "gz",
            "service",
            "-s",
            f"/world/{world_name}/control",
            "--reqtype",
            "gz.msgs.WorldControl",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "4000",
            "--req",
            req,
        ])
        out = f"{stdout}\n{stderr}".lower()
        if code != 0 or "data: true" not in out:
            raise GazeboRuntimeError(
                f"Gazebo step service failed in world '{world_name}': {(stderr or stdout).strip()}",
                code="GAZEBO_COMMAND_ERROR",
            )


def create_runtime(
    *,
    runtime_mode: str | None = None,
    world_name: str = "default",
    enable_px4: bool = False,
    command_runner: _SupportsRun | None = None,
) -> StubGazeboRuntime | RealGazeboRuntime:
    """Create runtime implementation from configuration/env."""
    mode = str(runtime_mode or os.environ.get("SOLIDMIND_GAZEBO_RUNTIME", "real")).strip().lower()
    if mode == "stub":
        return StubGazeboRuntime(world_name=world_name, enable_px4=enable_px4)
    if mode == "real":
        return RealGazeboRuntime(
            world_name=world_name,
            enable_px4=enable_px4,
            command_runner=command_runner,
        )
    raise GazeboRuntimeError(
        f"Unsupported Gazebo runtime mode '{mode}'. Use 'real' or 'stub'.",
        code="INVALID_INPUT",
    )

