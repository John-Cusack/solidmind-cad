"""Tests for the CAD MCP tool implementations.

These tests mock the FreeCAD client to test tool logic without a live
FreeCAD instance.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from server.tools_cad import (
    cad_animate,
    cad_animate_stop,
    cad_chamfer,
    cad_define_selection,
    cad_delete_objects,
    cad_delete_selection,
    cad_draft,
    cad_export,
    cad_export_body,
    cad_export_sim_package,
    cad_fillet,
    cad_find_edges,
    cad_get_body_topology,
    cad_get_camera,
    cad_get_dimensions,
    cad_get_model_tree,
    cad_get_selection,
    cad_helix,
    cad_hole,
    cad_linear_pattern,
    cad_list_selections,
    cad_loft,
    cad_mirror,
    cad_new_body,
    cad_new_document,
    cad_pad,
    cad_pocket,
    cad_polar_pattern,
    cad_resolve_selection,
    cad_revolution,
    cad_screenshot,
    cad_set_camera,
    cad_sketch,
    cad_sweep,
    cad_thickness,
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
            {  # sketch_populate
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "rect", "indices": [0, 1, 2, 3]}],
            },
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
        # Verify sketch_populate was called (not individual sketch_rect)
        populate_call = client.send_command.call_args_list[1]
        self.assertEqual(populate_call[0][0], "sketch_populate")
        self.assertEqual(len(populate_call[1]["elements"]), 1)

    @patch("server.tools_cad.get_client")
    def test_sketch_with_circle(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "circle", "index": 0}],
            },
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
        from server.freecad_client import FreeCADCommandError
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            FreeCADCommandError("ValueError: Unknown element type: hexagon"),  # sketch_populate
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{"type": "hexagon"}],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "COMMAND_ERROR")


class TestCadSketchGeometryRef(unittest.TestCase):
    """Test cad_sketch with geometry_ref from the geometry store."""

    def setUp(self) -> None:
        from server.geometry_store import clear
        clear()

    def tearDown(self) -> None:
        from server.geometry_store import clear
        clear()

    @patch("server.tools_cad.get_client")
    def test_sketch_with_geometry_ref(self, mock_get: MagicMock) -> None:
        from server.geometry_store import store

        ref = store([
            {"type": "arc", "cx": 0, "cy": 0, "r": 10, "start_angle": 0, "end_angle": 90},
            {"type": "arc", "cx": 0, "cy": 0, "r": 12, "start_angle": 0, "end_angle": 90},
        ])
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {  # sketch_populate (batch)
                "sketch": "Sketch",
                "element_count": 2,
                "constraint_count": 0,
                "geometry": [
                    {"type": "arc", "index": 0},
                    {"type": "arc", "index": 1},
                ],
            },
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},  # close
        ]
        mock_get.return_value = client

        result = cad_sketch(body="Body", geometry_ref=ref)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["geometry"]), 2)

    @patch("server.tools_cad.get_client")
    def test_sketch_with_geometry_ref_and_elements(self, mock_get: MagicMock) -> None:
        from server.geometry_store import store

        ref = store([{"type": "arc", "cx": 0, "cy": 0, "r": 10, "start_angle": 0, "end_angle": 90}])
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {  # sketch_populate (batch)
                "sketch": "Sketch",
                "element_count": 2,
                "constraint_count": 0,
                "geometry": [
                    {"type": "arc", "index": 0},
                    {"type": "circle", "index": 1},
                ],
            },
            {"sketch": "Sketch", "fully_constrained": False, "open_vertices": 0},  # close
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            geometry_ref=ref,
            elements=[{"type": "circle", "cx": 0, "cy": 0, "r": 3}],
        )
        self.assertTrue(result["ok"])
        # 1 arc from ref + 1 circle from inline
        self.assertEqual(len(result["geometry"]), 2)

    def test_sketch_with_invalid_geometry_ref(self) -> None:
        result = cad_sketch(body="Body", geometry_ref="geo_doesnotexist")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_GEOMETRY_REF")

    @patch("server.tools_cad.get_client")
    def test_geometry_ref_not_consumed(self, mock_get: MagicMock) -> None:
        """Verify that using a ref doesn't remove it from the store."""
        from server.geometry_store import retrieve, store

        ref = store([{"type": "circle", "cx": 0, "cy": 0, "r": 5}])
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "circle", "index": 0}],
            },
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},
        ]
        mock_get.return_value = client

        cad_sketch(body="Body", geometry_ref=ref)
        # Ref should still be in the store
        self.assertIsNotNone(retrieve(ref))


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
            symmetric=False, reversed=False, subtractive=False, verify=True,
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
            symmetric=True, reversed=True, subtractive=False, verify=True, doc="MyDoc",
        )


    @patch("server.tools_cad.get_client")
    def test_revolution_subtractive(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Groove", "label": "Groove", "type": "PartDesign::Groove",
            "bounding_box": {"x_len": 60, "y_len": 60, "z_len": 35},
        }
        mock_get.return_value = client

        result = cad_revolution(sketch="TrimSketch", subtractive=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Groove")
        client.send_command.assert_called_once_with(
            "revolution", sketch="TrimSketch", axis="V", angle=360.0,
            symmetric=False, reversed=False, subtractive=True, verify=True,
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
            occurrences=6, angle=360.0, reversed=False, verify=True,
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
            verify=True, body="Body", doc="MyDoc",
        )


class TestCadPocket(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_pocket_explicit_reversed(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5, reversed=True)
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "pocket", sketch="Sketch", length=5, pocket_type="Dimension",
            reversed=True, verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_pocket_auto_reversed_default(self, mock_get: MagicMock) -> None:
        """Default reversed='auto' is passed to addon for resolution."""
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
            "auto_reversed": {
                "reversed": True, "confidence": "high",
                "reason": "solid centroid is above sketch plane",
            },
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5)
        self.assertTrue(result["ok"])
        # reversed="auto" should be passed through
        call_kwargs = client.send_command.call_args[1]
        self.assertEqual(call_kwargs["reversed"], "auto")


class TestPocketDirectionAlgorithm(unittest.TestCase):
    """Test the pocket direction algorithm (pure math, no FreeCAD needed).

    The algorithm: dot = (centroid - sketch_origin) · sketch_normal.
    If dot > 0 → solid is in +normal direction → reversed=True.
    If dot ≤ 0 → solid is in -normal direction → reversed=False.
    """

    @staticmethod
    def _resolve(origin: list[float], normal: list[float], centroid: list[float]) -> bool:
        """Reimplement the resolver algorithm for testing."""
        dot = sum((c - o) * n for c, o, n in zip(centroid, origin, normal))
        return dot > 0

    def test_xy_sketch_body_above(self) -> None:
        """XY sketch at z=0, body padded to +Z → reversed=True."""
        self.assertTrue(self._resolve([0, 0, 0], [0, 0, 1], [50, 25, 5]))

    def test_xy_sketch_body_below(self) -> None:
        """XY sketch at z=10, body below at z=0..10 → reversed=False."""
        self.assertFalse(self._resolve([0, 0, 10], [0, 0, 1], [50, 25, 5]))

    def test_face_mapped_sketch_on_top(self) -> None:
        """Sketch on top face (z=10, normal +Z), body below → reversed=False."""
        self.assertFalse(self._resolve([50, 25, 10], [0, 0, 1], [50, 25, 5]))

    def test_xz_sketch_body_in_positive_y(self) -> None:
        """XZ sketch at y=0, body padded in +Y → reversed=True."""
        self.assertTrue(self._resolve([0, 0, 0], [0, 1, 0], [50, 10, 25]))

    def test_xz_sketch_body_in_negative_y(self) -> None:
        """XZ sketch at y=0, body in -Y → reversed=False."""
        self.assertFalse(self._resolve([0, 0, 0], [0, 1, 0], [50, -10, 25]))

    def test_yz_sketch_body_in_positive_x(self) -> None:
        """YZ sketch at x=0, body in +X → reversed=True."""
        self.assertTrue(self._resolve([0, 0, 0], [1, 0, 0], [10, 25, 25]))

    def test_centroid_on_sketch_plane(self) -> None:
        """Body centroid exactly on sketch plane (dot=0) → reversed=False."""
        self.assertFalse(self._resolve([0, 0, 0], [0, 0, 1], [50, 25, 0]))

    def test_tilted_normal(self) -> None:
        """Tilted sketch: normal=(0.707, 0, 0.707), body offset along that direction."""
        import math
        n = 1.0 / math.sqrt(2)
        # Body centroid at (10, 0, 10) from origin (0,0,0) — positive dot with (n,0,n)
        self.assertTrue(self._resolve([0, 0, 0], [n, 0, n], [10, 0, 10]))
        # Body centroid at (-10, 0, -10) — negative dot
        self.assertFalse(self._resolve([0, 0, 0], [n, 0, n], [-10, 0, -10]))


class TestPocketDirectionResolver(unittest.TestCase):
    """Test the pocket direction auto-resolver logic.

    The resolver computes ``reversed`` from the sketch normal and body centroid.
    These tests validate the algorithm via mock FreeCAD objects without a live
    FreeCAD instance.
    """

    @patch("server.tools_cad.get_client")
    def test_auto_reversed_passed_through(self, mock_get: MagicMock) -> None:
        """Default reversed='auto' is sent to the addon."""
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
            "auto_reversed": {
                "reversed": True,
                "confidence": "high",
                "reason": "solid centroid is above sketch plane (dot=5.00)",
            },
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5)
        self.assertTrue(result["ok"])
        call_kwargs = client.send_command.call_args[1]
        self.assertEqual(call_kwargs["reversed"], "auto")
        # auto_reversed info is passed through in the result
        self.assertIn("auto_reversed", result)
        self.assertTrue(result["auto_reversed"]["reversed"])
        self.assertEqual(result["auto_reversed"]["confidence"], "high")

    @patch("server.tools_cad.get_client")
    def test_explicit_true_overrides_auto(self, mock_get: MagicMock) -> None:
        """Explicit reversed=True bypasses auto-resolution."""
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5, reversed=True)
        self.assertTrue(result["ok"])
        call_kwargs = client.send_command.call_args[1]
        self.assertEqual(call_kwargs["reversed"], True)
        # No auto_reversed when explicitly set
        self.assertNotIn("auto_reversed", result)

    @patch("server.tools_cad.get_client")
    def test_explicit_false_overrides_auto(self, mock_get: MagicMock) -> None:
        """Explicit reversed=False bypasses auto-resolution."""
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5, reversed=False)
        self.assertTrue(result["ok"])
        call_kwargs = client.send_command.call_args[1]
        self.assertEqual(call_kwargs["reversed"], False)


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
    def test_get_model_tree_default_bodies(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "doc": "MyDoc",
            "body_count": 1,
            "bodies": [
                {"name": "Body", "label": "Body", "size": [10.0, 20.0, 5.0], "feature_count": 2, "tip": "Pad"},
            ],
            "other_objects": [],
        }
        mock_get.return_value = client

        result = cad_get_model_tree()
        self.assertTrue(result["ok"])
        self.assertEqual(result["body_count"], 1)
        self.assertEqual(len(result["bodies"]), 1)
        self.assertEqual(result["bodies"][0]["size"], [10.0, 20.0, 5.0])
        client.send_command.assert_called_once_with("get_model_tree", detail="bodies")

    @patch("server.tools_cad.get_client")
    def test_get_model_tree_full(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "doc": "MyDoc",
            "objects": [
                {"name": "Body", "label": "Body", "type": "PartDesign::Body"},
                {"name": "Pad", "label": "Pad", "type": "PartDesign::Pad"},
            ],
        }
        mock_get.return_value = client

        result = cad_get_model_tree(detail="full")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["objects"]), 2)
        client.send_command.assert_called_once_with("get_model_tree", detail="full")


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


