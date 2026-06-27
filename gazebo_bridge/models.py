"""Data models for the Gazebo bridge runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GazeboConfig:
    """Configuration for the Gazebo bridge server/runtime."""

    host: str = "127.0.0.1"
    port: int = 9879
    world_name: str = "default"
    world_file: str = ""
    physics_dt_s: float = 0.001
    real_time_factor: float = 1.0
    runtime_mode: str = "real"
    launch_gz: bool = False
    enable_px4: bool = False


@dataclass(slots=True)
class GazeboSession:
    """Mutable state for an active Gazebo simulation or teleop session."""

    session_id: str
    session_type: str  # "simulate" or "teleop"
    mechanism: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    world_name: str = "default"
    model_name: str = ""
    entity_id: int | None = None
    urdf_path: str | None = None
    sdf_path: str | None = None
    status: str = "created"
    controller_type: str = "multirotor_direct"
    created_at_s: float = field(default_factory=time.time)
    updated_at_s: float = field(default_factory=time.time)
    sim_time_s: float = 0.0
    vx_mps: float = 0.0
    yaw_rate_rps: float = 0.0
    body_height_m: float = 0.0
    vy_mps: float = 0.0
    vz_mps: float = 0.0
    tick_count: int = 0
    position_xyz_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    yaw_rad: float = 0.0
    rotor_setpoints: dict[str, float] = field(default_factory=dict)

    def mark_updated(self) -> None:
        self.updated_at_s = time.time()

    def telemetry(self) -> dict[str, Any]:
        """Return deterministic session telemetry payload."""
        x_m, y_m, z_m = self.position_xyz_m
        return {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "status": self.status,
            "world_name": self.world_name,
            "model_name": self.model_name,
            "entity_id": self.entity_id,
            "controller_type": self.controller_type,
            "tick_count": self.tick_count,
            "sim_time_s": self.sim_time_s,
            "state": {
                "vx_mps": self.vx_mps,
                "vy_mps": self.vy_mps,
                "vz_mps": self.vz_mps,
                "yaw_rate_rps": self.yaw_rate_rps,
                "body_height_m": self.body_height_m,
                "position_xyz_m": [x_m, y_m, z_m],
                "yaw_rad": self.yaw_rad,
                "rotor_setpoints": dict(self.rotor_setpoints),
            },
        }
