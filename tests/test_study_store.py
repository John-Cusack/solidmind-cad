"""Tests for server.study_store."""

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
    Variant,
)
from server.study_store import (
    delete_study,
    list_studies,
    load_study,
    save_study,
    study_exists,
)


class TestStudyStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_study(self, study_id: str = "s001", name: str = "Test") -> Study:
        return Study(
            id=study_id,
            name=name,
            variables=[
                DesignVariable(
                    name="x", var_type="continuous", min_val=0, max_val=10, coarse_step=5
                ),
            ],
            solver=SolverConfig(solver_type="mock"),
            objective=ObjectiveConfig(primary_metric="objective"),
        )

    def test_save_and_load_roundtrip(self) -> None:
        study = self._make_study()
        save_study(study, root=self.root)
        loaded = load_study("s001", root=self.root)
        self.assertEqual(study.id, loaded.id)
        self.assertEqual(study.name, loaded.name)
        self.assertEqual(len(study.variables), len(loaded.variables))

    def test_study_exists(self) -> None:
        self.assertFalse(study_exists("nope", root=self.root))
        study = self._make_study()
        save_study(study, root=self.root)
        self.assertTrue(study_exists("s001", root=self.root))

    def test_list_studies(self) -> None:
        self.assertEqual(list_studies(root=self.root), [])
        save_study(self._make_study("a", "Alpha"), root=self.root)
        save_study(self._make_study("b", "Beta"), root=self.root)
        summaries = list_studies(root=self.root)
        self.assertEqual(len(summaries), 2)
        ids = {s["id"] for s in summaries}
        self.assertEqual(ids, {"a", "b"})

    def test_delete_study(self) -> None:
        study = self._make_study()
        save_study(study, root=self.root)
        self.assertTrue(study_exists("s001", root=self.root))
        deleted = delete_study("s001", root=self.root)
        self.assertTrue(deleted)
        self.assertFalse(study_exists("s001", root=self.root))

    def test_delete_nonexistent(self) -> None:
        self.assertFalse(delete_study("nope", root=self.root))

    def test_load_nonexistent_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_study("nope", root=self.root)

    def test_save_with_variants(self) -> None:
        study = self._make_study()
        study.coarse_variants = [
            Variant(
                variant_id="c0000",
                params={"x": 0},
                phase="coarse",
                status="done",
                metrics={"objective": 100},
            ),
            Variant(
                variant_id="c0001",
                params={"x": 5},
                phase="coarse",
                status="done",
                metrics={"objective": 200},
            ),
        ]
        study.best_variant_id = "c0001"
        study.status = StudyStatus.COMPLETE
        save_study(study, root=self.root)

        loaded = load_study("s001", root=self.root)
        self.assertEqual(len(loaded.coarse_variants), 2)
        self.assertEqual(loaded.best_variant_id, "c0001")
        self.assertEqual(loaded.status, StudyStatus.COMPLETE)


if __name__ == "__main__":
    unittest.main()
