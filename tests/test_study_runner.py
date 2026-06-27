"""Tests for server.study_runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
)
from server.study_runner import run_study
from server.study_store import load_study, save_study


class TestStudyRunner(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_study(self) -> Study:
        return Study(
            id="run001",
            name="Runner Test",
            variables=[
                DesignVariable(
                    name="x",
                    var_type="continuous",
                    min_val=0,
                    max_val=100,
                    coarse_step=50,
                ),
            ],
            solver=SolverConfig(solver_type="mock"),
            objective=ObjectiveConfig(primary_metric="objective", direction="maximize"),
        )

    def test_full_run_completes(self) -> None:
        study = self._make_study()
        save_study(study, root=self.root)

        run_study("run001", root=self.root)

        result = load_study("run001", root=self.root)
        self.assertEqual(result.status, StudyStatus.COMPLETE)
        self.assertGreater(len(result.coarse_variants), 0)
        self.assertGreater(len(result.refined_variants), 0)
        self.assertIsNotNone(result.best_variant_id)

    def test_coarse_variants_all_done(self) -> None:
        study = self._make_study()
        save_study(study, root=self.root)

        run_study("run001", root=self.root)

        result = load_study("run001", root=self.root)
        for v in result.coarse_variants:
            self.assertEqual(v.status, "done")
            self.assertIn("objective", v.metrics)

    def test_refined_variants_all_done(self) -> None:
        study = self._make_study()
        save_study(study, root=self.root)

        run_study("run001", root=self.root)

        result = load_study("run001", root=self.root)
        for v in result.refined_variants:
            self.assertEqual(v.status, "done")
            self.assertIn("objective", v.metrics)

    def test_best_variant_is_optimal(self) -> None:
        study = self._make_study()
        save_study(study, root=self.root)

        run_study("run001", root=self.root)

        result = load_study("run001", root=self.root)
        # Find the actual best variant
        all_variants = result.coarse_variants + result.refined_variants
        best = max(
            (v for v in all_variants if v.status == "done"),
            key=lambda v: v.metrics.get("objective", float("-inf")),
        )
        self.assertEqual(result.best_variant_id, best.variant_id)

    def test_multi_variable_cartesian_product(self) -> None:
        study = Study(
            id="run002",
            name="Multi-var Test",
            variables=[
                DesignVariable(
                    name="a", var_type="continuous", min_val=0, max_val=10, coarse_step=10
                ),
                DesignVariable(
                    name="b", var_type="continuous", min_val=0, max_val=10, coarse_step=10
                ),
            ],
            solver=SolverConfig(solver_type="mock"),
            objective=ObjectiveConfig(primary_metric="objective", direction="maximize"),
        )
        save_study(study, root=self.root)

        run_study("run002", root=self.root)

        result = load_study("run002", root=self.root)
        # 2 values per variable × 2 variables = 4 coarse variants
        self.assertEqual(len(result.coarse_variants), 4)
        self.assertEqual(result.status, StudyStatus.COMPLETE)

    def test_constraint_bounds_filter(self) -> None:
        study = Study(
            id="run003",
            name="Constrained Test",
            variables=[
                DesignVariable(
                    name="x", var_type="continuous", min_val=0, max_val=100, coarse_step=25
                ),
            ],
            solver=SolverConfig(solver_type="mock"),
            objective=ObjectiveConfig(
                primary_metric="objective",
                direction="maximize",
                # total_param must be >= 40
                constraint_bounds={"total_param": (40.0, None)},
            ),
        )
        save_study(study, root=self.root)

        run_study("run003", root=self.root)

        result = load_study("run003", root=self.root)
        self.assertEqual(result.status, StudyStatus.COMPLETE)
        # Best variant must have total_param >= 40
        best = next(
            v
            for v in result.coarse_variants + result.refined_variants
            if v.variant_id == result.best_variant_id
        )
        self.assertGreaterEqual(best.metrics["total_param"], 40.0)


if __name__ == "__main__":
    unittest.main()
