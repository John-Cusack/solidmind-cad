"""Evaluate a trained Isaac Lab policy visually (non-headless).

Loads the trained JIT policy, creates the Isaac Lab managed environment,
and runs episodes with rendering so you can watch it walk.

Usage::

    $ISAAC_PYTHON scripts/eval_policy_isaac.py \
        --env-config training_runs/hex18_fresh_env_config.py \
        --policy training_runs/hex18_walk_1000/deployed/policy.pt \
        --num-steps 2000
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType


def _load_module(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_cfg", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate policy in Isaac Sim with GUI")
    parser.add_argument("--env-config", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--num-steps", type=int, default=2000)
    parser.add_argument("--num-envs", type=int, default=1)
    args = parser.parse_args()

    # Boot Isaac Sim with GUI
    from isaaclab.app import AppLauncher
    launcher = AppLauncher(headless=False)
    simulation_app = launcher.app

    import torch
    from isaaclab.envs import ManagerBasedRLEnv

    from rl_training.isaaclab_cfg import make_hexapod_flat_env_cfg

    # Load env config
    mod = _load_module(args.env_config)
    env_cfg = make_hexapod_flat_env_cfg(
        urdf_path=getattr(mod, "URDF_PATH", ""),
        joint_names=getattr(mod, "JOINT_NAMES", []),
        foot_links=getattr(mod, "FOOT_LINKS", []),
        base_link=getattr(mod, "BASE_LINK", "base_link"),
        standing_height_m=getattr(mod, "STANDING_HEIGHT_M", 0.14),
        default_joint_positions=getattr(mod, "DEFAULT_JOINT_POSITIONS", []),
        action_scale_per_joint=getattr(mod, "ACTION_SCALE_PER_JOINT", []),
        actuator_stiffness=getattr(mod, "ACTUATOR_STIFFNESS", 10.0),
        actuator_damping=getattr(mod, "ACTUATOR_DAMPING", 1.0),
        num_envs=args.num_envs,
    )

    # Create environment
    try:
        env = ManagerBasedRLEnv(cfg=env_cfg)
        print(f"Environment created: {args.num_envs} envs", flush=True)
    except Exception as exc:
        print(f"FATAL: env creation failed: {exc}", flush=True)
        import traceback; traceback.print_exc()
        simulation_app.close()
        return 1

    # Load policy
    policy = torch.jit.load(args.policy, map_location="cuda:0" if torch.cuda.is_available() else "cpu")
    policy.eval()
    print(f"Policy loaded from {args.policy}", flush=True)

    # Run evaluation
    try:
        obs_dict, _ = env.reset()
    except Exception as exc:
        print(f"FATAL: env.reset() failed: {exc}", flush=True)
        import traceback; traceback.print_exc()
        env.close()
        simulation_app.close()
        return 1
    obs = obs_dict["policy"]
    print(f"Obs shape: {obs.shape}, running {args.num_steps} steps...", flush=True)

    for step in range(args.num_steps):
        with torch.no_grad():
            actions = policy(obs)
        obs_dict, rewards, terminated, truncated, info = env.step(actions)
        obs = obs_dict["policy"]

        if step % 200 == 0:
            print(f"  step {step}/{args.num_steps}, reward={rewards.mean().item():.3f}", flush=True)

        # Small sleep so GUI renders smoothly
        time.sleep(0.005)

    print("Evaluation complete.")
    env.close()
    simulation_app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
