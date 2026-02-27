"""Tests for the design brief pipeline including parts and interfaces."""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from server.design_models import DesignBrief, InterfaceEntry, PartEntry
from server.design_store import clear as clear_briefs
from server.tools_design import (
    design_add_interface,
    design_add_part,
    design_get_brief,
    design_get_part,
    design_list_briefs,
    design_save_brief,
    design_update_brief,
    design_update_part,
    design_verify_build,
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
        self.assertEqual(b.status, "intent")
        self.assertEqual(b.research_notes, "")
        self.assertEqual(b.parts, [])
        self.assertEqual(b.interfaces, [])

    def test_part_entry_round_trip(self) -> None:
        p = PartEntry(
            name="motor",
            kind="purchased",
            quantity=4,
            specs={"model": "Emax 2306", "mass_g": 33},
        )
        d = p.to_dict()
        p2 = PartEntry.from_dict(d)
        self.assertEqual(p2.name, "motor")
        self.assertEqual(p2.kind, "purchased")
        self.assertEqual(p2.quantity, 4)
        self.assertEqual(p2.specs["mass_g"], 33)

    def test_interface_entry_round_trip(self) -> None:
        i = InterfaceEntry(
            part_a="motor_mount",
            port_a="top",
            part_b="motor",
            port_b="base",
            spec={"pattern": "M3_16mm_square"},
        )
        d = i.to_dict()
        i2 = InterfaceEntry.from_dict(d)
        self.assertEqual(i2.part_a, "motor_mount")
        self.assertEqual(i2.port_b, "base")
        self.assertEqual(i2.spec["pattern"], "M3_16mm_square")

    def test_brief_with_parts_round_trip(self) -> None:
        b = DesignBrief(
            brief_id="brief_x",
            name="Test",
            parts=[
                PartEntry(name="frame", kind="custom"),
                PartEntry(name="motor", kind="purchased", quantity=4),
            ],
            interfaces=[
                InterfaceEntry(
                    part_a="frame", port_a="mount",
                    part_b="motor", port_b="base",
                    spec={"pattern": "M3"},
                ),
            ],
        )
        d = b.to_dict()
        b2 = DesignBrief.from_dict(d)
        self.assertEqual(len(b2.parts), 2)
        self.assertEqual(len(b2.interfaces), 1)
        self.assertEqual(b2.parts[1].name, "motor")
        self.assertEqual(b2.interfaces[0].part_a, "frame")

    def test_get_part_and_interfaces(self) -> None:
        b = DesignBrief(
            brief_id="brief_x",
            name="Test",
            parts=[
                PartEntry(name="A"),
                PartEntry(name="B"),
                PartEntry(name="C"),
            ],
            interfaces=[
                InterfaceEntry(part_a="A", port_a="right", part_b="B", port_b="left"),
                InterfaceEntry(part_a="B", port_a="right", part_b="C", port_b="left"),
                InterfaceEntry(part_a="A", port_a="bottom", part_b="C", port_b="top"),
            ],
        )
        self.assertIsNotNone(b.get_part("A"))
        self.assertIsNone(b.get_part("D"))
        a_ifaces = b.get_interfaces_for("A")
        self.assertEqual(len(a_ifaces), 2)
        b_ifaces = b.get_interfaces_for("B")
        self.assertEqual(len(b_ifaces), 2)
        c_ifaces = b.get_interfaces_for("C")
        self.assertEqual(len(c_ifaces), 2)


class TestBriefCRUD(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_save_and_get(self) -> None:
        result = design_save_brief(
            name="Test Hexapod",
            parameters={"leg_count": 6, "chassis_radius_mm": 60.0},
            status="intent",
        )
        self.assertTrue(result["ok"])
        brief = result["brief"]
        self.assertEqual(brief["name"], "Test Hexapod")
        self.assertEqual(brief["status"], "intent")
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

    def test_new_phase_statuses(self) -> None:
        """New phase statuses are accepted."""
        result = design_save_brief(name="Test", parameters={}, status="intent")
        self.assertTrue(result["ok"])
        brief_id = result["brief"]["brief_id"]

        for status in ["sizing", "layout", "approved", "building", "done"]:
            update = design_update_brief(brief_id, status=status)
            self.assertTrue(update["ok"])
            self.assertEqual(update["brief"]["status"], status)


class TestStatusLifecycle(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_full_lifecycle(self) -> None:
        result = design_save_brief(name="Lifecycle", parameters={"x": 1})
        brief_id = result["brief"]["brief_id"]
        self.assertEqual(result["brief"]["status"], "intent")

        for status in ["sizing", "layout", "approved", "building", "done"]:
            update = design_update_brief(brief_id, status=status)
            self.assertTrue(update["ok"])
            self.assertEqual(update["brief"]["status"], status)

    def test_phased_lifecycle(self) -> None:
        result = design_save_brief(name="Drone", parameters={}, status="intent")
        brief_id = result["brief"]["brief_id"]
        self.assertEqual(result["brief"]["status"], "intent")

        for status in ["sizing", "layout", "approved", "building", "done"]:
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


class TestPartManagement(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def _make_brief(self) -> str:
        result = design_save_brief(name="Drone", parameters={}, status="sizing")
        return result["brief"]["brief_id"]

    def test_add_part(self) -> None:
        bid = self._make_brief()
        result = design_add_part(bid, name="motor", kind="purchased", quantity=4,
                                 specs={"model": "Emax 2306", "mass_g": 33})
        self.assertTrue(result["ok"])
        self.assertEqual(result["part"]["name"], "motor")
        self.assertEqual(result["part"]["kind"], "purchased")
        self.assertEqual(result["part"]["quantity"], 4)
        self.assertEqual(result["part_count"], 1)

    def test_add_multiple_parts(self) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")
        design_add_part(bid, name="motor", kind="purchased", quantity=4)
        result = design_add_part(bid, name="arm", kind="custom", quantity=4)
        self.assertEqual(result["part_count"], 3)

        brief = design_get_brief(bid)
        self.assertEqual(len(brief["brief"]["parts"]), 3)

    def test_add_duplicate_part(self) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="motor")
        result = design_add_part(bid, name="motor")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "DUPLICATE_PART")

    def test_add_part_invalid_kind(self) -> None:
        bid = self._make_brief()
        result = design_add_part(bid, name="widget", kind="imaginary")
        self.assertFalse(result["ok"])

    def test_add_part_brief_not_found(self) -> None:
        result = design_add_part("brief_nonexistent", name="widget")
        self.assertFalse(result["ok"])

    def test_add_part_empty_name(self) -> None:
        bid = self._make_brief()
        result = design_add_part(bid, name="")
        self.assertFalse(result["ok"])

    def test_update_part(self) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")

        result = design_update_part(bid, name="frame", status="built",
                                     body_label="frame_plate")
        self.assertTrue(result["ok"])
        self.assertEqual(result["part"]["status"], "built")
        self.assertEqual(result["part"]["body_label"], "frame_plate")

    def test_update_part_specs(self) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="motor", specs={"mass_g": 33})

        result = design_update_part(bid, name="motor",
                                     specs={"mass_g": 33, "kv": 2400})
        self.assertTrue(result["ok"])
        self.assertEqual(result["part"]["specs"]["kv"], 2400)

    def test_update_part_not_found(self) -> None:
        bid = self._make_brief()
        result = design_update_part(bid, name="nonexistent", status="built")
        self.assertFalse(result["ok"])

    def test_update_part_invalid_status(self) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame")
        result = design_update_part(bid, name="frame", status="bogus")
        self.assertFalse(result["ok"])

    def test_get_part(self) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", specs={"material": "CF"})
        design_add_part(bid, name="motor", kind="purchased")
        design_add_interface(bid, part_a="frame", port_a="mount",
                             part_b="motor", port_b="base",
                             spec={"pattern": "M3"})

        result = design_get_part(bid, name="frame")
        self.assertTrue(result["ok"])
        self.assertEqual(result["part"]["name"], "frame")
        self.assertEqual(result["part"]["specs"]["material"], "CF")
        self.assertEqual(len(result["interfaces"]), 1)
        self.assertEqual(result["interfaces"][0]["part_b"], "motor")

    def test_get_part_not_found(self) -> None:
        bid = self._make_brief()
        result = design_get_part(bid, name="nonexistent")
        self.assertFalse(result["ok"])

    def test_parts_preserved_on_brief_update(self) -> None:
        """Updating brief-level fields doesn't lose parts."""
        bid = self._make_brief()
        design_add_part(bid, name="frame")
        design_add_part(bid, name="motor", kind="purchased")

        design_update_brief(bid, status="layout")
        brief = design_get_brief(bid)
        self.assertEqual(len(brief["brief"]["parts"]), 2)
        self.assertEqual(brief["brief"]["status"], "layout")


class TestInterfaceManagement(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def _make_brief_with_parts(self) -> str:
        result = design_save_brief(name="Drone", parameters={})
        bid = result["brief"]["brief_id"]
        design_add_part(bid, name="frame")
        design_add_part(bid, name="arm")
        design_add_part(bid, name="motor", kind="purchased")
        return bid

    def test_add_interface(self) -> None:
        bid = self._make_brief_with_parts()
        result = design_add_interface(
            bid,
            part_a="frame", port_a="arm_slot",
            part_b="arm", port_b="root",
            spec={"pattern": "M3_bolt_pair", "spacing_mm": 15},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["interface"]["part_a"], "frame")
        self.assertEqual(result["interface"]["spec"]["spacing_mm"], 15)
        self.assertEqual(result["interface_count"], 1)

    def test_add_multiple_interfaces(self) -> None:
        bid = self._make_brief_with_parts()
        design_add_interface(bid, part_a="frame", port_a="slot",
                             part_b="arm", port_b="root")
        result = design_add_interface(bid, part_a="arm", port_a="tip",
                                       part_b="motor", port_b="base",
                                       spec={"pattern": "M3_16mm_square"})
        self.assertEqual(result["interface_count"], 2)

    def test_add_interface_part_not_found(self) -> None:
        bid = self._make_brief_with_parts()
        result = design_add_interface(bid, part_a="frame", port_a="x",
                                       part_b="nonexistent", port_b="y")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PART_NOT_FOUND")

    def test_add_interface_brief_not_found(self) -> None:
        result = design_add_interface("brief_xxx", part_a="a", port_a="x",
                                       part_b="b", port_b="y")
        self.assertFalse(result["ok"])

    def test_interfaces_in_get_brief(self) -> None:
        bid = self._make_brief_with_parts()
        design_add_interface(bid, part_a="frame", port_a="slot",
                             part_b="arm", port_b="root")

        brief = design_get_brief(bid)
        self.assertEqual(len(brief["brief"]["interfaces"]), 1)

    def test_interfaces_preserved_on_brief_update(self) -> None:
        bid = self._make_brief_with_parts()
        design_add_interface(bid, part_a="frame", port_a="slot",
                             part_b="arm", port_b="root")

        design_update_brief(bid, status="layout")
        brief = design_get_brief(bid)
        self.assertEqual(len(brief["brief"]["interfaces"]), 1)


class TestFullPipeline(unittest.TestCase):
    """Integration test: full phased design pipeline."""

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_drone_pipeline(self) -> None:
        # Phase 1: Intent
        result = design_save_brief(
            name="Racing Quad",
            parameters={"intent": "5-inch racing quad", "max_auw_g": 600},
            status="intent",
        )
        bid = result["brief"]["brief_id"]

        # Phase 2: Sizing — add parts
        design_update_brief(bid, status="sizing")
        design_add_part(bid, name="motor", kind="purchased", quantity=4,
                        specs={"model": "Emax 2306", "mass_g": 33,
                               "mount_pattern": "M3_16mm_square"})
        design_add_part(bid, name="frame_plate", kind="custom",
                        specs={"material": "CF 2mm"})
        design_add_part(bid, name="arm", kind="custom", quantity=4)
        design_add_part(bid, name="motor_mount", kind="custom", quantity=4)

        # Phase 3: Layout — add interfaces
        design_update_brief(bid, status="layout")
        design_add_interface(bid, part_a="motor_mount", port_a="top",
                             part_b="motor", port_b="base",
                             spec={"pattern": "M3_16mm_square"})
        design_add_interface(bid, part_a="motor_mount", port_a="bottom",
                             part_b="arm", port_b="tip",
                             spec={"type": "clamp", "tube_od_mm": 10})
        design_add_interface(bid, part_a="arm", port_a="root",
                             part_b="frame_plate", port_b="arm_slot",
                             spec={"pattern": "M3_bolt_pair"})

        design_update_brief(bid, parameters={
            "intent": "5-inch racing quad", "max_auw_g": 600,
            "layout": {"arm_length_mm": 110, "arm_angles_deg": [45, 135, 225, 315]},
        })

        # Approve
        design_update_brief(bid, status="approved")

        # Phase 4: Build
        design_update_brief(bid, status="building")

        # Get part with interfaces for building
        part_result = design_get_part(bid, "motor_mount")
        self.assertTrue(part_result["ok"])
        self.assertEqual(len(part_result["interfaces"]), 2)

        # Mark parts as built
        design_update_part(bid, name="frame_plate", status="built", body_label="frame_plate")
        design_update_part(bid, name="arm", status="built", body_label="arm_FL")
        design_update_part(bid, name="motor_mount", status="built", body_label="motor_mount_FL")

        design_update_brief(bid, status="done")

        # Verify final state
        brief = design_get_brief(bid)["brief"]
        self.assertEqual(brief["status"], "done")
        self.assertEqual(len(brief["parts"]), 4)
        self.assertEqual(len(brief["interfaces"]), 3)

        frame = next(p for p in brief["parts"] if p["name"] == "frame_plate")
        self.assertEqual(frame["status"], "built")
        self.assertEqual(frame["body_label"], "frame_plate")


class TestListBriefs(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_empty(self) -> None:
        result = design_list_briefs()
        self.assertTrue(result["ok"])
        self.assertEqual(result["briefs"], [])

    def test_multiple_briefs(self) -> None:
        design_save_brief(name="A", parameters={})
        design_save_brief(name="B", parameters={"x": 1})
        result = design_list_briefs()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["briefs"]), 2)
        names = {b["name"] for b in result["briefs"]}
        self.assertEqual(names, {"A", "B"})


def _mock_tree(bodies: list[dict[str, Any]]) -> dict[str, Any]:
    """Helper to build a mock model tree response."""
    return {"ok": True, "bodies": bodies}


def _body(label: str, size: list[float] | None = None) -> dict[str, Any]:
    """Helper to build a mock body entry."""
    b: dict[str, Any] = {"name": label, "label": label, "tip": "Pad", "feature_count": 1}
    if size is not None:
        b["size"] = size
    return b


class TestVerifyBuild(unittest.TestCase):

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def _make_brief(self) -> str:
        result = design_save_brief(name="TestBot", parameters={}, status="building")
        return result["brief"]["brief_id"]

    @patch("server.tools_cad.cad_get_model_tree")
    def test_all_parts_found(self, mock_tree_fn: Any) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")
        design_add_part(bid, name="arm", kind="custom")
        design_add_part(bid, name="leg", kind="custom")
        design_update_part(bid, name="frame", body_label="frame")
        design_update_part(bid, name="arm", body_label="arm")
        design_update_part(bid, name="leg", body_label="leg")

        mock_tree_fn.return_value = _mock_tree([
            _body("frame", [100, 50, 5]),
            _body("arm", [80, 10, 10]),
            _body("leg", [60, 15, 15]),
        ])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["custom_parts_planned"], 3)
        self.assertEqual(result["summary"]["custom_parts_found"], 3)
        self.assertEqual(result["summary"]["completeness_pct"], 100.0)
        self.assertEqual(result["action_items"], [])

        for part in result["parts"]:
            self.assertEqual(part["verdict"], "OK")

    @patch("server.tools_cad.cad_get_model_tree")
    def test_missing_parts(self, mock_tree_fn: Any) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")
        design_add_part(bid, name="arm", kind="custom")
        design_add_part(bid, name="propeller", kind="custom", quantity=4)
        design_update_part(bid, name="frame", body_label="frame")
        design_update_part(bid, name="arm", body_label="arm")

        mock_tree_fn.return_value = _mock_tree([
            _body("frame", [100, 50, 5]),
            _body("arm", [80, 10, 10]),
        ])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["custom_parts_missing"], 1)
        self.assertLess(result["summary"]["completeness_pct"], 100.0)
        self.assertTrue(len(result["action_items"]) > 0)
        self.assertIn("propeller", result["action_items"][0])

    @patch("server.tools_cad.cad_get_model_tree")
    def test_purchased_skipped(self, mock_tree_fn: Any) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")
        design_add_part(bid, name="motor", kind="purchased", quantity=4)
        design_update_part(bid, name="frame", body_label="frame")

        mock_tree_fn.return_value = _mock_tree([_body("frame")])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["purchased_parts"], 1)
        self.assertEqual(result["summary"]["custom_parts_planned"], 1)
        self.assertEqual(result["summary"]["custom_parts_found"], 1)

        motor_report = next(p for p in result["parts"] if p["name"] == "motor")
        self.assertEqual(motor_report["verdict"], "PURCHASED_SKIPPED")

    @patch("server.tools_cad.cad_get_model_tree")
    def test_dimension_warning(self, mock_tree_fn: Any) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="plate", kind="custom",
                        specs={"length_mm": 100, "width_mm": 50, "thickness_mm": 5})
        design_update_part(bid, name="plate", body_label="plate")

        # Size that is off by more than 10% on length
        mock_tree_fn.return_value = _mock_tree([
            _body("plate", [85, 50, 5]),
        ])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        plate = next(p for p in result["parts"] if p["name"] == "plate")
        self.assertTrue(len(plate["dimension_warnings"]) > 0)

    def test_brief_not_found(self) -> None:
        result = design_verify_build("brief_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BRIEF_NOT_FOUND")

    @patch("server.tools_cad.cad_get_model_tree")
    def test_freecad_disconnected(self, mock_tree_fn: Any) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")

        mock_tree_fn.return_value = {
            "ok": False,
            "error": {"code": "CONNECTION_ERROR", "message": "Not connected"},
        }

        result = design_verify_build(bid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "MODEL_TREE_ERROR")

    @patch("server.tools_cad.cad_get_model_tree")
    def test_quantity_partial(self, mock_tree_fn: Any) -> None:
        bid = self._make_brief()
        design_add_part(bid, name="arm", kind="custom", quantity=4)

        mock_tree_fn.return_value = _mock_tree([
            _body("arm_FL"),
            _body("arm_FR"),
        ])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        arm = next(p for p in result["parts"] if p["name"] == "arm")
        self.assertEqual(arm["verdict"], "PARTIAL")
        self.assertEqual(arm["found_count"], 2)
        self.assertTrue(any("arm" in ai for ai in result["action_items"]))

    @patch("server.tools_cad.cad_get_model_tree")
    def test_fuzzy_match(self, mock_tree_fn: Any) -> None:
        """Part has no body_label but name matches body label case-insensitively."""
        bid = self._make_brief()
        design_add_part(bid, name="motor_mount", kind="custom")

        mock_tree_fn.return_value = _mock_tree([
            _body("Motor_Mount"),
        ])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        mm = next(p for p in result["parts"] if p["name"] == "motor_mount")
        self.assertEqual(mm["verdict"], "OK")
        self.assertEqual(mm["found_count"], 1)

    @patch("server.tools_cad.cad_get_model_tree")
    def test_stale_part(self, mock_tree_fn: Any) -> None:
        """Brief says 'built' but body missing from tree."""
        bid = self._make_brief()
        design_add_part(bid, name="bracket", kind="custom")
        design_update_part(bid, name="bracket", status="built", body_label="bracket_v1")

        mock_tree_fn.return_value = _mock_tree([])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        bracket = next(p for p in result["parts"] if p["name"] == "bracket")
        self.assertEqual(bracket["verdict"], "STALE")
        self.assertTrue(len(result["action_items"]) > 0)

    @patch("server.tools_cad.cad_get_model_tree")
    def test_unmatched_bodies(self, mock_tree_fn: Any) -> None:
        """Bodies in FreeCAD not tracked in the brief appear in unmatched_bodies."""
        bid = self._make_brief()
        design_add_part(bid, name="frame", kind="custom")
        design_update_part(bid, name="frame", body_label="frame")

        mock_tree_fn.return_value = _mock_tree([
            _body("frame"),
            _body("Origin"),
            _body("random_extra"),
        ])

        result = design_verify_build(bid)
        self.assertTrue(result["ok"])
        self.assertIn("Origin", result["unmatched_bodies"])
        self.assertIn("random_extra", result["unmatched_bodies"])


