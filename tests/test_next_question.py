from __future__ import annotations

import unittest

from server.tools import spec_next_question
from tests.helpers import make_base_spec_draft


class TestNextQuestion(unittest.TestCase):
    def test_blocker_envelope_first(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="cnc")
        out = spec_next_question(spec_draft=spec, conversation_signals={"language_preference": "plain"})
        self.assertEqual(out["question_id"], "envelope")

    def test_blocker_material_after_envelope(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="cnc")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        out = spec_next_question(spec_draft=spec)
        self.assertEqual(out["question_id"], "material_grade")

    def test_skipped_blocker_is_not_reasked(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="cnc")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        spec["_interview"]["skipped"]["material_grade"] = "2026-02-10T00:00:00Z"
        out = spec_next_question(spec_draft=spec)
        self.assertNotEqual(out["question_id"], "material_grade")
        self.assertEqual(out["question_id"], "interfaces")

    def test_allow_revisit_skipped(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="cnc")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        spec["_interview"]["skipped"]["material_grade"] = "2026-02-10T00:00:00Z"
        out = spec_next_question(spec_draft=spec, conversation_signals={"allow_revisit_skipped": True})
        self.assertEqual(out["question_id"], "material_grade")

    def test_technical_language_for_expert(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="cnc")
        out = spec_next_question(spec_draft=spec, conversation_signals={"user_expertise": "expert", "language_preference": "auto"})
        self.assertEqual(out["question_id"], "envelope")
        self.assertIn("maximum", out["question_text"].lower())

    def test_print_3d_envelope_first(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="print_3d")
        out = spec_next_question(spec_draft=spec)
        self.assertEqual(out["question_id"], "envelope")

    def test_print_3d_material_after_envelope(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="print_3d")
        spec["part"]["envelope"] = {"x": 50, "y": 50, "z": 10}
        out = spec_next_question(spec_draft=spec)
        self.assertEqual(out["question_id"], "material_grade")

    def test_print_3d_output_target_after_envelope_answered(self) -> None:
        spec = make_base_spec_draft(maturity_level="L1", process="print_3d")
        spec["part"]["envelope"] = {"x": 50, "y": 50, "z": 10}
        spec["_interview"]["answered"]["envelope"] = "2026-02-10T00:00:00Z"
        out = spec_next_question(spec_draft=spec)
        self.assertEqual(out["question_id"], "output_target")

    def test_print_3d_in_house_settings_blocker(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="print_3d")
        spec["part"]["envelope"] = {"x": 50, "y": 50, "z": 10}
        spec["manufacturing"]["material"] = {"family": "thermoplastic", "grade": "PETG"}
        spec["part"]["interfaces"] = ["M3 inserts"]
        spec["deliverables"]["cad_formats"] = ["STL"]
        spec["manufacturing"]["output_target"] = "in_house"
        out = spec_next_question(spec_draft=spec)
        self.assertEqual(out["question_id"], "in_house_settings")


if __name__ == "__main__":
    unittest.main()
