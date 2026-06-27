"""Background runner for parametric design studies.

Can be invoked as a subprocess:
    python -m server.study_runner <study_id> [--root <path>]

Reads study definition from studies/<study_id>/study.json, runs coarse sweep,
refines around best result, ranks, and updates the file after each variant.
"""
from __future__ import annotations

import argparse
import itertools
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from server.study_models import Study, StudyStatus, Variant
from server.study_solvers import get_solver
from server.study_store import load_study, save_study

log = logging.getLogger("solidmind.study_runner")

_CANCELLED = False


def _handle_sigterm(signum: int, frame: Any) -> None:
    global _CANCELLED  # noqa: PLW0603
    log.info("Received signal %d, cancelling study", signum)
    _CANCELLED = True


def _build_coarse_variants(study: Study) -> list[Variant]:
    """Build cartesian product of coarse variable expansions."""
    names: list[str] = []
    value_lists: list[list[Any]] = []
    for var in study.variables:
        names.append(var.name)
        value_lists.append(var.expand_coarse())

    variants: list[Variant] = []
    for combo in itertools.product(*value_lists):
        params = dict(zip(names, combo, strict=False))
        vid = Variant(
            variant_id=f"c{len(variants):04d}",
            params=params,
            phase="coarse",
        )
        variants.append(vid)
    return variants


def _build_refined_variants(study: Study, center_params: dict[str, Any]) -> list[Variant]:
    """Build refined grid around center_params."""
    names: list[str] = []
    value_lists: list[list[Any]] = []
    for var in study.variables:
        names.append(var.name)
        center = center_params.get(var.name, 0.0)
        if isinstance(center, (int, float)):
            value_lists.append(var.expand_refined(float(center)))
        else:
            value_lists.append([center])

    variants: list[Variant] = []
    for combo in itertools.product(*value_lists):
        params = dict(zip(names, combo, strict=False))
        vid = Variant(
            variant_id=f"r{len(variants):04d}",
            params=params,
            phase="refined",
        )
        variants.append(vid)
    return variants


def _evaluate_variant(
    variant: Variant,
    study: Study,
) -> None:
    """Run the solver for a single variant, updating it in place."""
    solver = get_solver(study.solver.solver_type)
    variant.status = "running"
    t0 = time.monotonic()
    try:
        metrics = solver.solve(
            params=variant.params,
            fixed=study.fixed_params,
            config_params=study.solver.params,
        )
        variant.metrics = metrics
        variant.status = "done"
    except Exception as exc:
        variant.status = "failed"
        variant.error = str(exc)
    variant.solver_time_s = round(time.monotonic() - t0, 4)


def _satisfies_constraints(
    variant: Variant,
    study: Study,
) -> bool:
    """Check if variant metrics satisfy objective constraint bounds."""
    for metric, (lo, hi) in study.objective.constraint_bounds.items():
        val = variant.metrics.get(metric)
        if val is None:
            return False
        if lo is not None and val < lo:
            return False
        if hi is not None and val > hi:
            return False
    return True


def _rank_variants(variants: list[Variant], study: Study) -> list[Variant]:
    """Rank variants by primary metric, filtering by constraints."""
    feasible = [v for v in variants if v.status == "done" and _satisfies_constraints(v, study)]
    metric = study.objective.primary_metric
    reverse = study.objective.direction == "maximize"
    return sorted(
        feasible,
        key=lambda v: v.metrics.get(metric, float("-inf") if reverse else float("inf")),
        reverse=reverse,
    )


def run_study(study_id: str, *, root: Path | None = None) -> None:
    """Execute the full two-phase sweep for a study."""
    global _CANCELLED  # noqa: PLW0603
    _CANCELLED = False

    study = load_study(study_id, root=root)
    study.started_at = time.time()

    # --- Phase 1: Coarse sweep ---
    study.status = StudyStatus.RUNNING_COARSE
    study.coarse_variants = _build_coarse_variants(study)
    save_study(study, root=root)

    for variant in study.coarse_variants:
        if _CANCELLED:
            study.status = StudyStatus.CANCELLED
            save_study(study, root=root)
            return
        _evaluate_variant(variant, study)
        save_study(study, root=root)

    study.status = StudyStatus.COARSE_DONE
    save_study(study, root=root)

    # Find best coarse result
    ranked_coarse = _rank_variants(study.coarse_variants, study)
    if not ranked_coarse:
        study.status = StudyStatus.FAILED
        study.error = "No feasible coarse variants found"
        save_study(study, root=root)
        return

    best_coarse = ranked_coarse[0]

    # --- Phase 2: Refined sweep ---
    study.status = StudyStatus.RUNNING_REFINED
    study.refined_variants = _build_refined_variants(study, best_coarse.params)
    save_study(study, root=root)

    for variant in study.refined_variants:
        if _CANCELLED:
            study.status = StudyStatus.CANCELLED
            save_study(study, root=root)
            return
        _evaluate_variant(variant, study)
        save_study(study, root=root)

    # --- Rank all variants ---
    all_variants = study.coarse_variants + study.refined_variants
    ranked = _rank_variants(all_variants, study)
    if ranked:
        study.best_variant_id = ranked[0].variant_id

    study.status = StudyStatus.COMPLETE
    study.finished_at = time.time()
    save_study(study, root=root)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for subprocess invocation."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Run a parametric design study.")
    parser.add_argument("study_id", help="Study ID to run")
    parser.add_argument("--root", type=Path, default=None, help="Studies root directory")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        run_study(args.study_id, root=args.root)
    except Exception:
        log.exception("Study %s failed", args.study_id)
        try:
            study = load_study(args.study_id, root=args.root)
            study.status = StudyStatus.FAILED
            study.error = "Runner crashed — see stderr"
            save_study(study, root=args.root)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
