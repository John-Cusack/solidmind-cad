from __future__ import annotations

import unittest

from server.question_bank import load_question_bank
from server.tools import spec_validate
from tests.helpers import make_base_spec_draft


class TestValidation(unittest.TestCase):
    def test_shape_invalid_until_envelope_set(self) -> None:
        spec = make_base_spec_draft(maturity_level="L1")
        out = spec_validate(spec_draft=spec)
        self.assertFalse(out["shape_valid"])
        self.assertGreaterEqual(len(out["errors"]), 1)

        # Fix envelope -> shape should become valid (all required fields exist in base skeleton).
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        out2 = spec_validate(spec_draft=spec)
        self.assertTrue(out2["shape_valid"])

    def test_rules_blockers_l2(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        out = spec_validate(spec_draft=spec)
        blocker_qids = {b.get("question_id") for b in out["blockers"]}
        self.assertIn("material_grade", blocker_qids)
        self.assertIn("interfaces", blocker_qids)
        self.assertIn("cad_formats", blocker_qids)

    def test_coverage_score_1_when_all_answered(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}

        qb = load_question_bank("cnc")
        spec["_interview"]["answered"] = {q.id: "2026-02-10T00:00:00Z" for q in qb.questions}
        out = spec_validate(spec_draft=spec)
        self.assertAlmostEqual(out["coverage_score"], 1.0, places=9)
        self.assertEqual(out["coverage_threshold"], 0.8)


if __name__ == "__main__":
    unittest.main()

