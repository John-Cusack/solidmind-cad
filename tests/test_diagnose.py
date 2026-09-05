"""Tests for the deterministic diagnosis checklist."""

from __future__ import annotations

import unittest

from server.diagnose import Severity, diagnose, summarize
from server.eval_models import EvalResult
from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    Variant,
)

PATH = "/parts/latch_sear/specs/fillet_mm"


def make_study(
    *,
    variables: list[DesignVariable] | None = None,
    objective: ObjectiveConfig | None = None,
    variants: list[Variant] | None = None,
) -> Study:
    study = Study(
        id="s1",
        name="t",
        variables=variables
        or [
            DesignVariable(
                name="fillet_mm",
                var_type="continuous",
                min_val=0.0,
                max_val=0.6,
                coarse_step=0.2,
                path=PATH,
            )
        ],
        solver=SolverConfig(solver_type="evaluator"),
        objective=objective or ObjectiveConfig(primary_metric="fos", direction="maximize"),
    )
    study.coarse_variants = variants or []
    return study


def variant(vid: str, value: float, fos: float, status: str = "done") -> Variant:
    return Variant(
        variant_id=vid,
        params={"fillet_mm": value},
        phase="coarse",
        status=status,
        metrics={"fos": fos},
    )


def result(**kwargs) -> EvalResult:
    base = {"ok": True, "revision": "r", "request_hash": "h"}
    base.update(kwargs)
    return EvalResult(**base)


class TestHealthyStudy(unittest.TestCase):
    def test_no_findings_on_a_clean_interior_optimum(self) -> None:
        study = make_study(
            variants=[variant("c0", 0.0, 1.0), variant("c1", 0.2, 3.0), variant("c2", 0.4, 2.0)]
        )
        d = diagnose([], study)
        self.assertEqual(d.findings, ())
        self.assertEqual(summarize(d), "no findings")


class TestRunHealth(unittest.TestCase):
    def test_mass_failure_is_blocking(self) -> None:
        study = make_study(
            variants=[
                variant("c0", 0.0, 0.0, status="failed"),
                variant("c1", 0.2, 0.0, status="failed"),
                variant("c2", 0.4, 1.0),
            ]
        )
        study.coarse_variants[0].error = "binding_error: bad path"
        d = diagnose([], study)
        self.assertTrue(d.has("evaluations_failing"))
        finding = next(f for f in d.findings if f.code == "evaluations_failing")
        self.assertEqual(finding.severity, Severity.BLOCKING)

    def test_no_completed_evaluations_short_circuits(self) -> None:
        study = make_study(variants=[variant("c0", 0.0, 0.0, status="failed")])
        d = diagnose([], study)
        self.assertTrue(d.has("no_completed_evaluations"))


class TestObjective(unittest.TestCase):
    def test_flat_objective_detected(self) -> None:
        study = make_study(
            variants=[variant("c0", 0.0, 2.0), variant("c1", 0.2, 2.0), variant("c2", 0.4, 2.0)]
        )
        self.assertTrue(diagnose([], study).has("objective_flat"))

    def test_missing_metric_detected(self) -> None:
        study = make_study(
            objective=ObjectiveConfig(primary_metric="not_produced"),
            variants=[variant("c0", 0.0, 1.0)],
        )
        self.assertTrue(diagnose([], study).has("objective_metric_missing"))

    def test_no_feasible_variants(self) -> None:
        study = make_study(
            objective=ObjectiveConfig(
                primary_metric="fos", constraint_bounds={"fos": (10.0, None)}
            ),
            variants=[variant("c0", 0.0, 1.0), variant("c1", 0.2, 2.0)],
        )
        self.assertTrue(diagnose([], study).has("no_feasible_variants"))


class TestBounds(unittest.TestCase):
    def test_optimum_at_max_bound(self) -> None:
        study = make_study(variants=[variant("c0", 0.0, 1.0), variant("c1", 0.6, 5.0)])
        d = diagnose([], study)
        self.assertTrue(d.has("optimum_at_bound"))
        evidence = next(f.evidence for f in d.findings if f.code == "optimum_at_bound")
        self.assertEqual(evidence["bound"], "max")
        self.assertEqual(evidence["variable"], "fillet_mm")

    def test_interior_optimum_is_not_flagged(self) -> None:
        study = make_study(
            variants=[variant("c0", 0.0, 1.0), variant("c1", 0.2, 5.0), variant("c2", 0.6, 2.0)]
        )
        self.assertFalse(diagnose([], study).has("optimum_at_bound"))


class TestResultSignals(unittest.TestCase):
    def test_noop_needs_a_cluster_not_a_single_flag(self) -> None:
        study = make_study(variants=[variant("c0", 0.0, 1.0), variant("c1", 0.2, 2.0)])
        # Binding a parameter to the value it already holds flags once — that
        # is not evidence the variable is inert.
        results = [
            result(
                flags={"unchanged_fingerprint": True},
                applied_bindings=[{"path": PATH, "value": 0.0}],
            ),
            result(
                flags={"unchanged_fingerprint": False},
                applied_bindings=[{"path": PATH, "value": 0.2}],
            ),
        ]
        self.assertFalse(diagnose(results, study).has("unchanged_fingerprint"))

    def test_noop_detected_when_every_value_flags(self) -> None:
        study = make_study(variants=[variant("c0", 0.0, 1.0), variant("c1", 0.2, 1.0)])
        results = [
            result(
                flags={"unchanged_fingerprint": True},
                applied_bindings=[{"path": PATH, "value": v}],
            )
            for v in (0.0, 0.2, 0.4)
        ]
        d = diagnose(results, study)
        self.assertTrue(d.has("unchanged_fingerprint"))
        self.assertEqual(
            next(f.evidence for f in d.findings if f.code == "unchanged_fingerprint")["paths"],
            [PATH],
        )

    def test_validity_domain_hits_surface(self) -> None:
        study = make_study(variants=[variant("c0", 0.0, 1.0)])
        results = [result(validity_domain_hits=[{"path": PATH, "value": 9.9, "kind": "above_max"}])]
        d = diagnose(results, study)
        self.assertTrue(d.has("validity_domain_hit"))
        finding = next(f for f in d.findings if f.code == "validity_domain_hit")
        self.assertEqual(finding.suggested_action, "request_measurement")

    def test_uncalibrated_models_surface(self) -> None:
        study = make_study(variants=[variant("c0", 0.0, 1.0)])
        results = [result(uncalibrated_models=["analytic_acoustic_bearing:coherence_loss"])]
        self.assertTrue(diagnose(results, study).has("uncalibrated_model_in_optimum"))


class TestSensitivity(unittest.TestCase):
    def test_concentration_reported(self) -> None:
        variables = [
            DesignVariable(name="a", var_type="continuous", min_val=0, max_val=1, path="/a"),
            DesignVariable(name="b", var_type="continuous", min_val=0, max_val=1, path="/b"),
        ]
        variants = []
        for i, (a, b) in enumerate([(0, 0), (0, 1), (1, 0), (1, 1)]):
            variants.append(
                Variant(
                    variant_id=f"c{i}",
                    params={"a": float(a), "b": float(b)},
                    phase="coarse",
                    status="done",
                    metrics={"fos": 10.0 * a + 0.001 * b},
                )
            )
        d = diagnose([], make_study(variables=variables, variants=variants))
        self.assertTrue(d.has("sensitivity_concentrated"))
        evidence = next(f.evidence for f in d.findings if f.code == "sensitivity_concentrated")
        self.assertEqual(evidence["dominant"], ["a"])
        self.assertEqual(evidence["inert"], ["b"])


if __name__ == "__main__":
    unittest.main()
