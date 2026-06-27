"""Tests for aerodynamic solvers (SU2 + DUST) and the aero_check tool."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.analysis_models import (
    AeroReference,
    AeroResult,
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    FlowConditions,
    Material,
    MeshInfo,
    RotorSpec,
)
from server.analysis_solvers import FIELD_SOLVERS, get_solver

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestFlowConditions(unittest.TestCase):
    def test_round_trip(self) -> None:
        fc = FlowConditions(
            velocity_m_s=15.0,
            density_kg_m3=1.225,
            angle_of_attack_deg=5.0,
        )
        d = fc.to_dict()
        fc2 = FlowConditions.from_dict(d)
        self.assertEqual(fc, fc2)
        self.assertAlmostEqual(fc2.velocity_m_s, 15.0)

    def test_defaults(self) -> None:
        fc = FlowConditions(velocity_m_s=10.0)
        self.assertAlmostEqual(fc.density_kg_m3, 1.225)
        self.assertAlmostEqual(fc.angle_of_attack_deg, 0.0)


class TestAeroReference(unittest.TestCase):
    def test_round_trip(self) -> None:
        ref = AeroReference(area_m2=0.5, chord_m=0.2, span_m=1.0)
        d = ref.to_dict()
        ref2 = AeroReference.from_dict(d)
        self.assertEqual(ref, ref2)


class TestRotorSpec(unittest.TestCase):
    def test_round_trip(self) -> None:
        r = RotorSpec(
            rotor_id="front_left",
            center_xyz=(0.15, 0.15, 0.05),
            axis=(0, 0, 1),
            radius_m=0.127,
            rpm=5000,
            num_blades=2,
            collective_deg=12.0,
        )
        d = r.to_dict()
        r2 = RotorSpec.from_dict(d)
        self.assertEqual(r.rotor_id, r2.rotor_id)
        self.assertAlmostEqual(r.radius_m, r2.radius_m)
        self.assertEqual(r.num_blades, r2.num_blades)


class TestAeroResult(unittest.TestCase):
    def test_round_trip(self) -> None:
        from server.analysis_models import AnalysisCheck
        ar = AeroResult(
            analysis_id="aero_001",
            status=CheckStatus.PASS,
            cl=0.5,
            cd=0.03,
            cs=0.0,
            cmx=0.0,
            cmy=-0.02,
            cmz=0.0,
            l_over_d=16.67,
            lift_n=5.0,
            drag_n=0.3,
            checks=(
                AnalysisCheck(
                    name="l_over_d",
                    status=CheckStatus.PASS,
                    message="L/D = 16.67",
                ),
            ),
            rotor_forces={"rotor_1": {"thrust_n": 2.5, "torque_nm": 0.05}},
            solver_name="su2",
            solve_time_s=120.0,
        )
        d = ar.to_dict()
        ar2 = AeroResult.from_dict(d)
        self.assertEqual(ar.analysis_id, ar2.analysis_id)
        self.assertAlmostEqual(ar.cl, ar2.cl)
        self.assertEqual(ar.rotor_forces, ar2.rotor_forces)


# ---------------------------------------------------------------------------
# SU2 solver tests
# ---------------------------------------------------------------------------


class TestSU2Solver(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("su2", FIELD_SOLVERS)

    def test_name_and_types(self) -> None:
        solver = FIELD_SOLVERS["su2"]
        self.assertEqual(solver.name(), "su2")
        self.assertIn(AnalysisType.AERODYNAMIC, solver.analysis_types())
        self.assertIn(AnalysisType.HYDRODYNAMIC, solver.analysis_types())

    def test_available_returns_tuple(self) -> None:
        solver = FIELD_SOLVERS["su2"]
        ok, msg = solver.available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(msg, str)

    def test_write_input_incompressible(self) -> None:
        solver = FIELD_SOLVERS["su2"]
        mat = Material(
            name="air", youngs_modulus_mpa=0, poissons_ratio=0,
            density_kg_m3=1.225, yield_strength_mpa=0,
        )
        bcs = (
            BoundaryCondition(
                bc_type="freestream", faces=(),
                value={"velocity_m_s": 15.0, "density_kg_m3": 1.225,
                       "viscosity_pa_s": 1.789e-5, "angle_of_attack_deg": 5.0},
            ),
            BoundaryCondition(
                bc_type="reference", faces=(),
                value={"area_m2": 0.1, "chord_m": 0.2},
            ),
            BoundaryCondition(bc_type="wall", faces=("wall",)),
            BoundaryCondition(bc_type="farfield", faces=("farfield",)),
        )
        spec = AnalysisSpec(
            analysis_type=AnalysisType.AERODYNAMIC,
            body="drone_body",
            material=mat,
            boundary_conditions=bcs,
        )
        mesh_info = MeshInfo(
            path="/tmp/test.msh", num_nodes=100,
            num_elements=50, element_type="tri3",
        )

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            cfg = solver.write_input(spec, mesh_info, work_dir)
            self.assertTrue(cfg.exists())
            content = cfg.read_text()
            self.assertIn("INC_RANS", content)  # incompressible (Mach < 0.3)
            self.assertIn("AOA= 5.0", content)
            self.assertIn("SA", content)  # Spalart-Allmaras


# ---------------------------------------------------------------------------
# DUST solver tests
# ---------------------------------------------------------------------------


class TestDUSTSolver(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("dust", FIELD_SOLVERS)
        self.assertIn("mock_dust", FIELD_SOLVERS)

    def test_name_and_types(self) -> None:
        solver = FIELD_SOLVERS["dust"]
        self.assertEqual(solver.name(), "dust")
        self.assertEqual(solver.analysis_types(), [AnalysisType.AERODYNAMIC])

    def test_write_input_with_rotors(self) -> None:
        solver = FIELD_SOLVERS["dust"]
        mat = Material(
            name="air", youngs_modulus_mpa=0, poissons_ratio=0,
            density_kg_m3=1.225, yield_strength_mpa=0,
        )
        bcs = (
            BoundaryCondition(
                bc_type="freestream", faces=(),
                value={"velocity_m_s": 0.0},  # hover
            ),
            BoundaryCondition(
                bc_type="rotor", faces=(),
                value={
                    "rotor_id": "front_left",
                    "center_xyz": [0.15, 0.15, 0.05],
                    "axis": [0, 0, 1],
                    "radius_m": 0.127,
                    "rpm": 5000,
                    "num_blades": 2,
                },
            ),
            BoundaryCondition(
                bc_type="rotor", faces=(),
                value={
                    "rotor_id": "front_right",
                    "center_xyz": [0.15, -0.15, 0.05],
                    "axis": [0, 0, 1],
                    "radius_m": 0.127,
                    "rpm": 5000,
                    "num_blades": 2,
                },
            ),
        )
        spec = AnalysisSpec(
            analysis_type=AnalysisType.AERODYNAMIC,
            body="quad_frame",
            material=mat,
            boundary_conditions=bcs,
        )
        mesh_info = MeshInfo(
            path="/tmp/test.msh", num_nodes=100,
            num_elements=50, element_type="tri3",
        )

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            cfg = solver.write_input(spec, mesh_info, work_dir)
            self.assertTrue(cfg.exists())
            content = cfg.read_text()
            self.assertIn("front_left", content)
            self.assertIn("front_right", content)
            self.assertIn("lifting_line_rotor", content)
            self.assertIn("n_blades = 2", content)


class TestMockDUSTSolver(unittest.TestCase):
    def test_full_pipeline(self) -> None:
        solver = FIELD_SOLVERS["mock_dust"]
        mat = Material(
            name="air", youngs_modulus_mpa=0, poissons_ratio=0,
            density_kg_m3=1.225, yield_strength_mpa=0,
        )
        bcs = (
            BoundaryCondition(
                bc_type="rotor", faces=(),
                value={
                    "rotor_id": "rotor_1",
                    "center_xyz": [0, 0, 0],
                    "axis": [0, 0, 1],
                    "radius_m": 0.127,
                    "rpm": 5000,
                    "num_blades": 2,
                },
            ),
        )
        spec = AnalysisSpec(
            analysis_type=AnalysisType.AERODYNAMIC,
            body="Body",
            material=mat,
            boundary_conditions=bcs,
        )
        mesh_info = MeshInfo(
            path="/tmp/test.msh", num_nodes=100,
            num_elements=50, element_type="tri3",
        )

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            inp = solver.write_input(spec, mesh_info, work_dir)
            elapsed = solver.run(inp, work_dir)
            self.assertGreater(elapsed, 0)
            result = solver.parse_results(work_dir, spec)
            self.assertIsNotNone(result)
            # FieldResult wraps the aero data
            self.assertEqual(result.solver_name, "mock_dust")


# ---------------------------------------------------------------------------
# Aero solver auto-selection tests
# ---------------------------------------------------------------------------


class TestAeroSolverSelection(unittest.TestCase):
    def test_auto_select_aero(self) -> None:
        solver = get_solver("", AnalysisType.AERODYNAMIC)
        self.assertIsNotNone(solver)
        # Should find SU2, DUST, or mock_dust
        self.assertIn(solver.name(), ("su2", "dust", "mock_dust"))

    def test_list_includes_aero(self) -> None:
        from server.analysis_solvers import list_solvers
        solvers = list_solvers()
        names = {s["name"] for s in solvers}
        self.assertIn("su2", names)
        self.assertIn("dust", names)
        self.assertIn("mock_dust", names)
        # Check aero types
        for s in solvers:
            if s["name"] in ("su2", "dust", "mock_dust"):
                self.assertIn("aerodynamic", s["analysis_types"])


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


class TestAeroCheckTool(unittest.TestCase):
    def test_missing_flow_conditions(self) -> None:
        from server.tools_analysis import analysis_aero_check

        # Invalid flow conditions
        result = analysis_aero_check(
            body="Body",
            flow_conditions={},  # missing velocity_m_s
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_FLOW")

    def test_single_body_aero(self) -> None:
        import server.tools_analysis as mod

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/test.msh", num_nodes=100,
                num_elements=50, element_type="tri3",
            )

            result = mod.analysis_aero_check(
                body="drone_body",
                flow_conditions={"velocity_m_s": 15.0},
                reference={"area_m2": 0.1, "chord_m": 0.2},
                solver="mock_dust",
            )

        self.assertTrue(result["ok"])
        self.assertIn("solver_name", result)

    def test_multi_rotor_aero(self) -> None:
        import server.tools_analysis as mod

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/test.msh", num_nodes=100,
                num_elements=50, element_type="tri3",
            )

            result = mod.analysis_aero_check(
                body="quad_frame",
                flow_conditions={"velocity_m_s": 0.0},
                rotors=[
                    {
                        "rotor_id": "fl",
                        "center_xyz": [0.15, 0.15, 0.05],
                        "axis": [0, 0, 1],
                        "radius_m": 0.127,
                        "rpm": 5000,
                    },
                    {
                        "rotor_id": "fr",
                        "center_xyz": [0.15, -0.15, 0.05],
                        "axis": [0, 0, 1],
                        "radius_m": 0.127,
                        "rpm": 5000,
                    },
                ],
                solver="mock_dust",
            )

        self.assertTrue(result["ok"])
        self.assertIn("rotor_forces", result)
        rf = result["rotor_forces"]
        self.assertIn("fl", rf)
        self.assertIn("fr", rf)
        self.assertGreater(rf["fl"]["thrust_n"], 0)

    def test_export_failure(self) -> None:
        import server.tools_analysis as mod

        with patch.object(mod, "cad_export_body") as mock_export:
            mock_export.return_value = {
                "ok": False,
                "error": {"code": "NO_BODY", "message": "not found"},
            }

            result = mod.analysis_aero_check(
                body="missing",
                flow_conditions={"velocity_m_s": 10.0},
                solver="mock_dust",
            )
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
