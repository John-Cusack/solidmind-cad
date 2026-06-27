"""Tests for the built-in fluid library."""

from __future__ import annotations

import unittest

from server.analysis_fluids import get_fluid, list_fluids, make_flow_conditions


class TestGetFluid(unittest.TestCase):
    def test_air(self) -> None:
        f = get_fluid("air")
        self.assertIsNotNone(f)
        self.assertAlmostEqual(f["density_kg_m3"], 1.225)

    def test_seawater(self) -> None:
        f = get_fluid("seawater")
        self.assertIsNotNone(f)
        self.assertAlmostEqual(f["density_kg_m3"], 1025.0)
        self.assertGreater(f["viscosity_pa_s"], 1e-4)

    def test_freshwater(self) -> None:
        f = get_fluid("freshwater")
        self.assertIsNotNone(f)
        self.assertAlmostEqual(f["density_kg_m3"], 998.2, places=1)

    def test_alias(self) -> None:
        f = get_fluid("ocean")
        self.assertIsNotNone(f)
        self.assertAlmostEqual(f["density_kg_m3"], 1025.0)

    def test_unknown(self) -> None:
        f = get_fluid("mercury")
        self.assertIsNone(f)

    def test_case_insensitive(self) -> None:
        f = get_fluid("SeaWater")
        self.assertIsNotNone(f)


class TestListFluids(unittest.TestCase):
    def test_has_entries(self) -> None:
        fluids = list_fluids()
        self.assertGreaterEqual(len(fluids), 5)
        names = {f["name"] for f in fluids}
        self.assertIn("air", names)
        self.assertIn("seawater", names)
        self.assertIn("freshwater", names)


class TestMakeFlowConditions(unittest.TestCase):
    def test_air_flow(self) -> None:
        fc = make_flow_conditions("air", velocity_m_s=15.0, angle_of_attack_deg=5.0)
        self.assertIsNotNone(fc)
        self.assertAlmostEqual(fc.velocity_m_s, 15.0)
        self.assertAlmostEqual(fc.density_kg_m3, 1.225)
        self.assertAlmostEqual(fc.angle_of_attack_deg, 5.0)

    def test_seawater_flow(self) -> None:
        fc = make_flow_conditions("seawater", velocity_m_s=2.0)
        self.assertIsNotNone(fc)
        self.assertAlmostEqual(fc.density_kg_m3, 1025.0)

    def test_unknown_fluid(self) -> None:
        fc = make_flow_conditions("plasma", velocity_m_s=100.0)
        self.assertIsNone(fc)


class TestHydrodynamicTool(unittest.TestCase):
    """Test that aero_check works for hydrodynamic (water) analysis."""

    def test_fluid_preset_seawater(self) -> None:
        from unittest.mock import patch

        import server.tools_analysis as mod
        from server.analysis_models import MeshInfo

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/hull.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/hull.msh",
                num_nodes=200,
                num_elements=100,
                element_type="tri3",
            )

            result = mod.analysis_aero_check(
                body="hull",
                fluid="seawater",
                flow_conditions={"velocity_m_s": 3.0},
                reference={"area_m2": 2.0, "chord_m": 5.0},
                solver="mock_dust",
            )

        self.assertTrue(result["ok"])

    def test_unknown_fluid_error(self) -> None:
        from server.tools_analysis import analysis_aero_check

        result = analysis_aero_check(
            body="hull",
            fluid="liquid_nitrogen",
            flow_conditions={"velocity_m_s": 1.0},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_FLUID")


if __name__ == "__main__":
    unittest.main()
