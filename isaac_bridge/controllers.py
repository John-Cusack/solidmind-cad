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
from typing import TYPE_CHECKING, Any

from isaac_bridge.models import Controller, TeleopConfig, TeleopState

if TYPE_CHECKING:
    from isaac_bridge.hexapod_ik import HipMount

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


def _body_to_world(
    bx: float,
    by: float,
    bz: float,
    wx: float,
    wy: float,
    wyaw: float,
) -> tuple[float, float, float]:
    """Rotate+translate a body-frame point to world frame."""
    c = math.cos(wyaw)
    s = math.sin(wyaw)
    return (wx + c * bx - s * by, wy + s * bx + c * by, bz)


def _world_to_body(
    wx: float,
    wy: float,
    wz: float,
    body_wx: float,
    body_wy: float,
    body_wyaw: float,
) -> tuple[float, float, float]:
    """Inverse: world-frame point to body frame."""
    dx = wx - body_wx
    dy = wy - body_wy
    c = math.cos(-body_wyaw)
    s = math.sin(-body_wyaw)
    return (c * dx - s * dy, s * dx + c * dy, wz)


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
        self._filtered_height = _slew(
            self._filtered_height, target_height_norm, slew_height_norm, dt_s
        )

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


class Hexapod2DOFController:
    """2-DOF hexapod tripod gait controller.

    Generates per-leg coxa (yaw) and femur (pitch) targets for a tripod
    gait.  Coxa oscillates sinusoidally to swing legs forward/back.
    Femur lifts during the swing phase so the foot clears the ground,
    then returns to neutral during stance.

    Designed for a rectangular body with 3 legs per side, all pointing
    straight outward.  Uses explicit ``left_legs`` / ``right_legs``
    config lists (same as 1-DOF controller) instead of parsing names.
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
        """Compute coxa + femur joint targets for one tick.

        Returns (targets_rad, new_phase).
        """
        n_legs = len(config.leg_joint_names) // 2

        if dt_s <= 0:
            targets: dict[str, float] = {name: 0.0 for name in config.leg_joint_names}
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
        self._filtered_height = _slew(
            self._filtered_height, target_height_norm, slew_height_norm, dt_s
        )

        # ── 2. Advance gait phase ───────────────────────────────────
        speed_factor = abs(self._filtered_vx)
        phase_rate = config.stride_hz * _TWO_PI * speed_factor
        new_phase = (phase + phase_rate * dt_s) % _TWO_PI

        # ── 3. Compute per-leg targets ──────────────────────────────
        amplitude_rad = config.amplitude_deg * _DEG2RAD
        yaw_mix_rad = config.yaw_mix_deg * _DEG2RAD
        height_mix_rad = config.height_mix_deg * _DEG2RAD
        lift_rad = config.lift_deg * _DEG2RAD

        direction = 1.0 if self._filtered_vx >= 0 else -1.0

        # Use explicit left_legs set from config (same as 1-DOF controller).
        left_set = set(config.left_legs)
        set(config.tripod_a)

        targets: dict[str, float] = {}
        for leg_idx in range(n_legs):
            base = leg_idx * 2
            coxa_name = config.leg_joint_names[base]
            femur_name = config.leg_joint_names[base + 1]

            # Leg phase with tripod offset
            offset = config.leg_phase_offsets[leg_idx]
            leg_phase = (new_phase / _TWO_PI + offset) % 1.0

            # Stance/swing split
            in_swing = leg_phase >= config.duty_factor
            swing_frac = (
                (leg_phase - config.duty_factor) / (1.0 - config.duty_factor) if in_swing else 0.0
            )

            # ── Coxa (yaw): sinusoidal oscillation ──────────────────
            joint_phase_rad = leg_phase * _TWO_PI
            oscillation = direction * amplitude_rad * speed_factor * math.sin(joint_phase_rad)

            # Right-side legs are mirror-mounted — negate oscillation.
            is_left = coxa_name in left_set
            if not is_left:
                oscillation = -oscillation

            # Yaw differential for turning
            if is_left:
                yaw_offset = -yaw_mix_rad * self._filtered_yaw
            else:
                yaw_offset = yaw_mix_rad * self._filtered_yaw

            # Height offset on coxa
            height_offset = height_mix_rad * self._filtered_height

            targets[coxa_name] = oscillation + yaw_offset + height_offset

            # ── Femur (pitch): lift during swing, neutral in stance ──
            if in_swing:
                # Sine arc: 0 at start → peak at mid-swing → 0 at end
                femur_target = lift_rad * speed_factor * math.sin(math.pi * swing_frac)
            else:
                femur_target = 0.0

            targets[femur_name] = femur_target

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
        # IK standing bias: (coxa, femur, tibia) angles at neutral stance.
        # Subtracted from all IK output so that URDF joint zero = as-built pose.
        self._standing_bias: list[tuple[float, float, float]] | None = None

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
        from isaac_bridge.hexapod_ik import (
            HipMount,
            LegGeometry,
            body_to_hip_frame,
            default_foot_position,
            inverse_kinematics,
        )

        # Validate leg_joint_names length matches n_legs * dofs_per_leg
        n_joint_names = len(config.leg_joint_names)
        expected_dpl = config.dofs_per_leg
        if n_joint_names % expected_dpl != 0:
            raise ValueError(
                f"leg_joint_names length ({n_joint_names}) must be a multiple "
                f"of dofs_per_leg ({expected_dpl})"
            )

        geom = LegGeometry(
            l_coxa=config.l_coxa,
            l_femur=config.l_femur,
            l_tibia=config.l_tibia,
        )
        n_legs = n_joint_names // expected_dpl

        # Use explicit hip mounts from config if provided
        if config.hip_mounts and len(config.hip_mounts) == n_legs:
            self._mounts = [HipMount(x=m[0], y=m[1], angle=m[2]) for m in config.hip_mounts]
        elif n_legs == 6:
            half_len = config.body_length / 2.0
            half_wid = config.body_width / 2.0
            # Compute mounting angle from body geometry so it matches URDF.
            # Front angle = atan2(half_wid, half_len) ≈ 47° for the
            # hexapod_18dof_hybrid_v2 URDF (vs the old hard-coded 45°).
            front_angle = math.atan2(half_wid, half_len)
            rear_angle = math.pi - front_angle
            # Standard 6-leg layout: LF, LM, LR, RF, RM, RR
            self._mounts = [
                HipMount(x=half_len, y=half_wid, angle=front_angle),  # LF
                HipMount(x=0.0, y=half_wid, angle=math.pi / 2),  # LM
                HipMount(x=-half_len, y=half_wid, angle=rear_angle),  # LR
                HipMount(x=half_len, y=-half_wid, angle=-front_angle),  # RF
                HipMount(x=0.0, y=-half_wid, angle=-math.pi / 2),  # RM
                HipMount(x=-half_len, y=-half_wid, angle=-rear_angle),  # RR
            ]
        else:
            # Fallback: distribute legs evenly around the body
            half_len = config.body_length / 2.0
            half_wid = config.body_width / 2.0
            self._mounts = []
            for i in range(n_legs):
                angle = 2.0 * math.pi * i / n_legs
                self._mounts.append(
                    HipMount(
                        x=half_len * math.cos(angle),
                        y=half_wid * math.sin(angle),
                        angle=angle,
                    )
                )

        self._default_feet = [
            default_foot_position(m, geom, config.stance_height) for m in self._mounts
        ]

        # Compute standing bias: IK angles at the neutral foot position.
        # These are subtracted from all IK outputs so that URDF zero = as-built.
        self._standing_bias = []
        for leg_idx in range(n_legs):
            foot = self._default_feet[leg_idx]
            hip_pt = body_to_hip_frame(foot, self._mounts[leg_idx])
            angles = inverse_kinematics(hip_pt[0], hip_pt[1], hip_pt[2], geom)
            self._standing_bias.append((angles.coxa, angles.femur, angles.tibia))
            logger.debug(
                "Leg %d standing bias: coxa=%.3f femur=%.3f tibia=%.3f",
                leg_idx,
                angles.coxa,
                angles.femur,
                angles.tibia,
            )

        # (World-frame foot tracking removed — body-frame approach is
        # simpler and avoids dead-reckoning drift.)

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
            # Return neutral stance — all URDF joints at zero (as-built pose)
            targets: dict[str, float] = {}
            for leg_idx in range(n_legs):
                base = leg_idx * 3
                targets[config.leg_joint_names[base]] = 0.0
                targets[config.leg_joint_names[base + 1]] = 0.0
                targets[config.leg_joint_names[base + 2]] = 0.0
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
        self._filtered_height = _slew(
            self._filtered_height, target_height_norm, slew_height_norm, dt_s
        )

        # ── 2. Advance gait phase ───────────────────────────────────
        speed_factor = abs(self._filtered_vx)
        phase_rate = config.stride_hz * _TWO_PI * speed_factor
        new_phase = (phase + phase_rate * dt_s) % _TWO_PI

        # ── 3. Per-leg foot trajectory (body-frame) + IK ─────────────
        # Simple body-frame approach: no dead reckoning.  Each foot
        # offsets from its default position along body +X (forward).
        # Stance: slides from +half_stride to -half_stride (body moves
        # forward over planted foot).  Swing: arcs from -half_stride
        # back to +half_stride with a vertical lift.
        half_stride = config.stride_length / 2.0 * speed_factor
        direction = 1.0 if self._filtered_vx >= 0 else -1.0

        targets = {}
        for leg_idx in range(n_legs):
            mount = self._mounts[leg_idx]
            default_foot = self._default_feet[leg_idx]

            # Leg phase with offset
            offset = config.leg_phase_offsets[leg_idx]
            leg_phase = (new_phase / _TWO_PI + offset) % 1.0

            in_stance = leg_phase < config.duty_factor

            # Stride offset along body +X (forward direction).
            # This creates a tangential component in the hip frame
            # that makes coxa oscillate, plus a radial component
            # that femur/tibia handle.
            if in_stance:
                # Linear slide: front (+half_stride) to back (-half_stride)
                stance_frac = leg_phase / config.duty_factor
                stride_x = half_stride * (1.0 - 2.0 * stance_frac) * direction
                lift = 0.0
            else:
                # Swing arc: back (-half_stride) to front (+half_stride)
                swing_frac = (leg_phase - config.duty_factor) / (1.0 - config.duty_factor)
                stride_x = half_stride * (-1.0 + 2.0 * swing_frac) * direction
                lift = config.step_height * math.sin(math.pi * swing_frac)

            # Yaw offset: rotate foot placement around body center
            body_half_width = config.body_width / 2.0
            yaw_offset_x = (
                -self._filtered_yaw
                * half_stride
                * (mount.y / max(abs(mount.y), 1e-6))
                * abs(mount.y)
                / max(body_half_width, 1e-6)
            )
            # Simplified: left legs get -yaw, right legs get +yaw
            # (same sign convention as 1-DOF controller)

            foot_x = default_foot[0] + stride_x + yaw_offset_x * direction
            foot_y = default_foot[1]
            foot_z = default_foot[2] + lift

            # Body height offset
            height_offset = self._filtered_height * config.height_max_m
            foot_z += height_offset

            # Transform to hip frame and solve IK
            hip_pt = body_to_hip_frame((foot_x, foot_y, foot_z), mount)
            angles = inverse_kinematics(hip_pt[0], hip_pt[1], hip_pt[2], geom)

            # Subtract standing bias so URDF zero = as-built pose.
            bias = self._standing_bias[leg_idx]
            base = leg_idx * 3
            targets[config.leg_joint_names[base]] = angles.coxa - bias[0]
            targets[config.leg_joint_names[base + 1]] = angles.femur - bias[1]
            targets[config.leg_joint_names[base + 2]] = angles.tibia - bias[2]

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
            state,
            dt_s,
            config,
            phase,
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


class DirectPolicyController:
    """Direct RL policy controller (no base controller).

    Loads a JIT-traced policy that maps observations directly to joint
    position targets.  Unlike ``PolicyController`` (which adds residuals
    to a base controller), this controller runs the policy as the sole
    source of joint commands.

    Supports two observation layouts (auto-detected from deployment config):

    - ``"isaaclab"`` (66 dims for 18 joints, no gait clock):
        base_lin_vel(3), base_ang_vel(3), projected_gravity(3),
        commands(3), joint_pos_relative(N), joint_vel(N), prev_actions(N)
      Gravity is unit-normalized [0,0,-1].

    - ``"custom"`` (68 dims for 18 joints, legacy):
        base_lin_vel(3), base_ang_vel(3), projected_gravity(3),
        commands(3), gait_clock(2), joint_pos_relative(N),
        joint_vel(N), prev_actions(N)
      Gravity is raw [0,0,-9.81].

    Per-joint action scales are loaded from ``action_scale_per_joint``
    in deployment_config.json, fixing the training/deployment scale mismatch.
    """

    def __init__(
        self,
        policy_path: str,
        action_scale: float = 0.5,
    ) -> None:
        self._action_scale = action_scale
        self._action_scales: list[float] | None = None  # per-joint scales
        self._policy: Any = None
        self._joint_names: list[str] | None = None
        self._default_joint_positions: list[float] | None = None
        self._obs_mean: Any = None
        self._obs_std: Any = None
        self._obs_dim: int = 68
        self._obs_layout: str = "custom"  # "custom" or "isaaclab"
        self._prev_actions: dict[str, float] = {}
        self._normalized_policy: bool = False

        self._filtered_vx: float = 0.0
        self._filtered_yaw: float = 0.0
        self._filtered_height: float = 0.0

        # Gait phase clock (only used for "custom" obs layout)
        self._gait_phase: float = 0.0
        self._stride_frequency: float = 2.0

        self._load_policy(policy_path)

    def _load_policy(self, policy_path: str) -> None:
        """Load JIT-traced policy, normalization, and deployment config."""
        policy_file = Path(policy_path)
        if not policy_file.is_file():
            logger.warning(
                "Policy file not found: %s — controller will output zeros",
                policy_path,
            )
            return

        try:
            import torch  # type: ignore[import-not-found]

            self._policy = torch.jit.load(str(policy_file), map_location="cpu")
            self._policy.eval()
            logger.info("Loaded direct policy from %s", policy_path)
        except ImportError:
            logger.warning("PyTorch not available — controller will output zeros")
        except Exception as exc:
            logger.warning("Failed to load policy %s: %s", policy_path, exc)

        # Load deployment config (even if policy load failed — config is still useful)
        config_file = policy_file.parent / "deployment_config.json"
        if config_file.is_file():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
                self._joint_names = config.get("joint_names")
                self._obs_dim = config.get("obs_dim", self._obs_dim)
                self._default_joint_positions = config.get("default_joint_positions")
                self._normalized_policy = config.get("normalized_policy", False)
                self._stride_frequency = config.get("stride_frequency", self._stride_frequency)
                self._obs_layout = config.get("obs_layout", "custom")

                # Action scale mode: "per_joint" uses per-joint vector,
                # "scalar" uses a single scale for all joints.
                # Default: use per-joint if available (backward compat).
                scale_mode = config.get("action_scale_mode", "auto")
                per_joint = config.get("action_scale_per_joint")
                scalar_scale = config.get("action_scale")

                if scale_mode == "scalar" and scalar_scale is not None:
                    # Explicit scalar mode — ignore per-joint even if present
                    self._action_scale = float(scalar_scale)
                    self._action_scales = None
                    logger.info("Using scalar action scale: %.4f", self._action_scale)
                elif per_joint and isinstance(per_joint, list):
                    self._action_scales = [float(s) for s in per_joint]
                    logger.info(
                        "Loaded per-joint action scales (%d joints)",
                        len(self._action_scales),
                    )
                elif scalar_scale is not None:
                    self._action_scale = float(scalar_scale)
                    logger.info("Using scalar action scale: %.4f", self._action_scale)

                if self._default_joint_positions:
                    logger.info(
                        "Loaded default_joint_positions (%d joints)",
                        len(self._default_joint_positions),
                    )
                logger.info("Obs layout: %s, obs_dim: %d", self._obs_layout, self._obs_dim)
            except Exception as exc:
                logger.warning("Failed to load deployment config: %s", exc)

        # Load normalization params (used only if policy lacks built-in normalization)
        if not self._normalized_policy:
            norm_file = policy_file.parent / "normalization_params.json"
            if norm_file.is_file():
                try:
                    import torch  # type: ignore[import-not-found]

                    norm = json.loads(norm_file.read_text(encoding="utf-8"))
                    self._obs_mean = torch.tensor(norm["obs_mean"], dtype=torch.float32)
                    self._obs_std = torch.tensor(norm["obs_std"], dtype=torch.float32)
                    logger.info("Loaded normalization params (%d dims)", len(norm["obs_mean"]))
                except Exception as exc:
                    logger.warning("Failed to load normalization params: %s", exc)

    @property
    def filtered_vx(self) -> float:
        return self._filtered_vx

    @property
    def filtered_yaw(self) -> float:
        return self._filtered_yaw

    @property
    def filtered_height(self) -> float:
        return self._filtered_height

    def _build_observation(
        self,
        state: TeleopState,
        config: TeleopConfig,
        phase: float,
    ) -> Any:
        """Build observation tensor matching training layout.

        Supports two layouts (selected by ``self._obs_layout``):

        ``"isaaclab"`` (no gait clock, unit gravity):
            base_lin_vel(3), base_ang_vel(3), projected_gravity(3),
            commands(3), joint_pos_rel(N), joint_vel(N), prev_actions(N)

        ``"custom"`` (with gait clock, raw gravity):
            base_lin_vel(3), base_ang_vel(3), projected_gravity(3),
            commands(3), gait_clock(2), joint_pos_rel(N),
            joint_vel(N), prev_actions(N)
        """
        import torch  # type: ignore[import-not-found]

        n_joints = len(config.joint_names)
        obs_list: list[float] = []
        is_isaaclab = self._obs_layout == "isaaclab"

        # base_lin_vel (3) — use real physics feedback if available
        if state.base_lin_vel and len(state.base_lin_vel) >= 3:
            obs_list.extend(state.base_lin_vel[:3])
        else:
            obs_list.extend([self._filtered_vx * config.vx_max_mps, 0.0, 0.0])

        # base_ang_vel (3)
        if state.base_ang_vel and len(state.base_ang_vel) >= 3:
            obs_list.extend(state.base_ang_vel[:3])
        else:
            obs_list.extend([0.0, 0.0, self._filtered_yaw * config.yaw_max_rps])

        # projected_gravity (3)
        # Isaac Lab uses unit gravity [0, 0, -1]; custom env uses raw [0, 0, -9.81].
        # Runtime always provides unit gravity, so scale only for "custom" layout.
        if state.projected_gravity and len(state.projected_gravity) >= 3:
            if is_isaaclab:
                obs_list.extend(state.projected_gravity[:3])
            else:
                obs_list.extend([v * 9.81 for v in state.projected_gravity[:3]])
        else:
            if is_isaaclab:
                obs_list.extend([0.0, 0.0, -1.0])
            else:
                obs_list.extend([0.0, 0.0, -9.81])

        # velocity commands (3)
        obs_list.extend([state.vx_mps, 0.0, state.yaw_rate_rps])

        # gait phase clock (2) — only for "custom" layout
        if not is_isaaclab:
            phase_angle = self._gait_phase * _TWO_PI
            obs_list.extend([math.sin(phase_angle), math.cos(phase_angle)])

        # joint_pos_relative (N) — use real joint positions if available
        if state.joint_positions and len(state.joint_positions) >= n_joints:
            defaults = self._default_joint_positions or [0.0] * n_joints
            for i in range(n_joints):
                default_val = defaults[i] if i < len(defaults) else 0.0
                obs_list.append(state.joint_positions[i] - default_val)
        else:
            obs_list.extend([0.0] * n_joints)

        # joint_vel (N) — use real joint velocities if available
        if state.joint_velocities and len(state.joint_velocities) >= n_joints:
            obs_list.extend(state.joint_velocities[:n_joints])
        else:
            obs_list.extend([0.0] * n_joints)

        # prev_actions (N)
        for name in config.joint_names:
            obs_list.append(self._prev_actions.get(name, 0.0))

        obs = torch.tensor(obs_list, dtype=torch.float32).unsqueeze(0)

        # Normalize (only if policy doesn't have built-in normalization)
        if not self._normalized_policy and self._obs_mean is not None and self._obs_std is not None:
            obs_len = obs.shape[1]
            if len(self._obs_mean) >= obs_len:
                mean = self._obs_mean[:obs_len].unsqueeze(0)
                std = self._obs_std[:obs_len].unsqueeze(0)
                obs = (obs - mean) / (std + 1e-8)

        return obs

    def compute_targets(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        """Compute joint targets directly from policy network."""
        if dt_s <= 0:
            targets = {name: 0.0 for name in config.joint_names}
            return targets, phase

        # Slew-filter commands
        target_vx_norm = max(-1.0, min(1.0, state.vx_mps / config.vx_max_mps))
        target_yaw_norm = max(-1.0, min(1.0, state.yaw_rate_rps / config.yaw_max_rps))
        target_height_norm = max(-1.0, min(1.0, state.body_height_m / config.height_max_m))

        slew_vx = config.slew_vx_mps2 / config.vx_max_mps
        slew_yaw = config.slew_yaw_rps2 / config.yaw_max_rps
        slew_height = config.slew_height_mps2 / config.height_max_m

        self._filtered_vx = _slew(self._filtered_vx, target_vx_norm, slew_vx, dt_s)
        self._filtered_yaw = _slew(self._filtered_yaw, target_yaw_norm, slew_yaw, dt_s)
        self._filtered_height = _slew(self._filtered_height, target_height_norm, slew_height, dt_s)

        # Advance gait phase clock (matches training env logic)
        cmd_speed = max(abs(state.vx_mps), abs(state.yaw_rate_rps))
        self._gait_phase = (self._gait_phase + dt_s * self._stride_frequency * cmd_speed) % 1.0

        # Advance phase (for compatibility with teleop telemetry)
        speed_factor = abs(self._filtered_vx)
        new_phase = (phase + config.stride_hz * _TWO_PI * speed_factor * dt_s) % _TWO_PI

        # If no policy loaded, return zeros
        if self._policy is None:
            targets = {name: 0.0 for name in config.joint_names}
            return targets, new_phase

        try:
            import torch  # type: ignore[import-not-found]

            obs = self._build_observation(state, config, phase)
            with torch.no_grad():
                action_raw = self._policy(obs)

            # Scale actions to joint offsets and add default position
            # Training: target = default_pos + action * scale_per_joint
            # Per-joint scales fix the training/deployment mismatch bug
            targets: dict[str, float] = {}
            joint_names = list(config.joint_names)
            defaults = self._default_joint_positions or [0.0] * len(joint_names)
            for i, name in enumerate(joint_names):
                default_pos = defaults[i] if i < len(defaults) else 0.0
                if i < action_raw.shape[1]:
                    scale = (
                        self._action_scales[i]
                        if self._action_scales and i < len(self._action_scales)
                        else self._action_scale
                    )
                    offset = float(action_raw[0, i]) * scale
                else:
                    offset = 0.0
                targets[name] = default_pos + offset

            # Store for next observation
            self._prev_actions = {
                name: float(action_raw[0, i]) if i < action_raw.shape[1] else 0.0
                for i, name in enumerate(joint_names)
            }

            return targets, new_phase

        except Exception as exc:
            logger.warning("Direct policy inference failed: %s — returning zeros", exc)
            targets = {name: 0.0 for name in config.joint_names}
            return targets, new_phase


class QuadSpinController:
    """Simple controller that spins all joints at a constant velocity.

    Used for quadcopter propellers or any mechanism where joints should
    rotate continuously.  ``vx_mps`` controls RPM (0→0, 1→max_rpm).
    """

    drive_mode: str = "velocity"  # Use velocity drive for continuous spinning

    def __init__(self, *, max_rpm: float = 6000.0) -> None:
        self._max_rpm = max_rpm
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
        # Slew vx toward commanded value
        self._filtered_vx = _slew(
            self._filtered_vx,
            state.vx_mps,
            config.slew_vx_mps2,
            dt_s,
        )
        # Map vx (0–1 range of vx_max) to RPM
        throttle = abs(self._filtered_vx) / config.vx_max_mps if config.vx_max_mps > 0 else 0.0
        throttle = min(1.0, max(0.0, throttle))
        rpm = throttle * self._max_rpm
        rad_per_s = rpm * _TWO_PI / 60.0

        # Advance phase tracking
        new_phase = (phase + rad_per_s * dt_s) % _TWO_PI

        # Return velocity targets (rad/s) — runtime uses joint_velocities
        targets = {name: rad_per_s for name in config.joint_names}
        return targets, new_phase


_CONTROLLER_REGISTRY: dict[str, Any] = {
    "hexapod_1dof_tripod": lambda cfg: HexapodTripodController(),
    "hexapod_2dof_tripod": lambda cfg: Hexapod2DOFController(),
    "hexapod_3dof_tripod": lambda cfg: Hexapod3DOFController(),
    "rl_residual": lambda cfg: PolicyController(
        policy_path=cfg.policy_path,
        alpha=cfg.alpha,
    ),
    "rl_direct": lambda cfg: DirectPolicyController(
        policy_path=cfg.policy_path,
        action_scale=cfg.alpha,  # reuse alpha field as action_scale
    ),
    "quad_spin": lambda cfg: QuadSpinController(),
}


def create_controller(config: TeleopConfig) -> Controller:
    """Instantiate a controller from the registry.

    Raises ``ValueError`` if ``config.controller_type`` is not registered.
    """
    factory = _CONTROLLER_REGISTRY.get(config.controller_type)
    if factory is None:
        available = sorted(_CONTROLLER_REGISTRY.keys())
        raise ValueError(
            f"Unknown controller_type {config.controller_type!r}. Available: {available}"
        )
    return factory(config)
