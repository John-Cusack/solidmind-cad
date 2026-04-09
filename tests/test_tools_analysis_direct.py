"""Tests for direct-solver fallback helpers in analysis tools."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from server.analysis_models import (
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    FieldResult,
    Material,
    MeshInfo,
)
import server.tools_analysis as mod


class _FakeSolver:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name

    def analysis_types(self) -> list[AnalysisType]:
        return [AnalysisType.STRUCTURAL]

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def supports_direct_solve(self) -> bool:
        return False


class TestDirectFallbackHelpers(unittest.TestCase):
    def test_solver_chain_prefers_cudss_then_fallbacks(self) -> None:
        cudss = _FakeSolver("cudss")
        cholmod = _FakeSolver("cholmod")
        calculix = _FakeSolver("calculix")

        def _fake_get_solver(name: str, analysis_type: AnalysisType, dof_count=None):
            if name == "cudss":
                return cudss
            if name == "cholmod":
                return cholmod
            if name == "calculix":
                return calculix
            return None

        with patch.object(mod, "get_solver", side_effect=_fake_get_solver):
            chain = mod._structural_solver_chain(cudss, allow_fallback=True)

        self.assertEqual([s.name() for s in chain], ["cudss", "cholmod", "calculix"])

    def test_runtime_fallback_uses_next_solver_on_failure(self) -> None:
        cudss = _FakeSolver("cudss")
        cholmod = _FakeSolver("cholmod")

        result = FieldResult(
            analysis_id="",
            status=CheckStatus.PASS,
            safety_factor=2.0,
            max_von_mises_mpa=100.0,
            max_displacement_mm=0.1,
            checks=(),
            scalar_fields=(),
            solver_name="cholmod",
            solve_time_s=0.01,
        )

        with (
            patch.object(mod, "_structural_solver_chain", return_value=[cudss, cholmod]),
            patch.object(
                mod,
                "_run_structural_once",
                side_effect=[RuntimeError("gpu oom"), (result, 0.02)],
            ),
        ):
            used, out, solve_time = mod._solve_structural_with_fallback(
                analysis_id="a1",
                body="Body",
                material=Material(
                    name="steel",
                    youngs_modulus_mpa=200_000,
                    poissons_ratio=0.3,
                    density_kg_m3=7800,
                    yield_strength_mpa=250,
                ),
                boundary_conditions=(BoundaryCondition(bc_type="fixed", faces=("Face1",), value={}),),
                mesh_size=1.0,
                mesh_info=MeshInfo(
                    path="/tmp/m.msh",
                    num_nodes=10,
                    num_elements=2,
                    element_type="tet4",
                    physical_groups={"Face1": 1},
                ),
                primary_solver=cudss,
                allow_fallback=True,
            )

        self.assertEqual(used.name(), "cholmod")
        self.assertEqual(out.status, CheckStatus.PASS)
        self.assertGreater(solve_time, 0)


if __name__ == "__main__":
    unittest.main()
