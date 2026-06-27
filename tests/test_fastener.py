"""Tests for fastener dimension lookup."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from server.fastener_data import SUPPORTED_SIZES, lookup
from server.tools_fastener import cad_fastener_spec


class TestFastenerLookup(unittest.TestCase):

    def test_m4_socket_head(self) -> None:
        spec = lookup("M4", 20.0, "socket_head")
        assert spec is not None
        self.assertEqual(spec.size, "M4")
        self.assertEqual(spec.thread_diameter, 4.0)
        self.assertAlmostEqual(spec.pitch_coarse, 0.7)
        self.assertAlmostEqual(spec.head_diameter, 7.0)
        self.assertAlmostEqual(spec.head_height, 4.0)
        self.assertAlmostEqual(spec.socket_size, 3.0)
        self.assertAlmostEqual(spec.through_hole_normal, 4.5)
        self.assertAlmostEqual(spec.counterbore_diameter, 8.0)  # 7.0 + 1.0
        self.assertAlmostEqual(spec.counterbore_depth, 4.5)     # 4.0 + 0.5
        self.assertEqual(spec.length, 20.0)
        self.assertAlmostEqual(spec.tap_drill_coarse, 3.3)

    def test_m8_hex(self) -> None:
        spec = lookup("M8", 30.0, "hex")
        assert spec is not None
        self.assertEqual(spec.head_type, "hex")
        self.assertAlmostEqual(spec.head_diameter, 13.0)
        self.assertAlmostEqual(spec.head_height, 5.3)
        self.assertAlmostEqual(spec.through_hole_close, 8.4)
        self.assertAlmostEqual(spec.through_hole_loose, 10.0)
        self.assertAlmostEqual(spec.pitch_fine, 1.0)
        self.assertAlmostEqual(spec.tap_drill_fine, 7.0)

    def test_m6_countersunk(self) -> None:
        spec = lookup("M6", 25.0, "countersunk")
        assert spec is not None
        self.assertAlmostEqual(spec.countersink_diameter, 13.44)
        self.assertAlmostEqual(spec.countersink_angle, 90.0)
        self.assertAlmostEqual(spec.counterbore_diameter, 0.0)  # no counterbore

    def test_m3_button_head(self) -> None:
        spec = lookup("M3", 10.0, "button_head")
        assert spec is not None
        self.assertAlmostEqual(spec.head_diameter, 5.7)
        self.assertAlmostEqual(spec.head_height, 1.65)
        self.assertAlmostEqual(spec.socket_size, 2.0)

    def test_m5_set_screw(self) -> None:
        spec = lookup("M5", 8.0, "set_screw")
        assert spec is not None
        self.assertAlmostEqual(spec.head_diameter, 0.0)
        self.assertAlmostEqual(spec.head_height, 0.0)
        self.assertAlmostEqual(spec.socket_size, 5.0)  # equals thread dia

    def test_case_insensitive(self) -> None:
        spec = lookup("m4", 20.0)
        assert spec is not None
        self.assertEqual(spec.size, "M4")

    def test_unknown_size(self) -> None:
        self.assertIsNone(lookup("M7", 20.0))

    def test_unknown_head_type(self) -> None:
        self.assertIsNone(lookup("M4", 20.0, "torx_head"))

    def test_all_sizes_have_socket_head(self) -> None:
        for size in SUPPORTED_SIZES:
            spec = lookup(size, 20.0, "socket_head")
            self.assertIsNotNone(spec, f"Missing socket_head data for {size}")

    def test_washer_dimensions(self) -> None:
        spec = lookup("M6", 20.0)
        assert spec is not None
        self.assertAlmostEqual(spec.washer_od, 12.0)
        self.assertAlmostEqual(spec.washer_thickness, 1.6)

    def test_clearance_hole_ordering(self) -> None:
        """Close < normal < loose for all sizes."""
        for size in SUPPORTED_SIZES:
            spec = lookup(size, 20.0)
            assert spec is not None
            self.assertLess(spec.through_hole_close, spec.through_hole_normal,
                            f"{size}: close >= normal")
            self.assertLess(spec.through_hole_normal, spec.through_hole_loose,
                            f"{size}: normal >= loose")

    def test_to_dict(self) -> None:
        spec = lookup("M4", 20.0)
        assert spec is not None
        d = spec.to_dict()
        self.assertEqual(d["size"], "M4")
        self.assertEqual(d["length_mm"], 20.0)
        self.assertIn("through_hole_normal_mm", d)
        self.assertIn("counterbore_diameter_mm", d)


class TestFastenerTool(unittest.TestCase):

    def test_basic_call(self) -> None:
        result = cad_fastener_spec(size="M4", length=20.0)
        self.assertTrue(result["ok"])
        f = result["fastener"]
        self.assertEqual(f["size"], "M4")
        self.assertEqual(f["length_mm"], 20.0)
        self.assertAlmostEqual(f["head_diameter_mm"], 7.0)

    def test_with_head_type(self) -> None:
        result = cad_fastener_spec(size="M6", length=25.0, head_type="hex")
        self.assertTrue(result["ok"])
        self.assertEqual(result["fastener"]["head_type"], "hex")

    def test_invalid_size(self) -> None:
        result = cad_fastener_spec(size="M7", length=20.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "FASTENER_NOT_FOUND")

    def test_invalid_head_type(self) -> None:
        result = cad_fastener_spec(size="M4", length=20.0, head_type="star")
        self.assertFalse(result["ok"])


def _mock_client() -> MagicMock:
    """Create a mock FreeCAD client."""
    client = MagicMock()
    client.is_connected = True
    return client


class TestFastenerFreeCADBuild(unittest.TestCase):
    """Integration test: fastener spec → build bolt hole pattern in FreeCAD.

    Simulates the workflow an LLM would follow:
    1. Look up M4 socket head bolt dimensions
    2. Create a base plate (sketch + pad)
    3. Use through_hole_normal for the clearance hole
    4. Use counterbore dims for the counterbore pocket
    5. Add a bolt pattern (linear_pattern of 4 holes)
    """

    @patch("server.tools_cad.get_client")
    def test_m4_mounting_hole_workflow(self, mock_get: MagicMock) -> None:
        """Simulate: fastener_spec → sketch plate → pad → hole → linear_pattern."""
        from server.tools_cad import (
            cad_hole,
            cad_linear_pattern,
            cad_new_body,
            cad_new_document,
            cad_pad,
            cad_sketch,
        )

        # Step 1: Look up M4 socket head bolt
        spec_result = cad_fastener_spec(size="M4", length=20.0, head_type="socket_head")
        self.assertTrue(spec_result["ok"])
        f = spec_result["fastener"]

        # Verify the dimensions we'll use
        self.assertAlmostEqual(f["through_hole_normal_mm"], 4.5)
        self.assertAlmostEqual(f["counterbore_diameter_mm"], 8.0)
        self.assertAlmostEqual(f["counterbore_depth_mm"], 4.5)
        self.assertAlmostEqual(f["thread_diameter_mm"], 4.0)

        # Step 2: Set up mock FreeCAD client
        client = _mock_client()
        mock_get.return_value = client

        # Create document
        client.send_command.return_value = {"name": "Bracket", "label": "Bracket"}
        doc = cad_new_document(name="Bracket")
        self.assertTrue(doc["ok"])

        # Create body
        client.send_command.return_value = {"name": "Body", "label": "Body"}
        body = cad_new_body()
        self.assertTrue(body["ok"])

        # Step 3: Sketch base plate (40x40mm rect)
        client.send_command.side_effect = [
            {"sketch": "Sketch"},
            {
                "sketch": "Sketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "rect", "index": 0}],
            },
            {"sketch": "Sketch", "fully_constrained": True, "open_vertices": 0},
        ]
        sketch = cad_sketch(
            body="Body",
            elements=[{"type": "rect", "x": -20, "y": -20, "w": 40, "h": 40}],
        )
        self.assertTrue(sketch["ok"])

        # Step 4: Pad the base plate (5mm thick)
        client.send_command.side_effect = None
        client.send_command.return_value = {
            "name": "Pad",
            "label": "Pad",
            "type": "PartDesign::Pad",
            "bounding_box": {"x_len": 40, "y_len": 40, "z_len": 5},
        }
        pad = cad_pad(sketch="Sketch", length=5.0)
        self.assertTrue(pad["ok"])

        # Step 5: Add through-hole using fastener specs
        # Use the through_hole_normal_mm for the clearance hole diameter
        client.send_command.return_value = {
            "name": "Hole",
            "label": "Hole",
            "type": "PartDesign::Hole",
        }
        hole = cad_hole(
            face="Face6",  # top face of the pad
            diameter=f["through_hole_normal_mm"],
            depth=5.0,  # through the plate
        )
        self.assertTrue(hole["ok"])

        # Verify the hole command used the correct diameter from fastener spec
        hole_call = client.send_command.call_args
        self.assertAlmostEqual(hole_call[1]["diameter"], 4.5)
        self.assertAlmostEqual(hole_call[1]["depth"], 5.0)

        # Step 6: Linear pattern for M4 bolt pattern (e.g. 4 holes, 16mm spacing)
        client.send_command.return_value = {
            "name": "LinearPattern",
            "label": "LinearPattern",
            "type": "PartDesign::LinearPattern",
        }
        client.send_command.reset_mock()
        client.send_command.side_effect = None
        client.send_command.return_value = {
            "name": "LinearPattern",
            "label": "LinearPattern",
            "type": "PartDesign::LinearPattern",
        }
        pattern = cad_linear_pattern(
            features=["Hole"],
            axis="Base_X",
            length=16.0,
            occurrences=2,
        )
        self.assertTrue(pattern["ok"])
        client.send_command.assert_called_once_with(
            "linear_pattern",
            features=["Hole"],
            axis="Base_X",
            length=16.0,
            occurrences=2,
            reversed=False,
            verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_countersunk_hole_workflow(self, mock_get: MagicMock) -> None:
        """Simulate building a countersunk M6 bolt hole."""
        from server.tools_cad import cad_hole

        # Look up countersunk M6
        spec_result = cad_fastener_spec(size="M6", length=25.0, head_type="countersunk")
        self.assertTrue(spec_result["ok"])
        f = spec_result["fastener"]

        # Countersunk bolts have countersink, not counterbore
        self.assertAlmostEqual(f["countersink_diameter_mm"], 13.44)
        self.assertAlmostEqual(f["countersink_angle_deg"], 90.0)
        self.assertAlmostEqual(f["counterbore_diameter_mm"], 0.0)

        # Mock the hole command
        client = _mock_client()
        mock_get.return_value = client
        client.send_command.return_value = {
            "name": "Hole",
            "label": "Hole",
            "type": "PartDesign::Hole",
        }

        # Use through-hole normal for the shaft clearance
        hole = cad_hole(
            face="Face1",
            diameter=f["through_hole_normal_mm"],
            depth=10.0,
        )
        self.assertTrue(hole["ok"])
        self.assertAlmostEqual(
            client.send_command.call_args[1]["diameter"],
            6.6,  # M6 normal clearance
        )

    @patch("server.tools_cad.get_client")
    def test_m3_motor_mount_bolt_pattern(self, mock_get: MagicMock) -> None:
        """Simulate M3 bolt pattern for a motor mount (common drone pattern).

        Workflow: fastener_spec → sketch 4 circles at 16mm square pattern →
        pocket through for clearance holes.
        """
        from server.tools_cad import cad_pocket, cad_sketch

        # Step 1: Look up M3 socket head
        spec_result = cad_fastener_spec(size="M3", length=8.0, head_type="socket_head")
        self.assertTrue(spec_result["ok"])
        f = spec_result["fastener"]
        hole_dia = f["through_hole_normal_mm"]  # 3.4mm
        self.assertAlmostEqual(hole_dia, 3.4)

        # Step 2: Sketch 4 holes in a 16mm square pattern (motor mount)
        client = _mock_client()
        mock_get.return_value = client

        # Build 4 circle elements using the fastener clearance hole size
        spacing = 8.0  # half of 16mm pattern
        hole_elements = [
            {"type": "circle", "cx": -spacing, "cy": -spacing, "r": hole_dia / 2},
            {"type": "circle", "cx": spacing, "cy": -spacing, "r": hole_dia / 2},
            {"type": "circle", "cx": spacing, "cy": spacing, "r": hole_dia / 2},
            {"type": "circle", "cx": -spacing, "cy": spacing, "r": hole_dia / 2},
        ]

        client.send_command.side_effect = [
            {"sketch": "MountSketch"},
            {
                "sketch": "MountSketch",
                "element_count": 4,
                "constraint_count": 0,
                "geometry": [
                    {"type": "circle", "index": i} for i in range(4)
                ],
            },
            {"sketch": "MountSketch", "fully_constrained": True, "open_vertices": 0},
        ]

        sketch = cad_sketch(
            body="Body",
            plane="Face6",
            elements=hole_elements,
        )
        self.assertTrue(sketch["ok"])
        self.assertEqual(len(sketch["geometry"]), 4)

        # Verify the sketch received circles with the correct radius
        populate_call = [
            c for c in client.send_command.call_args_list
            if c[0][0] == "sketch_populate"
        ][0]
        sent_elements = populate_call[1]["elements"]
        self.assertEqual(len(sent_elements), 4)
        for elem in sent_elements:
            self.assertEqual(elem["type"], "circle")
            self.assertAlmostEqual(elem["r"], 1.7)  # 3.4 / 2

        # Step 3: Pocket through
        client.send_command.reset_mock()
        client.send_command.side_effect = None
        client.send_command.return_value = {
            "name": "Pocket",
            "label": "Pocket",
            "type": "PartDesign::Pocket",
            "bounding_box": {"x_len": 20, "y_len": 20, "z_len": 3},
        }
        pocket = cad_pocket(sketch="MountSketch", length=0, pocket_type="ThroughAll")
        self.assertTrue(pocket["ok"])
        client.send_command.assert_called_once_with(
            "pocket",
            sketch="MountSketch",
            length=0,
            pocket_type="ThroughAll",
            reversed="auto",
            verify=True,
        )

    def test_fastener_spec_to_cad_hole_dimension_chain(self) -> None:
        """Verify the dimension chain from spec to CAD parameters is correct.

        For a socket head bolt, the counterbore must clear the head:
        - counterbore_diameter > head_diameter
        - counterbore_depth > head_height
        - through_hole_normal > thread_diameter
        """
        for size in SUPPORTED_SIZES:
            spec = lookup(size, 20.0, "socket_head")
            if spec is None:
                continue
            self.assertGreater(
                spec.counterbore_diameter, spec.head_diameter,
                f"{size}: counterbore must clear head",
            )
            self.assertGreater(
                spec.counterbore_depth, spec.head_height,
                f"{size}: counterbore must be deeper than head",
            )
            self.assertGreater(
                spec.through_hole_normal, spec.thread_diameter,
                f"{size}: clearance hole must be larger than thread",
            )
            self.assertGreater(
                spec.tap_drill_coarse, 0,
                f"{size}: tap drill must be positive",
            )


if __name__ == "__main__":
    unittest.main()
