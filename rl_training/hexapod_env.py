"""Isaac Sim vectorized environment for hexapod locomotion RL.

Uses ``omni.isaac.core`` directly (GridCloner + ArticulationView) to
run thousands of parallel hexapods for PPO training.  No Isaac Lab
dependency — works with any Isaac Sim 2023.1+ install.

Observation space (68 dims for 18-joint hexapod):
    base_lin_vel(3), base_ang_vel(3), projected_gravity(3),
    velocity_commands(3), gait_clock(2),
    joint_pos_relative(18), joint_vel(18), prev_actions(18)

The gait phase clock (sin/cos pair) provides a temporal signal so the
policy can learn periodic gaits.  Phase advances proportional to
``stride_frequency * max(|vx_cmd|, |yaw_cmd|)`` — it ticks when
commands are nonzero and freezes at zero command (standing).

Action space (18 dims): joint position offsets scaled by ``action_scale``.

Episode: 20s max, terminates on fall (height < threshold, |roll/pitch| > 1 rad).
Physics: 200 Hz, control at 50 Hz (decimation=4).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("solidmind.hexapod_env")


@dataclass
class HexapodEnvConfig:
    """Configuration for the hexapod locomotion environment."""

    urdf_path: str = ""
    num_envs: int = 4096
    env_spacing: float = 2.5
    num_joints: int = 18
    physics_dt: float = 1.0 / 200.0
    decimation: int = 4
    episode_length_s: float = 20.0
    action_scale: float = 0.25  # radians — scalar fallback (used if action_scale_per_joint empty)
    action_scale_per_joint: list[float] = field(default_factory=list)  # per-DOF scales

    # Robot geometry
    standing_height_m: float = 0.09
    base_link: str = "base_link"
    foot_links: list[str] = field(default_factory=list)
    total_mass_kg: float = 1.0

    # Joint config
    joint_names: list[str] = field(default_factory=list)
    joint_lower_limits: list[float] = field(default_factory=list)
    joint_upper_limits: list[float] = field(default_factory=list)
    default_joint_positions: list[float] = field(default_factory=list)

    # Actuator (compliant PD — matches legged_gym / Walk-These-Ways defaults)
    actuator_stiffness: float = 15.0
    actuator_damping: float = 0.5

    # Domain randomization
    mass_randomization_pct: float = 20.0
    friction_randomization_pct: float = 30.0
    push_velocity_range: float = 0.5  # m/s
    push_interval_s: float = 8.0

    # Termination (tight thresholds — terminate early when falling)
    min_height_m: float = 0.10
    max_roll_pitch_rad: float = 0.50

    # Velocity command ranges (include zero so policy learns to stand)
    lin_vel_x_range: tuple[float, float] = (0.0, 0.3)
    lin_vel_y_range: tuple[float, float] = (-0.1, 0.1)
    ang_vel_z_range: tuple[float, float] = (-0.3, 0.3)
    command_resample_interval_s: float = 10.0

    # Gait phase clock
    stride_frequency: float = 2.0  # Hz — full gait cycles per second at max speed

    @property
    def obs_dim(self) -> int:
        """Observation dimensionality: 14 + 3 * num_joints (includes gait clock)."""
        return 14 + 3 * self.num_joints

    @property
    def action_dim(self) -> int:
        return self.num_joints

    @property
    def control_dt(self) -> float:
        return self.physics_dt * self.decimation

    @property
    def max_episode_steps(self) -> int:
        return int(self.episode_length_s / self.control_dt)

    @classmethod
    def from_env_config_module(cls, mod: Any) -> HexapodEnvConfig:
        """Build from a generated env config module (env_configurator output)."""
        cfg = cls(
            urdf_path=getattr(mod, "URDF_PATH", ""),
            num_envs=getattr(mod, "NUM_ENVS", 4096),
            env_spacing=getattr(mod, "ENV_SPACING", 2.5),
            num_joints=getattr(mod, "NUM_JOINTS", 18),
            physics_dt=getattr(mod, "PHYSICS_DT", 1.0 / 200.0),
            decimation=getattr(mod, "DECIMATION", 4),
            standing_height_m=getattr(mod, "STANDING_HEIGHT_M", 0.09),
            base_link=getattr(mod, "BASE_LINK", "base_link"),
            foot_links=list(getattr(mod, "FOOT_LINKS", [])),
            total_mass_kg=getattr(mod, "TOTAL_MASS_KG", 1.0),
            joint_names=list(getattr(mod, "JOINT_NAMES", [])),
            joint_lower_limits=list(getattr(mod, "JOINT_LOWER_LIMITS", [])),
            joint_upper_limits=list(getattr(mod, "JOINT_UPPER_LIMITS", [])),
            default_joint_positions=list(getattr(mod, "DEFAULT_JOINT_POSITIONS", [])),
            actuator_stiffness=getattr(mod, "ACTUATOR_STIFFNESS", 400.0),
            actuator_damping=getattr(mod, "ACTUATOR_DAMPING", 30.0),
            action_scale_per_joint=list(getattr(mod, "ACTION_SCALE_PER_JOINT", [])),
        )
        if not cfg.default_joint_positions:
            cfg.default_joint_positions = [0.0] * cfg.num_joints
        return cfg


class HexapodLocomotionEnv:
    """Vectorized hexapod locomotion environment for RL training.

    Uses Isaac Sim's ``omni.isaac.core`` API:
    - ``GridCloner`` for parallel environment creation
    - ``ArticulationView`` for batched joint read/write
    - ``RigidPrimView`` for base body state queries

    All tensors are GPU-resident (torch.Tensor on ``cuda:0``).
    """

    def __init__(self, cfg: HexapodEnvConfig) -> None:
        self.cfg = cfg
        self.device = "cuda:0"
        self._is_initialized = False
        self.render = False  # Set True for GUI mode (non-headless)

        # These are populated by _setup_scene()
        self._world: Any = None
        self._articulation_view: Any = None
        self._base_view: Any = None

        # Tensors (populated on first reset)
        self._obs_buf: Any = None  # (num_envs, obs_dim)
        self._prev_actions: Any = None  # (num_envs, action_dim)
        self._episode_lengths: Any = None  # (num_envs,)
        self._velocity_commands: Any = None  # (num_envs, 3) [vx, vy, yaw]
        self._default_joint_pos: Any = None  # (num_envs, num_joints)
        self._push_timer: Any = None  # (num_envs,)
        self._command_timer: Any = None  # (num_envs,)

        # Two-step-ago actions for DOF acceleration penalty
        self._prev_prev_actions: Any = None  # (num_envs, action_dim)

        # Feet air time tracking
        self._last_contact_time: Any = None  # (num_envs, num_feet)

        # Gravity vector in world frame
        self._gravity_vec: Any = None  # (3,) = [0, 0, -9.81]

        # Cached physics state (populated by _read_physics_state)
        self._cached_root_pos: Any = None
        self._cached_root_quat: Any = None
        self._cached_root_vel: Any = None
        self._cached_joint_pos: Any = None
        self._cached_joint_vel: Any = None

        # Episode stats tracking
        self._fall_count: int = 0
        self._timeout_count: int = 0
        self._episode_lengths_completed: list[int] = []

        # Cached capability flags (set in initialize) — avoids hasattr in hot loop
        self._has_foot_view: bool = False
        self._has_base_view: bool = False

    def initialize(self) -> None:
        """Set up the Isaac Sim scene with cloned environments.

        Must be called after Isaac Sim app is initialized.
        """
        import torch
        from omni.isaac.cloner import GridCloner
        from omni.isaac.core import World
        from omni.isaac.core.articulations import ArticulationView
        from omni.isaac.core.utils.prims import define_prim

        log.info("Initializing HexapodLocomotionEnv with %d envs", self.cfg.num_envs)

        self._world = World(
            physics_dt=self.cfg.physics_dt,
            rendering_dt=self.cfg.physics_dt * self.cfg.decimation,
            backend="torch",
            device=self.device,
        )

        # Create envs scope (env_0 will be created by MovePrim below)
        define_prim("/World/envs", "Scope")

        # Import URDF using omni.kit.commands (same pattern as runtime_isaac.py)
        import omni.kit.commands  # type: ignore[import-not-found]
        import omni.usd  # type: ignore[import-not-found]

        _ok, import_config = omni.kit.commands.execute("URDFCreateImportConfig")
        import_config.merge_fixed_joints = True
        import_config.fix_base = False

        # Drive type enum discovery (Isaac Sim 4.x uses typed enums)
        import sys as _sys
        _urdf_mod = _sys.modules.get("isaacsim.asset.importer.urdf._urdf")
        if _urdf_mod is None:
            try:
                import isaacsim.asset.importer.urdf._urdf as _urdf_mod  # type: ignore[import-not-found]
            except ImportError:
                _urdf_mod = None
        if _urdf_mod is not None:
            _UrdfJointTargetType = getattr(_urdf_mod, "UrdfJointTargetType", None)
            if _UrdfJointTargetType is not None:
                import_config.default_drive_type = _UrdfJointTargetType.JOINT_DRIVE_POSITION
            else:
                import_config.default_drive_type = 0  # position
        else:
            import_config.default_drive_type = 0

        # Stiffness/damping attrs vary across versions
        _cfg_attrs = dir(import_config)
        for attr, value in [
            ("default_drive_strength", self.cfg.actuator_stiffness),
            ("default_drive_stiffness", self.cfg.actuator_stiffness),
            ("default_drive_damping", self.cfg.actuator_damping),
        ]:
            if attr in _cfg_attrs:
                try:
                    setattr(import_config, attr, value)
                except Exception:
                    pass

        # Import URDF — this places the robot at stage root (e.g., /Hexapod_18DOF)
        _ok2, imported_prim_path = omni.kit.commands.execute(
            "URDFParseAndImportFile",
            urdf_path=self.cfg.urdf_path,
            import_config=import_config,
        )
        if not imported_prim_path:
            raise RuntimeError(f"URDF import failed for {self.cfg.urdf_path}")
        log.info("URDF imported at %s", imported_prim_path)

        # Move robot to /World/envs/env_0 using MovePrim (preserves joint refs).
        # Sdf.BatchNamespaceEdit breaks physics joint targets; MovePrim does not.
        omni.kit.commands.execute(
            "MovePrim",
            path_from=imported_prim_path,
            path_to="/World/envs/env_0",
        )
        log.info("Robot moved to /World/envs/env_0")

        # Clone environments using GridCloner
        cloner = GridCloner(spacing=self.cfg.env_spacing)
        cloner.define_base_env("/World/envs")
        prim_paths = cloner.generate_paths("/World/envs/env", self.cfg.num_envs)
        cloner.clone(
            source_prim_path="/World/envs/env_0",
            prim_paths=prim_paths,
        )

        # Add ground plane
        self._world.scene.add_ground_plane()

        # Create ArticulationView for batched joint access
        # env_0 IS the robot (MovePrim placed it there directly)
        art_path = "/World/envs/env_*"
        self._articulation_view = ArticulationView(
            prim_paths_expr=art_path,
            name="hexapod_view",
        )
        self._world.scene.add(self._articulation_view)

        # Reset world to finalize physics
        self._world.reset()

        n = self.cfg.num_envs
        nj = self.cfg.num_joints

        # Default joint positions tensor
        default_pos = torch.tensor(
            self.cfg.default_joint_positions[:nj] if self.cfg.default_joint_positions else [0.0] * nj,
            dtype=torch.float32, device=self.device,
        )
        self._default_joint_pos = default_pos.unsqueeze(0).expand(n, -1).clone()

        # Observation and state buffers
        self._obs_buf = torch.zeros(n, self.cfg.obs_dim, dtype=torch.float32, device=self.device)
        self._prev_actions = torch.zeros(n, self.cfg.action_dim, dtype=torch.float32, device=self.device)
        self._episode_lengths = torch.zeros(n, dtype=torch.int32, device=self.device)

        # Velocity commands: [vx, vy, yaw_rate]
        self._velocity_commands = torch.zeros(n, 3, dtype=torch.float32, device=self.device)

        # Timers for domain randomization
        self._push_timer = torch.zeros(n, dtype=torch.float32, device=self.device)
        self._command_timer = torch.zeros(n, dtype=torch.float32, device=self.device)

        # Gait phase clock — phase in [0, 1) per environment
        self._gait_phase = torch.zeros(n, dtype=torch.float32, device=self.device)

        # Two-step-ago actions for DOF acceleration penalty
        self._prev_prev_actions = torch.zeros(n, self.cfg.action_dim, dtype=torch.float32, device=self.device)

        # Feet air time tracking (6 feet for hexapod)
        n_feet = len(self.cfg.foot_links) if self.cfg.foot_links else 6
        self._last_contact_time = torch.zeros(n, n_feet, dtype=torch.float32, device=self.device)

        # Gravity vector
        self._gravity_vec = torch.tensor([0.0, 0.0, -9.81], dtype=torch.float32, device=self.device)

        # Joint limits
        # Per-joint action scale tensor
        if self.cfg.action_scale_per_joint and len(self.cfg.action_scale_per_joint) >= nj:
            self._action_scale = torch.tensor(
                self.cfg.action_scale_per_joint[:nj],
                dtype=torch.float32, device=self.device,
            )
        else:
            self._action_scale = torch.full(
                (nj,), self.cfg.action_scale,
                dtype=torch.float32, device=self.device,
            )

        self._joint_lower = torch.tensor(
            self.cfg.joint_lower_limits[:nj] if self.cfg.joint_lower_limits else [-math.pi] * nj,
            dtype=torch.float32, device=self.device,
        )
        self._joint_upper = torch.tensor(
            self.cfg.joint_upper_limits[:nj] if self.cfg.joint_upper_limits else [math.pi] * nj,
            dtype=torch.float32, device=self.device,
        )

        # Update capability flags for hot-loop checks
        self._has_foot_view = getattr(self, '_foot_view', None) is not None
        self._has_base_view = self._base_view is not None

        self._is_initialized = True
        log.info("HexapodLocomotionEnv initialized: %d envs, %d joints, obs_dim=%d",
                 n, nj, self.cfg.obs_dim)

    def reset(self, env_ids: Any = None) -> Any:
        """Reset specified environments (or all if None).

        Returns observation tensor (num_envs, obs_dim).
        """
        import torch

        if not self._is_initialized:
            raise RuntimeError("Environment not initialized. Call initialize() first.")

        if env_ids is None:
            env_ids = torch.arange(self.cfg.num_envs, device=self.device)

        n_reset = len(env_ids)

        # Reset joint positions to default + small noise
        noise = torch.randn(n_reset, self.cfg.num_joints, device=self.device) * 0.01
        joint_pos = self._default_joint_pos[env_ids] + noise
        joint_pos = torch.clamp(joint_pos, self._joint_lower, self._joint_upper)
        joint_vel = torch.zeros(n_reset, self.cfg.num_joints, device=self.device)

        self._articulation_view.set_joint_positions(joint_pos, indices=env_ids)
        self._articulation_view.set_joint_velocities(joint_vel, indices=env_ids)

        # Reset root state: upright at standing height
        root_pos = torch.zeros(n_reset, 3, device=self.device)
        root_pos[:, 2] = self.cfg.standing_height_m + 0.01  # small clearance
        root_quat = torch.zeros(n_reset, 4, device=self.device)
        root_quat[:, 0] = 1.0  # w=1, identity quaternion
        root_vel = torch.zeros(n_reset, 6, device=self.device)

        self._articulation_view.set_world_poses(root_pos, root_quat, indices=env_ids)
        self._articulation_view.set_velocities(root_vel, indices=env_ids)

        # Reset buffers
        self._prev_actions[env_ids] = 0.0
        self._prev_prev_actions[env_ids] = 0.0
        self._last_contact_time[env_ids] = 0.0
        self._episode_lengths[env_ids] = 0

        # Resample velocity commands
        self._resample_commands(env_ids)
        self._command_timer[env_ids] = 0.0
        self._push_timer[env_ids] = 0.0
        self._gait_phase[env_ids] = 0.0

        # Read physics state and compute initial observation
        self._read_physics_state()
        self._compute_observations()
        return self._obs_buf

    def step(self, actions: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
        """Execute one control step (with decimation).

        Args:
            actions: (num_envs, action_dim) joint position offsets.

        Returns:
            (observations, rewards, dones, info)
        """
        import torch

        # Clip actions
        actions = torch.clamp(actions, -1.0, 1.0)
        scaled_actions = actions * self._action_scale

        # Target positions: default + action offset
        target_pos = self._default_joint_pos + scaled_actions
        target_pos = torch.clamp(target_pos, self._joint_lower, self._joint_upper)

        # Step physics with decimation (render on last substep if GUI mode)
        render = self.render
        for i in range(self.cfg.decimation):
            self._articulation_view.set_joint_position_targets(target_pos)
            self._world.step(render=render and i == self.cfg.decimation - 1)

        self._episode_lengths += 1

        # Update timers
        dt = self.cfg.control_dt
        self._push_timer += dt
        self._command_timer += dt

        # Apply push disturbances
        push_mask = self._push_timer >= self.cfg.push_interval_s
        if push_mask.any():
            push_ids = torch.where(push_mask)[0]
            self._apply_push(push_ids)
            self._push_timer[push_ids] = 0.0

        # Resample commands periodically
        cmd_mask = self._command_timer >= self.cfg.command_resample_interval_s
        if cmd_mask.any():
            cmd_ids = torch.where(cmd_mask)[0]
            self._resample_commands(cmd_ids)
            self._command_timer[cmd_ids] = 0.0

        # Advance gait phase clock — proportional to command speed
        cmd_vx = self._velocity_commands[:, 0]   # (num_envs,)
        cmd_yaw = self._velocity_commands[:, 2]  # (num_envs,)
        cmd_speed = torch.max(torch.abs(cmd_vx), torch.abs(cmd_yaw))
        self._gait_phase = (self._gait_phase + dt * self.cfg.stride_frequency * cmd_speed) % 1.0

        # Read physics state once (eliminates redundant GPU readbacks)
        self._read_physics_state()

        # Compute observations
        self._compute_observations()

        # Compute rewards
        rewards = self._compute_rewards(actions)

        # Check termination
        dones = self._check_termination()

        # Mild terminal penalty — future-reward loss is the primary anti-flip signal
        fall_mask = dones & (self._episode_lengths < self.cfg.max_episode_steps)
        timeout_mask = dones & (self._episode_lengths >= self.cfg.max_episode_steps)
        rewards = torch.where(fall_mask, rewards - 20.0, rewards)

        # Track episode stats — gate behind dones.any() for zero-cost no-termination steps
        if dones.any():
            done_ids = torch.where(dones)[0]
            n_falls = int(fall_mask.sum().item())
            n_timeouts = int(timeout_mask.sum().item())
            self._fall_count += n_falls
            self._timeout_count += n_timeouts
            # Batch transfer of episode lengths (one GPU→CPU sync)
            done_lengths = self._episode_lengths[done_ids].tolist()
            self._episode_lengths_completed.extend(done_lengths)

        # Store previous actions — in-place copy avoids allocation
        self._prev_prev_actions.copy_(self._prev_actions)
        self._prev_actions.copy_(actions)

        # Info dict
        info: dict[str, Any] = {
            "episode_lengths": self._episode_lengths.clone(),
        }

        # Auto-reset terminated environments
        reset_ids = torch.where(dones)[0]
        if len(reset_ids) > 0:
            self.reset(reset_ids)

        return self._obs_buf, rewards, dones, info

    def _read_physics_state(self) -> None:
        """Read all physics state from GPU once per step."""
        self._cached_root_pos, self._cached_root_quat = self._articulation_view.get_world_poses()
        self._cached_root_vel = self._articulation_view.get_velocities()
        self._cached_joint_pos = self._articulation_view.get_joint_positions()
        self._cached_joint_vel = self._articulation_view.get_joint_velocities()

    def get_episode_stats(self) -> dict[str, Any]:
        """Return episode-level diagnostics since last call or init."""
        total = self._fall_count + self._timeout_count
        fall_pct = (100.0 * self._fall_count / total) if total > 0 else 0.0
        mean_ep_len = (
            sum(self._episode_lengths_completed) / len(self._episode_lengths_completed)
            if self._episode_lengths_completed else 0.0
        )
        stats: dict[str, Any] = {
            "fall_count": self._fall_count,
            "timeout_count": self._timeout_count,
            "fall_pct": fall_pct,
            "mean_episode_length": mean_ep_len,
        }
        # Reset counters
        self._fall_count = 0
        self._timeout_count = 0
        self._episode_lengths_completed = []
        return stats

    def _compute_observations(self) -> None:
        """Build observation tensor from cached physics state."""
        import torch

        # Use cached state
        root_quat = self._cached_root_quat
        root_vel = self._cached_root_vel
        joint_pos = self._cached_joint_pos
        joint_vel = self._cached_joint_vel

        # Base linear velocity in body frame
        base_lin_vel = _quat_rotate_inverse(root_quat, root_vel[:, :3])

        # Base angular velocity in body frame
        base_ang_vel = _quat_rotate_inverse(root_quat, root_vel[:, 3:])

        # Projected gravity in body frame
        gravity_world = self._gravity_vec.unsqueeze(0).expand(self.cfg.num_envs, -1)
        projected_gravity = _quat_rotate_inverse(root_quat, gravity_world)

        # Joint positions relative to default
        joint_pos_rel = joint_pos - self._default_joint_pos

        # Gait phase clock: sin/cos pair for periodic signal
        phase_angle = self._gait_phase.unsqueeze(-1) * (2.0 * math.pi)  # (n, 1)
        gait_clock = torch.cat([
            torch.sin(phase_angle),
            torch.cos(phase_angle),
        ], dim=-1)  # (n, 2)

        # Build observation: [base_lin_vel(3), base_ang_vel(3), proj_gravity(3),
        #                     commands(3), gait_clock(2),
        #                     joint_pos_rel(N), joint_vel(N), prev_actions(N)]
        self._obs_buf = torch.cat([
            base_lin_vel,           # 3
            base_ang_vel,           # 3
            projected_gravity,      # 3
            self._velocity_commands, # 3
            gait_clock,             # 2
            joint_pos_rel,          # num_joints
            joint_vel,              # num_joints
            self._prev_actions,     # num_joints
        ], dim=-1)

    def _compute_rewards(self, actions: Any) -> Any:
        """Compute per-environment reward using Walk-These-Ways composition.

        Imports from rewards_vectorized for GPU-batched computation.
        """
        import torch

        root_pos = self._cached_root_pos
        root_quat = self._cached_root_quat
        root_vel = self._cached_root_vel
        joint_pos = self._cached_joint_pos
        joint_vel = self._cached_joint_vel

        # Base velocities in body frame
        base_lin_vel = _quat_rotate_inverse(root_quat, root_vel[:, :3])
        base_ang_vel = _quat_rotate_inverse(root_quat, root_vel[:, 3:])

        # Projected gravity
        gravity_world = self._gravity_vec.unsqueeze(0).expand(self.cfg.num_envs, -1)
        projected_gravity = _quat_rotate_inverse(root_quat, gravity_world)

        # Get joint torques (effort)
        joint_torques = self._articulation_view.get_applied_joint_efforts()

        # Feet air time reward — compute from foot contact forces
        from rl_training.rewards_vectorized import (
            compute_feet_air_time_reward,
            compute_locomotion_reward,
        )

        feet_air_reward = None
        try:
            # Get contact forces for foot links
            if self._has_foot_view:
                foot_forces = self._foot_view.get_net_contact_forces()  # (N, num_feet, 3)
                feet_air_reward, self._last_contact_time = compute_feet_air_time_reward(
                    foot_contact_forces=foot_forces,
                    last_contact_time=self._last_contact_time,
                    dt=self.cfg.control_dt,
                )
        except Exception:
            pass  # Gracefully degrade if contact forces unavailable

        # Undesired contacts: count non-foot body links touching ground
        undesired_contacts = None
        try:
            if self._has_base_view:
                base_forces = self._base_view.get_net_contact_forces()  # (N, 3)
                force_mag = torch.norm(base_forces, dim=-1)  # (N,)
                undesired_contacts = (force_mag > 1.0).float()
        except Exception:
            pass

        return compute_locomotion_reward(
            base_lin_vel=base_lin_vel,
            base_ang_vel=base_ang_vel,
            projected_gravity=projected_gravity,
            velocity_commands=self._velocity_commands,
            joint_torques=joint_torques,
            joint_velocities=joint_vel,
            actions=actions,
            prev_actions=self._prev_actions,
            prev_prev_actions=self._prev_prev_actions,
            base_height=root_pos[:, 2],
            target_height=self.cfg.standing_height_m,
            feet_air_time_reward=feet_air_reward,
            undesired_contact_count=undesired_contacts,
            joint_positions=joint_pos,
            joint_lower_limits=self._joint_lower,
            joint_upper_limits=self._joint_upper,
        )

    def _check_termination(self) -> Any:
        """Check termination conditions: fall detection and episode timeout."""
        import torch

        root_pos = self._cached_root_pos
        root_quat = self._cached_root_quat

        # Height check
        too_low = root_pos[:, 2] < self.cfg.min_height_m

        # Orientation check: |roll| or |pitch| > threshold
        # Extract roll/pitch from quaternion
        roll, pitch, _ = _quat_to_euler(root_quat)
        too_tilted = (torch.abs(roll) > self.cfg.max_roll_pitch_rad) | (
            torch.abs(pitch) > self.cfg.max_roll_pitch_rad
        )

        # Episode timeout
        timed_out = self._episode_lengths >= self.cfg.max_episode_steps

        return too_low | too_tilted | timed_out

    def _resample_commands(self, env_ids: Any) -> None:
        """Resample velocity commands for specified environments."""
        import torch

        n = len(env_ids)
        self._velocity_commands[env_ids, 0] = torch.empty(n, device=self.device).uniform_(
            *self.cfg.lin_vel_x_range
        )
        self._velocity_commands[env_ids, 1] = torch.empty(n, device=self.device).uniform_(
            *self.cfg.lin_vel_y_range
        )
        self._velocity_commands[env_ids, 2] = torch.empty(n, device=self.device).uniform_(
            *self.cfg.ang_vel_z_range
        )

    def _apply_push(self, env_ids: Any) -> None:
        """Apply random velocity push for domain randomization."""
        import torch

        n = len(env_ids)
        push_vel = torch.zeros(n, 6, device=self.device)
        push_vel[:, :2] = torch.empty(n, 2, device=self.device).uniform_(
            -self.cfg.push_velocity_range, self.cfg.push_velocity_range
        )

        current_vel = self._articulation_view.get_velocities()[env_ids]
        self._articulation_view.set_velocities(current_vel + push_vel, indices=env_ids)

    @property
    def num_envs(self) -> int:
        return self.cfg.num_envs

    @property
    def obs_dim(self) -> int:
        return self.cfg.obs_dim

    @property
    def action_dim(self) -> int:
        return self.cfg.action_dim

    def close(self) -> None:
        """Clean up the simulation."""
        if self._world is not None:
            self._world.stop()


# ── Quaternion utilities (GPU-batched) ────────────────────────────────

def _quat_rotate_inverse(q: Any, v: Any) -> Any:
    """Rotate vectors by the inverse of quaternions (batched).

    q: (N, 4) wxyz quaternions
    v: (N, 3) vectors
    Returns: (N, 3) rotated vectors
    """
    import torch

    # Extract components (Isaac uses wxyz convention)
    w, _x, _y, _z = q[:, 0:1], q[:, 1:2], q[:, 2:3], q[:, 3:4]

    # Conjugate rotation: q* v q
    # Using the formula: v' = v + 2w(u x v) + 2(u x (u x v))
    # where u = [x, y, z]
    u = q[:, 1:4]  # (N, 3)
    uv = torch.cross(u, v, dim=-1)  # (N, 3)
    uuv = torch.cross(u, uv, dim=-1)  # (N, 3)
    return v + 2.0 * (w * uv + uuv)


def _quat_to_euler(q: Any) -> tuple[Any, Any, Any]:
    """Convert wxyz quaternions to roll, pitch, yaw (batched).

    Returns: (roll, pitch, yaw) each (N,) tensors.
    """
    import torch

    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]

    # Roll (x-axis rotation)
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = torch.atan2(sinr_cosp, cosr_cosp)

    # Pitch (y-axis rotation)
    sinp = 2.0 * (w * y - z * x)
    sinp = torch.clamp(sinp, -1.0, 1.0)
    pitch = torch.asin(sinp)

    # Yaw (z-axis rotation)
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = torch.atan2(siny_cosp, cosy_cosp)

    return roll, pitch, yaw
