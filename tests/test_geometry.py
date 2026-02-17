"""Tests for the solidmind_geometry Rust extension and MCP tool wrappers."""

from __future__ import annotations

import math
import unittest

try:
    import solidmind_geometry as geom

    HAS_GEOM = True
except ImportError:
    HAS_GEOM = False


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestInvolutePoints(unittest.TestCase):
    def test_returns_correct_count(self):
        pts = geom.involute_points(10.0, 10.0, 15.0, 20)
        self.assertEqual(len(pts), 20)

    def test_points_are_tuples(self):
        pts = geom.involute_points(10.0, 10.0, 15.0, 5)
        for p in pts:
            self.assertIsInstance(p, tuple)
            self.assertEqual(len(p), 2)

    def test_radii_monotonic(self):
        pts = geom.involute_points(10.0, 10.0, 15.0, 20)
        prev_r = 0.0
        for x, y in pts:
            r = math.hypot(x, y)
            self.assertGreaterEqual(r, prev_r - 1e-10)
            prev_r = r


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestGearParams(unittest.TestCase):
    def test_pitch_diameter(self):
        p = geom.gear_params(2.0, 20)
        self.assertAlmostEqual(p["pitch_diameter"], 40.0, places=10)

    def test_base_diameter(self):
        p = geom.gear_params(2.0, 20)
        expected = 40.0 * math.cos(math.radians(20.0))
        self.assertAlmostEqual(p["base_diameter"], expected, places=10)

    def test_internal_gear(self):
        p = geom.gear_params(2.0, 40, internal=True)
        self.assertLess(p["tip_diameter"], p["pitch_diameter"])
        self.assertGreater(p["root_diameter"], p["pitch_diameter"])


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestSpurGear(unittest.TestCase):
    def test_returns_elements(self):
        result = geom.spur_gear(1.0, 12)
        self.assertIn("elements", result)
        self.assertIsInstance(result["elements"], list)
        self.assertGreater(len(result["elements"]), 0)

    def test_element_count(self):
        result = geom.spur_gear(1.0, 12)
        # For 12T m=1: rf < rb → 6 elements per tooth (with radial lines)
        self.assertEqual(len(result["elements"]), 12 * 6)

    def test_elements_are_cad_sketch_dicts(self):
        result = geom.spur_gear(1.0, 18)
        for elem in result["elements"]:
            self.assertIsInstance(elem, dict)
            self.assertIn("type", elem)
            self.assertIn(elem["type"], ("spline", "arc", "line", "circle"))

    def test_spline_has_enough_points(self):
        result = geom.spur_gear(1.0, 18, num_involute_pts=20)
        for elem in result["elements"]:
            if elem["type"] == "spline":
                degree = elem["degree"]
                self.assertGreaterEqual(len(elem["points"]), degree + 1)

    def test_has_params(self):
        result = geom.spur_gear(1.25, 18)
        self.assertIn("params", result)
        params = result["params"]
        self.assertIn("pitch_diameter", params)
        self.assertAlmostEqual(params["pitch_diameter"], 1.25 * 18, places=10)

    def test_has_build_hint(self):
        result = geom.spur_gear(1.0, 12)
        self.assertIn("build_hint", result)

    def test_internal_gear(self):
        result = geom.spur_gear(1.0, 40, internal=True)
        self.assertIn("elements", result)
        self.assertEqual(len(result["elements"]), 40 * 4)

    def test_center_offset(self):
        result = geom.spur_gear(1.0, 12, center_x=10.0, center_y=20.0)
        for elem in result["elements"]:
            if elem["type"] == "arc":
                self.assertAlmostEqual(elem["cx"], 10.0, places=10)
                self.assertAlmostEqual(elem["cy"], 20.0, places=10)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestToothSlot(unittest.TestCase):
    def test_returns_correct_elements(self):
        result = geom.tooth_slot(1.0, 18)
        # For 18T m=1: rf < rb → 6 elements (with radial lines)
        self.assertEqual(len(result["elements"]), 6)

    def test_has_build_hint(self):
        result = geom.tooth_slot(1.0, 18)
        self.assertIn("build_hint", result)
        self.assertIn("polar_pattern", result["build_hint"])

    def test_has_teeth_count(self):
        result = geom.tooth_slot(1.0, 24)
        self.assertEqual(result["teeth"], 24)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestPlanetaryLayout(unittest.TestCase):
    def test_valid_layout(self):
        # sun=18, planet=9, ring=36, 3 planets
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertIn("sun", result)
        self.assertIn("planet", result)
        self.assertIn("ring", result)
        self.assertIn("planet_positions", result)

    def test_planet_positions_count(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertEqual(len(result["planet_positions"]), 3)

    def test_sun_has_elements(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertIn("elements", result["sun"])
        self.assertGreater(len(result["sun"]["elements"]), 0)

    def test_ring_teeth(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertEqual(result["params"]["ring_teeth"], 36)

    def test_assembly_condition_fail(self):
        with self.assertRaises(ValueError):
            geom.planetary_layout(1.0, 18, 10, num_planets=3)

    def test_too_few_planets(self):
        with self.assertRaises(ValueError):
            geom.planetary_layout(1.0, 18, 9, num_planets=1)

    def test_planet_positions_equidistant(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        positions = result["planet_positions"]
        r0 = math.hypot(positions[0][0], positions[0][1])
        for pos in positions[1:]:
            r = math.hypot(pos[0], pos[1])
            self.assertAlmostEqual(r, r0, places=10)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestMCPToolWrappers(unittest.TestCase):
    """Test the Python MCP tool wrappers return geometry_ref handles."""

    def setUp(self):
        from server.geometry_store import clear
        clear()

    def tearDown(self):
        from server.geometry_store import clear
        clear()

    def test_spur_gear_tool(self):
        from server.tools_geometry import geometry_spur_gear
        from server.geometry_store import retrieve

        result = geometry_spur_gear(module=1.25, teeth=18)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertNotIn("elements", result)
        # For 18T m=1.25: rf < rb → 6 elements per tooth
        self.assertEqual(result["element_count"], 18 * 6)
        # Verify ref resolves to actual elements
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)
        self.assertEqual(len(elems), 18 * 6)

    def test_gear_params_tool(self):
        from server.tools_geometry import geometry_gear_params

        result = geometry_gear_params(module=2.0, teeth=20)
        self.assertTrue(result["ok"])
        self.assertIn("params", result)
        self.assertAlmostEqual(result["params"]["pitch_diameter"], 40.0)

    def test_involute_points_tool(self):
        from server.tools_geometry import geometry_involute_points

        result = geometry_involute_points(10.0, 10.0, 15.0, 20)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["points"]), 20)

    def test_planetary_layout_tool(self):
        from server.tools_geometry import geometry_planetary_layout
        from server.geometry_store import retrieve

        result = geometry_planetary_layout(module=1.0, sun_teeth=18, planet_teeth=9)
        self.assertTrue(result["ok"])
        self.assertIn("sun", result)
        self.assertIn("planet", result)
        self.assertIn("ring", result)
        # Each gear should have geometry_ref, not elements
        for gear_key in ("sun", "planet", "ring"):
            self.assertIn("geometry_ref", result[gear_key])
            self.assertNotIn("elements", result[gear_key])
            self.assertIn("element_count", result[gear_key])
            elems = retrieve(result[gear_key]["geometry_ref"])
            self.assertIsNotNone(elems)
            self.assertEqual(len(elems), result[gear_key]["element_count"])

    def test_tooth_slot_tool(self):
        from server.tools_geometry import geometry_tooth_slot
        from server.geometry_store import retrieve

        result = geometry_tooth_slot(module=1.0, teeth=18)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertNotIn("elements", result)
        # For 18T m=1: rf < rb → 6 elements (with radial lines)
        self.assertEqual(result["element_count"], 6)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)
        self.assertEqual(len(elems), 6)


if __name__ == "__main__":
    unittest.main()
