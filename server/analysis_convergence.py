"""Mesh-convergence study over the shared structural engine.

A single structural solve gives a peak von Mises number; it does not say whether
that number is *trustworthy*. A stress that keeps climbing as the mesh refines is
an unbounded singularity (a sharp re-entrant corner) — the rejection itself, not a
value to compare against an allowable. The honest verdict therefore needs two
solves at different mesh densities and the relative change between them.

This module owns that two-density study so both structural front doors share it:
the orchestrator's batch ``run_l2_fea`` (variant scoring) and the foam-dart
example's ``fea_latch`` (the reference behaviour these semantics were lifted from).
The engine underneath is ``solve_structural_from_step`` — one mesh→solve→parse
path, run twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from server.analysis_models import BoundaryCondition, FieldResult, Material

# Default: a peak that moves <= 10% under refinement is treated as converged.
# Matches the foam-dart reference and the retired orchestrator threshold.
DEFAULT_CONVERGENCE_TOL = 0.10


def relative_change(reference: float, value: float | None) -> float | None:
    """Relative change ``|value - reference| / value`` (a fraction, not percent).

    ``value`` is the denominator — the trusted/finer quantity. A missing or
    non-positive ``value`` yields ``None`` (no meaningful comparison). This is the
    one formula the convergence verdict is built on; callers must not re-derive it.
    """
    if not value or value <= 0.0:
        return None
    return abs(value - reference) / value


@dataclass(frozen=True, slots=True)
class ConvergenceReport:
    """Two-density structural study with a convergence verdict.

    The ``fine`` solve is the trusted result; ``coarse`` exists only to establish
    the trend. ``convergence_delta`` is a fraction (0.05 == 5%).
    """

    coarse: FieldResult
    fine: FieldResult
    peak_coarse_mpa: float
    peak_fine_mpa: float
    convergence_delta: float
    converged: bool

    @property
    def safety_factor(self) -> float:
        """Yield safety factor on the trusted (fine) peak — only valid if converged."""
        return self.fine.safety_factor

    @property
    def passed(self) -> bool:
        """A part passes only when the solve converged *and* it clears yield."""
        return self.converged and self.fine.safety_factor >= 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "peak_coarse_mpa": self.peak_coarse_mpa,
            "peak_fine_mpa": self.peak_fine_mpa,
            "convergence_delta": self.convergence_delta,
            "converged": self.converged,
            "safety_factor": self.safety_factor,
            "passed": self.passed,
            "coarse": self.coarse.to_dict(),
            "fine": self.fine.to_dict(),
        }


def run_convergence_study(
    *,
    step_path: str,
    material: Material,
    boundary_conditions: Sequence[BoundaryCondition],
    coarse_size: float,
    fine_size: float,
    solver: str = "",
    threshold: float = DEFAULT_CONVERGENCE_TOL,
    body_label: str = "",
    persist: bool = False,
    mesh_order: int = 1,
) -> ConvergenceReport:
    """Solve the same part at two mesh densities and judge convergence on peak vM.

    Both solves go through ``solve_structural_from_step`` (linear tet4, the shared
    engine), so meshing, solver fallback, and parsing are identical to the live-body
    ``analysis.stress_check`` path — only the mesh size differs between the two runs.

    ``persist`` defaults to ``False``: a convergence study runs *two* solves per part
    and is driven by batch scoring (one part is just one candidate among many), so it
    must not flood the shared analysis result store the way the interactive
    single-solve tool does.

    Raises ``StructuralSolveError`` (from the shared engine) if either solve fails, or
    if a load is applied but the fine mesh reports ~zero peak stress — that means the
    load never reached the structure (an unbound load face, or a support face that
    absorbed it), which must surface as a failure rather than a vacuous "pass".
    """
    # Imported lazily to avoid a module-load cycle (tools_analysis pulls in a wide
    # surface; this module is a thin layer over its one structural entry point).
    from server.tools_analysis import StructuralSolveError, solve_structural_from_step

    coarse = solve_structural_from_step(
        step_path=step_path,
        material=material,
        boundary_conditions=boundary_conditions,
        mesh_size=coarse_size,
        solver=solver,
        body_label=body_label,
        persist=persist,
        mesh_order=mesh_order,
    )
    fine = solve_structural_from_step(
        step_path=step_path,
        material=material,
        boundary_conditions=boundary_conditions,
        mesh_size=fine_size,
        solver=solver,
        body_label=body_label,
        persist=persist,
        mesh_order=mesh_order,
    )

    peak_coarse = coarse.max_von_mises_mpa
    peak_fine = fine.max_von_mises_mpa

    # A load that produces no stress did not actually bind — fail closed instead of
    # reporting "converged" on a degenerate zero peak.
    has_load = any(bc.bc_type in ("force", "pressure") for bc in boundary_conditions)
    if has_load and peak_fine <= 1e-9:
        raise StructuralSolveError(
            "ZERO_PEAK_UNDER_LOAD",
            "A load was applied but the fine-mesh peak von Mises is ~0 — the load did "
            "not reach the structure (check that the load face bound and is distinct "
            "from the support face).",
        )

    delta = relative_change(peak_coarse, peak_fine)
    # Only an unloaded part legitimately has no trend to judge (caught above for the
    # loaded case); treat that as converged.
    converged = True if delta is None else delta <= threshold

    return ConvergenceReport(
        coarse=coarse,
        fine=fine,
        peak_coarse_mpa=peak_coarse,
        peak_fine_mpa=peak_fine,
        convergence_delta=delta if delta is not None else 0.0,
        converged=converged,
    )
