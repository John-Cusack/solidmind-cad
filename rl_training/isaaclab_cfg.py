"""Isaac Lab environment configuration for hexapod locomotion.

Declarative ``ManagerBasedRLEnvCfg`` replacing the manual ``hexapod_env.py``.
Isaac Lab handles scene creation, contact sensors, domain randomization,
and reward composition — eliminating the dead-contact and obs-layout bugs.

Usage::

    from rl_training.isaaclab_cfg import make_hexapod_flat_env_cfg
    cfg = make_hexapod_flat_env_cfg(urdf_path, joint_names, ...)
    env = ManagerBasedRLEnv(cfg)
"""
from __future__ import annotations

import math
from dataclasses import MISSING

import isaaclab.envs.mdp as mdp  # type: ignore[import-not-found]
import isaaclab.sim as sim_utils  # type: ignore[import-not-found]
from isaaclab.actuators import ImplicitActuatorCfg  # type: ignore[import-not-found]
from isaaclab.assets import ArticulationCfg, AssetBaseCfg  # type: ignore[import-not-found]
from isaaclab.sim.converters.urdf_converter_cfg import UrdfConverterCfg  # type: ignore[import-not-found]
from isaaclab.sim.spawners import UrdfFileCfg  # type: ignore[import-not-found]
from isaaclab.envs import ManagerBasedRLEnvCfg  # type: ignore[import-not-found]
from isaaclab.managers import (  # type: ignore[import-not-found]
    CurriculumTermCfg,
    EventTermCfg,
    ObservationGroupCfg,
    ObservationTermCfg,
    RewardTermCfg,
    SceneEntityCfg,
    TerminationTermCfg,
)
from isaaclab.scene import InteractiveSceneCfg  # type: ignore[import-not-found]
from isaaclab.sensors import ContactSensorCfg  # type: ignore[import-not-found]
from isaaclab.sim import PhysxCfg, SimulationCfg  # type: ignore[import-not-found]
from isaaclab.terrains import TerrainImporterCfg  # type: ignore[import-not-found]
from isaaclab.utils import configclass  # type: ignore[import-not-found]
from isaaclab.utils.noise import AdditiveUniformNoiseCfg as Unoise  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------

@configclass
class HexapodSceneCfg(InteractiveSceneCfg):
    """Scene with ground plane, hexapod articulation, and contact sensors."""

    terrain = TerrainImporterCfg(
        prim_path="/World/ground",
        terrain_type="plane",
        collision_group=-1,
        physics_material=sim_utils.RigidBodyMaterialCfg(
            friction_combine_mode="multiply",
            restitution_combine_mode="multiply",
            static_friction=1.0,
            dynamic_friction=1.0,
        ),
    )

    # Placeholder — overridden per-robot by make_hexapod_flat_env_cfg()
    robot: ArticulationCfg = MISSING  # type: ignore[assignment]

    # Contact sensor on foot links — track_air_time fixes dead-contact bug
    contact_forces: ContactSensorCfg = MISSING  # type: ignore[assignment]

    # Lighting
    light = AssetBaseCfg(
        prim_path="/World/light",
        spawn=sim_utils.DistantLightCfg(intensity=3000.0),
    )


# ---------------------------------------------------------------------------
# Observation group
# ---------------------------------------------------------------------------

@configclass
class ObservationsCfg:
    """Observation specifications — wraps the policy group."""

    @configclass
    class PolicyCfg(ObservationGroupCfg):
        """66-dim observation (no gait clock) — Isaac Lab contact sensor replaces it.

        Layout: base_lin_vel(3), base_ang_vel(3), projected_gravity(3),
                commands(3), joint_pos_rel(N), joint_vel(N), last_action(N).
        Total for 18-joint hexapod: 12 + 3*18 = 66.
        """

        base_lin_vel = ObservationTermCfg(func=mdp.base_lin_vel, noise=Unoise(n_min=-0.1, n_max=0.1))
        base_ang_vel = ObservationTermCfg(func=mdp.base_ang_vel, noise=Unoise(n_min=-0.2, n_max=0.2))
        projected_gravity = ObservationTermCfg(func=mdp.projected_gravity, noise=Unoise(n_min=-0.05, n_max=0.05))
        velocity_commands = ObservationTermCfg(func=mdp.generated_commands, params={"command_name": "base_velocity"})
        joint_pos_rel = ObservationTermCfg(func=mdp.joint_pos_rel, noise=Unoise(n_min=-0.01, n_max=0.01))
        joint_vel = ObservationTermCfg(func=mdp.joint_vel_rel, noise=Unoise(n_min=-1.5, n_max=1.5))
        last_action = ObservationTermCfg(func=mdp.last_action)

        def __post_init__(self):
            self.enable_corruption = True
            self.concatenate_terms = True

    policy: PolicyCfg = PolicyCfg()


# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------

@configclass
class RewardsCfg:
    """Additive reward composition — standard for RSL-RL."""

    # Alive bonus — strong survival incentive to escape death spiral
    alive = RewardTermCfg(func=mdp.is_alive, weight=2.0)

    # Tracking — primary task signal, should dominate reward
    track_lin_vel_xy_exp = RewardTermCfg(
        func=mdp.track_lin_vel_xy_exp, weight=3.0,
        params={"command_name": "base_velocity", "std": math.sqrt(0.25)},
    )
    track_ang_vel_z_exp = RewardTermCfg(
        func=mdp.track_ang_vel_z_exp, weight=0.75,
        params={"command_name": "base_velocity", "std": math.sqrt(0.25)},
    )

    # Penalties
    lin_vel_z_l2 = RewardTermCfg(func=mdp.lin_vel_z_l2, weight=-2.0)
    ang_vel_xy_l2 = RewardTermCfg(func=mdp.ang_vel_xy_l2, weight=-0.05)
    flat_orientation_l2 = RewardTermCfg(func=mdp.flat_orientation_l2, weight=-2.0)
    base_height_l2 = RewardTermCfg(
        func=mdp.base_height_l2, weight=-5.0,  # squared error at 0.153m target is 4x larger than at 0.076m
        params={"target_height": 0.14},  # overridden per-robot
    )

    # Joint deviation penalty — reduced to allow exploration needed for gait discovery
    joint_deviation_l1 = RewardTermCfg(
        func=mdp.joint_deviation_l1, weight=-0.1,
        params={"asset_cfg": SceneEntityCfg("robot")},
    )

    # Joint limit penalty — penalize approaching DOF limits
    joint_pos_limits = RewardTermCfg(
        func=mdp.joint_pos_limits, weight=-1.0,  # reduced so it doesn't compete with height
        params={"asset_cfg": SceneEntityCfg("robot")},
    )

    # desired/undesired contacts — body_names filtering is applied
    # per-robot by make_hexapod_flat_env_cfg() below.  Defaults use
    # the broad sensor as a safe fallback.
    desired_contacts = RewardTermCfg(
        func=mdp.desired_contacts, weight=0.25,
        params={"sensor_cfg": SceneEntityCfg("contact_forces"), "threshold": 0.5},
    )
    undesired_contacts = RewardTermCfg(
        func=mdp.undesired_contacts, weight=-1.0,
        params={"sensor_cfg": SceneEntityCfg("contact_forces"), "threshold": 1.0},
    )

    # Regularization
    joint_torques_l2 = RewardTermCfg(func=mdp.joint_torques_l2, weight=-1e-5)
    action_rate_l2 = RewardTermCfg(func=mdp.action_rate_l2, weight=-0.01)  # match ANYmal reference
    joint_acc_l2 = RewardTermCfg(func=mdp.joint_acc_l2, weight=-2.5e-7)  # match ANYmal reference


# ---------------------------------------------------------------------------
# Terminations
# ---------------------------------------------------------------------------

@configclass
class TerminationsCfg:
    """Episode termination conditions."""

    time_out = TerminationTermCfg(func=mdp.time_out, time_out=True)
    # base_contact — body_names filtering is applied per-robot by
    # make_hexapod_flat_env_cfg() below.  Default uses broad sensor.
    base_contact = TerminationTermCfg(
        func=mdp.illegal_contact,
        params={"sensor_cfg": SceneEntityCfg("contact_forces"), "threshold": 1.0},
    )
    bad_orientation = TerminationTermCfg(
        func=mdp.bad_orientation,
        params={"limit_angle": 1.0},  # radians (~57 deg) — hexapods can recover from larger tilts
    )
    # Terminate if body drops too low (crumpled) — overridden per-robot
    low_height = TerminationTermCfg(
        func=mdp.root_height_below_minimum,
        params={"minimum_height": 0.05},  # overridden per-robot
    )


# ---------------------------------------------------------------------------
# Events (domain randomization)
# ---------------------------------------------------------------------------

