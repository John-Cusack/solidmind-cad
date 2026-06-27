from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.feature_support import (
    BackendCapabilities,
    GeometryCapabilities,
    load_geometry_capabilities,
)


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    strategy_name: str
    target_features: list[str]
    score: float
    rationale: str
    confidence: float = 1.0
    fallback_chain: list[str] = field(default_factory=list)
    tie_break_key: float = 0.0


@dataclass(frozen=True, slots=True)
class RankedStrategies:
    primary: StrategyCandidate
    alternatives: list[StrategyCandidate]
    metadata: dict[str, Any] = field(default_factory=dict)


class StrategyPlanner:
    def __init__(self, capabilities: GeometryCapabilities | None = None) -> None:
        self._capabilities = capabilities or load_geometry_capabilities()
        self._tie_break_counter: float = 0.0

    def _get_next_tie_break(self) -> float:
        self._tie_break_counter += 1.0
        return self._tie_break_counter

    def select_strategy(
        self,
        gir: dict[str, Any],
        backend: str = "freecad",
    ) -> RankedStrategies:
        backend_caps = self._capabilities.backends.get(backend)
        if not backend_caps:
            return self._fallback_to_basic(gir, backend)

        features = gir.get("features", [])
        feature_types = [f.get("type") for f in features]

        candidates = self._enumerate_candidates(feature_types, features, backend_caps)

        scored = [(c, self._score_candidate(c, backend_caps)) for c in candidates]

        ranked = sorted(scored, key=lambda x: (x[1], x[0].tie_break_key), reverse=True)

        if not ranked:
            return self._fallback_to_basic(gir, backend)

        primary = ranked[0][0]
        alternatives = [c for c, _ in ranked[1:]]

        return RankedStrategies(
            primary=primary,
            alternatives=alternatives,
            metadata={"backend": backend, "candidates_evaluated": len(candidates)},
        )

    def _enumerate_candidates(
        self,
        feature_types: list[str],
        features: list[dict[str, Any]],
        backend_caps: BackendCapabilities,
    ) -> list[StrategyCandidate]:
        candidates: list[StrategyCandidate] = []

        has_extrude = "extrude_intent" in feature_types
        has_revolve = "revolve_intent" in feature_types
        has_pattern = "pattern_intent" in feature_types

        prism_score = 0.0
        revolve_score = 0.0
        hybrid_score = 0.0

        if has_extrude:
            extrude_count = sum(
                1 for f in features if f.get("type") == "extrude_intent"
            )
            prism_score += extrude_count * 10
            hybrid_score += extrude_count * 5

        if has_revolve:
            revolve_count = sum(
                1 for f in features if f.get("type") == "revolve_intent"
            )
            revolve_score += revolve_count * 15
            hybrid_score += revolve_count * 5

        if has_pattern:
            pattern_count = sum(
                1 for f in features if f.get("type") == "pattern_intent"
            )
            prism_score += pattern_count * 5
            revolve_score += pattern_count * 3

        has_pad = backend_caps.operations.get("pad")
        has_revolution = backend_caps.operations.get("revolve")
        backend_caps.operations.get("pocket")

        if has_pad and has_pad.status == "Yes" and prism_score > 0:
            candidates.append(
                StrategyCandidate(
                    strategy_name="prism_driven",
                    target_features=["pad", "pocket", "fillet", "chamfer"],
                    score=prism_score,
                    rationale="Extrude-dominated geometry, prism-driven strategy optimal",
                    confidence=0.85,
                    fallback_chain=["basic_box", "primitive_only"],
                    tie_break_key=self._get_next_tie_break(),
                )
            )

        if has_revolution and has_revolution.status == "Yes" and revolve_score > 0:
            candidates.append(
                StrategyCandidate(
                    strategy_name="revolve_driven",
                    target_features=["revolve", "fillet", "chamfer"],
                    score=revolve_score,
                    rationale="Revolution-dominated geometry, revolve-driven strategy optimal",
                    confidence=0.9,
                    fallback_chain=["prism_driven", "basic_box"],
                    tie_break_key=self._get_next_tie_break(),
                )
            )

        if hybrid_score > 0 and prism_score > 0 and revolve_score > 0:
            candidates.append(
                StrategyCandidate(
                    strategy_name="hybrid",
                    target_features=["pad", "revolve", "pocket", "fillet", "chamfer"],
                    score=hybrid_score * 1.2,
                    rationale="Mixed extrude/revolve geometry, hybrid strategy required",
                    confidence=0.75,
                    fallback_chain=["prism_driven", "revolve_driven"],
                    tie_break_key=self._get_next_tie_break(),
                )
            )

        if not candidates:
            candidates.append(
                StrategyCandidate(
                    strategy_name="basic_box",
                    target_features=["primitive"],
                    score=5.0,
                    rationale="No clear dominant pattern, using basic box strategy",
                    confidence=0.6,
                    tie_break_key=self._get_next_tie_break(),
                )
            )

        return candidates

    def _score_candidate(
        self,
        candidate: StrategyCandidate,
        backend_caps: BackendCapabilities,
    ) -> float:
        score = candidate.score * candidate.confidence

        capability_score = 0.0
        supported_ops = 0
        total_ops = len(candidate.target_features)

        for op_name in candidate.target_features:
            op_cap = backend_caps.operations.get(op_name)
            if op_cap:
                if op_cap.status == "Yes" and op_cap.stability == "stable":
                    supported_ops += 1
                    capability_score += 1.0
                elif op_cap.status == "Yes" and op_cap.stability == "beta":
                    supported_ops += 1
                    capability_score += 0.7
                elif op_cap.status == "Partial":
                    capability_score += 0.5

        if total_ops > 0:
            capability_score = (capability_score / total_ops) * 10

        score += capability_score

        if backend_caps.reference_behavior.rebinding_quality == "high":
            score *= 1.1

        return score

    def _fallback_to_basic(self, gir: dict[str, Any], backend: str) -> RankedStrategies:
        return RankedStrategies(
            primary=StrategyCandidate(
                strategy_name="basic_box",
                target_features=["primitive"],
                score=1.0,
                rationale=f"Backend {backend} not fully supported, falling back to basic box",
                confidence=0.5,
                tie_break_key=self._get_next_tie_break(),
            ),
            alternatives=[],
            metadata={"fallback": True, "backend": backend},
        )
