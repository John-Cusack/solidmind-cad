"""RL training entry point.

Runs RSL-RL PPO training with residual locomotion environment.
Designed to be spawned as a subprocess in Isaac Lab's Python:

    ISAAC_PYTHON=../isaacsim/_build/.../python.sh
    $ISAAC_PYTHON -m rl_training.train \\
        --env-config /path/to/env_config.py \\
        --output-dir training_runs/<run_id>/

Hyperparameters are research-validated defaults from the SolidMind
RL pipeline design document.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("solidmind.rl_train")


@dataclass(frozen=True, slots=True)
class PPOConfig:
    """RSL-RL PPO hyperparameters.

    Research-validated defaults for locomotion training.
    """

    learning_rate: float = 1e-3
    num_epochs: int = 5
    num_mini_batches: int = 4
    gamma: float = 0.99
    lam: float = 0.95
    entropy_coef: float = 0.01
    kl_target: float = 0.01
    clip_param: float = 0.2
    max_grad_norm: float = 1.0
    # Network architecture
    actor_hidden_dims: tuple[int, ...] = (256, 128, 64)
    critic_hidden_dims: tuple[int, ...] = (512, 256, 128)
    activation: str = "elu"
    # Training schedule
    max_iterations: int = 3000
    num_steps_per_env: int = 24
    # Checkpointing
    save_interval: int = 100
    log_interval: int = 10


def write_training_config(
    output_dir: Path,
    ppo_config: PPOConfig,
    env_config_path: str,
) -> Path:
    """Write training configuration to JSON for reproducibility."""
    config_path = output_dir / "training_config.json"
    config_data: dict[str, Any] = {
        "ppo": {
            "learning_rate": ppo_config.learning_rate,
            "num_epochs": ppo_config.num_epochs,
            "num_mini_batches": ppo_config.num_mini_batches,
            "gamma": ppo_config.gamma,
            "lam": ppo_config.lam,
            "entropy_coef": ppo_config.entropy_coef,
            "kl_target": ppo_config.kl_target,
            "clip_param": ppo_config.clip_param,
            "max_grad_norm": ppo_config.max_grad_norm,
            "actor_hidden_dims": list(ppo_config.actor_hidden_dims),
            "critic_hidden_dims": list(ppo_config.critic_hidden_dims),
            "activation": ppo_config.activation,
            "max_iterations": ppo_config.max_iterations,
            "num_steps_per_env": ppo_config.num_steps_per_env,
            "save_interval": ppo_config.save_interval,
            "log_interval": ppo_config.log_interval,
        },
        "env_config_path": env_config_path,
        "started_at": time.time(),
    }
    config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
    return config_path


def write_progress(
    output_dir: Path,
    iteration: int,
    max_iterations: int,
    mean_reward: float,
    **extra: Any,
) -> None:
    """Write training progress to a JSON file for monitoring."""
    progress_path = output_dir / "progress.json"
    data: dict[str, Any] = {
        "iteration": iteration,
        "max_iterations": max_iterations,
        "mean_reward": mean_reward,
        "updated_at": time.time(),
    }
    data.update(extra)
    progress_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for training subprocess."""
    parser = argparse.ArgumentParser(description="RL training for SolidMind CAD")
    parser.add_argument(
        "--env-config", required=True,
        help="Path to generated env config .py file",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Output directory for checkpoints and logs",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=None,
        help="Override max training iterations",
    )
    parser.add_argument(
        "--num-envs", type=int, default=None,
        help="Override number of parallel environments",
    )
    parser.add_argument(
        "--early-stop-fall-pct", type=float, default=95.0,
        help="Stop if fall%% exceeds this after min iterations (default: 95)",
    )
    parser.add_argument(
        "--early-stop-patience", type=int, default=200,
        help="Stop if no reward improvement for this many iterations (default: 200)",
    )
    parser.add_argument(
        "--no-early-stop", action="store_true",
        help="Disable early stopping",
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Run with Isaac Sim GUI (visible rendering)",
    )
    parser.add_argument(
        "--patience", type=int, default=500,
        help="Stop if reward doesn't improve by >1.0 over this many iterations (Isaac Lab pipeline)",
    )
    parser.add_argument(
        "--resume", type=str, default=None,
        help="Path to checkpoint .pt to resume from (Isaac Lab pipeline)",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Pipeline dispatch ─────────────────────────────────────────
    # If the env config declares PIPELINE = "isaaclab", route to the
    # Isaac Lab + RSL-RL training entry point.  Otherwise fall through
    # to the legacy custom pipeline for backward compatibility.
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_env_cfg_check", args.env_config)
        if _spec is not None and _spec.loader is not None:
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _pipeline = getattr(_mod, "PIPELINE", "custom")
            if _pipeline == "isaaclab":
                from rl_training.isaaclab_train import run_isaaclab_training
                log.info("Dispatching to Isaac Lab pipeline")
                return run_isaaclab_training(args, _mod)
    except Exception as exc:
        log.warning("Pipeline detection failed (%s), falling back to custom", exc)

    ppo_config = PPOConfig(
        max_iterations=args.max_iterations or PPOConfig.max_iterations,
    )

    # Write config for reproducibility
    write_training_config(output_dir, ppo_config, args.env_config)

    log.info("Training config written to %s", output_dir / "training_config.json")
    log.info("Environment config: %s", args.env_config)
    log.info("Max iterations: %d", ppo_config.max_iterations)

    # The actual Isaac Lab training loop requires Isaac Lab Python.
    # This entry point validates config and sets up the output directory.
    # When run in Isaac Lab Python, it would:
    # 1. Load env config via build_env_config_from_file()
    # 2. Create ResidualLocomotionEnv
    # 3. Create RSL-RL PPO runner
    # 4. Train for max_iterations
    # 5. Save checkpoints and progress

    try:
        from rl_training.residual_env import build_env_config_from_file
        env_cfg = build_env_config_from_file(args.env_config)
        log.info(
            "Env config loaded: %d joints, obs_dim=%d, action_dim=%d",
            env_cfg.num_joints, env_cfg.obs_dim, env_cfg.action_dim,
        )
    except Exception as exc:
        log.error("Failed to load env config: %s", exc)
        return 1

    # Write initial progress
    write_progress(
        output_dir,
        iteration=0,
        max_iterations=ppo_config.max_iterations,
        mean_reward=0.0,
        status="initialized",
    )

    # Attempt to import PyTorch
    try:
        import torch  # type: ignore[import-not-found]
        log.info("PyTorch available: %s (CUDA: %s)", torch.__version__, torch.cuda.is_available())
    except ImportError:
        log.warning("PyTorch not available — writing config only (no training)")
        write_progress(
            output_dir,
            iteration=0,
            max_iterations=ppo_config.max_iterations,
            mean_reward=0.0,
            status="config_only",
            error="PyTorch not available in this Python environment",
        )
        return 0

    # ── Bootstrap Isaac Sim (SimulationApp) ────────────────────────
    # Isaac Sim requires the Kit application to be running before any
    # omni.isaac imports work.  SimulationApp must be created ONCE,
    # before importing omni.isaac.core or any Omniverse extension.
    simulation_app = None
    try:
        from isaacsim import SimulationApp  # type: ignore[import-not-found]
        headless = not args.no_headless
        simulation_app = SimulationApp({"headless": headless})
        log.info("Isaac Sim SimulationApp initialized (headless=%s)", headless)
    except ImportError:
        log.warning(
            "isaacsim.SimulationApp not available — "
            "training requires Isaac Sim Python ($ISAAC_PYTHON)"
        )
        write_progress(
            output_dir,
            iteration=0,
            max_iterations=ppo_config.max_iterations,
            mean_reward=0.0,
            status="ready",
            note="SimulationApp not available. Run with $ISAAC_PYTHON.",
        )
        return 0
    except Exception as exc:
        log.error("Failed to initialize SimulationApp: %s", exc)
        write_progress(
            output_dir, iteration=0,
            max_iterations=ppo_config.max_iterations,
            mean_reward=0.0, status="error",
            error=f"SimulationApp init failed: {exc}",
        )
        return 1

    # Now that SimulationApp is running, import training modules
    try:
        from rl_training.hexapod_env import HexapodEnvConfig, HexapodLocomotionEnv
        from rl_training.ppo import PPOHyperparams, PPOTrainer
    except ImportError as exc:
        log.error("Training modules not available: %s", exc)
        write_progress(
            output_dir,
            iteration=0,
            max_iterations=ppo_config.max_iterations,
            mean_reward=0.0,
            status="error",
            error=f"Import failed: {exc}",
        )
        if simulation_app:
            simulation_app.close()
        return 1

    # Build environment config from generated module
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("_env_cfg_mod", args.env_config)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {args.env_config}")
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        hex_cfg = HexapodEnvConfig.from_env_config_module(mod)
        if args.num_envs is not None:
            hex_cfg.num_envs = args.num_envs
    except Exception as exc:
        log.error("Failed to build HexapodEnvConfig: %s", exc)
        write_progress(
            output_dir, iteration=0,
            max_iterations=ppo_config.max_iterations,
            mean_reward=0.0, status="error",
            error=f"Env config build failed: {exc}",
        )
        if simulation_app:
            simulation_app.close()
        return 1

    # Create and initialize environment
    try:
        env = HexapodLocomotionEnv(hex_cfg)
        env.render = not headless
        env.initialize()
    except Exception as exc:
        log.error("Failed to initialize environment: %s", exc)
        write_progress(
            output_dir, iteration=0,
            max_iterations=ppo_config.max_iterations,
            mean_reward=0.0, status="error",
            error=f"Env init failed: {exc}",
        )
        if simulation_app:
            simulation_app.close()
        return 1

    # Create PPO trainer
    ppo_params = PPOHyperparams(
        learning_rate=ppo_config.learning_rate,
        num_epochs=ppo_config.num_epochs,
        num_mini_batches=ppo_config.num_mini_batches,
        gamma=ppo_config.gamma,
        lam=ppo_config.lam,
        entropy_coef=ppo_config.entropy_coef,
        clip_param=ppo_config.clip_param,
        max_grad_norm=ppo_config.max_grad_norm,
        actor_hidden_dims=ppo_config.actor_hidden_dims,
        critic_hidden_dims=ppo_config.critic_hidden_dims,
        activation=ppo_config.activation,
        num_steps_per_env=ppo_config.num_steps_per_env,
        desired_kl=ppo_config.kl_target,
    )

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    trainer = PPOTrainer(env, params=ppo_params, device=device)

    log.info(
        "Training started: %d envs, %d joints, obs_dim=%d, action_dim=%d, device=%s",
        hex_cfg.num_envs, hex_cfg.num_joints, hex_cfg.obs_dim, hex_cfg.action_dim, device,
    )

    write_progress(
        output_dir, iteration=0,
        max_iterations=ppo_config.max_iterations,
        mean_reward=0.0, status="training",
    )

    # ── Early stopping config ────────────────────────────────────
    early_stop_enabled = not args.no_early_stop
    early_stop_fall_pct = args.early_stop_fall_pct
    early_stop_patience = args.early_stop_patience
    early_stop_min_iterations = 100

    # ── Training loop ─────────────────────────────────────────────
    best_reward = float("-inf")
    best_reward_iter = 0
    checkpoints_dir = output_dir / "checkpoints"
    checkpoints_dir.mkdir(exist_ok=True)

    try:
        for iteration in range(1, ppo_config.max_iterations + 1):
            t0 = time.time()

            # Collect rollouts
            rollout_stats = trainer.collect_rollouts()
            mean_reward = rollout_stats["mean_reward"]

            # PPO update
            update_stats = trainer.update()

            elapsed = time.time() - t0

            # Episode stats from environment
            ep_stats = env.get_episode_stats()
            fall_pct = ep_stats["fall_pct"]
            mean_ep_len = ep_stats["mean_episode_length"]

            # Adaptive logging: every iteration for first 50, then every log_interval
            should_log = (iteration <= 50) or (iteration % ppo_config.log_interval == 0)
            if should_log:
                log.info(
                    "Iter %d/%d | reward=%.3f | fall=%.0f%% | ep_len=%.0f | "
                    "policy_loss=%.4f | value_loss=%.4f | "
                    "entropy=%.4f | kl=%.5f | lr=%.2e | %.1fs",
                    iteration, ppo_config.max_iterations,
                    mean_reward,
                    fall_pct,
                    mean_ep_len,
                    update_stats["policy_loss"],
                    update_stats["value_loss"],
                    update_stats["entropy"],
                    update_stats["kl_divergence"],
                    update_stats["learning_rate"],
                    elapsed,
                )

            # Write progress
            write_progress(
                output_dir,
                iteration=iteration,
                max_iterations=ppo_config.max_iterations,
                mean_reward=mean_reward,
                status="training",
                fall_pct=fall_pct,
                mean_episode_length=mean_ep_len,
                policy_loss=update_stats["policy_loss"],
                value_loss=update_stats["value_loss"],
                entropy=update_stats["entropy"],
                kl_divergence=update_stats["kl_divergence"],
                learning_rate=update_stats["learning_rate"],
                elapsed_s=elapsed,
            )

            # Save checkpoint
            if iteration % ppo_config.save_interval == 0:
                ckpt_path = checkpoints_dir / f"model_{iteration}.pt"
                trainer.save_checkpoint(str(ckpt_path))

            # Track best model
            if mean_reward > best_reward:
                best_reward = mean_reward
                best_reward_iter = iteration
                trainer.save_checkpoint(str(checkpoints_dir / "model_best.pt"))

            # Early stopping checks (only after min iterations)
            if early_stop_enabled and iteration >= early_stop_min_iterations:
                # Stop if fall rate is too high
                if fall_pct >= early_stop_fall_pct and (ep_stats["fall_count"] + ep_stats["timeout_count"]) > 0:
                    log.warning(
                        "Early stop: fall rate %.0f%% >= %.0f%% at iteration %d",
                        fall_pct, early_stop_fall_pct, iteration,
                    )
                    break
                # Stop if no reward improvement for patience iterations
                if iteration - best_reward_iter >= early_stop_patience:
                    log.warning(
                        "Early stop: no reward improvement for %d iterations (best at iter %d)",
                        early_stop_patience, best_reward_iter,
                    )
                    break

    except KeyboardInterrupt:
        log.info("Training interrupted at iteration %d", trainer.current_iteration)
    except Exception as exc:
        log.error("Training failed at iteration %d: %s", trainer.current_iteration, exc)
        write_progress(
            output_dir,
            iteration=trainer.current_iteration,
            max_iterations=ppo_config.max_iterations,
            mean_reward=best_reward if best_reward > float("-inf") else 0.0,
            status="error",
            error=str(exc),
        )
        env.close()
        if simulation_app:
            simulation_app.close()
        return 1

    # Export final policy and save checkpoint BEFORE closing SimulationApp
    # (SimulationApp.close() may terminate the process)
    deployed_dir = output_dir / "deployed"
    try:
        policy_path = trainer.export_policy(str(deployed_dir))
        deploy_config = {
            "joint_names": hex_cfg.joint_names,
            "action_scale": hex_cfg.action_scale,
            "default_joint_positions": hex_cfg.default_joint_positions,
            "alpha": 1.0,  # Direct mode, no residual blending
            "obs_dim": hex_cfg.obs_dim,
            "action_dim": hex_cfg.action_dim,
            "normalized_policy": True,  # Policy has built-in normalization
            "stride_frequency": hex_cfg.stride_frequency,
        }
        (deployed_dir / "deployment_config.json").write_text(
            json.dumps(deploy_config, indent=2), encoding="utf-8",
        )
        log.info("Final policy exported to %s", policy_path)
    except Exception as exc:
        log.warning("Failed to export final policy: %s", exc)

    trainer.save_checkpoint(str(checkpoints_dir / "model_final.pt"))

    write_progress(
        output_dir,
        iteration=ppo_config.max_iterations,
        max_iterations=ppo_config.max_iterations,
        mean_reward=best_reward if best_reward > float("-inf") else 0.0,
        status="completed",
    )

    log.info("Training complete. Best reward: %.3f", best_reward)

    # Cleanup
    env.close()
    if simulation_app:
        simulation_app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
