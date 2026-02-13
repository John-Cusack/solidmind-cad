from __future__ import annotations

import unittest

from server.feature_support import load_planning_policy


class TestPlanningPolicy(unittest.TestCase):
    def test_load_planning_policy(self) -> None:
        manifest = load_planning_policy()
        self.assertEqual(manifest.version, "1.0")
        self.assertGreaterEqual(manifest.default_question_budget, 0)
        self.assertIn("cnc_prismatic", manifest.policies)
        self.assertIn("fdm_prismatic", manifest.policies)

    def test_policy_has_phases_and_playbooks(self) -> None:
        manifest = load_planning_policy()
        policy = manifest.policies["cnc_prismatic"]
        self.assertTrue(policy.phase_order)
        self.assertTrue(policy.phase_policies)
        self.assertTrue(policy.repair_playbooks)


if __name__ == "__main__":
    unittest.main()