class TestDesignUpdateBriefAutoRegistersPlan(unittest.TestCase):
    """Auto-register placement plan when brief transitions to 'building'."""

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    @patch("server.tools_cad.cad_register_placement_plan")
    def test_auto_registers_plan_on_building_transition(
        self, mock_register: Any,
    ) -> None:
        mock_register.return_value = {"ok": True, "registered": 2}

        result = design_save_brief(
            name="Bot",
            parameters={
                "layout": {
                    "positions": {
                        "chassis": [0, 0, 0],
                        "arm": [50, 0, 5],
                    },
                },
            },
            status="approved",
        )
        bid = result["brief"]["brief_id"]
        design_add_part(bid, name="chassis", kind="custom")
        design_add_part(bid, name="arm", kind="custom",
                        specs={"position_mm": [50, 0, 5]})

        update = design_update_brief(bid, status="building")
        self.assertTrue(update["ok"])
        self.assertEqual(update.get("placement_plan_registered"), 2)
        mock_register.assert_called_once()

        # Verify the plan contains correct positions
        call_kwargs = mock_register.call_args[1]
        plan = call_kwargs["plan"]
        self.assertIn("chassis", plan)
        self.assertEqual(plan["chassis"]["position"], [0, 0, 0])
        self.assertIn("arm", plan)
        self.assertEqual(plan["arm"]["position"], [50, 0, 5])

    @patch("server.tools_cad.cad_register_placement_plan")
    def test_no_reregister_when_already_building(
        self, mock_register: Any,
    ) -> None:
        """Updating a brief that's already 'building' should NOT re-register."""
        result = design_save_brief(
            name="Bot",
            parameters={"layout": {"positions": {"chassis": [0, 0, 0]}}},
            status="building",
        )
        bid = result["brief"]["brief_id"]
        design_add_part(bid, name="chassis", kind="custom")

        # Update something else while already in "building" status
        update = design_update_brief(bid, name="Bot v2")
        self.assertTrue(update["ok"])
        mock_register.assert_not_called()

    @patch("server.tools_cad.cad_register_placement_plan")
    def test_no_plan_when_no_positions(
        self, mock_register: Any,
    ) -> None:
        """No plan registered when brief has no position data."""
        result = design_save_brief(
            name="Bot",
            parameters={},
            status="approved",
        )
        bid = result["brief"]["brief_id"]
        design_add_part(bid, name="chassis", kind="custom")

        update = design_update_brief(bid, status="building")
        self.assertTrue(update["ok"])
        self.assertNotIn("placement_plan_registered", update)
        mock_register.assert_not_called()

    @patch("server.tools_cad.cad_register_placement_plan")
    def test_purchased_parts_excluded(
        self, mock_register: Any,
    ) -> None:
        """Purchased parts should not appear in the placement plan."""
        mock_register.return_value = {"ok": True, "registered": 1}

        result = design_save_brief(
            name="Bot",
            parameters={
                "layout": {
                    "positions": {
                        "chassis": [0, 0, 0],
                        "motor": [50, 0, 5],
                    },
                },
            },
            status="approved",
        )
        bid = result["brief"]["brief_id"]
        design_add_part(bid, name="chassis", kind="custom")
        design_add_part(bid, name="motor", kind="purchased")

        update = design_update_brief(bid, status="building")
        self.assertTrue(update["ok"])
        mock_register.assert_called_once()
        plan = mock_register.call_args[1]["plan"]
        self.assertIn("chassis", plan)
        self.assertNotIn("motor", plan)


if __name__ == "__main__":
    unittest.main()
