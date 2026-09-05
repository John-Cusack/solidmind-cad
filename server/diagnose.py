"""Deterministic diagnosis — ``diagnose(results, study) -> findings``.

Much of "was the frame wrong?" is computable without a language model:

- the optimum sits on a bound  -> the bound, not the design, is binding
- the objective barely moves   -> the parameterization misses what matters
- a variable never changes the fingerprint -> it is a no-op in this model set
- validity-domain hits         -> the model is being queried outside its envelope
- nothing feasible             -> the constraints or objective are wrong
- evaluations failing en masse -> the setup, not the design, is broken

This module is that checklist. It runs **before** any LLM and is the baseline
the LLM arm has to beat at the decision gate — a prescriber that cannot beat
a list of six deterministic rules has not earned its place. Findings carry a
``suggested_action`` naming the typed action a prescriber would emit, so the
checklist can be scored on exactly the same axis as the LLM.

The findings are evidence, not verdicts: several map to more than one defect,
and telling them apart is precisely the judgment the benchmark measures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from server.eval_models import EvalResult
from server.study_models import Study, Variant

# Relative span below which the objective is considered flat across the sweep.
FLAT_OBJECTIVE_REL_SPAN = 1e-6
# Fraction of the sweep's objective range a single variable must explain to
# count as dominant (used for the "sensitivity concentrated" finding).
DOMINANCE_FRACTION = 0.9
# Fraction of evaluations that must fail before the run itself is suspect.
FAILURE_RATE_THRESHOLD = 0.5


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_action: str = ""  # typed action a prescriber would emit

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True)
class Diagnosis:
    findings: tuple[Finding, ...] = ()

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [f.to_dict() for f in self.findings]}


def _completed(study: Study) -> list[Variant]:
    return [v for v in study.coarse_variants + study.refined_variants if v.status == "done"]


def _objective_values(variants: Sequence[Variant], metric: str) -> list[float]:
    return [v.metrics[metric] for v in variants if metric in v.metrics]


def _best(variants: Sequence[Variant], study: Study) -> Variant | None:
    metric = study.objective.primary_metric
    scored = [v for v in variants if metric in v.metrics]
    if not scored:
        return None
    reverse = study.objective.direction == "maximize"
    return sorted(scored, key=lambda v: v.metrics[metric], reverse=reverse)[0]


def _at_bound(value: Any, lo: float | None, hi: float | None) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    tol = 1e-9
    if lo is not None and abs(float(value) - lo) <= tol * max(1.0, abs(lo)):
        return "min"
    if hi is not None and abs(float(value) - hi) <= tol * max(1.0, abs(hi)):
        return "max"
    return None


def diagnose(results: Sequence[EvalResult], study: Study) -> Diagnosis:
    """Deterministic findings over a completed study and its evaluation results."""
    findings: list[Finding] = []
    all_variants = study.coarse_variants + study.refined_variants
    completed = _completed(study)
    metric = study.objective.primary_metric

    # --- the run itself -----------------------------------------------------
    if all_variants:
        failed = [v for v in all_variants if v.status == "failed"]
        if len(failed) / len(all_variants) >= FAILURE_RATE_THRESHOLD:
            reasons = sorted({(v.error or "").split(":")[0] for v in failed if v.error})
            findings.append(
                Finding(
                    code="evaluations_failing",
                    severity=Severity.BLOCKING,
                    summary=f"{len(failed)}/{len(all_variants)} evaluations failed",
                    evidence={
                        "failed": len(failed),
                        "total": len(all_variants),
                        "reasons": reasons,
                    },
                    suggested_action="escalate",
                )
            )

    if not completed:
        findings.append(
            Finding(
                code="no_completed_evaluations",
                severity=Severity.BLOCKING,
                summary="No evaluation completed; nothing to optimize over",
                suggested_action="escalate",
            )
        )
        return Diagnosis(findings=tuple(findings))

    # --- feasibility --------------------------------------------------------
    bounds = study.objective.constraint_bounds
    if bounds:
        feasible = []
        for v in completed:
            ok = True
            for name, (lo, hi) in bounds.items():
                value = v.metrics.get(name)
                if (
                    value is None
                    or (lo is not None and value < lo)
                    or (hi is not None and value > hi)
                ):
                    ok = False
                    break
            if ok:
                feasible.append(v)
        if not feasible:
            findings.append(
                Finding(
                    code="no_feasible_variants",
                    severity=Severity.BLOCKING,
                    summary="No evaluated design satisfied the constraints",
                    evidence={"constraints": {k: list(v) for k, v in bounds.items()}},
                    suggested_action="patch:study.constraints|escalate",
                )
            )

    # --- objective discrimination ------------------------------------------
    values = _objective_values(completed, metric)
    if not values:
        findings.append(
            Finding(
                code="objective_metric_missing",
                severity=Severity.BLOCKING,
                summary=f"Objective metric {metric!r} was not produced by any evaluation",
                evidence={"metric": metric},
                suggested_action="patch:study.objectives",
            )
        )
    else:
        span = max(values) - min(values)
        scale = max(abs(max(values)), abs(min(values)), 1e-12)
        if span / scale <= FLAT_OBJECTIVE_REL_SPAN:
            findings.append(
                Finding(
                    code="objective_flat",
                    severity=Severity.WARNING,
                    summary=(
                        f"Objective {metric!r} is effectively constant across the sweep "
                        "— the parameterization may not contain what matters"
                    ),
                    evidence={"metric": metric, "span": span, "value": values[0]},
                    suggested_action="patch:study.variables",
                )
            )

    # --- optimum on a bound -------------------------------------------------
    best = _best(completed, study)
    if best is not None:
        for var in study.variables:
            side = _at_bound(best.params.get(var.name), var.min_val, var.max_val)
            if side is not None:
                findings.append(
                    Finding(
                        code="optimum_at_bound",
                        severity=Severity.WARNING,
                        summary=(
                            f"Best design sits on the {side} bound of {var.name!r} "
                            "— the bound is binding, not the physics"
                        ),
                        evidence={
                            "variable": var.name,
                            "bound": side,
                            "value": best.params.get(var.name),
                            "min": var.min_val,
                            "max": var.max_val,
                        },
                        suggested_action="patch:study.bounds",
                    )
                )

    # --- per-variable sensitivity ------------------------------------------
    if values and len(study.variables) > 1:
        spans: dict[str, float] = {}
        for var in study.variables:
            groups: dict[Any, list[float]] = {}
            for v in completed:
                if metric in v.metrics and var.name in v.params:
                    groups.setdefault(v.params[var.name], []).append(v.metrics[metric])
            if len(groups) > 1:
                means = [sum(g) / len(g) for g in groups.values()]
                spans[var.name] = max(means) - min(means)
        total = sum(spans.values())
        if total > 0:
            dominant = [name for name, s in spans.items() if s / total >= DOMINANCE_FRACTION]
            inert = [name for name, s in spans.items() if s / total <= (1 - DOMINANCE_FRACTION)]
            if dominant and inert:
                findings.append(
                    Finding(
                        code="sensitivity_concentrated",
                        severity=Severity.INFO,
                        summary=(
                            f"{dominant[0]!r} explains almost all objective variation; "
                            f"{inert} barely move it"
                        ),
                        evidence={"spans": spans, "dominant": dominant, "inert": inert},
                        suggested_action="patch:study.variables",
                    )
                )

    # --- signals carried on the evaluation results --------------------------
    # A no-op variable is one whose bindings *never* move the materialized
    # inputs. A single unchanged fingerprint means nothing — binding a
    # parameter to the value it already holds is trivially a no-op — so the
    # detector is a cluster: every evaluation of that path flagged, across at
    # least two distinct values.
    bound_values: dict[str, set[Any]] = {}
    flagged_values: dict[str, set[Any]] = {}
    for r in results:
        flagged = bool(r.flags.get("unchanged_fingerprint"))
        for binding in r.applied_bindings:
            path, value = binding["path"], binding["value"]
            key = value if isinstance(value, (str, bool)) else float(value)
            bound_values.setdefault(path, set()).add(key)
            if flagged:
                flagged_values.setdefault(path, set()).add(key)
    no_op = sorted(
        path
        for path, values in bound_values.items()
        if len(values) >= 2 and flagged_values.get(path, set()) == values
    )
    if no_op:
        findings.append(
            Finding(
                code="unchanged_fingerprint",
                severity=Severity.WARNING,
                summary=(
                    "Bindings changed no materialized input — these variables are "
                    "no-ops for the selected models"
                ),
                evidence={"paths": no_op},
                suggested_action="patch:study.variables",
            )
        )

    hits: dict[str, list[Any]] = {}
    for r in results:
        for hit in r.validity_domain_hits:
            hits.setdefault(hit["path"], []).append(hit["value"])
    if hits:
        findings.append(
            Finding(
                code="validity_domain_hit",
                severity=Severity.WARNING,
                summary=(
                    "Evaluations queried a model outside its calibrated envelope "
                    "— results there are extrapolation"
                ),
                evidence={"paths": sorted(hits), "count": sum(len(v) for v in hits.values())},
                suggested_action="request_measurement",
            )
        )

    uncalibrated = sorted({m for r in results for m in r.uncalibrated_models})
    if uncalibrated and best is not None:
        findings.append(
            Finding(
                code="uncalibrated_model_in_optimum",
                severity=Severity.INFO,
                summary=(
                    "The optimum leans on model terms with no anchored validity "
                    "envelope — a calibration debt, not a result"
                ),
                evidence={"terms": uncalibrated},
                suggested_action="request_measurement",
            )
        )

    return Diagnosis(findings=tuple(findings))


def diagnose_study(
    study: Study, results: Sequence[EvalResult] | None = None, *, root: Any = None
) -> Diagnosis:
    """``diagnose`` with results loaded from the artifact store when available."""
    if results is None:
        from server import artifact_store as cas  # local import: keeps diagnose pure

        loaded: list[EvalResult] = []
        for variant in study.coarse_variants + study.refined_variants:
            if not variant.result_hash:
                continue
            try:
                loaded.append(EvalResult.from_dict(cas.get_json(variant.result_hash, root=root)))
            except cas.ArtifactError:
                continue
        results = loaded
    return diagnose(results, study)


def summarize(diagnosis: Diagnosis) -> str:
    """One line per finding — for logs and human review, never for machines."""
    if not diagnosis.findings:
        return "no findings"
    return "\n".join(f"[{f.severity.value}] {f.code}: {f.summary}" for f in diagnosis.findings)
