"""Data models for the Gazebo bridge runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GazeboConfig:
    """Configuration for the Gazebo bridge server."""

    host: str = "127.0.0.1"
    port: int = 9879
    world_file: str = ""
    physics_dt_s: float = 0.001
    real_time_factor: float = 1.0


@dataclass(slots=True)
class GazeboSession:
    """Mutable state for an active Gazebo simulation or teleop session."""

    session_id: str
    session_type: str  # "simulate" or "teleop"
    mechanism: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    urdf_path: str | None = None
    vx_mps: float = 0.0
    yaw_rate_rps: float = 0.0
    body_height_m: float = 0.0
    vy_mps: float = 0.0
    vz_mps: float = 0.0
    tick_count: int = 0
