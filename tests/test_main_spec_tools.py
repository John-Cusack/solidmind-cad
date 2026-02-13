from __future__ import annotations

import unittest

from server import main as mcp_main
from tests.helpers import make_base_spec_draft


class TestMainSpecTools(unittest.TestCase):
    def test_tools_list_includes_spec_tools(self) -> None:
        names = {entry.get("name") for entry in mcp_main._tool_list()}
        expected = {
            "spec.select_schema",
            "spec.apply_answer",
            "spec.validate",
            "spec.next_question",
            "spec.finalize",
            "spec.export_brief",
            "spec.export_rfq_summary",
            "spec.assess_design_path",
            "spec.generate_cad",
        }
        self.assertTrue(expected.issubset(names))

    def test_call_tool_spec_select_schema(self) -> None:
        out = mcp_main._call_tool(
            "spec.select_schema",
            {"process": "print_3d", "maturity_level": "L2", "spec_version": "1.0.0"},
        )
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["schema_id"], "print_3d_v1")
        self.assertEqual(out["question_bank_id"], "print_3d_v1")
        self.assertEqual(out["coverage_threshold"], 0.8)

    def test_call_tool_spec_apply_answer(self) -> None:
        draft = make_base_spec_draft(process="print_3d", maturity_level="L1")
        out = mcp_main._call_tool(
            "spec.apply_answer",
            {
                "spec_draft": draft,
                "op": "set",
                "path": "/part/envelope",
                "value": {"x": 120, "y": 60, "z": 40},
                "question_id": "envelope",
                "source": "user",
            },
        )
        self.assertTrue(out["applied"])
        self.assertEqual(out["errors"], [])
        updated = out["spec_draft_updated"]
        self.assertEqual(updated["part"]["envelope"], {"x": 120, "y": 60, "z": 40})
        self.assertIn("envelope", updated["_interview"]["answered"])

    def test_call_tool_spec_next_question(self) -> None:
        draft = make_base_spec_draft(process="print_3d", maturity_level="L2")
        draft["part"]["envelope"] = {"x": 40, "y": 30, "z": 20}
        out = mcp_main._call_tool("spec.next_question", {"spec_draft": draft})
        self.assertEqual(out["question_id"], "material_grade")

    def test_call_tool_spec_finalize(self) -> None:
        draft = make_base_spec_draft(process="print_3d", maturity_level="L1")
        draft["part"]["envelope"] = {"x": 40, "y": 30, "z": 20}
        out = mcp_main._call_tool("spec.finalize", {"spec_draft": draft})
        self.assertIn("spec", out)
        self.assertIn("hash", out)
        self.assertNotIn("_interview", out["spec"])
        self.assertNotIn("_audit", out["spec"])

    def test_call_tool_spec_plan_geometry_with_options(self) -> None:
        spec = {
            "meta": {"process": "cnc", "units": "mm"},
            "part": {"envelope": {"x": 100, "y": 50, "z": 20}},
            "geometry": {},
        }
        out = mcp_main._call_tool(
            "spec.plan_geometry",
            {"spec": spec, "options": {"planning_mode": "policy_v1"}},
        )
        self.assertIn("gir", out)
        self.assertIn("eir", out)
        self.assertIn("planning_plan", out)

    def test_call_tool_spec_generate_cad_policy_v1_metadata(self) -> None:
        spec = {
            "meta": {"process": "cnc", "units": "mm", "maturity_level": "L2"},
            "part": {"envelope": {"x": 100, "y": 50, "z": 20}},
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    }
                ]
            },
        }
        out = mcp_main._call_tool(
            "spec.generate_cad",
            {
                "spec": spec,
                "output_format": "step",
                "options": {"planning_mode": "policy_v1"},
            },
        )
        self.assertEqual(out["errors"], [])
        self.assertIn("planning_plan_hash", out["metadata"])
        self.assertIn("policy_key", out["metadata"])
        self.assertIn("checkpoint_summary", out["metadata"])
        self.assertIn("repair_recommendations_present", out["metadata"])


if __name__ == "__main__":
    unittest.main()
