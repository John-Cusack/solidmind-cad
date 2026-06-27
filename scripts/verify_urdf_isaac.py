"""Load a merged URDF into Isaac Lab env and verify visual integrity.

Spawns a single hexapod with zero actions so you can visually confirm
all meshes are connected and the articulation holds together.

Usage::
    $ISAAC_PYTHON scripts/verify_urdf_isaac.py \
        --env-config training_runs/Hexapod18DOF_env_config.py \
        --num-steps 1000
"""
from __future__ import annotations

import argparse
import importlib.util
import time
from types import ModuleType


def _load_module(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_cfg", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify URDF visual integrity in Isaac Sim")
    parser.add_argument("--env-config", default="training_runs/Hexapod18DOF_env_config.py")
    parser.add_argument("--num-steps", type=int, default=1000)
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
        num_envs=1,
    )

    # Create environment
    try:
        env = ManagerBasedRLEnv(cfg=env_cfg)
        print(f"Environment created with URDF: {getattr(mod, 'URDF_PATH', '?')}", flush=True)
    except Exception as exc:
        print(f"FATAL: env creation failed: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        simulation_app.close()
        return 1

    # Reset and run with zero actions
    try:
        obs_dict, _ = env.reset()
    except Exception as exc:
        print(f"FATAL: env.reset() failed: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        env.close()
        simulation_app.close()
        return 1

    obs = obs_dict["policy"]
    num_actions = env.action_space.shape[-1]
    print(f"Obs shape: {obs.shape}, Actions: {num_actions}", flush=True)
    print(f"Running {args.num_steps} steps with ZERO actions (standing test)...", flush=True)

    for step in range(args.num_steps):
        actions = torch.zeros(1, num_actions, device=obs.device)
        obs_dict, rewards, terminated, truncated, info = env.step(actions)
        obs = obs_dict["policy"]

        if step % 200 == 0:
            print(f"  step {step}/{args.num_steps}, reward={rewards.mean().item():.3f}", flush=True)

        time.sleep(0.01)

    print("Verification complete. Robot should be standing intact.", flush=True)
    env.close()
    simulation_app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
