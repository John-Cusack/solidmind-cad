"""Tests for the StudyDriver seam and GridDriver grid parity."""

from __future__ import annotations

import unittest
from typing import Any

from server.study_drivers import (
    DakotaDriver,
    EvalOutcome,
    GridDriver,
    binding_paths,
    get_driver,
)
from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
    Variant,
)
from server.study_runner import build_coarse_variants, build_refined_variants

FILLET = "/parts/latch_sear/specs/fillet_mm"


def make_study(**objective_overrides: Any) -> Study:
    objective = {"primary_metric": "score", "direction": "maximize"}
    objective.update(objective_overrides)
    return Study(
        id="s1",
        name="latch fillet sweep",
        variables=[
            DesignVariable(
                name="fillet_mm",
                var_type="continuous",
                min_val=0.0,
                max_val=0.6,
                coarse_step=0.2,
                path=FILLET,
            )
        ],
        solver=SolverConfig(solver_type="evaluator"),
        objective=ObjectiveConfig(**objective),
        driver="grid",
    )


class FakeBackend:
    """Parabola peaking at fillet=0.35; records every call."""

    def __init__(self, fail_paths: set[float] | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], str]] = []
        self.fail_values = fail_paths or set()

    def evaluate(self, bindings: dict[str, Any], tag: str) -> EvalOutcome:
        self.calls.append((dict(bindings), tag))
        value = bindings[FILLET]
        if value in self.fail_values:
            return EvalOutcome(ok=False, error="boom")
        score = -((value - 0.35) ** 2)
        return EvalOutcome(
            ok=True,
            metrics={"score": score, "fillet": value},
            result_hash="r" * 64,
            flags={"unchanged_fingerprint": False},
            duration_s=0.01,
        )


def run_grid(study: Study, backend: FakeBackend, cancel_after: int | None = None):
    saves: list[Variant | None] = []
    evaluated = 0

    def on_variant(v: Variant | None) -> None:
        saves.append(v)

    def cancelled() -> bool:
        return cancel_after is not None and evaluated_count() >= cancel_after

    def evaluated_count() -> int:
        return len(backend.calls)

    best = GridDriver().run(study, backend, on_variant=on_variant, cancelled=cancelled)
    del evaluated
    return best, saves


class TestGridDriver(unittest.TestCase):
    def test_grid_parity_with_legacy_builders(self) -> None:
        study = make_study()
        driven = make_study()
        backend = FakeBackend()
        run_grid(driven, backend)

        legacy_coarse = build_coarse_variants(study)
        self.assertEqual(
            [(v.variant_id, v.params) for v in driven.coarse_variants],
            [(v.variant_id, v.params) for v in legacy_coarse],
        )
        best_coarse_params = {"fillet_mm": 0.4}  # closest coarse point to 0.35
        legacy_refined = build_refined_variants(study, best_coarse_params)
        self.assertEqual(
            [(v.variant_id, v.params) for v in driven.refined_variants],
            [(v.variant_id, v.params) for v in legacy_refined],
        )

    def test_complete_run_selects_best(self) -> None:
        study = make_study()
        backend = FakeBackend()
        best, _ = run_grid(study, backend)
        self.assertEqual(study.status, StudyStatus.COMPLETE)
        self.assertIsNotNone(best)
        best_variant = next(
            v for v in study.coarse_variants + study.refined_variants if v.variant_id == best
        )
        # Refined grid reaches 0.36 — the closest representable point to 0.35.
        self.assertAlmostEqual(best_variant.params["fillet_mm"], 0.36)
        self.assertEqual(best_variant.status, "done")
        self.assertEqual(best_variant.result_hash, "r" * 64)

    def test_backend_receives_path_keyed_bindings(self) -> None:
        study = make_study()
        backend = FakeBackend()
        run_grid(study, backend)
        for bindings, _tag in backend.calls:
            self.assertEqual(set(bindings), {FILLET})

    def test_constraint_bounds_filter_best(self) -> None:
        # Feasible region excludes the parabola's peak neighborhood.
        study = make_study(constraint_bounds={"fillet": (None, 0.21)})
        backend = FakeBackend()
        best, _ = run_grid(study, backend)
        best_variant = next(
            v for v in study.coarse_variants + study.refined_variants if v.variant_id == best
        )
        self.assertLessEqual(best_variant.params["fillet_mm"], 0.21)

    def test_failed_variants_recorded_not_fatal(self) -> None:
        study = make_study()
        backend = FakeBackend(fail_paths={0.0})
        best, _ = run_grid(study, backend)
        self.assertEqual(study.status, StudyStatus.COMPLETE)
        failed = [v for v in study.coarse_variants if v.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error, "boom")
        self.assertIsNotNone(best)

    def test_cancellation_stops_backend_calls(self) -> None:
        study = make_study()
        backend = FakeBackend()
        run_grid(study, backend, cancel_after=2)
        self.assertEqual(study.status, StudyStatus.CANCELLED)
        self.assertEqual(len(backend.calls), 2)
        self.assertIsNone(study.best_variant_id)

    def test_infeasible_coarse_fails_study(self) -> None:
        study = make_study(constraint_bounds={"missing_metric": (0.0, 1.0)})
        backend = FakeBackend()
        best, _ = run_grid(study, backend)
        self.assertIsNone(best)
        self.assertEqual(study.status, StudyStatus.FAILED)
        self.assertIn("No feasible", study.error or "")

    def test_on_variant_called_per_point_and_phase(self) -> None:
        study = make_study()
        backend = FakeBackend()
        _, saves = run_grid(study, backend)
        variant_saves = [s for s in saves if s is not None]
        self.assertEqual(len(variant_saves), len(backend.calls))
        self.assertGreaterEqual(len(saves) - len(variant_saves), 3)  # phase changes


class TestSeam(unittest.TestCase):
    def test_missing_path_raises(self) -> None:
        study = make_study()
        study.variables = [DesignVariable(name="x", var_type="continuous", min_val=0, max_val=1)]
        with self.assertRaises(ValueError):
            binding_paths(study)

    def test_dakota_driver_is_gated(self) -> None:
        with self.assertRaises(NotImplementedError):
            DakotaDriver().run(
                make_study(),
                FakeBackend(),
                on_variant=lambda v: None,
                cancelled=lambda: False,
            )

    def test_get_driver(self) -> None:
        self.assertIsInstance(get_driver("grid"), GridDriver)
        with self.assertRaises(KeyError):
            get_driver("annealing")


if __name__ == "__main__":
    unittest.main()
