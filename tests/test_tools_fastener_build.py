"""Tests for bolt and nut geometry building tools.

These tests mock the FreeCAD client to verify correct command sequences
without a live FreeCAD instance.
"""

from __future__ import annotations

import math
import unittest
from unittest.mock import MagicMock, patch

from server.fastener_data import match_bolt_size, nut_lookup
from server.tools_fastener_build import _hex_elements, cad_bolt, cad_find_holes, cad_nut


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.is_connected = True
    return client


class TestHexElements(unittest.TestCase):
    def test_six_lines(self) -> None:
        lines = _hex_elements(10.0)
        self.assertEqual(len(lines), 6)
        for el in lines:
            self.assertEqual(el["type"], "line")
            self.assertIn("x1", el)
            self.assertIn("y1", el)
            self.assertIn("x2", el)
            self.assertIn("y2", el)

    def test_closed_hexagon(self) -> None:
        """Last line endpoint should match first line start point."""
        lines = _hex_elements(10.0)
        self.assertAlmostEqual(lines[-1]["x2"], lines[0]["x1"], places=3)
        self.assertAlmostEqual(lines[-1]["y2"], lines[0]["y1"], places=3)

    def test_across_flats_dimension(self) -> None:
        """Min distance from center to an edge should equal across_flats/2."""
        af = 13.0
        lines = _hex_elements(af)
        # Collect all vertices
        verts = [(line["x1"], line["y1"]) for line in lines]
        r = math.sqrt(verts[0][0] ** 2 + verts[0][1] ** 2)
        # across_corners = 2*r, across_flats = across_corners * cos(30)
        computed_af = 2 * r * math.cos(math.radians(30))
        self.assertAlmostEqual(computed_af, af, places=2)


class TestNutLookup(unittest.TestCase):
    def test_hex_m4(self) -> None:
        spec = nut_lookup("M4", "hex")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.size, "M4")
        self.assertEqual(spec.across_flats, 7.0)
        self.assertEqual(spec.height, 3.2)
        self.assertEqual(spec.thread_diameter, 4.0)

    def test_thin_m6(self) -> None:
        spec = nut_lookup("M6", "thin")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.height, 3.2)

    def test_nyloc_m8(self) -> None:
        spec = nut_lookup("M8", "nyloc")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.height, 8.0)

    def test_across_corners_computed(self) -> None:
        spec = nut_lookup("M10", "hex")
        expected_ac = 16.0 / math.cos(math.radians(30))
        self.assertAlmostEqual(spec.across_corners, round(expected_ac, 2), places=2)

    def test_invalid_size(self) -> None:
        self.assertIsNone(nut_lookup("M99"))

    def test_invalid_type(self) -> None:
        self.assertIsNone(nut_lookup("M4", "wing"))

    def test_to_dict(self) -> None:
        spec = nut_lookup("M4", "hex")
        d = spec.to_dict()
        self.assertEqual(d["size"], "M4")
        self.assertEqual(d["nut_type"], "hex")
        self.assertIn("across_flats_mm", d)
        self.assertIn("height_mm", d)


class TestMatchBoltSize(unittest.TestCase):
    def test_m4_normal_fit(self) -> None:
        """4.5mm hole should match M4 normal fit."""
        match = match_bolt_size(4.5)
        self.assertIsNotNone(match)
        self.assertEqual(match["size"], "M4")
        self.assertEqual(match["fit"], "normal")

    def test_m6_close_fit(self) -> None:
        """6.4mm hole should match M6 close fit."""
        match = match_bolt_size(6.4)
        self.assertIsNotNone(match)
        self.assertEqual(match["size"], "M6")
        self.assertEqual(match["fit"], "close")

    def test_m8_loose_fit(self) -> None:
        """10.0mm hole should match M8 loose fit."""
        match = match_bolt_size(10.0)
        self.assertIsNotNone(match)
        self.assertEqual(match["size"], "M8")
        self.assertEqual(match["fit"], "loose")

    def test_no_match(self) -> None:
        """50mm hole should not match any standard bolt."""
        match = match_bolt_size(50.0)
        self.assertIsNone(match)

    def test_exact_match_zero_delta(self) -> None:
        """Exact clearance hole diameter should have 0 delta."""
        match = match_bolt_size(3.4)  # M3 normal
        self.assertIsNotNone(match)
        self.assertEqual(match["size"], "M3")
        self.assertAlmostEqual(match["delta_mm"], 0.0, places=3)


