"""Tests for server.study_solvers."""
from __future__ import annotations

import unittest

from server.study_solvers import (
    BEMTXfoilSolver,
    MockSolver,
    OpenFOAMSolver,
    get_solver,
)


class TestMockSolver(unittest.TestCase):
    def setUp(self) -> None:
        self.solver = MockSolver()

    def test_name(self) -> None:
        self.assertEqual(self.solver.name(), "mock")

    def test_available(self) -> None:
        self.assertTrue(self.solver.available())

    def test_estimate(self) -> None:
        est = self.solver.estimate_per_variant_s({})
        self.assertGreater(est, 0)

    def test_describe_pipeline(self) -> None:
        desc = self.solver.describe_pipeline()
        self.assertIsInstance(desc, str)
        self.assertTrue(len(desc) > 0)

    def test_validate_params(self) -> None:
        errors = self.solver.validate_params({}, {}, {})
        self.assertEqual(errors, [])

    def test_solve(self) -> None:
        result = self.solver.solve({"x": 50}, {}, {})
        self.assertIn("objective", result)
        self.assertIn("total_param", result)
        self.assertEqual(result["total_param"], 50.0)
        # At x=50, objective = -(50-50)^2 + 2500 = 2500
        self.assertEqual(result["objective"], 2500.0)

    def test_solve_multiple_params(self) -> None:
        result = self.solver.solve({"a": 20, "b": 30}, {}, {})
        self.assertEqual(result["total_param"], 50.0)
        self.assertEqual(result["objective"], 2500.0)


class TestBEMTXfoilSolver(unittest.TestCase):
    def test_not_available(self) -> None:
        solver = BEMTXfoilSolver()
        self.assertFalse(solver.available())

    def test_estimate(self) -> None:
        solver = BEMTXfoilSolver()
        est = solver.estimate_per_variant_s({"radial_stations": 20})
        self.assertGreater(est, 0)

    def test_validate_requires_re(self) -> None:
        solver = BEMTXfoilSolver()
        errors = solver.validate_params({}, {}, {})
        self.assertTrue(len(errors) > 0)
        self.assertIn("Re", errors[0])

    def test_validate_re_in_fixed(self) -> None:
        solver = BEMTXfoilSolver()
        errors = solver.validate_params({}, {"Re": 500000}, {})
        self.assertEqual(errors, [])


class TestOpenFOAMSolver(unittest.TestCase):
    def test_not_available(self) -> None:
        solver = OpenFOAMSolver()
        self.assertFalse(solver.available())

    def test_estimate_scales_with_refinement(self) -> None:
        solver = OpenFOAMSolver()
        est_coarse = solver.estimate_per_variant_s({"mesh_refinement": 1})
        est_fine = solver.estimate_per_variant_s({"mesh_refinement": 3})
        self.assertGreater(est_fine, est_coarse)

    def test_validate_requires_mesh_refinement(self) -> None:
        solver = OpenFOAMSolver()
        errors = solver.validate_params({}, {}, {})
        self.assertTrue(len(errors) > 0)


class TestGetSolver(unittest.TestCase):
    def test_get_mock(self) -> None:
        solver = get_solver("mock")
        self.assertEqual(solver.name(), "mock")

    def test_get_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_solver("nonexistent")


if __name__ == "__main__":
    unittest.main()
