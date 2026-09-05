"""StudyDriver seam — search strategies behind the public ``study.*`` surface.

Control inverts here: a driver decides which design points to try and invokes
the evaluation backend for each; MCP tools submit and observe. The first
driver ports the legacy coarse→refine grid semantics onto the seam by reusing
the same builders (``server/study_runner.py``); the Dakota driver defines the
adapter slot whose file protocol lives in ``server/dakota_io.py`` — the
binary integration is a deliberate fast-follow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.study_models import Study, StudyStatus, Variant
from server.study_runner import (
    build_coarse_variants,
    build_refined_variants,
    rank_variants,
)


@dataclass(slots=True)
class EvalOutcome:
    """What one evaluation produced, flattened for ranking."""

    ok: bool
    metrics: dict[str, float] = field(default_factory=dict)
    result_hash: str | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_s: float = 0.0


class EvaluationBackend(Protocol):
    def evaluate(self, bindings: dict[str, Any], tag: str) -> EvalOutcome:
        """Evaluate one design point; bindings are keyed by binding path."""
        ...


class StudyDriver(ABC):
    name: str

    @abstractmethod
    def run(
        self,
        study: Study,
        backend: EvaluationBackend,
        *,
        on_variant: Callable[[Variant | None], None],
        cancelled: Callable[[], bool],
    ) -> str | None:
        """Drive the study to completion; return best_variant_id or None.

        Mutates ``study`` (variants, status, error, best_variant_id) and calls
        ``on_variant(variant)`` after each evaluated point — or
        ``on_variant(None)`` after a phase/status change — so the caller can
        persist. ``cancelled()`` is polled between points.
        """


def binding_paths(study: Study) -> dict[str, str]:
    """variable name → binding path map (driver mode requires paths)."""
    paths: dict[str, str] = {}
    for var in study.variables:
        if var.path is None:
            raise ValueError(f"Variable {var.name!r} has no binding path")
        paths[var.name] = var.path
    return paths


class GridDriver(StudyDriver):
    """Legacy two-phase grid semantics (coarse cartesian → refine around best)."""

    name = "grid"

    def _evaluate(
        self,
        variants: list[Variant],
        study: Study,
        backend: EvaluationBackend,
        paths: dict[str, str],
        on_variant: Callable[[Variant | None], None],
        cancelled: Callable[[], bool],
    ) -> bool:
        """Evaluate a phase; returns False when cancelled part-way."""
        for variant in variants:
            if cancelled():
                study.status = StudyStatus.CANCELLED
                on_variant(None)
                return False
            variant.status = "running"
            bindings = {paths[name]: value for name, value in variant.params.items()}
            outcome = backend.evaluate(bindings, tag=variant.variant_id)
            variant.metrics = outcome.metrics
            variant.result_hash = outcome.result_hash
            variant.flags = outcome.flags
            variant.solver_time_s = round(outcome.duration_s, 4)
            if outcome.ok:
                variant.status = "done"
            else:
                variant.status = "failed"
                variant.error = outcome.error
            on_variant(variant)
        return True

    def run(
        self,
        study: Study,
        backend: EvaluationBackend,
        *,
        on_variant: Callable[[Variant | None], None],
        cancelled: Callable[[], bool],
    ) -> str | None:
        paths = binding_paths(study)

        study.status = StudyStatus.RUNNING_COARSE
        study.coarse_variants = build_coarse_variants(study)
        on_variant(None)
        if not self._evaluate(study.coarse_variants, study, backend, paths, on_variant, cancelled):
            return None

        study.status = StudyStatus.COARSE_DONE
        on_variant(None)

        ranked_coarse = rank_variants(study.coarse_variants, study)
        if not ranked_coarse:
            study.status = StudyStatus.FAILED
            study.error = "No feasible coarse variants found"
            on_variant(None)
            return None

        study.status = StudyStatus.RUNNING_REFINED
        study.refined_variants = build_refined_variants(study, ranked_coarse[0].params)
        on_variant(None)
        if not self._evaluate(study.refined_variants, study, backend, paths, on_variant, cancelled):
            return None

        ranked = rank_variants(study.coarse_variants + study.refined_variants, study)
        if ranked:
            study.best_variant_id = ranked[0].variant_id
        study.status = StudyStatus.COMPLETE
        on_variant(None)
        return study.best_variant_id


class DakotaDriver(StudyDriver):
    """Adapter slot for the Dakota binary (file-protocol integration).

    The params.in/results.out codecs and the analysis-driver bridge live in
    ``server/dakota_io.py`` and are proven against a fake Dakota double; the
    real binary integration is a fast-follow. Without ``dakota_argv`` this
    driver refuses to run.
    """

    name = "dakota"

    def __init__(self, dakota_argv: list[str] | None = None) -> None:
        self.dakota_argv = dakota_argv

    def run(
        self,
        study: Study,
        backend: EvaluationBackend,
        *,
        on_variant: Callable[[Variant | None], None],
        cancelled: Callable[[], bool],
    ) -> str | None:
        if self.dakota_argv is None:
            raise NotImplementedError("dakota binary integration is a fast-follow")
        from server.dakota_io import run_dakota_study

        return run_dakota_study(
            study,
            backend,
            dakota_argv=self.dakota_argv,
            on_variant=on_variant,
            cancelled=cancelled,
        )


DRIVERS: dict[str, type[StudyDriver]] = {
    GridDriver.name: GridDriver,
    DakotaDriver.name: DakotaDriver,
}


def get_driver(name: str) -> StudyDriver:
    if name not in DRIVERS:
        raise KeyError(f"Unknown study driver {name!r}; expected one of {sorted(DRIVERS)}")
    return DRIVERS[name]()
