"""End-to-end tests for analysis.* MCP tools using MockFieldSolver."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from server.analysis_models import MeshInfo


class TestAnalysisStressCheck(unittest.TestCase):
    """Test analysis_stress_check with mock solver (no FreeCAD needed)."""

    def test_unknown_material(self) -> None:
        from server.tools_analysis import analysis_stress_check

        result = analysis_stress_check(
            body="Body",
            material="unobtanium",
            boundary_conditions=[{"bc_type": "fixed", "faces": ["Face1"]}],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_MATERIAL")

    def test_no_boundary_conditions(self) -> None:
        from server.tools_analysis import analysis_stress_check

        result = analysis_stress_check(
            body="Body",
            material="aluminum_6061_t6",
            boundary_conditions=[],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_BCS")

    def test_inline_material_dict(self) -> None:
        from server.tools_analysis import _resolve_material

        mat = _resolve_material(
            {
                "name": "custom",
                "youngs_modulus_mpa": 100_000,
                "poissons_ratio": 0.3,
                "density_kg_m3": 5000,
                "yield_strength_mpa": 300,
            }
        )
        self.assertIsNotNone(mat)
        self.assertEqual(mat.name, "custom")

    def test_end_to_end_mock_solver(self) -> None:
        import server.tools_analysis as mod

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/test.msh",
                num_nodes=100,
                num_elements=50,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face5": 2},
            )

            result = mod.analysis_stress_check(
                body="Body",
                material="aluminum_6061_t6",
                boundary_conditions=[
                    {"bc_type": "fixed", "faces": ["Face1"]},
                    {"bc_type": "force", "faces": ["Face5"], "value": {"fz": -100}},
                ],
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("status", result)
        self.assertIn("safety_factor", result)
        self.assertIn("max_von_mises_mpa", result)
        self.assertIn("checks", result)
        self.assertIn("scalar_fields", result)
        self.assertGreater(result["safety_factor"], 1.0)

    def test_export_failure(self) -> None:
        import server.tools_analysis as mod

        with patch.object(mod, "cad_export_body") as mock_export:
            mock_export.return_value = {
                "ok": False,
                "error": {"code": "NO_BODY", "message": "Body not found"},
            }

            result = mod.analysis_stress_check(
                body="NonExistent",
                material="steel",
                boundary_conditions=[{"bc_type": "fixed", "faces": ["Face1"]}],
                solver="mock",
            )
        self.assertFalse(result["ok"])

    def test_no_solver_available(self) -> None:
        from server.tools_analysis import analysis_stress_check

        result = analysis_stress_check(
            body="Body",
            material="steel",
            boundary_conditions=[{"bc_type": "fixed", "faces": ["Face1"]}],
            solver="nonexistent_solver_xyz",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SOLVER_NOT_FOUND")


class TestAnalysisListMaterials(unittest.TestCase):
    def test_list_all(self) -> None:
        from server.tools_analysis import analysis_list_materials

        result = analysis_list_materials()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["materials"]), 10)

    def test_list_filtered(self) -> None:
        from server.tools_analysis import analysis_list_materials

        result = analysis_list_materials(category="plastic")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["materials"]), 3)


class TestAnalysisListSolvers(unittest.TestCase):
    def test_list(self) -> None:
        from server.tools_analysis import analysis_list_solvers

        result = analysis_list_solvers()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["solvers"]), 2)
        names = {s["name"] for s in result["solvers"]}
        self.assertIn("mock", names)
        self.assertIn("calculix", names)


class TestAnalysisTorqueSweep(unittest.TestCase):
    """Test analysis_torque_sweep with mock solver."""

    def test_unknown_material(self) -> None:
        from server.tools_analysis import analysis_torque_sweep

        result = analysis_torque_sweep(
            body="Body",
            materials=["unobtanium"],
            fixed_faces=["Face1"],
            load_face="Face5",
            pitch_radius_mm=12.0,
            torques_nmm=[50, 100],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_MATERIAL")

    def test_empty_torques(self) -> None:
        from server.tools_analysis import analysis_torque_sweep

        result = analysis_torque_sweep(
            body="Body",
            materials=["steel_4140"],
            fixed_faces=["Face1"],
            load_face="Face5",
            pitch_radius_mm=12.0,
            torques_nmm=[],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_TORQUES")

    def test_bad_radius(self) -> None:
        from server.tools_analysis import analysis_torque_sweep

        result = analysis_torque_sweep(
            body="Body",
            materials=["steel_4140"],
            fixed_faces=["Face1"],
            load_face="Face5",
            pitch_radius_mm=0,
            torques_nmm=[50],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BAD_RADIUS")

    def test_sweep_with_mock_solver(self) -> None:
        import server.tools_analysis as mod

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/test.msh",
                num_nodes=100,
                num_elements=50,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face5": 2},
            )

            result = mod.analysis_torque_sweep(
                body="Body",
                materials=["pla", "aluminum_6061_t6", "steel_4140"],
                fixed_faces=["Face1"],
                load_face="Face5",
                pitch_radius_mm=12.0,
                torques_nmm=[25, 50, 100, 200],
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sweep"]), 4)
        self.assertIn("breaking_torques", result)
        self.assertEqual(len(result["breaking_torques"]), 3)

        # Verify torques are sorted
        torques = [row["torque_nmm"] for row in result["sweep"]]
        self.assertEqual(torques, [25, 50, 100, 200])

        # Each row should have all 3 materials
        for row in result["sweep"]:
            self.assertIn("pla", row["materials"])
            self.assertIn("aluminum_6061_t6", row["materials"])
            self.assertIn("steel_4140", row["materials"])
            for mat_data in row["materials"].values():
                self.assertIn("safety_factor", mat_data)
                self.assertIn("peak_stress_mpa", mat_data)
                self.assertIn("status", mat_data)

        # Breaking torques should exist for all materials
        for mat_name in ["pla", "aluminum_6061_t6", "steel_4140"]:
            bt = result["breaking_torques"][mat_name]
            self.assertIn("breaking_torque_nmm", bt)
            self.assertIn("yield_mpa", bt)

        # SF should decrease as torque increases
        pla_sfs = [row["materials"]["pla"]["safety_factor"] for row in result["sweep"]]
        self.assertGreater(pla_sfs[0], pla_sfs[-1])

        # Mock solver returns stress proportional to yield, so SFs are
        # identical across materials.  With real solver, steel SF > PLA SF
        # at the same torque because the stress is geometry-dependent.
        # Just verify all SFs are positive and finite.
        for row in result["sweep"]:
            for mat_data in row["materials"].values():
                self.assertGreater(mat_data["safety_factor"], 0)


if __name__ == "__main__":
    unittest.main()
