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

    def test_no_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = rl_deploy_policy(checkpoint_dir=tmpdir)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "CHECKPOINT_NOT_FOUND")

    def test_deploy_with_mock_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake model.pt
            model_path = Path(tmpdir) / "model.pt"
            model_path.write_bytes(b"fake_model_data")

            result = rl_deploy_policy(
                checkpoint_dir=tmpdir,
                alpha=0.5,
            )
            self.assertTrue(result["ok"])
            self.assertIn("policy_path", result)
            self.assertAlmostEqual(result["alpha"], 0.5)

            # Check deployment files exist
            deployed_dir = Path(tmpdir) / "deployed"
            self.assertTrue((deployed_dir / "policy.pt").is_file())
            self.assertTrue((deployed_dir / "normalization_params.json").is_file())
            self.assertTrue((deployed_dir / "deployment_config.json").is_file())


if __name__ == "__main__":
    unittest.main()