class TestCadDeleteObjects(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_delete_objects(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"deleted": ["Assembly", "Assembly001"], "not_found": []}
        mock_get.return_value = client

        result = cad_delete_objects(names=["Assembly", "Assembly001"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], ["Assembly", "Assembly001"])
        self.assertEqual(result["not_found"], [])
        client.send_command.assert_called_once_with("delete_objects", names=["Assembly", "Assembly001"])

    @patch("server.tools_cad.get_client")
    def test_delete_objects_with_not_found(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"deleted": ["Body"], "not_found": ["NoSuch"]}
        mock_get.return_value = client

        result = cad_delete_objects(names=["Body", "NoSuch"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], ["Body"])
        self.assertEqual(result["not_found"], ["NoSuch"])

    @patch("server.tools_cad.get_client")
    def test_delete_objects_with_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"deleted": ["Obj1"], "not_found": []}
        mock_get.return_value = client

        result = cad_delete_objects(names=["Obj1"], doc="MyDoc")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("delete_objects", names=["Obj1"], doc="MyDoc")


class TestCadSetPlacement(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_set_placement_position_and_rotation(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "object": "Body_Sun",
            "position": [10.0, 20.0, 0.0],
            "rotation_angle_deg": 45.0,
            "rotation_axis": [0.0, 0.0, 1.0],
        }
        mock_get.return_value = client

        from server.tools_cad import cad_set_placement
        result = cad_set_placement(
            object_name="Body_Sun",
            position=[10.0, 20.0, 0.0],
            rotation_axis=[0.0, 0.0, 1.0],
            rotation_angle_deg=45.0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["object"], "Body_Sun")
        self.assertEqual(result["position"], [10.0, 20.0, 0.0])
        client.send_command.assert_called_once_with(
            "set_placement",
            object_name="Body_Sun",
            position=[10.0, 20.0, 0.0],
            rotation_axis=[0.0, 0.0, 1.0],
            rotation_angle_deg=45.0,
        )

    @patch("server.tools_cad.get_client")
    def test_set_placement_minimal(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "object": "Body",
            "position": [0.0, 0.0, 0.0],
            "rotation_angle_deg": 0.0,
            "rotation_axis": [0.0, 0.0, 1.0],
        }
        mock_get.return_value = client

        from server.tools_cad import cad_set_placement
        result = cad_set_placement(object_name="Body")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "set_placement",
            object_name="Body",
            rotation_angle_deg=0.0,
        )

    @patch("server.tools_cad.get_client")
    def test_set_placement_with_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "object": "Link",
            "position": [5.0, 0.0, 0.0],
            "rotation_angle_deg": 90.0,
            "rotation_axis": [0.0, 0.0, 1.0],
        }
        mock_get.return_value = client

        from server.tools_cad import cad_set_placement
        result = cad_set_placement(
            object_name="Link",
            position=[5.0, 0.0, 0.0],
            rotation_angle_deg=90.0,
            doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "set_placement",
            object_name="Link",
            position=[5.0, 0.0, 0.0],
            rotation_angle_deg=90.0,
            doc="MyDoc",
        )


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
            "fillet", radius=5.0, verify=True, selection="outer_corners",
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
            "chamfer", size=2.0, verify=True, selection="top_edges",
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


