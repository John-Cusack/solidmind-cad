"""Tests for orchestrator.__main__ CLI."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestOrchestratorCLI(unittest.TestCase):
    def test_help(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator", "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("orchestrator", result.stdout)

    def test_no_args(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)

    def test_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            result = subprocess.run(
                [sys.executable, "-m", "orchestrator",
                 "build a gear", "-d", str(run_dir)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Initialized run", result.stdout)
            self.assertTrue((run_dir / "spec.yaml").exists())


if __name__ == "__main__":
    unittest.main()
