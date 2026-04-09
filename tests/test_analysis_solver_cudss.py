"""Tests for cuDSS direct solver adapter."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from server.analysis_models import AnalysisType
from server.analysis_solver_cudss import CuDSSSolver, _GPUFactorCache, _solve_with_solver


class TestCuDSSSolver(unittest.TestCase):
    def test_basic_metadata(self) -> None:
        solver = CuDSSSolver()
        self.assertEqual(solver.name(), "cudss")
        self.assertTrue(solver.supports_direct_solve())
        self.assertIn(AnalysisType.STRUCTURAL, solver.analysis_types())

    def test_available_returns_tuple(self) -> None:
        solver = CuDSSSolver()
        ok, msg = solver.available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(msg, str)

    def test_gpu_cache_eviction(self) -> None:
        cache = _GPUFactorCache(max_vram_bytes=10)
        cache.put("a", object(), 7)
        cache.put("b", object(), 7)
        # "a" should be evicted first (LRU)
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))

    def test_solve_with_solver_signature_fallback(self) -> None:
        solver = MagicMock()
        solver.solve.side_effect = [TypeError("needs no args"), 123]
        out = _solve_with_solver(solver, object())
        self.assertEqual(out, 123)


if __name__ == "__main__":
    unittest.main()
