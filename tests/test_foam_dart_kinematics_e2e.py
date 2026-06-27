"""Tests for the foam-dart launcher's kinematic Tier-2 rung.

The analytical baseline (moving clearance from brief specs, prismatic-travel
validation, binding from clearance) runs in CI without any backend. The
geometric FreeCAD confirmation is guarded behind ``freecad_ready()``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "foam_dart_spring_launcher"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

import run as launcher_run  # noqa: E402

from orchestrator.worker_builds import common  # noqa: E402

_BRIEF = json.loads((_EXAMPLE_DIR / "design_brief.json").read_text())


class TestKinematicAnalytical(unittest.TestCase):
    """Clearance + travel + binding computed from the real brief geometry."""

    def _run(self, *, smoke: bool) -> dict:
        log = launcher_run.StepLog(lines=[])
        with tempfile.TemporaryDirectory() as tmp:
            return launcher_run.kinematic_tier2(brief=_BRIEF, out=Path(tmp), smoke=smoke, log=log)

    def test_moving_clearance_from_specs(self) -> None:
        # guide bore 16.0 vs plunger head 15.2 -> 0.4 mm radial == min clearance.
        kin = self._run(smoke=True)
        cl = kin["clearance"]
        self.assertAlmostEqual(cl["value_mm"], 0.4, places=6)
        self.assertAlmostEqual(cl["target_mm"], 0.4, places=6)
        self.assertTrue(cl["pass"])

    def test_smoke_skips_travel_and_binding(self) -> None:
        kin = self._run(smoke=True)
        self.assertEqual(kin["travel"]["status"], "SKIPPED")
        self.assertEqual(kin["binding"]["status"], "SKIPPED")

    def test_real_mode_validates_travel_and_binding(self) -> None:
        # No FreeCAD needed: define_mechanism + validate are pure-Python, and the
        # geometric confirmation degrades to None when the addon is absent.
        kin = self._run(smoke=False)
        self.assertIn(kin["travel"]["status"], {"PASS", "SKIPPED"})
        self.assertEqual(kin["binding"]["status"], "PASS")  # clearance > 0
        self.assertTrue(kin["clearance"]["pass"])


class TestKinematicReportRows(unittest.TestCase):
    """The smoke report carries the kinematic table with no faked PASS rows."""

    def test_smoke_report_kinematic_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            rc = launcher_run.run(["--out", str(out), "--smoke"])
            self.assertEqual(rc, 0)
            report = (out / "validation_report.md").read_text()
        self.assertIn("Kinematic checks", report)
        self.assertIn("Plunger travel", report)
        self.assertIn("Moving clearance", report)
        # Travel/binding must read SKIPPED in smoke, not a fabricated PASS.
        kin_section = report.split("Kinematic checks")[1]
        self.assertIn("SKIPPED", kin_section)


@unittest.skipUnless(common.freecad_ready(), "needs a live FreeCAD addon")
class TestKinematicGeometric(unittest.TestCase):
    """Best-effort geometric interference confirmation when the addon is up."""

    def test_geometric_confirmation_runs_or_skips_cleanly(self) -> None:
        # Build the two parts, then attempt the assembly interference check.
        from orchestrator.worker_builds import foam_dart_launcher as fdl

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            (out / "step").mkdir(parents=True)
            (out / "stl").mkdir(parents=True)
            specs = {p["name"]: p.get("specs", {}) for p in _BRIEF["parts"]}
            fdl.build_all(out, specs=specs, log_fn=lambda _m: None)
            log = launcher_run.StepLog(lines=[])
            # Either a dict with a 'clear' verdict, or None (cleanly skipped).
            geom = launcher_run._geometric_interference(out, log)
            self.assertTrue(geom is None or "clear" in geom)


if __name__ == "__main__":
    unittest.main()
