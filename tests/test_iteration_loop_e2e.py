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

See `docs/ROADMAP.md` §"The loop, stage by stage" for the full gap
analysis and §"What 'autonomous on a part class' would mean" for the
three-test bar that has to be cleared before the loop is considered
closed on a given class of part.

## Intended shape of this test

```python
def test_under_dimensioned_hip_bracket_self_corrects(self) -> None:
    # 1. BUILD a deliberately under-dimensioned hip bracket
    bracket = build_hip_bracket(fillet_radius_mm=0.0)

    # 2. SIMULATE — run stress check, assert it fails
    result_before = analysis_stress_check(
        bracket, load_case=peak_walking_torque, material="al_6061"
    )
    self.assertEqual(result_before.status, CheckStatus.FAIL)
    self.assertLess(result_before.factor_of_safety, 1.5)

    # 3. DIAGNOSE — assert structured failure mode (not free text)
    self.assertEqual(
        result_before.failure_mode,
        FailureMode.STRESS_CONCENTRATION,  # see ROADMAP §Stage 4
    )
    self.assertTrue(result_before.candidates)  # at least one fix option

    # 4. DECIDE + ACT — have the orchestrator pick and apply a fix
    #    with NO human input between this call and the next assertion
    outcome = iterate_until_pass(
        bracket,
        analysis=analysis_stress_check,
        target_fos=1.5,
        max_iterations=5,
    )
    self.assertTrue(outcome.converged)
    self.assertLessEqual(outcome.iterations, 5)

    # 5. SIMULATE again — assert the loop actually improved the thing
    result_after = analysis_stress_check(
        outcome.final_geometry,
        load_case=peak_walking_torque,
        material="al_6061",
    )
    self.assertGreaterEqual(result_after.factor_of_safety, 1.5)

    # 6. LEARN — assert the finding was persisted for next session
    findings = knowledge_search(
        "hip_bracket stress concentration fillet",
        top_k=1,
    )
    self.assertTrue(findings)
```

## What has to land first

This test is blocked on three things, all captured in the roadmap:

1. **`FailureMode` enum on `AnalysisCheck`** (`server/analysis_models.py:191`)
   — needed so step 3 can dispatch on a typed value instead of a string.
2. **A `decide_from_failure` tool or equivalent orchestration helper**
   that turns a failing `AnalysisCheck` into a concrete `cad.*` call.
   Without it, `iterate_until_pass` has nowhere to live.
3. **A persistence test for `knowledge.*`** proving that ingested
   findings survive a fresh process. Without it, step 6 can pass
   accidentally and we'd be none the wiser.

Once those three are in place, unskip this test and wire it up against
a real under-dimensioned bracket geometry.
"""
from __future__ import annotations

import unittest


@unittest.skip(
    "Not implemented yet — structural placeholder. "
    "See docs/ROADMAP.md §'What autonomous on a part class would mean' "
    "for the three-test bar this is the first of."
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
