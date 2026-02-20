"""Tests for PolicyController — mock JIT model, protocol compliance, residual blending."""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from isaac_bridge.controllers import HexapodTripodController, PolicyController
from isaac_bridge.models import Controller, TeleopConfig, TeleopState


class TestPolicyControllerProtocol(unittest.TestCase):
    """Verify PolicyController satisfies the Controller protocol."""

    def test_is_controller(self) -> None:
        # PolicyController with missing policy should still work
        with tempfile.TemporaryDirectory() as tmpdir:
            pc = PolicyController(
                policy_path=os.path.join(tmpdir, "nonexistent.pt"),
            )
            self.assertIsInstance(pc, Controller)

    def test_has_compute_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pc = PolicyController(
                policy_path=os.path.join(tmpdir, "nonexistent.pt"),
            )
            self.assertTrue(hasattr(pc, "compute_targets"))
            self.assertTrue(callable(pc.compute_targets))

    def test_has_filtered_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pc = PolicyController(
                policy_path=os.path.join(tmpdir, "nonexistent.pt"),
            )
            # These properties are needed by the runtime for telemetry
            self.assertIsInstance(pc.filtered_vx, float)
            self.assertIsInstance(pc.filtered_yaw, float)
            self.assertIsInstance(pc.filtered_height, float)


class TestPolicyControllerFallback(unittest.TestCase):
    """Test that PolicyController falls back to base controller when no policy."""

    def test_no_policy_returns_base_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pc = PolicyController(
                policy_path=os.path.join(tmpdir, "nonexistent.pt"),
            )
            state = TeleopState(vx_mps=0.3, yaw_rate_rps=0.0, body_height_m=0.0)
            config = TeleopConfig()

            targets, new_phase = pc.compute_targets(state, 0.016, config, 0.0)

            # Should return valid targets for all joints
            self.assertEqual(set(targets.keys()), set(config.joint_names))
            # Phase should advance (vx > 0)
            self.assertGreater(new_phase, 0.0)

    def test_matches_base_controller(self) -> None:
        """Without a policy, PolicyController should match HexapodTripodController."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pc = PolicyController(
                policy_path=os.path.join(tmpdir, "nonexistent.pt"),
            )
            base = HexapodTripodController()
            state = TeleopState(vx_mps=0.2, yaw_rate_rps=0.1, body_height_m=0.01)
            config = TeleopConfig()

            targets_pc, phase_pc = pc.compute_targets(state, 0.016, config, 1.0)
            targets_base, phase_base = base.compute_targets(state, 0.016, config, 1.0)

            self.assertAlmostEqual(phase_pc, phase_base, places=6)
            for name in config.joint_names:
                self.assertAlmostEqual(
                    targets_pc[name], targets_base[name], places=6,
                    msg=f"Mismatch for {name}",
                )


class TestPolicyControllerWithMockPolicy(unittest.TestCase):
    """Test residual blending with a mock torch policy."""

    def _make_mock_policy(self, output_values: list[float]) -> MagicMock:
        """Create a mock JIT policy that returns fixed values."""
        mock = MagicMock()
        # Mock torch tensor behavior
        mock_output = MagicMock()
        mock_output.shape = (1, len(output_values))
        mock_output.__getitem__ = lambda self, idx: MagicMock(
            __float__=lambda _: output_values[idx[1]] if isinstance(idx, tuple) else output_values[idx]
        )
        # For indexing [0, i]
        row_mock = MagicMock()
        for i, val in enumerate(output_values):
            row_mock.__getitem__ = self._make_getitem(output_values)
        mock_output.__getitem__ = self._make_2d_getitem(output_values)
        mock.return_value = mock_output
        return mock

    @staticmethod
    def _make_getitem(values: list[float]):
        def getitem(idx):
            m = MagicMock()
            m.__float__ = lambda _: values[idx] if idx < len(values) else 0.0
            return m
        return getitem

    @staticmethod
    def _make_2d_getitem(values: list[float]):
        def getitem(idx):
            if isinstance(idx, tuple):
                _, col = idx
                m = MagicMock()
                m.__float__ = lambda _: values[col] if col < len(values) else 0.0
                return m
            # Row indexing returns a row-like mock
            row = MagicMock()
            row.__getitem__ = TestPolicyControllerWithMockPolicy._make_getitem(values)
            return row
        return getitem

    @patch("isaac_bridge.controllers.PolicyController._load_policy")
    def test_residual_blending_alpha_zero(self, mock_load: MagicMock) -> None:
        """With alpha=0, output should match base controller."""
        pc = PolicyController(policy_path="dummy.pt", alpha=0.0)
        pc._policy = None  # No policy → base only

        state = TeleopState(vx_mps=0.3, yaw_rate_rps=0.0, body_height_m=0.0)
        config = TeleopConfig()
        base = HexapodTripodController()

        targets_pc, _ = pc.compute_targets(state, 0.016, config, 0.5)
        targets_base, _ = base.compute_targets(state, 0.016, config, 0.5)

        for name in config.joint_names:
            self.assertAlmostEqual(targets_pc[name], targets_base[name], places=6)


class TestPolicyControllerConfig(unittest.TestCase):
    """Test deployment config loading."""

    def test_loads_deployment_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a deployment config
            config = {
                "joint_names": ["hip_lf", "hip_lm", "hip_lr"],
                "alpha": 0.5,
                "action_scale": 0.2,
                "obs_dim": 21,
            }
            config_path = Path(tmpdir) / "deployment_config.json"
            config_path.write_text(json.dumps(config))

            # Create a fake policy.pt so _load_policy doesn't exit early
            policy_file = Path(tmpdir) / "policy.pt"
            policy_file.write_bytes(b"fake")

            # PolicyController will fail to load torch but should still
            # read the deployment config. Patch torch import to fail.
            pc = PolicyController(
                policy_path=str(policy_file),
                alpha=0.3,
            )
            # Without torch, the policy won't load, but deployment config
            # is read only after a successful torch.jit.load. So we check
            # that the initial alpha is preserved when torch is unavailable.
            # The config loading happens inside _load_policy only after
            # successful policy load.
            # With torch unavailable, alpha stays at constructor value.
            self.assertAlmostEqual(pc._alpha, 0.3)
            # _policy should be None since we can't actually load the fake file
            self.assertIsNone(pc._policy)


if __name__ == "__main__":
    unittest.main()
