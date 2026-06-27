"""Tests for CHOLMOD direct solver adapter."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from server.analysis_models import (
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    FieldResult,
    Material,
    MeshInfo,
)
from server.analysis_solver_cholmod import CHOLMODSolver


def _spec() -> AnalysisSpec:
    return AnalysisSpec(
        analysis_type=AnalysisType.STRUCTURAL,
        body="Body",
        material=Material(
            name="steel",
            youngs_modulus_mpa=200_000,
            poissons_ratio=0.3,
            density_kg_m3=7800,
            yield_strength_mpa=250,
        ),
        boundary_conditions=(BoundaryCondition(bc_type="fixed", faces=("Face1",), value={}),),
        mesh_size=0.0,
        solver="cholmod",
    )


def _mesh() -> MeshInfo:
    return MeshInfo(
        path="/tmp/test.msh",
        num_nodes=10,
        num_elements=5,
        element_type="tet4",
        physical_groups={"Face1": 1},
    )


class TestCHOLMODSolver(unittest.TestCase):
    def test_basic_metadata(self) -> None:
        solver = CHOLMODSolver()
        self.assertEqual(solver.name(), "cholmod")
        self.assertTrue(solver.supports_direct_solve())
        self.assertIn(AnalysisType.STRUCTURAL, solver.analysis_types())

    def test_available_returns_tuple(self) -> None:
        solver = CHOLMODSolver()
        ok, msg = solver.available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(msg, str)

    def test_factor_cache_reuse(self) -> None:
        solver = CHOLMODSolver(max_cache_entries=4)
        spec = _spec()
        mesh = _mesh()

        fake_system = MagicMock()
        fake_system.K = MagicMock()
        fake_system.f = np.array([1.0, 2.0], dtype=np.float64)
        fake_system.factor_cache_key.return_value = "cache_key"

        fake_factor = MagicMock()
        fake_factor.solve_A.return_value = np.array([0.1, 0.2], dtype=np.float64)

        fake_result = FieldResult(
            analysis_id="",
            status=CheckStatus.PASS,
            safety_factor=2.0,
            max_von_mises_mpa=100.0,
            max_displacement_mm=0.01,
            checks=(),
            scalar_fields=(),
            solver_name="cholmod",
            solve_time_s=0.01,
        )

        with (
            patch("server.analysis_solver_cholmod.assemble_system", return_value=fake_system),
            patch.object(solver, "_factorize", return_value=fake_factor) as mock_fact,
            patch(
                "server.analysis_solver_cholmod.build_field_result_from_solution",
                return_value=fake_result,
            ),
        ):
            solver.solve_direct(spec, mesh, Path("/tmp"))
            solver.solve_direct(spec, mesh, Path("/tmp"))

        self.assertEqual(mock_fact.call_count, 1)


if __name__ == "__main__":
    unittest.main()
