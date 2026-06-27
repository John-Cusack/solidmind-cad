"""Tests for conjugate heat transfer (CHT) — coupled Navier-Stokes + Heat."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from server.analysis_models import AnalysisType, MeshInfo


class TestConjugateThermalCheck(unittest.TestCase):
    """Test analysis_conjugate_thermal_check with mock solver."""

    def test_unknown_material(self) -> None:
        from server.tools_analysis import analysis_conjugate_thermal_check

        result = analysis_conjugate_thermal_check(
            body="motor_mount",
            material="unobtanium",
            flow_velocity=[8.33, 0, 0],
            flow_faces_inlet=["Face1"],
            flow_faces_outlet=["Face2"],
            heat_source_faces=["Face6"],
            heat_flux_w_m2=5000,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_MATERIAL")

    def test_zero_conductivity(self) -> None:
        from server.tools_analysis import analysis_conjugate_thermal_check

        result = analysis_conjugate_thermal_check(
            body="motor_mount",
            material={
                "name": "insulator",
                "youngs_modulus_mpa": 1000,
                "poissons_ratio": 0.3,
                "density_kg_m3": 500,
                "yield_strength_mpa": 10,
                "thermal_conductivity_w_mk": 0,
            },
            flow_velocity=[8.33, 0, 0],
            flow_faces_inlet=["Face1"],
            flow_faces_outlet=["Face2"],
            heat_source_faces=["Face6"],
            heat_flux_w_m2=5000,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ZERO_CONDUCTIVITY")

    def test_invalid_velocity(self) -> None:
        from server.tools_analysis import analysis_conjugate_thermal_check

        result = analysis_conjugate_thermal_check(
            body="motor_mount",
            material="aluminum_6061_t6",
            flow_velocity=[8.33, 0],  # missing z component
            flow_faces_inlet=["Face1"],
            flow_faces_outlet=["Face2"],
            heat_source_faces=["Face6"],
            heat_flux_w_m2=5000,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_end_to_end_mock_solver(self) -> None:
        """Full CHT pipeline with mock solver (no Elmer needed)."""
        import server.tools_analysis as mod

        mock_mesh_info = MeshInfo(
            path="/tmp/mount.msh",
            num_nodes=500,
            num_elements=250,
            element_type="tet4",
            physical_groups={"Face1": 1, "Face2": 2, "Face6": 3},
        )

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
            patch.object(mod, "mesh_step_to_cht_msh") as mock_cht_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/mount.step"}
            mock_mesh.return_value = mock_mesh_info
            mock_cht_mesh.return_value = mock_mesh_info

            result = mod.analysis_conjugate_thermal_check(
                body="motor_mount",
                material="aluminum_6061_t6",
                flow_velocity=[8.33, 0, 0],
                flow_faces_inlet=["Face1"],
                flow_faces_outlet=["Face2"],
                heat_source_faces=["Face6"],
                heat_flux_w_m2=5000,
                max_temperature_k=353,
                fluid="air",
                solver="mock",
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("status", result)
        self.assertIn("scalar_fields", result)

    def test_water_cooling(self) -> None:
        """CHT with water as fluid (e.g. liquid-cooled motor mount)."""
        import server.tools_analysis as mod

        mock_mesh_info = MeshInfo(
            path="/tmp/mount.msh",
            num_nodes=500,
            num_elements=250,
            element_type="tet4",
            physical_groups={"Face1": 1, "Face2": 2, "Face6": 3},
        )

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
            patch.object(mod, "mesh_step_to_cht_msh") as mock_cht_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/mount.step"}
            mock_mesh.return_value = mock_mesh_info
            mock_cht_mesh.return_value = mock_mesh_info

            result = mod.analysis_conjugate_thermal_check(
                body="motor_mount",
                material="aluminum_6061_t6",
                flow_velocity=[0.5, 0, 0],
                flow_faces_inlet=["Face1"],
                flow_faces_outlet=["Face2"],
                heat_source_faces=["Face6"],
                heat_flux_w_m2=10000,
                fluid="water",
                solver="mock",
            )

        self.assertTrue(result["ok"], result)


class TestElmerConjugateSif(unittest.TestCase):
    """Test the .sif generation for conjugate heat transfer."""

    def test_sif_has_navier_stokes_solver(self) -> None:
        """Conjugate .sif should include Navier-Stokes solver block."""
        from server.analysis_models import AnalysisSpec, BoundaryCondition, Material
        from server.analysis_solver_elmer import ElmerSolver

        mat = Material(
            name="aluminum",
            youngs_modulus_mpa=69000,
            poissons_ratio=0.33,
            density_kg_m3=2700,
            yield_strength_mpa=276,
            thermal_conductivity_w_mk=167,
            specific_heat_j_kgk=896,
        )
        bcs = (
            BoundaryCondition(
                bc_type="velocity_inlet",
                faces=("Face1",),
                value={
                    "vx_m_s": 8.33, "vy_m_s": 0, "vz_m_s": 0,
                    "temperature_k": 300,
                    "fluid_density_kg_m3": 1.225,
                    "fluid_viscosity_pa_s": 1.789e-5,
                    "fluid_conductivity_w_mk": 0.026,
                    "fluid_capacity_j_kgk": 1006,
                },
            ),
            BoundaryCondition(
                bc_type="pressure_outlet",
                faces=("Face2",),
                value={"pressure_pa": 0},
            ),
            BoundaryCondition(
                bc_type="heat_flux",
                faces=("Face6",),
                value={"flux_w_m2": 5000},
            ),
        )
        spec = AnalysisSpec(
            analysis_type=AnalysisType.CONJUGATE_HEAT,
            body="motor_mount",
            material=mat,
            boundary_conditions=bcs,
        )
        mesh_info = MeshInfo(
            path="/tmp/test.msh",
            num_nodes=100,
            num_elements=50,
            element_type="tet4",
            physical_groups={"Face1": 1, "Face2": 2, "Face6": 3},
        )

        from pathlib import Path
        solver = ElmerSolver()
        sif_lines = solver._build_sif(spec, mesh_info, Path("/tmp/mesh_db"))
        sif_text = "\n".join(sif_lines)

        # Should have both solvers
        self.assertIn("Navier-Stokes", sif_text)
        self.assertIn("FlowSolve", sif_text)
        self.assertIn("HeatSolve", sif_text)

        # Should have two bodies (solid + fluid)
        self.assertIn("Body 1", sif_text)
        self.assertIn("Body 2", sif_text)

        # Should have two materials (solid + fluid)
        self.assertIn("Material 1", sif_text)
        self.assertIn("Material 2", sif_text)

        # Fluid material should have viscosity
        self.assertIn("Viscosity", sif_text)

        # Should have two equations (solid: heat only, fluid: flow + heat)
        self.assertIn("Equation 1", sif_text)
        self.assertIn("Equation 2", sif_text)
        self.assertIn("Convection = Computed", sif_text)
        self.assertIn("Convection = None", sif_text)

        # Flow BCs
        self.assertIn("Velocity 1 = 8.33", sif_text)
        self.assertIn("Pressure = 0", sif_text)

        # Thermal BC
        self.assertIn("Heat Flux = 5000", sif_text)

    def test_thermal_sif_unchanged(self) -> None:
        """Pure thermal spec should NOT include Navier-Stokes."""
        from server.analysis_models import AnalysisSpec, BoundaryCondition, Material
        from server.analysis_solver_elmer import ElmerSolver

        mat = Material(
            name="aluminum",
            youngs_modulus_mpa=69000,
            poissons_ratio=0.33,
            density_kg_m3=2700,
            yield_strength_mpa=276,
            thermal_conductivity_w_mk=167,
            specific_heat_j_kgk=896,
        )
        bcs = (
            BoundaryCondition(
                bc_type="heat_flux",
                faces=("Face1",),
                value={"flux_w_m2": 5000},
            ),
            BoundaryCondition(
                bc_type="temperature",
                faces=("Face6",),
                value={"temperature_k": 300},
            ),
        )
        spec = AnalysisSpec(
            analysis_type=AnalysisType.THERMAL,
            body="box",
            material=mat,
            boundary_conditions=bcs,
        )
        mesh_info = MeshInfo(
            path="/tmp/test.msh",
            num_nodes=100,
            num_elements=50,
            element_type="tet4",
            physical_groups={"Face1": 1, "Face6": 2},
        )

        from pathlib import Path
        solver = ElmerSolver()
        sif_lines = solver._build_sif(spec, mesh_info, Path("/tmp/mesh_db"))
        sif_text = "\n".join(sif_lines)

        # Should NOT have Navier-Stokes
        self.assertNotIn("Navier-Stokes", sif_text)
        self.assertNotIn("FlowSolve", sif_text)
        # Should still have Heat
        self.assertIn("HeatSolve", sif_text)


if __name__ == "__main__":
    unittest.main()