class TestCadBolt(unittest.TestCase):
    @patch("server.tools_fastener_build.get_client")
    def test_socket_head_bolt(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Bolt_M4_socket_head"},  # new_body
            {"sketch": "Sketch"},  # new_sketch (head)
            {"element_count": 1},  # sketch_populate
            {"fully_constrained": True},  # close_sketch
            {"feature": "Pad"},  # pad (head)
            {"sketch": "Sketch001"},  # new_sketch (shaft)
            {"element_count": 1},  # sketch_populate
            {"fully_constrained": True},  # close_sketch
            {"feature": "Pad001"},  # pad (shaft)
        ]
        mock_get.return_value = client

        result = cad_bolt(size="M4", length=12.0, head_type="socket_head")

        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "Bolt_M4_socket_head")
        self.assertEqual(result["size"], "M4")
        self.assertEqual(result["head_type"], "socket_head")
        self.assertEqual(result["length_mm"], 12.0)
        self.assertEqual(result["head_diameter_mm"], 7.0)
        self.assertEqual(result["head_height_mm"], 4.0)

        # Verify command sequence
        calls = client.send_command.call_args_list
        self.assertEqual(calls[0][0][0], "new_body")
        # Head sketch: circle
        self.assertEqual(calls[1][0][0], "new_sketch")
        self.assertEqual(calls[2][0][0], "sketch_populate")
        head_elements = calls[2][1]["elements"]
        self.assertEqual(len(head_elements), 1)
        self.assertEqual(head_elements[0]["type"], "circle")
        self.assertEqual(calls[3][0][0], "close_sketch")
        self.assertEqual(calls[4][0][0], "pad")
        # Shaft sketch: circle
        self.assertEqual(calls[5][0][0], "new_sketch")
        self.assertEqual(calls[6][0][0], "sketch_populate")
        shaft_elements = calls[6][1]["elements"]
        self.assertEqual(len(shaft_elements), 1)
        self.assertEqual(shaft_elements[0]["type"], "circle")
        self.assertAlmostEqual(shaft_elements[0]["r"], 2.0)  # M4 / 2
        self.assertEqual(calls[7][0][0], "close_sketch")
        self.assertEqual(calls[8][0][0], "pad")
        # Shaft pad should be reversed
        self.assertTrue(calls[8][1]["reversed"])

    @patch("server.tools_fastener_build.get_client")
    def test_hex_bolt_uses_hex_lines(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Bolt_M6_hex"},
            {"sketch": "Sketch"},
            {"element_count": 6},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad001"},
        ]
        mock_get.return_value = client

        result = cad_bolt(size="M6", length=20.0, head_type="hex")

        self.assertTrue(result["ok"])
        calls = client.send_command.call_args_list
        head_elements = calls[2][1]["elements"]
        self.assertEqual(len(head_elements), 6)
        for el in head_elements:
            self.assertEqual(el["type"], "line")

    @patch("server.tools_fastener_build.get_client")
    def test_set_screw_no_head(self, mock_get: MagicMock) -> None:
        """Set screws have no head — only shaft."""
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Bolt_M4_set_screw"},
            {"sketch": "Sketch"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad"},
        ]
        mock_get.return_value = client

        result = cad_bolt(size="M4", length=8.0, head_type="set_screw")

        self.assertTrue(result["ok"])
        # Only 5 commands: new_body + 1 sketch cycle (3) + 1 pad = 5
        self.assertEqual(client.send_command.call_count, 5)

    @patch("server.tools_fastener_build.get_client")
    def test_bolt_with_position(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "MyBolt"},
            {"sketch": "Sketch"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad001"},
            {},  # set_placement
        ]
        mock_get.return_value = client

        result = cad_bolt(size="M4", length=10.0, position=[50, 30, 0], name="MyBolt")

        self.assertTrue(result["ok"])
        self.assertEqual(result["position"], [50, 30, 0])
        # Last call should be set_placement
        last_call = client.send_command.call_args_list[-1]
        self.assertEqual(last_call[0][0], "set_placement")
        self.assertEqual(last_call[1]["position"], [50, 30, 0])

    @patch("server.tools_fastener_build.get_client")
    def test_bolt_with_rotation(self, mock_get: MagicMock) -> None:
        """Bolt with rotation should call set_placement with rotation params."""
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Bolt_M4_socket_head"},
            {"sketch": "Sketch"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad001"},
            {},  # set_placement
        ]
        mock_get.return_value = client

        result = cad_bolt(
            size="M4",
            length=10.0,
            position=[50, 30, 0],
            rotation_axis=[1, 0, 0],
            rotation_angle_deg=90.0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rotation_axis"], [1, 0, 0])
        self.assertEqual(result["rotation_angle_deg"], 90.0)
        last_call = client.send_command.call_args_list[-1]
        self.assertEqual(last_call[0][0], "set_placement")
        self.assertEqual(last_call[1]["rotation_axis"], [1, 0, 0])
        self.assertEqual(last_call[1]["rotation_angle_deg"], 90.0)
        self.assertEqual(last_call[1]["position"], [50, 30, 0])

    @patch("server.tools_fastener_build.get_client")
    def test_bolt_rotation_only_no_position(self, mock_get: MagicMock) -> None:
        """Rotation without position should still call set_placement."""
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Bolt_M4_socket_head"},
            {"sketch": "Sketch"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad001"},
            {},  # set_placement
        ]
        mock_get.return_value = client

        result = cad_bolt(
            size="M4",
            length=10.0,
            rotation_axis=[0, 1, 0],
            rotation_angle_deg=180.0,
        )

        self.assertTrue(result["ok"])
        last_call = client.send_command.call_args_list[-1]
        self.assertEqual(last_call[0][0], "set_placement")
        self.assertEqual(last_call[1]["rotation_axis"], [0, 1, 0])
        self.assertNotIn("position", last_call[1])

    def test_bolt_invalid_size(self) -> None:
        result = cad_bolt(size="M99", length=10.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "FASTENER_NOT_FOUND")

    def test_bolt_invalid_head_type(self) -> None:
        result = cad_bolt(size="M4", length=10.0, head_type="wing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "FASTENER_NOT_FOUND")

    @patch("server.tools_fastener_build.get_client")
    def test_bolt_custom_name(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Motor_Bolt_1"},
            {"sketch": "Sketch"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pad001"},
        ]
        mock_get.return_value = client

        result = cad_bolt(size="M3", length=8.0, name="Motor_Bolt_1")

        self.assertTrue(result["ok"])
        create_call = client.send_command.call_args_list[0]
        self.assertEqual(create_call[1]["name"], "Motor_Bolt_1")


class TestCadNut(unittest.TestCase):
    @patch("server.tools_fastener_build.get_client")
    def test_hex_nut(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Nut_M6_hex"},  # new_body
            {"sketch": "Sketch"},  # new_sketch (hex)
            {"element_count": 6},  # sketch_populate
            {"fully_constrained": True},  # close_sketch
            {"feature": "Pad"},  # pad
            {"sketch": "Sketch001"},  # new_sketch (hole)
            {"element_count": 1},  # sketch_populate
            {"fully_constrained": True},  # close_sketch
            {"feature": "Pocket"},  # pocket
        ]
        mock_get.return_value = client

        result = cad_nut(size="M6", nut_type="hex")

        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "Nut_M6_hex")
        self.assertEqual(result["size"], "M6")
        self.assertEqual(result["nut_type"], "hex")
        self.assertEqual(result["across_flats_mm"], 10.0)
        self.assertEqual(result["height_mm"], 5.2)

        calls = client.send_command.call_args_list
        # Hex pad
        hex_elements = calls[2][1]["elements"]
        self.assertEqual(len(hex_elements), 6)
        for el in hex_elements:
            self.assertEqual(el["type"], "line")
        self.assertEqual(calls[4][0][0], "pad")
        self.assertEqual(calls[4][1]["length"], 5.2)

        # Through hole pocket
        hole_elements = calls[6][1]["elements"]
        self.assertEqual(len(hole_elements), 1)
        self.assertEqual(hole_elements[0]["type"], "circle")
        self.assertAlmostEqual(hole_elements[0]["r"], 3.0)  # M6 / 2
        self.assertEqual(calls[8][0][0], "pocket")
        self.assertEqual(calls[8][1]["pocket_type"], "Through")

    @patch("server.tools_fastener_build.get_client")
    def test_nyloc_nut_taller(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Nut_M8_nyloc"},
            {"sketch": "Sketch"},
            {"element_count": 6},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pocket"},
        ]
        mock_get.return_value = client

        result = cad_nut(size="M8", nut_type="nyloc")

        self.assertTrue(result["ok"])
        self.assertEqual(result["height_mm"], 8.0)
        # Pad should use nyloc height
        pad_call = client.send_command.call_args_list[4]
        self.assertEqual(pad_call[1]["length"], 8.0)

    @patch("server.tools_fastener_build.get_client")
    def test_nut_with_position(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Nut_M4_hex"},
            {"sketch": "Sketch"},
            {"element_count": 6},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pocket"},
            {},  # set_placement
        ]
        mock_get.return_value = client

        result = cad_nut(size="M4", position=[10, 20, -5])

        self.assertTrue(result["ok"])
        self.assertEqual(result["position"], [10, 20, -5])
        last_call = client.send_command.call_args_list[-1]
        self.assertEqual(last_call[0][0], "set_placement")

    @patch("server.tools_fastener_build.get_client")
    def test_nut_with_rotation(self, mock_get: MagicMock) -> None:
        """Nut with rotation should call set_placement with rotation params."""
        client = _mock_client()
        client.send_command.side_effect = [
            {"body": "Nut_M4_hex"},
            {"sketch": "Sketch"},
            {"element_count": 6},
            {"fully_constrained": True},
            {"feature": "Pad"},
            {"sketch": "Sketch001"},
            {"element_count": 1},
            {"fully_constrained": True},
            {"feature": "Pocket"},
            {},  # set_placement
        ]
        mock_get.return_value = client

        result = cad_nut(
            size="M4",
            position=[10, 20, 0],
            rotation_axis=[1, 0, 0],
            rotation_angle_deg=90.0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rotation_axis"], [1, 0, 0])
        self.assertEqual(result["rotation_angle_deg"], 90.0)
        last_call = client.send_command.call_args_list[-1]
        self.assertEqual(last_call[0][0], "set_placement")
        self.assertEqual(last_call[1]["rotation_axis"], [1, 0, 0])

    def test_nut_invalid_size(self) -> None:
        result = cad_nut(size="M99")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NUT_NOT_FOUND")

    def test_nut_invalid_type(self) -> None:
        result = cad_nut(size="M4", nut_type="wing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NUT_NOT_FOUND")


class TestCadFindHoles(unittest.TestCase):
    @patch("server.tools_fastener_build.get_client")
    def test_find_holes_with_suggestions(self, mock_get: MagicMock) -> None:
        """find_holes should enrich results with bolt size suggestions."""
        client = _mock_client()
        client.send_command.return_value = {
            "body": "MainBody",
            "hole_count": 2,
            "holes": [
                {
                    "face": "Face3",
                    "diameter_mm": 4.5,
                    "radius_mm": 2.25,
                    "depth_mm": 12.0,
                    "axis": [0, 0, 1],
                    "center": [20, 30, 0],
                    "midpoint": [20, 30, 6],
                },
                {
                    "face": "Face5",
                    "diameter_mm": 6.6,
                    "radius_mm": 3.3,
                    "depth_mm": 15.0,
                    "axis": [0, 0, 1],
                    "center": [50, 30, 0],
                    "midpoint": [50, 30, 7.5],
                },
            ],
        }
        mock_get.return_value = client

        result = cad_find_holes(body="MainBody")

        self.assertTrue(result["ok"])
        self.assertEqual(result["hole_count"], 2)

        # First hole: 4.5mm → M4 normal fit
        hole1 = result["holes"][0]
        self.assertIn("suggested_bolt", hole1)
        self.assertEqual(hole1["suggested_bolt"]["size"], "M4")
        self.assertEqual(hole1["suggested_bolt"]["fit"], "normal")

        # Second hole: 6.6mm → M6 normal fit
        hole2 = result["holes"][1]
        self.assertIn("suggested_bolt", hole2)
        self.assertEqual(hole2["suggested_bolt"]["size"], "M6")

    @patch("server.tools_fastener_build.get_client")
    def test_find_holes_no_match(self, mock_get: MagicMock) -> None:
        """Non-standard hole diameter should have no suggestion."""
        client = _mock_client()
        client.send_command.return_value = {
            "body": "MainBody",
            "hole_count": 1,
            "holes": [
                {
                    "face": "Face3",
                    "diameter_mm": 50.0,
                    "radius_mm": 25.0,
                    "depth_mm": 20.0,
                    "axis": [0, 0, 1],
                    "center": [0, 0, 0],
                    "midpoint": [0, 0, 10],
                },
            ],
        }
        mock_get.return_value = client

        result = cad_find_holes()

        self.assertTrue(result["ok"])
        self.assertNotIn("suggested_bolt", result["holes"][0])

    @patch("server.tools_fastener_build.get_client")
    def test_find_holes_passes_filter_params(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Body",
            "hole_count": 0,
            "holes": [],
        }
        mock_get.return_value = client

        cad_find_holes(body="MyBody", min_diameter=3.0, max_diameter=10.0)

        call_kw = client.send_command.call_args[1]
        self.assertEqual(call_kw["body"], "MyBody")
        self.assertEqual(call_kw["min_diameter"], 3.0)
        self.assertEqual(call_kw["max_diameter"], 10.0)


class TestToolSchemas(unittest.TestCase):
    def test_bolt_tool_registered(self) -> None:
        from server import main as mcp_main

        names = {entry.get("name") for entry in mcp_main._tool_list()}
        self.assertIn("cad.bolt", names)

    def test_nut_tool_registered(self) -> None:
        from server import main as mcp_main

        names = {entry.get("name") for entry in mcp_main._tool_list()}
        self.assertIn("cad.nut", names)

    def test_find_holes_tool_registered(self) -> None:
        from server import main as mcp_main

        names = {entry.get("name") for entry in mcp_main._tool_list()}
        self.assertIn("cad.find_holes", names)

    def test_bolt_schema_has_rotation(self) -> None:
        from server import main as mcp_main

        tool = next(t for t in mcp_main._tool_list() if t["name"] == "cad.bolt")
        props = tool["inputSchema"]["properties"]
        self.assertIn("size", props)
        self.assertIn("length", props)
        self.assertIn("head_type", props)
        self.assertIn("position", props)
        self.assertIn("rotation_axis", props)
        self.assertIn("rotation_angle_deg", props)
        self.assertIn("name", props)
        self.assertEqual(set(tool["inputSchema"]["required"]), {"size", "length"})

    def test_nut_schema_has_rotation(self) -> None:
        from server import main as mcp_main

        tool = next(t for t in mcp_main._tool_list() if t["name"] == "cad.nut")
        props = tool["inputSchema"]["properties"]
        self.assertIn("size", props)
        self.assertIn("nut_type", props)
        self.assertIn("position", props)
        self.assertIn("rotation_axis", props)
        self.assertIn("rotation_angle_deg", props)
        self.assertEqual(tool["inputSchema"]["required"], ["size"])
        self.assertEqual(props["nut_type"]["enum"], ["hex", "thin", "nyloc"])

    def test_find_holes_schema(self) -> None:
        from server import main as mcp_main

        tool = next(t for t in mcp_main._tool_list() if t["name"] == "cad.find_holes")
        props = tool["inputSchema"]["properties"]
        self.assertIn("body", props)
        self.assertIn("min_diameter", props)
        self.assertIn("max_diameter", props)


if __name__ == "__main__":
    unittest.main()
