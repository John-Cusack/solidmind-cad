"""Isaac Lab + RSL-RL training entry point.

Replaces the custom env + custom PPO pipeline with battle-tested
Isaac Lab (declarative env configs, built-in contact sensors) +
RSL-RL v5 (OnPolicyRunner with adaptive LR and KL targeting).

CLI interface is identical to ``train.py`` so the MCP tool
``rl.start_training`` spawns the same subprocess command — the
generated env config's ``PIPELINE`` constant routes here.

Usage::

    $ISAAC_PYTHON -m rl_training.isaaclab_train \\
        --env-config /path/to/env_config.py \\
        --output-dir training_runs/<run_id>/
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import logging
import math
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("solidmind.isaaclab_train")


class EarlyStopException(Exception):
    """Raised when early-stopping criteria are met."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _load_env_config_module(path: str) -> ModuleType:
    """Import the generated env config .py as a module."""
    spec = importlib.util.spec_from_file_location("_env_cfg_mod", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load env config from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_progress(
    output_dir: Path,
    iteration: int,
    max_iterations: int,
    mean_reward: float,
    **extra: Any,
) -> None:
    """Write training progress to JSON (same format as custom pipeline)."""
    data: dict[str, Any] = {
        "iteration": iteration,
        "max_iterations": max_iterations,
        "mean_reward": mean_reward,
        "updated_at": time.time(),
    }
    data.update(extra)
    (output_dir / "progress.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8",
    )


def _export_deployment_config(
    output_dir: Path,
    mod: ModuleType,
) -> Path:
    """Write deployment_config.json for the policy controller."""
    deployed_dir = output_dir / "deployed"
    deployed_dir.mkdir(parents=True, exist_ok=True)

    joint_names = getattr(mod, "JOINT_NAMES", [])
    action_scales = getattr(mod, "ACTION_SCALE_PER_JOINT", [0.25] * len(joint_names))
    defaults = getattr(mod, "DEFAULT_JOINT_POSITIONS", [0.0] * len(joint_names))
    n_joints = len(joint_names)

    config = {
        "joint_names": joint_names,
        "action_scale_per_joint": action_scales,
        "action_scale_mode": "per_joint",
        "default_joint_positions": defaults,
        "obs_dim": 12 + 3 * n_joints,
        "action_dim": n_joints,
        "obs_layout": "isaaclab",
        "normalized_policy": True,
        "stride_frequency": 2.0,
        "alpha": 1.0,
    }

    config_path = deployed_dir / "deployment_config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return deployed_dir


def run_isaaclab_training(args: argparse.Namespace, mod: ModuleType) -> int:
    """Run Isaac Lab + RSL-RL training.

    Called from ``train.py`` when the env config has ``PIPELINE = "isaaclab"``.

    Args:
        args: Parsed CLI arguments (--output-dir, --max-iterations, etc.).
        mod: The loaded env config module with URDF_PATH, JOINT_NAMES, etc.
    """
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Extract config from module
    urdf_path = getattr(mod, "URDF_PATH", "")
    joint_names: list[str] = getattr(mod, "JOINT_NAMES", [])
    foot_links: list[str] = getattr(mod, "FOOT_LINKS", [])
    base_link: str = getattr(mod, "BASE_LINK", "base_link")
    standing_height: float = getattr(mod, "STANDING_HEIGHT_M", 0.14)
    max_leg_reach: float = getattr(mod, "MAX_LEG_REACH_M", 0.0)
    default_positions: list[float] = getattr(mod, "DEFAULT_JOINT_POSITIONS", [])
    action_scales: list[float] = getattr(mod, "ACTION_SCALE_PER_JOINT", [])
    stiffness: float = getattr(mod, "ACTUATOR_STIFFNESS", 10.0)
    damping: float = getattr(mod, "ACTUATOR_DAMPING", 1.0)
    num_envs: int = args.num_envs or getattr(mod, "NUM_ENVS", 4096)
    max_iterations: int = args.max_iterations or 3000
    n_joints = len(joint_names)

    log.info(
        "Isaac Lab training: %s, %d joints, %d envs, %d iters",
        urdf_path, n_joints, num_envs, max_iterations,
    )

    _write_progress(output_dir, 0, max_iterations, 0.0, status="initializing")

    # ── Bootstrap Isaac Sim via AppLauncher ─────────────────────────
    try:
        from isaaclab.app import AppLauncher  # type: ignore[import-not-found]
    except ImportError:
        log.error(
            "Isaac Lab not available. Install with:\n"
            "  $ISAACSIM_PYTHON -m pip install -e source/isaaclab"
        )
        _write_progress(output_dir, 0, max_iterations, 0.0,
                        status="error", error="Isaac Lab not installed")
        return 1

    headless = not getattr(args, "no_headless", False)
    launcher = AppLauncher(headless=headless)
    simulation_app = launcher.app
    log.info("Isaac Sim AppLauncher initialized (headless=%s)", headless)

    # ── Imports that require running Kit app ────────────────────────
    import torch  # type: ignore[import-not-found]

    # Enable TF32 for ~2-3x faster matmul on Ampere+ GPUs (RTX 3090/4090)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True

    from isaaclab.envs import ManagerBasedRLEnv  # type: ignore[import-not-found]
    from rsl_rl.runners import OnPolicyRunner  # type: ignore[import-not-found]

    from isaaclab_rl.rsl_rl import RslRlVecEnvWrapper  # type: ignore[import-not-found]

    from rl_training.isaaclab_cfg import make_hexapod_flat_env_cfg
    from rl_training.rsl_rl_cfg import HexapodPPORunnerCfg

    # ── Build env config ───────────────────────────────────────────
    env_cfg = make_hexapod_flat_env_cfg(
        urdf_path=urdf_path,
        joint_names=joint_names,
        foot_links=foot_links,
        base_link=base_link,
        standing_height_m=standing_height,
        default_joint_positions=default_positions,
        action_scale_per_joint=action_scales,
        actuator_stiffness=stiffness,
        actuator_damping=damping,
        num_envs=num_envs,
        max_leg_reach_m=max_leg_reach,
    )

    # ── Create environment ─────────────────────────────────────────
    try:
        env = ManagerBasedRLEnv(cfg=env_cfg)
        log.info("Environment created: %d envs", num_envs)
    except Exception as exc:
        log.error("Failed to create environment: %s", exc)
        _write_progress(output_dir, 0, max_iterations, 0.0,
                        status="error", error=f"Env creation failed: {exc}")
        simulation_app.close()
        return 1

    # Wrap for RSL-RL
    env_wrapped = RslRlVecEnvWrapper(env)

    # ── Create RSL-RL runner ───────────────────────────────────────
    device = "cuda:0" if torch.cuda.is_available() else "cpu"

    agent_cfg = HexapodPPORunnerCfg()
    agent_cfg.max_iterations = max_iterations
    agent_cfg.device = device

    runner = OnPolicyRunner(
        env_wrapped,
        agent_cfg.to_dict(),
        log_dir=str(output_dir),
        device=device,
    )
    log.info("RSL-RL OnPolicyRunner created (device=%s)", device)

    # Write training config for reproducibility
    training_config = {
        "pipeline": "isaaclab",
        "urdf_path": urdf_path,
        "joint_names": joint_names,
        "foot_links": foot_links,
        "num_envs": num_envs,
        "max_iterations": max_iterations,
        "standing_height_m": standing_height,
        "default_joint_positions": default_positions,
        "action_scale_per_joint": action_scales,
        "started_at": time.time(),
    }
    (output_dir / "training_config.json").write_text(
        json.dumps(training_config, indent=2), encoding="utf-8",
    )

    _write_progress(output_dir, 0, max_iterations, 0.0, status="training")

    # ── Training with progress monitoring ──────────────────────────
    # Monkey-patch the runner's log() method to intercept per-iteration
    # stats and write progress.json for MCP monitoring.
    # RSL-RL 3.x calls runner.log(locals()) with rewbuffer/lenbuffer.
    best_reward = float("-inf")
    best_reward_iter = 0
    current_iteration = 0
    patience = getattr(args, "patience", 200)
    reward_history: collections.deque[float] = collections.deque(maxlen=patience)

    _original_log = runner.log

    def _patched_log(locs: dict, width: int = 80, pad: int = 35) -> None:
        nonlocal best_reward, best_reward_iter, current_iteration
        _original_log(locs, width, pad)
        current_iteration = locs.get("it", current_iteration) + 1
        import statistics as _stats
        ep_reward = 0.0
        ep_len = 0.0
        rewbuffer = locs.get("rewbuffer", [])
        lenbuffer = locs.get("lenbuffer", [])
        if len(rewbuffer) > 0:
            ep_reward = _stats.mean(rewbuffer)
        if len(lenbuffer) > 0:
            ep_len = _stats.mean(lenbuffer)

        # ── NaN detection ──────────────────────────────────────────
        if math.isnan(ep_reward) or math.isinf(ep_reward):
            log.error("NaN/inf reward at iter %d — stopping", current_iteration)
            raise EarlyStopException(f"NaN/inf reward at iter {current_iteration}")

        # ── Best reward tracking ───────────────────────────────────
        if ep_reward > best_reward:
            best_reward = ep_reward
            best_reward_iter = current_iteration

        reward_history.append(ep_reward)

        # ── Reward breakdown from ep_infos ─────────────────────────
        reward_breakdown: dict[str, float] = {}
        ep_infos = locs.get("ep_infos", [])
        if ep_infos:
            # ep_infos is a list of dicts; aggregate means for reward keys
            agg: dict[str, list[float]] = {}
            for info in ep_infos:
                for key, val in info.items():
                    if key.startswith("rew_") or key.startswith("Episode_Reward/"):
                        clean_key = key.replace("Episode_Reward/", "").replace("rew_", "")
                        try:
                            agg.setdefault(clean_key, []).append(float(val))
                        except (TypeError, ValueError):
                            pass
            reward_breakdown = {k: _stats.mean(v) for k, v in agg.items() if v}

        # ── Plateau detection ──────────────────────────────────────
        if (len(reward_history) >= patience
                and current_iteration > patience
                and current_iteration - best_reward_iter >= patience):
            old_reward = reward_history[0]
            improvement = best_reward - old_reward
            if improvement < 1.0:
                log.warning(
                    "Reward plateau: best %.2f hasn't improved by >1.0 "
                    "over last %d iters (old=%.2f) — early stopping",
                    best_reward, patience, old_reward,
                )
                raise EarlyStopException(
                    f"Reward plateau at iter {current_iteration} "
                    f"(best={best_reward:.2f}, no >1.0 improvement in {patience} iters)"
                )

        lr = runner.alg.learning_rate if hasattr(runner.alg, "learning_rate") else 0.0

        # ── FPS / timing metrics ────────────────────────────────
        collection_time = locs.get("collection_time", 0.0)
        learn_time = locs.get("learn_time", 0.0)
        total_time = collection_time + learn_time
        num_transitions = num_envs * runner.num_steps_per_env
        fps = num_transitions / total_time if total_time > 0 else 0.0

        _write_progress(
            output_dir,
            iteration=current_iteration,
            max_iterations=max_iterations,
            mean_reward=ep_reward,
            status="training",
            mean_episode_length=ep_len,
            learning_rate=lr,
            best_reward=best_reward,
            best_reward_iter=best_reward_iter,
            reward_breakdown=reward_breakdown,
            fps=fps,
            collection_time=collection_time,
            learn_time=learn_time,
        )

    runner.log = _patched_log

    early_stopped = False
    try:
        runner.learn(
            num_learning_iterations=max_iterations,
            init_at_random_ep_len=True,
        )
    except EarlyStopException as exc:
        log.info("Early stop: %s", exc.reason)
        early_stopped = True
    except KeyboardInterrupt:
        log.info("Training interrupted at iteration %d", current_iteration)
        early_stopped = True
    except Exception as exc:
        log.error("Training failed: %s", exc, exc_info=True)
        _write_progress(output_dir, current_iteration, max_iterations,
                        best_reward if best_reward > float("-inf") else 0.0,
                        status="error", error=str(exc))
        env.close()
        simulation_app.close()
        return 1

    # ── Export policy ──────────────────────────────────────────────
    deployed_dir = _export_deployment_config(output_dir, mod)

    try:
        # RSL-RL 3.x: policy is runner.alg.policy (ActorCritic)
        # act_inference takes TensorDict, but deployment needs plain tensor input.
        # Extract the actor MLP and obs normalizer into a simple wrapper.
        policy = runner.alg.policy
        policy.eval()

        obs_dict = env_wrapped.get_observations()
        if hasattr(obs_dict, "keys") and "policy" in obs_dict.keys():
            obs_dim = obs_dict["policy"].shape[-1]
        else:
            obs_dim = obs_dict.shape[-1]

        class _PolicyWrapper(torch.nn.Module):
            """Thin wrapper: plain tensor in → action tensor out."""
            def __init__(self, actor_net: torch.nn.Module, normalizer: torch.nn.Module):
                super().__init__()
                self.actor = actor_net
                self.normalizer = normalizer

            def forward(self, obs: torch.Tensor) -> torch.Tensor:
                return self.actor(self.normalizer(obs))

        wrapper = _PolicyWrapper(policy.actor, policy.actor_obs_normalizer).to("cpu")
        wrapper.eval()
        dummy_obs = torch.zeros(1, obs_dim, device="cpu")
        traced = torch.jit.trace(wrapper, dummy_obs)
        policy_path = deployed_dir / "policy.pt"
        traced.save(str(policy_path))
        log.info("JIT-traced policy exported to %s", policy_path)
    except Exception as exc:
        log.warning("Failed to export JIT policy: %s", exc)

    final_status = "early_stopped" if early_stopped else "completed"
    _write_progress(
        output_dir,
        iteration=current_iteration if early_stopped else max_iterations,
        max_iterations=max_iterations,
        mean_reward=best_reward if best_reward > float("-inf") else 0.0,
        status=final_status,
    )

    log.info(
        "Training %s at iter %d. Best reward: %.3f",
        final_status, current_iteration, best_reward,
    )

    env.close()
    simulation_app.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — can also be called directly."""
    parser = argparse.ArgumentParser(description="Isaac Lab RL training for SolidMind CAD")
    parser.add_argument("--env-config", required=True, help="Path to generated env config .py")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--max-iterations", type=int, default=None, help="Override max iterations")
    parser.add_argument("--num-envs", type=int, default=None, help="Override num envs")
    parser.add_argument("--no-headless", action="store_true", help="Run with GUI")
    parser.add_argument("--patience", type=int, default=200,
                        help="Stop if reward doesn't improve by >1.0 over this many iterations")
    args = parser.parse_args(argv)

    mod = _load_env_config_module(args.env_config)
    return run_isaaclab_training(args, mod)


if __name__ == "__main__":
    raise SystemExit(main())
