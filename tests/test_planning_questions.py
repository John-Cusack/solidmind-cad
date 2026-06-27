from __future__ import annotations

import unittest

from server.planning_questions import evaluate_planning_question_budget


class TestPlanningQuestions(unittest.TestCase):
    def test_budget_capped_to_two(self) -> None:
        spec = {
            "planning": {},
            "manufacturing": {},
            "part": {},
        }
        out = evaluate_planning_question_budget(
            spec, process="fdm", archetype="prismatic", max_questions=2
        )
        self.assertLessEqual(len(out.questions_asked), 2)
        self.assertEqual(out.max_questions, 2)

    def test_cnc_machine_mode_assumption(self) -> None:
        spec = {
            "planning": {},
            "manufacturing": {},
            "part": {"interfaces": []},
        }
        out = evaluate_planning_question_budget(
            spec, process="cnc", archetype="prismatic", max_questions=2
        )
        joined = " ".join(out.assumptions)
        self.assertIn("3-axis", joined)


if __name__ == "__main__":
    unittest.main()
