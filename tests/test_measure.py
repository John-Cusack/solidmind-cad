"""Unit tests for ``orchestrator.measure`` feature-measurement strategies.

These don't require a running FreeCAD addon — they call the strategy
functions directly with a mocked ``cad`` module that returns canned
responses for ``cad_find_holes``, ``cad_get_body_topology``, and
``cad_get_dimensions``.  The integration coverage (real STEP import +
re-measure) lives in ``tests/test_orchestrator_real_worker_e2e.py``.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.measure import (
    _FEATURE_STRATEGIES,
    _measure_bbox_diagonal,
    _measure_bore_diameter,
    _measure_pin_circle_diameter,
    _measure_pocket_depth,
    _measure_segment_length,
)


def _mock_cad(
    *,
    holes: list[dict] | None = None,
    faces: list[dict] | None = None,
    bbox: dict | None = None,
):
    """Build a mock ``cad`` module exposing the three functions strategies use."""
    holes_response = {"holes": holes or []}
    topo_response = {"faces": faces or [], "edges": []}
    dims_response = {"bounding_box": bbox or {}}
    return SimpleNamespace(
        cad_find_holes=lambda **kwargs: holes_response,
        cad_get_body_topology=lambda **kwargs: topo_response,
        cad_get_dimensions=lambda **kwargs: dims_response,
    )


class TestStrategyRegistry(unittest.TestCase):
    """The strategy registry should expose all the keys builders use."""

    def test_registered_keys(self) -> None:
        for key in [
            # Bore family
            "bore_diameter", "bore_dia", "central_bore_dia",
            "axle_bore_dia", "hip_yaw_bore_dia", "hip_pitch_bore_dia",
            "knee_bore_dia",
            # PCD family
            "pin_circle_dia", "pcd_diameter", "bolt_circle_dia",
            "motor_mount_pcd", "mounting_pcd",
            # Pocket family
            "pocket_depth", "servo_pocket_depth",
            # Segment family
            "segment_length", "coxa_length", "femur_length", "tibia_length",
            # Bbox
            "bbox_diagonal",
        ]:
            self.assertIn(key, _FEATURE_STRATEGIES, f"strategy missing: {key}")


class TestMeasureBoreDiameter(unittest.TestCase):
    """Smoke-test the existing strategy still behaves after registry changes."""

    def test_picks_closest_to_expected(self) -> None:
        cad = _mock_cad(holes=[
            {"diameter_mm": 8.0},
            {"diameter_mm": 17.5},
            {"diameter_mm": 22.0},
        ])
        # Expected 8 mm: pick 8.0
        self.assertEqual(
            _measure_bore_diameter(cad, "Body", "Doc", expected_mm=8.0),
            8.0,
        )
        # Expected 22 mm: pick 22.0
        self.assertEqual(
            _measure_bore_diameter(cad, "Body", "Doc", expected_mm=22.0),
            22.0,
        )

    def test_no_hint_picks_smallest(self) -> None:
        cad = _mock_cad(holes=[
            {"diameter_mm": 22.0},
            {"diameter_mm": 8.0},
            {"diameter_mm": 17.5},
        ])
        self.assertEqual(
            _measure_bore_diameter(cad, "Body", "Doc"),
            8.0,
        )

    def test_empty_holes_returns_none(self) -> None:
        cad = _mock_cad(holes=[])
        self.assertIsNone(_measure_bore_diameter(cad, "Body", "Doc"))


class TestMeasurePinCircleDiameter(unittest.TestCase):
    """PCD strategy: planet_carrier and motor-mount hole patterns."""

    def test_three_pin_carrier(self) -> None:
        """Planet carrier: 3 pins at PCD=22, all 4 mm diameter."""
        # Planet carrier has central bore (8 mm at origin) + 3 pins (4 mm) at 120°.
        import math
        pcd = 22.0
        r = pcd / 2
        pin_centers = [
            (r * math.cos(math.radians(angle)), r * math.sin(math.radians(angle)), 5.0)
            for angle in (90, 210, 330)
        ]
        holes = [
            # Central bore
            {"diameter_mm": 8.0, "center": [0.0, 0.0, 0.0]},
        ] + [
            {"diameter_mm": 4.0, "center": [x, y, z]}
            for (x, y, z) in pin_centers
        ]
        cad = _mock_cad(holes=holes)

        # With expected hint: picks the 22.0 PCD group (the 4-mm pins).
        result = _measure_pin_circle_diameter(
            cad, "Body", "Doc", expected_mm=22.0,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 22.0, places=2)

    def test_four_hole_motor_mount_square(self) -> None:
        """Square motor-mount pattern: 4 holes at corners of 16 mm square.

        PCD for a square pattern = side × √2 = 16 × 1.4142 ≈ 22.6 mm.
        """
        import math
        side = 16.0
        half = side / 2
        # 4 holes at (±half, ±half), all 3.2 mm
        holes = [
            {"diameter_mm": 3.2, "center": [half, half, 5.0]},
            {"diameter_mm": 3.2, "center": [-half, half, 5.0]},
            {"diameter_mm": 3.2, "center": [-half, -half, 5.0]},
            {"diameter_mm": 3.2, "center": [half, -half, 5.0]},
        ]
        cad = _mock_cad(holes=holes)
        expected_pcd = side * math.sqrt(2)
        result = _measure_pin_circle_diameter(
            cad, "Body", "Doc", expected_mm=expected_pcd,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, expected_pcd, places=2)

    def test_dedup_segmented_faces(self) -> None:
        """Same pin reported as multiple cylindrical face segments at same XY.

        After STEP import a pin's cylindrical face can be split into
        several segments. Dedup-by-position must collapse them so the
        PCD calculation isn't biased by duplicate counts.
        """
        # Three pins at PCD=22, but each appears as 4 face segments.
        import math
        pcd = 22.0
        r = pcd / 2
        holes: list[dict] = []
        for angle in (90, 210, 330):
            x = r * math.cos(math.radians(angle))
            y = r * math.sin(math.radians(angle))
            for seg_z in (0.0, 1.5, 3.0, 4.5):
                holes.append({
                    "diameter_mm": 4.0,
                    "center": [x, y, seg_z],
                })
        cad = _mock_cad(holes=holes)
        result = _measure_pin_circle_diameter(
            cad, "Body", "Doc", expected_mm=22.0,
        )
        # Without dedup the centroid drift would corrupt this; with it,
        # we get the true PCD back.
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 22.0, places=2)

    def test_too_few_holes_returns_none(self) -> None:
        """Need at least 3 unique centers for a circle."""
        cad = _mock_cad(holes=[
            {"diameter_mm": 4.0, "center": [10.0, 0.0, 0.0]},
            {"diameter_mm": 4.0, "center": [-10.0, 0.0, 0.0]},
        ])
        self.assertIsNone(_measure_pin_circle_diameter(cad, "Body", "Doc"))

    def test_no_hint_picks_largest_pattern(self) -> None:
        """With no expected hint, the strategy picks the densest group."""
        import math
        # 3-hole group at PCD=20, 6-hole group at PCD=40 (different dia).
        small_r = 10.0
        big_r = 20.0
        small_holes = [
            {
                "diameter_mm": 3.0,
                "center": [
                    small_r * math.cos(math.radians(a)),
                    small_r * math.sin(math.radians(a)),
                    0.0,
                ],
            }
            for a in (0, 120, 240)
        ]
        big_holes = [
            {
                "diameter_mm": 5.0,
                "center": [
                    big_r * math.cos(math.radians(a)),
                    big_r * math.sin(math.radians(a)),
                    0.0,
                ],
            }
            for a in range(0, 360, 60)  # 6 holes
        ]
        cad = _mock_cad(holes=small_holes + big_holes)
        result = _measure_pin_circle_diameter(cad, "Body", "Doc")
        # 6-hole group wins.
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 40.0, places=2)


class TestMeasurePocketDepth(unittest.TestCase):
    """Pocket-depth strategy: top face minus pocket floor."""

    def test_single_pocket(self) -> None:
        """Body with top at z=8, pocket floor at z=4 -> depth=4 mm."""
        faces = [
            # Bottom of body, normal pointing down (-Z)
            {
                "surface_type": "Plane",
                "normal": [0.0, 0.0, -1.0],
                "center": [0.0, 0.0, 0.0],
                "area": 100.0,
            },
            # Body top (donut around the pocket), normal +Z, z=8
            {
                "surface_type": "Plane",
                "normal": [0.0, 0.0, 1.0],
                "center": [0.0, 0.0, 8.0],
                "area": 80.0,
            },
            # Pocket floor (small rectangle), normal +Z, z=4
            {
                "surface_type": "Plane",
                "normal": [0.0, 0.0, 1.0],
                "center": [0.0, 0.0, 4.0],
                "area": 20.0,
            },
            # Side wall, vertical normal — must be filtered out
            {
                "surface_type": "Plane",
                "normal": [1.0, 0.0, 0.0],
                "center": [5.0, 0.0, 4.0],
                "area": 40.0,
            },
        ]
        cad = _mock_cad(faces=faces)
        depth = _measure_pocket_depth(cad, "Body", "Doc", expected_mm=4.0)
        self.assertIsNotNone(depth)
        self.assertAlmostEqual(depth, 4.0, places=2)

    def test_multiple_pockets_picks_closest_to_expected(self) -> None:
        """Three pockets at depths 2, 4, 6. expected=4 -> picks 4."""
        faces = [
            # Top
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 8], "area": 100},
            # Three pockets
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 6], "area": 10},
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 4], "area": 10},
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 2], "area": 10},
        ]
        cad = _mock_cad(faces=faces)
        self.assertAlmostEqual(
            _measure_pocket_depth(cad, "Body", "Doc", expected_mm=4.0),
            4.0,
        )
        self.assertAlmostEqual(
            _measure_pocket_depth(cad, "Body", "Doc", expected_mm=2.5),
            2.0,
        )

    def test_no_pocket_returns_none(self) -> None:
        """Solid body with only top + bottom -> no pocket -> None."""
        faces = [
            {"surface_type": "Plane", "normal": [0, 0, -1], "center": [0, 0, 0], "area": 100},
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 8], "area": 100},
        ]
        cad = _mock_cad(faces=faces)
        self.assertIsNone(_measure_pocket_depth(cad, "Body", "Doc"))

    def test_no_hint_picks_deepest(self) -> None:
        faces = [
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 8], "area": 100},
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 6], "area": 10},
            {"surface_type": "Plane", "normal": [0, 0, 1], "center": [0, 0, 2], "area": 10},
        ]
        cad = _mock_cad(faces=faces)
        self.assertAlmostEqual(
            _measure_pocket_depth(cad, "Body", "Doc"),
            6.0,  # 8 - 2
        )


class TestMeasureSegmentLength(unittest.TestCase):
    def test_max_of_x_and_y(self) -> None:
        cad = _mock_cad(bbox={"x_len": 50.0, "y_len": 250.0, "z_len": 8.0})
        self.assertEqual(_measure_segment_length(cad, "Body", "Doc"), 250.0)

    def test_returns_none_when_bbox_missing(self) -> None:
        cad = _mock_cad(bbox={})
        self.assertIsNone(_measure_segment_length(cad, "Body", "Doc"))


class TestMeasureBboxDiagonal(unittest.TestCase):
    """The fixed bbox strategy now reads from cad_get_dimensions."""

    def test_returns_max_of_three_dims(self) -> None:
        cad = _mock_cad(bbox={"x_len": 22.0, "y_len": 22.0, "z_len": 8.0})
        self.assertEqual(_measure_bbox_diagonal(cad, "Body", "Doc"), 22.0)


if __name__ == "__main__":
    unittest.main()
