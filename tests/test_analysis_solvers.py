"""Tests for field solver ABC, registry, and pack discovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.analysis_models import (
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    Material,
    MeshInfo,
)
from server.analysis_solvers import (
    FIELD_SOLVERS,
    CalculiXSolver,
    FieldSolver,
    MockFieldSolver,
    _discover_solver_packs,
    get_solver,
    list_solvers,
    register_solver,
)


def _make_spec() -> AnalysisSpec:
    mat = Material(
        name="test_steel",
        youngs_modulus_mpa=200_000,
        poissons_ratio=0.3,
        density_kg_m3=7800,
        yield_strength_mpa=250,
    )
    bc = BoundaryCondition(bc_type="fixed", faces=("Face1",))
    return AnalysisSpec(
        analysis_type=AnalysisType.STRUCTURAL,
        body="Body",
        material=mat,
        boundary_conditions=(bc,),
    )


def _make_mesh_info() -> MeshInfo:
    return MeshInfo(
        path="/tmp/test.msh",
        num_nodes=100,
        num_elements=50,
        element_type="tet4",
        physical_groups={"Face1": 1},
    )


class TestFieldSolverABC(unittest.TestCase):
    def test_cannot_instantiate_abc(self) -> None:
        with self.assertRaises(TypeError):
            FieldSolver()  # type: ignore[abstract]


class TestMockFieldSolver(unittest.TestCase):
    def test_available(self) -> None:
        solver = MockFieldSolver()
        ok, msg = solver.available()
        self.assertTrue(ok)

    def test_name(self) -> None:
        solver = MockFieldSolver()
        self.assertEqual(solver.name(), "mock")

    def test_analysis_types(self) -> None:
        solver = MockFieldSolver()
        types = solver.analysis_types()
        self.assertIn(AnalysisType.STRUCTURAL, types)
        self.assertIn(AnalysisType.THERMAL, types)

    def test_solve_and_parse(self) -> None:
        solver = MockFieldSolver()
        spec = _make_spec()
        mesh_info = _make_mesh_info()

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            inp = solver.write_input(spec, mesh_info, work_dir)
            self.assertTrue(inp.exists())

            elapsed = solver.run(inp, work_dir)
            self.assertGreater(elapsed, 0)

            result = solver.parse_results(work_dir, spec)
            self.assertEqual(result.status, CheckStatus.PASS)
            self.assertGreater(result.safety_factor, 1.0)
            self.assertGreater(result.max_von_mises_mpa, 0)

    def test_solve_failure(self) -> None:
        solver = MockFieldSolver(fail=True)
        spec = _make_spec()
        mesh_info = _make_mesh_info()

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            inp = solver.write_input(spec, mesh_info, work_dir)
            with self.assertRaises(RuntimeError):
                solver.run(inp, work_dir)


class TestCalculiXSolver(unittest.TestCase):
    def test_name_and_types(self) -> None:
        solver = CalculiXSolver()
        self.assertEqual(solver.name(), "calculix")
        self.assertEqual(solver.analysis_types(), [AnalysisType.STRUCTURAL])

    def test_available_returns_tuple(self) -> None:
        solver = CalculiXSolver()
        result = solver.available()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], str)

    def test_write_input(self) -> None:
        solver = CalculiXSolver()
        spec = _make_spec()
        mesh_info = _make_mesh_info()

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            inp = solver.write_input(spec, mesh_info, work_dir)
            self.assertTrue(inp.exists())
            content = inp.read_text()
            self.assertIn("MATERIAL", content)
            self.assertIn("ELASTIC", content)
            self.assertIn("200000", content)


class TestSolverRegistry(unittest.TestCase):
    def test_default_solvers_registered(self) -> None:
        self.assertIn("calculix", FIELD_SOLVERS)
        self.assertIn("mock", FIELD_SOLVERS)

    def test_get_solver_by_name(self) -> None:
        solver = get_solver("mock")
        self.assertIsNotNone(solver)
        self.assertEqual(solver.name(), "mock")

    def test_get_solver_unknown(self) -> None:
        solver = get_solver("nonexistent_solver_xyz")
        self.assertIsNone(solver)

    def test_auto_select(self) -> None:
        solver = get_solver("", AnalysisType.STRUCTURAL)
        self.assertIsNotNone(solver)

    def test_list_solvers(self) -> None:
        solvers = list_solvers()
        self.assertGreaterEqual(len(solvers), 2)
        names = {s["name"] for s in solvers}
        self.assertIn("calculix", names)
        self.assertIn("mock", names)
        for s in solvers:
            self.assertIn("available", s)
            self.assertIn("diagnostic", s)
            self.assertIn("analysis_types", s)

    def test_duplicate_solver_skipped(self) -> None:
        # Registering mock again should be a no-op (already registered)
        count_before = len(FIELD_SOLVERS)
        register_solver(MockFieldSolver())
        self.assertEqual(len(FIELD_SOLVERS), count_before)


class TestSolverPackDiscovery(unittest.TestCase):
    @patch("server.analysis_solvers.importlib.metadata.entry_points")
    def test_loads_pack(self, mock_eps: MagicMock) -> None:
        # Create a fake solver
        class FakeSolver(FieldSolver):
            def name(self) -> str:
                return "fake_test_solver"

            def analysis_types(self) -> list[AnalysisType]:
                return [AnalysisType.STRUCTURAL]

            def available(self) -> tuple[bool, str]:
                return True, "fake"

            def write_input(self, spec, mesh_info, work_dir):
                return work_dir / "fake.inp"

            def run(self, input_path, work_dir):
                return 0.0

            def parse_results(self, work_dir, spec):
                pass

        fake_mod = MagicMock()
        fake_mod.SOLVERS = [FakeSolver()]
        ep = MagicMock()
        ep.name = "fake_pack"
        ep.load.return_value = fake_mod
        mock_eps.return_value = [ep]

        _discover_solver_packs()

        self.assertIn("fake_test_solver", FIELD_SOLVERS)

        # Cleanup
        del FIELD_SOLVERS["fake_test_solver"]

    @patch("server.analysis_solvers.importlib.metadata.entry_points")
    def test_broken_pack_doesnt_crash(self, mock_eps: MagicMock) -> None:
        ep = MagicMock()
        ep.name = "broken_pack"
        ep.load.side_effect = ImportError("no such module")
        mock_eps.return_value = [ep]

        # Should not raise
        _discover_solver_packs()


if __name__ == "__main__":
    unittest.main()