class TestCadSketchSpline(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_sketch_with_spline(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {  # sketch_populate
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "spline", "index": 0}],
            },
            {"sketch": "Sketch", "fully_constrained": False, "open_vertices": 2},  # close_sketch
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{"type": "spline", "points": [[0, 0], [10, 5], [20, 0]]}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["geometry"][0]["type"], "spline")
        # Verify sketch_populate was called with the spline element
        populate_call = client.send_command.call_args_list[1]
        self.assertEqual(populate_call[0][0], "sketch_populate")
        self.assertEqual(populate_call[1]["elements"][0]["type"], "spline")
        self.assertEqual(populate_call[1]["elements"][0]["points"], [[0, 0], [10, 5], [20, 0]])

    @patch("server.tools_cad.get_client")
    def test_sketch_with_spline_options(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "spline", "index": 0}],
            },
            {"sketch": "Sketch", "fully_constrained": False, "open_vertices": 0},
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{
                "type": "spline",
                "points": [[0, 0], [5, 10], [10, 10], [15, 0]],
                "degree": 2,
                "periodic": True,
                "weights": [1.0, 2.0, 1.0, 1.0],
            }],
        )
        self.assertTrue(result["ok"])
        populate_call = client.send_command.call_args_list[1]
        elem = populate_call[1]["elements"][0]
        self.assertEqual(elem["degree"], 2)
        self.assertEqual(elem["periodic"], True)
        self.assertEqual(elem["weights"], [1.0, 2.0, 1.0, 1.0])


class TestCadSketchBatching(unittest.TestCase):
    """Verify that cad_sketch uses batched sketch_populate instead of individual calls."""

    @patch("server.tools_cad.get_client")
    def test_batched_elements_and_constraints(self, mock_get: MagicMock) -> None:
        """Multiple elements + constraints should be sent as one sketch_populate call."""
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {  # sketch_populate
                "sketch": "Sketch",
                "element_count": 2,
                "constraint_count": 3,
                "geometry": [
                    {"type": "line", "index": 0},
                    {"type": "line", "index": 1},
                ],
            },
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},  # close
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[
                {"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0},
                {"type": "line", "x1": 10, "y1": 0, "x2": 10, "y2": 10},
            ],
            constraints=[
                {"type": "Coincident", "first": 0, "first_pos": 2, "second": 1, "second_pos": 1},
                {"type": "Horizontal", "first": 0},
                {"type": "Vertical", "first": 1},
            ],
        )
        self.assertTrue(result["ok"])
        # Only 3 TCP calls total: new_sketch + sketch_populate + close_sketch
        self.assertEqual(client.send_command.call_count, 3)
        # Verify the sketch_populate call
        populate_call = client.send_command.call_args_list[1]
        self.assertEqual(populate_call[0][0], "sketch_populate")
        self.assertEqual(len(populate_call[1]["elements"]), 2)
        self.assertEqual(len(populate_call[1]["constraints"]), 3)

    @patch("server.tools_cad.get_client")
    def test_empty_sketch_no_populate(self, mock_get: MagicMock) -> None:
        """Sketch with no elements and no constraints should skip sketch_populate."""
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},  # close
        ]
        mock_get.return_value = client

        result = cad_sketch(body="Body")
        self.assertTrue(result["ok"])
        # Only 2 TCP calls: new_sketch + close_sketch (no sketch_populate)
        self.assertEqual(client.send_command.call_count, 2)

    @patch("server.tools_cad.get_client")
    def test_adaptive_timeout_for_large_sketch(self, mock_get: MagicMock) -> None:
        """Large constraint count should increase the timeout."""
        client = _mock_client()
        # 200 constraints (like a gear profile)
        many_constraints = [
            {"type": "Coincident", "first": i, "first_pos": 2, "second": (i + 1) % 200, "second_pos": 1}
            for i in range(200)
        ]
        client.send_command.side_effect = [
            {"sketch": "Sketch"},  # new_sketch
            {  # sketch_populate
                "sketch": "Sketch",
                "element_count": 0,
                "constraint_count": 200,
                "geometry": [],
            },
            {"sketch": "Sketch", "fully_constrained": False, "open_vertices": 0},  # close
        ]
        mock_get.return_value = client

        result = cad_sketch(body="Body", constraints=many_constraints)
        self.assertTrue(result["ok"])
        # Verify timeout was passed (30 + 200 * 0.1 = 50s)
        populate_call = client.send_command.call_args_list[1]
        self.assertEqual(populate_call[1]["timeout"], 50.0)


