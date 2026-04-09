"""Tests for orchestrator.sbce."""
from __future__ import annotations

import unittest

from orchestrator.sbce import (
    AssemblyCandidate,
    Variant,
    beam_search,
    enumerate_candidates,
    filter_feasible,
    intersect_feasible_sets,
    pareto_frontier,
    rank_candidates,
)
from orchestrator.spec import Interface, MasterSpec, Objective


def _make_spec() -> MasterSpec:
    return MasterSpec(
        name="test",
        objectives=[
            Objective(name="mass", direction="minimize", unit="kg", weight=1.0, threshold=0.1),
            Objective(name="strength", direction="maximize", unit="MPa", weight=0.5),
        ],
    )


def _variants(name: str, count: int) -> list[Variant]:
    return [
        Variant(subsystem_name=name, variant_index=i, feasible=True)
        for i in range(count)
    ]


class TestFilterFeasible(unittest.TestCase):
    def test_passes_within_threshold(self) -> None:
        spec = _make_spec()
        v = Variant(subsystem_name="gear", variant_index=0, scores={"mass": 0.05})
        result = filter_feasible([v], spec)
        self.assertEqual(len(result), 1)

    def test_eliminates_above_threshold(self) -> None:
        spec = _make_spec()
        v = Variant(subsystem_name="gear", variant_index=0, scores={"mass": 0.2})
        result = filter_feasible([v], spec)
        self.assertEqual(len(result), 0)
        self.assertFalse(v.feasible)

    def test_maximize_threshold(self) -> None:
        spec = MasterSpec(objectives=[
            Objective(name="strength", direction="maximize", unit="MPa", threshold=100),
        ])
        v_pass = Variant(subsystem_name="a", variant_index=0, scores={"strength": 150})
        v_fail = Variant(subsystem_name="a", variant_index=1, scores={"strength": 50})
        result = filter_feasible([v_pass, v_fail], spec)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].variant_index, 0)

    def test_no_score_passes(self) -> None:
        spec = _make_spec()
        v = Variant(subsystem_name="gear", variant_index=0, scores={})
        result = filter_feasible([v], spec)
        self.assertEqual(len(result), 1)


class TestIntersectFeasibleSets(unittest.TestCase):
    def test_no_narrowing_needed(self) -> None:
        spec = MasterSpec(name="test", interfaces=[
            Interface(id="i1", subsystem_a="a", subsystem_b="b"),
        ])
        by_sub = {"a": _variants("a", 2), "b": _variants("b", 2)}
        result = intersect_feasible_sets(by_sub, spec)
        self.assertEqual(len(result["a"]), 2)
        self.assertEqual(len(result["b"]), 2)

    def test_empty_partner_eliminates(self) -> None:
        spec = MasterSpec(name="test", interfaces=[
            Interface(id="i1", subsystem_a="a", subsystem_b="b"),
        ])
        by_sub = {"a": _variants("a", 2), "b": []}
        result = intersect_feasible_sets(by_sub, spec)
        self.assertEqual(len(result["a"]), 0)


class TestEnumerateCandidates(unittest.TestCase):
    def test_cartesian_product(self) -> None:
        by_sub = {"a": _variants("a", 2), "b": _variants("b", 3)}
        candidates = enumerate_candidates(by_sub)
        self.assertEqual(len(candidates), 6)

    def test_max_limit(self) -> None:
        by_sub = {"a": _variants("a", 5), "b": _variants("b", 5)}
        candidates = enumerate_candidates(by_sub, max_candidates=10)
        self.assertEqual(len(candidates), 10)

    def test_empty(self) -> None:
        candidates = enumerate_candidates({})
        self.assertEqual(len(candidates), 0)


class TestBeamSearch(unittest.TestCase):
    def test_basic_search(self) -> None:
        spec = _make_spec()
        va = _variants("a", 3)
        vb = _variants("b", 3)
        for v in va:
            v.scores = {"mass": 0.05 * (v.variant_index + 1)}
        for v in vb:
            v.scores = {"mass": 0.03 * (v.variant_index + 1)}

        by_sub = {"a": va, "b": vb}
        results = beam_search(by_sub, spec, beam_width=2)
        self.assertLessEqual(len(results), 2)

    def test_empty(self) -> None:
        spec = _make_spec()
        results = beam_search({}, spec)
        self.assertEqual(len(results), 0)


