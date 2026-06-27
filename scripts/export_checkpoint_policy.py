"""Export a RSL-RL checkpoint to a JIT-traced policy for evaluation.

Usage::

    $ISAAC_PYTHON scripts/export_checkpoint_policy.py \
        --env-config training_runs/hex18_fresh_env_config.py \
        --checkpoint training_runs/hex18_debug/model_600.pt \
        --output training_runs/hex18_debug/policy_600.pt
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
    parser = argparse.ArgumentParser(description="Export RSL-RL checkpoint to JIT policy")
    parser.add_argument("--env-config", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    from isaaclab.app import AppLauncher

    launcher = AppLauncher(headless=True)
    simulation_app = launcher.app

    import torch
    from isaaclab.envs import ManagerBasedRLEnv
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
        num_envs=1,
    )

    # Create env to get obs shape
    from isaaclab_rl.rsl_rl import RslRlVecEnvWrapper

    env = ManagerBasedRLEnv(cfg=env_cfg)
    env_wrapped = RslRlVecEnvWrapper(env)

    # Create runner to build policy with correct architecture
    agent_cfg = HexapodPPORunnerCfg()
    agent_cfg.max_iterations = 1
    agent_cfg.device = "cuda:0" if torch.cuda.is_available() else "cpu"

    runner = OnPolicyRunner(
        env_wrapped,
        agent_cfg.to_dict(),
        log_dir="/tmp/export_tmp",
        device=agent_cfg.device,
    )

    # Load checkpoint weights
    runner.load(args.checkpoint)
    print(f"Loaded checkpoint: {args.checkpoint}")

    # Export JIT policy
    policy = runner.alg.policy
    policy.eval()

    obs_dict = env_wrapped.get_observations()
    if hasattr(obs_dict, "keys") and "policy" in obs_dict.keys():
        obs_dim = obs_dict["policy"].shape[-1]
    else:
        obs_dim = obs_dict.shape[-1]

    class _PolicyWrapper(torch.nn.Module):
        def __init__(self, actor_net, normalizer):
            super().__init__()
            self.actor = actor_net
            self.normalizer = normalizer

        def forward(self, obs: torch.Tensor) -> torch.Tensor:
            return self.actor(self.normalizer(obs))

    wrapper = _PolicyWrapper(policy.actor, policy.actor_obs_normalizer).to("cpu")
    wrapper.eval()
    dummy_obs = torch.zeros(1, obs_dim, device="cpu")
    traced = torch.jit.trace(wrapper, dummy_obs)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    traced.save(str(output_path))
    print(f"JIT policy saved to: {output_path}")

    env.close()
    simulation_app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
