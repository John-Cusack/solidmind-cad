"""Data models for the Isaac bridge runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_JOINT_TYPES = frozenset({"revolute", "prismatic", "fixed"})


@dataclass(slots=True)
class TeleopState:
    """Mutable teleop drive state for a session."""

    vx_mps: float = 0.0
    yaw_rate_rps: float = 0.0
    body_height_m: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "vx_mps": self.vx_mps,
            "yaw_rate_rps": self.yaw_rate_rps,
            "body_height_m": self.body_height_m,
        }


@dataclass(slots=True)
class TeleopSession:
    """In-memory teleop session."""

    session_id: str
    mechanism: dict[str, Any]
    profile: dict[str, Any]
    started_at_s: float
    state: TeleopState = field(default_factory=TeleopState)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at_s": self.started_at_s,
            "profile": dict(self.profile),
        }
