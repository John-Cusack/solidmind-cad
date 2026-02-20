"""Teleop controllers for the Isaac bridge.

Each controller implements the ``Controller`` protocol from
``isaac_bridge.models`` and maps high-level teleop commands to
per-joint targets.

Adding a new controller
-----------------------

1. Create a class with ``compute_targets(state, dt_s, config, phase)``
   returning ``(targets_rad, new_phase)`` plus ``filtered_vx``,
   ``filtered_yaw``, ``filtered_height`` read-only properties (the
   runtime syncs these to the session for telemetry).

2. Register it in ``_CONTROLLER_REGISTRY`` at module level::

       _CONTROLLER_REGISTRY["my_controller"] = MyController

3. The user selects it via ``profile={"controller_type": "my_controller"}``
   in ``motion.teleop_start``.  No protocol or runtime changes needed.

4. If the controller needs extra config fields, add them to
   ``TeleopConfig`` (with defaults) and ``from_profile()`` parsing.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from isaac_bridge.models import Controller, TeleopConfig, TeleopState

logger = logging.getLogger("solidmind.controllers")

_TWO_PI = 2.0 * math.pi
_DEG2RAD = math.pi / 180.0


def _slew(current: float, target: float, max_rate: float, dt_s: float) -> float:
    """Rate-limit *current* toward *target* at *max_rate* per second."""
    delta = target - current
    max_step = max_rate * dt_s
    if abs(delta) <= max_step:
        return target
    return current + math.copysign(max_step, delta)


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Clamp *value* to [lo, hi]. Returns (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class HexapodTripodController:
    """1-DOF hexapod tripod gait controller.

    Generates sinusoidal hip oscillations for a tripod gait where two
    groups of three legs alternate with 180° phase offset.  Forward
    velocity scales the oscillation amplitude, yaw creates a differential
    offset between left and right legs, and height adds a constant
    offset to all joints.

    The controller owns the slew-filtered command state so that command
    jumps are rate-limited over successive ticks.
    """

    def __init__(self) -> None:
        self._filtered_vx: float = 0.0
        self._filtered_yaw: float = 0.0
        self._filtered_height: float = 0.0

    @property
    def filtered_vx(self) -> float:
        return self._filtered_vx

    @property
    def filtered_yaw(self) -> float:
        return self._filtered_yaw

    @property
    def filtered_height(self) -> float:
        return self._filtered_height

    def compute_targets(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        """Compute hip joint targets for one tick.

        Returns (targets_rad, new_phase).
        """
        if dt_s <= 0:
            # No time elapsed — return neutral targets, no phase advance.
            neutral_rad = config.neutral_deg * _DEG2RAD
            targets = {name: neutral_rad for name in config.joint_names}
            return targets, phase

        # ── 1. Slew-filter the commanded inputs ──────────────────────
        #    Normalize commands to [-1, 1] using config maxima.
        target_vx_norm = max(-1.0, min(1.0, state.vx_mps / config.vx_max_mps))
        target_yaw_norm = max(-1.0, min(1.0, state.yaw_rate_rps / config.yaw_max_rps))
        target_height_norm = max(-1.0, min(1.0, state.body_height_m / config.height_max_m))

        #    Slew rates are in physical units/s² — convert to normalized/s.
        slew_vx_norm = config.slew_vx_mps2 / config.vx_max_mps
        slew_yaw_norm = config.slew_yaw_rps2 / config.yaw_max_rps
        slew_height_norm = config.slew_height_mps2 / config.height_max_m

        self._filtered_vx = _slew(self._filtered_vx, target_vx_norm, slew_vx_norm, dt_s)
        self._filtered_yaw = _slew(self._filtered_yaw, target_yaw_norm, slew_yaw_norm, dt_s)
        self._filtered_height = _slew(self._filtered_height, target_height_norm, slew_height_norm, dt_s)

        # ── 2. Advance gait phase ───────────────────────────────────
        #    Phase advances proportional to filtered forward speed.
        #    At full speed the gait runs at stride_hz; at zero the
        #    phase freezes (legs hold position).
        speed_factor = abs(self._filtered_vx)
        phase_rate = config.stride_hz * _TWO_PI * speed_factor
        new_phase = (phase + phase_rate * dt_s) % _TWO_PI

        # ── 3. Compute per-joint targets ─────────────────────────────
        neutral_rad = config.neutral_deg * _DEG2RAD
        amplitude_rad = config.amplitude_deg * _DEG2RAD
        yaw_mix_rad = config.yaw_mix_deg * _DEG2RAD
        height_mix_rad = config.height_mix_deg * _DEG2RAD

        tripod_a_set = set(config.tripod_a)
        left_set = set(config.left_legs)

        # Direction sign: positive vx → forward walking.
        # Reverse the oscillation when walking backward so the gait
        # direction matches the commanded velocity.
        direction = 1.0 if self._filtered_vx >= 0 else -1.0

        targets: dict[str, float] = {}
        for name in config.joint_names:
            # Base oscillation — tripod A at current phase, tripod B
            # offset by π (alternating gait).
            joint_phase = new_phase if name in tripod_a_set else new_phase + math.pi
            oscillation = direction * amplitude_rad * speed_factor * math.sin(joint_phase)

            # Right-side legs are mirror-mounted — negate the oscillation
            # so both sides swing in the same walking direction.
            if name not in left_set:
                oscillation = -oscillation

            # Yaw differential: left legs get +yaw, right legs get -yaw
            # (positive yaw_rate → turn left → left legs slower, right
            # faster).  Sign convention: positive yaw offset on right
            # legs pushes them forward, negative on left holds them back.
            if name in left_set:
                yaw_offset = -yaw_mix_rad * self._filtered_yaw
            else:
                yaw_offset = yaw_mix_rad * self._filtered_yaw

            # Height offset: uniform across all joints.
            height_offset = height_mix_rad * self._filtered_height

            targets[name] = neutral_rad + oscillation + yaw_offset + height_offset

        return targets, new_phase


class Hexapod3DOFController:
    """3-DOF hexapod controller with analytical IK.

    Generates per-leg foot trajectories using phase-based gait
    scheduling and solves 3-DOF IK (coxa/femur/tibia) per leg.
    Swing legs follow a sine-arc lift; stance legs push linearly
    backward.  Forward velocity, yaw, and body height commands are
    slew-filtered identically to the 1-DOF controller.
    """

    def __init__(self) -> None:
        self._filtered_vx: float = 0.0
        self._filtered_yaw: float = 0.0
        self._filtered_height: float = 0.0
        # Lazy-init on first tick
        self._mounts: list[HipMount] | None = None
        self._default_feet: list[tuple[float, float, float]] | None = None

    @property
    def filtered_vx(self) -> float:
        return self._filtered_vx

    @property
    def filtered_yaw(self) -> float:
        return self._filtered_yaw

    @property
    def filtered_height(self) -> float:
        return self._filtered_height

    def _init_geometry(self, config: TeleopConfig) -> None:
        """Build hip mounts and default foot positions from config."""
        from isaac_bridge.hexapod_ik import HipMount, LegGeometry, default_foot_position

        geom = LegGeometry(
            l_coxa=config.l_coxa,
            l_femur=config.l_femur,
            l_tibia=config.l_tibia,
        )
        n_legs = len(config.leg_joint_names) // 3
        half_len = config.body_length / 2.0
        half_wid = config.body_width / 2.0

        # Standard 6-leg layout: LF, LM, LR, RF, RM, RR
        # Extend to arbitrary leg count by computing positions
        if n_legs == 6:
            self._mounts = [
                HipMount(x=half_len, y=half_wid, angle=math.pi / 4),       # LF
                HipMount(x=0.0, y=half_wid, angle=math.pi / 2),            # LM
                HipMount(x=-half_len, y=half_wid, angle=3 * math.pi / 4),  # LR
                HipMount(x=half_len, y=-half_wid, angle=-math.pi / 4),     # RF
                HipMount(x=0.0, y=-half_wid, angle=-math.pi / 2),          # RM
                HipMount(x=-half_len, y=-half_wid, angle=-3 * math.pi / 4),  # RR
            ]
        else:
            # Fallback: distribute legs evenly around the body
            self._mounts = []
            for i in range(n_legs):
                angle = 2.0 * math.pi * i / n_legs
                self._mounts.append(HipMount(
                    x=half_len * math.cos(angle),
                    y=half_wid * math.sin(angle),
                    angle=angle,
                ))

        self._default_feet = [
            default_foot_position(m, geom, config.stance_height) for m in self._mounts
        ]

    def compute_targets(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        """Compute 3-DOF joint targets for one tick.

        Returns (targets_rad, new_phase) where targets_rad maps each
        of the 18 joint names to a target angle in radians.
        """
        from isaac_bridge.hexapod_ik import (
            LegGeometry,
            body_to_hip_frame,
            inverse_kinematics,
        )

        if self._mounts is None:
            self._init_geometry(config)
        assert self._mounts is not None
        assert self._default_feet is not None

        n_legs = len(self._mounts)
        geom = LegGeometry(
            l_coxa=config.l_coxa,
            l_femur=config.l_femur,
            l_tibia=config.l_tibia,
        )

        if dt_s <= 0:
            # Return neutral stance — solve IK for default feet
            targets: dict[str, float] = {}
            for leg_idx in range(n_legs):
                foot_body = self._default_feet[leg_idx]
                hip_pt = body_to_hip_frame(foot_body, self._mounts[leg_idx])
                angles = inverse_kinematics(hip_pt[0], hip_pt[1], hip_pt[2], geom)
                base = leg_idx * 3
                targets[config.leg_joint_names[base]] = angles.coxa
                targets[config.leg_joint_names[base + 1]] = angles.femur
                targets[config.leg_joint_names[base + 2]] = angles.tibia
            return targets, phase

        # ── 1. Slew-filter commanded inputs ──────────────────────────
        target_vx_norm = max(-1.0, min(1.0, state.vx_mps / config.vx_max_mps))
        target_yaw_norm = max(-1.0, min(1.0, state.yaw_rate_rps / config.yaw_max_rps))
        target_height_norm = max(-1.0, min(1.0, state.body_height_m / config.height_max_m))

        slew_vx_norm = config.slew_vx_mps2 / config.vx_max_mps
        slew_yaw_norm = config.slew_yaw_rps2 / config.yaw_max_rps
        slew_height_norm = config.slew_height_mps2 / config.height_max_m

        self._filtered_vx = _slew(self._filtered_vx, target_vx_norm, slew_vx_norm, dt_s)
        self._filtered_yaw = _slew(self._filtered_yaw, target_yaw_norm, slew_yaw_norm, dt_s)
        self._filtered_height = _slew(self._filtered_height, target_height_norm, slew_height_norm, dt_s)

        # ── 2. Advance gait phase ───────────────────────────────────
        speed_factor = abs(self._filtered_vx)
        phase_rate = config.stride_hz * _TWO_PI * speed_factor
        new_phase = (phase + phase_rate * dt_s) % _TWO_PI

        # ── 3. Per-leg foot trajectory + IK ──────────────────────────
        half_stride = config.stride_length / 2.0
        direction = 1.0 if self._filtered_vx >= 0 else -1.0
        half_wid = config.body_width / 2.0

        targets = {}
        for leg_idx in range(n_legs):
            mount = self._mounts[leg_idx]
            default_foot = self._default_feet[leg_idx]

            # Leg phase with offset
            offset = config.leg_phase_offsets[leg_idx]
            leg_phase = (new_phase / _TWO_PI + offset) % 1.0

            # Yaw differential: scale stride by hip lateral position
            yaw_scale = 1.0
            if half_wid > 1e-9:
                yaw_scale = 1.0 + self._filtered_yaw * (mount.y / half_wid)

            stride = half_stride * speed_factor * direction * yaw_scale

            # Compute foot displacement along the mount angle
            if leg_phase < config.duty_factor:
                # Stance phase: push linearly backward
                stance_frac = leg_phase / config.duty_factor
                dx = -stride * (stance_frac * 2.0 - 1.0)
                dz = 0.0
            else:
                # Swing phase: lift + move forward
                swing_frac = (leg_phase - config.duty_factor) / (1.0 - config.duty_factor)
                dx = stride * (swing_frac * 2.0 - 1.0)
                dz = config.step_height * math.sin(math.pi * swing_frac)

            # Body height offset
            height_offset = self._filtered_height * config.height_max_m

            # Foot position in body frame
            foot_x = default_foot[0] + dx * math.cos(mount.angle)
            foot_y = default_foot[1] + dx * math.sin(mount.angle)
            foot_z = default_foot[2] + dz + height_offset

            # Transform to hip frame and solve IK
            hip_pt = body_to_hip_frame((foot_x, foot_y, foot_z), mount)
            angles = inverse_kinematics(hip_pt[0], hip_pt[1], hip_pt[2], geom)

            base = leg_idx * 3
            targets[config.leg_joint_names[base]] = angles.coxa
            targets[config.leg_joint_names[base + 1]] = angles.femur
            targets[config.leg_joint_names[base + 2]] = angles.tibia

        return targets, new_phase


class PolicyController:
    """Residual RL policy controller.

    Blends a base controller's output with a JIT-traced residual policy:
    ``target = base_target + alpha * residual``.

    Implements the ``Controller`` protocol so it drops into the teleop
    loop with zero changes to the runtime tick path.
    """

    def __init__(
        self,
        policy_path: str,
        alpha: float = 0.3,
        *,
        base_controller: HexapodTripodController | None = None,
    ) -> None:
        self._alpha = alpha
        self._base = base_controller or HexapodTripodController()
        self._policy: Any = None
        self._joint_names: list[str] | None = None
        self._obs_mean: Any = None
        self._obs_std: Any = None
        self._policy_path = policy_path

        self._load_policy(policy_path)

    def _load_policy(self, policy_path: str) -> None:
        """Load JIT-traced policy and deployment config."""
        policy_file = Path(policy_path)
        if not policy_file.is_file():
            logger.warning(
                "Policy file not found: %s — running base controller only",
                policy_path,
            )
            return

        try:
            import torch  # type: ignore[import-not-found]
            self._policy = torch.jit.load(str(policy_file), map_location="cpu")
            self._policy.eval()
            logger.info("Loaded JIT policy from %s", policy_path)
        except ImportError:
            logger.warning("PyTorch not available — running base controller only")
            return
        except Exception as exc:
            logger.warning("Failed to load policy %s: %s", policy_path, exc)
            return

        # Load deployment config if present
        config_file = policy_file.parent / "deployment_config.json"
        if config_file.is_file():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
                self._joint_names = config.get("joint_names")
                self._alpha = config.get("alpha", self._alpha)
                logger.info(
                    "Deployment config: %d joints, alpha=%.2f",
                    len(self._joint_names or []),
                    self._alpha,
                )
            except Exception as exc:
                logger.warning("Failed to load deployment config: %s", exc)

        # Load normalization params if present
        norm_file = policy_file.parent / "normalization_params.json"
        if norm_file.is_file():
            try:
                import torch  # type: ignore[import-not-found]
                norm = json.loads(norm_file.read_text(encoding="utf-8"))
                self._obs_mean = torch.tensor(norm["obs_mean"], dtype=torch.float32)
                self._obs_std = torch.tensor(norm["obs_std"], dtype=torch.float32)
            except Exception as exc:
                logger.warning("Failed to load normalization params: %s", exc)

    @property
    def filtered_vx(self) -> float:
        return self._base.filtered_vx

    @property
    def filtered_yaw(self) -> float:
        return self._base.filtered_yaw

    @property
    def filtered_height(self) -> float:
        return self._base.filtered_height

    def _build_observation(
        self,
        state: TeleopState,
        base_targets: dict[str, float],
        config: TeleopConfig,
        phase: float,
    ) -> Any:
        """Build observation tensor for the policy network.

        Layout: [vx, yaw, height, phase_sin, phase_cos,
                 *base_targets_values, ...zero-padded to obs_dim]
        """
        import torch  # type: ignore[import-not-found]

        obs_list: list[float] = [
            state.vx_mps,
            state.yaw_rate_rps,
            state.body_height_m,
            math.sin(phase),
            math.cos(phase),
        ]
        # Add base targets in joint order
        for name in config.joint_names:
            obs_list.append(base_targets.get(name, 0.0))

        obs = torch.tensor(obs_list, dtype=torch.float32).unsqueeze(0)

        # Normalize if params available
        if self._obs_mean is not None and self._obs_std is not None:
            # Pad/truncate normalization to match obs size
            obs_len = obs.shape[1]
            mean = self._obs_mean[:obs_len] if len(self._obs_mean) >= obs_len else self._obs_mean
            std = self._obs_std[:obs_len] if len(self._obs_std) >= obs_len else self._obs_std
            if len(mean) == obs_len:
                obs = (obs - mean.unsqueeze(0)) / (std.unsqueeze(0) + 1e-8)

        return obs

    def compute_targets(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        """Compute blended targets: base + alpha * residual."""
        # Always compute base targets (handles phase, slew filtering)
        base_targets, new_phase = self._base.compute_targets(
            state, dt_s, config, phase,
        )

        # If no policy loaded, return base targets only
        if self._policy is None:
            return base_targets, new_phase

        try:
            import torch  # type: ignore[import-not-found]

            obs = self._build_observation(state, base_targets, config, phase)
            with torch.no_grad():
                residual = self._policy(obs)

            # Blend: base + alpha * residual
            joint_names = list(config.joint_names)
            blended: dict[str, float] = {}
            for i, name in enumerate(joint_names):
                base_val = base_targets.get(name, 0.0)
                res_val = float(residual[0, i]) if i < residual.shape[1] else 0.0
                blended[name] = base_val + self._alpha * res_val
            return blended, new_phase

        except Exception as exc:
            logger.warning("Policy inference failed: %s — using base targets", exc)
            return base_targets, new_phase


def clamp_targets(
    targets: dict[str, float],
    limits: dict[str, tuple[float, float]],
) -> tuple[dict[str, float], int]:
    """Clamp joint targets to limits. Returns (clamped_targets, clamp_count)."""
    clamped: dict[str, float] = {}
    clamp_count = 0
    for name, value in targets.items():
        if name in limits:
            lo, hi = limits[name]
            value, was_clamped = _clamp(value, lo, hi)
            if was_clamped:
                clamp_count += 1
        clamped[name] = value
    return clamped, clamp_count


# ── Controller registry ──────────────────────────────────────────────
#
# Maps controller_type strings to factory callables.  Each factory
# takes a TeleopConfig and returns a Controller instance.
#
# To add a new controller: define the class above, then register it:
#   _CONTROLLER_REGISTRY["my_type"] = lambda cfg: MyController(...)

_CONTROLLER_REGISTRY: dict[str, Any] = {
    "hexapod_1dof_tripod": lambda cfg: HexapodTripodController(),
    "hexapod_3dof_tripod": lambda cfg: Hexapod3DOFController(),
    "rl_residual": lambda cfg: PolicyController(
        policy_path=cfg.policy_path,
        alpha=cfg.alpha,
    ),
}


def create_controller(config: TeleopConfig) -> Controller:
    """Instantiate a controller from the registry.

    Raises ``ValueError`` if ``config.controller_type`` is not registered.
    """
    factory = _CONTROLLER_REGISTRY.get(config.controller_type)
    if factory is None:
        available = sorted(_CONTROLLER_REGISTRY.keys())
        raise ValueError(
            f"Unknown controller_type {config.controller_type!r}. "
            f"Available: {available}"
        )
    return factory(config)
