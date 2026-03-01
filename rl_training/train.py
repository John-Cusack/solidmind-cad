"""RL training entry point.

Runs RSL-RL PPO training with residual locomotion environment.
Designed to be spawned as a subprocess in Isaac Lab's Python:

    ISAAC_PYTHON=./isaacsim/_build/.../python.sh
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
    actor_hidden_dims: tuple[int, ...] = (512, 256, 128)
    critic_hidden_dims: tuple[int, ...] = (512, 256, 128)
    activation: str = "elu"
    # Training schedule
    max_iterations: int = 1500
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
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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
        headless = True  # Training always headless for throughput
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

    # ── Training loop ─────────────────────────────────────────────
    best_reward = float("-inf")
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

            # Logging
            if iteration % ppo_config.log_interval == 0 or iteration == 1:
                log.info(
                    "Iter %d/%d | reward=%.3f | policy_loss=%.4f | value_loss=%.4f | "
                    "entropy=%.4f | kl=%.5f | lr=%.2e | %.1fs",
                    iteration, ppo_config.max_iterations,
                    mean_reward,
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
                trainer.save_checkpoint(str(checkpoints_dir / "model_best.pt"))

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
            "alpha": 1.0,  # Direct mode, no residual blending
            "obs_dim": hex_cfg.obs_dim,
            "action_dim": hex_cfg.action_dim,
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
