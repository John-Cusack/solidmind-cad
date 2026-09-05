"""Evaluation request/result contracts (``eval.request/v1``, ``eval.result/v1``).

The result body is content-addressed, so it must stay reproducible: **no
timestamps or durations** live in it — timing belongs to job records and study
variants. The request hash is the evaluation cache key and includes the
environment identity and the identities of every model (stage) requested, so a
result computed under a different interpreter, package set, or physics version
is never replayed as if it were the same evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from server.dg_binding import Binding
from server.dg_models import hash_doc

REQUEST_SCHEMA = "eval.request/v1"
RESULT_SCHEMA = "eval.result/v1"


class FailureClass(str, Enum):
    BINDING_ERROR = "binding_error"
    REVISION_NOT_FOUND = "revision_not_found"
    INVALID_REQUEST = "invalid_request"
    MODEL_ERROR = "model_error"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class MetricValue:
    value: float
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetricValue:
        return cls(value=data["value"], unit=data.get("unit", "1"))


@dataclass(frozen=True, slots=True)
class EvalRequest:
    revision: str  # revision id or dg_revisions ref name
    bindings: tuple[Binding, ...] = ()
    scenario: str = "latch_hold"
    models: tuple[str, ...] = ("analytic_latch",)
    seed: int = 0
    # Model chain revision (server/model_registry.py). None = every catalogued
    # term enabled; a chain with terms ablated changes each stage's identity
    # and therefore the cache key.
    models_revision: str | None = None
    schema: str = REQUEST_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema": self.schema,
            "revision": self.revision,
            "bindings": [b.to_dict() for b in self.bindings],
            "scenario": self.scenario,
            "models": list(self.models),
            "seed": self.seed,
        }
        if self.models_revision is not None:
            doc["models_revision"] = self.models_revision
        return doc

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalRequest:
        return cls(
            revision=data["revision"],
            bindings=tuple(Binding.from_dict(b) for b in data.get("bindings", ())),
            scenario=data.get("scenario", "latch_hold"),
            models=tuple(data.get("models", ("analytic_latch",))),
            seed=int(data.get("seed", 0)),
            models_revision=data.get("models_revision"),
            schema=data.get("schema", REQUEST_SCHEMA),
        )

    def request_hash(
        self,
        *,
        resolved_revision: str,
        env_identity_hash: str,
        model_identities: dict[str, str],
    ) -> str:
        """Cache key for this evaluation under the current environment."""
        payload = {
            "schema": self.schema,
            "revision": resolved_revision,
            "bindings": [b.to_dict() for b in sorted(self.bindings, key=lambda b: b.path)],
            "scenario": self.scenario,
            "models": sorted(self.models),
            "seed": self.seed,
            "env_identity_hash": env_identity_hash,
            "model_identities": model_identities,
        }
        return hash_doc(payload)


@dataclass(slots=True)
class EvalResult:
    ok: bool
    revision: str
    request_hash: str
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    uncalibrated_models: list[str] = field(default_factory=list)
    applied_bindings: list[dict[str, Any]] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    seed: int = 0
    environment: dict[str, Any] = field(default_factory=dict)
    model_identity: dict[str, str] = field(default_factory=dict)
    validity_domain_hits: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] | None = None
    schema: str = RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "ok": self.ok,
            "revision": self.revision,
            "request_hash": self.request_hash,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "checks": self.checks,
            "uncalibrated_models": self.uncalibrated_models,
            "applied_bindings": self.applied_bindings,
            "artifact_hashes": self.artifact_hashes,
            "seed": self.seed,
            "environment": self.environment,
            "model_identity": self.model_identity,
            "validity_domain_hits": self.validity_domain_hits,
            "flags": self.flags,
            "failure": self.failure,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalResult:
        return cls(
            ok=data["ok"],
            revision=data["revision"],
            request_hash=data["request_hash"],
            metrics={k: MetricValue.from_dict(v) for k, v in data.get("metrics", {}).items()},
            checks=list(data.get("checks", ())),
            uncalibrated_models=list(data.get("uncalibrated_models", ())),
            applied_bindings=list(data.get("applied_bindings", ())),
            artifact_hashes=dict(data.get("artifact_hashes", {})),
            seed=int(data.get("seed", 0)),
            environment=dict(data.get("environment", {})),
            model_identity=dict(data.get("model_identity", {})),
            validity_domain_hits=list(data.get("validity_domain_hits", ())),
            flags=dict(data.get("flags", {})),
            failure=data.get("failure"),
            schema=data.get("schema", RESULT_SCHEMA),
        )
