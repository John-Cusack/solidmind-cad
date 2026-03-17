"""Tests for the turned profile generator."""

from __future__ import annotations

import math
import unittest

from server.turned_profile import turned_profile


class TestTurnedProfileBasic(unittest.TestCase):
    """Basic solid shaft profiles."""

    def test_single_cylinder(self):
        """Single segment → 4 elements (left, top, right, bottom)."""
        r = turned_profile(segments=[{"diameter": 10, "length": 20}])
        self.assertEqual(len(r["elements"]), 4)
        self.assertAlmostEqual(r["total_length"], 20.0)
        self.assertAlmostEqual(r["max_diameter"], 10.0)
        self.assertAlmostEqual(r["bore_diameter"], 0.0)
        # Volume of cylinder: π * 5² * 20 ≈ 1570.8
        self.assertAlmostEqual(r["volume_mm3"], math.pi * 25 * 20, places=1)

    def test_two_step_shaft(self):
        """Two segments with different diameters — shoulder step."""
        r = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15},
        ])
        # left edge + seg1 body + shoulder + seg2 body + right edge + bottom = 6
        self.assertEqual(len(r["elements"]), 6)
        self.assertAlmostEqual(r["total_length"], 25.0)
        self.assertAlmostEqual(r["max_diameter"], 10.0)
        self.assertAlmostEqual(r["min_diameter"], 6.0)

    def test_same_diameter_segments(self):
        """Adjacent segments at the same diameter — no transition."""
        r = turned_profile(segments=[
            {"diameter": 8, "length": 10},
            {"diameter": 8, "length": 10},
        ])
        # left + seg1 body + seg2 body + right + bottom = 5
        self.assertEqual(len(r["elements"]), 5)
        self.assertAlmostEqual(r["total_length"], 20.0)


class TestTurnedProfileBore(unittest.TestCase):
    """Profiles with center bore."""

    def test_hollow_cylinder(self):
        """Single hollow segment."""
        r = turned_profile(
            segments=[{"diameter": 20, "length": 30}],
            bore_diameter=10,
        )
        self.assertAlmostEqual(r["bore_diameter"], 10.0)
        # Volume = π * (10² - 5²) * 30
        expected = math.pi * (100 - 25) * 30
        self.assertAlmostEqual(r["volume_mm3"], expected, places=1)

    def test_bore_exceeds_segment_raises(self):
        """Bore larger than a segment diameter is rejected."""
        with self.assertRaises(ValueError):
            turned_profile(
                segments=[{"diameter": 6, "length": 10}],
                bore_diameter=8,
            )


class TestTurnedProfileTransitions(unittest.TestCase):
    """Fillets and chamfers at junctions."""

    def test_step_up_fillet(self):
        """Fillet at step-up adds an arc element."""
        r = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15, "fillet": 1.0},
        ])
        types = [e["type"] for e in r["elements"]]
        self.assertIn("arc", types)

    def test_step_down_fillet(self):
        """Fillet at step-down adds an arc element."""
        r = turned_profile(segments=[
            {"diameter": 10, "length": 15},
            {"diameter": 6, "length": 10, "fillet": 1.0},
        ])
        types = [e["type"] for e in r["elements"]]
        self.assertIn("arc", types)

    def test_step_up_chamfer(self):
        """Chamfer at step-up adds extra line elements."""
        r_sharp = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15},
        ])
        r_cham = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15, "chamfer": 0.5},
        ])
        # Chamfer replaces 1 vertical line with 2 lines (chamfer + partial vertical)
        self.assertGreater(len(r_cham["elements"]), len(r_sharp["elements"]))

    def test_fillet_clamped_to_step(self):
        """Fillet larger than step height is clamped silently."""
        r = turned_profile(segments=[
            {"diameter": 8, "length": 10},
            {"diameter": 10, "length": 10, "fillet": 5.0},  # step is only 1mm
        ])
        # Should not raise — fillet clamped to 1.0
        types = [e["type"] for e in r["elements"]]
        self.assertIn("arc", types)

    def test_fillet_and_chamfer_rejects(self):
        """Cannot specify both fillet and chamfer on same segment."""
        with self.assertRaises(ValueError):
            turned_profile(segments=[
                {"diameter": 6, "length": 10},
                {"diameter": 10, "length": 15, "fillet": 0.5, "chamfer": 0.3},
            ])


