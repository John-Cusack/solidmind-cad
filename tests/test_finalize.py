from __future__ import annotations

import unittest

from server.tools import spec_apply_answer, spec_finalize
from tests.helpers import make_base_spec_draft


class TestFinalize(unittest.TestCase):
    def test_strips_internal_fields(self) -> None:
        spec = make_base_spec_draft(maturity_level="L2")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        out = spec_finalize(spec_draft=spec)
        frozen = out["spec"]
        self.assertNotIn("_interview", frozen)
        self.assertNotIn("_audit", frozen)

    def test_hash_is_deterministic_for_same_input(self) -> None:
        spec = make_base_spec_draft(maturity_level="L1")
        spec["part"]["envelope"] = {"x": 10, "y": 10, "z": 10}
        out1 = spec_finalize(spec_draft=spec)
        out2 = spec_finalize(spec_draft=spec)
        self.assertEqual(out1["hash_algo"], "sha256_jcs_rfc8785")
        self.assertEqual(out1["hash"], out2["hash"])

    def test_provenance_is_last_write(self) -> None:
        spec = make_base_spec_draft(maturity_level="L1")
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
            op="set",
            path="/manufacturing/material/grade",
            value="6061-T6",
            question_id="material_grade",
            source="user",
        )
        spec3 = out2["spec_draft_updated"]
        out3 = spec_apply_answer(
            spec_draft=spec3,
            op="set",
            path="/manufacturing/material/grade",
            value="7075-T6",
            question_id="material_grade",
            source="user",
        )
        frozen = spec_finalize(spec_draft=out3["spec_draft_updated"])
        prov = frozen["provenance"]["/manufacturing/material/grade"]
        self.assertEqual(prov["op"], "set")
        self.assertEqual(prov["question_id"], "material_grade")
        self.assertEqual(prov["source"], "user")


if __name__ == "__main__":
    unittest.main()

