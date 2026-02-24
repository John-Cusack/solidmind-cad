"""Tests for the design brief pipeline."""
from __future__ import annotations

import unittest
from typing import Any

from server.design_models import DesignBrief
from server.design_store import clear as clear_briefs
from server.tools_design import (
    design_get_brief,
    design_save_brief,
    design_update_brief,
)


class TestDesignModels(unittest.TestCase):

    def test_brief_round_trip(self) -> None:
        b = DesignBrief(
            brief_id="brief_abc123",
            name="My Hexapod",
            parameters={"leg_count": 6, "chassis_radius_mm": 60},
            status="draft",
            research_notes="Some notes",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        d = b.to_dict()
        b2 = DesignBrief.from_dict(d)
        self.assertEqual(b2.brief_id, "brief_abc123")
        self.assertEqual(b2.parameters["leg_count"], 6)
        self.assertEqual(b2.research_notes, "Some notes")

    def test_brief_defaults(self) -> None:
        b = DesignBrief(brief_id="x", name="Test")
        self.assertEqual(b.parameters, {})
        self.assertEqual(b.status, "draft")
        self.assertEqual(b.research_notes, "")


class TestBriefCRUD(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_save_and_get(self) -> None:
        result = design_save_brief(
            name="Test Hexapod",
            parameters={"leg_count": 6, "chassis_radius_mm": 60.0},
        )
        self.assertTrue(result["ok"])
        brief = result["brief"]
        self.assertEqual(brief["name"], "Test Hexapod")
        self.assertEqual(brief["status"], "draft")
        self.assertIn("brief_", brief["brief_id"])
        self.assertEqual(brief["parameters"]["leg_count"], 6)

        get_result = design_get_brief(brief["brief_id"])
        self.assertTrue(get_result["ok"])
        self.assertEqual(get_result["brief"]["name"], "Test Hexapod")

    def test_save_with_research_notes(self) -> None:
        result = design_save_brief(
            name="Wind Turbine",
            parameters={"chord_mm": 50, "airfoil": "NACA4412"},
            research_notes="Based on NACA report 123",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["brief"]["research_notes"], "Based on NACA report 123")

    def test_save_any_parameters(self) -> None:
        """Parameters dict is completely open — any keys accepted."""
        result = design_save_brief(
            name="Custom Widget",
            parameters={
                "material": "aluminum",
                "wall_thickness_mm": 2.5,
                "nested": {"sub_key": [1, 2, 3]},
                "count": 42,
            },
        )
        self.assertTrue(result["ok"])
        params = result["brief"]["parameters"]
        self.assertEqual(params["material"], "aluminum")
        self.assertEqual(params["nested"]["sub_key"], [1, 2, 3])

    def test_save_missing_name(self) -> None:
        result = design_save_brief(name="", parameters={"x": 1})
        self.assertFalse(result["ok"])

    def test_save_invalid_status(self) -> None:
        result = design_save_brief(name="Test", parameters={}, status="bogus")
        self.assertFalse(result["ok"])

    def test_get_not_found(self) -> None:
        result = design_get_brief("brief_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BRIEF_NOT_FOUND")

    def test_update_status(self) -> None:
        result = design_save_brief(name="Test", parameters={"a": 1})
        brief_id = result["brief"]["brief_id"]

        update = design_update_brief(brief_id, status="approved")
        self.assertTrue(update["ok"])
        self.assertEqual(update["brief"]["status"], "approved")

    def test_update_parameters(self) -> None:
        result = design_save_brief(name="Test", parameters={"a": 1, "b": 2})
        brief_id = result["brief"]["brief_id"]

        update = design_update_brief(brief_id, parameters={"a": 10, "b": 2, "c": 3})
        self.assertTrue(update["ok"])
        self.assertEqual(update["brief"]["parameters"]["a"], 10)
        self.assertEqual(update["brief"]["parameters"]["c"], 3)

    def test_update_name(self) -> None:
        result = design_save_brief(name="Old Name", parameters={})
        brief_id = result["brief"]["brief_id"]

        update = design_update_brief(brief_id, name="New Name")
        self.assertTrue(update["ok"])
        self.assertEqual(update["brief"]["name"], "New Name")

    def test_update_research_notes(self) -> None:
        result = design_save_brief(name="Test", parameters={})
        brief_id = result["brief"]["brief_id"]

        update = design_update_brief(brief_id, research_notes="Found better data")
        self.assertTrue(update["ok"])
        self.assertEqual(update["brief"]["research_notes"], "Found better data")

    def test_update_not_found(self) -> None:
        result = design_update_brief("brief_nonexistent", status="approved")
        self.assertFalse(result["ok"])

    def test_update_invalid_status(self) -> None:
        result = design_save_brief(name="Test", parameters={})
        brief_id = result["brief"]["brief_id"]
        update = design_update_brief(brief_id, status="bogus")
        self.assertFalse(update["ok"])


class TestStatusLifecycle(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_full_lifecycle(self) -> None:
        result = design_save_brief(name="Lifecycle", parameters={"x": 1})
        brief_id = result["brief"]["brief_id"]
        self.assertEqual(result["brief"]["status"], "draft")

        for status in ["proposed", "approved", "building", "done"]:
            update = design_update_brief(brief_id, status=status)
            self.assertTrue(update["ok"])
            self.assertEqual(update["brief"]["status"], status)

    def test_timestamps_update(self) -> None:
        result = design_save_brief(name="Timestamps", parameters={})
        brief_id = result["brief"]["brief_id"]
        created = result["brief"]["created_at"]

        update = design_update_brief(brief_id, name="Updated")
        self.assertTrue(update["ok"])
        self.assertEqual(update["brief"]["created_at"], created)
        self.assertGreaterEqual(update["brief"]["updated_at"], created)


if __name__ == "__main__":
    unittest.main()
