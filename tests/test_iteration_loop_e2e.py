"""End-to-end iteration loop closure test (placeholder).

This is a **skipped** structural TODO. It exists so that the missing
autonomous-loop test is visible in CI output instead of buried in docs.

The thesis behind SolidMind CAD is: with enough simulation in the loop,
an LLM can iterate on its own mechanical designs — build a part, watch
it break in physics, fix it, and repeat — until the thing works.

No test in this suite currently closes that loop. Everything under
`tests/test_project_*.py` and `tests/test_full_pipeline_e2e.py` walks
the pipeline forward in one pass; if simulation finds a problem, the
test logs it and moves on. That is adequate for tool validation but
does not demonstrate that the system can iterate.

See `docs/ROADMAP.md` §"The loop, step by step" for the full gap
analysis and §"What 'autonomous on a part class' would mean" for the
three-test bar that has to be cleared before the loop is considered
closed on a given class of part.

## The nine steps this test exercises

The loop has nine steps (see ROADMAP for textbook pedigree). This test
drives all nine in sequence on a single deliberately-bad hip bracket:

1. **Specify** — load the brief and target factor of safety
2. **Synthesize** — build an under-dimensioned bracket
3. **Reflect** — file pre-sim expectations (failure modes, expected stress ranges)
4. **Screen** — analytical first-pass (should flag the issue without FEA)
5. **Simulate** — FEA, because Screen flagged but wasn't definitive
6. **Interpret** — compare FEA result vs expectations, assert typed FailureMode
7. **Decide** — pick a fix from the candidate list
8. **Act** — apply the fix and re-check
9. **Learn** — assert the finding was persisted for next session

## Intended shape of this test

```python
def test_under_dimensioned_hip_bracket_self_corrects(self) -> None:
    # 1. SPECIFY — load the brief with its target FoS
    brief = design_save_brief(
        name="hexapod_hip_bracket",
        parameters={"target_fos": 1.5, "material": "al_6061"},
    )

    # 2. SYNTHESIZE — build a deliberately under-dimensioned bracket
    bracket = build_hip_bracket(fillet_radius_mm=0.0)

    # 3. REFLECT — file expectations before touching the solver
    expectations = ReflectExpectations(
        part_class="hexapod_hip_bracket",
        failure_modes_to_check=[
            FailureMode.STRESS_CONCENTRATION,
            FailureMode.YIELD,
            FailureMode.FATIGUE,
        ],
        expected_hotspot="fillet_at_servo_mount",
        expected_peak_stress_mpa=(40, 120),  # nominal × SCF range
    )

    # 4. SCREEN — analytical first-pass (no FEA yet)
    screen_result = screen_stress(bracket, expectations)
    self.assertEqual(screen_result.status, CheckStatus.FAIL)
    # Screen should catch the obviously-bad geometry without running FEA

    # 5. SIMULATE — only reached if Screen wasn't definitive
    result_before = analysis_stress_check(
        bracket,
        load_case=peak_walking_torque,
        material="al_6061",
        expectations=expectations,  # required, not optional
    )
    self.assertEqual(result_before.status, CheckStatus.FAIL)
    self.assertLess(result_before.factor_of_safety, 1.5)

    # 6. INTERPRET — assert typed failure mode (not free text)
    self.assertEqual(
        result_before.failure_mode,
        FailureMode.STRESS_CONCENTRATION,
    )
    comparison = interpret_compare_to_expectations(
        result_before, expectations
    )
    self.assertTrue(comparison.hotspot_matches_expectation)
    self.assertTrue(result_before.candidates)  # at least one fix option

    # 7 + 8. DECIDE + ACT — pick and apply a fix; loop until target
    #    No human input between steps 3 and the final assertion.
    outcome = iterate_until_pass(
        bracket,
        brief=brief,
        expectations=expectations,
        target_fos=1.5,
        max_iterations=5,
    )
    self.assertTrue(outcome.converged)
    self.assertLessEqual(outcome.iterations, 5)

    # Post-fix simulate — assert the loop actually improved the part
    result_after = analysis_stress_check(
        outcome.final_geometry,
        load_case=peak_walking_torque,
        material="al_6061",
        expectations=expectations,
    )
    self.assertGreaterEqual(result_after.factor_of_safety, 1.5)

    # 9. LEARN — assert the finding was persisted for next session
    findings = knowledge_search(
        "hip_bracket stress concentration fillet",
        top_k=1,
    )
    self.assertTrue(findings)
```

## What has to land first

This test is blocked on four things, all captured in the roadmap's
"highest-leverage first move — paired tools" section:

1. **`FailureMode` enum on `AnalysisCheck`** (`server/analysis_models.py:191`)
   — needed so the Interpret step can dispatch on a typed value
   instead of a free-form `name` string.
2. **`ReflectExpectations` dataclass + enforcement point** on the
   `analysis.*` tools — so the test can file expectations before
   running the solver and the Interpret step can compare result
   against expectation.
3. **A `decide.from_failure` tool or equivalent orchestration helper**
   that turns a failing `AnalysisCheck` into a concrete `cad.*` call.
   Without it, `iterate_until_pass` has nowhere to live.
4. **A persistence test for `knowledge.*`** proving that ingested
   findings survive a fresh process. Without it, step 9 (Learn) can
   pass accidentally and we'd be none the wiser.

Once those four are in place, unskip this test and wire it up against
a real under-dimensioned bracket geometry.
"""
from __future__ import annotations

import unittest


@unittest.skip(
    "Hexapod hip-bracket instance still needs FreeCAD geometry. The loop is "
    "now CLOSED for the foam-dart latch part class — see "
    "tests/test_iteration_loop_foam_dart_e2e.py, which walks all nine steps "
    "for real on the analytical Screen tier + Decide/Interpret."
)
class TestIterationLoopClosure(unittest.TestCase):
    """Placeholder for the end-to-end autonomous iteration test.

    When this unskips, it should exercise: build deliberately bad →
    simulate → diagnose (with a typed FailureMode) → decide a fix →
    apply the fix → re-simulate → assert the result improved — with no
    human input between the diagnose and re-simulate steps.
    """

    def test_under_dimensioned_hip_bracket_self_corrects(self) -> None:
        # Intentionally fails to force implementation when unskipped.
        raise NotImplementedError(
            "See module docstring and docs/ROADMAP.md for the intended "
            "assertion shape and the blocking dependencies."
        )


if __name__ == "__main__":
    unittest.main()
