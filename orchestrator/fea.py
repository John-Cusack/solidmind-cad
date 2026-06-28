"""L2 batch FEA: a thin adapter over the shared convergence-aware structural engine.

This module used to carry its own duplicate ``mesh → CalculiX deck → solve →
parse`` pipeline. That copy rotted (it broke silently against CalculiX 2.21) while
the live-body ``analysis.stress_check`` path stayed correct. Both now ride one
engine: meshing, deck generation, the solver fallback chain, ``.frd`` parsing, and
the two-density convergence study all live under ``server/`` and are exercised by
both front doors. What remains here is the batch adapter — frozen interfaces +
STEP file in, an ``FEAReport`` for the scorer out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.fea_bc_mapper import (
    has_loaded_interface,
    map_interface_bcs,
    surface_geometry,
)
from orchestrator.materials import Material
from orchestrator.spec import Interface, Subsystem
from server.analysis_convergence import (
    DEFAULT_CONVERGENCE_TOL,
    relative_change,
    run_convergence_study,
)
from server.analysis_models import FieldResult
from server.analysis_models import Material as AnalysisMaterial
from server.tools_analysis import StructuralSolveError

log = logging.getLogger(__name__)


class FEAError(Exception):
    """Raised on FEA pipeline failures."""


#: Safety factor reported for an unloaded part. A large *finite* sentinel rather
#: than float('inf') — the value flows into VerificationResults and variant scores
#: that are serialized with ``json.dumps``, and ``inf`` emits the non-standard
#: ``Infinity`` token that strict JSON consumers (and the socket protocol) reject.
UNLOADED_SAFETY_FACTOR = 1.0e9


@dataclass(slots=True)
class FEAReport:
    """L2 FEA verdict for one variant, populated from the shared convergence study.

    ``peak_fine_mpa`` is the *filtered* peak von Mises on the fine mesh — the
    top-5% mesh-singularity spikes excluded — and ``safety_factor``/``converged``
    are taken on it, so an incidental sharp corner doesn't tank a sound part.
    ``convergence_pct`` is the relative change of that filtered peak in percent.
    """

    subsystem_name: str = ""
    coarse: FieldResult | None = None
    fine: FieldResult | None = None
    peak_fine_mpa: float = 0.0
    convergence_pct: float = 0.0
    converged: bool = False
    safety_factor: float = 0.0
    passed: bool = False


def _to_analysis_material(m: Material) -> AnalysisMaterial:
    """Translate the orchestrator material into the shared-engine material."""
    return AnalysisMaterial(
        name=m.name,
        youngs_modulus_mpa=m.young_modulus_mpa,
        poissons_ratio=m.poisson_ratio,
        density_kg_m3=m.density_kg_m3,
        yield_strength_mpa=m.yield_strength_mpa,
    )


def run_l2_fea(
    step_path: Path,
    subsystem: Subsystem,
    interfaces: list[Interface],
    material: Material,
    work_dir: Path,
) -> FEAReport:
    """Run the batch L2 FEA on a STEP file via the shared convergence engine.

    1. Locate STEP faces for each interface frame, build face-tagged BCs.
    2. Solve at coarse (2× min feature) and fine (1× min feature) mesh densities.
    3. Judge convergence on peak von Mises; safety factor from the fine peak.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    report = FEAReport(subsystem_name=subsystem.name)

    geometry = surface_geometry(step_path)
    if not geometry:
        # surface_geometry returns {} only when the STEP faces could not be
        # enumerated at all (gmsh missing, or an unreadable/degenerate STEP). That
        # is an analysis *failure*, not an unloaded part — fail closed so it never
        # masquerades as a structural pass.
        raise FEAError(
            f"could not enumerate STEP faces for {subsystem.name} "
            "(gmsh not installed or STEP unreadable)"
        )

    bcs = map_interface_bcs(subsystem, interfaces, geometry)
    has_load = any(b.bc_type in ("force", "pressure") for b in bcs)
    if not has_load:
        if has_loaded_interface(subsystem, interfaces):
            # Loads were declared but none could be placed on a face (e.g. every
            # load face collided with a support, or the frames fell outside the
            # geometry). That is a setup failure, NOT an unloaded part — fail closed
            # so an un-stressed load-bearing variant can't pass the gate.
            raise FEAError(
                f"{subsystem.name} has loaded interfaces but none mapped to a face; cannot run FEA"
            )
        # Genuinely no load declared → FEA is not applicable; don't gate on it.
        log.warning("No applied load for %s — skipping FEA", subsystem.name)
        report.passed = True
        report.converged = True
        report.safety_factor = UNLOADED_SAFETY_FACTOR
        return report

    min_feat = subsystem.manufacturing.min_feature_size_mm
    coarse_size = max(2.0 * min_feat, 1.0)
    fine_size = max(1.0 * min_feat, 0.5)

    try:
        study = run_convergence_study(
            step_path=str(step_path),
            material=_to_analysis_material(material),
            boundary_conditions=bcs,
            coarse_size=coarse_size,
            fine_size=fine_size,
            body_label=subsystem.name,
            # Batch scoring uses quadratic tet10 — linear tets are over-stiff and
            # under-predict peak stress, which would bias the safety gate optimistic.
            mesh_order=2,
        )
    except StructuralSolveError as exc:
        raise FEAError(str(exc)) from exc

    # Batch scoring judges the part on the *filtered* peak (mesh-singularity spikes
    # excluded), not the raw peak: real CAD variants routinely have an incidental
    # sharp corner whose singular spike both never converges and tanks the safety
    # factor. Convergence and SF are therefore taken on the filtered stress so a
    # structurally sound variant isn't eliminated by a meshing artifact. (The
    # foam-dart example keeps the raw-peak verdict — there a sharp root *should*
    # diverge and be rejected.)
    peak_c = study.coarse.filtered_peak_von_mises_mpa or study.coarse.max_von_mises_mpa
    peak_f = study.fine.filtered_peak_von_mises_mpa or study.fine.max_von_mises_mpa
    delta = relative_change(peak_c, peak_f)
    yield_mpa = material.yield_strength_mpa

    report.coarse = study.coarse
    report.fine = study.fine
    report.peak_fine_mpa = peak_f
    report.convergence_pct = (delta or 0.0) * 100.0
    report.converged = delta is None or delta <= DEFAULT_CONVERGENCE_TOL
    report.safety_factor = yield_mpa / peak_f if peak_f > 0 else UNLOADED_SAFETY_FACTOR
    report.passed = report.converged and report.safety_factor >= 1.0

    log.info(
        "L2 FEA %s: peak=%.1f MPa, SF=%.2f, conv=%.1f%%, %s",
        subsystem.name,
        report.peak_fine_mpa,
        report.safety_factor,
        report.convergence_pct,
        "PASS" if report.passed else "FAIL",
    )
    return report


def run_l3_fea(*args: Any, **kwargs: Any) -> None:
    """L3 high-fidelity FEA — not yet implemented.

    Future: nonlinear materials, fatigue (S-N curves), refined meshing.
    """
    raise NotImplementedError("L3 high-fidelity FEA is not yet implemented")
