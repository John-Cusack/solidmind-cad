"""Tests for orchestrator.worker."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.spec import (
    Interface,
    MasterSpec,
    Subsystem,
    SubsystemKind,
    WorkerMode,
)
from orchestrator.worker import (
    WorkerTask,
    assess_results,
    plan_tasks,
)
from orchestrator.spec import WorkerResult


def _make_spec() -> MasterSpec:
    spec = MasterSpec(name="test")
    spec.subsystems.append(Subsystem(
        id="s1", name="gear",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 10],
        material="steel",
        worker_count=2,
    ))
    spec.subsystems.append(Subsystem(
        id="s2", name="bearing",
        kind=SubsystemKind.CATALOG,
        supplier_part="SKF 6201",
    ))
    return spec


class TestPlanTasks(unittest.TestCase):
    def test_only_generated(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            tasks = plan_tasks(spec, Path(td))
            # 1 GENERATED subsystem with worker_count=2 → 2 tasks
            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0].subsystem.name, "gear")
            self.assertEqual(tasks[0].variant_index, 0)
            self.assertEqual(tasks[1].variant_index, 1)

    def test_output_dirs_created(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            tasks = plan_tasks(spec, Path(td))
            for task in tasks:
                self.assertTrue(task.output_dir.exists())

    def test_prompts_non_empty(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            tasks = plan_tasks(spec, Path(td))
            for task in tasks:
                self.assertGreater(len(task.prompt), 0)
                self.assertIn("gear", task.prompt)


class TestAssessResults(unittest.TestCase):
    def test_all_success(self) -> None:
        results = [
            WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success"),
            WorkerResult(subsystem_name="gear", worker_id="gear_1", status="success"),
        ]
        ok, issues = assess_results(results)
        self.assertTrue(ok)

    def test_failure_detected(self) -> None:
        results = [
            WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success"),
            WorkerResult(subsystem_name="gear", worker_id="gear_1", status="failed", error="timeout"),
        ]
        ok, issues = assess_results(results)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 1)
        self.assertIn("gear_1", issues[0])


if __name__ == "__main__":
    unittest.main()
