"""cuDSS direct structural solver adapter (GPU)."""

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

log = logging.getLogger("solidmind.analysis_solver_cudss")


class _GPUFactorCache:
    """LRU cache with a soft VRAM budget."""

    def __init__(self, max_vram_bytes: int = 2 * 1024**3) -> None:
        self._max_vram_bytes = int(max_vram_bytes)
        self._entries: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._used_bytes = 0

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: str, solver_handle: Any, size_bytes: int) -> None:
        if key in self._entries:
            _, old_size = self._entries[key]
            self._used_bytes -= old_size
            self._entries.pop(key, None)

        self._entries[key] = (solver_handle, int(size_bytes))
        self._entries.move_to_end(key)
        self._used_bytes += int(size_bytes)

        while self._entries and self._used_bytes > self._max_vram_bytes:
            _, (_, evicted_size) = self._entries.popitem(last=False)
            self._used_bytes -= evicted_size

    def clear(self) -> None:
        self._entries.clear()
        self._used_bytes = 0


class CuDSSSolver(FieldSolver):
    """In-process GPU direct solver via nvmath/cuDSS."""

    def __init__(
        self,
        *,
        max_vram_bytes: int = 2 * 1024**3,
        precision: str = "float64",
        execution: str = "cuda",
    ) -> None:
        self._cache = _GPUFactorCache(max_vram_bytes=max_vram_bytes)
        self._precision = precision
        self._execution = execution

    def name(self) -> str:
        return "cudss"

    def analysis_types(self) -> list[AnalysisType]:
        return [AnalysisType.STRUCTURAL]

    def available(self) -> tuple[bool, str]:
        try:
            import cupy as cp
            import nvmath.sparse.advanced as _nvs  # noqa: F401
        except Exception as exc:
            return (
                False,
                "cuDSS unavailable. Install GPU deps (for example: pip install nvidia-cudss-cu12 cupy-cuda12x). "
                f"(detail: {exc})",
            )

        try:
            ndev = int(cp.cuda.runtime.getDeviceCount())
        except Exception as exc:
            return False, f"CUDA runtime unavailable (detail: {exc})"

        if ndev <= 0:
            return False, "No CUDA GPU detected"
        return True, f"cuDSS available with {ndev} CUDA device(s)"

    def supports_direct_solve(self) -> bool:
        return True

    def solve_direct(
        self,
        spec: AnalysisSpec,
        mesh_info: MeshInfo,
        work_dir: Path,
    ) -> FieldResult:
        import cupy as cp
        import cupyx.scipy.sparse as cpx_sparse

        t0 = time.monotonic()
        options = {
            "precision": self._precision,
            "execution": self._execution,
        }
        system = assemble_system(
            mesh_info,
            spec,
            precision=self._precision,
            solver_options=options,
        )
        cache_key = system.factor_cache_key(self.name())

        K_gpu = cpx_sparse.csr_matrix(system.K.tocsr())
        f_gpu = cp.asarray(
            system.f, dtype=cp.float64 if self._precision == "float64" else cp.float32
        )

        solver = self._cache.get(cache_key)
        if solver is None:
            solver = self._create_and_factorize_solver(K_gpu, f_gpu)
            self._cache.put(cache_key, solver, _estimate_csc_vram_bytes(K_gpu))
        else:
            _try_reset_operands(solver, K_gpu, f_gpu)

        x = _solve_with_solver(solver, f_gpu)
        u = cp.asnumpy(x).reshape(-1)

        solve_time = time.monotonic() - t0
        return build_field_result_from_solution(
            spec,
            system,
            np.asarray(u, dtype=np.float64),
            solver_name=self.name(),
            solve_time_s=solve_time,
        )

    def _create_and_factorize_solver(self, K_gpu: Any, f_gpu: Any) -> Any:
        import nvmath.sparse.advanced as nvs
        from nvmath.sparse.advanced._configuration import DirectSolverOptions

        # SPD (symmetric positive definite) enables Cholesky instead of LU.
        MatrixType = type(DirectSolverOptions().sparse_system_type)
        opts = DirectSolverOptions(sparse_system_type=MatrixType.SPD)
        solver = nvs.DirectSolver(K_gpu, f_gpu, options=opts)

        # nvmath API currently uses plan()/factorize(); older docs mention analyze().
        if hasattr(solver, "plan"):
            solver.plan()
        elif hasattr(solver, "analyze"):
            solver.analyze()
        else:
            raise RuntimeError("Unsupported DirectSolver API: missing plan()/analyze()")

        if hasattr(solver, "factorize"):
            solver.factorize()
        else:
            raise RuntimeError("Unsupported DirectSolver API: missing factorize()")

        return solver

    def write_input(self, spec: AnalysisSpec, mesh_info: MeshInfo, work_dir: Path) -> Path:
        raise RuntimeError("cuDSS solver uses in-process direct solve path")

    def run(self, input_path: Path, work_dir: Path) -> float:
        raise RuntimeError("cuDSS solver uses in-process direct solve path")

    def parse_results(self, work_dir: Path, spec: AnalysisSpec) -> FieldResult:
        raise RuntimeError("cuDSS solver uses in-process direct solve path")


def _try_reset_operands(solver: Any, K_gpu: Any, f_gpu: Any) -> None:
    if not hasattr(solver, "reset_operands"):
        return

    try:
        solver.reset_operands(K_gpu, f_gpu)
        return
    except TypeError:
        pass
    except Exception:
        return

    try:
        solver.reset_operands(a=K_gpu, b=f_gpu)
    except Exception:
        return


def _solve_with_solver(solver: Any, f_gpu: Any) -> Any:
    if not hasattr(solver, "solve"):
        raise RuntimeError("Unsupported DirectSolver API: missing solve()")

    try:
        return solver.solve(f_gpu)
    except TypeError:
        return solver.solve()


def _estimate_csc_vram_bytes(K_gpu: Any) -> int:
    size = 0
    for attr in ("data", "indices", "indptr"):
        arr = getattr(K_gpu, attr, None)
        if arr is None:
            continue
        try:
            size += int(arr.nbytes)
        except Exception:
            continue
    return max(size, 1)
