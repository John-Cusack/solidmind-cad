"""Tests for TICKET-A additions to server.analysis_models.

Covers the FailureMode enum, the ReflectExpectations dataclass, and the new
result fields (failure_mode / candidates / factor_of_safety) that the
iteration-loop test depends on.
"""

from __future__ import annotations

import dataclasses
import unittest

from server.analysis_models import (
    AnalysisCheck,
    CheckStatus,
    FailureMode,
    FieldResult,
    ReflectExpectations,
)


class TestFailureMode(unittest.TestCase):
    def test_taxonomy_values_present(self) -> None:
        names = {m.name for m in FailureMode}
        for expected in (
            "STRESS_CONCENTRATION",
            "YIELD",
            "FATIGUE",
            "BUCKLING",
            "CONTACT",
            "DEFLECTION",
            "RESONANCE",
            "THERMAL",
            "WEAR",
            "CORROSION",
        ):
            self.assertIn(expected, names)

    def test_is_str_enum(self) -> None:
        # str, Enum → compares/serialises as its value
        self.assertEqual(FailureMode.YIELD, "yield")
        self.assertEqual(FailureMode("stress_concentration"), FailureMode.STRESS_CONCENTRATION)


class TestReflectExpectations(unittest.TestCase):
    def _sample(self) -> ReflectExpectations:
        return ReflectExpectations(
            part_class="foam_dart_latch",
            failure_modes_to_check=(
                FailureMode.STRESS_CONCENTRATION,
                FailureMode.YIELD,
            ),
            expected_hotspot="latch_tooth_root",
            expected_peak_stress_mpa=(20.0, 60.0),
        )

    def test_frozen_and_hashable(self) -> None:
        exp = self._sample()
        self.assertEqual(len({exp, exp}), 1)  # hashable
        with self.assertRaises(dataclasses.FrozenInstanceError):
            exp.part_class = "other"  # type: ignore[misc]

    def test_dict_round_trip(self) -> None:
        exp = self._sample()
        restored = ReflectExpectations.from_dict(exp.to_dict())
        self.assertEqual(restored, exp)
        self.assertEqual(
            exp.to_dict()["failure_modes_to_check"],
            ["stress_concentration", "yield"],
        )


class TestResultFieldExtensions(unittest.TestCase):
    def test_factor_of_safety_aliases_safety_factor(self) -> None:
        r = FieldResult(
            analysis_id="a1",
            status=CheckStatus.PASS,
            safety_factor=1.2,
            max_von_mises_mpa=100.0,
            max_displacement_mm=0.5,
            checks=(),
            scalar_fields=(),
        )
        self.assertEqual(r.factor_of_safety, 1.2)

    def test_check_failure_mode_round_trip(self) -> None:
        c = AnalysisCheck(
            name="latch FoS",
            status=CheckStatus.FAIL,
            message="below target",
            failure_mode=FailureMode.STRESS_CONCENTRATION,
        )
        restored = AnalysisCheck.from_dict(c.to_dict())
        self.assertEqual(restored.failure_mode, FailureMode.STRESS_CONCENTRATION)

    def test_check_failure_mode_optional(self) -> None:
        c = AnalysisCheck(name="x", status=CheckStatus.PASS, message="ok")
        self.assertIsNone(c.failure_mode)
        self.assertIsNone(c.to_dict()["failure_mode"])
        self.assertIsNone(AnalysisCheck.from_dict(c.to_dict()).failure_mode)

    def test_result_failure_mode_and_candidates_round_trip(self) -> None:
        r = FieldResult(
            analysis_id="a2",
            status=CheckStatus.FAIL,
            safety_factor=0.8,
            max_von_mises_mpa=300.0,
            max_displacement_mm=1.0,
            checks=(),
            scalar_fields=(),
            failure_mode=FailureMode.YIELD,
            candidates=("thicken_wall", "add_fillet"),
        )
        restored = FieldResult.from_dict(r.to_dict())
        self.assertEqual(restored.failure_mode, FailureMode.YIELD)
        self.assertEqual(restored.candidates, ("thicken_wall", "add_fillet"))

    def test_result_defaults_backward_compatible(self) -> None:
        # Old-shaped dict (no failure_mode/candidates) still parses.
        old = {
            "analysis_id": "a3",
            "status": "pass",
            "safety_factor": 2.5,
            "max_von_mises_mpa": 50.0,
            "max_displacement_mm": 0.1,
            "checks": [],
            "scalar_fields": [],
        }
        r = FieldResult.from_dict(old)
        self.assertIsNone(r.failure_mode)
        self.assertEqual(r.candidates, ())


if __name__ == "__main__":
    unittest.main()
