"""Tests for the CAD MCP tool implementations.

These tests mock the FreeCAD client to test tool logic without a live
FreeCAD instance.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from server.tools_cad import (
    cad_chamfer,
    cad_define_selection,
    cad_delete_selection,
    cad_export,
    cad_fillet,
    cad_find_edges,
    cad_get_body_topology,
    cad_get_dimensions,
    cad_get_model_tree,
    cad_get_selection,
    cad_hole,
    cad_list_selections,
    cad_new_body,
    cad_new_document,
    cad_pad,
    cad_pocket,
    cad_polar_pattern,
    cad_resolve_selection,
    cad_revolution,
    cad_sketch,
    cad_undo,
)


def _mock_client() -> MagicMock:
    """Create a mock FreeCAD client."""
    client = MagicMock()
    client.is_connected = True
    return client


class TestCadNewDocument(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_creates_document(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"name": "MyDoc", "label": "MyDoc"}
        mock_get.return_value = client

        result = cad_new_document(name="MyDoc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "MyDoc")
        client.send_command.assert_called_once_with("new_document", name="MyDoc")


class TestCadNewBody(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_creates_body(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"name": "Body", "label": "Body"}
        mock_get.return_value = client

        result = cad_new_body(name="Body")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Body")


class TestCadSketch(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_sketch_with_rect(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {"sketch": "Sketch", "geometry_indices": [0, 1, 2, 3]},  # sketch_rect
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},  # close_sketch
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            plane="XY",
            elements=[{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["sketch"], "Sketch")
        self.assertEqual(len(result["geometry"]), 1)
        self.assertEqual(result["geometry"][0]["type"], "rect")

    @patch("server.tools_cad.get_client")
    def test_sketch_with_circle(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {"sketch": "Sketch", "geometry_index": 0},
            {"sketch": "Sketch", "fully_constrained": False, "open_vertices": 0},
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{"type": "circle", "cx": 50, "cy": 25, "r": 10}],
        )
        self.assertTrue(result["ok"])

    @patch("server.tools_cad.get_client")
    def test_sketch_invalid_element_type(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"sketch": "Sketch"}
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{"type": "hexagon"}],
        )
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_ELEMENT", result["error"]["code"])


class TestCadPad(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_pad(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pad", "label": "Pad", "type": "PartDesign::Pad",
            "bounding_box": {"x_len": 100, "y_len": 50, "z_len": 20},
            "volume": 100000,
        }
        mock_get.return_value = client

        result = cad_pad(sketch="Sketch", length=20)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Pad")


class TestCadRevolution(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_revolution_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Revolution", "label": "Revolution", "type": "PartDesign::Revolution",
            "bounding_box": {"x_len": 60, "y_len": 60, "z_len": 35},
            "volume": 50000,
        }
        mock_get.return_value = client

        result = cad_revolution(sketch="Sketch")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Revolution")
        client.send_command.assert_called_once_with(
            "revolution", sketch="Sketch", axis="V", angle=360.0,
            symmetric=False, reversed=False,
        )

    @patch("server.tools_cad.get_client")
    def test_revolution_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Revolution", "label": "Revolution", "type": "PartDesign::Revolution",
        }
        mock_get.return_value = client

        result = cad_revolution(
            sketch="Sketch", axis="Base_Z", angle=180.0,
            symmetric=True, reversed=True, doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "revolution", sketch="Sketch", axis="Base_Z", angle=180.0,
            symmetric=True, reversed=True, doc="MyDoc",
        )


class TestCadPolarPattern(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_polar_pattern_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "PolarPattern", "label": "PolarPattern",
            "type": "PartDesign::PolarPattern",
        }
        mock_get.return_value = client

        result = cad_polar_pattern(features=["Pocket"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "PolarPattern")
        client.send_command.assert_called_once_with(
            "polar_pattern", features=["Pocket"], axis="Base_Z",
            occurrences=6, angle=360.0, reversed=False,
        )

    @patch("server.tools_cad.get_client")
    def test_polar_pattern_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "PolarPattern", "label": "PolarPattern",
            "type": "PartDesign::PolarPattern",
        }
        mock_get.return_value = client

        result = cad_polar_pattern(
            features=["Pocket", "Pocket001"], axis="Base_X",
            occurrences=11, angle=360.0, body="Body", doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "polar_pattern", features=["Pocket", "Pocket001"],
            axis="Base_X", occurrences=11, angle=360.0, reversed=False,
            body="Body", doc="MyDoc",
        )


class TestCadPocket(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_pocket(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5)
        self.assertTrue(result["ok"])


class TestCadHole(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_hole(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Hole", "label": "Hole", "type": "PartDesign::Hole",
        }
        mock_get.return_value = client

        result = cad_hole(face="Face6", diameter=6.6, depth=10)
        self.assertTrue(result["ok"])


class TestCadFillet(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_fillet(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Fillet", "label": "Fillet", "type": "PartDesign::Fillet",
        }
        mock_get.return_value = client

        result = cad_fillet(edges=["Edge1", "Edge3"], radius=2.0)
        self.assertTrue(result["ok"])


class TestCadChamfer(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_chamfer(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Chamfer", "label": "Chamfer", "type": "PartDesign::Chamfer",
        }
        mock_get.return_value = client

        result = cad_chamfer(edges=["Edge5"], size=1.0)
        self.assertTrue(result["ok"])


class TestCadGetDimensions(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_get_dimensions(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "object": "Pad",
            "bounding_box": {
                "x_min": 0, "y_min": 0, "z_min": 0,
                "x_max": 100, "y_max": 50, "z_max": 20,
                "x_len": 100, "y_len": 50, "z_len": 20,
            },
            "num_faces": 6,
            "num_edges": 12,
            "num_vertices": 8,
            "volume": 100000,
            "surface_area": 22000,
        }
        mock_get.return_value = client

        result = cad_get_dimensions(object_name="Pad")
        self.assertTrue(result["ok"])
        self.assertEqual(result["num_faces"], 6)
        self.assertEqual(result["num_edges"], 12)
        self.assertEqual(result["volume"], 100000)
        client.send_command.assert_called_once_with("get_dimensions", object_name="Pad")

    @patch("server.tools_cad.get_client")
    def test_get_dimensions_with_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"object": "Pad", "num_faces": 6, "num_edges": 12, "num_vertices": 8}
        mock_get.return_value = client

        result = cad_get_dimensions(object_name="Pad", doc="MyDoc")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("get_dimensions", object_name="Pad", doc="MyDoc")


class TestCadGetBodyTopology(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_get_body_topology(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body",
            "tip_feature": "Pad",
            "num_faces": 6,
            "num_edges": 12,
            "faces": [
                {"name": "Face1", "surface_type": "Plane", "normal": [0, 0, 1], "center": [50, 25, 20], "area": 5000.0},
                {"name": "Face2", "surface_type": "Plane", "normal": [0, 0, -1], "center": [50, 25, 0], "area": 5000.0},
            ],
            "edges": [
                {"name": "Edge1", "curve_type": "Line", "length": 100.0, "start": [0, 0, 20], "end": [100, 0, 20]},
                {"name": "Edge2", "curve_type": "Line", "length": 50.0, "start": [100, 0, 20], "end": [100, 50, 20]},
            ],
        }
        mock_get.return_value = client

        result = cad_get_body_topology()
        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "Body")
        self.assertEqual(result["num_faces"], 6)
        self.assertEqual(result["num_edges"], 12)
        self.assertEqual(len(result["faces"]), 2)
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(result["faces"][0]["surface_type"], "Plane")
        self.assertEqual(result["edges"][0]["curve_type"], "Line")
        client.send_command.assert_called_once_with("get_body_topology")

    @patch("server.tools_cad.get_client")
    def test_get_body_topology_with_body(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body2", "tip_feature": "Pad", "num_faces": 6, "num_edges": 12, "faces": [], "edges": [],
        }
        mock_get.return_value = client

        result = cad_get_body_topology(body="Body2")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("get_body_topology", body="Body2")


class TestCadGetSelection(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_get_selection(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "selections": [
                {
                    "object_name": "Body",
                    "sub_elements": [
                        {"name": "Face6", "type": "face", "normal": [0, 0, 1]},
                    ],
                }
            ]
        }
        mock_get.return_value = client

        result = cad_get_selection()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["selections"]), 1)


class TestCadGetModelTree(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_get_model_tree(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "doc": "MyDoc",
            "objects": [
                {"name": "Body", "label": "Body", "type": "PartDesign::Body"},
                {"name": "Pad", "label": "Pad", "type": "PartDesign::Pad"},
            ],
        }
        mock_get.return_value = client

        result = cad_get_model_tree()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["objects"]), 2)


class TestCadUndo(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_undo(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"undone": True}
        mock_get.return_value = client

        result = cad_undo()
        self.assertTrue(result["ok"])


class TestCadExport(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_export(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"path": "/tmp/test.step", "format": "step"}
        mock_get.return_value = client

        result = cad_export(format="step")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/tmp/test.step")


class TestRecomputeFailure(unittest.TestCase):
    """Verify that recompute failures in FreeCAD propagate as error dicts."""

    @patch("server.tools_cad.get_client")
    def test_fillet_recompute_failure_returns_error(self, mock_get: MagicMock) -> None:
        from server.freecad_client import FreeCADCommandError
        client = _mock_client()
        client.send_command.side_effect = FreeCADCommandError(
            "ValueError: Fillet failed: recompute error "
            "(radius may be too large for the selected edges, "
            "or edge references may be invalid). "
            "The failed feature has been removed."
        )
        mock_get.return_value = client

        result = cad_fillet(edges=["Edge1"], radius=999.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "COMMAND_ERROR")
        self.assertIn("recompute error", result["error"]["message"])

    @patch("server.tools_cad.get_client")
    def test_pad_recompute_failure_returns_error(self, mock_get: MagicMock) -> None:
        from server.freecad_client import FreeCADCommandError
        client = _mock_client()
        client.send_command.side_effect = FreeCADCommandError(
            "ValueError: Pad failed: recompute error "
            "(sketch profile may be invalid or self-intersecting). "
            "The failed feature has been removed."
        )
        mock_get.return_value = client

        result = cad_pad(sketch="Sketch", length=10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "COMMAND_ERROR")
        self.assertIn("Pad failed", result["error"]["message"])


class TestCadFindEdges(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_find_edges_no_filters(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body",
            "filters_applied": {},
            "matched_edges": [
                {"name": "Edge1", "length": 100.0},
                {"name": "Edge2", "length": 50.0},
            ],
            "total_edges": 12,
            "num_matched": 2,
        }
        mock_get.return_value = client

        result = cad_find_edges()
        self.assertTrue(result["ok"])
        self.assertEqual(result["num_matched"], 2)
        client.send_command.assert_called_once_with("find_edges")

    @patch("server.tools_cad.get_client")
    def test_find_edges_all_filters(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body",
            "filters_applied": {"axis": "Z", "convexity": "convex"},
            "matched_edges": [{"name": "Edge5", "length": 75.0}],
            "total_edges": 57,
            "num_matched": 1,
        }
        mock_get.return_value = client

        result = cad_find_edges(
            body="Body",
            axis="Z",
            curve_type="Line",
            min_length=10.0,
            max_length=100.0,
            on_face="Face3",
            near_point=[0.0, 0.0, 0.0],
            near_distance=5.0,
            convexity="convex",
            doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "find_edges",
            body="Body",
            axis="Z",
            curve_type="Line",
            min_length=10.0,
            max_length=100.0,
            on_face="Face3",
            near_point=[0.0, 0.0, 0.0],
            near_distance=5.0,
            convexity="convex",
            doc="MyDoc",
        )

    @patch("server.tools_cad.get_client")
    def test_find_edges_with_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body",
            "filters_applied": {},
            "matched_edges": [],
            "total_edges": 12,
            "num_matched": 0,
        }
        mock_get.return_value = client

        result = cad_find_edges(doc="MyDoc")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("find_edges", doc="MyDoc")


class TestCadGetBodyTopologyAdjacency(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_edge_names_in_face_data(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body",
            "tip_feature": "Pad",
            "num_faces": 1,
            "num_edges": 4,
            "faces": [
                {
                    "name": "Face1",
                    "surface_type": "Plane",
                    "normal": [0, 0, 1],
                    "center": [50, 25, 20],
                    "area": 5000.0,
                    "edge_names": ["Edge1", "Edge2", "Edge3", "Edge4"],
                },
            ],
            "edges": [
                {"name": "Edge1", "curve_type": "Line", "length": 100.0},
                {"name": "Edge2", "curve_type": "Line", "length": 50.0},
                {"name": "Edge3", "curve_type": "Line", "length": 100.0},
                {"name": "Edge4", "curve_type": "Line", "length": 50.0},
            ],
        }
        mock_get.return_value = client

        result = cad_get_body_topology()
        self.assertTrue(result["ok"])
        self.assertIn("edge_names", result["faces"][0])
        self.assertEqual(result["faces"][0]["edge_names"], ["Edge1", "Edge2", "Edge3", "Edge4"])


class TestCadDefineSelection(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_define_selection(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "outer_corners",
            "matched_edges": [
                {"name": "Edge5", "length": 75.0},
                {"name": "Edge22", "length": 75.0},
            ],
            "num_matched": 2,
            "invariants_ok": True,
            "violations": [],
        }
        mock_get.return_value = client

        result = cad_define_selection(
            name="outer_corners",
            query={"axis": "Z", "convexity": "convex"},
            invariants={"expected_count": 2},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "outer_corners")
        self.assertEqual(result["num_matched"], 2)
        self.assertTrue(result["invariants_ok"])
        client.send_command.assert_called_once_with(
            "define_selection",
            name="outer_corners",
            query={"axis": "Z", "convexity": "convex"},
            invariants={"expected_count": 2},
        )

    @patch("server.tools_cad.get_client")
    def test_define_selection_with_body_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "sel1",
            "matched_edges": [],
            "num_matched": 0,
            "invariants_ok": True,
            "violations": [],
        }
        mock_get.return_value = client

        result = cad_define_selection(name="sel1", query={"axis": "X"}, body="Body2", doc="MyDoc")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "define_selection",
            name="sel1",
            query={"axis": "X"},
            body="Body2",
            doc="MyDoc",
        )


class TestCadResolveSelection(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_resolve_selection(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "outer_corners",
            "matched_edges": [{"name": "Edge5", "length": 75.0}],
            "num_matched": 1,
            "invariants_ok": False,
            "violations": ["expected_count: expected 4, got 1"],
        }
        mock_get.return_value = client

        result = cad_resolve_selection(name="outer_corners")
        self.assertTrue(result["ok"])
        self.assertFalse(result["invariants_ok"])
        self.assertEqual(len(result["violations"]), 1)
        client.send_command.assert_called_once_with("resolve_selection", name="outer_corners")


class TestCadListSelections(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_list_selections(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "selection_sets": [
                {"name": "outer_corners", "query": {"axis": "Z"}, "invariants": {}},
            ],
            "count": 1,
        }
        mock_get.return_value = client

        result = cad_list_selections()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        client.send_command.assert_called_once_with("list_selections")


class TestCadDeleteSelection(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_delete_selection(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"deleted": "outer_corners"}
        mock_get.return_value = client

        result = cad_delete_selection(name="outer_corners")
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], "outer_corners")
        client.send_command.assert_called_once_with("delete_selection", name="outer_corners")


class TestCadFilletWithSelection(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_fillet_with_selection(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Fillet", "label": "Fillet", "type": "PartDesign::Fillet",
        }
        mock_get.return_value = client

        result = cad_fillet(selection="outer_corners", radius=5.0)
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "fillet", radius=5.0, selection="outer_corners",
        )


class TestCadChamferWithSelection(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_chamfer_with_selection(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Chamfer", "label": "Chamfer", "type": "PartDesign::Chamfer",
        }
        mock_get.return_value = client

        result = cad_chamfer(selection="top_edges", size=2.0)
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "chamfer", size=2.0, selection="top_edges",
        )


class TestShapeDigest(unittest.TestCase):
    """Verify digest + delta keys flow through tool responses."""

    @patch("server.tools_cad.get_client")
    def test_pad_response_includes_digest(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pad", "label": "Pad", "type": "PartDesign::Pad",
            "bounding_box": {"x_len": 100, "y_len": 50, "z_len": 20},
            "volume": 100000,
            "digest": {
                "volume": 100000.0,
                "surface_area": 22000.0,
                "bbox": [100.0, 50.0, 20.0],
                "faces": 6,
                "edges": 12,
                "vertices": 8,
            },
            "delta": None,
        }
        mock_get.return_value = client

        result = cad_pad(sketch="Sketch", length=20)
        self.assertTrue(result["ok"])
        self.assertIn("digest", result)
        self.assertIsNone(result["delta"])
        self.assertEqual(result["digest"]["faces"], 6)

    @patch("server.tools_cad.get_client")
    def test_pocket_response_includes_delta(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
            "digest": {
                "volume": 80000.0,
                "surface_area": 24000.0,
                "bbox": [100.0, 50.0, 20.0],
                "faces": 10,
                "edges": 24,
                "vertices": 16,
            },
            "delta": {
                "volume": -20000.0,
                "surface_area": 2000.0,
                "faces": 4,
                "edges": 12,
                "vertices": 8,
            },
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5)
        self.assertTrue(result["ok"])
        self.assertIn("digest", result)
        self.assertIn("delta", result)
        self.assertIsNotNone(result["delta"])
        self.assertEqual(result["delta"]["volume"], -20000.0)
        self.assertEqual(result["delta"]["faces"], 4)

    @patch("server.tools_cad.get_client")
    def test_fillet_response_includes_digest(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Fillet", "label": "Fillet", "type": "PartDesign::Fillet",
            "digest": {
                "volume": 99000.0,
                "surface_area": 22500.0,
                "bbox": [100.0, 50.0, 20.0],
                "faces": 10,
                "edges": 24,
                "vertices": 16,
            },
            "delta": {
                "volume": -1000.0,
                "surface_area": 500.0,
                "faces": 4,
                "edges": 12,
                "vertices": 8,
            },
        }
        mock_get.return_value = client

        result = cad_fillet(edges=["Edge1"], radius=2.0)
        self.assertTrue(result["ok"])
        self.assertIn("digest", result)
        self.assertIn("delta", result)


class TestSelectionDrift(unittest.TestCase):
    """Verify selection_drift key flows through operation responses."""

    @patch("server.tools_cad.get_client")
    def test_pad_response_includes_selection_drift(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pad", "label": "Pad", "type": "PartDesign::Pad",
            "bounding_box": {"x_len": 100, "y_len": 50, "z_len": 20},
            "digest": {"volume": 100000.0, "surface_area": 22000.0, "bbox": [100, 50, 20], "faces": 6, "edges": 12, "vertices": 8},
            "delta": None,
            "selection_drift": [
                {"name": "outer_corners", "status": "ok", "count": 4},
                {"name": "pocket_rim", "status": "DRIFT", "count": 6, "expected_count": 4, "actual_count": 6,
                 "violations": ["expected_count: expected 4, got 6"]},
            ],
        }
        mock_get.return_value = client

        result = cad_pad(sketch="Sketch", length=20)
        self.assertTrue(result["ok"])
        self.assertIn("selection_drift", result)
        self.assertEqual(len(result["selection_drift"]), 2)
        self.assertEqual(result["selection_drift"][0]["status"], "ok")
        self.assertEqual(result["selection_drift"][1]["status"], "DRIFT")
        self.assertEqual(result["selection_drift"][1]["expected_count"], 4)
        self.assertEqual(result["selection_drift"][1]["actual_count"], 6)

    @patch("server.tools_cad.get_client")
    def test_fillet_response_includes_selection_drift(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Fillet", "label": "Fillet", "type": "PartDesign::Fillet",
            "digest": {"volume": 99000.0, "surface_area": 22500.0, "bbox": [100, 50, 20], "faces": 10, "edges": 24, "vertices": 16},
            "delta": {"volume": -1000.0, "surface_area": 500.0, "faces": 4, "edges": 12, "vertices": 8},
            "selection_drift": [],
        }
        mock_get.return_value = client

        result = cad_fillet(edges=["Edge1"], radius=2.0)
        self.assertTrue(result["ok"])
        self.assertIn("selection_drift", result)
        self.assertEqual(result["selection_drift"], [])


class TestConnectionError(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_connection_error_returns_error_dict(self, mock_get: MagicMock) -> None:
        from server.freecad_client import FreeCADConnectionError
        mock_get.side_effect = FreeCADConnectionError("Not connected")

        result = cad_new_document(name="Test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "CONNECTION_ERROR")


if __name__ == "__main__":
    unittest.main()
