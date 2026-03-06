"""Standalone PPO implementation for hexapod locomotion training.

Implements Proximal Policy Optimization with:
- Generalized Advantage Estimation (GAE, γ=0.99, λ=0.95)
- Clipped surrogate objective (ε=0.2)
- Entropy bonus (coefficient=0.01)
- Actor-critic MLP with ELU activations (512, 256, 128)
- Running observation normalization
- Gradient clipping (max_norm=1.0)

Designed to run inside Isaac Sim's Python (with PyTorch + CUDA).
Falls back to CPU if CUDA is unavailable.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("solidmind.ppo")


@dataclass(frozen=True, slots=True)
class PPOHyperparams:
    """PPO training hyperparameters."""

    learning_rate: float = 1e-3
    num_epochs: int = 5
    num_mini_batches: int = 4
    gamma: float = 0.99
    lam: float = 0.95
    entropy_coef: float = 0.01
    value_loss_coef: float = 1.0
    clip_param: float = 0.2
    max_grad_norm: float = 1.0
    actor_hidden_dims: tuple[int, ...] = (512, 256, 128)
    critic_hidden_dims: tuple[int, ...] = (512, 256, 128)
    activation: str = "elu"
    num_steps_per_env: int = 24
    desired_kl: float = 0.01
    lr_schedule: str = "adaptive"  # "adaptive" or "fixed"


def _get_activation(name: str) -> Any:
    """Get PyTorch activation function by name."""
    import torch.nn as nn

    activations = {
        "elu": nn.ELU,
        "relu": nn.ReLU,
        "tanh": nn.Tanh,
        "leaky_relu": nn.LeakyReLU,
    }
    cls = activations.get(name.lower())
    if cls is None:
        raise ValueError(f"Unknown activation: {name}. Available: {sorted(activations)}")
    return cls


class _NormalizedActor:
    """Wraps an actor ``nn.Module`` with running-mean normalization for JIT export.

    ``torch.jit.trace`` needs a real ``nn.Module`` at the top level.  We
    lazily import ``torch.nn`` and construct the module in ``__init__``
    so that the rest of the file can be imported without PyTorch.
    """

    def __new__(cls, actor: Any, obs_mean: Any, obs_var: Any) -> Any:
        import torch
        import torch.nn as nn

        module = _NormalizedActorImpl(actor, obs_mean, obs_var)
        return module


class _NormalizedActorImpl:
    """Placeholder — replaced at import time by a real nn.Module subclass."""
    pass


# Build the real nn.Module subclass at import time only if torch is available.
# This avoids issues with torch.jit.trace not finding a qualified name.
try:
    import torch as _torch
    import torch.nn as _nn

    class _NormalizedActorImpl(_nn.Module):  # type: ignore[no-redef]
        """nn.Module that normalizes observations then runs the actor."""

        def __init__(self, actor: _nn.Module, obs_mean: Any, obs_var: Any) -> None:
            super().__init__()
            self.actor_net = actor
            self.register_buffer("obs_mean", obs_mean.clone())
            self.register_buffer("obs_var", obs_var.clone())

        def forward(self, obs: Any) -> Any:
            norm_obs = (obs - self.obs_mean) / _torch.sqrt(self.obs_var + 1e-8)
            return self.actor_net(norm_obs)
except ImportError:
    pass


class ActorCritic:
    """MLP actor-critic network for PPO.

    Actor outputs action mean (std is learned as a parameter).
    Critic outputs scalar state value.
    """

    def __init__(
        self,
        obs_dim: int,
        action_dim: int,
        params: PPOHyperparams,
        device: str = "cuda:0",
    ) -> None:
        import torch
        import torch.nn as nn

        self.device = device

        activation_cls = _get_activation(params.activation)

        # Build actor network
        actor_layers: list[nn.Module] = []
        in_dim = obs_dim
        for hidden_dim in params.actor_hidden_dims:
            actor_layers.append(nn.Linear(in_dim, hidden_dim))
            actor_layers.append(activation_cls())
            in_dim = hidden_dim
        actor_layers.append(nn.Linear(in_dim, action_dim))
        self.actor = nn.Sequential(*actor_layers).to(device)

        # Build critic network
        critic_layers: list[nn.Module] = []
        in_dim = obs_dim
        for hidden_dim in params.critic_hidden_dims:
            critic_layers.append(nn.Linear(in_dim, hidden_dim))
            critic_layers.append(activation_cls())
            in_dim = hidden_dim
        critic_layers.append(nn.Linear(in_dim, 1))
        self.critic = nn.Sequential(*critic_layers).to(device)

        # Learned action log-std (one per action dimension).
        # Initialize to log(0.3) ≈ -1.2 so initial std ~ 0.3 rad,
        # reasonable exploration within action_scale range.
        self.log_std = nn.Parameter(
            torch.full((action_dim,), math.log(0.3), device=device)
        )

        # Initialize weights
        self._init_weights()

    def _init_weights(self) -> None:
        """Initialize network weights with scaled orthogonal init."""
        import torch.nn as nn

        for module in [self.actor, self.critic]:
            for m in module.modules():
                if isinstance(m, nn.Linear):
                    nn.init.orthogonal_(m.weight, gain=1.0)
                    nn.init.constant_(m.bias, 0.0)

        # Output layers with smaller gain
        actor_last = list(self.actor.modules())[-1]
        if hasattr(actor_last, 'weight'):
            import torch.nn as nn
            nn.init.orthogonal_(actor_last.weight, gain=0.01)

    def act(self, obs: Any) -> tuple[Any, Any, Any, Any]:
        """Sample actions from the policy.

        Returns: (actions, log_probs, values, action_mean)
        """
        import torch
        import torch.distributions as D

        action_mean = self.actor(obs)
        std = torch.exp(self.log_std)
        dist = D.Normal(action_mean, std)
        actions = dist.sample()
        log_probs = dist.log_prob(actions).sum(dim=-1)
        values = self.critic(obs).squeeze(-1)

        return actions, log_probs, values, action_mean

    def evaluate(self, obs: Any, actions: Any) -> tuple[Any, Any, Any]:
        """Evaluate actions for PPO update.

        Returns: (log_probs, values, entropy)
        """
        import torch
        import torch.distributions as D

        action_mean = self.actor(obs)
        std = torch.exp(self.log_std)
        dist = D.Normal(action_mean, std)
        log_probs = dist.log_prob(actions).sum(dim=-1)
        values = self.critic(obs).squeeze(-1)
        entropy = dist.entropy().mean(dim=-1)

        return log_probs, values, entropy

    def parameters(self) -> Any:
        """All trainable parameters."""
        import itertools
        return itertools.chain(
            self.actor.parameters(),
            self.critic.parameters(),
            [self.log_std],
        )

    def state_dict(self) -> dict[str, Any]:
        """Serialize model state."""
        return {
            "actor": self.actor.state_dict(),
            "critic": self.critic.state_dict(),
            "log_std": self.log_std.data,
        }

    def load_state_dict(self, state: dict[str, Any]) -> None:
        """Restore model state."""
        self.actor.load_state_dict(state["actor"])
        self.critic.load_state_dict(state["critic"])
        self.log_std.data.copy_(state["log_std"])

    def export_actor_jit(
        self,
        obs_dim: int,
        obs_mean: Any = None,
        obs_var: Any = None,
    ) -> Any:
        """Export the actor with built-in normalization as a JIT-traced module.

        When *obs_mean* and *obs_var* are provided, the exported module
        normalizes raw observations internally so the deployment controller
        does not need to apply normalization separately.
        """
        import torch

        self.actor.eval()

        if obs_mean is not None and obs_var is not None:
            wrapper = _NormalizedActor(self.actor, obs_mean, obs_var)
            wrapper.eval()
            dummy = torch.zeros(1, obs_dim, device=self.device)
            traced = torch.jit.trace(wrapper, dummy)
        else:
            dummy = torch.zeros(1, obs_dim, device=self.device)
            traced = torch.jit.trace(self.actor, dummy)
        return traced


class RunningMeanStd:
    """Running mean and standard deviation for observation normalization.

    Uses Welford's online algorithm for numerically stable updates.
    """

    def __init__(self, shape: tuple[int, ...], device: str = "cuda:0") -> None:
        import torch

        self.mean = torch.zeros(shape, dtype=torch.float32, device=device)
        self.var = torch.ones(shape, dtype=torch.float32, device=device)
        self.count: float = 1e-4  # small epsilon to avoid div-by-zero

    def update(self, batch: Any) -> None:
        """Update running statistics with a new batch of data."""
        import torch

        batch_mean = torch.mean(batch, dim=0)
        batch_var = torch.var(batch, dim=0, unbiased=False)
        batch_count = batch.shape[0]

        # Welford's parallel algorithm
        delta = batch_mean - self.mean
        total_count = self.count + batch_count

        self.mean = self.mean + delta * batch_count / total_count
        m_a = self.var * self.count
        m_b = batch_var * batch_count
        m2 = m_a + m_b + delta ** 2 * self.count * batch_count / total_count
        self.var = m2 / total_count

        self.count = total_count

    def normalize(self, x: Any) -> Any:
        """Normalize observations using running statistics."""
        import torch

        return (x - self.mean) / torch.sqrt(self.var + 1e-8)


class RolloutStorage:
    """Buffer for storing PPO rollout data."""

    def __init__(
        self,
        num_envs: int,
        num_steps: int,
        obs_dim: int,
        action_dim: int,
        device: str = "cuda:0",
    ) -> None:
        import torch

        self.num_envs = num_envs
        self.num_steps = num_steps
        self.device = device

        self.observations = torch.zeros(num_steps, num_envs, obs_dim, device=device)
        self.actions = torch.zeros(num_steps, num_envs, action_dim, device=device)
        self.rewards = torch.zeros(num_steps, num_envs, device=device)
        self.dones = torch.zeros(num_steps, num_envs, device=device)
        self.log_probs = torch.zeros(num_steps, num_envs, device=device)
        self.values = torch.zeros(num_steps, num_envs, device=device)
        self.returns = torch.zeros(num_steps, num_envs, device=device)
        self.advantages = torch.zeros(num_steps, num_envs, device=device)

        self.step = 0

    def add(
        self,
        obs: Any,
        actions: Any,
        rewards: Any,
        dones: Any,
        log_probs: Any,
        values: Any,
    ) -> None:
        """Add one timestep of data."""
        self.observations[self.step] = obs
        self.actions[self.step] = actions
        self.rewards[self.step] = rewards
        self.dones[self.step] = dones.float()
        self.log_probs[self.step] = log_probs
        self.values[self.step] = values
        self.step += 1

    def compute_returns(self, last_values: Any, gamma: float, lam: float) -> None:
        """Compute GAE advantages and returns."""
        import torch

        last_gae = torch.zeros(self.num_envs, device=self.device)

        for t in reversed(range(self.num_steps)):
            if t == self.num_steps - 1:
                next_values = last_values
            else:
                next_values = self.values[t + 1]

            next_non_terminal = 1.0 - self.dones[t]
            delta = self.rewards[t] + gamma * next_values * next_non_terminal - self.values[t]
            last_gae = delta + gamma * lam * next_non_terminal * last_gae
            self.advantages[t] = last_gae

        self.returns = self.advantages + self.values

    def mini_batch_generator(self, num_mini_batches: int) -> Any:
        """Generate shuffled mini-batches for PPO update."""
        import torch

        batch_size = self.num_envs * self.num_steps
        mini_batch_size = batch_size // num_mini_batches

        # Flatten time and env dimensions
        obs = self.observations.reshape(-1, self.observations.shape[-1])
        actions = self.actions.reshape(-1, self.actions.shape[-1])
        log_probs = self.log_probs.reshape(-1)
        values = self.values.reshape(-1)
        returns = self.returns.reshape(-1)
        advantages = self.advantages.reshape(-1)

        # Shuffle indices
        indices = torch.randperm(batch_size, device=self.device)

        for start in range(0, batch_size, mini_batch_size):
            end = start + mini_batch_size
            mb_indices = indices[start:end]

            yield (
                obs[mb_indices],
                actions[mb_indices],
                log_probs[mb_indices],
                returns[mb_indices],
                advantages[mb_indices],
                values[mb_indices],
            )

    def reset(self) -> None:
        """Reset step counter for next rollout."""
        self.step = 0


class PPOTrainer:
    """PPO training loop manager.

    Handles the collect-rollouts → compute-advantages → PPO-update cycle.
    """

    def __init__(
        self,
        env: Any,  # HexapodLocomotionEnv
        params: PPOHyperparams | None = None,
        device: str = "cuda:0",
    ) -> None:
        import torch

        self.env = env
        self.params = params or PPOHyperparams()
        self.device = device

        # Create actor-critic
        self.actor_critic = ActorCritic(
            obs_dim=env.obs_dim,
            action_dim=env.action_dim,
            params=self.params,
            device=device,
        )

        # Optimizer
        self.optimizer = torch.optim.Adam(
            self.actor_critic.parameters(),
            lr=self.params.learning_rate,
        )

        # Observation normalization
        self.obs_normalizer = RunningMeanStd(
            shape=(env.obs_dim,), device=device,
        )

        # Rollout storage
        self.storage = RolloutStorage(
            num_envs=env.num_envs,
            num_steps=self.params.num_steps_per_env,
            obs_dim=env.obs_dim,
            action_dim=env.action_dim,
            device=device,
        )

        # Pre-allocated buffer for raw (un-normalized) observations
        # used to update the normalizer after each rollout without cloning.
        self._raw_obs_buf = torch.zeros(
            self.params.num_steps_per_env, env.num_envs, env.obs_dim,
            device=device,
        )

        # Iteration counter
        self.current_iteration = 0

        # Learning rate (may be adapted)
        self._current_lr = self.params.learning_rate

    def collect_rollouts(self) -> dict[str, float]:
        """Collect rollout data from the environment.

        Returns basic statistics (mean_reward, mean_episode_length).
        """
        import torch

        self.storage.reset()
        obs = self.env.reset() if self.current_iteration == 0 else self._last_obs

        # Accumulate on GPU — only sync once after the loop
        total_reward_t = torch.zeros(1, device=self.device)
        total_episodes_t = torch.zeros(1, device=self.device)

        for step in range(self.params.num_steps_per_env):
            # Normalize observations with frozen normalizer
            norm_obs = self.obs_normalizer.normalize(obs)
            # Store raw obs in pre-allocated buffer (no clone needed)
            self._raw_obs_buf[step] = obs

            # Get action from policy
            with torch.no_grad():
                actions, log_probs, values, _ = self.actor_critic.act(norm_obs)

            # Step environment
            next_obs, rewards, dones, info = self.env.step(actions)

            # NaN/Inf safety check (#13) — no .item() unless assertion fails
            assert torch.isfinite(next_obs).all(), (
                f"Non-finite observation at step {step}: "
                f"NaN={torch.isnan(next_obs).sum().item()}, "
                f"Inf={torch.isinf(next_obs).sum().item()}"
            )

            # Store transition
            self.storage.add(norm_obs, actions, rewards, dones, log_probs, values)

            total_reward_t += rewards.sum()
            total_episodes_t += dones.sum()

            obs = next_obs

        self._last_obs = obs

        # ONE GPU→CPU sync for rollout stats
        total_reward = total_reward_t.item()
        total_episodes = total_episodes_t.item()

        # Update normalizer once with the full rollout batch (#10)
        all_obs = self._raw_obs_buf.reshape(-1, self._raw_obs_buf.shape[-1])
        self.obs_normalizer.update(all_obs)

        # Compute returns with GAE
        with torch.no_grad():
            norm_obs = self.obs_normalizer.normalize(obs)
            _, _, last_values, _ = self.actor_critic.act(norm_obs)
        self.storage.compute_returns(last_values, self.params.gamma, self.params.lam)

        mean_reward = total_reward / max(total_episodes, 1)
        return {
            "mean_reward": mean_reward,
            "total_episodes": int(total_episodes),
        }

    def update(self) -> dict[str, float]:
        """Run PPO update epochs on collected rollout data.

        Returns training statistics (policy_loss, value_loss, entropy, kl_divergence).
        """
        import torch

        # Accumulate losses on GPU — one sync after all epochs
        total_policy_loss_t = torch.zeros(1, device=self.device)
        total_value_loss_t = torch.zeros(1, device=self.device)
        total_entropy_t = torch.zeros(1, device=self.device)
        total_kl_t = torch.zeros(1, device=self.device)
        num_updates = 0

        # Normalize advantages once before the epoch loop (avoids 20 redundant mean/std)
        flat_adv = self.storage.advantages.reshape(-1)
        flat_adv = (flat_adv - flat_adv.mean()) / (flat_adv.std() + 1e-5)
        self.storage.advantages = flat_adv.reshape(self.storage.num_steps, self.storage.num_envs)

        for _epoch in range(self.params.num_epochs):
            for (
                mb_obs, mb_actions, mb_old_log_probs, mb_returns, mb_advantages, mb_old_values,
            ) in self.storage.mini_batch_generator(self.params.num_mini_batches):
                # Evaluate actions under current policy
                new_log_probs, values, entropy = self.actor_critic.evaluate(mb_obs, mb_actions)

                # Advantages already normalized above
                advantages = mb_advantages

                # Policy loss (clipped surrogate)
                ratio = torch.exp(new_log_probs - mb_old_log_probs)
                surr1 = ratio * advantages
                surr2 = torch.clamp(ratio, 1.0 - self.params.clip_param, 1.0 + self.params.clip_param) * advantages
                policy_loss = -torch.min(surr1, surr2).mean()

                # Clipped value loss (#11) — prevent value function overshooting
                v_clipped = mb_old_values + torch.clamp(
                    values - mb_old_values,
                    -self.params.clip_param,
                    self.params.clip_param,
                )
                vl_unclipped = (values - mb_returns) ** 2
                vl_clipped = (v_clipped - mb_returns) ** 2
                value_loss = torch.max(vl_unclipped, vl_clipped).mean()

                # Entropy bonus
                entropy_loss = -entropy.mean()

                # Total loss
                loss = (
                    policy_loss
                    + self.params.value_loss_coef * value_loss
                    + self.params.entropy_coef * entropy_loss
                )

                # Optimize
                self.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    list(self.actor_critic.parameters()),
                    self.params.max_grad_norm,
                )
                self.optimizer.step()

                # Accumulate on GPU (detach to avoid graph retention)
                with torch.no_grad():
                    total_kl_t += (mb_old_log_probs - new_log_probs).mean()
                    total_policy_loss_t += policy_loss.detach()
                    total_value_loss_t += value_loss.detach()
                    total_entropy_t += entropy.mean().detach()
                num_updates += 1

        # ONE GPU→CPU sync for all update stats
        total_policy_loss = total_policy_loss_t.item()
        total_value_loss = total_value_loss_t.item()
        total_entropy = total_entropy_t.item()
        total_kl = total_kl_t.item()

        # Adaptive learning rate based on KL divergence
        mean_kl = total_kl / max(num_updates, 1)
        if self.params.lr_schedule == "adaptive":
            if mean_kl > self.params.desired_kl * 2.0:
                self._current_lr = max(self._current_lr / 1.5, 5e-5)
            elif mean_kl < self.params.desired_kl / 2.0:
                self._current_lr = min(self._current_lr * 1.5, 5e-4)
            for pg in self.optimizer.param_groups:
                pg["lr"] = self._current_lr

        self.current_iteration += 1

        return {
            "policy_loss": total_policy_loss / max(num_updates, 1),
            "value_loss": total_value_loss / max(num_updates, 1),
            "entropy": total_entropy / max(num_updates, 1),
            "kl_divergence": mean_kl,
            "learning_rate": self._current_lr,
        }

    def save_checkpoint(self, path: str) -> None:
        """Save training checkpoint."""
        import torch

        state = {
            "actor_critic": self.actor_critic.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "obs_mean": self.obs_normalizer.mean,
            "obs_var": self.obs_normalizer.var,
            "obs_count": self.obs_normalizer.count,
            "iteration": self.current_iteration,
            "learning_rate": self._current_lr,
            "obs_dim": self.env.obs_dim,
            "action_dim": self.env.action_dim,
            "hyperparams": {
                "actor_hidden_dims": list(self.params.actor_hidden_dims),
                "critic_hidden_dims": list(self.params.critic_hidden_dims),
                "activation": self.params.activation,
            },
        }
        torch.save(state, path)
        log.info("Checkpoint saved: %s (iteration %d)", path, self.current_iteration)

    def load_checkpoint(self, path: str) -> None:
        """Load training checkpoint."""
        import torch

        state = torch.load(path, map_location=self.device, weights_only=False)

        # Rebuild actor-critic if architecture differs from current
        hp = state.get("hyperparams", {})
        if hp:
            saved_actor = tuple(hp.get("actor_hidden_dims", list(self.params.actor_hidden_dims)))
            saved_critic = tuple(hp.get("critic_hidden_dims", list(self.params.critic_hidden_dims)))
            saved_activation = hp.get("activation", self.params.activation)
            if (saved_actor != self.params.actor_hidden_dims
                    or saved_critic != self.params.critic_hidden_dims
                    or saved_activation != self.params.activation):
                log.info(
                    "Rebuilding actor-critic to match checkpoint: actor=%s critic=%s",
                    saved_actor, saved_critic,
                )
                rebuilt_params = PPOHyperparams(
                    actor_hidden_dims=saved_actor,
                    critic_hidden_dims=saved_critic,
                    activation=saved_activation,
                    learning_rate=self.params.learning_rate,
                    num_epochs=self.params.num_epochs,
                    num_mini_batches=self.params.num_mini_batches,
                    gamma=self.params.gamma,
                    lam=self.params.lam,
                    entropy_coef=self.params.entropy_coef,
                    clip_param=self.params.clip_param,
                    max_grad_norm=self.params.max_grad_norm,
                    num_steps_per_env=self.params.num_steps_per_env,
                    desired_kl=self.params.desired_kl,
                )
                obs_dim = state.get("obs_dim", self.env.obs_dim)
                action_dim = state.get("action_dim", self.env.action_dim)
                self.actor_critic = ActorCritic(
                    obs_dim=obs_dim,
                    action_dim=action_dim,
                    params=rebuilt_params,
                    device=self.device,
                )
                self.optimizer = torch.optim.Adam(
                    self.actor_critic.parameters(),
                    lr=self.params.learning_rate,
                )

        self.actor_critic.load_state_dict(state["actor_critic"])
        self.optimizer.load_state_dict(state["optimizer"])
        self.obs_normalizer.mean = state["obs_mean"]
        self.obs_normalizer.var = state["obs_var"]
        self.obs_normalizer.count = state["obs_count"]
        self.current_iteration = state.get("iteration", 0)
        self._current_lr = state.get("learning_rate", self.params.learning_rate)
        log.info("Checkpoint loaded: %s (iteration %d)", path, self.current_iteration)

    def export_policy(self, output_dir: str) -> str:
        """Export the actor as a JIT-traced policy + normalization params.

        Returns the path to the exported policy.pt file.
        """
        import json
        import torch
        from pathlib import Path

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # JIT trace the actor WITH built-in normalization (#4)
        traced = self.actor_critic.export_actor_jit(
            self.env.obs_dim,
            obs_mean=self.obs_normalizer.mean,
            obs_var=self.obs_normalizer.var,
        )
        policy_path = out / "policy.pt"
        torch.jit.save(traced, str(policy_path))

        # Also save normalization parameters for backward compatibility
        norm_path = out / "normalization_params.json"
        norm_data = {
            "obs_mean": self.obs_normalizer.mean.cpu().tolist(),
            "obs_std": torch.sqrt(self.obs_normalizer.var + 1e-8).cpu().tolist(),
        }
        norm_path.write_text(json.dumps(norm_data, indent=2), encoding="utf-8")

        log.info("Policy exported to %s", output_dir)
        return str(policy_path)
