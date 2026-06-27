"""Tests for server.study_models."""

from __future__ import annotations

import unittest

from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
    Variant,
)


class TestDesignVariable(unittest.TestCase):
    def test_expand_coarse_continuous(self) -> None:
        v = DesignVariable(
            name="angle",
            var_type="continuous",
            min_val=0,
            max_val=10,
            coarse_step=5,
        )
        vals = v.expand_coarse()
        self.assertEqual(vals, [0, 5, 10])

    def test_expand_coarse_with_pinned(self) -> None:
        v = DesignVariable(
            name="angle",
            var_type="continuous",
            min_val=0,
            max_val=10,
            coarse_step=5,
            pinned_values=(3.0,),
        )
        vals = v.expand_coarse()
        self.assertIn(3.0, vals)
        self.assertEqual(vals, [0, 3.0, 5, 10])

    def test_expand_coarse_categorical(self) -> None:
        v = DesignVariable(
            name="airfoil",
            var_type="categorical",
            categories=("NACA4412", "NACA2412"),
        )
        vals = v.expand_coarse()
        self.assertEqual(vals, ["NACA4412", "NACA2412"])

    def test_expand_coarse_default_steps(self) -> None:
        v = DesignVariable(
            name="x",
            var_type="continuous",
            min_val=0,
            max_val=10,
        )
        vals = v.expand_coarse()
        self.assertEqual(len(vals), 6)  # 0, 2, 4, 6, 8, 10

    def test_expand_refined(self) -> None:
        v = DesignVariable(
            name="angle",
            var_type="continuous",
            min_val=0,
            max_val=10,
            coarse_step=5,
            fine_step=1,
        )
        vals = v.expand_refined(5.0, num_steps=5)
        self.assertIn(5.0, vals)
        self.assertTrue(all(0 <= x <= 10 for x in vals))

    def test_expand_refined_clips_to_bounds(self) -> None:
        v = DesignVariable(
            name="x",
            var_type="continuous",
            min_val=0,
            max_val=3,
            coarse_step=1,
            fine_step=1,
        )
        vals = v.expand_refined(1.0, num_steps=5)
        self.assertTrue(all(0 <= x <= 3 for x in vals))

    def test_roundtrip(self) -> None:
        v = DesignVariable(
            name="angle",
            var_type="continuous",
            min_val=0,
            max_val=45,
            coarse_step=5,
            fine_step=1,
            pinned_values=(12.5,),
        )
        d = v.to_dict()
        v2 = DesignVariable.from_dict(d)
        self.assertEqual(v, v2)


class TestSolverConfig(unittest.TestCase):
    def test_roundtrip(self) -> None:
        cfg = SolverConfig(solver_type="bemt_xfoil", params={"Re": 500000}, timeout_s=60.0)
        d = cfg.to_dict()
        cfg2 = SolverConfig.from_dict(d)
        self.assertEqual(cfg, cfg2)


class TestObjectiveConfig(unittest.TestCase):
    def test_roundtrip(self) -> None:
        obj = ObjectiveConfig(
            primary_metric="efficiency",
            direction="maximize",
            constraint_bounds={"thrust_N": (10.0, None)},
            weights={"efficiency": 1.0},
        )
        d = obj.to_dict()
        obj2 = ObjectiveConfig.from_dict(d)
        self.assertEqual(obj.primary_metric, obj2.primary_metric)
        self.assertEqual(obj.direction, obj2.direction)


class TestVariant(unittest.TestCase):
    def test_roundtrip(self) -> None:
        v = Variant(
            variant_id="c0001",
            params={"angle": 5.0, "blades": 3},
            phase="coarse",
            status="done",
            metrics={"efficiency": 0.85},
            solver_time_s=1.23,
        )
        d = v.to_dict()
        v2 = Variant.from_dict(d)
        self.assertEqual(v.variant_id, v2.variant_id)
        self.assertEqual(v.params, v2.params)
        self.assertEqual(v.metrics, v2.metrics)


class TestStudy(unittest.TestCase):
    def _make_study(self) -> Study:
        return Study(
            id="test123",
            name="Test Study",
            variables=[
                DesignVariable(
                    name="angle", var_type="continuous", min_val=0, max_val=10, coarse_step=5
                ),
            ],
            solver=SolverConfig(solver_type="mock"),
            objective=ObjectiveConfig(primary_metric="objective"),
        )

    def test_roundtrip(self) -> None:
        s = self._make_study()
        d = s.to_dict()
        s2 = Study.from_dict(d)
        self.assertEqual(s.id, s2.id)
        self.assertEqual(s.name, s2.name)
        self.assertEqual(len(s.variables), len(s2.variables))
        self.assertEqual(s.status, s2.status)

    def test_new_id(self) -> None:
        id1 = Study.new_id()
        id2 = Study.new_id()
        self.assertNotEqual(id1, id2)
        self.assertEqual(len(id1), 12)

    def test_status_enum(self) -> None:
        self.assertEqual(StudyStatus.DRAFT.value, "draft")
        self.assertEqual(StudyStatus("running_coarse"), StudyStatus.RUNNING_COARSE)


if __name__ == "__main__":
    unittest.main()
