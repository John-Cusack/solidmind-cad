"""RSL-RL PPO configuration for hexapod locomotion training.

Uses Isaac Lab's ``RslRlOnPolicyRunnerCfg`` which serializes to the dict
format RSL-RL v5 expects via ``.to_dict()``.
"""

from __future__ import annotations

from isaaclab.utils import configclass  # type: ignore[import-not-found]
from isaaclab_rl.rsl_rl import (  # type: ignore[import-not-found]
    RslRlOnPolicyRunnerCfg,
    RslRlPpoActorCriticCfg,
    RslRlPpoAlgorithmCfg,
)


@configclass
class HexapodPPORunnerCfg(RslRlOnPolicyRunnerCfg):
    """SolidMind hexapod locomotion PPO config.

    Upgrades from custom PPO:
    - Actor/Critic dims [256,128,64] -> [512,256,128] (Isaac Lab default)
    - init_noise_std 0.3 -> 0.5 (reduced from 1.0 to limit early jitter)
    - Adaptive LR with KL target (RSL-RL built-in)
    """

    seed: int = 42
    num_steps_per_env: int = 48  # longer rollouts for better value estimates with short episodes
    max_iterations: int = 3000
    save_interval: int = 100
    experiment_name: str = "solidmind_hexapod"
    logger: str = "tensorboard"  # type: ignore[assignment]

    obs_groups: dict = {"actor": ["policy"], "critic": ["policy"]}  # type: ignore[assignment]

    policy = RslRlPpoActorCriticCfg(
        class_name="ActorCritic",
        init_noise_std=0.5,
        noise_std_type="scalar",  # was "log" — log type can underflow to 0/negative, crashing Normal dist
        actor_obs_normalization=False,
        critic_obs_normalization=False,
        actor_hidden_dims=[256, 128, 128],
        critic_hidden_dims=[256, 128, 128],
        activation="elu",
    )

    algorithm = RslRlPpoAlgorithmCfg(
        class_name="PPO",
        value_loss_coef=1.0,
        use_clipped_value_loss=True,
        clip_param=0.2,
        entropy_coef=0.005,  # less exploration noise once survival is solved
        num_learning_epochs=5,
        num_mini_batches=8,
        learning_rate=1e-3,
        schedule="adaptive",
        desired_kl=0.01,  # tighter trust region prevents destructive updates
        gamma=0.99,
        lam=0.95,
        max_grad_norm=1.0,
    )
