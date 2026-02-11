from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class TestTranscripts(unittest.TestCase):
    def _run(self, relpath: str) -> None:
        p = Path(relpath)
        self.assertTrue(p.exists(), f"Missing transcript: {relpath}")
        subprocess.run([sys.executable, "scripts/replay_transcript.py", relpath], check=True)

    def test_cnc_L1(self) -> None:
        self._run("tests/transcripts/cnc_L1.yml")

    def test_cnc_L2(self) -> None:
        self._run("tests/transcripts/cnc_L2.yml")

    def test_cnc_L3(self) -> None:
        self._run("tests/transcripts/cnc_L3.yml")


if __name__ == "__main__":
    unittest.main()

