"""Layered binding checks and application for design graph parameters.

Layer 1 (pre-materialization, HARD FAIL): a binding whose path does not parse,
does not resolve in the params doc, is not declared as a ``ParamSpec`` in the
structure doc, or whose value type mismatches the declaration aborts the
evaluation before anything is materialized.

Layer 2 (post-materialization, FLAG) lives in the evaluator: a binding that
was applied but left the materialized fingerprint unchanged is a legitimate
zero-sensitivity signal, reported as a flag on the result — never a failure.

Bindings address the ``{"value": ..., "unit": ...}`` leaf object and replace
only its ``value`` field; units are immutable under binding.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from server import json_pointer
from server.dg_models import StructureDoc

BINDING_PATH_UNKNOWN = "BINDING_PATH_UNKNOWN"
BINDING_PATH_UNDECLARED = "BINDING_PATH_UNDECLARED"
BINDING_TYPE_MISMATCH = "BINDING_TYPE_MISMATCH"


class BindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Binding:
    path: str
    value: float | int | str | bool

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Binding:
        return cls(path=data["path"], value=data["value"])


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False  # bool is an int subclass; never accept it for numerics
    if kind == "float":
        return isinstance(value, (int, float))
    if kind == "int":
        return isinstance(value, int)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    return False


def validate_bindings(
    structure: dict[str, Any], params: dict[str, Any], bindings: Sequence[Binding]
) -> None:
    """Layer-1 check; raises BindingError on the first violation."""
    doc = StructureDoc.from_dict(structure)
    for binding in bindings:
        try:
            leaf = json_pointer.get(params, binding.path)
        except json_pointer.JsonPointerError as e:
            raise BindingError(
                BINDING_PATH_UNKNOWN,
                f"Binding path {binding.path!r} does not resolve in the params doc: {e}",
            ) from e
        if not isinstance(leaf, dict) or "value" not in leaf:
            raise BindingError(
                BINDING_PATH_UNKNOWN,
                f"Binding path {binding.path!r} does not address a value leaf",
            )
        spec = doc.spec_for(binding.path)
        if spec is None:
            raise BindingError(
                BINDING_PATH_UNDECLARED,
                f"Binding path {binding.path!r} is not declared in the structure doc",
            )
        if not _type_ok(spec.kind, binding.value):
            raise BindingError(
                BINDING_TYPE_MISMATCH,
                f"Binding {binding.path!r} expects {spec.kind}, got {type(binding.value).__name__}",
            )


def apply_bindings(params: dict[str, Any], bindings: Sequence[Binding]) -> dict[str, Any]:
    """Return a deep copy of the params doc with binding values applied.

    Callers run ``validate_bindings`` first; this still raises on unresolvable
    paths but performs no declaration or type checks.
    """
    bound = copy.deepcopy(params)
    for binding in bindings:
        leaf = json_pointer.get(bound, binding.path)
        if not isinstance(leaf, dict) or "value" not in leaf:
            raise BindingError(
                BINDING_PATH_UNKNOWN,
                f"Binding path {binding.path!r} does not address a value leaf",
            )
        leaf["value"] = binding.value
    return bound


def domain_hits(structure: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate every declared validity domain against the current values.

    Hits are recorded on evaluation results (tranche 1 records, does not
    enforce — enforcement is a later tranche per the architecture build order).
    """
    doc = StructureDoc.from_dict(structure)
    hits: list[dict[str, Any]] = []
    for spec in doc.param_specs:
        if spec.domain is None:
            continue
        try:
            leaf = json_pointer.get(params, spec.path)
        except json_pointer.JsonPointerError:
            continue
        if not isinstance(leaf, dict) or "value" not in leaf:
            continue
        violation = spec.domain.check(leaf["value"])
        if violation is not None:
            hits.append(
                {
                    "path": spec.path,
                    "value": leaf["value"],
                    "kind": violation,
                    "domain": spec.domain.to_dict(),
                }
            )
    return hits
