"""Tests for geometry.propeller_blade tool."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestPropellerBladeTool(unittest.TestCase):
    """Test the Python tool wrapper (geometry_propeller_blade)."""

    def setUp(self) -> None:
        """Build a mock Rust result matching propeller_blade_py output."""
        self.mock_section = {
            "elements": [
                {"type": "spline", "points": [[0, 0], [1, 1]], "degree": 3, "periodic": False},
                {"type": "spline", "points": [[0, 0], [1, -1]], "degree": 3, "periodic": False},
            ],
            "station_radius_mm": 25.0,
            "chord_mm": 28.5,
            "twist_deg": 32.1,
            "plane_offset_mm": 25.0,
        }
        self.mock_result = {
            "sections": [dict(self.mock_section) for _ in range(6)],
            "hub": {
                "elements": [{"type": "circle", "cx": 0, "cy": 0, "r": 10}],
                "diameter_mm": 20.0,
                "height_mm": 12.0,
            },
            "blade_table": {
                "r_frac": [0.15, 0.30, 0.45, 0.60, 0.75, 0.90],
                "chord_mm": [28.5, 25.2, 21.8, 18.5, 15.1, 11.8],
                "twist_deg": [32.1, 22.5, 16.8, 13.2, 10.8, 9.1],
                "Re_at_5000rpm": [45000, 67000, 82000, 93000, 101000, 106000],
            },
            "airfoil_dat": "NACA4412\n  1.000000  0.000000\n  0.950000  0.011234\n",
            "params": {
                "diameter_mm": 254.0,
                "pitch_mm": 114.0,
                "hub_diameter_mm": 20.0,
                "num_blades": 2,
                "airfoil": "NACA4412",
                "chord_root_mm": 30.0,
                "chord_tip_mm": 12.0,
                "num_sections": 6,
                "num_points": 40,
            },
        }

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_returns_sections_with_geometry_refs(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.return_value = self.mock_result

        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sections"]), 6)

        for sec in result["sections"]:
            self.assertIn("geometry_ref", sec)
            self.assertIn("station_radius_mm", sec)
            self.assertIn("chord_mm", sec)
            self.assertIn("twist_deg", sec)
            self.assertIn("plane_offset_mm", sec)
            # Elements should NOT be in the output (stored server-side)
            self.assertNotIn("elements", sec)

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_hub_has_geometry_ref(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.return_value = self.mock_result

        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        hub = result["hub"]
        self.assertIn("geometry_ref", hub)
        self.assertIn("diameter_mm", hub)
        self.assertIn("height_mm", hub)
        self.assertNotIn("elements", hub)

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_blade_table_fields(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.return_value = self.mock_result

        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        bt = result["blade_table"]
        self.assertEqual(len(bt["r_frac"]), 6)
        self.assertEqual(len(bt["chord_mm"]), 6)
        self.assertEqual(len(bt["twist_deg"]), 6)
        self.assertEqual(len(bt["Re_at_5000rpm"]), 6)

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_airfoil_dat_is_string(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.return_value = self.mock_result

        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        self.assertIsInstance(result["airfoil_dat"], str)
        self.assertGreater(len(result["airfoil_dat"]), 0)
        self.assertTrue(result["airfoil_dat"].startswith("NACA"))

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_has_build_hint(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.return_value = self.mock_result

        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        self.assertIn("build_hint", result)
        self.assertIn("cad.sketch", result["build_hint"])

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_error_invalid_naca(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.side_effect = ValueError("Invalid NACA 4-digit code 'NACA123'")

        from server.tools_geometry import geometry_propeller_blade

        with self.assertRaises(ValueError):
            geometry_propeller_blade(
                diameter=254.0,
                pitch=114.0,
                hub_diameter=20.0,
                airfoil="NACA123",
            )

    @patch("server.tools_geometry._geom")
    @patch("server.tools_geometry._AVAILABLE", True)
    def test_error_hub_larger_than_diameter(self, mock_geom: MagicMock) -> None:
        mock_geom.propeller_blade_py.side_effect = ValueError("hub_diameter must be < diameter")

        from server.tools_geometry import geometry_propeller_blade

        with self.assertRaises(ValueError):
            geometry_propeller_blade(
                diameter=100.0,
                pitch=50.0,
                hub_diameter=120.0,
            )


class TestBEMTBladeTableIntegration(unittest.TestCase):
    """Test that _bemt_solve accepts a blade_table parameter."""

    def test_interp_blade_table(self) -> None:
        from server.study_solvers import _interp_blade_table

        r_table = [0.15, 0.30, 0.45, 0.60, 0.75, 0.90]
        chord_table = [28.5, 25.2, 21.8, 18.5, 15.1, 11.8]
        twist_table = [32.1, 22.5, 16.8, 13.2, 10.8, 9.1]

        # Exact match at first station
        c, t = _interp_blade_table(0.15, r_table, chord_table, twist_table)
        self.assertAlmostEqual(c, 28.5)
        self.assertAlmostEqual(t, 32.1)

        # Exact match at last station
        c, t = _interp_blade_table(0.90, r_table, chord_table, twist_table)
        self.assertAlmostEqual(c, 11.8)
        self.assertAlmostEqual(t, 9.1)

        # Clamp below range
        c, t = _interp_blade_table(0.05, r_table, chord_table, twist_table)
        self.assertAlmostEqual(c, 28.5)
        self.assertAlmostEqual(t, 32.1)

        # Clamp above range
        c, t = _interp_blade_table(0.99, r_table, chord_table, twist_table)
        self.assertAlmostEqual(c, 11.8)
        self.assertAlmostEqual(t, 9.1)

        # Midpoint interpolation between stations 0 and 1
        c, t = _interp_blade_table(0.225, r_table, chord_table, twist_table)
        expected_c = 28.5 + 0.5 * (25.2 - 28.5)
        expected_t = 32.1 + 0.5 * (22.5 - 32.1)
        self.assertAlmostEqual(c, expected_c)
        self.assertAlmostEqual(t, expected_t)


class TestPropellerRustIntegration(unittest.TestCase):
    """Test the real Rust extension (propeller_blade_py) end-to-end."""

    def setUp(self) -> None:
        from server.geometry_store import clear

        clear()

    def tearDown(self) -> None:
        from server.geometry_store import clear

        clear()

    def test_real_blade_sections_have_valid_refs(self) -> None:
        """Call the real Rust extension and verify geometry_refs resolve."""
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
            num_sections=4,
            num_points=20,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sections"]), 4)

        # Each section ref should resolve to spline elements
        for sec in result["sections"]:
            elems = retrieve(sec["geometry_ref"])
            self.assertIsNotNone(elems, f"ref {sec['geometry_ref']} not in store")
            self.assertEqual(len(elems), 2, "expect upper + lower spline")
            for e in elems:
                self.assertEqual(e["type"], "spline")
                self.assertGreater(len(e["points"]), 5)

        # Hub ref should resolve to a circle
        hub_elems = retrieve(result["hub"]["geometry_ref"])
        self.assertIsNotNone(hub_elems)
        self.assertEqual(len(hub_elems), 1)
        self.assertEqual(hub_elems[0]["type"], "circle")

    def test_blade_table_twist_decreasing(self) -> None:
        """Verify twist decreases root→tip (positive pitch)."""
        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        twists = result["blade_table"]["twist_deg"]
        for i in range(1, len(twists)):
            self.assertLess(twists[i], twists[i - 1])

    def test_airfoil_dat_selig_format(self) -> None:
        """Verify airfoil_dat is valid Selig format."""
        from server.tools_geometry import geometry_propeller_blade

        result = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        lines = result["airfoil_dat"].strip().split("\n")
        self.assertTrue(lines[0].startswith("NACA"))
        for line in lines[1:]:
            parts = line.split()
            self.assertEqual(len(parts), 2)
            x, _y = float(parts[0]), float(parts[1])
            # x should be in [0, 1+epsilon] for normalized airfoil
            self.assertGreaterEqual(x, -0.01)
            self.assertLessEqual(x, 1.01)

    def test_error_on_invalid_naca(self) -> None:
        from server.tools_geometry import geometry_propeller_blade

        with self.assertRaises(ValueError):
            geometry_propeller_blade(
                diameter=254.0,
                pitch=114.0,
                hub_diameter=20.0,
                airfoil="NACA123",
            )

    def test_error_hub_exceeds_diameter(self) -> None:
        from server.tools_geometry import geometry_propeller_blade

        with self.assertRaises(ValueError):
            geometry_propeller_blade(
                diameter=100.0,
                pitch=50.0,
                hub_diameter=120.0,
            )


def _mock_client() -> MagicMock:
    """Create a mock FreeCAD client."""
    client = MagicMock()
    client.is_connected = True
    return client


class TestPropellerFreeCADBuild(unittest.TestCase):
    """Integration test: generate propeller blade → sketch sections → loft → polar pattern.

    Simulates the full workflow that an LLM would execute after calling
    geometry.propeller_blade, using a mocked FreeCAD client.
    """

    def setUp(self) -> None:
        from server.geometry_store import clear

        clear()

    def tearDown(self) -> None:
        from server.geometry_store import clear

        clear()

    @patch("server.tools_cad.get_client")
    def test_full_propeller_build_workflow(self, mock_get: MagicMock) -> None:
        """Simulate: propeller_blade → sketch each section → loft → polar_pattern."""
        from server.tools_cad import (
            cad_loft,
            cad_new_body,
            cad_new_document,
            cad_polar_pattern,
            cad_sketch,
        )
        from server.tools_geometry import geometry_propeller_blade

        # Step 1: Generate blade geometry
        blade = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
            num_blades=3,
            num_sections=4,
            num_points=20,
        )
        self.assertTrue(blade["ok"])
        sections = blade["sections"]
        self.assertEqual(len(sections), 4)

        # Step 2: Mock FreeCAD client
        client = _mock_client()
        mock_get.return_value = client

        # new_document response
        client.send_command.return_value = {"name": "Propeller", "label": "Propeller"}
        doc_result = cad_new_document(name="Propeller")
        self.assertTrue(doc_result["ok"])

        # new_body response
        client.send_command.return_value = {"name": "Blade", "label": "Blade"}
        body_result = cad_new_body(name="Blade")
        self.assertTrue(body_result["ok"])

        # Step 3: Sketch each section using geometry_ref
        sketch_names = []
        for i, sec in enumerate(sections):
            sketch_name = f"Sketch{i}"
            # Mock responses for: new_sketch → sketch_populate → close_sketch
            client.send_command.side_effect = [
                {"sketch": sketch_name},  # new_sketch
                {
                    "sketch": sketch_name,
                    "element_count": 2,
                    "constraint_count": 0,
                    "geometry": [
                        {"type": "spline", "index": 0},
                        {"type": "spline", "index": 1},
                    ],
                },  # sketch_populate
                {
                    "sketch": sketch_name,
                    "fully_constrained": False,
                    "open_vertices": 0,
                },  # close_sketch
            ]

            result = cad_sketch(
                body="Blade",
                plane=f"XZ_offset_{sec['plane_offset_mm']:.1f}",
                geometry_ref=sec["geometry_ref"],
            )
            self.assertTrue(result["ok"], f"Section {i} sketch failed: {result}")
            self.assertEqual(len(result["geometry"]), 2)
            sketch_names.append(sketch_name)

        self.assertEqual(len(sketch_names), 4)

        # Step 4: Verify sketch_populate received spline elements from the geometry_ref
        # Each sketch call generates 3 send_command calls; the 2nd has the elements
        # Check the last sketch's populate call
        populate_calls = [
            c for c in client.send_command.call_args_list if c[0][0] == "sketch_populate"
        ]
        self.assertGreater(len(populate_calls), 0)
        # The elements kwarg should contain spline data
        last_populate = populate_calls[-1]
        elements_sent = last_populate[1]["elements"]
        self.assertEqual(len(elements_sent), 2)
        for elem in elements_sent:
            self.assertEqual(elem["type"], "spline")
            self.assertIn("points", elem)

        # Step 5: Loft across all sections
        client.send_command.reset_mock()
        client.send_command.side_effect = None
        client.send_command.return_value = {
            "name": "Loft",
            "label": "Loft",
            "type": "PartDesign::AdditiveLoft",
            "bounding_box": {"x_len": 120, "y_len": 30, "z_len": 120},
        }

        loft_result = cad_loft(sketches=sketch_names)
        self.assertTrue(loft_result["ok"])
        client.send_command.assert_called_once_with(
            "loft",
            sketches=sketch_names,
            ruled=False,
            closed=False,
            subtractive=False,
            verify=True,
        )

        # Step 6: Polar pattern to replicate blade around hub
        client.send_command.reset_mock()
        client.send_command.return_value = {
            "name": "PolarPattern",
            "label": "PolarPattern",
            "type": "PartDesign::PolarPattern",
        }

        pattern_result = cad_polar_pattern(
            features=["Loft"],
            occurrences=blade["params"]["num_blades"],
            angle=360.0,
        )
        self.assertTrue(pattern_result["ok"])
        client.send_command.assert_called_once_with(
            "polar_pattern",
            timeout=120.0,
            features=["Loft"],
            axis="Base_Z",
            occurrences=3,
            angle=360.0,
            reversed=False,
            verify=True,
        )

    @patch("server.tools_cad.get_client")
    def test_hub_sketch_from_geometry_ref(self, mock_get: MagicMock) -> None:
        """Verify the hub geometry_ref produces a valid circle sketch."""
        from server.tools_cad import cad_sketch
        from server.tools_geometry import geometry_propeller_blade

        blade = geometry_propeller_blade(
            diameter=254.0,
            pitch=114.0,
            hub_diameter=20.0,
        )
        hub_ref = blade["hub"]["geometry_ref"]

        client = _mock_client()
        mock_get.return_value = client
        client.send_command.side_effect = [
            {"sketch": "HubSketch"},
            {
                "sketch": "HubSketch",
                "element_count": 1,
                "constraint_count": 0,
                "geometry": [{"type": "circle", "index": 0}],
            },
            {"sketch": "HubSketch", "fully_constrained": True, "open_vertices": 0},
        ]

        result = cad_sketch(body="Body", geometry_ref=hub_ref)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["geometry"]), 1)

        # Verify circle was sent with correct radius
        populate_call = [
            c for c in client.send_command.call_args_list if c[0][0] == "sketch_populate"
        ][0]
        elements_sent = populate_call[1]["elements"]
        self.assertEqual(len(elements_sent), 1)
        self.assertEqual(elements_sent[0]["type"], "circle")
        self.assertAlmostEqual(elements_sent[0]["r"], 10.0)  # hub_diameter/2 = 20/2


if __name__ == "__main__":
    unittest.main()
