"""Data models for the Isaac bridge runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_JOINT_TYPES = frozenset({"revolute", "prismatic", "fixed"})

_URDF_IMPORT_FIELDS: frozenset[str] = frozenset({
    "merge_fixed_joints",
    "convex_decomp",
    "import_inertia_tensor",
    "fix_base",
    "distance_scale",
    "default_drive_type",
    "default_drive_stiffness",
    "default_drive_damping",
})


@dataclass(frozen=True, slots=True)
class URDFImportConfig:
    """Configuration for URDF import into Isaac Sim."""

    merge_fixed_joints: bool = False
    convex_decomp: bool = False
    import_inertia_tensor: bool = True
    fix_base: bool = True
    distance_scale: float = 1.0
    default_drive_type: str = "position"
    default_drive_stiffness: float = 1000.0
    default_drive_damping: float = 100.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> URDFImportConfig:
        if not d:
            return cls()
        filtered = {k: v for k, v in d.items() if k in _URDF_IMPORT_FIELDS}
        return cls(**filtered)


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
class SimulationSession:
    """Unified in-memory session for both simulation and teleop."""

    session_id: str
    session_type: str  # "simulate" | "teleop"
    mechanism: dict[str, Any]
    profile: dict[str, Any]
    started_at_s: float
    state: TeleopState = field(default_factory=TeleopState)
    prim_path: str | None = None
    articulation: Any = None
    # Simulation-specific fields
    target_steps: int = 0  # 0 = interactive (no batch target)
    completed_steps: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"  # "running" | "complete"
    warning: str | None = None

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "started_at_s": self.started_at_s,
            "profile": dict(self.profile),
            "status": self.status,
        }
        if self.session_type == "simulate":
            result["target_steps"] = self.target_steps
            result["completed_steps"] = self.completed_steps
            result["samples_count"] = len(self.samples)
        return result


# Backward-compat alias
TeleopSession = SimulationSession