class TestTurnedProfileEndChamfers(unittest.TestCase):
    """Lead and trail chamfers."""

    def test_lead_chamfer(self):
        """Lead chamfer adds an extra line element."""
        r_plain = turned_profile(
            segments=[{"diameter": 10, "length": 20}],
        )
        r_cham = turned_profile(
            segments=[{"diameter": 10, "length": 20}],
            lead_chamfer=0.5,
        )
        self.assertGreater(len(r_cham["elements"]), len(r_plain["elements"]))

    def test_trail_chamfer(self):
        """Trail chamfer adds extra line elements."""
        r_plain = turned_profile(
            segments=[{"diameter": 10, "length": 20}],
        )
        r_cham = turned_profile(
            segments=[{"diameter": 10, "length": 20}],
            trail_chamfer=0.5,
        )
        self.assertGreater(len(r_cham["elements"]), len(r_plain["elements"]))


class TestTurnedProfileTaper(unittest.TestCase):
    """Tapered (conical) segments."""

    def test_taper_metadata(self):
        """Taper segment reports correct min diameter."""
        r = turned_profile(segments=[
            {"diameter": 10, "length": 20, "taper_diameter": 6},
        ])
        self.assertAlmostEqual(r["min_diameter"], 6.0)
        self.assertAlmostEqual(r["max_diameter"], 10.0)

    def test_taper_volume(self):
        """Taper volume matches frustum formula."""
        r = turned_profile(segments=[
            {"diameter": 10, "length": 20, "taper_diameter": 6},
        ])
        # Frustum: π/3 * L * (r1² + r2² + r1*r2)
        expected = math.pi / 3 * 20 * (25 + 9 + 15)
        self.assertAlmostEqual(r["volume_mm3"], expected, places=1)


class TestTurnedProfileValidation(unittest.TestCase):
    """Input validation."""

    def test_empty_segments_raises(self):
        with self.assertRaises(ValueError):
            turned_profile(segments=[])

    def test_missing_diameter_raises(self):
        with self.assertRaises(ValueError):
            turned_profile(segments=[{"length": 10}])

    def test_missing_length_raises(self):
        with self.assertRaises(ValueError):
            turned_profile(segments=[{"diameter": 10}])

    def test_negative_diameter_raises(self):
        with self.assertRaises(ValueError):
            turned_profile(segments=[{"diameter": -5, "length": 10}])


class TestTurnedProfileMCPWrapper(unittest.TestCase):
    """MCP tool wrapper returns geometry_ref."""

    def test_returns_geometry_ref(self):
        from server.tools_geometry import geometry_turned_profile

        r = geometry_turned_profile(
            segments=[
                {"diameter": 6, "length": 5},
                {"diameter": 10, "length": 20, "fillet": 0.5},
            ],
        )
        self.assertTrue(r["ok"])
        self.assertIn("geometry_ref", r)
        self.assertTrue(r["geometry_ref"].startswith("geo_"))
        self.assertGreater(r["element_count"], 0)
        self.assertIn("total_length", r)
        self.assertIn("build_hint", r)


class TestTurnedProfileTaperStepUp(unittest.TestCase):
    """Taper followed by step-up uses taper end diameter at junction."""

    def test_taper_then_step_up(self):
        """Taper from 10→6 followed by step-up to 12."""
        r = turned_profile(segments=[
            {"diameter": 10, "length": 20, "taper_diameter": 6},
            {"diameter": 12, "length": 10},
        ])
        # Should have: left edge, taper body, step-up vertical, seg2 body,
        # right edge, bottom = 6 elements minimum
        self.assertGreaterEqual(len(r["elements"]), 6)
        self.assertAlmostEqual(r["total_length"], 30.0)
        self.assertAlmostEqual(r["max_diameter"], 12.0)
        self.assertAlmostEqual(r["min_diameter"], 6.0)

    def test_taper_then_step_up_closed(self):
        """Taper→step-up profile forms a closed contour."""
        r = turned_profile(segments=[
            {"diameter": 10, "length": 20, "taper_diameter": 6},
            {"diameter": 12, "length": 10},
        ])
        elements = r["elements"]
        pts = []
        for e in elements:
            if e["type"] == "line":
                pts.append(((e["x1"], e["y1"]), (e["x2"], e["y2"])))
            elif e["type"] == "arc":
                cx, cy, radius = e["cx"], e["cy"], e["r"]
                sa = math.radians(e["start_angle"])
                ea = math.radians(e["end_angle"])
                pts.append((
                    (cx + radius * math.cos(sa), cy + radius * math.sin(sa)),
                    (cx + radius * math.cos(ea), cy + radius * math.sin(ea)),
                ))
        for i in range(len(pts) - 1):
            dist = math.hypot(
                pts[i][1][0] - pts[i + 1][0][0],
                pts[i][1][1] - pts[i + 1][0][1],
            )
            self.assertLess(dist, 0.01,
                            f"Gap between element {i} and {i + 1}: {dist}")
        dist = math.hypot(
            pts[-1][1][0] - pts[0][0][0],
            pts[-1][1][1] - pts[0][0][1],
        )
        self.assertLess(dist, 0.01, f"Profile not closed: gap = {dist}")


