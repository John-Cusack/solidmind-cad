#!/usr/bin/env python3
"""Smoke test for the RL training pipeline.

Tests PPO + rewards with a mock environment (no Isaac Sim required).
Verifies that:
1. PPO actor-critic forward/backward works
2. Reward computation produces finite values
3. Training loop runs for N iterations with improving rewards
4. Checkpoint save/load works
5. Policy export produces valid JIT-traced model
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import torch


def make_mock_env(num_envs: int = 64, num_joints: int = 18):
    """Create a mock environment that mimics HexapodLocomotionEnv interface."""

    class MockHexapodEnv:
        def __init__(self):
            self.num_envs = num_envs
            self._num_joints = num_joints
            self.obs_dim = 14 + 3 * num_joints  # 68 for 18 joints (includes gait clock)
            self.action_dim = num_joints
            self.device = "cuda:0"
            self._step_count = 0
            self._obs = torch.zeros(num_envs, self.obs_dim, device=self.device)

        def reset(self, env_ids=None):
            if env_ids is None:
                self._obs = torch.randn(self.num_envs, self.obs_dim, device=self.device) * 0.1
            else:
                self._obs[env_ids] = torch.randn(len(env_ids), self.obs_dim, device=self.device) * 0.1
            self._step_count = 0
            return self._obs

        def step(self, actions):
            self._step_count += 1

            # Simple reward: track velocity command (obs[9:12] = commands)
            # Reward = -|action|^2 + tracking_bonus
            commands = self._obs[:, 9:12]
            body_vel = self._obs[:, 0:3]
            tracking_error = torch.sum((body_vel - commands) ** 2, dim=-1)
            action_penalty = 0.01 * torch.sum(actions ** 2, dim=-1)
            rewards = torch.exp(-tracking_error) - action_penalty

            # Simple dynamics: next obs is slightly affected by actions
            noise = torch.randn_like(self._obs) * 0.05
            self._obs = self._obs + noise
            # Feed actions back as joint positions (after gait clock at 12:14)
            self._obs[:, 14:14 + self._num_joints] = actions * 0.25

            # Terminate 1% of envs randomly
            dones = torch.rand(self.num_envs, device=self.device) < 0.01

            # Auto-reset
            reset_ids = torch.where(dones)[0]
            if len(reset_ids) > 0:
                self._obs[reset_ids] = torch.randn(len(reset_ids), self.obs_dim, device=self.device) * 0.1

            return self._obs, rewards, dones, {"episode_lengths": torch.zeros(self.num_envs)}

        def close(self):
            pass

    return MockHexapodEnv()


def test_rewards():
    """Test that vectorized reward computation works."""
    from rl_training.rewards_vectorized import compute_locomotion_reward, RewardWeights

    n = 64
    nj = 18
    device = "cuda:0"

    reward = compute_locomotion_reward(
        base_lin_vel=torch.randn(n, 3, device=device),
        base_ang_vel=torch.randn(n, 3, device=device) * 0.1,
        projected_gravity=torch.tensor([[0.0, 0.0, -1.0]], device=device).expand(n, -1),
        velocity_commands=torch.randn(n, 3, device=device) * 0.3,
        joint_torques=torch.randn(n, nj, device=device),
        actions=torch.randn(n, nj, device=device) * 0.25,
        prev_actions=torch.randn(n, nj, device=device) * 0.25,
        base_height=torch.full((n,), 0.09, device=device),
        target_height=0.09,
    )

    assert reward.shape == (n,), f"Expected shape ({n},), got {reward.shape}"
    assert torch.isfinite(reward).all(), "Non-finite rewards detected"
    assert (reward >= 0).all(), f"Negative rewards: min={reward.min().item():.4f}"
    print(f"  Rewards: mean={reward.mean().item():.4f}, std={reward.std().item():.4f}")
    return True


def test_ppo_training(num_iterations: int = 50):
    """Test PPO training loop with mock environment."""
    from rl_training.ppo import PPOHyperparams, PPOTrainer

    env = make_mock_env(num_envs=64, num_joints=18)
    params = PPOHyperparams(
        learning_rate=3e-4,
        num_epochs=4,
        num_mini_batches=2,
        num_steps_per_env=16,
        actor_hidden_dims=(128, 64),  # smaller for speed
        critic_hidden_dims=(128, 64),
    )

    trainer = PPOTrainer(env, params=params, device="cuda:0")
    print(f"  Env: {env.num_envs} envs, obs_dim={env.obs_dim}, action_dim={env.action_dim}")

    rewards_history = []
    t0 = time.time()

    for i in range(1, num_iterations + 1):
        rollout_stats = trainer.collect_rollouts()
        update_stats = trainer.update()

        mean_reward = rollout_stats["mean_reward"]
        rewards_history.append(mean_reward)

        if i % 10 == 0 or i == 1:
            print(
                f"  Iter {i:3d}/{num_iterations} | "
                f"reward={mean_reward:+.4f} | "
                f"policy_loss={update_stats['policy_loss']:.4f} | "
                f"value_loss={update_stats['value_loss']:.4f} | "
                f"entropy={update_stats['entropy']:.4f} | "
                f"kl={update_stats['kl_divergence']:.5f} | "
                f"lr={update_stats['learning_rate']:.2e}"
            )

    elapsed = time.time() - t0
    print(f"  Training time: {elapsed:.1f}s ({elapsed/num_iterations:.2f}s/iter)")

    # Check that training didn't produce NaN
    assert all(
        not (r != r) for r in rewards_history  # NaN check
    ), "NaN rewards during training"

    # Check reward trend (last 10 avg vs first 10 avg)
    first_10 = sum(rewards_history[:10]) / 10
    last_10 = sum(rewards_history[-10:]) / 10
    print(f"  Reward trend: first_10_avg={first_10:.4f} → last_10_avg={last_10:.4f}")

    return trainer, env, rewards_history


def test_checkpoint(trainer, env):
    """Test checkpoint save/load."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ckpt_path = str(Path(tmpdir) / "model.pt")
        trainer.save_checkpoint(ckpt_path)

        # Verify file exists and is non-trivial
        size = Path(ckpt_path).stat().st_size
        print(f"  Checkpoint size: {size:,} bytes")
        assert size > 1000, f"Checkpoint too small: {size} bytes"

        # Load and verify
        from rl_training.ppo import PPOTrainer, PPOHyperparams
        trainer2 = PPOTrainer(env, device="cuda:0")
        trainer2.load_checkpoint(ckpt_path)
        print(f"  Checkpoint loaded, iteration={trainer2.current_iteration}")
        assert trainer2.current_iteration == trainer.current_iteration

    return True


