"""Model registry — physics model classes and the chains that enable them.

Artifact #4 of the target architecture. A *stage* (``server/eval_stages.py``)
is built from named **terms**; a **model chain** document says which terms are
enabled for each stage. Chains are content-addressed like every other
artifact, so an evaluation run under an ablated chain can never be confused
with one run under the full model — the chain changes each stage's identity,
which is part of the evaluation cache key.

Two consumers:

- **Prescription** (``server/prescribe.py``): a model-registry patch enables a
  term. Per Addendum A.1 that patch is calibration-gated — a term whose
  ``calibrated`` flag is false enters with an empty validity domain and a
  standing measurement/calibration requirement, so proposed physics cannot be
  optimized against until it is anchored.
- **The seeded-defect benchmark** (``server/benchmark.py``): the
  *missing mechanism* defect class is seeded by ablating a term from an
  already-working chain, per Addendum A.2. ``hidden_catalog`` removes the
  ablated term from the registry the benchmark arms can see, so recovering it
  is an open-vocabulary proposal rather than spotting an unused entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server import artifact_store as cas
from server.dg_models import ValidityDomain

MODEL_CHAIN_SCHEMA = "dg.models/v1"
CHAIN_NAMESPACE = "model_chains"


class ModelRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelTerm:
    """One mechanism inside a stage's model chain."""

    id: str
    stage: str
    summary: str = ""
    calibrated: bool = True
    validity: ValidityDomain | None = None
    provenance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "summary": self.summary,
            "calibrated": self.calibrated,
            "validity": self.validity.to_dict() if self.validity is not None else None,
            "provenance": self.provenance,
        }


# The catalog of terms the platform knows how to evaluate. Terms marked
# calibrated=False carry no anchored validity envelope: they are literature
# parameterizations, usable for screening and explicitly not for sizing.
TERM_CATALOG: dict[str, dict[str, ModelTerm]] = {
    "analytic_latch": {
        "bending": ModelTerm(
            id="bending",
            stage="analytic_latch",
            summary="Nominal cantilever bending stress sigma = M c / I.",
            calibrated=True,
            provenance="server/screen_stress.py::beam_bending_stress_mpa",
        ),
        "stress_concentration": ModelTerm(
            id="stress_concentration",
            stage="analytic_latch",
            summary="Handbook Kt at the fillet/sharp root (Peterson-range tables).",
            calibrated=True,
            provenance="server/screen_stress.py::stress_concentration_factor",
        ),
    },
    "analytic_acoustic_bearing": {
        "crlb": ModelTerm(
            id="crlb",
            stage="analytic_acoustic_bearing",
            summary="Knapp-Carter delay variance (f^2 weighting, coherence term).",
            calibrated=True,
            provenance="Knapp & Carter 1976",
        ),
        "coherence_loss": ModelTerm(
            id="coherence_loss",
            stage="analytic_acoustic_bearing",
            summary="Turbulence-induced transverse coherence loss over the aperture.",
            calibrated=False,  # literature parameters; campaign-calibrated in tranche 3
            provenance="literature:kolmogorov-transverse-coherence",
        ),
        "anomaly": ModelTerm(
            id="anomaly",
            stage="analytic_acoustic_bearing",
            summary="Threshold/ambiguity blending — CRLB is a local bound only.",
            calibrated=False,
            provenance="literature:ziv-zakai-threshold",
        ),
    },
}


def catalog_for(stage: str) -> dict[str, ModelTerm]:
    if stage not in TERM_CATALOG:
        raise ModelRegistryError(f"Unknown stage {stage!r} in the model registry")
    return TERM_CATALOG[stage]


def hidden_catalog(hidden: dict[str, set[str]]) -> dict[str, dict[str, ModelTerm]]:
    """The catalog with some terms withheld (benchmark visibility control)."""
    out: dict[str, dict[str, ModelTerm]] = {}
    for stage, terms in TERM_CATALOG.items():
        withheld = hidden.get(stage, set())
        out[stage] = {tid: term for tid, term in terms.items() if tid not in withheld}
    return out


@dataclass(frozen=True, slots=True)
class ModelChain:
    """Which terms are enabled, per stage."""

    chains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    schema: str = MODEL_CHAIN_SCHEMA

    def terms_for(self, stage: str) -> frozenset[str] | None:
        entry = self.chains.get(stage)
        return None if entry is None else frozenset(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "chains": {stage: sorted(terms) for stage, terms in self.chains.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelChain:
        if data.get("schema") != MODEL_CHAIN_SCHEMA:
            raise ModelRegistryError(f"Model chain doc must have schema {MODEL_CHAIN_SCHEMA!r}")
        return cls(
            chains={stage: tuple(sorted(terms)) for stage, terms in data.get("chains", {}).items()}
        )


def full_chain(stages: list[str] | None = None) -> ModelChain:
    """Every catalogued term enabled, for the requested stages (default: all)."""
    names = stages if stages is not None else list(TERM_CATALOG)
    return ModelChain(chains={s: tuple(sorted(catalog_for(s))) for s in names})


def ablate(chain: ModelChain, stage: str, term: str) -> ModelChain:
    """A chain with one term removed — the seeded missing-mechanism defect."""
    enabled = chain.terms_for(stage)
    if enabled is None:
        raise ModelRegistryError(f"Chain does not configure stage {stage!r}")
    if term not in enabled:
        raise ModelRegistryError(f"Term {term!r} is not enabled for stage {stage!r}")
    chains = dict(chain.chains)
    chains[stage] = tuple(sorted(enabled - {term}))
    return ModelChain(chains=chains)


def enable(chain: ModelChain, stage: str, term: str) -> ModelChain:
    """A chain with one term added — the restoring patch."""
    if term not in catalog_for(stage):
        raise ModelRegistryError(f"Unknown term {term!r} for stage {stage!r}")
    enabled = chain.terms_for(stage) or frozenset()
    chains = dict(chain.chains)
    chains[stage] = tuple(sorted(enabled | {term}))
    return ModelChain(chains=chains)


def commit_chain(chain: ModelChain, *, root: Path | None = None) -> str:
    """Store a chain document; returns its content hash (the models revision)."""
    doc = chain.to_dict()
    for stage, terms in doc["chains"].items():
        known = catalog_for(stage)
        unknown = [t for t in terms if t not in known]
        if unknown:
            raise ModelRegistryError(f"Unknown term(s) {unknown} for stage {stage!r}")
    chain_hash = cas.put_json(doc, root=root)
    cas.set_ref(CHAIN_NAMESPACE, chain_hash, chain_hash, root=root)
    return chain_hash


def load_chain(models_revision: str, *, root: Path | None = None) -> ModelChain:
    try:
        resolved = cas.resolve(models_revision, namespace=CHAIN_NAMESPACE, root=root)
        doc = cas.get_json(resolved, root=root)
    except cas.ArtifactError as exc:
        raise ModelRegistryError(str(exc)) from exc
    return ModelChain.from_dict(doc)


def uncalibrated_terms(chain: ModelChain) -> list[ModelTerm]:
    """Enabled terms that carry no anchored validity envelope."""
    out: list[ModelTerm] = []
    for stage, terms in chain.chains.items():
        known = catalog_for(stage)
        out.extend(known[t] for t in terms if t in known and not known[t].calibrated)
    return out
