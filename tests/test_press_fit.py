"""Tests for the press-fit bore dimensioning tool."""

from __future__ import annotations

import math
import unittest

from server.press_fit import press_fit_bore, _range_index, _it_value


class TestISOLookup(unittest.TestCase):
    """Verify ISO 286 tolerance table lookups."""

    def test_range_index_small(self):
        self.assertEqual(_range_index(1.0), 0)
        self.assertEqual(_range_index(3.0), 0)

    def test_range_index_mid(self):
        self.assertEqual(_range_index(10.0), 2)
        self.assertEqual(_range_index(25.0), 4)

    def test_range_index_large(self):
        self.assertEqual(_range_index(100.0), 7)
        self.assertEqual(_range_index(400.0), 11)

    def test_range_out_of_bounds(self):
        with self.assertRaises(ValueError):
            _range_index(0)
        with self.assertRaises(ValueError):
            _range_index(500)

    def test_it7_values(self):
        # IT7 at 0-3mm = 10μm
        self.assertEqual(_it_value(7, 0), 10)
        # IT7 at 18-30mm = 21μm
        self.assertEqual(_it_value(7, 4), 21)
        # IT7 at 50-80mm = 30μm
        self.assertEqual(_it_value(7, 6), 30)


class TestFitPresets(unittest.TestCase):
    """Verify preset fit types produce correct characteristics."""

    def test_press_is_interference(self):
        r = press_fit_bore(10.0, fit="press", depth=0)
        # H7/p6 should have max clearance ≤ 0
        self.assertLessEqual(r["clearance_max_um"], 0)

    def test_clearance_is_positive(self):
        r = press_fit_bore(10.0, fit="clearance", depth=0)
        # H8/f7 should have min clearance > 0
        self.assertGreater(r["clearance_min_um"], 0)

    def test_heavy_press_more_interference_than_press(self):
        rp = press_fit_bore(20.0, fit="press", depth=0)
        rh = press_fit_bore(20.0, fit="heavy_press", depth=0)
        # Heavy press should have more interference (more negative clearance)
        self.assertLess(rh["clearance_min_um"], rp["clearance_min_um"])

    def test_sliding_zero_or_positive(self):
        r = press_fit_bore(15.0, fit="sliding", depth=0)
        self.assertGreaterEqual(r["clearance_min_um"], 0)


class TestISOPairParsing(unittest.TestCase):
    """Verify direct ISO class specification."""

    def test_h7p6(self):
        r = press_fit_bore(10.0, fit="H7p6", depth=0)
        self.assertEqual(r["hole_tolerance_class"], "H7")
        self.assertEqual(r["shaft_tolerance_class"], "p6")

    def test_h7_slash_p6(self):
        r = press_fit_bore(10.0, fit="H7/p6", depth=0)
        self.assertEqual(r["hole_tolerance_class"], "H7")
        self.assertEqual(r["shaft_tolerance_class"], "p6")

    def test_hole_only(self):
        r = press_fit_bore(10.0, fit="H8", depth=0)
        self.assertEqual(r["hole_tolerance_class"], "H8")

    def test_invalid_hole_position(self):
        with self.assertRaises(ValueError):
            press_fit_bore(10.0, fit="K7p6", depth=0)


class TestBoreDimensions(unittest.TestCase):
    """Verify bore dimension calculations."""

    def test_h7_bore_range_10mm(self):
        """H7 at 10mm: tolerance = 15μm, lower dev = 0."""
        r = press_fit_bore(10.0, fit="press", depth=0)
        self.assertAlmostEqual(r["bore_diameter_min"], 10.0000, places=4)
        self.assertAlmostEqual(r["bore_diameter_max"], 10.0150, places=4)

    def test_h7_bore_range_25mm(self):
        """H7 at 25mm: tolerance = 21μm."""
        r = press_fit_bore(25.0, fit="sliding", depth=0)
        self.assertAlmostEqual(r["bore_diameter_max"] - r["bore_diameter_min"],
                               0.021, places=4)

    def test_small_bore(self):
        """H7 at 2mm (0-3 range): tolerance = 10μm."""
        r = press_fit_bore(2.0, fit="press", depth=0)
        self.assertAlmostEqual(r["hole_tolerance_um"], 10.0)

    def test_target_is_midpoint(self):
        r = press_fit_bore(10.0, fit="press", depth=0)
        expected = (r["bore_diameter_min"] + r["bore_diameter_max"]) / 2
        self.assertAlmostEqual(r["bore_diameter_target"], expected, places=4)


