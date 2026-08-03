"""Reference engine runtime — the contract, implemented plainly.

Reads the canonical sim package, runs the small analytic physics in
``reference_engine/physics.py``, and answers every verb the handshake
advertises.  An engine author can read this top to bottom and see exactly what
their own bridge has to do.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import time
from typing import Any

from reference_engine.physics import (
    GRAVITY_M_S2,
    fall_height,
    gear_train_speeds,
    pendulum_angle,
    settle_time,
)

logger = logging.getLogger("solidmind.reference_engine")

ENGINE_NAME = "reference"
ENGINE_VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0.0"
CONTRACT_VERSIONS_SUPPORTED = ("1",)

CAPABILITIES: dict[str, Any] = {
    "modes": ["batch", "session", "teleop"],
    "formats": ["package", "mechanism", "urdf"],
    "features": ["diagnose"],
    "teleop_dofs": ["vx_mps", "yaw_rate_rps", "body_height_m"],
    "fields": {"emits": [], "accepts": []},
}

#: Idle sessions are kept forever by default (contract §5).
IDLE_TTL_S: float | None = None


class ReferenceError(Exception):
    """Raised with a contract error code (``docs/engine-contract.md`` §4)."""

    def __init__(self, message: str, *, code: str = "ENGINE_ERROR") -> None:
        super().__init__(message)
        self.code = code


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class ReferenceRuntime:
    """One process's worth of engine state: sessions and nothing else."""

    def __init__(self) -> None:
        self._sim_sessions: dict[str, dict[str, Any]] = {}
        self._teleop_sessions: dict[str, dict[str, Any]] = {}
        self._models_loaded = 0

    # -- required floor --------------------------------------------------

    def hello(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "contract_versions_supported": list(CONTRACT_VERSIONS_SUPPORTED),
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            # The reference engine is always itself — there is no stub of a
            # stub.  It computes real (if simple) physics.
            "runtime_mode": "real",
            "capabilities": {
                "modes": list(CAPABILITIES["modes"]),
                "formats": list(CAPABILITIES["formats"]),
                "features": list(CAPABILITIES["features"]),
                "teleop_dofs": list(CAPABILITIES["teleop_dofs"]),
                "fields": dict(CAPABILITIES["fields"]),
            },
        }

    def ping(self) -> dict[str, Any]:
        return {"pong": True, "models_loaded": self._models_loaded}

    def simulate(self, args: dict[str, Any]) -> dict[str, Any]:
        model = self._load_model(args)
        duration_s = float(args.get("duration_s", 1.0))
        dt_s = float(args.get("dt_s", 0.001))
        output_interval = float(args.get("output_interval", 0.01))
        return self._run(model, duration_s, dt_s, output_interval)

    def shutdown(self) -> dict[str, Any]:
        """Drain: sessions are stopped, then the caller exits the process."""
        drained = len(self._sim_sessions) + len(self._teleop_sessions)
        self._sim_sessions.clear()
        self._teleop_sessions.clear()
        return {"message": "Shutting down", "sessions_drained": drained}

    # -- model loading ---------------------------------------------------

    def _load_model(self, args: dict[str, Any]) -> dict[str, Any]:
        """Resolve the model from a package, a mechanism, or a URDF path.

        A package is the canonical input, so it is tried first; ``mechanism``
        stays supported because it is what core's motion tools send today.
        """
        package_path = str(args.get("package_path", "")).strip()
        if package_path:
            manifest = self._read_manifest(package_path)
            self._models_loaded += 1
            return {
                "source": "package",
                "name": str(manifest.get("name", "package")),
                "manifest": manifest,
                "mechanism": _as_dict(args.get("mechanism")),
            }

        mechanism = _as_dict(args.get("mechanism"))
        if mechanism:
            self._models_loaded += 1
            return {
                "source": "mechanism",
                "name": str(mechanism.get("name", "mechanism")),
                "manifest": {},
                "mechanism": mechanism,
            }

        urdf_path = str(args.get("urdf_path", "")).strip()
        if urdf_path:
            if not os.path.isfile(urdf_path):
                raise ReferenceError(f"URDF not found: {urdf_path}", code="PACKAGE_INVALID")
            self._models_loaded += 1
            return {
                "source": "urdf",
                "name": os.path.splitext(os.path.basename(urdf_path))[0],
                "manifest": {},
                "mechanism": {},
            }

        raise ReferenceError(
            "simulate needs a package_path, a mechanism, or a urdf_path",
            code="INVALID_REQUEST",
        )

    def _read_manifest(self, package_path: str) -> dict[str, Any]:
        path = (
            os.path.join(package_path, "manifest.json")
            if os.path.isdir(package_path)
            else package_path
        )
        if not os.path.isfile(path):
            raise ReferenceError(f"No sim package manifest at {path}", code="PACKAGE_INVALID")
        try:
            with open(path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReferenceError(
                f"Cannot read manifest {path}: {exc}", code="PACKAGE_INVALID"
            ) from exc
        if not isinstance(manifest, dict) or not manifest.get("links"):
            raise ReferenceError(f"Manifest {path} declares no links", code="PACKAGE_INVALID")
        return manifest

    # -- simulation ------------------------------------------------------

    def _run(
        self,
        model: dict[str, Any],
        duration_s: float,
        dt_s: float,
        output_interval: float,
    ) -> dict[str, Any]:
        """Pick the scenario the model describes and produce contract results."""
        if duration_s <= 0 or dt_s <= 0:
            raise ReferenceError("duration_s and dt_s must be > 0", code="INVALID_REQUEST")
        interval = max(output_interval, dt_s)
        sample_times = _sample_times(duration_s, interval)

        mechanism = model["mechanism"]
        scenario = _classify(mechanism, model["manifest"])

        if scenario == "gear_train":
            result = self._gear_train(mechanism, sample_times, duration_s)
        elif scenario == "pendulum":
            result = self._pendulum(mechanism, sample_times)
        else:
            result = self._free_fall(model, sample_times)

        summary = result["summary"]
        summary.update(
            {
                "simulation_time_s": duration_s,
                "dt_s": dt_s,
                "output_interval": interval,
                "engine_mode": "reference",
                "scenario": scenario,
                "model_name": model["name"],
                "model_source": model["source"],
            }
        )
        if model["manifest"]:
            summary["link_count"] = len(model["manifest"].get("links") or [])
            summary["joint_count"] = len(model["manifest"].get("joints") or [])
        return result

    def _gear_train(
        self,
        mechanism: dict[str, Any],
        sample_times: list[float],
        duration_s: float,
    ) -> dict[str, Any]:
        """Steady-state gear propagation, ramped over the run."""
        steady = gear_train_speeds(mechanism)
        joints = [j for j in mechanism.get("joints") or [] if isinstance(j, dict)]
        joint_ids = [str(j.get("id", "")) for j in joints]
        torque = max(
            (float(d.get("torque_nm") or 0.0) for d in mechanism.get("drives") or []),
            default=0.0,
        )

        time_series: list[dict[str, Any]] = []
        for t in sample_times:
            # Linear spin-up to steady state, so the series is not a constant.
            scale = 1.0 if duration_s <= 0 else min(1.0, t / duration_s)
            time_series.append(
                {
                    "t": round(t, 6),
                    "parts": {
                        part: {"omega_rpm": round(rpm * scale, 6)} for part, rpm in steady.items()
                    },
                    "joint_efforts": [round(torque * scale, 6) for _ in joint_ids],
                }
            )

        return {
            "time_series": time_series,
            "summary": {
                "steady_state_speeds": {k: round(v, 6) for k, v in steady.items()},
                "peak_joint_forces": {jid: round(torque, 6) for jid in joint_ids},
            },
        }

    def _pendulum(self, mechanism: dict[str, Any], sample_times: list[float]) -> dict[str, Any]:
        """Small-angle pendulum on the first undriven revolute joint."""
        joint = _first_free_revolute(mechanism)
        length_m = _joint_length_m(joint)
        theta0 = float(joint.get("initial_angle_rad", 0.1))
        part = str(joint.get("child_part", "bob"))

        time_series = []
        for t in sample_times:
            theta = pendulum_angle(length_m, theta0, t)
            time_series.append(
                {
                    "t": round(t, 6),
                    "parts": {part: {"angle_rad": round(theta, 9)}},
                    "joint_efforts": [0.0],
                }
            )
        return {
            "time_series": time_series,
            "summary": {
                "pendulum_length_m": length_m,
                "pendulum_period_s": round(2.0 * math.pi * math.sqrt(length_m / GRAVITY_M_S2), 6),
                "steady_state_speeds": {part: 0.0},
            },
        }

    def _free_fall(self, model: dict[str, Any], sample_times: list[float]) -> dict[str, Any]:
        """A body dropped from its manifest height, settling on the ground."""
        height_m, part = _initial_height(model)
        time_series = []
        for t in sample_times:
            time_series.append(
                {
                    "t": round(t, 6),
                    "parts": {part: {"z_m": round(fall_height(height_m, t), 9)}},
                }
            )
        return {
            "time_series": time_series,
            "summary": {
                "initial_height_m": height_m,
                "settle_time_s": round(settle_time(height_m), 6),
                "steady_state_speeds": {part: 0.0},
            },
        }

    # -- sessions (capability: modes.session) ----------------------------

    def simulate_start(self, args: dict[str, Any]) -> dict[str, Any]:
        model = self._load_model(args)
        duration_s = float(args.get("duration_s", 1.0))
        dt_s = float(args.get("dt_s", 0.001))
        output_interval = float(args.get("output_interval", 0.01))
        session_id = f"ref_sim_{secrets.token_hex(4)}"
        self._sim_sessions[session_id] = {
            "result": self._run(model, duration_s, dt_s, output_interval),
            "started_at": time.time(),
            "target_steps": int(duration_s / dt_s),
        }
        return {
            "session_id": session_id,
            "status": "running",
            "model_name": model["name"],
            "steady_state_speeds": self._sim_sessions[session_id]["result"]["summary"].get(
                "steady_state_speeds", {}
            ),
        }

    def simulate_status(self, args: dict[str, Any]) -> dict[str, Any]:
        # Raises SESSION_NOT_FOUND for an unknown id, which the TCK checks.
        self._session(self._sim_sessions, args)
        # The reference engine computes eagerly, so a started run is done.
        return {"session_id": args.get("session_id"), "status": "complete", "progress": 1.0}

    def simulate_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._session(self._sim_sessions, args)
        self._sim_sessions.pop(session_id, None)
        result = session["result"]
        return {
            "session_id": session_id,
            "stopped": True,
            "samples": result["time_series"],
            "summary": result["summary"],
            "target_steps": session["target_steps"],
        }

    # -- teleop (capability: modes.teleop) -------------------------------

    def teleop_start(self, args: dict[str, Any]) -> dict[str, Any]:
        model = self._load_model(args)
        session_id = f"ref_teleop_{secrets.token_hex(4)}"
        profile = _as_dict(args.get("profile"))
        self._teleop_sessions[session_id] = {
            "model_name": model["name"],
            "profile": profile,
            "tick_count": 0,
            "sim_time_s": 0.0,
            "state": {axis: 0.0 for axis in CAPABILITIES["teleop_dofs"]},
            "position_xyz_m": [0.0, 0.0, 0.0],
            "yaw_rad": 0.0,
        }
        return {
            "session_id": session_id,
            "status": "started",
            "controller_type": str(profile.get("controller_type", "reference")),
            "model_name": model["name"],
            "profile_used": dict(profile),
        }

    def teleop_command(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._session(self._teleop_sessions, args)
        dt_s = float(args.get("dt_s", 0.02))
        for axis in CAPABILITIES["teleop_dofs"]:
            if axis in args:
                session["state"][axis] = float(args[axis])

        # Dead-reckon the body so state reads back something meaningful.
        vx = session["state"]["vx_mps"]
        yaw_rate = session["state"]["yaw_rate_rps"]
        session["yaw_rad"] += yaw_rate * dt_s
        session["position_xyz_m"][0] += vx * math.cos(session["yaw_rad"]) * dt_s
        session["position_xyz_m"][1] += vx * math.sin(session["yaw_rad"]) * dt_s
        session["position_xyz_m"][2] = session["state"]["body_height_m"]
        session["tick_count"] += 1
        session["sim_time_s"] += dt_s
        return {"applied": True, "tick_count": session["tick_count"], "state": self._state(session)}

    def teleop_state(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._session(self._teleop_sessions, args)
        return {
            "session_id": args.get("session_id"),
            "state": self._state(session),
            "tick_count": session["tick_count"],
            "uptime_s": round(session["sim_time_s"], 6),
            "controller_type": str(session["profile"].get("controller_type", "reference")),
        }

    def teleop_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._session(self._teleop_sessions, args)
        self._teleop_sessions.pop(session_id, None)
        return {
            "stopped": True,
            "tick_count": session["tick_count"],
            "final_state": self._state(session),
        }

    # -- diagnose (capability: features.diagnose) ------------------------

    def diagnose(self, args: dict[str, Any]) -> dict[str, Any]:
        """Normalized scene report (contract §3.3).

        The reference engine has no scene graph, so it reports what it was
        last asked to load — enough for the urdf-vs-diagnose check to be
        exercised end to end.
        """
        package_path = str(args.get("package_path", "")).strip()
        manifest = self._read_manifest(package_path) if package_path else {}
        joints = manifest.get("joints") or []

        counts: dict[str, int] = {}
        for joint in joints:
            generic = str(joint.get("type", "fixed"))
            counts[generic] = counts.get(generic, 0) + 1

        actuated = [j for j in joints if j.get("type") in ("revolute", "prismatic", "continuous")]
        return {
            "engine": ENGINE_NAME,
            "joint_counts": counts,
            "joint_total": len(joints),
            "link_count": len(manifest.get("links") or []),
            "dof_count": len(actuated),
            "dof_names": [str(j.get("name", "")) for j in actuated],
            "joints": [
                {
                    "name": str(j.get("name", "")),
                    "type": str(j.get("type", "fixed")),
                    "connected": bool(j.get("parent")) and bool(j.get("child")),
                    "has_drive": True,
                }
                for j in joints
            ],
            "sessions": {
                "simulate": len(self._sim_sessions),
                "teleop": len(self._teleop_sessions),
            },
        }

    # -- helpers ---------------------------------------------------------

    def _session(self, store: dict[str, dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        session = store.get(session_id)
        if session is None:
            raise ReferenceError(f"No such session: {session_id!r}", code="SESSION_NOT_FOUND")
        return session

    @staticmethod
    def _state(session: dict[str, Any]) -> dict[str, Any]:
        return {
            **session["state"],
            "position_xyz_m": list(session["position_xyz_m"]),
            "yaw_rad": round(session["yaw_rad"], 9),
            "sim_time_s": round(session["sim_time_s"], 6),
        }


# ---------------------------------------------------------------------------
# Scenario selection
# ---------------------------------------------------------------------------


def _sample_times(duration_s: float, interval: float) -> list[float]:
    count = max(2, int(duration_s / interval) + 1)
    return [min(duration_s, i * interval) for i in range(count)]


def _classify(mechanism: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Which scenario does this model describe?"""
    joints = [j for j in mechanism.get("joints") or [] if isinstance(j, dict)]
    drives = mechanism.get("drives") or []
    if drives and joints:
        return "gear_train"
    if joints and _first_free_revolute(mechanism) is not None:
        return "pendulum"
    if manifest.get("links") or mechanism.get("parts"):
        return "free_fall"
    return "free_fall"


def _first_free_revolute(mechanism: dict[str, Any]) -> dict[str, Any] | None:
    driven = {d.get("joint_id") for d in mechanism.get("drives") or []}
    for joint in mechanism.get("joints") or []:
        if not isinstance(joint, dict):
            continue
        if joint.get("joint_type") == "revolute" and joint.get("id") not in driven:
            return joint
    return None


def _joint_length_m(joint: dict[str, Any] | None) -> float:
    """Pendulum length from the joint's origin offset (mm), with a sane floor."""
    if joint is None:
        return 1.0
    origin = joint.get("origin") or (0.0, 0.0, 0.0)
    try:
        length_mm = math.sqrt(sum(float(v) ** 2 for v in origin))
    except (TypeError, ValueError):
        length_mm = 0.0
    length_m = length_mm / 1000.0
    return length_m if length_m > 1e-6 else 1.0


def _initial_height(model: dict[str, Any]) -> tuple[float, str]:
    """Drop height and body name, from the manifest or the mechanism."""
    for link in model["manifest"].get("links") or []:
        pose = link.get("world_pose") or {}
        xyz = pose.get("xyz_m") or [0.0, 0.0, 0.0]
        if len(xyz) > 2 and float(xyz[2]) > 0:
            return float(xyz[2]), str(link.get("name", "body"))
    parts = [p for p in model["mechanism"].get("parts") or [] if isinstance(p, dict)]
    for part in parts:
        height = part.get("initial_height_m")
        if height is not None:
            return float(height), str(part.get("id", "body"))
    name = str(parts[0].get("id", "body")) if parts else "body"
    return 1.0, name
