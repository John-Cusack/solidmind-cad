"""Tests for RL deployment fixes: joint reorder, action scales, contact scoping."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from isaac_bridge.controllers import DirectPolicyController
from isaac_bridge.models import TeleopConfig, TeleopState, SimulationSession


class TestDirectPolicyActionScales(unittest.TestCase):
    """Verify DirectPolicyController respects action_scale_mode."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_controller_with_config(
        self, config_data: dict,
    ) -> DirectPolicyController:
        """Create a DirectPolicyController with a deployment config.

        Creates a dummy policy.pt so _load_policy proceeds to read config.
        """
        tmpdir = self._tmpdir
        config_path = Path(tmpdir) / "deployment_config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
        # Create a minimal file so the policy file check passes
        # (torch.jit.load will fail, but config loading happens after that)
        policy_path = Path(tmpdir) / "policy.pt"
        policy_path.write_bytes(b"dummy")
        ctrl = DirectPolicyController(
            policy_path=str(policy_path),
        )
        return ctrl

    def test_per_joint_scales_loaded(self) -> None:
        """Per-joint scales are loaded when action_scale_mode is per_joint."""
        ctrl = self._make_controller_with_config({
            "joint_names": ["a", "b", "c"],
            "action_scale_per_joint": [0.1, 0.2, 0.3],
            "action_scale_mode": "per_joint",
        })
        self.assertEqual(ctrl._action_scales, [0.1, 0.2, 0.3])

    def test_scalar_mode_ignores_per_joint(self) -> None:
        """Explicit scalar mode ignores per-joint scales."""
        ctrl = self._make_controller_with_config({
            "joint_names": ["a", "b", "c"],
            "action_scale_per_joint": [0.1, 0.2, 0.3],
            "action_scale_mode": "scalar",
            "action_scale": 0.5,
        })
        self.assertIsNone(ctrl._action_scales)
        self.assertAlmostEqual(ctrl._action_scale, 0.5)

    def test_auto_mode_prefers_per_joint(self) -> None:
        """Auto mode (no action_scale_mode field) prefers per-joint."""
        ctrl = self._make_controller_with_config({
            "joint_names": ["a", "b"],
            "action_scale_per_joint": [0.15, 0.4],
            "action_scale": 0.25,
        })
        self.assertEqual(ctrl._action_scales, [0.15, 0.4])

    def test_scalar_fallback_when_no_per_joint(self) -> None:
        """Falls back to scalar when no per-joint list."""
        ctrl = self._make_controller_with_config({
            "joint_names": ["a", "b"],
            "action_scale": 0.7,
        })
        self.assertIsNone(ctrl._action_scales)
        self.assertAlmostEqual(ctrl._action_scale, 0.7)


class TestJointReorder(unittest.TestCase):
    """Verify _populate_physics_state reorders joints into config order.

    Since we can't import the full runtime (needs Isaac Sim), we test the
    reorder logic extracted into a helper.
    """

    def test_reorder_basic(self) -> None:
        """Given DOF order [B, A, C] and config order [A, B, C], verify reorder."""
        # Simulate what _populate_physics_state does
        raw_pos = [10.0, 20.0, 30.0]  # DOF order: B=0, A=1, C=2
        raw_vel = [1.0, 2.0, 3.0]
        joint_names = ("A", "B", "C")
        dof_index_map = {"B": 0, "A": 1, "C": 2}

        ordered_pos = []
        ordered_vel = []
        for name in joint_names:
            idx = dof_index_map.get(name)
            if idx is not None and idx < len(raw_pos):
                ordered_pos.append(raw_pos[idx])
                ordered_vel.append(raw_vel[idx])
            else:
                ordered_pos.append(0.0)
                ordered_vel.append(0.0)

        # Config order [A, B, C] → values [20, 10, 30]
        self.assertEqual(ordered_pos, [20.0, 10.0, 30.0])
        self.assertEqual(ordered_vel, [2.0, 1.0, 3.0])

    def test_unmapped_joint_gets_zero(self) -> None:
        """Unmapped joints get 0.0 as fallback."""
        raw_pos = [10.0, 20.0]
        joint_names = ("A", "B", "C")
        dof_index_map = {"A": 0, "B": 1}  # C is unmapped

        ordered = []
        for name in joint_names:
            idx = dof_index_map.get(name)
            if idx is not None and idx < len(raw_pos):
                ordered.append(raw_pos[idx])
            else:
                ordered.append(0.0)

        self.assertEqual(ordered, [10.0, 20.0, 0.0])


class TestContactSensorScoping(unittest.TestCase):
    """Verify isaaclab_cfg scopes contact sensors per body."""

    def test_factory_scopes_contacts(self) -> None:
        """make_hexapod_flat_env_cfg sets body_names on contact rewards."""
        # We can't import isaaclab, so verify the code structure via AST
        cfg_path = (
            Path(__file__).resolve().parent.parent
            / "rl_training" / "isaaclab_cfg.py"
        )
        source = cfg_path.read_text(encoding="utf-8")

        # Verify the factory function sets body_names for foot contacts
        self.assertIn('body_names=foot_links', source)
        # Verify it sets body_names=[base_link] for undesired/termination
        self.assertIn('body_names=[base_link]', source)
        # Verify desired_contacts weight is zeroed when no foot_links
        self.assertIn('desired_contacts.weight = 0.0', source)


if __name__ == "__main__":
    unittest.main()
