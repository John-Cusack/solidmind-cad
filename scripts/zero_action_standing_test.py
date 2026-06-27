"""Zero-action standing test for hexapod RL.

Spawns the robot in its default pose, sends zero actions for 5 seconds,
and checks whether it remains standing. Useful for validating actuator
damping before committing to a full training run.

Usage::

    $ISAAC_PYTHON scripts/zero_action_standing_test.py \
        --env-config training_runs/hex18_fresh_env_config.py

    # Override damping without editing config:
    $ISAAC_PYTHON scripts/zero_action_standing_test.py \
        --env-config training_runs/hex18_fresh_env_config.py \
        --damping-override 1.5
"""
from __future__ import annotations

import argparse
import importlib.util
from types import ModuleType


def _load_module(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_cfg", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description="Zero-action standing test")
    parser.add_argument("--env-config", required=True)
    parser.add_argument("--damping-override", type=float, default=None,
                        help="Override actuator damping (test values without editing config)")
    parser.add_argument("--stiffness-override", type=float, default=None,
                        help="Override actuator stiffness")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="Test duration in seconds (default: 5.0)")
    parser.add_argument("--num-envs", type=int, default=4,
                        help="Number of parallel envs (default: 4)")
    parser.add_argument("--threshold", type=float, default=0.8,
                        help="Pass if height stays above this fraction of standing_height (default: 0.8)")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="Run without GUI (default: show Isaac Sim window)")
    args = parser.parse_args()

    # Boot Isaac Sim
    from isaaclab.app import AppLauncher
    launcher = AppLauncher(headless=args.headless)
    simulation_app = launcher.app

    import isaaclab.envs.mdp as mdp
    import torch
    from isaaclab.envs import ManagerBasedRLEnv

    from rl_training.isaaclab_cfg import make_hexapod_flat_env_cfg

    # Check if mdp.is_alive exists (Isaac Lab version compatibility)
    if not hasattr(mdp, 'is_alive'):
        print("WARNING: mdp.is_alive not available in this Isaac Lab version")
        print("Available alive-like functions:", [x for x in dir(mdp) if 'alive' in x.lower()])

    # Load env config module
    mod = _load_module(args.env_config)

    actuator_damping = args.damping_override if args.damping_override is not None else getattr(mod, "ACTUATOR_DAMPING", 1.0)
    actuator_stiffness = args.stiffness_override if args.stiffness_override is not None else getattr(mod, "ACTUATOR_STIFFNESS", 10.0)
    standing_height = getattr(mod, "STANDING_HEIGHT_M", 0.14)

    env_cfg = make_hexapod_flat_env_cfg(
        urdf_path=getattr(mod, "URDF_PATH", ""),
        joint_names=getattr(mod, "JOINT_NAMES", []),
        foot_links=getattr(mod, "FOOT_LINKS", []),
        base_link=getattr(mod, "BASE_LINK", "base_link"),
        standing_height_m=standing_height,
        default_joint_positions=getattr(mod, "DEFAULT_JOINT_POSITIONS", []),
        action_scale_per_joint=getattr(mod, "ACTION_SCALE_PER_JOINT", []),
        actuator_stiffness=actuator_stiffness,
        actuator_damping=actuator_damping,
        num_envs=args.num_envs,
    )

    # Disable domain randomization — we want a clean test
    # Zero out velocity randomization on reset
    env_cfg.events.reset_base.params["velocity_range"] = {
        "x": (0.0, 0.0), "y": (0.0, 0.0), "z": (0.0, 0.0),
        "roll": (0.0, 0.0), "pitch": (0.0, 0.0), "yaw": (0.0, 0.0),
    }
    # Reset joints to exact default positions (scale range 1.0 to 1.0)
    env_cfg.events.reset_joints.params["position_range"] = (1.0, 1.0)
    # Disable interval pushes
    env_cfg.events.push_robot.interval_range_s = (1e6, 1e6)
    # Disable mass/friction randomization
    env_cfg.events.add_mass.params["mass_distribution_params"] = (1.0, 1.0)
    env_cfg.events.randomize_friction.params["static_friction_range"] = (1.0, 1.0)
    env_cfg.events.randomize_friction.params["dynamic_friction_range"] = (1.0, 1.0)

    # Disable all terminations except time_out by setting permissive thresholds
    env_cfg.terminations.base_contact.params["threshold"] = 1e6  # effectively never triggers
    env_cfg.terminations.bad_orientation.params["limit_angle"] = 3.14  # ~180 deg
    env_cfg.terminations.low_height.params["minimum_height"] = -1.0  # below ground

    # Set episode length to test duration
    env_cfg.episode_length_s = args.duration + 1.0  # extra margin

    # Zero pose randomization
    env_cfg.events.reset_base.params["pose_range"] = {
        "x": (0.0, 0.0), "y": (0.0, 0.0), "yaw": (0.0, 0.0),
    }

    # Create environment
    print("Creating environment...", flush=True)
    try:
        env = ManagerBasedRLEnv(cfg=env_cfg)
    except Exception as exc:
        print(f"FATAL: env creation failed: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        simulation_app.close()
        return 1
    n_joints = len(getattr(mod, "JOINT_NAMES", []))

    print(f"Standing test: damping={actuator_damping}, stiffness={actuator_stiffness}", flush=True)
    print(f"  standing_height={standing_height:.3f}m, threshold={args.threshold}, duration={args.duration}s")
    print(f"  num_envs={args.num_envs}, n_joints={n_joints}")

    # Reset and run
    obs_dict, _ = env.reset()
    zero_actions = torch.zeros(args.num_envs, n_joints, device="cuda:0")

    control_dt = getattr(mod, "CONTROL_DT", 0.02)
    total_steps = int(args.duration / control_dt)
    min_height = standing_height * args.threshold
    heights = []

    for step in range(total_steps):
        obs_dict, rewards, terminated, truncated, info = env.step(zero_actions)

        # Extract base height from root state
        root_pos = env.scene["robot"].data.root_pos_w  # (num_envs, 3)
        h = root_pos[:, 2].mean().item()
        heights.append(h)

        if step % 50 == 0:
            h_min = root_pos[:, 2].min().item()
            h_max = root_pos[:, 2].max().item()
            t = step * control_dt
            print(f"  t={t:5.2f}s  h_mean={h:.4f}  h_min={h_min:.4f}  h_max={h_max:.4f}")

    # Analyze results BEFORE closing (close kills the process)
    import statistics
    h_final = heights[-1]
    h_min_overall = min(heights)
    h_mean = statistics.mean(heights)

    print("\nResults:", flush=True)
    print(f"  Height at start:  {heights[0]:.4f} m", flush=True)
    print(f"  Height at end:    {h_final:.4f} m", flush=True)
    print(f"  Minimum height:   {h_min_overall:.4f} m", flush=True)
    print(f"  Mean height:      {h_mean:.4f} m", flush=True)
    print(f"  Threshold:        {min_height:.4f} m ({args.threshold*100:.0f}% of {standing_height:.3f})", flush=True)

    passed = h_min_overall >= min_height
    if passed:
        print(f"\n  PASS — robot held pose for {args.duration}s (min height {h_min_overall:.4f} >= {min_height:.4f})", flush=True)
    else:
        print(f"\n  FAIL — robot collapsed (min height {h_min_overall:.4f} < {min_height:.4f})", flush=True)
        print(f"  Try: --damping-override {actuator_damping * 2:.1f}", flush=True)

    env.close()
    simulation_app.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