class TestBoreProfile(unittest.TestCase):
    """Verify bore profile generation."""

    def test_no_profile_at_zero_depth(self):
        r = press_fit_bore(10.0, fit="press", depth=0)
        self.assertEqual(len(r["elements"]), 0)

    def test_simple_bore_elements(self):
        r = press_fit_bore(10.0, fit="press", depth=15.0)
        # Simple bore: top, bottom, axis, left = 4 lines minimum
        self.assertGreaterEqual(len(r["elements"]), 4)

    def test_chamfer_adds_elements(self):
        r_plain = press_fit_bore(10.0, fit="press", depth=15.0)
        r_cham = press_fit_bore(10.0, fit="press", depth=15.0, chamfer=0.5)
        self.assertGreater(len(r_cham["elements"]), len(r_plain["elements"]))

    def test_counterbore_adds_elements(self):
        r_plain = press_fit_bore(10.0, fit="press", depth=15.0)
        r_cb = press_fit_bore(10.0, fit="press", depth=15.0,
                              counterbore_diameter=14.0, counterbore_depth=3.0)
        self.assertGreater(len(r_cb["elements"]), len(r_plain["elements"]))

    def test_counterbore_must_exceed_nominal(self):
        with self.assertRaises(ValueError):
            press_fit_bore(10.0, fit="press", depth=15.0,
                           counterbore_diameter=8.0, counterbore_depth=3.0)

    def test_profile_closure(self):
        """Profile elements should form a closed contour."""
        r = press_fit_bore(10.0, fit="press", depth=15.0, chamfer=0.3)
        elements = r["elements"]
        # Extract endpoint pairs
        pts = []
        for e in elements:
            pts.append(((e["x1"], e["y1"]), (e["x2"], e["y2"])))
        # Check consecutive connectivity
        for i in range(len(pts) - 1):
            dist = math.hypot(
                pts[i][1][0] - pts[i + 1][0][0],
                pts[i][1][1] - pts[i + 1][0][1],
            )
            self.assertLess(dist, 0.01,
                            f"Gap between element {i} and {i + 1}: {dist}")
        # Check closure
        dist = math.hypot(
            pts[-1][1][0] - pts[0][0][0],
            pts[-1][1][1] - pts[0][0][1],
        )
        self.assertLess(dist, 0.01, f"Profile not closed: gap = {dist}")


class TestCounterboreProfileClosure(unittest.TestCase):
    """Counterbore profile forms a closed contour (distinct geometry path)."""

    def test_counterbore_profile_closure(self):
        r = press_fit_bore(
            10.0, fit="press", depth=15.0,
            counterbore_diameter=14.0, counterbore_depth=3.0,
        )
        elements = r["elements"]
        self.assertGreater(len(elements), 0)
        pts = []
        for e in elements:
            pts.append(((e["x1"], e["y1"]), (e["x2"], e["y2"])))
        # Consecutive connectivity
        for i in range(len(pts) - 1):
            dist = math.hypot(
                pts[i][1][0] - pts[i + 1][0][0],
                pts[i][1][1] - pts[i + 1][0][1],
            )
            self.assertLess(dist, 0.01,
                            f"Gap between element {i} and {i + 1}: {dist}")
        # Closure
        dist = math.hypot(
            pts[-1][1][0] - pts[0][0][0],
            pts[-1][1][1] - pts[0][0][1],
        )
        self.assertLess(dist, 0.01, f"Profile not closed: gap = {dist}")


class TestInvalidFit(unittest.TestCase):
    """Invalid fit strings raise ValueError."""

    def test_invalid_fit_string(self):
        with self.assertRaises(ValueError):
            press_fit_bore(10.0, fit="banana", depth=10.0)


class TestMCPWrapper(unittest.TestCase):
    """MCP tool wrapper returns geometry_ref."""

    def test_returns_ref_with_depth(self):
        from server.tools_geometry import geometry_press_fit_bore
        r = geometry_press_fit_bore(nominal_diameter=10.0, depth=15.0)
        self.assertTrue(r["ok"])
        self.assertIn("geometry_ref", r)
        self.assertIn("bore_diameter_target", r)

    def test_no_ref_without_depth(self):
        from server.tools_geometry import geometry_press_fit_bore
        r = geometry_press_fit_bore(nominal_diameter=10.0, depth=0)
        self.assertTrue(r["ok"])
        self.assertNotIn("geometry_ref", r)
        self.assertIn("bore_diameter_target", r)


if __name__ == "__main__":
    unittest.main()