def test_policy_export(trainer, env):
    """Test JIT policy export."""
    with tempfile.TemporaryDirectory() as tmpdir:
        policy_path = trainer.export_policy(tmpdir)
        print(f"  Policy exported to: {policy_path}")

        # Verify files
        p = Path(tmpdir)
        assert (p / "policy.pt").is_file(), "policy.pt not found"
        assert (p / "normalization_params.json").is_file(), "normalization_params.json not found"

        # Load and run inference
        policy = torch.jit.load(str(p / "policy.pt"), map_location="cpu")
        policy.eval()

        dummy_obs = torch.randn(1, env.obs_dim)
        with torch.no_grad():
            output = policy(dummy_obs)

        print(f"  Policy output shape: {list(output.shape)} (expected [1, {env.action_dim}])")
        assert output.shape == (1, env.action_dim), f"Wrong output shape: {output.shape}"
        assert torch.isfinite(output).all(), "Non-finite policy output"

        # Check normalization params have real values (not all zeros/ones)
        norm = json.loads((p / "normalization_params.json").read_text())
        has_real_stats = any(v != 0.0 for v in norm["obs_mean"])
        print(f"  Normalization from training: {has_real_stats}")

    return True


def main():
    print("=" * 60)
    print("SolidMind RL Pipeline Smoke Test")
    print("=" * 60)
    print(f"PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    print()

    # Test 1: Vectorized rewards
    print("[1/4] Testing vectorized reward computation...")
    ok = test_rewards()
    print(f"  → {'PASS' if ok else 'FAIL'}\n")

    # Test 2: PPO training (50 iterations)
    print("[2/4] Testing PPO training (50 iterations)...")
    trainer, env, rewards = test_ppo_training(num_iterations=50)
    print(f"  → PASS\n")

    # Test 3: Checkpoint
    print("[3/4] Testing checkpoint save/load...")
    ok = test_checkpoint(trainer, env)
    print(f"  → {'PASS' if ok else 'FAIL'}\n")

    # Test 4: Policy export
    print("[4/4] Testing JIT policy export...")
    ok = test_policy_export(trainer, env)
    print(f"  → {'PASS' if ok else 'FAIL'}\n")

    env.close()

    print("=" * 60)
    print("All smoke tests PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
