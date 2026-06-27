"""GPU-vectorized reward functions for hexapod locomotion training.

All functions operate on batched PyTorch tensors (N, ...) and run
entirely on GPU.  Uses the Walk-These-Ways multiplicative composition:

    reward = r_positive * exp(r_negative / temperature)

where r_positive encourages velocity tracking and r_negative penalizes
undesirable behaviors (vertical bounce, orientation error, energy waste).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Pre-computed constant — avoids math.sin() call every step
_STEEP_PITCH_THRESHOLD = math.sin(0.35)  # ~20° tilt boundary


@dataclass(frozen=True, slots=True)
class RewardWeights:
    """Scalar weights for each reward term."""

    # Positive (tracking) terms
    track_lin_vel_xy: float = 1.5
    track_ang_vel_z: float = 0.5
    feet_air_time: float = 0.25
    base_velocity: float = 1.0
    survival: float = 1.0  # strong incentive to stay alive / upright

    # Negative (penalty) terms
    lin_vel_z: float = -2.0
    ang_vel_xy: float = -0.05
    orientation: float = -2.0  # strong upright signal — penalize any tilt
    steep_pitch_penalty: float = -8.0  # harsh quadratic ramp above ~20° tilt
    joint_torques: float = -1e-5
    action_rate: float = -0.01
    base_height: float = -50.0  # strong height tracking — dropping body is heavily penalized
    dof_acceleration: float = -2.5e-7  # smooths joint commands, prevents oscillation
    undesired_contacts: float = -1.0  # penalize non-foot body parts hitting ground
    joint_limit_proximity: float = -2.0  # soft penalty near joint limits

    # Composition temperature (higher = softer penalty curve)
    # Walk-These-Ways default is 5.0 — lower values crush reward signal
    temperature: float = 5.0

    # Tracking kernel width
    tracking_sigma: float = 0.25


def compute_locomotion_reward(
    *,
    base_lin_vel: Any,  # (N, 3) body-frame linear velocity
    base_ang_vel: Any,  # (N, 3) body-frame angular velocity
    projected_gravity: Any,  # (N, 3) gravity in body frame
    velocity_commands: Any,  # (N, 3) [cmd_vx, cmd_vy, cmd_yaw]
    joint_torques: Any,  # (N, num_joints)
    joint_velocities: Any | None = None,  # (N, num_joints) for dof_acc penalty
    actions: Any,  # (N, num_joints)
    prev_actions: Any,  # (N, num_joints)
    prev_prev_actions: Any | None = None,  # (N, num_joints) for dof_acc
    base_height: Any,  # (N,) base z-coordinate
    target_height: float = 0.09,
    feet_air_time_reward: Any | None = None,  # (N,) pre-computed feet air time reward
    undesired_contact_count: Any | None = None,  # (N,) number of non-foot contacts
    joint_positions: Any | None = None,  # (N, num_joints) for joint limit penalty
    joint_lower_limits: Any | None = None,  # (num_joints,) lower joint limits
    joint_upper_limits: Any | None = None,  # (num_joints,) upper joint limits
    weights: RewardWeights | None = None,
) -> Any:
    """Compute total reward for all environments.

    Uses Walk-These-Ways multiplicative composition:
        total = r_positive * exp(r_negative / temperature)

    Args:
        base_lin_vel: Body-frame linear velocity (N, 3).
        base_ang_vel: Body-frame angular velocity (N, 3).
        projected_gravity: Gravity vector in body frame (N, 3).
        velocity_commands: Commanded [vx, vy, yaw_rate] (N, 3).
        joint_torques: Per-joint torques (N, num_joints).
        joint_velocities: Per-joint velocities for DOF acceleration penalty (N, num_joints).
        actions: Current actions (N, num_joints).
        prev_actions: Previous actions (N, num_joints).
        prev_prev_actions: Two-step-ago actions for DOF acceleration (N, num_joints).
        base_height: Robot base height (N,).
        target_height: Target standing height in meters.
        feet_air_time_reward: Pre-computed feet air time reward (N,).
        undesired_contact_count: Count of non-foot body contacts (N,).
        weights: Reward weights. Defaults to standard locomotion weights.

    Returns:
        (N,) total reward per environment.
    """
    import torch

    if weights is None:
        weights = RewardWeights()

    sigma = weights.tracking_sigma

    # ── Positive terms (tracking rewards) ─────────────────────────
    # Linear velocity XY tracking (exponential kernel)
    vel_error_xy = torch.sum((base_lin_vel[:, :2] - velocity_commands[:, :2]) ** 2, dim=-1)
    r_track_lin = torch.exp(-vel_error_xy / (sigma**2))

    # Angular velocity Z tracking
    yaw_error = (base_ang_vel[:, 2] - velocity_commands[:, 2]) ** 2
    r_track_ang = torch.exp(-yaw_error / (sigma**2))

    # Base velocity reward: rewards moving WHEN commanded, stillness when
    # commanded zero.  |cmd_vel| * |actual_vel| is zero when either is zero
    # and positive only when both are nonzero in the same direction.
    cmd_speed_xy = torch.norm(velocity_commands[:, :2], dim=-1)
    actual_speed_xy = torch.norm(base_lin_vel[:, :2], dim=-1)
    r_base_velocity = torch.tanh(cmd_speed_xy * actual_speed_xy * 10.0)

    # Survival bonus: constant reward for staying alive
    r_survival = torch.ones(base_lin_vel.shape[0], device=base_lin_vel.device)

    # Feet air time reward (encourages alternating tripod gait)
    r_feet_air = torch.zeros(base_lin_vel.shape[0], device=base_lin_vel.device)
    if feet_air_time_reward is not None and weights.feet_air_time != 0.0:
        r_feet_air = feet_air_time_reward

    r_positive = (
        weights.track_lin_vel_xy * r_track_lin
        + weights.track_ang_vel_z * r_track_ang
        + weights.base_velocity * r_base_velocity
        + weights.survival * r_survival
        + weights.feet_air_time * r_feet_air
    )

    # ── Negative terms (penalties) ────────────────────────────────
    # Vertical velocity penalty
    r_lin_vel_z = weights.lin_vel_z * base_lin_vel[:, 2] ** 2

    # Roll/pitch angular velocity penalty
    r_ang_vel_xy = weights.ang_vel_xy * torch.sum(base_ang_vel[:, :2] ** 2, dim=-1)

    # Orientation penalty (deviation from upright)
    # projected_gravity should be [0, 0, -1] when upright
    r_orientation = weights.orientation * torch.sum(projected_gravity[:, :2] ** 2, dim=-1)

    # Joint torque penalty
    r_torques = weights.joint_torques * torch.sum(joint_torques**2, dim=-1)

    # Action rate penalty (smoothness)
    r_action_rate = weights.action_rate * torch.sum((actions - prev_actions) ** 2, dim=-1)

    # Base height penalty (deviation from target)
    r_height = weights.base_height * (base_height - target_height) ** 2

    # DOF acceleration penalty — smooths joint commands, prevents oscillation
    r_dof_acc = torch.zeros(base_lin_vel.shape[0], device=base_lin_vel.device)
    if prev_prev_actions is not None and weights.dof_acceleration != 0.0:
        # Finite-difference second derivative of actions
        dof_acc = actions - 2.0 * prev_actions + prev_prev_actions
        r_dof_acc = weights.dof_acceleration * torch.sum(dof_acc**2, dim=-1)

    # Undesired contacts penalty — penalize non-foot body parts hitting ground
    r_contacts = torch.zeros(base_lin_vel.shape[0], device=base_lin_vel.device)
    if undesired_contact_count is not None and weights.undesired_contacts != 0.0:
        r_contacts = weights.undesired_contacts * undesired_contact_count.float()

    # Joint limit proximity penalty — soft penalty when within 10% of limit range
    r_joint_limits = torch.zeros(base_lin_vel.shape[0], device=base_lin_vel.device)
    if (
        joint_positions is not None
        and joint_lower_limits is not None
        and joint_upper_limits is not None
        and weights.joint_limit_proximity != 0.0
    ):
        joint_range = joint_upper_limits - joint_lower_limits
        margin = 0.1 * joint_range
        below = torch.clamp(joint_lower_limits + margin - joint_positions, min=0.0)
        above = torch.clamp(joint_positions - (joint_upper_limits - margin), min=0.0)
        r_joint_limits = weights.joint_limit_proximity * torch.sum(below**2 + above**2, dim=-1)

    # Steep pitch penalty — quadratic ramp above ~20° tilt (sin(0.35) threshold)
    r_steep_pitch = torch.zeros(base_lin_vel.shape[0], device=base_lin_vel.device)
    if weights.steep_pitch_penalty != 0.0:
        tilt_magnitude = torch.norm(projected_gravity[:, :2], dim=-1)  # 0 when upright
        threshold = _STEEP_PITCH_THRESHOLD
        pitch_excess = torch.clamp(tilt_magnitude - threshold, min=0.0)
        r_steep_pitch = weights.steep_pitch_penalty * pitch_excess**2

    r_negative = (
        r_lin_vel_z
        + r_ang_vel_xy
        + r_orientation
        + r_torques
        + r_action_rate
        + r_height
        + r_dof_acc
        + r_contacts
        + r_joint_limits
        + r_steep_pitch
    )

    # ── Multiplicative composition ────────────────────────────────
    total = r_positive * torch.exp(r_negative / weights.temperature)

    return total


def compute_feet_air_time_reward(
    *,
    foot_contact_forces: Any,  # (N, num_feet, 3)
    last_contact_time: Any,  # (N, num_feet) seconds since last contact
    dt: float,
    target_air_time: float = 0.3,
) -> tuple[Any, Any]:
    """Compute feet air time reward to encourage tripod gait pattern.

    Rewards each foot for achieving a target air time between contacts.
    Encourages the natural alternating tripod pattern.

    Args:
        foot_contact_forces: Contact forces per foot (N, num_feet, 3).
        last_contact_time: Time since each foot last contacted ground (N, num_feet).
        dt: Control timestep.
        target_air_time: Desired air time per foot in seconds.

    Returns:
        (reward (N,), updated_last_contact_time (N, num_feet))
    """
    import torch

    # Detect contacts: force magnitude > threshold
    contact_threshold = 1.0  # N
    force_mag = torch.norm(foot_contact_forces, dim=-1)  # (N, num_feet)
    in_contact = force_mag > contact_threshold

    # Update air time: reset on contact, accumulate otherwise
    new_time = torch.where(in_contact, torch.zeros_like(last_contact_time), last_contact_time + dt)

    # Reward: positive for air times near target, zero otherwise
    # Only reward at the moment of landing (transition from air to contact)
    was_in_air = last_contact_time > 0.0
    just_landed = in_contact & was_in_air

    air_time_reward = torch.where(
        just_landed,
        torch.clamp(last_contact_time - target_air_time, min=0.0),
        torch.zeros_like(last_contact_time),
    )

    # Sum over feet
    total_reward = torch.sum(air_time_reward, dim=-1)

    return total_reward, new_time
