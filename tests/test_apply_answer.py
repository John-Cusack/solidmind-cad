from __future__ import annotations

import unittest

from server.tools import spec_apply_answer
from tests.helpers import make_base_spec_draft


class TestApplyAnswer(unittest.TestCase):
    def test_invalid_pointer_is_atomic(self) -> None:
        spec = make_base_spec_draft()
        out = spec_apply_answer(
            spec_draft=spec,
            op="set",
            path="not/a/pointer",
            value=123,
            question_id="q",
            source="user",
        )
        self.assertFalse(out["applied"])
        self.assertEqual(out["spec_draft_updated"], spec)

    def test_set_allows_null_value(self) -> None:
        spec = make_base_spec_draft(maturity_level="L3", process="cnc")
        out = spec_apply_answer(
            spec_draft=spec,
            op="set",
            path="/part/envelope",
            value={"x": 10, "y": 10, "z": 10},
            question_id="envelope",
            source="user",
        )
        self.assertTrue(out["applied"])
        spec2 = out["spec_draft_updated"]

        out2 = spec_apply_answer(
            spec_draft=spec2,
            op="set",
            path="/manufacturing/surface_finish/ra_um",
            value=None,  # explicit JSON null is valid for drafts
            question_id="surface_finish",
            source="user",
        )
        self.assertTrue(out2["applied"])
        self.assertIsNone(out2["spec_draft_updated"]["manufacturing"]["surface_finish"]["ra_um"])

    def test_updates_answered_and_audit(self) -> None:
        spec = make_base_spec_draft()
        out = spec_apply_answer(
            spec_draft=spec,
            op="set",
            path="/part/envelope",
            value={"x": 1, "y": 2, "z": 3},
            question_id="envelope",
            source="user",
        )
        self.assertTrue(out["applied"])
        updated = out["spec_draft_updated"]
        self.assertEqual(updated["_interview"]["_counter"], 1)
        self.assertEqual(updated["_interview"]["answered"]["envelope"], "2026-02-10T00:00:01Z")
        self.assertEqual(len(updated["_audit"]), 1)
        self.assertEqual(updated["_audit"][0]["path"], "/part/envelope")

    def test_skip_marks_skipped(self) -> None:
        spec = make_base_spec_draft()
        out = spec_apply_answer(
            spec_draft=spec,
            op="set",
            path="/part/envelope",
            value={"x": 1, "y": 2, "z": 3},
            question_id="envelope",
            source="user",
        )
        spec2 = out["spec_draft_updated"]
        out2 = spec_apply_answer(
            spec_draft=spec2,
            op="set",
            path="/manufacturing/material/grade",
            value="",
            question_id="material_grade",
            source="user_skip",
        )
        self.assertTrue(out2["applied"])
        updated = out2["spec_draft_updated"]
        self.assertIn("material_grade", updated["_interview"]["skipped"])
        self.assertNotIn("material_grade", updated["_interview"]["answered"])

    def test_append_and_remove(self) -> None:
        spec = make_base_spec_draft()
        out = spec_apply_answer(
            spec_draft=spec,
            op="set",
            path="/part/envelope",
            value={"x": 10, "y": 10, "z": 10},
            question_id="envelope",
            source="user",
        )
        spec2 = out["spec_draft_updated"]
        out2 = spec_apply_answer(
            spec_draft=spec2,
            op="append",
            path="/open_questions",
            value="What is the coating?",
            question_id=None,
            source="llm_proposal",
        )
        self.assertTrue(out2["applied"])
        self.assertEqual(out2["spec_draft_updated"]["open_questions"], ["What is the coating?"])

        out3 = spec_apply_answer(
            spec_draft=out2["spec_draft_updated"],
            op="remove",
            path="/open_questions/0",
            question_id=None,
            source="import",
        )
        self.assertTrue(out3["applied"])
        self.assertEqual(out3["spec_draft_updated"]["open_questions"], [])


if __name__ == "__main__":
    unittest.main()
