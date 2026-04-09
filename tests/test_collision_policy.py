"""Tests for server.collision_policy — policy filtering for interference detection."""
from __future__ import annotations

import unittest

from server.collision_policy import (
    CONTACT_DEFAULTS,
    CollisionPolicy,
    build_name_map,
    derive_policies,
    filter_violations,
)


class TestCollisionPolicy(unittest.TestCase):
    """Test the CollisionPolicy dataclass and CONTACT_DEFAULTS."""

    def test_policy_creation(self):
        p = CollisionPolicy(
            interface_id="a:teeth->b:teeth",
            part_a="a",
            part_b="b",
            contact_type="gear_mesh",
            max_penetration_mm=0.0,
            min_clearance_mm=0.1,
        )
        self.assertEqual(p.contact_type, "gear_mesh")
        self.assertEqual(p.max_penetration_mm, 0.0)

    def test_contact_defaults_has_standard_types(self):
        for ctype in ("gear_mesh", "press_fit", "bolt_pattern", "thread"):
            self.assertIn(ctype, CONTACT_DEFAULTS)
            pen, clr = CONTACT_DEFAULTS[ctype]
            self.assertGreaterEqual(pen, 0.0)
            self.assertGreaterEqual(clr, 0.0)


class TestDerivePolicy(unittest.TestCase):
    """Test derive_policies from interface dicts."""

    def test_gear_mesh_interface(self):
        interfaces = [{
            "part_a": "gear_a",
            "port_a": "teeth",
            "part_b": "gear_b",
            "port_b": "teeth",
            "spec": {"type": "gear_mesh"},
        }]
        policies = derive_policies(interfaces)
        self.assertEqual(len(policies), 1)
        p = policies[0]
        self.assertEqual(p.part_a, "gear_a")
        self.assertEqual(p.part_b, "gear_b")
        self.assertEqual(p.contact_type, "gear_mesh")
        self.assertAlmostEqual(p.max_penetration_mm, 0.0)
        self.assertAlmostEqual(p.min_clearance_mm, 0.1)

    def test_press_fit_with_custom_threshold(self):
        interfaces = [{
            "part_a": "shaft",
            "port_a": "bore",
            "part_b": "bearing",
            "port_b": "outer",
            "spec": {"type": "press_fit", "max_penetration_mm": 0.03},
        }]
        policies = derive_policies(interfaces)
        self.assertEqual(len(policies), 1)
        self.assertAlmostEqual(policies[0].max_penetration_mm, 0.03)

    def test_no_type_skipped(self):
        interfaces = [{
            "part_a": "a",
            "port_a": "",
            "part_b": "b",
            "port_b": "",
            "spec": {},
        }]
        policies = derive_policies(interfaces)
        self.assertEqual(len(policies), 0)

    def test_pattern_field_used_as_type(self):
        interfaces = [{
            "part_a": "a",
            "port_a": "",
            "part_b": "b",
            "port_b": "",
            "spec": {"pattern": "bolt_pattern"},
        }]
        policies = derive_policies(interfaces)
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0].contact_type, "bolt_pattern")

    def test_unknown_type_gets_zero_defaults(self):
        interfaces = [{
            "part_a": "a",
            "port_a": "",
            "part_b": "b",
            "port_b": "",
            "spec": {"type": "custom_weld"},
        }]
        policies = derive_policies(interfaces)
        self.assertEqual(len(policies), 1)
        self.assertAlmostEqual(policies[0].max_penetration_mm, 0.0)
        self.assertAlmostEqual(policies[0].min_clearance_mm, 0.0)


class TestBuildNameMap(unittest.TestCase):
    """Test name resolution from brief/mechanism parts."""

    def test_brief_parts(self):
        parts = [
            {"name": "gear_a", "body_label": "Body_GearA"},
            {"name": "gear_b", "body_label": "Body_GearB"},
        ]
        nm = build_name_map(parts)
        self.assertEqual(nm["gear_a"], "Body_GearA")
        self.assertEqual(nm["gear_b"], "Body_GearB")

    def test_mechanism_parts_override(self):
        brief = [{"name": "arm", "body_label": "Body_Arm_old"}]
        mech = [{"id": "arm", "body_name": "Body_Arm_v2"}]
        nm = build_name_map(brief, mech)
        self.assertEqual(nm["arm"], "Body_Arm_v2")

    def test_empty_labels_skipped(self):
        parts = [{"name": "a", "body_label": ""}]
        nm = build_name_map(parts)
        self.assertNotIn("a", nm)


