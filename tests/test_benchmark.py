"""Tests for the seeded-defect benchmark — the decision gate.

These tests check the *harness*, not the thesis: that every defect class is
seeded so it actually manifests, that the answer key scores actions the way
Addendum A.2 specifies, that the ablated term is genuinely hidden, and that
the deterministic checklist produces an honest baseline (no false alarms on
controls, and blind to the classes that need open-world knowledge).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server import prescribe
from server.benchmark import (
    ANSWER_KEY,
    ConfusionMatrix,
    DefectClass,
    build_problems,
    checklist_arm,
    run_benchmark,
    run_inner_loop,
    score_action,
)
from server.diagnose import diagnose
from server.model_registry import load_chain
from server.prescribe import ActionType, PatchOp, PatchTarget
from server.study_models import StudyStatus


class BenchmarkBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "artifacts"
        cls.problems = build_problems(root=cls.root)
        cls.by_id = {p.problem_id: p for p in cls.problems}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


class TestProblemSet(BenchmarkBase):
    def test_every_defect_class_and_both_domains_present(self) -> None:
        classes = {p.defect for p in self.problems}
        self.assertEqual(classes, set(DefectClass))
        self.assertEqual({p.domain for p in self.problems}, {"mechanical", "acoustic"})

    def test_controls_exist_in_both_domains(self) -> None:
        controls = [p for p in self.problems if p.defect is DefectClass.CONTROL]
        self.assertEqual({p.domain for p in controls}, {"mechanical", "acoustic"})

    def test_answer_key_covers_every_class(self) -> None:
        self.assertEqual(set(ANSWER_KEY), set(DefectClass))


class TestDefectsActuallyManifest(BenchmarkBase):
    """A seeded defect that does not show up in the results is not a defect."""

    def diagnose_problem(self, problem_id: str):
        problem = self.by_id[problem_id]
        results = run_inner_loop(problem, root=self.root)
        return problem, results, diagnose(results, problem.study)

    def test_control_runs_clean(self) -> None:
        problem, _results, d = self.diagnose_problem("latch_control")
        self.assertEqual(problem.study.status, StudyStatus.COMPLETE)
        actionable = d.codes - {"uncalibrated_model_in_optimum", "sensitivity_concentrated"}
        self.assertEqual(actionable, set(), f"control should look clean, got {d.codes}")

    def test_missing_variable_shows_a_flat_objective(self) -> None:
        _p, _r, d = self.diagnose_problem("latch_missing_variable")
        self.assertTrue(d.has("objective_flat"))
        self.assertTrue(d.has("unchanged_fingerprint"))

    def test_topology_defect_is_infeasible_at_every_aperture(self) -> None:
        _p, _r, d = self.diagnose_problem("mesh_topology")
        self.assertTrue(d.has("no_feasible_variants"))

    def test_missing_mechanism_drives_the_optimum_to_the_bound(self) -> None:
        problem, _r, d = self.diagnose_problem("mesh_missing_mechanism")
        self.assertTrue(d.has("optimum_at_bound"))
        best = next(
            v
            for v in problem.study.coarse_variants + problem.study.refined_variants
            if v.variant_id == problem.study.best_variant_id
        )
        self.assertAlmostEqual(best.params["aperture_m"], 6.0)

    def test_full_model_finds_an_interior_optimum(self) -> None:
        problem, _r, _d = self.diagnose_problem("mesh_control")
        best = next(
            v
            for v in problem.study.coarse_variants + problem.study.refined_variants
            if v.variant_id == problem.study.best_variant_id
        )
        self.assertLess(best.params["aperture_m"], 6.0)
        self.assertGreater(best.params["aperture_m"], 0.5)

    def test_wrong_objective_optimum_is_undetectable_in_truth(self) -> None:
        # Optimizing the build-cost proxy alone picks the cheapest array,
        # which cannot actually produce a usable fix — that is what makes the
        # objective wrong rather than merely imperfect.
        problem, _r, _d = self.diagnose_problem("mesh_wrong_objective")
        best = next(
            v
            for v in problem.study.coarse_variants + problem.study.refined_variants
            if v.variant_id == problem.study.best_variant_id
        )
        self.assertEqual(best.metrics["detected"], 0.0)
        self.assertLess(best.metrics["position_margin"], 1.0)

    def test_minimizing_bearing_sigma_would_not_be_a_defect(self) -> None:
        # Guards the seeding rationale: with fixed geometry, bearing sigma is
        # a monotone transform of position error, so it is NOT a wrong
        # objective — using it would have seeded a non-defect.
        problem = self.by_id["mesh_control"]
        run_inner_loop(problem, root=self.root)
        done = [
            v
            for v in problem.study.coarse_variants + problem.study.refined_variants
            if v.status == "done"
        ]
        by_sigma = min(done, key=lambda v: v.metrics["bearing_sigma_deg"])
        by_position = min(done, key=lambda v: v.metrics["position_rms_m"])
        self.assertEqual(by_sigma.params["aperture_m"], by_position.params["aperture_m"])


class TestHiddenRegistry(BenchmarkBase):
    def test_ablated_term_is_withheld_from_the_arms(self) -> None:
        problem = self.by_id["mesh_missing_mechanism"]
        visible = problem.visible_catalog()
        self.assertNotIn("coherence_loss", visible["analytic_acoustic_bearing"])
        # Still present for stages the defect does not touch.
        self.assertIn("stress_concentration", visible["analytic_latch"])

    def test_ablation_is_real_in_the_chain(self) -> None:
        problem = self.by_id["mesh_missing_mechanism"]
        chain = load_chain(problem.models_revision, root=self.root)
        self.assertNotIn("coherence_loss", chain.terms_for("analytic_acoustic_bearing"))

    def test_controls_see_the_full_catalog(self) -> None:
        visible = self.by_id["mesh_control"].visible_catalog()
        self.assertIn("coherence_loss", visible["analytic_acoustic_bearing"])


class TestScoring(BenchmarkBase):
    def test_answer_key_accepts_the_specified_actions(self) -> None:
        control = self.by_id["latch_control"]
        self.assertTrue(score_action(control, prescribe.no_action()))
        self.assertFalse(score_action(control, prescribe.escalate("something_feels_off")))

        mech = self.by_id["mesh_missing_mechanism"]
        self.assertTrue(
            score_action(
                mech,
                prescribe.patch(
                    PatchOp(
                        target=PatchTarget.MODEL_REGISTRY,
                        path="analytic_acoustic_bearing:coherence_loss",
                        op="add",
                    )
                ),
            )
        )
        # request_measurement is also accepted for this class...
        self.assertTrue(score_action(mech, prescribe.request_measurement({"what": "coherence"})))
        # ...but widening a bound is not.
        self.assertFalse(
            score_action(
                mech,
                prescribe.patch(PatchOp(target=PatchTarget.STUDY_BOUNDS, path="aperture_m")),
            )
        )

    def test_wrong_objective_accepts_patch_or_escalate(self) -> None:
        problem = self.by_id["mesh_wrong_objective"]
        self.assertTrue(score_action(problem, prescribe.escalate("objective_mismeasures")))
        self.assertTrue(
            score_action(
                problem,
                prescribe.patch(
                    PatchOp(
                        target=PatchTarget.STUDY_OBJECTIVES,
                        path="primary_metric",
                        value="position_margin",
                    ),
                    approved_by="john",
                ),
            )
        )
        self.assertFalse(score_action(problem, prescribe.no_action()))

    def test_confusion_matrix_separates_controls(self) -> None:
        matrix = ConfusionMatrix(
            true_positive=3, false_negative=1, wrong_action=1, true_negative=4, false_positive=1
        )
        self.assertAlmostEqual(matrix.hit_rate, 0.6)
        self.assertAlmostEqual(matrix.false_alarm_rate, 0.2)

    def test_empty_matrix_does_not_divide_by_zero(self) -> None:
        matrix = ConfusionMatrix()
        self.assertEqual(matrix.hit_rate, 0.0)
        self.assertEqual(matrix.false_alarm_rate, 0.0)


class TestChecklistBaseline(unittest.TestCase):
    """The checklist is the bar the LLM must clear — measure it honestly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name) / "artifacts"
        cls.report = run_benchmark({"checklist": checklist_arm}, root=root)
        cls.results = cls.report["arms"]["checklist"]["results"]
        cls.matrix = cls.report["arms"]["checklist"]["matrix"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def action_for(self, problem_id: str) -> dict:
        return next(r["action"] for r in self.results if r["problem_id"] == problem_id)

    def test_no_false_alarms_on_controls(self) -> None:
        # The number that matters most: a prescriber that invents problems on
        # clean studies burns engineering weeks.
        self.assertEqual(self.matrix["false_positive"], 0)
        self.assertEqual(self.matrix["false_alarm_rate"], 0.0)
        for control in ("latch_control", "mesh_control"):
            self.assertEqual(self.action_for(control)["type"], ActionType.NO_ACTION.value)

    def test_catches_the_parameterization_defect(self) -> None:
        action = self.action_for("latch_missing_variable")
        self.assertEqual(action["type"], ActionType.PATCH.value)
        self.assertEqual(
            [p["target"] for p in action["patches"]], [PatchTarget.STUDY_VARIABLES.value]
        )

    def test_blind_to_the_missing_mechanism(self) -> None:
        # The checklist sees the optimum pinned at a bound and says "widen the
        # bound" — which pushes further into the model's blind spot. This is
        # the asymmetry the decision gate exists to measure.
        action = self.action_for("mesh_missing_mechanism")
        self.assertEqual([p["target"] for p in action["patches"]], [PatchTarget.STUDY_BOUNDS.value])
        result = next(r for r in self.results if r["problem_id"] == "mesh_missing_mechanism")
        self.assertFalse(result["diagnosis_correct"])

    def test_baseline_is_recorded_not_assumed(self) -> None:
        self.assertGreater(self.matrix["hit_rate"], 0.0)
        self.assertLess(self.matrix["hit_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
