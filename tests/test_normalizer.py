"""Tests for orchestrator.normalizer."""
from __future__ import annotations

import unittest

from orchestrator.normalizer import (
    NormalizedGoal,
    goal_to_spec_fields,
    normalize_from_dict,
    validate_normalized_goal,
)


class TestValidateNormalizedGoal(unittest.TestCase):
    def _valid_goal(self) -> NormalizedGoal:
        return NormalizedGoal(
            objectives=[
                {"name": "mass", "direction": "minimize", "unit": "kg", "weight": 1.0},
                {"name": "cost", "direction": "minimize", "unit": "USD", "weight": 0.5},
            ],
            global_constraints={"max_mass_kg": 1.0},
        )

    def test_valid_goal_passes(self) -> None:
        ok, issues = validate_normalized_goal(self._valid_goal())
        self.assertTrue(ok, issues)

    def test_missing_direction_fails(self) -> None:
        goal = NormalizedGoal(
            objectives=[{"name": "mass", "unit": "kg"}],
            global_constraints={"max_mass_kg": 1.0},
        )
        ok, issues = validate_normalized_goal(goal)
        self.assertFalse(ok)
        self.assertTrue(any("direction" in i for i in issues))

    def test_missing_unit_fails(self) -> None:
        goal = NormalizedGoal(
            objectives=[{"name": "mass", "direction": "minimize"}],
            global_constraints={"max_mass_kg": 1.0},
        )
        ok, issues = validate_normalized_goal(goal)
        self.assertFalse(ok)
        self.assertTrue(any("unit" in i for i in issues))

    def test_duplicate_names_fail(self) -> None:
        goal = NormalizedGoal(
            objectives=[
                {"name": "mass", "direction": "minimize", "unit": "kg"},
                {"name": "mass", "direction": "maximize", "unit": "kg"},
            ],
            global_constraints={"max_mass_kg": 1.0},
        )
        ok, issues = validate_normalized_goal(goal)
        self.assertFalse(ok)
        self.assertTrue(any("Duplicate" in i for i in issues))

    def test_no_objectives_fails(self) -> None:
        goal = NormalizedGoal(global_constraints={"max_mass_kg": 1.0})
        ok, issues = validate_normalized_goal(goal)
        self.assertFalse(ok)
        self.assertTrue(any("No objectives" in i for i in issues))

    def test_no_constraints_fails(self) -> None:
        goal = NormalizedGoal(
            objectives=[{"name": "mass", "direction": "minimize", "unit": "kg"}],
        )
        ok, issues = validate_normalized_goal(goal)
        self.assertFalse(ok)
        self.assertTrue(any("constraints" in i for i in issues))

    def test_invalid_direction_fails(self) -> None:
        goal = NormalizedGoal(
            objectives=[{"name": "mass", "direction": "reduce", "unit": "kg"}],
            global_constraints={"max_mass_kg": 1.0},
        )
        ok, issues = validate_normalized_goal(goal)
        self.assertFalse(ok)
        self.assertTrue(any("minimize" in i for i in issues))


class TestNormalizeFromDict(unittest.TestCase):
    def test_round_trip(self) -> None:
        raw = {
            "objectives": [{"name": "mass", "direction": "minimize", "unit": "kg"}],
            "global_constraints": {"max_mass_kg": 1.0},
            "process_assumptions": ["CNC available"],
            "duty_cycle": "continuous",
            "notes": "test",
        }
        goal = normalize_from_dict(raw)
        self.assertEqual(len(goal.objectives), 1)
        self.assertEqual(goal.duty_cycle, "continuous")
        self.assertEqual(goal.notes, "test")

    def test_empty_dict(self) -> None:
        goal = normalize_from_dict({})
        self.assertEqual(goal.objectives, [])
        self.assertEqual(goal.global_constraints, {})


class TestGoalToSpecFields(unittest.TestCase):
    def test_produces_objectives(self) -> None:
        goal = NormalizedGoal(
            objectives=[
                {"name": "mass", "direction": "minimize", "unit": "kg", "weight": 0.8},
            ],
            global_constraints={"max_mass_kg": 1.0},
        )
        fields = goal_to_spec_fields(goal)
        self.assertEqual(len(fields["objectives"]), 1)
        self.assertEqual(fields["objectives"][0].name, "mass")
        self.assertEqual(fields["objectives"][0].weight, 0.8)
        self.assertEqual(fields["global_constraints"]["max_mass_kg"], 1.0)


if __name__ == "__main__":
    unittest.main()
