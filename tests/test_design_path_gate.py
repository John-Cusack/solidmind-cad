from __future__ import annotations

import unittest

from server.tools import spec_assess_design_path
from tests.helpers import make_base_spec_draft


class TestDesignPathGate(unittest.TestCase):
    def test_print_3d_basic_box_path(self) -> None:
        spec = make_base_spec_draft(maturity_level="L1", process="print_3d")
        spec["part"]["envelope"] = {"x": 60, "y": 40, "z": 20}
        out = spec_assess_design_path(spec_draft=spec)

        self.assertEqual(out["design_path"], "basic_box")
        self.assertFalse(out["requires_full_spec"])
        self.assertEqual(out["reason_codes"], [])

    def test_print_3d_l2_is_spec_driven(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2", process="print_3d")
        spec["part"]["envelope"] = {"x": 60, "y": 40, "z": 20}
        out = spec_assess_design_path(spec_draft=spec)

        self.assertEqual(out["design_path"], "spec_driven")
        self.assertTrue(out["requires_full_spec"])
        self.assertIn("maturity_not_l1", out["reason_codes"])

    def test_print_3d_interfaces_are_spec_driven(self) -> None:
        spec = make_base_spec_draft(maturity_level="L1", process="print_3d")
        spec["part"]["envelope"] = {"x": 60, "y": 40, "z": 20}
        spec["part"]["interfaces"] = ["Mates to rail with snap fit"]
        out = spec_assess_design_path(spec_draft=spec)

        self.assertEqual(out["design_path"], "spec_driven")
        self.assertTrue(out["requires_full_spec"])
        self.assertIn("interfaces_present", out["reason_codes"])


if __name__ == "__main__":
    unittest.main()
