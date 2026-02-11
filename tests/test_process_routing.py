from __future__ import annotations

import unittest

from server.tools import spec_export_rfq_summary, spec_select_schema
from tests.helpers import make_base_spec_draft


class TestProcessRouting(unittest.TestCase):
    def test_select_schema_print_3d(self) -> None:
        out = spec_select_schema(process="print_3d", maturity_level="L2", spec_version="1.0.0")
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["schema_id"], "print_3d_v1")
        self.assertEqual(out["question_bank_id"], "print_3d_v1")
        self.assertEqual(out["coverage_threshold"], 0.8)

    def test_export_rfq_summary_print_3d_is_process_specific(self) -> None:
        spec = make_base_spec_draft(process="print_3d", maturity_level="L2")
        spec["part"]["envelope"] = {"x": 120, "y": 60, "z": 20}
        spec["part"]["quantity"] = 10
        spec["manufacturing"]["material"] = {"family": "thermoplastic", "grade": "PETG"}
        spec["manufacturing"]["appearance"] = {
            "color": "black",
            "finish": "as-printed",
            "support_marks_ok": True,
            "cosmetic_surfaces": [],
        }
        spec["manufacturing"]["post_processing"] = ["Remove supports"]
        spec["deliverables"]["cad_formats"] = ["STL", "STEP"]
        spec["deliverables"]["drawing_required"] = True
        out = spec_export_rfq_summary(spec=spec)
        md = out["markdown"]
        self.assertIn("# RFQ Summary (3D Printing)", md)
        self.assertIn("Technology:", md)
        self.assertIn("Post processing:", md)
        self.assertNotIn("Surface finish (Ra um)", md)


if __name__ == "__main__":
    unittest.main()
