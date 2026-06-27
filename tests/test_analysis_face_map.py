"""Tests for face mapping between FreeCAD and Gmsh."""
from __future__ import annotations

import unittest

from server.analysis_face_map import (
    _parse_face_index,
    map_face_refs_to_gmsh,
    match_faces_geometric,
)


class TestParseFaceIndex(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(_parse_face_index("Face1"), 1)
        self.assertEqual(_parse_face_index("Face42"), 42)

    def test_invalid(self) -> None:
        self.assertIsNone(_parse_face_index("Edge1"))
        self.assertIsNone(_parse_face_index("FaceABC"))


class TestMapFaceRefsToGmsh(unittest.TestCase):
    def test_index_correspondence_no_gmsh(self) -> None:
        topology = [
            {"name": "Face1", "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100},
            {"name": "Face2", "center": [0, 0, 5], "normal": [0, 0, -1], "area": 100},
            {"name": "Face3", "center": [5, 0, 2.5], "normal": [1, 0, 0], "area": 50},
        ]
        result = map_face_refs_to_gmsh(["Face1", "Face3"], topology)
        self.assertEqual(result, {"Face1": 1, "Face3": 3})

    def test_index_correspondence_with_gmsh(self) -> None:
        topology = [
            {"name": "Face1", "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100},
            {"name": "Face2", "center": [0, 0, 5], "normal": [0, 0, -1], "area": 100},
        ]
        gmsh_surfaces = [(2, 10), (2, 20)]
        result = map_face_refs_to_gmsh(["Face1", "Face2"], topology, gmsh_surfaces)
        self.assertEqual(result, {"Face1": 10, "Face2": 20})

    def test_out_of_range(self) -> None:
        topology = [{"name": "Face1", "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100}]
        gmsh_surfaces = [(2, 10)]
        result = map_face_refs_to_gmsh(["Face1", "Face5"], topology, gmsh_surfaces)
        self.assertEqual(result, {"Face1": 10})  # Face5 omitted


class TestMatchFacesGeometric(unittest.TestCase):
    def test_exact_match(self) -> None:
        topology = [
            {"name": "Face1", "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100},
            {"name": "Face2", "center": [10, 0, 0], "normal": [1, 0, 0], "area": 50},
        ]
        gmsh_data = [
            {"tag": 10, "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100},
            {"tag": 20, "center": [10, 0, 0], "normal": [1, 0, 0], "area": 50},
        ]
        result = match_faces_geometric(["Face1", "Face2"], topology, gmsh_data)
        self.assertEqual(result, {"Face1": 10, "Face2": 20})

    def test_no_match(self) -> None:
        topology = [
            {"name": "Face1", "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100},
        ]
        gmsh_data = [
            {"tag": 10, "center": [100, 100, 100], "normal": [0, 0, 1], "area": 100},
        ]
        result = match_faces_geometric(["Face1"], topology, gmsh_data, tolerance=0.1)
        self.assertEqual(result, {})

    def test_normal_mismatch(self) -> None:
        topology = [
            {"name": "Face1", "center": [0, 0, 0], "normal": [0, 0, 1], "area": 100},
        ]
        gmsh_data = [
            {"tag": 10, "center": [0, 0, 0], "normal": [1, 0, 0], "area": 100},
        ]
        result = match_faces_geometric(["Face1"], topology, gmsh_data)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
