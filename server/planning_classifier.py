from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_ARC_ORDER = [
    "prismatic",
    "revolved",
    "thin_wall",
    "loft_sweep",
    "multi_body",
    "organic",
]


@dataclass(frozen=True, slots=True)
class ClassifierResult:
    process: str
    archetype: str
    scores: dict[str, float]


def resolve_process(normalized_spec: dict[str, Any]) -> str:
    process = str(normalized_spec.get("process", "")).lower()
    if process in ("cnc", "fdm"):
        return process
    if process == "print_3d":
        return "fdm"
    return "cnc"


def classify_archetype(normalized_spec: dict[str, Any]) -> ClassifierResult:
    process = resolve_process(normalized_spec)

    geometry = normalized_spec.get("geometry", {})
    if not isinstance(geometry, dict):
        geometry = {}

    features = geometry.get("features", [])
    if not isinstance(features, list):
        features = []

    # deterministic score accumulator
    scores = {k: 0.0 for k in _ARC_ORDER}

    # baseline defaults
    scores["prismatic"] += 1.0

    # clues from explicit feature declarations
    for feat in features:
        if not isinstance(feat, dict):
            continue
        ftype = str(feat.get("type", "")).lower()
        if "revol" in ftype:
            scores["revolved"] += 3.0
        if "loft" in ftype or "sweep" in ftype:
            scores["loft_sweep"] += 3.0
        if "shell" in ftype or "thin" in ftype:
            scores["thin_wall"] += 2.0
        if "body" in ftype or "boolean" in ftype:
            scores["multi_body"] += 2.0

    # clues from geometry sections
    if geometry.get("hole_features"):
        scores["prismatic"] += 1.0
    if geometry.get("fillets"):
        scores["prismatic"] += 0.5
    if geometry.get("revolve_features"):
        scores["revolved"] += 2.0
    if geometry.get("loft_features") or geometry.get("sweep_features"):
        scores["loft_sweep"] += 2.0
    if geometry.get("shell"):
        scores["thin_wall"] += 2.0

    # process priors
    if process == "cnc":
        scores["prismatic"] += 0.75
        scores["revolved"] += 0.5
    elif process == "fdm":
        scores["thin_wall"] += 0.75
        scores["prismatic"] += 0.5

    # deterministic tie-break on fixed archetype order
    archetype = _ARC_ORDER[0]
    best = scores[archetype]
    for candidate in _ARC_ORDER[1:]:
        val = scores[candidate]
        if val > best:
            archetype = candidate
            best = val

    return ClassifierResult(process=process, archetype=archetype, scores=scores)
