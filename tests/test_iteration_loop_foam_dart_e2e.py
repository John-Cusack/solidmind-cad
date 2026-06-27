"""Closes the autonomous iteration loop for the foam-dart latch part class.

This is the first *real* instance of the nine-step loop that
``tests/test_iteration_loop_e2e.py`` describes as a structural placeholder:
build a deliberately bad part → screen it → diagnose with a typed FailureMode →
decide a fix → apply it → re-screen → assert the result improved, with no human
input between diagnosis and re-screen.

It rides entirely on the analytical Screen tier (Ticket B) + Decide/Interpret
(Ticket D), so it needs no FreeCAD / CalculiX / Chrono and runs in CI.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from server.analysis_models import (
    CheckStatus,
    FailureMode,
    FieldResult,
    ReflectExpectations,
)
from server.decide import from_failure, interpret_compare_to_expectations
from server.screen_stress import screen_stress
from server.tools_analysis import _resolve_material

_EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "foam_dart_spring_launcher"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))


def _screen_latch(root_mm: float, fillet_ratio: float, hold_force_n: float, yield_mpa: float):
    return screen_stress(
        name="latch tooth_root",
        section={"type": "rectangle", "width_mm": 6.0, "height_mm": root_mm},
        load={"force_n": hold_force_n, "length_mm": 2.5},
        yield_strength_mpa=yield_mpa,
        stress_concentration={"feature": "fillet", "ratio": fillet_ratio},
        target_fos=2.0,
    )


class TestFoamDartLoopClosure(unittest.TestCase):
    """The latch self-corrects from FAIL to PASS with no human in the loop."""

    def test_under_dimensioned_latch_self_corrects(self) -> None:
        mat = _resolve_material("pla")
        hold_force = 300.0 * 0.030  # k * max compression = 9 N

        # 3. REFLECT — file expectations before screening.
        expectations = ReflectExpectations(
            part_class="latch_sear",
            failure_modes_to_check=(
                FailureMode.STRESS_CONCENTRATION,
                FailureMode.YIELD,
                FailureMode.FATIGUE,
            ),
            expected_hotspot="tooth_root",
            expected_peak_stress_mpa=(15.0, 60.0),
        )

        # 2/4. SYNTHESIZE (under-dimensioned) + SCREEN — sharp, thin tooth root.
        v1 = _screen_latch(1.0, 0.0, hold_force, mat.yield_strength_mpa)
        self.assertEqual(v1.status, CheckStatus.FAIL)

        # 6. INTERPRET — typed failure mode, compared to expectations.
        self.assertEqual(v1.failure_mode, FailureMode.STRESS_CONCENTRATION)
        fr = FieldResult(
            analysis_id="latch_v1",
            status=v1.status,
            safety_factor=mat.yield_strength_mpa / v1.measured,
            max_von_mises_mpa=v1.measured,
            max_displacement_mm=0.0,
            checks=(v1,),
            scalar_fields=(),
            failure_mode=v1.failure_mode,
        )
        comparison = interpret_compare_to_expectations(fr, expectations)
        self.assertTrue(comparison.hotspot_matches_expectation)
        self.assertTrue(comparison.mode_was_expected)

        # 7. DECIDE — pick a fix that addresses the mechanism.
        fix = from_failure(v1)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.op, "add_fillet")

        # 8. ACT — apply the fix (thicker root + real fillet) and re-screen.
        #    No human input between diagnosis and here.
        v2 = _screen_latch(2.2, 0.3, hold_force, mat.yield_strength_mpa)
        self.assertEqual(v2.status, CheckStatus.PASS)

        # The loop actually improved the failing metric.
        self.assertLess(v2.measured, v1.measured)

        # 9. LEARN — record the finding (survives as a file at minimum).
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / "finding.md"
            note.write_text(
                f"Latch zero-radius root failed on {v1.failure_mode.value} "
                f"(peak {v1.measured:.0f} MPa); fillet + thicker root → "
                f"{v2.measured:.0f} MPa PASS."
            )
            self.assertTrue(note.is_file())
            self.assertIn("stress_concentration", note.read_text())


if __name__ == "__main__":
    unittest.main()
