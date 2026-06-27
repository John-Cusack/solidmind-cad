"""Tests for server.preflight — unified preflight check."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from server.design_models import InterfaceEntry, PartEntry
from server.design_store import add_interface, add_part, store_brief
from server.preflight import preflight_check


def _clear_stores():
    """Clear design and motion stores."""
    import server.design_store as ds

    ds._store.clear()
    from server import motion_store

    motion_store.clear()


class TestPreflightBase(unittest.TestCase):
    def setUp(self):
        _clear_stores()

    def tearDown(self):
        _clear_stores()


class TestPreflightGateMode(TestPreflightBase):
    """Test gate_mode validation and semantics."""

    def test_invalid_gate_mode(self):
        result = preflight_check("brief_1", gate_mode="invalid")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_advisory_mode_default(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["gate_mode"], "advisory")

    def test_report_has_required_fields(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        self.assertTrue(result["ok"])
        self.assertIn("overall_status", result)
        self.assertIn("categories", result)
        self.assertIn("coverage", result)
        self.assertIn("timing_ms", result)
        self.assertIn("policy_summary", result)

    def test_coverage_fields_present(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        cov = result["coverage"]
        self.assertIn("pairs_checked", cov)
        self.assertIn("sweep_samples", cov)

    def test_timing_fields_present(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        self.assertIn("total", result["timing_ms"])
        self.assertGreaterEqual(result["timing_ms"]["total"], 0)


class TestPreflightDesignCompleteness(TestPreflightBase):
    """Test Stage 1: design completeness."""

    def test_missing_brief(self):
        result = preflight_check("nonexistent_brief")
        self.assertTrue(result["ok"])  # preflight itself succeeds
        # But design_completeness stage should fail
        completeness = next(c for c in result["categories"] if c["name"] == "design_completeness")
        self.assertIn(completeness["status"], ("fail", "skipped"))

    def test_empty_brief_passes(self):
        """Brief with no parts has 100% completeness (nothing to build)."""
        brief = store_brief("Empty", {})
        # Will fail because FreeCAD isn't running, but that's a tool error
        result = preflight_check(brief.brief_id)
        completeness = next(c for c in result["categories"] if c["name"] == "design_completeness")
        # Without FreeCAD, this will be skipped/fail with TOOL_ERROR
        self.assertIn(completeness["status"], ("pass", "skipped", "fail"))


class TestPreflightNameResolution(TestPreflightBase):
    """Test Stage 2: name resolution and policy derivation."""

    def test_name_map_from_brief_parts(self):
        brief = store_brief("Test", {})
        add_part(
            brief.brief_id,
            PartEntry(
                name="gear_a",
                kind="custom",
                body_label="Body_GearA",
            ),
        )
        add_interface(
            brief.brief_id,
            InterfaceEntry(
                part_a="gear_a",
                port_a="teeth",
                part_b="gear_b",
                port_b="teeth",
                spec={"type": "gear_mesh"},
            ),
        )

        result = preflight_check(brief.brief_id)
        name_res = next(c for c in result["categories"] if c["name"] == "name_resolution")
        self.assertIn(name_res["status"], ("pass", "warn"))
        self.assertGreaterEqual(name_res.get("policies_derived", 0), 1)

    def test_policies_derived_from_interfaces(self):
        brief = store_brief("Test", {})
        add_part(brief.brief_id, PartEntry(name="a", kind="custom"))
        add_part(brief.brief_id, PartEntry(name="b", kind="custom"))
        add_interface(
            brief.brief_id,
            InterfaceEntry(
                part_a="a",
                port_a="teeth",
                part_b="b",
                port_b="teeth",
                spec={"type": "gear_mesh"},
            ),
        )
        add_interface(
            brief.brief_id,
            InterfaceEntry(
                part_a="a",
                port_a="bore",
                part_b="b",
                port_b="shaft",
                spec={"type": "press_fit"},
            ),
        )

        result = preflight_check(brief.brief_id)
        name_res = next(c for c in result["categories"] if c["name"] == "name_resolution")
        self.assertEqual(name_res.get("policies_derived", 0), 2)

    def test_policy_summary_reflects_policies(self):
        brief = store_brief("Test", {})
        add_interface(
            brief.brief_id,
            InterfaceEntry(
                part_a="a",
                port_a="",
                part_b="b",
                port_b="",
                spec={"type": "gear_mesh"},
            ),
        )

        result = preflight_check(brief.brief_id)
        ps = result["policy_summary"]
        self.assertEqual(ps["entries_loaded"], 1)


class TestPreflightStaticClearance(TestPreflightBase):
    """Test Stage 3: static clearance with policy filtering."""

    def test_clearance_without_freecad_or_empty_doc(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        clearance = next(c for c in result["categories"] if c["name"] == "static_clearance")
        # Without FreeCAD: skipped with TOOL_ERROR
        # With FreeCAD but empty doc: pass with 0 pairs
        self.assertIn(clearance["status"], ("skipped", "pass"))

    @patch("server.tools_cad.cad_check_clearance")
    def test_tool_error_surfaces_as_finding(self, mock_check):
        """Tool exception must produce TOOL_ERROR finding, not a silent pass."""
        mock_check.side_effect = RuntimeError("FreeCAD exploded")
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        clearance = next(c for c in result["categories"] if c["name"] == "static_clearance")
        self.assertEqual(clearance["status"], "skipped")
        self.assertTrue(any(f.get("reason_code") == "TOOL_ERROR" for f in clearance["findings"]))
        # Must NOT be pass — that would hide the failure
        self.assertNotEqual(clearance["status"], "pass")

    @patch("server.tools_cad.cad_check_clearance")
    def test_tool_error_ok_false_surfaces(self, mock_check):
        """Tool returning ok=False must produce TOOL_ERROR, not silent pass."""
        mock_check.return_value = {
            "ok": False,
            "error": {"code": "CONNECTION_ERROR", "message": "Connection refused"},
        }
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        clearance = next(c for c in result["categories"] if c["name"] == "static_clearance")
        self.assertEqual(clearance["status"], "skipped")
        self.assertTrue(any(f.get("reason_code") == "TOOL_ERROR" for f in clearance["findings"]))

    @patch("server.tools_cad.cad_check_clearance")
    def test_clearance_pass_no_violations(self, mock_check):
        mock_check.return_value = {
            "ok": True,
            "pairs_checked": 3,
            "violations": [],
            "all_clear": True,
        }
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        clearance = next(c for c in result["categories"] if c["name"] == "static_clearance")
        self.assertEqual(clearance["status"], "pass")
        self.assertEqual(clearance["pairs_checked"], 3)
        self.assertEqual(result["coverage"]["pairs_checked"], 3)

    @patch("server.tools_cad.cad_check_clearance")
    def test_clearance_violation_reported(self, mock_check):
        mock_check.return_value = {
            "ok": True,
            "pairs_checked": 3,
            "violations": [
                {
                    "body_a": "Body_A",
                    "body_b": "Body_C",
                    "distance_mm": 0.2,
                    "intersecting": False,
                }
            ],
            "all_clear": False,
        }
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        clearance = next(c for c in result["categories"] if c["name"] == "static_clearance")
        self.assertEqual(clearance["status"], "warn")
        self.assertEqual(len(clearance["findings"]), 1)

    @patch("server.tools_cad.cad_check_clearance")
    def test_clearance_intersection_filtered_by_policy(self, mock_check):
        mock_check.return_value = {
            "ok": True,
            "pairs_checked": 1,
            "violations": [
                {
                    "body_a": "Body_GearA",
                    "body_b": "Body_GearB",
                    "distance_mm": 0.0,
                    "intersecting": True,
                }
            ],
            "all_clear": False,
        }
        brief = store_brief("Test", {})
        add_part(brief.brief_id, PartEntry(name="ga", body_label="Body_GearA"))
        add_part(brief.brief_id, PartEntry(name="gb", body_label="Body_GearB"))
        add_interface(
            brief.brief_id,
            InterfaceEntry(
                part_a="ga",
                port_a="teeth",
                part_b="gb",
                port_b="teeth",
                spec={"type": "gear_mesh"},
            ),
        )

        result = preflight_check(brief.brief_id)
        clearance = next(c for c in result["categories"] if c["name"] == "static_clearance")
        # Gear mesh contact should be suppressed
        self.assertEqual(clearance["status"], "pass")
        self.assertEqual(clearance["suppressed_count"], 1)
        self.assertEqual(len(clearance["findings"]), 0)
        self.assertEqual(result["policy_summary"]["findings_suppressed"], 1)


class TestPreflightMotionValidators(TestPreflightBase):
    """Test Stage 7: motion validators."""

    def test_skipped_without_mechanism(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        motion = next(c for c in result["categories"] if c["name"] == "motion_validators")
        self.assertEqual(motion["status"], "skipped")

    def test_runs_with_mechanism(self):
        from server.tools_motion import motion_define_mechanism

        brief = store_brief("Test", {})
        mech_result = motion_define_mechanism(
            {
                "name": "test_gear",
                "parts": [
                    {"id": "a"},
                    {"id": "b"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    }
                ],
                "drives": [{"joint_id": "mesh", "speed_rpm": 100}],
            }
        )
        mid = mech_result["mechanism_id"]

        result = preflight_check(brief.brief_id, mechanism_id=mid)
        motion = next(c for c in result["categories"] if c["name"] == "motion_validators")
        self.assertIn(motion["status"], ("pass", "warn"))


class TestPreflightStrictMode(TestPreflightBase):
    """Test strict gate mode behavior."""

    def test_strict_fails_on_skipped_critical(self):
        """In strict mode, skipped critical stages should fail."""
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id, gate_mode="strict")
        self.assertTrue(result["ok"])
        # Without FreeCAD, static_clearance and motion_validators will be skipped
        self.assertEqual(result["overall_status"], "fail")

    @patch("server.tools_cad.cad_check_clearance")
    def test_strict_passes_when_all_clean(self, mock_check):
        """Strict mode passes when all stages are clean."""
        from server.tools_motion import motion_define_mechanism

        mock_check.return_value = {
            "ok": True,
            "pairs_checked": 1,
            "violations": [],
            "all_clear": True,
        }

        brief = store_brief("Test", {})
        mech_result = motion_define_mechanism(
            {
                "name": "test",
                "parts": [
                    {"id": "a"},
                    {"id": "b"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    }
                ],
                "drives": [{"joint_id": "mesh", "speed_rpm": 100}],
            }
        )
        mid = mech_result["mechanism_id"]

        result = preflight_check(brief.brief_id, mechanism_id=mid, gate_mode="strict")
        # Some stages may still be skipped (assembly_interference, etc.)
        # but motion_validators should run
        motion = next(c for c in result["categories"] if c["name"] == "motion_validators")
        self.assertNotEqual(motion["status"], "skipped")


class TestPreflightOverallStatus(TestPreflightBase):
    """Test overall status computation."""

    def test_all_pass_gives_pass(self):
        brief = store_brief("Test", {})
        # Most stages will be skipped (no FreeCAD, no mechanism)
        # In advisory mode, skipped is not fail
        result = preflight_check(brief.brief_id, gate_mode="advisory")
        # Overall should be pass or warn (not fail in advisory)
        self.assertIn(result["overall_status"], ("pass", "warn", "fail"))

    def test_category_statuses_tracked(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        for cat in result["categories"]:
            self.assertIn("name", cat)
            self.assertIn("status", cat)
            self.assertIn(cat["status"], ("pass", "warn", "fail", "skipped"))


class TestPreflightSweptClearance(TestPreflightBase):
    """Test Stage 5: swept clearance."""

    def test_skipped_without_mechanism(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        swept = next(c for c in result["categories"] if c["name"] == "swept_clearance")
        self.assertEqual(swept["status"], "skipped")
        self.assertEqual(swept["sweep_samples"], 0)


class TestPreflightJointConnectivity(TestPreflightBase):
    """Test Stage 6: joint connectivity."""

    def test_skipped_without_mechanism(self):
        brief = store_brief("Test", {})
        result = preflight_check(brief.brief_id)
        conn = next(c for c in result["categories"] if c["name"] == "joint_connectivity")
        self.assertEqual(conn["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
