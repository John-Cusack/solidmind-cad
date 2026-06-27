"""Convert an RSL-RL checkpoint (model_*.pt) to a JIT-traced policy.pt.

Usage::

    $ISAAC_PYTHON scripts/export_checkpoint_to_jit.py \
        --checkpoint training_runs/.../model_700.pt \
        --env-config training_runs/hex18_v3_env_config.py \
        --output training_runs/.../deployed/policy.pt
"""

from __future__ import annotations

import argparse
import importlib.util
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--env-config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    import torch
    from rsl_rl.modules import ActorCritic

    mod = _load_module(args.env_config)
    n_joints = len(getattr(mod, "JOINT_NAMES", []))
    obs_dim = 12 + 3 * n_joints  # base(12) + 3*joints
    action_dim = n_joints

    # Create the same network architecture as training
    policy = ActorCritic(
        num_actor_obs=obs_dim,
        num_critic_obs=obs_dim,
        num_actions=action_dim,
        actor_hidden_dims=[512, 256, 128],
        critic_hidden_dims=[512, 256, 128],
        activation="elu",
        init_noise_std=0.5,
        noise_std_type="log",
    )

    # Load checkpoint
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    policy.load_state_dict(ckpt["model_state_dict"])
    policy.eval()
    print(f"Loaded checkpoint from {args.checkpoint}")

    # Wrap actor + normalizer for deployment
    class _PolicyWrapper(torch.nn.Module):
        def __init__(self, actor_net, normalizer):
            super().__init__()
            self.actor = actor_net
            self.normalizer = normalizer

        def forward(self, obs: torch.Tensor) -> torch.Tensor:
            return self.actor(self.normalizer(obs))

    wrapper = _PolicyWrapper(policy.actor, policy.actor_obs_normalizer)
    wrapper.eval()

    dummy = torch.zeros(1, obs_dim)
    traced = torch.jit.trace(wrapper, dummy)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    traced.save(str(out_path))
    print(f"JIT policy saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