class TestFilterViolations(unittest.TestCase):
    """Test violation filtering against policies."""

    def _gear_policy(self) -> list[CollisionPolicy]:
        return [CollisionPolicy(
            interface_id="gear_a:teeth->gear_b:teeth",
            part_a="gear_a",
            part_b="gear_b",
            contact_type="gear_mesh",
            max_penetration_mm=0.0,
            min_clearance_mm=0.1,
        )]

    def test_no_policies_passes_all(self):
        violations = [{"body_a": "A", "body_b": "B", "distance_mm": 0.3, "intersecting": False}]
        filtered, suppressed = filter_violations(violations, [])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(suppressed), 0)

    def test_gear_mesh_suppresses_contact(self):
        policies = self._gear_policy()
        name_map = {"gear_a": "Body_A", "gear_b": "Body_B"}
        violations = [{
            "body_a": "Body_A",
            "body_b": "Body_B",
            "distance_mm": 0.0,
            "intersecting": True,
        }]
        filtered, suppressed = filter_violations(violations, policies, name_map)
        self.assertEqual(len(filtered), 0)
        self.assertEqual(len(suppressed), 1)

    def test_unrelated_pair_not_suppressed(self):
        policies = self._gear_policy()
        name_map = {"gear_a": "Body_A", "gear_b": "Body_B"}
        violations = [{
            "body_a": "Body_C",
            "body_b": "Body_D",
            "distance_mm": 0.0,
            "intersecting": True,
        }]
        filtered, suppressed = filter_violations(violations, policies, name_map)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(suppressed), 0)

    def test_pair_order_independent(self):
        """body_a/body_b order shouldn't matter."""
        policies = self._gear_policy()
        name_map = {"gear_a": "Body_A", "gear_b": "Body_B"}
        violations = [{
            "body_a": "Body_B",
            "body_b": "Body_A",
            "distance_mm": 0.0,
            "intersecting": True,
        }]
        filtered, suppressed = filter_violations(violations, policies, name_map)
        self.assertEqual(len(suppressed), 1)

    def test_press_fit_allows_limited_interference(self):
        policies = [CollisionPolicy(
            interface_id="shaft:bore->bearing:outer",
            part_a="shaft",
            part_b="bearing",
            contact_type="press_fit",
            max_penetration_mm=0.05,
            min_clearance_mm=0.0,
        )]
        name_map = {"shaft": "Body_Shaft", "bearing": "Body_Bearing"}
        violations = [{
            "body_a": "Body_Shaft",
            "body_b": "Body_Bearing",
            "distance_mm": 0.0,
            "intersecting": True,
        }]
        filtered, suppressed = filter_violations(violations, policies, name_map)
        self.assertEqual(len(suppressed), 1)

    def test_mixed_violations(self):
        """Some violations suppressed, some not."""
        policies = self._gear_policy()
        name_map = {"gear_a": "Body_A", "gear_b": "Body_B"}
        violations = [
            {"body_a": "Body_A", "body_b": "Body_B", "distance_mm": 0.0, "intersecting": True},
            {"body_a": "Body_A", "body_b": "Body_C", "distance_mm": 0.2, "intersecting": False},
        ]
        filtered, suppressed = filter_violations(violations, policies, name_map)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(filtered[0]["body_b"], "Body_C")

    def test_no_name_map_uses_part_names_directly(self):
        """When no name_map, policy part names must match body names."""
        policies = [CollisionPolicy(
            interface_id="A:->B:",
            part_a="Body_A",
            part_b="Body_B",
            contact_type="gear_mesh",
            max_penetration_mm=0.0,
            min_clearance_mm=0.1,
        )]
        violations = [{
            "body_a": "Body_A",
            "body_b": "Body_B",
            "distance_mm": 0.0,
            "intersecting": True,
        }]
        filtered, suppressed = filter_violations(violations, policies)
        self.assertEqual(len(suppressed), 1)


if __name__ == "__main__":
    unittest.main()