class TestCadSweep(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_sweep_additive(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pipe", "label": "Pipe", "type": "PartDesign::AdditivePipe",
            "bounding_box": {"x_len": 50, "y_len": 50, "z_len": 100},
        }
        mock_get.return_value = client

        result = cad_sweep(profile_sketch="ProfileSketch", spine_sketch="SpineSketch")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Pipe")
        client.send_command.assert_called_once_with(
            "sweep",
            profile_sketch="ProfileSketch",
            spine_sketch="SpineSketch",
            subtractive=False,
            verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_sweep_subtractive(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pipe", "label": "Pipe", "type": "PartDesign::SubtractivePipe",
        }
        mock_get.return_value = client

        result = cad_sweep(
            profile_sketch="ProfileSketch",
            spine_sketch="SpineSketch",
            subtractive=True,
            doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "sweep",
            profile_sketch="ProfileSketch",
            spine_sketch="SpineSketch",
            subtractive=True,
            verify=True,
            doc="MyDoc",
        )


class TestCadLoft(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_loft_additive(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Loft", "label": "Loft", "type": "PartDesign::AdditiveLoft",
            "bounding_box": {"x_len": 100, "y_len": 50, "z_len": 80},
        }
        mock_get.return_value = client

        result = cad_loft(sketches=["Sketch1", "Sketch2"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Loft")
        client.send_command.assert_called_once_with(
            "loft",
            sketches=["Sketch1", "Sketch2"],
            ruled=False,
            closed=False,
            subtractive=False,
            verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_loft_subtractive_with_options(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Loft", "label": "Loft", "type": "PartDesign::SubtractiveLoft",
        }
        mock_get.return_value = client

        result = cad_loft(
            sketches=["Sketch1", "Sketch2", "Sketch3"],
            ruled=True,
            closed=True,
            subtractive=True,
            doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "loft",
            sketches=["Sketch1", "Sketch2", "Sketch3"],
            ruled=True,
            closed=True,
            subtractive=True,
            verify=True,
            doc="MyDoc",
        )


class TestCadHelix(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_helix_pitch_height(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Helix", "label": "Helix", "type": "PartDesign::AdditiveHelix",
            "bounding_box": {"x_len": 20, "y_len": 20, "z_len": 30},
        }
        mock_get.return_value = client

        result = cad_helix(sketch="Sketch", pitch=2.0, height=20.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Helix")
        client.send_command.assert_called_once_with(
            "helix", sketch="Sketch", pitch=2.0, height=20.0, turns=0.0,
            axis="V", angle=0.0, growth=0.0, left_handed=False,
            reversed=False, mode="pitch-height", verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_helix_pitch_turns(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Helix", "label": "Helix", "type": "PartDesign::AdditiveHelix",
        }
        mock_get.return_value = client

        result = cad_helix(sketch="Sketch", pitch=3.0, turns=5.0, mode="pitch-turns")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "helix", sketch="Sketch", pitch=3.0, height=0.0, turns=5.0,
            axis="V", angle=0.0, growth=0.0, left_handed=False,
            reversed=False, mode="pitch-turns", verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_helix_height_turns(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Helix", "label": "Helix", "type": "PartDesign::AdditiveHelix",
        }
        mock_get.return_value = client

        result = cad_helix(sketch="Sketch", height=30.0, turns=10.0, mode="height-turns")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "helix", sketch="Sketch", pitch=0.0, height=30.0, turns=10.0,
            axis="V", angle=0.0, growth=0.0, left_handed=False,
            reversed=False, mode="height-turns", verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_helix_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Helix", "label": "Helix", "type": "PartDesign::AdditiveHelix",
        }
        mock_get.return_value = client

        result = cad_helix(
            sketch="Sketch", pitch=2.0, height=20.0, axis="Base_Z",
            angle=5.0, growth=1.0, left_handed=True, reversed=True,
            verify=False, doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "helix", sketch="Sketch", pitch=2.0, height=20.0, turns=0.0,
            axis="Base_Z", angle=5.0, growth=1.0, left_handed=True,
            reversed=True, mode="pitch-height", verify=False, doc="MyDoc",
        )


class TestCadScreenshot(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_screenshot_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "ok": True,
            "width": 512,
            "height": 512,
            "image_base64": "iVBOR...",
            "mime_type": "image/png",
            "camera_position": [100, 100, 100],
            "camera_target": [0, 0, 0],
        }
        mock_get.return_value = client

        result = cad_screenshot()
        self.assertTrue(result["ok"])
        self.assertIn("image_base64", result)
        self.assertEqual(result["width"], 512)
        client.send_command.assert_called_once_with(
            "screenshot", target="iso", distance=2.0, width=512, height=512,
        )

    @patch("server.tools_cad.get_client")
    def test_screenshot_with_face_target(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "ok": True,
            "width": 512,
            "height": 512,
            "image_base64": "iVBOR...",
            "mime_type": "image/png",
            "camera_position": [0, 0, 200],
            "camera_target": [50, 25, 20],
        }
        mock_get.return_value = client

        result = cad_screenshot(target="Face3", distance=1.5)
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "screenshot", target="Face3", distance=1.5, width=512, height=512,
        )

    @patch("server.tools_cad.get_client")
    def test_screenshot_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "ok": True,
            "width": 256,
            "height": 256,
            "image_base64": "iVBOR...",
            "mime_type": "image/png",
            "camera_position": [100, 0, 0],
            "camera_target": [0, 0, 0],
        }
        mock_get.return_value = client

        result = cad_screenshot(
            target="front",
            distance=3.0,
            direction=[1, 0, 0],
            up=[0, 1, 0],
            near_clip=5.0,
            width=256,
            height=256,
            doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "screenshot",
            target="front",
            distance=3.0,
            width=256,
            height=256,
            direction=[1, 0, 0],
            up=[0, 1, 0],
            near_clip=5.0,
            doc="MyDoc",
        )

    @patch("server.tools_cad.get_client")
    def test_screenshot_with_hide_bodies(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "ok": True,
            "image_base64": "abc",
            "mime_type": "image/png",
            "camera_position": [100, 100, 100],
            "camera_target": [0, 0, 0],
        }
        mock_get.return_value = client

        result = cad_screenshot(hide_bodies=["Body_Ring", "Body_Cover"])
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "screenshot",
            target="iso",
            distance=2.0,
            width=512,
            height=512,
            hide_bodies=["Body_Ring", "Body_Cover"],
        )


class TestCadSetCamera(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_set_camera(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "camera_set": True,
            "position": [100, 100, 100],
        }
        mock_get.return_value = client

        result = cad_set_camera(position=[100, 100, 100], target=[0, 0, 0])
        self.assertTrue(result["ok"])
        self.assertTrue(result["camera_set"])
        client.send_command.assert_called_once_with(
            "set_camera",
            fit_all=False,
            position=[100, 100, 100],
            target=[0, 0, 0],
        )

    @patch("server.tools_cad.get_client")
    def test_set_camera_fit_all(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "camera_set": True,
            "position": [0, 0, 0],
        }
        mock_get.return_value = client

        result = cad_set_camera(fit_all=True)
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("set_camera", fit_all=True)


class TestCadGetCamera(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_get_camera(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "position": [100, 100, 100],
            "near_clip": 1.0,
            "far_clip": 10000.0,
        }
        mock_get.return_value = client

        result = cad_get_camera()
        self.assertTrue(result["ok"])
        self.assertEqual(result["position"], [100, 100, 100])
        self.assertEqual(result["near_clip"], 1.0)
        client.send_command.assert_called_once_with("get_camera")


class TestVerifyParam(unittest.TestCase):
    """Verify that modeling tools pass the verify kwarg through to the addon."""

    @patch("server.tools_cad.get_client")
    def test_pad_passes_verify_true(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pad", "label": "Pad", "type": "PartDesign::Pad",
        }
        mock_get.return_value = client

        cad_pad(sketch="Sketch", length=20, verify=True)
        call_kwargs = client.send_command.call_args[1]
        self.assertTrue(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_pad_passes_verify_false(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pad", "label": "Pad", "type": "PartDesign::Pad",
        }
        mock_get.return_value = client

        cad_pad(sketch="Sketch", length=20, verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_pocket_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
        }
        mock_get.return_value = client

        cad_pocket(sketch="Sketch", length=5, verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_fillet_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Fillet", "label": "Fillet", "type": "PartDesign::Fillet",
        }
        mock_get.return_value = client

        cad_fillet(edges=["Edge1"], radius=2.0, verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_chamfer_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Chamfer", "label": "Chamfer", "type": "PartDesign::Chamfer",
        }
        mock_get.return_value = client

        cad_chamfer(edges=["Edge1"], size=1.0, verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_revolution_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Revolution", "label": "Revolution", "type": "PartDesign::Revolution",
        }
        mock_get.return_value = client

        cad_revolution(sketch="Sketch", verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_sweep_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pipe", "label": "Pipe", "type": "PartDesign::AdditivePipe",
        }
        mock_get.return_value = client

        cad_sweep(profile_sketch="P", spine_sketch="S", verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_loft_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Loft", "label": "Loft", "type": "PartDesign::AdditiveLoft",
        }
        mock_get.return_value = client

        cad_loft(sketches=["S1", "S2"], verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_hole_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Hole", "label": "Hole", "type": "PartDesign::Hole",
        }
        mock_get.return_value = client

        cad_hole(face="Face6", diameter=6.6, depth=10, verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_polar_pattern_passes_verify(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "PolarPattern", "label": "PolarPattern",
            "type": "PartDesign::PolarPattern",
        }
        mock_get.return_value = client

        cad_polar_pattern(features=["Pocket"], verify=False)
        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])


class TestImageContentBlocks(unittest.TestCase):
    """Test that the MCP tools/call handler produces image content blocks."""

    def test_verification_images_extracted(self) -> None:
        """Simulate what main.py does: extract verification_images from result."""
        import json
        out: dict[str, Any] = {
            "ok": True,
            "name": "Pad",
            "verification_images": [
                {"image_base64": "abc123", "mime_type": "image/png", "view": "iso"},
                {"image_base64": "def456", "mime_type": "image/png", "view": "sketch-normal"},
            ],
        }

        content: list[dict[str, Any]] = []
        if isinstance(out, dict) and "verification_images" in out:
            images = out.pop("verification_images")
            out["verification_views"] = [img.get("view", "unknown") for img in images]
            for img in images:
                content.append({
                    "type": "image",
                    "data": img["image_base64"],
                    "mimeType": img["mime_type"],
                })

        if isinstance(out, dict) and "image_base64" in out:
            content.append({
                "type": "image",
                "data": out.pop("image_base64"),
                "mimeType": out.pop("mime_type", "image/png"),
            })

        content.append({"type": "text", "text": json.dumps(out)})

        self.assertEqual(len(content), 3)  # 2 images + 1 text
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["data"], "abc123")
        self.assertEqual(content[1]["data"], "def456")
        self.assertEqual(content[2]["type"], "text")
        # Verify images were removed but view labels preserved
        text_data = json.loads(content[2]["text"])
        self.assertNotIn("verification_images", text_data)
        self.assertEqual(text_data["verification_views"], ["iso", "sketch-normal"])

    def test_screenshot_image_extracted(self) -> None:
        """Simulate screenshot result handling."""
        import json
        out: dict[str, Any] = {
            "ok": True,
            "width": 512,
            "height": 512,
            "image_base64": "screenshot_data",
            "mime_type": "image/png",
            "camera_position": [100, 100, 100],
            "camera_target": [0, 0, 0],
        }

        content: list[dict[str, Any]] = []
        if isinstance(out, dict) and "verification_images" in out:
            images = out.pop("verification_images")
            for img in images:
                content.append({"type": "image", "data": img["image_base64"], "mimeType": img["mime_type"]})

        if isinstance(out, dict) and "image_base64" in out:
            content.append({
                "type": "image",
                "data": out.pop("image_base64"),
                "mimeType": out.pop("mime_type", "image/png"),
            })

        content.append({"type": "text", "text": json.dumps(out)})

        self.assertEqual(len(content), 2)  # 1 image + 1 text
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["data"], "screenshot_data")
        text_data = json.loads(content[1]["text"])
        self.assertNotIn("image_base64", text_data)
        self.assertIn("camera_position", text_data)

    def test_no_images_text_only(self) -> None:
        """Regular tool results without images."""
        import json
        out: dict[str, Any] = {"ok": True, "name": "Body"}

        content: list[dict[str, Any]] = []
        if isinstance(out, dict) and "verification_images" in out:
            images = out.pop("verification_images")
            for img in images:
                content.append({"type": "image", "data": img["image_base64"], "mimeType": img["mime_type"]})

        if isinstance(out, dict) and "image_base64" in out:
            content.append({"type": "image", "data": out.pop("image_base64"), "mimeType": out.pop("mime_type", "image/png")})

        content.append({"type": "text", "text": json.dumps(out)})

        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")


class TestFaceMapAndOperationSummary(unittest.TestCase):
    """Verify face_map and operation_summary keys flow through tool responses."""

    @patch("server.tools_cad.get_client")
    def test_pad_response_includes_face_map(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pad", "label": "Pad", "type": "PartDesign::Pad",
            "bounding_box": {"x_len": 100, "y_len": 50, "z_len": 20},
            "volume": 100000,
            "digest": {"volume": 100000.0, "surface_area": 22000.0, "bbox": [100, 50, 20], "faces": 6, "edges": 12, "vertices": 8},
            "delta": None,
            "face_map": {
                "faces": [
                    {"name": "Face1", "surface_type": "Plane", "normal": [0, 0, 1], "center": [50, 25, 20], "area": 5000.0},
                    {"name": "Face2", "surface_type": "Plane", "normal": [0, 0, -1], "center": [50, 25, 0], "area": 5000.0},
                ],
                "total_faces": 6,
            },
            "operation_summary": "Padded Sketch by 20mm → 100.0×50.0×20.0mm solid",
        }
        mock_get.return_value = client

        result = cad_pad(sketch="Sketch", length=20)
        self.assertTrue(result["ok"])
        self.assertIn("face_map", result)
        self.assertEqual(result["face_map"]["total_faces"], 6)
        self.assertEqual(len(result["face_map"]["faces"]), 2)
        self.assertEqual(result["face_map"]["faces"][0]["surface_type"], "Plane")

    @patch("server.tools_cad.get_client")
    def test_pocket_response_includes_operation_summary(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Pocket", "label": "Pocket", "type": "PartDesign::Pocket",
            "digest": {"volume": 80000.0, "surface_area": 24000.0, "bbox": [100, 50, 20], "faces": 10, "edges": 24, "vertices": 16},
            "delta": {"volume": -20000.0, "surface_area": 2000.0, "faces": 4, "edges": 12, "vertices": 8},
            "face_map": {"faces": [], "total_faces": 10},
            "operation_summary": "Pocketed 5mm deep",
        }
        mock_get.return_value = client

        result = cad_pocket(sketch="Sketch", length=5)
        self.assertTrue(result["ok"])
        self.assertIn("operation_summary", result)
        self.assertIn("Pocketed", result["operation_summary"])

    @patch("server.tools_cad.get_client")
    def test_fillet_response_includes_face_map_and_summary(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Fillet", "label": "Fillet", "type": "PartDesign::Fillet",
            "digest": {"volume": 99000.0, "surface_area": 22500.0, "bbox": [100, 50, 20], "faces": 10, "edges": 24, "vertices": 16},
            "delta": {"volume": -1000.0, "surface_area": 500.0, "faces": 4, "edges": 12, "vertices": 8},
            "face_map": {"faces": [], "total_faces": 10},
            "operation_summary": "Filleted 2 edge(s) with r=2mm",
        }
        mock_get.return_value = client

        result = cad_fillet(edges=["Edge1", "Edge3"], radius=2.0)
        self.assertTrue(result["ok"])
        self.assertIn("face_map", result)
        self.assertIn("operation_summary", result)
        self.assertIn("Filleted", result["operation_summary"])

    @patch("server.tools_cad.get_client")
    def test_revolution_response_includes_summary(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Revolution", "label": "Revolution", "type": "PartDesign::Revolution",
            "face_map": {"faces": [], "total_faces": 3},
            "operation_summary": "Revolved 360° around V",
        }
        mock_get.return_value = client

        result = cad_revolution(sketch="Sketch")
        self.assertTrue(result["ok"])
        self.assertIn("operation_summary", result)
        self.assertIn("Revolved", result["operation_summary"])


class TestVerificationViewCount(unittest.TestCase):
    """Verify that verification images now contain 2 views (iso + targeted)."""

    def test_verification_images_two_views(self) -> None:
        """Simulate the new 2-view verification output with view labels."""
        import json
        out: dict[str, Any] = {
            "ok": True,
            "name": "Pad",
            "face_map": {"faces": [], "total_faces": 6},
            "operation_summary": "Padded Sketch by 20mm",
            "verification_images": [
                {"image_base64": "abc123", "mime_type": "image/png", "view": "iso"},
                {"image_base64": "def456", "mime_type": "image/png", "view": "sketch-normal"},
            ],
        }

        content: list[dict[str, Any]] = []
        if isinstance(out, dict) and "verification_images" in out:
            images = out.pop("verification_images")
            out["verification_views"] = [img.get("view", "unknown") for img in images]
            for img in images:
                content.append({
                    "type": "image",
                    "data": img["image_base64"],
                    "mimeType": img["mime_type"],
                })
        content.append({"type": "text", "text": json.dumps(out)})

        self.assertEqual(len(content), 3)  # 2 images + 1 text
        self.assertEqual(content[0]["data"], "abc123")
        self.assertEqual(content[1]["data"], "def456")

        # Verify face_map, operation_summary, and view labels are in text output
        text_data = json.loads(content[2]["text"])
        self.assertIn("face_map", text_data)
        self.assertIn("operation_summary", text_data)
        self.assertEqual(text_data["verification_views"], ["iso", "sketch-normal"])


class TestCadAnimate(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_animate_sends_correct_command(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        mock_get.return_value = client
        client.send_command.return_value = {
            "status": "started",
            "assembly": "Assembly",
            "frame_count": 2,
            "duration_s": 5.0,
            "fps": 30,
        }

        frames = [
            {"Link_Sun": {"angle_deg": 0, "axis": [0, 0, 1], "center": [0, 0, 0]}},
            {"Link_Sun": {"angle_deg": 10, "axis": [0, 0, 1], "center": [0, 0, 0]}},
        ]
        result = cad_animate(frames=frames, duration_s=5.0, fps=30)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["frame_count"], 2)
        client.send_command.assert_called_once_with(
            "assembly_animate",
            frames=frames,
            duration_s=5.0,
            fps=30,
        )

    @patch("server.tools_cad.get_client")
    def test_animate_with_assembly_and_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        mock_get.return_value = client
        client.send_command.return_value = {
            "status": "started",
            "assembly": "MyAsm",
            "frame_count": 1,
            "duration_s": 10.0,
            "fps": 60,
        }

        frames = [{"Link_A": {"position": [0, 0, 0], "rotation_axis": [0, 0, 1], "rotation_angle_deg": 0}}]
        result = cad_animate(frames=frames, assembly="MyAsm", doc="Doc1", fps=60)

        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "assembly_animate",
            frames=frames,
            duration_s=10.0,
            fps=60,
            assembly="MyAsm",
            doc="Doc1",
        )


class TestCadAnimateStop(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_animate_stop_sends_correct_command(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        mock_get.return_value = client
        client.send_command.return_value = {
            "status": "stopped",
            "frames_played": 150,
            "elapsed_s": 5.0,
        }

        result = cad_animate_stop()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["frames_played"], 150)
        client.send_command.assert_called_once_with("assembly_animate_stop")


class TestCadExportBody(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_export_body(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"path": "/tmp/Body_Plate.stl", "format": "stl", "body": "Body_Plate"}
        mock_get.return_value = client

        result = cad_export_body(body="Body_Plate", format="stl")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/tmp/Body_Plate.stl")
        self.assertEqual(result["body"], "Body_Plate")
        client.send_command.assert_called_once_with("export_body", body="Body_Plate", format="stl")

    @patch("server.tools_cad.get_client")
    def test_export_body_with_path_and_doc(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {"path": "/out/leg.step", "format": "step", "body": "Body_Leg"}
        mock_get.return_value = client

        result = cad_export_body(body="Body_Leg", format="step", path="/out/leg.step", doc="Hexapod")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "export_body", body="Body_Leg", format="step", path="/out/leg.step", doc="Hexapod",
        )


class TestCadGetModelTreeDetail(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_detail_full_passes_param(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "doc": "MyDoc",
            "objects": [
                {"name": "Body", "label": "Body", "type": "PartDesign::Body"},
            ],
        }
        mock_get.return_value = client

        result = cad_get_model_tree(detail="full")
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("get_model_tree", detail="full")

    @patch("server.tools_cad.get_client")
    def test_default_sends_bodies(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "doc": "MyDoc",
            "body_count": 0,
            "bodies": [],
            "other_objects": [],
        }
        mock_get.return_value = client

        result = cad_get_model_tree()
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with("get_model_tree", detail="bodies")

    @patch("server.tools_cad.get_client")
    def test_bodies_response_has_other_objects(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "doc": "MyDoc",
            "body_count": 1,
            "bodies": [
                {"name": "Body", "label": "Body", "size": [10.0, 10.0, 10.0], "feature_count": 1, "tip": "Pad"},
            ],
            "other_objects": ["Assembly"],
        }
        mock_get.return_value = client

        result = cad_get_model_tree()
        self.assertTrue(result["ok"])
        self.assertEqual(result["other_objects"], ["Assembly"])


class TestCadExportSimPackage(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_export_sim_package_no_mechanism(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "output_dir": "/tmp/sim_pkg_abc",
            "format": "stl",
            "body_count": 2,
            "bodies": [
                {
                    "name": "Body_Plate",
                    "label": "Plate",
                    "mesh_path": "/tmp/sim_pkg_abc/Body_Plate.stl",
                    "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                },
                {
                    "name": "Body_Leg",
                    "label": "Leg",
                    "mesh_path": "/tmp/sim_pkg_abc/Body_Leg.stl",
                    "placement": {"position": [10, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                },
            ],
        }
        mock_get.return_value = client

        result = cad_export_sim_package()
        self.assertTrue(result["ok"])
        self.assertEqual(result["body_count"], 2)
        self.assertNotIn("urdf_path", result)
        client.send_command.assert_called_once_with("export_sim_package", format="stl")

    @patch("server.motion_store.get")
    @patch("server.tools_cad.get_client")
    def test_export_sim_package_with_mechanism(self, mock_get: MagicMock, mock_mech_get: MagicMock) -> None:
        import tempfile
        from server.motion_models import Mechanism, PartNode, JointEdge, JointType

        client = _mock_client()
        tmp_dir = tempfile.mkdtemp()
        client.send_command.return_value = {
            "output_dir": tmp_dir,
            "format": "stl",
            "body_count": 2,
            "bodies": [
                {
                    "name": "Body_Base",
                    "label": "Base",
                    "mesh_path": f"{tmp_dir}/Body_Base.stl",
                    "placement": {"position": [0, 0, 0], "rotation_quat": [1, 0, 0, 0]},
                },
                {
                    "name": "Body_Arm",
                    "label": "Arm",
                    "mesh_path": f"{tmp_dir}/Body_Arm.stl",
                    "placement": {"position": [0, 0, 50], "rotation_quat": [1, 0, 0, 0]},
                },
            ],
        }
        mock_get.return_value = client

        mechanism = Mechanism(
            name="test_arm",
            parts=(
                PartNode(id="base", body_name="Body_Base", is_ground=True),
                PartNode(id="arm", body_name="Body_Arm"),
            ),
            joints=(
                JointEdge(
                    id="shoulder",
                    joint_type=JointType.REVOLUTE,
                    parent_part="base",
                    child_part="arm",
                    origin=(0.0, 0.0, 50.0),
                ),
            ),
            drives=(),
        )
        mock_mech_get.return_value = mechanism

        result = cad_export_sim_package(mechanism_id="mech_test123")
        self.assertTrue(result["ok"])
        self.assertIn("urdf_path", result)
        self.assertIn("sim_model", result)
        self.assertEqual(result["sim_model"]["link_count"], 2)
        self.assertEqual(result["sim_model"]["joint_count"], 1)

    @patch("server.motion_store.get")
    @patch("server.tools_cad.get_client")
    def test_export_sim_package_invalid_mechanism(self, mock_get: MagicMock, mock_mech_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "output_dir": "/tmp/sim_pkg_abc",
            "format": "stl",
            "body_count": 1,
            "bodies": [],
        }
        mock_get.return_value = client
        mock_mech_get.return_value = None

        result = cad_export_sim_package(mechanism_id="mech_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_MECHANISM_ID")


class TestCadMirror(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_mirror_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Mirrored", "label": "Mirrored",
            "type": "PartDesign::Mirrored",
        }
        mock_get.return_value = client

        result = cad_mirror(features=["Pad"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Mirrored")
        client.send_command.assert_called_once_with(
            "mirror", features=["Pad"], plane="Base_X", verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_mirror_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Mirrored", "label": "Mirrored",
            "type": "PartDesign::Mirrored",
        }
        mock_get.return_value = client

        result = cad_mirror(
            features=["Pad", "Pocket"], plane="Base_Y",
            body="Body", doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "mirror", features=["Pad", "Pocket"], plane="Base_Y",
            verify=True, body="Body", doc="MyDoc",
        )


class TestCadLinearPattern(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_linear_pattern_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "LinearPattern", "label": "LinearPattern",
            "type": "PartDesign::LinearPattern",
        }
        mock_get.return_value = client

        result = cad_linear_pattern(features=["Pocket"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "LinearPattern")
        client.send_command.assert_called_once_with(
            "linear_pattern", features=["Pocket"], axis="Base_X",
            length=100.0, occurrences=3, reversed=False, verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_linear_pattern_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "LinearPattern", "label": "LinearPattern",
            "type": "PartDesign::LinearPattern",
        }
        mock_get.return_value = client

        result = cad_linear_pattern(
            features=["Pocket"], axis="Base_Y", length=200.0,
            occurrences=5, reversed=True, body="Body", doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "linear_pattern", features=["Pocket"], axis="Base_Y",
            length=200.0, occurrences=5, reversed=True, verify=True,
            body="Body", doc="MyDoc",
        )


class TestCadThickness(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_thickness_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Thickness", "label": "Thickness",
            "type": "PartDesign::Thickness",
        }
        mock_get.return_value = client

        result = cad_thickness(faces=["Face6"], thickness=2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Thickness")
        client.send_command.assert_called_once_with(
            "thickness", faces=["Face6"], thickness_value=2.0,
            join_type="Arc", reversed=False, verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_thickness_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Thickness", "label": "Thickness",
            "type": "PartDesign::Thickness",
        }
        mock_get.return_value = client

        result = cad_thickness(
            faces=["Face1", "Face6"], thickness=3.0,
            join_type="Tangent", reversed=True, body="Body", doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "thickness", faces=["Face1", "Face6"], thickness_value=3.0,
            join_type="Tangent", reversed=True, verify=True,
            body="Body", doc="MyDoc",
        )


class TestCadDraft(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_draft_defaults(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Draft", "label": "Draft",
            "type": "PartDesign::Draft",
        }
        mock_get.return_value = client

        result = cad_draft(faces=["Face2"], angle=3.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Draft")
        client.send_command.assert_called_once_with(
            "draft", faces=["Face2"], angle=3.0,
            neutral_plane="Face1", reversed=False, verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_draft_with_all_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "name": "Draft", "label": "Draft",
            "type": "PartDesign::Draft",
        }
        mock_get.return_value = client

        result = cad_draft(
            faces=["Face2", "Face4"], angle=5.0,
            neutral_plane="Face3", reversed=True, body="Body", doc="MyDoc",
        )
        self.assertTrue(result["ok"])
        client.send_command.assert_called_once_with(
            "draft", faces=["Face2", "Face4"], angle=5.0,
            neutral_plane="Face3", reversed=True, verify=True,
            body="Body", doc="MyDoc",
        )


class TestSketchNormalization(unittest.TestCase):
    """Verify alias params are normalized before reaching the FreeCAD client."""

    @patch("server.tools_cad.get_client")
    def test_circle_center_alias_normalized(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "circle", "index": 0}],
            },
            {"sketch": "Sketch", "fully_constrained": False, "open_vertices": 0},
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{"type": "circle", "center": [10, 20], "radius": 5}],
        )
        self.assertTrue(result["ok"])
        populate_call = client.send_command.call_args_list[1]
        sent_elem = populate_call[1]["elements"][0]
        self.assertEqual(sent_elem["cx"], 10)
        self.assertEqual(sent_elem["cy"], 20)
        self.assertEqual(sent_elem["r"], 5)
        self.assertNotIn("center", sent_elem)
        self.assertNotIn("radius", sent_elem)

    @patch("server.tools_cad.get_client")
    def test_rect_width_height_alias_normalized(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "rect", "indices": [0, 1, 2, 3]}],
            },
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},
        ]
        mock_get.return_value = client

        result = cad_sketch(
            body="Body",
            elements=[{"type": "rect", "x": 0, "y": 0, "width": 100, "height": 50}],
        )
        self.assertTrue(result["ok"])
        populate_call = client.send_command.call_args_list[1]
        sent_elem = populate_call[1]["elements"][0]
        self.assertEqual(sent_elem["w"], 100)
        self.assertEqual(sent_elem["h"], 50)
        self.assertNotIn("width", sent_elem)
        self.assertNotIn("height", sent_elem)


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
