"""JIT-trace and export a trained residual actor network.

Produces a deployment package:
- ``policy.pt`` — JIT-traced model
- ``normalization_params.json`` — observation mean/std
- ``training_config.json`` — joint order, action scale, alpha
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger("solidmind.rl_export")


def export_policy(
    checkpoint_dir: Path,
    output_dir: Path,
    *,
    joint_names: list[str],
    action_scale: float = 0.3,
    alpha: float = 0.3,
    obs_dim: int = 30,
) -> dict[str, Any]:
    """Export a trained policy checkpoint to a deployment package.

    Args:
        checkpoint_dir: Directory containing the RSL-RL checkpoint.
        output_dir: Where to write the deployment package.
        joint_names: Ordered joint names matching the policy's action dim.
        action_scale: Scale factor for policy output → radians.
        alpha: Residual blending factor for deployment.
        obs_dim: Observation dimensionality.

    Returns:
        Dict with exported file paths and metadata.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Look for checkpoint file — prefer best, then latest numbered, then model.pt
    ckpt_file = checkpoint_dir / "checkpoints" / "model_best.pt"
    if not ckpt_file.is_file():
        ckpt_file = checkpoint_dir / "model.pt"
    if not ckpt_file.is_file():
        # Try common RSL-RL checkpoint naming
        candidates = sorted(checkpoint_dir.glob("model_*.pt"))
        if not candidates:
            candidates = sorted(checkpoint_dir.glob("checkpoints/model_*.pt"))
        if candidates:
            ckpt_file = candidates[-1]  # Latest
        else:
            raise FileNotFoundError(
                f"No model checkpoint found in {checkpoint_dir}"
            )

    # Copy/trace the policy
    policy_path = output_dir / "policy.pt"

    # Track whether we extracted normalization from checkpoint
    norm_from_checkpoint = False
    obs_mean_list: list[float] = [0.0] * obs_dim
    obs_std_list: list[float] = [1.0] * obs_dim

    try:
        import torch  # type: ignore[import-not-found]
        # Load checkpoint and JIT trace
        checkpoint = torch.load(ckpt_file, map_location="cpu", weights_only=False)

        if isinstance(checkpoint, torch.jit.ScriptModule):
            # Already JIT-traced
            torch.jit.save(checkpoint, str(policy_path))
        elif isinstance(checkpoint, dict):
            # Extract normalization stats if present in checkpoint
            if "obs_mean" in checkpoint:
                obs_mean_t = checkpoint["obs_mean"]
                obs_var_t = checkpoint.get("obs_var", None)
                if hasattr(obs_mean_t, 'cpu'):
                    obs_mean_list = obs_mean_t.cpu().tolist()
                    if obs_var_t is not None:
                        import math
                        obs_std_list = [
                            math.sqrt(v + 1e-8) for v in obs_var_t.cpu().tolist()
                        ]
                    norm_from_checkpoint = True
                    log.info("Extracted normalization stats from checkpoint")

            if "model_state_dict" in checkpoint:
                # Need to reconstruct and trace — copy raw for now
                shutil.copy2(ckpt_file, policy_path)
                log.warning(
                    "Checkpoint is a state dict, not JIT-traced. "
                    "Copied raw — full JIT tracing requires model architecture."
                )
            elif "actor_critic" in checkpoint:
                # Our PPOTrainer checkpoint format — try to reconstruct actor
                try:
                    from rl_training.ppo import ActorCritic, PPOHyperparams
                    action_dim = len(joint_names) if joint_names else obs_dim
                    ac = ActorCritic(obs_dim, action_dim, PPOHyperparams(), device="cpu")
                    ac.load_state_dict(checkpoint["actor_critic"])
                    traced = ac.export_actor_jit(obs_dim)
                    torch.jit.save(traced, str(policy_path))
                    log.info("JIT-traced actor from PPOTrainer checkpoint")
                except Exception as exc:
                    log.warning("Failed to JIT-trace actor: %s — copying raw", exc)
                    shutil.copy2(ckpt_file, policy_path)
            else:
                shutil.copy2(ckpt_file, policy_path)
        else:
            shutil.copy2(ckpt_file, policy_path)
    except ImportError:
        # No torch — just copy the file
        shutil.copy2(ckpt_file, policy_path)
        log.warning("PyTorch not available — copied checkpoint without tracing")
    except Exception as exc:
        # torch.load failed (corrupt/incompatible file) — copy raw
        shutil.copy2(ckpt_file, policy_path)
        log.warning("torch.load failed (%s) — copied checkpoint raw", exc)

    # Check for normalization params written by PPOTrainer.export_policy()
    # in the deployed/ subdirectory of the training output
    if not norm_from_checkpoint:
        deployed_norm = checkpoint_dir / "deployed" / "normalization_params.json"
        if deployed_norm.is_file():
            try:
                existing = json.loads(deployed_norm.read_text(encoding="utf-8"))
                if existing.get("obs_mean") and any(v != 0.0 for v in existing["obs_mean"]):
                    obs_mean_list = existing["obs_mean"]
                    obs_std_list = existing.get("obs_std", [1.0] * obs_dim)
                    norm_from_checkpoint = True
                    log.info("Loaded normalization stats from deployed/ directory")
            except Exception:
                pass

    # Write normalization params
    norm_path = output_dir / "normalization_params.json"
    norm_data: dict[str, Any] = {
        "obs_mean": obs_mean_list,
        "obs_std": obs_std_list,
    }
    if not norm_from_checkpoint:
        norm_data["_note"] = "Placeholder values — replace with stats from training"
    norm_path.write_text(json.dumps(norm_data, indent=2), encoding="utf-8")

    # Write deployment config
    config_path = output_dir / "deployment_config.json"
    config_data: dict[str, Any] = {
        "joint_names": joint_names,
        "action_scale": action_scale,
        "alpha": alpha,
        "obs_dim": obs_dim,
        "action_dim": len(joint_names),
    }
    config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")

    return {
        "policy_path": str(policy_path),
        "normalization_path": str(norm_path),
        "config_path": str(config_path),
        "joint_names": joint_names,
        "action_scale": action_scale,
        "alpha": alpha,
        "normalization_from_training": norm_from_checkpoint,
    }
