"""End-to-end test for the foam-dart launcher example (smoke path).

Runs run.py's --smoke path (no solvers, CI-safe) and asserts the orchestration
produces a complete, correctly-shaped set of outputs and the V1→V2 improvement.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

_EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "foam_dart_spring_launcher"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

import run as launcher_run  # noqa: E402


class TestFoamDartSmokeE2E(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "run"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_smoke(self, *extra: str) -> None:
        rc = launcher_run.run(["--out", str(self.out), "--smoke", *extra])
        self.assertEqual(rc, 0)

    def test_smoke_produces_all_outputs(self) -> None:
        self._run_smoke()
        for name in (
            "validation_report.md",
            "range_prediction.csv",
            "motion_trace.csv",
            "bom.json",
        ):
            self.assertTrue((self.out / name).is_file(), f"missing {name}")
        for sub in ("launcher_v1", "launcher_v2", "step", "stl"):
            self.assertTrue((self.out / sub).is_dir(), f"missing dir {sub}")

    def test_report_has_required_sections_and_banner(self) -> None:
        self._run_smoke()
        report = (self.out / "validation_report.md").read_text()
        for section in (
            "Project summary",
            "Assumptions",
            "Sim-to-real chain",
            "Predicted ranges",
            "Inner-loop trace",
            "Structural checks",
            "V1 failure",
            "Print / test instructions",
        ):
            self.assertIn(section, report)
        self.assertIn("PHYSICS NOT VALIDATED", report)  # smoke banner

    def test_v1_fails_v2_passes_explicit_improvement(self) -> None:
        self._run_smoke()
        report = (self.out / "validation_report.md").read_text()
        # The structural table shows the latch FAIL → PASS with a peak-stress drop.
        self.assertIn("FAIL (peak", report)
        self.assertIn("PASS (peak", report)

    def test_range_csv_three_pullbacks_monotonic(self) -> None:
        self._run_smoke()
        with (self.out / "range_prediction.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["pullback_mm"] for r in rows], ["10.0", "20.0", "30.0"])
        ranges = [float(r["predicted_range_m"]) for r in rows]
        self.assertTrue(all(b > a for a, b in zip(ranges, ranges[1:], strict=False)))

    def test_calibration_fills_actual_and_error(self) -> None:
        self._run_smoke("--calibrate-from-shot", "20", "4.5")
        with (self.out / "range_prediction.csv").open() as f:
            rows = {r["pullback_mm"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows["20.0"]["actual_range_m"], "4.5")
        self.assertTrue(rows["20.0"]["rel_error"].endswith("%"))


if __name__ == "__main__":
    unittest.main()