@configclass
class EventsCfg:
    """Domain randomization events."""

    reset_base = EventTermCfg(
        func=mdp.reset_root_state_uniform,
        mode="reset",
        params={
            "pose_range": {"x": (-0.5, 0.5), "y": (-0.5, 0.5), "yaw": (-3.14, 3.14)},
            "velocity_range": {
                "x": (-0.1, 0.1), "y": (-0.1, 0.1), "z": (-0.1, 0.1),
                "roll": (-0.1, 0.1), "pitch": (-0.1, 0.1), "yaw": (-0.2, 0.2),
            },
        },
    )
    reset_joints = EventTermCfg(
        func=mdp.reset_joints_by_scale,
        mode="reset",
        params={"position_range": (0.8, 1.2), "velocity_range": (0.0, 0.0)},
    )
    push_robot = EventTermCfg(
        func=mdp.push_by_setting_velocity,
        mode="interval",
        interval_range_s=(8.0, 12.0),
        params={"velocity_range": {"x": (-0.5, 0.5), "y": (-0.5, 0.5)}},
    )
    add_mass = EventTermCfg(
        func=mdp.randomize_rigid_body_mass,
        mode="startup",
        params={"asset_cfg": SceneEntityCfg("robot"), "mass_distribution_params": (0.8, 1.2),
                "operation": "scale"},
    )
    randomize_friction = EventTermCfg(
        func=mdp.randomize_rigid_body_material,
        mode="startup",
        params={
            "asset_cfg": SceneEntityCfg("robot"),
            "static_friction_range": (0.7, 1.3),  # friction DR — policy needs robustness for walking
            "dynamic_friction_range": (0.7, 1.3),
            "restitution_range": (0.0, 0.0),
            "num_buckets": 64,
        },
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@configclass
class CommandsCfg:
    """Velocity command sampling."""

    base_velocity = mdp.UniformVelocityCommandCfg(
        asset_name="robot",
        resampling_time_range=(8.0, 12.0),
        rel_standing_envs=0.2,  # 20% standing — policy already knows how to stand
        rel_heading_envs=0.0,
        heading_command=False,
        ranges=mdp.UniformVelocityCommandCfg.Ranges(
            lin_vel_x=(0.0, 0.5),  # moderate walking speed; literature uses 0.5-1.0+
            lin_vel_y=(-0.15, 0.15),  # allow lateral movement for turns
            ang_vel_z=(-0.5, 0.5),  # allow real turns
        ),
    )


# ---------------------------------------------------------------------------
# Top-level env config
# ---------------------------------------------------------------------------

@configclass
class ActionsCfg:
    """Action configuration — joint position targets."""

    joint_pos = mdp.JointPositionActionCfg(
        asset_name="robot",
        joint_names=[".*"],  # overridden per-robot
        scale=0.25,  # overridden per-robot
        use_default_offset=True,
    )


@configclass
class HexapodFlatEnvCfg(ManagerBasedRLEnvCfg):
    """Full env config — scene + obs + rewards + terminations + events."""

    scene: HexapodSceneCfg = HexapodSceneCfg(num_envs=4096, env_spacing=2.5)
    observations: ObservationsCfg = ObservationsCfg()
    actions: ActionsCfg = ActionsCfg()
    rewards: RewardsCfg = RewardsCfg()
    terminations: TerminationsCfg = TerminationsCfg()
    events: EventsCfg = EventsCfg()
    commands: CommandsCfg = CommandsCfg()  # type: ignore[assignment]

    sim: SimulationCfg = SimulationCfg(
        dt=1.0 / 200.0,
        render_interval=4,
        physx=PhysxCfg(
            solver_type=1,  # TGS — accurate for articulations
            max_position_iteration_count=4,  # down from default 255; legged robots don't need high iters
            min_position_iteration_count=1,
            # Pre-allocate GPU buffers to avoid runtime reallocation stalls
            gpu_found_lost_pairs_capacity=2**21,
            gpu_found_lost_aggregate_pairs_capacity=2**25,
            gpu_total_aggregate_pairs_capacity=2**21,
            gpu_heap_capacity=2**26,
            gpu_temp_buffer_capacity=2**24,
            gpu_max_rigid_contact_count=2**23,
            gpu_max_rigid_patch_count=5 * 2**15,
        ),
    )
    decimation = 4  # control at 50 Hz
    episode_length_s = 20.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_hexapod_flat_env_cfg(
    urdf_path: str,
    joint_names: list[str],
    foot_links: list[str],
    base_link: str = "base_link",
    *,
    standing_height_m: float = 0.14,
    default_joint_positions: list[float] | None = None,
    action_scale_per_joint: list[float] | None = None,
    actuator_stiffness: float = 10.0,
    actuator_damping: float = 1.0,
    num_envs: int = 4096,
    env_spacing: float = 2.5,
    max_leg_reach_m: float = 0.0,
) -> HexapodFlatEnvCfg:
    """Build a robot-specific env config from URDF analysis parameters.

    This is called by ``isaaclab_train.py`` after loading the generated
    env config module.
    """
    n_joints = len(joint_names)

    # Build joint name regex for articulation config
    joint_regex = "|".join(joint_names)

    # Default positions dict
    defaults = default_joint_positions or [0.0] * n_joints
    init_positions = {name: defaults[i] for i, name in enumerate(joint_names)}

    # Action scales
    scales = action_scale_per_joint or [0.25] * n_joints

    cfg = HexapodFlatEnvCfg()
    cfg.scene.num_envs = num_envs
    cfg.scene.env_spacing = env_spacing

    # Robot articulation — spawn from URDF via UrdfFileCfg
    cfg.scene.robot = ArticulationCfg(
        prim_path="{ENV_REGEX_NS}/Robot",
        spawn=UrdfFileCfg(
            asset_path=urdf_path,
            fix_base=False,
            make_instanceable=False,
            activate_contact_sensors=True,
            link_density=1000.0,  # fallback density for links missing inertia (e.g. chassis)
            joint_drive=UrdfConverterCfg.JointDriveCfg(
                drive_type="force",
                target_type="position",
                gains=UrdfConverterCfg.JointDriveCfg.PDGainsCfg(
                    stiffness=actuator_stiffness,
                    damping=actuator_damping,
                ),
            ),
        ),
        init_state=ArticulationCfg.InitialStateCfg(
            pos=(0.0, 0.0, standing_height_m + 0.05),  # slight drop
            joint_pos=init_positions,
        ),
        actuators={
            "legs": ImplicitActuatorCfg(
                joint_names_expr=[f"({joint_regex})"],
                stiffness=actuator_stiffness,
                damping=actuator_damping,
            ),
        },
    )

    # Contact sensor — tracks all robot links, air time for gait learning
    cfg.scene.contact_forces = ContactSensorCfg(
        prim_path="{ENV_REGEX_NS}/Robot/.*",
        history_length=3,
        track_air_time=True,
    )

    # Action config — per-joint scales require explicit joint name list
    # (not a regex).  This makes training match deployment exactly.
    cfg.actions.joint_pos.joint_names = list(joint_names)
    # Isaac Lab expects scale as float (uniform) or dict (per-joint), not list
    if len(set(scales)) == 1:
        cfg.actions.joint_pos.scale = scales[0]
    else:
        cfg.actions.joint_pos.scale = {name: scales[i] for i, name in enumerate(joint_names)}

    # Override base_height target and minimum height termination.
    # Minimum height is derived from leg geometry when available:
    # 15% of max leg reach = legs nearly horizontal, definitely collapsed.
    # Falls back to 50% of standing height if leg reach is unknown.
    cfg.rewards.base_height_l2.params["target_height"] = standing_height_m
    if max_leg_reach_m > 0:
        cfg.terminations.low_height.params["minimum_height"] = max_leg_reach_m * 0.08
    else:
        cfg.terminations.low_height.params["minimum_height"] = standing_height_m * 0.5

    # ── Contact sensor scoping ──────────────────────────────────────
    # Scope desired_contacts to foot links only (reward foot ground contact).
    # Scope undesired_contacts + base_contact termination to base link
    # (penalize/terminate on chassis ground contact, NOT foot contact).
    if foot_links:
        cfg.rewards.desired_contacts.params["sensor_cfg"] = SceneEntityCfg(
            "contact_forces", body_names=foot_links,
        )
    else:
        # No foot links identified — disable desired_contacts reward
        # to avoid rewarding belly-flop behaviour.
        cfg.rewards.desired_contacts.weight = 0.0

    cfg.rewards.undesired_contacts.params["sensor_cfg"] = SceneEntityCfg(
        "contact_forces", body_names=[base_link],
    )
    cfg.terminations.base_contact.params["sensor_cfg"] = SceneEntityCfg(
        "contact_forces", body_names=[base_link],
    )

    # Store metadata for training script
    cfg._urdf_path = urdf_path  # type: ignore[attr-defined]
    cfg._joint_names = joint_names  # type: ignore[attr-defined]
    cfg._foot_links = foot_links  # type: ignore[attr-defined]
    cfg._base_link = base_link  # type: ignore[attr-defined]
    cfg._default_joint_positions = defaults  # type: ignore[attr-defined]
    cfg._action_scale_per_joint = scales  # type: ignore[attr-defined]
    cfg._standing_height_m = standing_height_m  # type: ignore[attr-defined]
    cfg._n_joints = n_joints  # type: ignore[attr-defined]

    return cfg
