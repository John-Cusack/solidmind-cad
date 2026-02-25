"""Data models for the Isaac bridge runtime."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
    "robot_type",
    "initial_joint_positions",
})

# Defaults applied when robot_type == "mobile" and the field is not
# explicitly provided by the caller.  Stiffness/damping tuned for
# hexapod locomotion (PD position drives need enough authority to
# overcome gravity and accelerate limbs).  Previous values of 10/1
# were too weak — legs cycled but produced no ground-reaction force.
_MOBILE_DEFAULTS: dict[str, Any] = {
    "fix_base": False,
    "merge_fixed_joints": True,
    "default_drive_stiffness": 400.0,
    "default_drive_damping": 30.0,
}


@dataclass(frozen=True, slots=True)
class URDFImportConfig:
    """Configuration for URDF import into Isaac Sim.

    When ``robot_type="mobile"``, research-validated defaults are applied
    for fields not explicitly overridden by the caller (fix_base=False,
    merge_fixed_joints=True, lower stiffness/damping).
    """

    merge_fixed_joints: bool = False
    convex_decomp: bool = False
    import_inertia_tensor: bool = True
    fix_base: bool = True
    distance_scale: float = 1.0
    default_drive_type: str = "position"
    default_drive_stiffness: float = 1000.0
    default_drive_damping: float = 100.0
    robot_type: str = "manipulator"
    initial_joint_positions: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> URDFImportConfig:
        if not d:
            return cls()
        filtered = {k: v for k, v in d.items() if k in _URDF_IMPORT_FIELDS}
        # initial_joint_positions needs special handling (not a simple scalar)
        ijp = filtered.pop("initial_joint_positions", None)
        # Apply mobile-robot defaults for fields not explicitly provided.
        robot_type = filtered.get("robot_type", "manipulator")
        if robot_type == "mobile":
            for key, default_val in _MOBILE_DEFAULTS.items():
                if key not in d:
                    filtered[key] = default_val
        config = cls(**filtered)
        if ijp and isinstance(ijp, dict):
            object.__setattr__(config, "initial_joint_positions", ijp)
        return config


# ──────────────────────────────────────────────────────────────────────
# Teleop configuration
# ──────────────────────────────────────────────────────────────────────

# Default joint names for the 6-leg hexapod (1-DOF hip per leg).
_DEFAULT_JOINT_NAMES: tuple[str, ...] = (
    "hip_lf", "hip_lm", "hip_lr", "hip_rf", "hip_rm", "hip_rr",
)
_DEFAULT_TRIPOD_A: tuple[str, ...] = ("hip_rf", "hip_rr", "hip_lm")
_DEFAULT_TRIPOD_B: tuple[str, ...] = ("hip_rm", "hip_lr", "hip_lf")
_DEFAULT_LEFT_LEGS: tuple[str, ...] = ("hip_lf", "hip_lm", "hip_lr")
_DEFAULT_RIGHT_LEGS: tuple[str, ...] = ("hip_rf", "hip_rm", "hip_rr")


class TeleopConfigError(Exception):
    """Raised when teleop profile values are invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _check_positive(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise TeleopConfigError(f"{name} must be a finite positive number, got {value!r}")
    return float(value)


def _check_non_negative(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise TeleopConfigError(f"{name} must be a finite non-negative number, got {value!r}")
    return float(value)


def _check_finite(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TeleopConfigError(f"{name} must be a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class TeleopConfig:
    """Parsed and validated teleop profile for a session.

    All fields have sensible defaults for a 1-DOF hexapod with tripod
    gait.  Construct via ``from_profile(profile_dict)`` which validates
    types, ranges, and set-consistency of joint/tripod groups.
    """

    controller_type: str = "hexapod_1dof_tripod"
    joint_names: tuple[str, ...] = _DEFAULT_JOINT_NAMES
    tripod_a: tuple[str, ...] = _DEFAULT_TRIPOD_A
    tripod_b: tuple[str, ...] = _DEFAULT_TRIPOD_B
    left_legs: tuple[str, ...] = _DEFAULT_LEFT_LEGS
    right_legs: tuple[str, ...] = _DEFAULT_RIGHT_LEGS
    neutral_deg: float = 0.0
    amplitude_deg: float = 18.0
    stride_hz: float = 2.0
    yaw_mix_deg: float = 8.0
    height_mix_deg: float = 5.0
    vx_max_mps: float = 0.3
    yaw_max_rps: float = 1.0
    height_max_m: float = 0.03
    slew_vx_mps2: float = 1.0
    slew_yaw_rps2: float = 2.0
    slew_height_mps2: float = 0.05
    policy_path: str = ""
    alpha: float = 0.3

    # 3-DOF leg geometry (meters) — matched to hexapod_18dof_hybrid_v2.urdf
    # l_coxa: coxa joint→femur joint (URDF femur origin X)
    # l_femur: femur joint→tibia joint (Euclidean distance of tibia origin)
    # l_tibia: tibia joint→foot tip (Euclidean distance from joint to mesh far end)
    l_coxa: float = 0.052
    l_femur: float = 0.066
    l_tibia: float = 0.150

    # Body dimensions for hip mount positions (meters)
    # Derived from URDF coxa joint origins: ±0.07 X, ±0.075 Y
    body_length: float = 0.14
    body_width: float = 0.15

    # Gait parameters
    step_height: float = 0.03
    stance_height: float = -0.09
    stride_length: float = 0.06
    duty_factor: float = 0.65

    # Per-leg joint names: flat tuple, groups of 3 (coxa, femur, tibia)
    # Order: LF, LM, LR, RF, RM, RR (matching leg_phase_offsets)
    leg_joint_names: tuple[str, ...] = (
        "coxa_lf", "femur_lf", "tibia_lf",
        "coxa_lm", "femur_lm", "tibia_lm",
        "coxa_lr", "femur_lr", "tibia_lr",
        "coxa_rf", "femur_rf", "tibia_rf",
        "coxa_rm", "femur_rm", "tibia_rm",
        "coxa_rr", "femur_rr", "tibia_rr",
    )

    # Tripod phase offsets [0,1) per leg: LF, LM, LR, RF, RM, RR
    leg_phase_offsets: tuple[float, ...] = (0.0, 0.5, 0.0, 0.5, 0.0, 0.5)

    # Explicit hip mount positions: tuple of (x_m, y_m, angle_rad) per leg.
    # Order must match leg_joint_names (groups of 3).  When empty, mounts
    # are auto-computed from body_length/body_width.
    hip_mounts: tuple[tuple[float, float, float], ...] = ()

    @classmethod
    def from_profile(cls, profile: dict[str, Any] | None) -> TeleopConfig:
        """Parse a teleop profile dict into a validated ``TeleopConfig``.

        Missing keys use defaults.  Invalid values raise ``TeleopConfigError``.
        """
        if not profile:
            return cls()

        kwargs: dict[str, Any] = {}

        # Controller type
        ct = profile.get("controller_type")
        if ct is not None:
            if not isinstance(ct, str) or not ct.strip():
                raise TeleopConfigError("controller_type must be a non-empty string")
            kwargs["controller_type"] = ct.strip()

        # Joint name lists
        for list_key, default in (
            ("joint_names", _DEFAULT_JOINT_NAMES),
            ("tripod_a", _DEFAULT_TRIPOD_A),
            ("tripod_b", _DEFAULT_TRIPOD_B),
            ("left_legs", _DEFAULT_LEFT_LEGS),
            ("right_legs", _DEFAULT_RIGHT_LEGS),
        ):
            val = profile.get(list_key)
            if val is not None:
                if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
                    raise TeleopConfigError(f"{list_key} must be a list of strings")
                if not val:
                    raise TeleopConfigError(f"{list_key} must not be empty")
                kwargs[list_key] = tuple(val)

        # Numeric fields: (key, validator, default_value)
        _NUMERIC_FIELDS: list[tuple[str, Any]] = [
            ("neutral_deg", _check_finite),
            ("amplitude_deg", _check_positive),
            ("stride_hz", _check_positive),
            ("yaw_mix_deg", _check_non_negative),
            ("height_mix_deg", _check_non_negative),
            ("vx_max_mps", _check_positive),
            ("yaw_max_rps", _check_positive),
            ("height_max_m", _check_positive),
            ("slew_vx_mps2", _check_positive),
            ("slew_yaw_rps2", _check_positive),
            ("slew_height_mps2", _check_positive),
        ]
        for key, validator in _NUMERIC_FIELDS:
            val = profile.get(key)
            if val is not None:
                kwargs[key] = validator(key, val)

        # Policy fields (for rl_residual controller)
        pp = profile.get("policy_path")
        if pp is not None:
            if not isinstance(pp, str):
                raise TeleopConfigError("policy_path must be a string")
            kwargs["policy_path"] = pp

        alpha_val = profile.get("alpha")
        if alpha_val is not None:
            kwargs["alpha"] = _check_non_negative("alpha", alpha_val)

        # 3-DOF leg geometry and gait fields
        _3DOF_POSITIVE: list[str] = [
            "l_coxa", "l_femur", "l_tibia",
            "body_length", "body_width",
            "step_height", "stride_length",
        ]
        for key in _3DOF_POSITIVE:
            val = profile.get(key)
            if val is not None:
                kwargs[key] = _check_positive(key, val)

        sh = profile.get("stance_height")
        if sh is not None:
            kwargs["stance_height"] = _check_finite("stance_height", sh)

        df = profile.get("duty_factor")
        if df is not None:
            v = _check_positive("duty_factor", df)
            if v >= 1.0:
                raise TeleopConfigError("duty_factor must be < 1.0")
            kwargs["duty_factor"] = v

        # Per-leg joint names
        ljn = profile.get("leg_joint_names")
        if ljn is not None:
            if not isinstance(ljn, list) or not all(isinstance(s, str) for s in ljn):
                raise TeleopConfigError("leg_joint_names must be a list of strings")
            if len(ljn) < 3 or len(ljn) % 3 != 0:
                raise TeleopConfigError(
                    f"leg_joint_names length must be a positive multiple of 3, got {len(ljn)}"
                )
            kwargs["leg_joint_names"] = tuple(ljn)

        # Hip mounts: list of [x, y, angle_rad] per leg
        hm = profile.get("hip_mounts")
        if hm is not None:
            if not isinstance(hm, list):
                raise TeleopConfigError("hip_mounts must be a list of [x, y, angle_rad]")
            parsed_mounts: list[tuple[float, float, float]] = []
            for i, m in enumerate(hm):
                if not isinstance(m, (list, tuple)) or len(m) != 3:
                    raise TeleopConfigError(
                        f"hip_mounts[{i}] must be [x, y, angle_rad], got {m!r}"
                    )
                parsed_mounts.append((float(m[0]), float(m[1]), float(m[2])))
            kwargs["hip_mounts"] = tuple(parsed_mounts)

        # Phase offsets
        lpo = profile.get("leg_phase_offsets")
        if lpo is not None:
            if not isinstance(lpo, list) or not all(isinstance(v, (int, float)) for v in lpo):
                raise TeleopConfigError("leg_phase_offsets must be a list of numbers")
            kwargs["leg_phase_offsets"] = tuple(float(v) for v in lpo)

        config = cls(**kwargs)

        # Validation dispatch based on controller type
        if config.controller_type == "hexapod_3dof_tripod":
            _validate_3dof_consistency(config)
        elif config.controller_type in ("rl_residual", "quad_spin"):
            if len(config.joint_names) < 1:
                raise TeleopConfigError("joint_names must have at least 1 entry")
        else:
            _validate_tripod_consistency(config)
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "controller_type": self.controller_type,
            "joint_names": list(self.joint_names),
            "tripod_a": list(self.tripod_a),
            "tripod_b": list(self.tripod_b),
            "left_legs": list(self.left_legs),
            "right_legs": list(self.right_legs),
            "neutral_deg": self.neutral_deg,
            "amplitude_deg": self.amplitude_deg,
            "stride_hz": self.stride_hz,
            "yaw_mix_deg": self.yaw_mix_deg,
            "height_mix_deg": self.height_mix_deg,
            "vx_max_mps": self.vx_max_mps,
            "yaw_max_rps": self.yaw_max_rps,
            "height_max_m": self.height_max_m,
            "slew_vx_mps2": self.slew_vx_mps2,
            "slew_yaw_rps2": self.slew_yaw_rps2,
            "slew_height_mps2": self.slew_height_mps2,
            "policy_path": self.policy_path,
            "alpha": self.alpha,
            "l_coxa": self.l_coxa,
            "l_femur": self.l_femur,
            "l_tibia": self.l_tibia,
            "body_length": self.body_length,
            "body_width": self.body_width,
            "step_height": self.step_height,
            "stance_height": self.stance_height,
            "stride_length": self.stride_length,
            "duty_factor": self.duty_factor,
            "leg_joint_names": list(self.leg_joint_names),
            "leg_phase_offsets": list(self.leg_phase_offsets),
            "hip_mounts": [list(m) for m in self.hip_mounts],
        }


def _validate_3dof_consistency(config: TeleopConfig) -> None:
    """Validate 3-DOF hexapod configuration."""
    n_joints = len(config.leg_joint_names)
    if n_joints < 3 or n_joints % 3 != 0:
        raise TeleopConfigError(
            f"leg_joint_names length must be a positive multiple of 3, got {n_joints}"
        )
    n_legs = n_joints // 3
    if len(config.leg_phase_offsets) != n_legs:
        raise TeleopConfigError(
            f"leg_phase_offsets length ({len(config.leg_phase_offsets)}) "
            f"must equal number of legs ({n_legs})"
        )
    # For 3-DOF, joint_names should be set to leg_joint_names so the
    # runtime resolves all 18 DOFs.  We mutate via object.__setattr__
    # since the dataclass is frozen.
    object.__setattr__(config, "joint_names", config.leg_joint_names)


def _validate_tripod_consistency(config: TeleopConfig) -> None:
    """Validate that tripod groups are consistent with joint_names."""
    joints = set(config.joint_names)
    tripod_a = set(config.tripod_a)
    tripod_b = set(config.tripod_b)

    # tripod_a and tripod_b must not overlap
    overlap = tripod_a & tripod_b
    if overlap:
        raise TeleopConfigError(
            f"tripod_a and tripod_b overlap: {sorted(overlap)}"
        )

    # tripod_a ∪ tripod_b must equal joint_names
    union = tripod_a | tripod_b
    if union != joints:
        missing = joints - union
        extra = union - joints
        parts: list[str] = []
        if missing:
            parts.append(f"missing from tripods: {sorted(missing)}")
        if extra:
            parts.append(f"not in joint_names: {sorted(extra)}")
        raise TeleopConfigError(
            f"tripod_a ∪ tripod_b must equal joint_names; {'; '.join(parts)}"
        )

    # Joint count must be even (tripod gait needs paired groups)
    if len(config.joint_names) < 2:
        raise TeleopConfigError(
            f"joint_names must have at least 2 entries, got {len(config.joint_names)}"
        )


# ──────────────────────────────────────────────────────────────────────
# Controller protocol (early extraction — P7 brought forward)
# ──────────────────────────────────────────────────────────────────────

@runtime_checkable
class Controller(Protocol):
    """Interface for teleop controllers.

    Implementations map high-level commands (velocity, yaw, height)
    to per-joint targets each tick.  The runtime calls ``compute_targets``
    on the main thread after ``app.update()``.
    """

    def compute_targets(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        """Compute joint targets for one tick.

        Args:
            state: Current commanded velocities.
            dt_s: Time since last tick (seconds).
            config: Validated teleop configuration.
            phase: Current gait phase in radians [0, 2π).

        Returns:
            (targets_rad, new_phase) where targets_rad maps joint name
            to target angle in radians.
        """
        ...


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
    # Teleop runtime fields (populated only for session_type="teleop")
    teleop_config: TeleopConfig | None = None
    controller: Any = None  # Controller instance (typed as Any to avoid slot issues)
    gait_phase: float = 0.0  # Current gait phase in radians [0, 2π)
    filtered_vx: float = 0.0  # Slew-filtered forward velocity
    filtered_yaw: float = 0.0  # Slew-filtered yaw rate
    filtered_height: float = 0.0  # Slew-filtered body height
    dof_index_map: dict[str, int] = field(default_factory=dict)  # joint_name → DOF index
    joint_limits: dict[str, tuple[float, float]] = field(default_factory=dict)  # joint_name → (lo, hi) rad
    last_joint_targets_rad: dict[str, float] = field(default_factory=dict)
    limit_clamp_count: int = 0
    tick_count: int = 0
    last_apply_ok: bool = True

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
        if self.session_type == "teleop" and self.teleop_config is not None:
            result["controller_type"] = self.teleop_config.controller_type
            result["tick_count"] = self.tick_count
            result["limit_clamp_count"] = self.limit_clamp_count
        return result


# Backward-compat alias
TeleopSession = SimulationSession
