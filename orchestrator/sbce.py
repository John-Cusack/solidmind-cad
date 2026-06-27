"""Set-Based Concurrent Engineering — feasible-set intersection and beam search.

SBCE narrows the design space by intersecting feasible sets across subsystem
boundaries, then ranks assembly-level combinations rather than per-subsystem
variants.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any

from orchestrator.spec import MasterSpec

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Variant tracking
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Variant:
    """A single variant of a subsystem."""

    subsystem_name: str
    variant_index: int
    feasible: bool = True
    measured: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    elimination_reason: str = ""


@dataclass(slots=True)
class AssemblyCandidate:
    """A specific combination of variants — one per subsystem."""

    variants: dict[str, Variant] = field(default_factory=dict)  # subsystem → variant
    feasible: bool = True
    assembly_score: float = 0.0
    interface_passes: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Feasible-set intersection
# ---------------------------------------------------------------------------


def filter_feasible(
    variants: list[Variant],
    spec: MasterSpec,
) -> list[Variant]:
    """Remove variants that fail hard thresholds or validation."""
    feasible: list[Variant] = []
    for v in variants:
        if not v.feasible:
            continue
        ok = True
        for obj in spec.objectives:
            score = v.scores.get(obj.name)
            if score is None:
                score = v.measured.get(obj.name)
            if obj.threshold is not None and score is not None:
                if obj.direction == "minimize" and score > obj.threshold:
                    v.feasible = False
                    v.elimination_reason = (
                        f"{obj.name}={score:.4f} > threshold {obj.threshold}"
                    )
                    ok = False
                    break
                elif obj.direction == "maximize" and score < obj.threshold:
                    v.feasible = False
                    v.elimination_reason = (
                        f"{obj.name}={score:.4f} < threshold {obj.threshold}"
                    )
                    ok = False
                    break
        if ok:
            feasible.append(v)
    return feasible


def intersect_feasible_sets(
    variants_by_subsystem: dict[str, list[Variant]],
    spec: MasterSpec,
) -> dict[str, list[Variant]]:
    """Narrow feasible sets by interface compatibility.

    A variant is only feasible if at least one compatible variant exists
    for each connected subsystem.
    """
    # Build adjacency from interfaces
    connections: dict[str, set[str]] = {}
    for ifc in spec.interfaces:
        a, b = ifc.subsystem_a, ifc.subsystem_b
        if a in variants_by_subsystem:
            connections.setdefault(a, set()).add(b)
        if b in variants_by_subsystem:
            connections.setdefault(b, set()).add(a)

    narrowed = dict(variants_by_subsystem)
    changed = True
    iterations = 0

    while changed and iterations < 20:
        changed = False
        iterations += 1
        for sub_name, partners in connections.items():
            if sub_name not in narrowed:
                continue
            for partner in partners:
                if partner not in narrowed:
                    continue
                # A variant is feasible only if at least one partner variant exists
                before = len(narrowed[sub_name])
                narrowed[sub_name] = [
                    v for v in narrowed[sub_name]
                    if v.feasible and len(narrowed[partner]) > 0
                ]
                if len(narrowed[sub_name]) < before:
                    changed = True

    return narrowed


# ---------------------------------------------------------------------------
# Assembly combination enumeration
# ---------------------------------------------------------------------------


def enumerate_candidates(
    variants_by_subsystem: dict[str, list[Variant]],
    max_candidates: int = 50,
) -> list[AssemblyCandidate]:
    """Generate assembly candidates from the Cartesian product of variants.

    If the product exceeds max_candidates, only the first max_candidates
    combinations are returned (use beam_search for smarter pruning).
    """
    subsystem_names = sorted(variants_by_subsystem.keys())
    if not subsystem_names:
        return []

    variant_lists = [variants_by_subsystem[name] for name in subsystem_names]
    candidates: list[AssemblyCandidate] = []

    for combo in itertools.islice(itertools.product(*variant_lists), max_candidates):
        candidate = AssemblyCandidate(
            variants={v.subsystem_name: v for v in combo},
        )
        candidates.append(candidate)

    return candidates


# ---------------------------------------------------------------------------
# Beam search
# ---------------------------------------------------------------------------


def beam_search(
    variants_by_subsystem: dict[str, list[Variant]],
    spec: MasterSpec,
    *,
    beam_width: int = 5,
) -> list[AssemblyCandidate]:
    """Assembly-level beam search when Cartesian product is too large.

    Incrementally extends partial assemblies, scoring and pruning to
    beam_width at each step.
    """
    subsystem_names = sorted(variants_by_subsystem.keys())
    if not subsystem_names:
        return []

    # Start with first subsystem's variants
    beams: list[AssemblyCandidate] = [
        AssemblyCandidate(variants={v.subsystem_name: v})
        for v in variants_by_subsystem[subsystem_names[0]]
    ]

    # Extend by each subsequent subsystem
    for sub_name in subsystem_names[1:]:
        next_beams: list[AssemblyCandidate] = []
        for partial in beams:
            for variant in variants_by_subsystem[sub_name]:
                extended = AssemblyCandidate(
                    variants={**partial.variants, variant.subsystem_name: variant},
                )
                extended.assembly_score = _score_partial(extended, spec)
                next_beams.append(extended)

        # Prune to beam_width best candidates
        next_beams.sort(key=lambda c: c.assembly_score, reverse=True)
        beams = next_beams[:beam_width]

    return beams


_ADDITIVE_OBJECTIVES = {"mass", "cost", "volume_mm3"}


def _score_partial(candidate: AssemblyCandidate, spec: MasterSpec) -> float:
    """Score a partial assembly by summing weighted objective scores."""
    total = 0.0
    for obj in spec.objectives:
        values: list[float] = []
        for v in candidate.variants.values():
            score = v.scores.get(obj.name)
            if score is None:
                score = v.measured.get(obj.name)
            if score is not None:
                values.append(score)
        if not values:
            continue
        # Additive properties (mass, cost, volume) are summed; others averaged
        if obj.name in _ADDITIVE_OBJECTIVES:
            agg = sum(values)
        else:
            agg = sum(values) / len(values)
        # Normalize direction: higher is better
        if obj.direction == "minimize":
            agg = -agg
        total += agg * obj.weight
    return total


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank_candidates(
    candidates: list[AssemblyCandidate],
    spec: MasterSpec,
) -> list[AssemblyCandidate]:
    """Score and sort assembly candidates by weighted objectives."""
    for c in candidates:
        c.assembly_score = _score_partial(c, spec)
    candidates.sort(key=lambda c: c.assembly_score, reverse=True)
    return candidates


def pareto_frontier(
    candidates: list[AssemblyCandidate],
    spec: MasterSpec,
) -> list[AssemblyCandidate]:
    """Extract the Pareto frontier from ranked candidates.

    A candidate is Pareto-dominated if another candidate is strictly
    better on all objectives.
    """
    if not candidates or not spec.objectives:
        return list(candidates)

    def _obj_values(c: AssemblyCandidate) -> list[float]:
        vals = []
        for obj in spec.objectives:
            total = 0.0
            count = 0
            for v in c.variants.values():
                score = v.scores.get(obj.name)
                if score is None:
                    score = v.measured.get(obj.name)
                if score is not None:
                    total += score
                    count += 1
            vals.append(total / count if count else 0.0)
        return vals

    frontier: list[AssemblyCandidate] = []
    for c in candidates:
        c_vals = _obj_values(c)
        dominated = False
        for other in candidates:
            if other is c:
                continue
            o_vals = _obj_values(other)
            if _dominates(o_vals, c_vals, spec):
                dominated = True
                break
        if not dominated:
            frontier.append(c)
    return frontier


def _dominates(
    a: list[float],
    b: list[float],
    spec: MasterSpec,
) -> bool:
    """True if *a* strictly dominates *b* on all objectives."""
    all_better = True
    for i, obj in enumerate(spec.objectives):
        if obj.direction == "minimize":
            if a[i] >= b[i]:
                all_better = False
                break
        else:
            if a[i] <= b[i]:
                all_better = False
                break
    return all_better