class TestTurnedProfileClosure(unittest.TestCase):
    """Profile forms a closed contour (first point == last point)."""

    def _endpoints(self, elements: list[dict]) -> list[tuple[float, float]]:
        """Extract start and end points from elements in order."""
        pts = []
        for e in elements:
            if e["type"] == "line":
                pts.append(((e["x1"], e["y1"]), (e["x2"], e["y2"])))
            elif e["type"] == "arc":
                # Arc endpoints computed from center + radius + angles
                cx, cy, r = e["cx"], e["cy"], e["r"]
                sa = math.radians(e["start_angle"])
                ea = math.radians(e["end_angle"])
                pts.append((
                    (cx + r * math.cos(sa), cy + r * math.sin(sa)),
                    (cx + r * math.cos(ea), cy + r * math.sin(ea)),
                ))
        return pts

    def _assert_closed(self, elements: list[dict], tol: float = 0.01):
        """Check that consecutive elements connect and the loop closes."""
        pts = self._endpoints(elements)
        self.assertTrue(len(pts) >= 3, "Need at least 3 elements")
        for i in range(len(pts) - 1):
            end = pts[i][1]
            start = pts[i + 1][0]
            dist = math.hypot(end[0] - start[0], end[1] - start[1])
            self.assertLess(
                dist, tol,
                f"Gap between element {i} end {end} and element {i+1} "
                f"start {start}: {dist:.4f}",
            )
        # Check closure: last element end == first element start
        dist = math.hypot(
            pts[-1][1][0] - pts[0][0][0],
            pts[-1][1][1] - pts[0][0][1],
        )
        self.assertLess(dist, tol, f"Profile not closed: gap = {dist:.4f}")

    def test_simple_cylinder_closed(self):
        r = turned_profile(segments=[{"diameter": 10, "length": 20}])
        self._assert_closed(r["elements"])

    def test_stepped_shaft_closed(self):
        r = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15},
            {"diameter": 8, "length": 5},
        ])
        self._assert_closed(r["elements"])

    def test_fillet_shaft_closed(self):
        r = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15, "fillet": 0.5},
            {"diameter": 8, "length": 5, "fillet": 0.3},
        ])
        self._assert_closed(r["elements"])

    def test_chamfer_shaft_closed(self):
        r = turned_profile(segments=[
            {"diameter": 6, "length": 10},
            {"diameter": 10, "length": 15, "chamfer": 0.5},
            {"diameter": 8, "length": 5, "chamfer": 0.3},
        ])
        self._assert_closed(r["elements"])

    def test_full_featured_closed(self):
        r = turned_profile(
            segments=[
                {"diameter": 6, "length": 5},
                {"diameter": 10, "length": 20, "fillet": 0.5},
                {"diameter": 10, "length": 5, "taper_diameter": 8},
                {"diameter": 8, "length": 8, "chamfer": 0.3},
            ],
            bore_diameter=3,
            lead_chamfer=0.3,
            trail_chamfer=0.3,
        )
        self._assert_closed(r["elements"])

    def test_step_down_fillet_closed(self):
        r = turned_profile(segments=[
            {"diameter": 10, "length": 15},
            {"diameter": 6, "length": 10, "fillet": 1.0},
        ])
        self._assert_closed(r["elements"])


if __name__ == "__main__":
    unittest.main()
