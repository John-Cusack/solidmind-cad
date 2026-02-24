"""Pure-Python tests for sketch element parameter normalization."""
from __future__ import annotations

import unittest

from server.sketch_elements import normalize_elements


class TestRectNormalization(unittest.TestCase):
    def test_width_height_aliases(self) -> None:
        elems = [{"type": "rect", "x": 0, "y": 0, "width": 100, "height": 50}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["w"], 100)
        self.assertEqual(elems[0]["h"], 50)
        self.assertNotIn("width", elems[0])
        self.assertNotIn("height", elems[0])

    def test_canonical_unchanged(self) -> None:
        elems = [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["w"], 100)
        self.assertEqual(elems[0]["h"], 50)


class TestCircleNormalization(unittest.TestCase):
    def test_center_array(self) -> None:
        elems = [{"type": "circle", "center": [10, 20], "r": 5}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 10)
        self.assertEqual(elems[0]["cy"], 20)
        self.assertNotIn("center", elems[0])

    def test_center_x_y_aliases(self) -> None:
        elems = [{"type": "circle", "center_x": 10, "center_y": 20, "r": 5}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 10)
        self.assertEqual(elems[0]["cy"], 20)
        self.assertNotIn("center_x", elems[0])

    def test_radius_alias(self) -> None:
        elems = [{"type": "circle", "cx": 0, "cy": 0, "radius": 15}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["r"], 15)
        self.assertNotIn("radius", elems[0])

    def test_canonical_unchanged(self) -> None:
        elems = [{"type": "circle", "cx": 5, "cy": 10, "r": 3}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 5)
        self.assertEqual(elems[0]["cy"], 10)
        self.assertEqual(elems[0]["r"], 3)


class TestLineNormalization(unittest.TestCase):
    def test_start_end_arrays(self) -> None:
        elems = [{"type": "line", "start": [0, 0], "end": [100, 50]}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["x1"], 0)
        self.assertEqual(elems[0]["y1"], 0)
        self.assertEqual(elems[0]["x2"], 100)
        self.assertEqual(elems[0]["y2"], 50)
        self.assertNotIn("start", elems[0])
        self.assertNotIn("end", elems[0])

    def test_canonical_unchanged(self) -> None:
        elems = [{"type": "line", "x1": 1, "y1": 2, "x2": 3, "y2": 4}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["x1"], 1)
        self.assertEqual(elems[0]["x2"], 3)


class TestArcNormalization(unittest.TestCase):
    def test_center_array(self) -> None:
        elems = [{"type": "arc", "center": [10, 20], "r": 5,
                  "start_angle": 0, "end_angle": 90}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 10)
        self.assertEqual(elems[0]["cy"], 20)
        self.assertNotIn("center", elems[0])

    def test_center_x_y_aliases(self) -> None:
        elems = [{"type": "arc", "center_x": 3, "center_y": 4, "radius": 8,
                  "start_angle": 0, "end_angle": 180}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 3)
        self.assertEqual(elems[0]["cy"], 4)
        self.assertEqual(elems[0]["r"], 8)


class TestPassthrough(unittest.TestCase):
    def test_unknown_type_passthrough(self) -> None:
        elems = [{"type": "fancy_widget", "foo": 42, "bar": "baz"}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["foo"], 42)
        self.assertEqual(elems[0]["bar"], "baz")

    def test_extra_keys_preserved(self) -> None:
        elems = [{"type": "circle", "center": [1, 2], "r": 5,
                  "construction": True}]
        normalize_elements(elems)
        self.assertTrue(elems[0]["construction"])
        self.assertEqual(elems[0]["cx"], 1)

    def test_empty_list(self) -> None:
        result = normalize_elements([])
        self.assertEqual(result, [])

    def test_returns_same_list(self) -> None:
        elems = [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]
        result = normalize_elements(elems)
        self.assertIs(result, elems)


class TestEdgeCases(unittest.TestCase):
    def test_canonical_takes_precedence_over_center_array(self) -> None:
        """If both cx and center are present, cx wins (setdefault)."""
        elems = [{"type": "circle", "cx": 99, "cy": 88, "center": [1, 2], "r": 5}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 99)
        self.assertEqual(elems[0]["cy"], 88)

    def test_canonical_takes_precedence_over_alias(self) -> None:
        """If both w and width are present, w wins."""
        elems = [{"type": "rect", "x": 0, "y": 0, "w": 100, "width": 200, "h": 50}]
        normalize_elements(elems)
        self.assertEqual(elems[0]["w"], 100)

    def test_multiple_elements(self) -> None:
        elems = [
            {"type": "circle", "center": [10, 20], "r": 5},
            {"type": "rect", "x": 0, "y": 0, "width": 30, "height": 40},
            {"type": "line", "start": [0, 0], "end": [10, 10]},
        ]
        normalize_elements(elems)
        self.assertEqual(elems[0]["cx"], 10)
        self.assertEqual(elems[1]["w"], 30)
        self.assertEqual(elems[2]["x2"], 10)


if __name__ == "__main__":
    unittest.main()
