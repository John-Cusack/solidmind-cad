"""CHOLMOD direct structural solver adapter."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from server.analysis_assembly import assemble_system, build_field_result_from_solution
from server.analysis_models import AnalysisSpec, AnalysisType, FieldResult, MeshInfo
from server.analysis_solvers import FieldSolver

log = logging.getLogger("solidmind.analysis_solver_cholmod")


class _FactorCache:
    """Small LRU cache for CHOLMOD factors."""

    def __init__(self, max_entries: int = 8) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: str, factor: Any) -> None:
        self._entries[key] = factor
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class CHOLMODSolver(FieldSolver):
    """In-process direct solver backed by SuiteSparse CHOLMOD."""

    def __init__(
        self,
        *,
        max_cache_entries: int = 8,
        ordering_method: str = "default",
        precision: str = "float64",
    ) -> None:
        self._cache = _FactorCache(max_entries=max_cache_entries)
        self._ordering_method = ordering_method
        self._precision = precision

    def name(self) -> str:
        return "cholmod"

    def analysis_types(self) -> list[AnalysisType]:
        return [AnalysisType.STRUCTURAL]

    def available(self) -> tuple[bool, str]:
        try:
            import sksparse.cholmod  # noqa: F401

            return True, "scikit-sparse CHOLMOD available"
        except Exception as exc:
            return (
                False,
                "CHOLMOD unavailable. Install with: pip install scikit-sparse and apt install libsuitesparse-dev "
                f"(detail: {exc})",
            )

    def supports_direct_solve(self) -> bool:
        return True

    def solve_direct(
        self,
        spec: AnalysisSpec,
        mesh_info: MeshInfo,
        work_dir: Path,
    ) -> FieldResult:
        t0 = time.monotonic()
        options = {
            "ordering_method": self._ordering_method,
            "precision": self._precision,
        }

        system = assemble_system(
            mesh_info,
            spec,
            precision=self._precision,
            solver_options=options,
        )
        cache_key = system.factor_cache_key(self.name())

        factor = self._cache.get(cache_key)
        if factor is None:
            factor = self._factorize(system.K)
            self._cache.put(cache_key, factor)

        # scikit-sparse >=0.5: .solve(); older: .solve_A()
        solve_fn = getattr(factor, "solve", None) or factor.solve_A
        u = solve_fn(system.f)
        solve_time = time.monotonic() - t0
        return build_field_result_from_solution(
            spec,
            system,
            np.asarray(u, dtype=np.float64),
            solver_name=self.name(),
            solve_time_s=solve_time,
        )

    def _factorize(self, K: Any) -> Any:
        import sksparse.cholmod as cholmod

        # scikit-sparse >=0.5 uses cho_factor() returning CholeskyFactor
        # with .solve(); older versions use cholesky() with .solve_A().
        if hasattr(cholmod, "cho_factor"):
            return cholmod.cho_factor(K, order=self._ordering_method)
        return cholmod.cholesky(K, ordering_method=self._ordering_method)

    def write_input(self, spec: AnalysisSpec, mesh_info: MeshInfo, work_dir: Path) -> Path:
        raise RuntimeError("CHOLMOD solver uses in-process direct solve path")

    def run(self, input_path: Path, work_dir: Path) -> float:
        raise RuntimeError("CHOLMOD solver uses in-process direct solve path")

    def parse_results(self, work_dir: Path, spec: AnalysisSpec) -> FieldResult:
        raise RuntimeError("CHOLMOD solver uses in-process direct solve path")
