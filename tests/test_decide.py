"""TICKET-D: decide.from_failure + interpret_compare_to_expectations."""
from __future__ import annotations

import unittest

from server.analysis_models import (
    AnalysisCheck,
    CheckStatus,
    FailureMode,
    FieldResult,
    ReflectExpectations,
)
from server.decide import (
    FixProposal,
    from_failure,
    interpret_compare_to_expectations,
)
from server.tools_decide import decide_from_failure, decide_interpret


def _check(mode: FailureMode | None, face_group: str = "tooth_root") -> AnalysisCheck:
    return AnalysisCheck(
        name="latch FoS",
        status=CheckStatus.FAIL,
        message="below target",
        face_group=face_group,
        failure_mode=mode,
    )


class TestFromFailure(unittest.TestCase):
    def test_distinct_fix_per_mode(self) -> None:
        ops = {
            from_failure(_check(m)).op
            for m in (
                FailureMode.STRESS_CONCENTRATION,
                FailureMode.YIELD,
                FailureMode.BUCKLING,
            )
        }
        # Each mapped mode yields a recognisable, non-empty op; at least two distinct.
        self.assertGreaterEqual(len(ops), 2)

    def test_stress_concentration_adds_fillet(self) -> None:
        fix = from_failure(_check(FailureMode.STRESS_CONCENTRATION))
        self.assertEqual(fix.op, "add_fillet")
        self.assertEqual(fix.param, "radius_mm")
        self.assertGreater(fix.delta, 0)
        self.assertIn("tooth_root", fix.rationale)

    def test_yield_thickens_wall(self) -> None:
        fix = from_failure(_check(FailureMode.YIELD))
        self.assertEqual(fix.op, "thicken_wall")

    def test_no_mode_returns_none(self) -> None:
        self.assertIsNone(from_failure(_check(None)))

    def test_unmapped_mode_returns_review(self) -> None:
        fix = from_failure(_check(FailureMode.CORROSION))
        self.assertEqual(fix.op, "review")

    def test_fixproposal_round_trip(self) -> None:
        fix = from_failure(_check(FailureMode.YIELD))
        self.assertEqual(FixProposal.from_dict(fix.to_dict()), fix)


def _result(*, mode, max_vm, check_face="tooth_root") -> FieldResult:
    return FieldResult(
        analysis_id="a1",
        status=CheckStatus.FAIL,
        safety_factor=0.8,
        max_von_mises_mpa=max_vm,
        max_displacement_mm=0.2,
        checks=(AnalysisCheck(name="c", status=CheckStatus.FAIL,
                              message="m", face_group=check_face),),
        scalar_fields=(),
        failure_mode=mode,
    )


class TestInterpret(unittest.TestCase):
    def _exp(self) -> ReflectExpectations:
        return ReflectExpectations(
            part_class="latch",
            failure_modes_to_check=(FailureMode.STRESS_CONCENTRATION, FailureMode.YIELD),
            expected_hotspot="tooth_root",
            expected_peak_stress_mpa=(20.0, 60.0),
        )

    def test_hotspot_match_and_band_in(self) -> None:
        cmp = interpret_compare_to_expectations(
            _result(mode=FailureMode.STRESS_CONCENTRATION, max_vm=40.0), self._exp()
        )
        self.assertTrue(cmp.hotspot_matches_expectation)
        self.assertTrue(cmp.peak_within_expected_band)
        self.assertTrue(cmp.mode_was_expected)

    def test_hotspot_mismatch(self) -> None:
        cmp = interpret_compare_to_expectations(
            _result(mode=FailureMode.YIELD, max_vm=40.0, check_face="some_other_face"),
            self._exp(),
        )
        self.assertFalse(cmp.hotspot_matches_expectation)

    def test_peak_out_of_band(self) -> None:
        cmp = interpret_compare_to_expectations(
            _result(mode=FailureMode.YIELD, max_vm=300.0), self._exp()
        )
        self.assertFalse(cmp.peak_within_expected_band)

    def test_unexpected_mode_flagged(self) -> None:
        cmp = interpret_compare_to_expectations(
            _result(mode=FailureMode.BUCKLING, max_vm=40.0), self._exp()
        )
        self.assertFalse(cmp.mode_was_expected)


class TestDecideTools(unittest.TestCase):
    def test_tool_from_failure(self) -> None:
        out = decide_from_failure(check=_check(FailureMode.STRESS_CONCENTRATION).to_dict())
        self.assertTrue(out["ok"])
        self.assertEqual(out["proposal"]["op"], "add_fillet")

    def test_tool_from_failure_no_mode(self) -> None:
        out = decide_from_failure(check=_check(None).to_dict())
        self.assertTrue(out["ok"])
        self.assertIsNone(out["proposal"])

    def test_tool_interpret(self) -> None:
        exp = ReflectExpectations(
            part_class="latch",
            failure_modes_to_check=(FailureMode.YIELD,),
            expected_hotspot="tooth_root",
            expected_peak_stress_mpa=(20.0, 60.0),
        )
        out = decide_interpret(
            result=_result(mode=FailureMode.YIELD, max_vm=40.0).to_dict(),
            expectations=exp.to_dict(),
        )
        self.assertTrue(out["ok"])
        self.assertTrue(out["hotspot_matches_expectation"])
        self.assertTrue(out["mode_was_expected"])


if __name__ == "__main__":
    unittest.main()
