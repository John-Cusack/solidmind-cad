"""Evaluate a trained Isaac Lab policy visually (non-headless).

Loads a trained RSL-RL checkpoint or JIT policy, creates the Isaac Lab
managed environment, and runs episodes with rendering so you can watch it.

Usage::

    $ISAAC_PYTHON scripts/eval_policy_isaac.py \
        --env-config training_runs/hex18_v3_env_config.py \
        --checkpoint training_runs/.../model_700.pt \
        --num-steps 2000

    # Or with a JIT-traced policy:
    $ISAAC_PYTHON scripts/eval_policy_isaac.py \
        --env-config training_runs/hex18_v3_env_config.py \
        --policy training_runs/.../deployed/policy.pt \
        --num-steps 2000
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
    parser = argparse.ArgumentParser(description="Evaluate policy in Isaac Sim with GUI")
    parser.add_argument("--env-config", required=True)
    parser.add_argument("--policy", default=None, help="JIT-traced policy.pt")
    parser.add_argument("--checkpoint", default=None, help="RSL-RL model_*.pt checkpoint")
    parser.add_argument("--num-steps", type=int, default=2000)
    parser.add_argument("--num-envs", type=int, default=1)
    parser.add_argument("--forward-vel", type=float, default=None,
                        help="Override velocity command with constant forward speed (m/s)")
    parser.add_argument("--no-reset", action="store_true",
                        help="Disable episode resets (run continuously)")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="Run without GUI (for capturing stdout proof of policy execution)")
    args = parser.parse_args()

    if not args.policy and not args.checkpoint:
        parser.error("Provide either --policy (JIT) or --checkpoint (RSL-RL)")

    # Boot Isaac Sim
    from isaaclab.app import AppLauncher
    launcher = AppLauncher(headless=args.headless)
    simulation_app = launcher.app

    import torch
    from isaaclab.envs import ManagerBasedRLEnv
    from isaaclab_rl.rsl_rl import RslRlVecEnvWrapper
    from rsl_rl.runners import OnPolicyRunner

    from rl_training.isaaclab_cfg import make_hexapod_flat_env_cfg
    from rl_training.rsl_rl_cfg import HexapodPPORunnerCfg

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

    # For eval: disable terminations so the robot runs continuously
    if args.no_reset:
        env_cfg.terminations.base_contact = None  # type: ignore[assignment]
        env_cfg.terminations.bad_orientation = None  # type: ignore[assignment]
        env_cfg.terminations.low_height = None  # type: ignore[assignment]
        env_cfg.episode_length_s = args.num_steps * env_cfg.sim.dt * env_cfg.decimation * 2  # long enough

    # For eval: force constant forward velocity instead of random sampling
    if args.forward_vel is not None:
        import isaaclab.envs.mdp as mdp_mod  # type: ignore[import-not-found]
        env_cfg.commands.base_velocity = mdp_mod.UniformVelocityCommandCfg(
            asset_name="robot",
            resampling_time_range=(1000.0, 1000.0),  # never resample
            rel_standing_envs=0.0,
            rel_heading_envs=0.0,
            heading_command=False,
            ranges=mdp_mod.UniformVelocityCommandCfg.Ranges(
                lin_vel_x=(args.forward_vel, args.forward_vel),
                lin_vel_y=(0.0, 0.0),
                ang_vel_z=(0.0, 0.0),
            ),
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

    # ── Point camera at the robot ──────────────────────────────────
    try:
        from isaaclab.envs.ui import ViewportCameraController  # type: ignore[import-not-found]
        _cam = ViewportCameraController(env, cfg=ViewportCameraController.cfg)
        _cam.cfg.eye = (1.5, 1.5, 1.0)  # type: ignore[attr-defined]
        _cam.cfg.lookat = (0.0, 0.0, 0.15)  # type: ignore[attr-defined]
        _cam.update_view_location(_cam.cfg.eye, _cam.cfg.lookat)
        print("Camera positioned at (1.5, 1.5, 1.0) looking at origin", flush=True)
    except Exception as cam_exc:
        # Fallback: set camera via USD stage
        try:
            import omni.kit.viewport.utility as vp_util  # type: ignore[import-not-found]
            viewport = vp_util.get_active_viewport()
            if viewport is not None:
                from pxr import Gf  # type: ignore[import-not-found]
                import omni.kit.commands  # type: ignore[import-not-found]
                # Create a camera prim and set it as active
                from omni.kit.viewport.utility.camera_state import ViewportCameraState  # type: ignore[import-not-found]
                cam_state = ViewportCameraState(viewport)
                cam_state.set_position_world(Gf.Vec3d(1.5, 1.5, 1.0), True)
                cam_state.set_target_world(Gf.Vec3d(0.0, 0.0, 0.15), True)
                print("Camera positioned via ViewportCameraState", flush=True)
        except Exception as fallback_exc:
            print(f"Could not set camera position: {cam_exc} / {fallback_exc}", flush=True)

    if args.checkpoint:
        # Load via RSL-RL runner (handles TensorDict obs init)
        env_wrapped = RslRlVecEnvWrapper(env)
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        agent_cfg = HexapodPPORunnerCfg()
        agent_cfg.device = device
        runner = OnPolicyRunner(env_wrapped, agent_cfg.to_dict(), log_dir=None, device=device)
        runner.load(args.checkpoint)
        policy_obj = runner.alg.policy
        policy_obj.eval()
        print(f"Checkpoint loaded from {args.checkpoint}", flush=True)

        # Extract actor network for direct inference (bypasses TensorDict)
        actor_net = policy_obj.actor
        normalizer = policy_obj.actor_obs_normalizer

        obs_dict, _ = env.reset()
        obs = obs_dict["policy"]
        # Capture initial body root position so we can measure
        # forward-distance progress over the rollout.
        try:
            robot = env.scene["robot"]
            initial_pos = robot.data.root_pos_w.clone()
        except Exception:
            robot = None
            initial_pos = None
        print(f"Obs shape: {obs.shape}, running {args.num_steps} steps...", flush=True)

        for step in range(args.num_steps):
            with torch.no_grad():
                actions = actor_net(normalizer(obs))
            obs_dict, rewards, terminated, truncated, info = env.step(actions)
            obs = obs_dict["policy"]

            if step % 100 == 0:
                msg = f"  step {step}/{args.num_steps}, reward={rewards.mean().item():.3f}"
                if robot is not None:
                    pos = robot.data.root_pos_w
                    if initial_pos is not None:
                        dx = (pos[:, 0] - initial_pos[:, 0]).mean().item()
                        dy = (pos[:, 1] - initial_pos[:, 1]).mean().item()
                        msg += f", dx={dx:+.3f}m, dy={dy:+.3f}m"
                    h = pos[:, 2].mean().item()
                    msg += f", height={h:.3f}m"
                print(msg, flush=True)

            time.sleep(0.005)

        # Final summary so the rollout proof persists in the log.
        if robot is not None and initial_pos is not None:
            pos = robot.data.root_pos_w
            dx = (pos[:, 0] - initial_pos[:, 0]).cpu().numpy()
            dy = (pos[:, 1] - initial_pos[:, 1]).cpu().numpy()
            h = pos[:, 2].cpu().numpy()
            print(f"\nFinal rollout summary ({args.num_envs} envs, {args.num_steps} steps):", flush=True)
            for i in range(len(dx)):
                print(f"  env {i}: forward dx={dx[i]:+.3f}m, lateral dy={dy[i]:+.3f}m, "
                      f"final height={h[i]:.3f}m", flush=True)
            print(f"  mean: dx={dx.mean():+.3f}m, dy={dy.mean():+.3f}m, h={h.mean():.3f}m",
                  flush=True)
    else:
        # JIT policy path
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        policy_jit = torch.jit.load(args.policy, map_location=device)
        policy_jit.eval()
        print(f"JIT policy loaded from {args.policy}", flush=True)

        obs_dict, _ = env.reset()
        obs = obs_dict["policy"]
        print(f"Obs shape: {obs.shape}, running {args.num_steps} steps...", flush=True)

        for step in range(args.num_steps):
            with torch.no_grad():
                actions = policy_jit(obs)
            obs_dict, rewards, terminated, truncated, info = env.step(actions)
            obs = obs_dict["policy"]

            if step % 200 == 0:
                print(f"  step {step}/{args.num_steps}, reward={rewards.mean().item():.3f}", flush=True)

            time.sleep(0.005)

    print("Evaluation complete.")
    env.close()
    simulation_app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
