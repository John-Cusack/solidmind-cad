"""Tests for server.tools_study MCP tool wrappers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
    Variant,
)
from server.study_store import save_study
from server.tools_study import (
    study_cancel,
    study_create,
    study_get_variant,
    study_list,
    study_results,
    study_status,
)


class TestStudyCreate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        # Patch the default root so tests don't write to real studies/
        self._patcher = patch("server.tools_study._error_result", side_effect=lambda c, m: {"ok": False, "error": {"code": c, "message": m}})

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_create_valid(self) -> None:
        with patch("server.tools_study.save_study") as mock_save:
            mock_save.return_value = self.root / "test" / "study.json"
            result = study_create(
                name="Test Study",
                variables=[
                    {"name": "x", "var_type": "continuous", "min_val": 0, "max_val": 10, "coarse_step": 5},
                ],
                solver={"solver_type": "mock"},
                objective={"primary_metric": "objective"},
            )
        self.assertTrue(result["ok"])
        self.assertIn("study_id", result)
        self.assertIn("execution_plan", result)
        plan = result["execution_plan"]
        self.assertEqual(plan["phase_1_coarse"]["variant_count"], 3)  # 0, 5, 10
        self.assertIn("total_est_human", plan)
        self.assertIn("pipeline_per_variant", plan)

    def test_create_empty_name(self) -> None:
        result = study_create(
            name="",
            variables=[{"name": "x", "var_type": "continuous", "min_val": 0, "max_val": 10}],
            solver={"solver_type": "mock"},
            objective={"primary_metric": "obj"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_create_no_variables(self) -> None:
        result = study_create(
            name="Test",
            variables=[],
            solver={"solver_type": "mock"},
            objective={"primary_metric": "obj"},
        )
        self.assertFalse(result["ok"])

    def test_create_unknown_solver(self) -> None:
        result = study_create(
            name="Test",
            variables=[{"name": "x", "var_type": "continuous", "min_val": 0, "max_val": 10}],
            solver={"solver_type": "nonexistent"},
            objective={"primary_metric": "obj"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_SOLVER")


class TestStudyToolsWithStore(unittest.TestCase):
    """Test study tools using a real temp store."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        # Patch the default root for all store operations
        self._patchers = [
            patch("server.tools_study.save_study", side_effect=lambda s, **kw: save_study(s, root=self.root)),
            patch("server.tools_study.load_study", side_effect=lambda sid, **kw: self._load(sid)),
            patch("server.tools_study.study_exists", side_effect=lambda sid, **kw: self._exists(sid)),
            patch("server.tools_study.list_studies", side_effect=lambda **kw: self._list()),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()

    def _load(self, study_id: str) -> Study:
        from server.study_store import load_study as _real_load
        return _real_load(study_id, root=self.root)

    def _exists(self, study_id: str) -> bool:
        from server.study_store import study_exists as _real_exists
        return _real_exists(study_id, root=self.root)

    def _list(self) -> list:
        from server.study_store import list_studies as _real_list
        return _real_list(root=self.root)

    def _create_study(self, study_id: str = "test01") -> Study:
        study = Study(
            id=study_id,
            name="Test Study",
            variables=[
                DesignVariable(name="x", var_type="continuous", min_val=0, max_val=10, coarse_step=5),
            ],
            solver=SolverConfig(solver_type="mock"),
            objective=ObjectiveConfig(primary_metric="objective"),
            status=StudyStatus.COMPLETE,
            coarse_variants=[
                Variant(variant_id="c0000", params={"x": 0}, phase="coarse", status="done", metrics={"objective": 100}),
                Variant(variant_id="c0001", params={"x": 5}, phase="coarse", status="done", metrics={"objective": 200}),
            ],
            best_variant_id="c0001",
        )
        save_study(study, root=self.root)
        return study

    def test_study_status(self) -> None:
        self._create_study()
        result = study_status("test01")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coarse_progress"], "2/2")

    def test_study_status_with_timing(self) -> None:
        import time
        study = self._create_study()
        study.started_at = time.time() - 60  # started 60s ago
        study.finished_at = time.time()
        study.coarse_variants[0].solver_time_s = 1.5
        study.coarse_variants[1].solver_time_s = 2.0
        save_study(study, root=self.root)
        result = study_status("test01")
        self.assertIn("elapsed_s", result)
        self.assertIn("elapsed_human", result)
        self.assertIn("avg_per_variant_s", result)

    def test_study_status_not_found(self) -> None:
        result = study_status("nope")
        self.assertFalse(result["ok"])

    def test_study_results(self) -> None:
        self._create_study()
        result = study_results("test01")
        self.assertTrue(result["ok"])
        self.assertEqual(result["completed_variants"], 2)
        self.assertEqual(len(result["results"]), 2)

    def test_study_results_top_n(self) -> None:
        self._create_study()
        result = study_results("test01", top_n=1)
        self.assertEqual(len(result["results"]), 1)

    def test_study_list(self) -> None:
        self._create_study("a")
        self._create_study("b")
        result = study_list()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["studies"]), 2)

    def test_study_get_variant(self) -> None:
        self._create_study()
        result = study_get_variant("test01", "c0000")
        self.assertTrue(result["ok"])
        self.assertEqual(result["variant"]["params"]["x"], 0)

    def test_study_get_variant_not_found(self) -> None:
        self._create_study()
        result = study_get_variant("test01", "nope")
        self.assertFalse(result["ok"])

    def test_study_cancel_no_pid(self) -> None:
        self._create_study()
        result = study_cancel("test01")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_PROCESS")


if __name__ == "__main__":
    unittest.main()