class TestRankCandidates(unittest.TestCase):
    def test_ranking_order(self) -> None:
        spec = _make_spec()
        v1 = Variant(subsystem_name="a", variant_index=0, scores={"mass": 0.05})
        v2 = Variant(subsystem_name="a", variant_index=1, scores={"mass": 0.02})

        c1 = AssemblyCandidate(variants={"a": v1})
        c2 = AssemblyCandidate(variants={"a": v2})

        ranked = rank_candidates([c1, c2], spec)
        # Lower mass is better (minimize), so v2 should rank first
        self.assertEqual(ranked[0].variants["a"].variant_index, 1)


class TestParetoFrontier(unittest.TestCase):
    def test_single_candidate(self) -> None:
        spec = _make_spec()
        v = Variant(subsystem_name="a", variant_index=0, scores={"mass": 0.05, "strength": 100})
        c = AssemblyCandidate(variants={"a": v})
        frontier = pareto_frontier([c], spec)
        self.assertEqual(len(frontier), 1)

    def test_dominated_removed(self) -> None:
        spec = MasterSpec(objectives=[
            Objective(name="mass", direction="minimize", unit="kg"),
            Objective(name="cost", direction="minimize", unit="USD"),
        ])
        v1 = Variant(subsystem_name="a", variant_index=0, scores={"mass": 0.05, "cost": 10})
        v2 = Variant(subsystem_name="a", variant_index=1, scores={"mass": 0.06, "cost": 11})
        c1 = AssemblyCandidate(variants={"a": v1})
        c2 = AssemblyCandidate(variants={"a": v2})
        frontier = pareto_frontier([c1, c2], spec)
        # v1 dominates v2 on both objectives
        self.assertEqual(len(frontier), 1)
        self.assertEqual(frontier[0].variants["a"].variant_index, 0)


class TestFilterFeasibleChecksMeasured(unittest.TestCase):
    """Phase 3b: filter_feasible falls back to measured dict."""

    def test_measured_mass_triggers_threshold(self) -> None:
        spec = _make_spec()  # mass threshold = 0.1
        v = Variant(
            subsystem_name="gear", variant_index=0,
            scores={},  # no scores
            measured={"mass": 0.2},  # exceeds threshold
        )
        result = filter_feasible([v], spec)
        self.assertEqual(len(result), 0)
        self.assertFalse(v.feasible)

    def test_measured_mass_within_threshold(self) -> None:
        spec = _make_spec()  # mass threshold = 0.1
        v = Variant(
            subsystem_name="gear", variant_index=0,
            scores={},
            measured={"mass": 0.05},
        )
        result = filter_feasible([v], spec)
        self.assertEqual(len(result), 1)


class TestScorePartialSumsMass(unittest.TestCase):
    """Phase 3c: additive objectives (mass) summed not averaged."""

    def test_mass_is_summed(self) -> None:
        from orchestrator.sbce import _score_partial
        spec = MasterSpec(objectives=[
            Objective(name="mass", direction="minimize", unit="kg", weight=1.0),
        ])
        v1 = Variant(subsystem_name="a", variant_index=0, scores={"mass": 0.03})
        v2 = Variant(subsystem_name="b", variant_index=0, scores={"mass": 0.04})
        candidate = AssemblyCandidate(variants={"a": v1, "b": v2})
        score = _score_partial(candidate, spec)
        # mass is minimize → score = -(0.03 + 0.04) * 1.0 = -0.07
        self.assertAlmostEqual(score, -0.07)

    def test_non_additive_is_averaged(self) -> None:
        from orchestrator.sbce import _score_partial
        spec = MasterSpec(objectives=[
            Objective(name="strength", direction="maximize", unit="MPa", weight=1.0),
        ])
        v1 = Variant(subsystem_name="a", variant_index=0, scores={"strength": 100})
        v2 = Variant(subsystem_name="b", variant_index=0, scores={"strength": 200})
        candidate = AssemblyCandidate(variants={"a": v1, "b": v2})
        score = _score_partial(candidate, spec)
        # strength is maximize, not additive → avg = 150
        self.assertAlmostEqual(score, 150.0)

    def test_measured_fallback_in_scoring(self) -> None:
        from orchestrator.sbce import _score_partial
        spec = MasterSpec(objectives=[
            Objective(name="mass", direction="minimize", unit="kg", weight=1.0),
        ])
        v1 = Variant(subsystem_name="a", variant_index=0, measured={"mass": 0.05})
        candidate = AssemblyCandidate(variants={"a": v1})
        score = _score_partial(candidate, spec)
        self.assertAlmostEqual(score, -0.05)


if __name__ == "__main__":
    unittest.main()
