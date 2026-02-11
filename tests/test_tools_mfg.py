"""Tests for the manufacturing readiness MCP tools."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from server.tools_mfg import mfg_export_rfq, mfg_readiness_check, mfg_set_property


class TestMfgSetProperty(unittest.TestCase):
    @patch("server.tools_mfg.get_client")
    def test_valid_properties(self, mock_get: MagicMock) -> None:
        client = MagicMock()
        mock_get.return_value = client

        result = mfg_set_property(properties={
            "process": "cnc",
            "material_family": "aluminum",
            "material_grade": "6061-T6",
        })
        self.assertTrue(result["ok"])
        self.assertIn("process", result["properties_set"])
        self.assertIn("material_family", result["properties_set"])

    @patch("server.tools_mfg.get_client")
    def test_invalid_property_name(self, mock_get: MagicMock) -> None:
        client = MagicMock()
        mock_get.return_value = client

        result = mfg_set_property(properties={"bogus_key": "value"})
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_PROPERTY", result["error"]["code"])


class TestMfgReadinessCheck(unittest.TestCase):
    def test_missing_material(self) -> None:
        result = mfg_readiness_check(process="cnc", properties={})
        self.assertTrue(result["ok"])
        blocker_ids = [b["rule_id"] for b in result["blockers"]]
        self.assertIn("cnc.material.required", blocker_ids)

    def test_cnc_with_material(self) -> None:
        result = mfg_readiness_check(
            process="cnc",
            properties={"material_family": "aluminum", "quantity": 10},
        )
        self.assertTrue(result["ok"])
        # Should not have material blocker
        blocker_ids = [b["rule_id"] for b in result["blockers"]]
        self.assertNotIn("cnc.material.required", blocker_ids)

    def test_cnc_missing_grade_is_warning(self) -> None:
        result = mfg_readiness_check(
            process="cnc",
            properties={"material_family": "aluminum"},
        )
        warning_ids = [w["rule_id"] for w in result["warnings"]]
        self.assertIn("cnc.material.grade.required", warning_ids)

    def test_print_3d_process(self) -> None:
        result = mfg_readiness_check(
            process="fdm",
            properties={"material_family": "pla", "quantity": 1},
        )
        self.assertTrue(result["ok"])
        # Should have layer_height note
        note_ids = [n["rule_id"] for n in result["notes"]]
        self.assertIn("print.layer_height.recommended", note_ids)

    def test_readiness_percent(self) -> None:
        result = mfg_readiness_check(
            process="cnc",
            properties={
                "material_family": "aluminum",
                "material_grade": "6061-T6",
                "quantity": 10,
                "tolerance_general": "ISO 2768-m",
                "surface_finish_ra": 3.2,
            },
        )
        self.assertTrue(result["ok"])
        # With most properties filled, should have few issues
        self.assertEqual(len(result["blockers"]), 0)


class TestMfgExportRfq(unittest.TestCase):
    def test_generates_markdown(self) -> None:
        result = mfg_export_rfq(properties={
            "process": "cnc",
            "material_family": "aluminum",
            "material_grade": "6061-T6",
            "quantity": 10,
            "tolerance_general": "ISO 2768-m",
        })
        self.assertTrue(result["ok"])
        md = result["rfq_markdown"]
        self.assertIn("Request for Quote", md)
        self.assertIn("aluminum", md)
        self.assertIn("6061-T6", md)
        self.assertIn("10", md)

    def test_handles_empty_properties(self) -> None:
        result = mfg_export_rfq(properties={})
        self.assertTrue(result["ok"])
        self.assertIn("Not specified", result["rfq_markdown"])


if __name__ == "__main__":
    unittest.main()
