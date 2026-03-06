"""Tests for server.tools_rl — MCP tool dispatch with mocked subprocess."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.tools_rl import (
    rl_configure_environment,
    rl_deploy_policy,
    rl_monitor_training,
    rl_start_training,
    rl_stop_training,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HEXAPOD_URDF = _PROJECT_ROOT / "hexapod_sim_pkg" / "Hexapod_v2_1DOF.urdf"

_SAMPLE_JOINT_NAMES = ["coxa_lf", "femur_lf", "tibia_lf",
                        "coxa_rf", "femur_rf", "tibia_rf"]
_SAMPLE_ACTION_SCALES = [0.15, 0.3, 0.4, 0.15, 0.3, 0.4]


def _write_training_config(tmpdir: str, **overrides: object) -> None:
    """Write a training_config.json with joint_names in tmpdir."""
    data = {
        "pipeline": "isaaclab",
        "joint_names": _SAMPLE_JOINT_NAMES,
        "action_scale_per_joint": _SAMPLE_ACTION_SCALES,
        "default_joint_positions": [0.0] * len(_SAMPLE_JOINT_NAMES),
    }
    data.update(overrides)
    (Path(tmpdir) / "training_config.json").write_text(
        json.dumps(data), encoding="utf-8",
    )


class TestRLConfigureEnvironment(unittest.TestCase):
    """Test rl.configure_environment tool."""

    def test_hexapod_urdf(self) -> None:
        if not _HEXAPOD_URDF.is_file():
            self.skipTest("Hexapod URDF not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "env_config.py")
            result = rl_configure_environment(
                urdf_path=str(_HEXAPOD_URDF),
                output_path=out_path,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["config_path"], out_path)
            self.assertTrue(os.path.isfile(out_path))

            analysis = result["analysis"]
            self.assertEqual(analysis["robot_name"], "Hexapod_v2_1DOF")
            self.assertEqual(analysis["morphology"], "hexapod_1dof")
            self.assertEqual(analysis["num_joints"], 6)
            self.assertAlmostEqual(analysis["total_mass_kg"], 0.66, places=2)

            # Verify generated file is valid Python
            content = Path(out_path).read_text()
            compile(content, out_path, "exec")

    def test_missing_urdf(self) -> None:
        result = rl_configure_environment(urdf_path="/nonexistent.urdf")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "URDF_NOT_FOUND")


class TestRLStartTraining(unittest.TestCase):
    """Test rl.start_training tool."""

    def test_missing_config(self) -> None:
        result = rl_start_training(env_config="/nonexistent_config.py")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "CONFIG_NOT_FOUND")

    @patch("server.tools_rl.subprocess.Popen")
    def test_spawn_training(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"# dummy config")
            f.flush()
            try:
                result = rl_start_training(env_config=f.name)
            finally:
                os.unlink(f.name)

        self.assertTrue(result["ok"])
        self.assertIn("training_id", result)
        self.assertEqual(result["pid"], 12345)
        self.assertIn("output_dir", result)


class TestRLMonitorTraining(unittest.TestCase):
    """Test rl.monitor_training tool."""

    def test_unknown_training(self) -> None:
        result = rl_monitor_training(training_id="nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_TRAINING")


class TestRLStopTraining(unittest.TestCase):
    """Test rl.stop_training tool."""

    def test_unknown_training(self) -> None:
        result = rl_stop_training(training_id="nonexistent")
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_stopped"])


class TestRLDeployPolicy(unittest.TestCase):
    """Test rl.deploy_policy tool."""

    def test_missing_dir(self) -> None:
        result = rl_deploy_policy(checkpoint_dir="/nonexistent_dir")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "DIR_NOT_FOUND")

    def test_no_joint_names(self) -> None:
        """No training_config.json → JOINT_NAMES_NOT_FOUND."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = rl_deploy_policy(checkpoint_dir=tmpdir)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "JOINT_NAMES_NOT_FOUND")

    def test_deploy_reads_joint_names_from_training_config(self) -> None:
        """training_config.json has joint_names → they flow to deployment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_training_config(tmpdir)
            # Create a fake model.pt
            (Path(tmpdir) / "model.pt").write_bytes(b"fake_model_data")

            result = rl_deploy_policy(checkpoint_dir=tmpdir, alpha=0.5)
            self.assertTrue(result["ok"])
            self.assertEqual(result["joint_names"], _SAMPLE_JOINT_NAMES)
            self.assertAlmostEqual(result["alpha"], 0.5)

            # Check deployment files exist
            deployed_dir = Path(tmpdir) / "deployed"
            self.assertTrue((deployed_dir / "policy.pt").is_file())
            self.assertTrue((deployed_dir / "deployment_config.json").is_file())

    def test_deploy_returns_existing_artifacts(self) -> None:
        """When deployed/ already has policy.pt + config, skip re-export."""
        with tempfile.TemporaryDirectory() as tmpdir:
            deployed = Path(tmpdir) / "deployed"
            deployed.mkdir()
            (deployed / "policy.pt").write_bytes(b"existing_policy")
            config = {
                "joint_names": _SAMPLE_JOINT_NAMES,
                "action_scale_per_joint": _SAMPLE_ACTION_SCALES,
                "action_scale_mode": "per_joint",
                "alpha": 1.0,
            }
            (deployed / "deployment_config.json").write_text(
                json.dumps(config), encoding="utf-8",
            )

            result = rl_deploy_policy(checkpoint_dir=tmpdir)
            self.assertTrue(result["ok"])
            self.assertTrue(result.get("reused_existing"))
            self.assertEqual(result["joint_names"], _SAMPLE_JOINT_NAMES)
            self.assertEqual(
                result["action_scale_per_joint"], _SAMPLE_ACTION_SCALES,
            )

    def test_deploy_action_scale_from_training_config(self) -> None:
        """Per-joint action scales are averaged for scalar fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_training_config(tmpdir)
            (Path(tmpdir) / "model.pt").write_bytes(b"fake")

            result = rl_deploy_policy(checkpoint_dir=tmpdir)
            self.assertTrue(result["ok"])
            # export_policy receives the averaged scale
            expected_avg = sum(_SAMPLE_ACTION_SCALES) / len(_SAMPLE_ACTION_SCALES)
            self.assertAlmostEqual(result["action_scale"], expected_avg, places=4)


if __name__ == "__main__":
    unittest.main()
