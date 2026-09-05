"""Design graph document models.

A design graph revision is three content-addressed documents:

- **Structure doc** (``dg.structure/v1``) — the shape of the design: components,
  interfaces, frames, materials, and the declaration of every bindable
  parameter (``ParamSpec``). Never holds values.
- **Params doc** (``dg.params/v1``) — plain JSON values at stable RFC 6901
  paths. Leaf values are ``{"value": ..., "unit": ...}`` objects; bindings
  replace only the ``value`` field, so units are immutable under binding.
- **Revision manifest** (``dg.revision/v1``) — ``{structure_hash, params_hash,
  parent}``. The revision id is the sha256_jcs hash of the manifest.

Hashing is ``sha256(jcs.canonicalize(doc))`` with **no** float rounding —
the analytic tier's equality policy is bit-exact, and JCS float serialization
is already shortest-round-trip deterministic. Timestamps and free-text
metadata live in store ref records, never in hashed payloads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from server import jcs

STRUCTURE_SCHEMA = "dg.structure/v1"
PARAMS_SCHEMA = "dg.params/v1"
REVISION_SCHEMA = "dg.revision/v1"

ParamKind = Literal["float", "int", "string", "bool", "array"]


def hash_doc(doc: dict[str, Any]) -> str:
    """sha256 over the JCS canonical form of a document."""
    return hashlib.sha256(jcs.canonicalize(doc).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidityDomain:
    """The envelope within which a parameter's model means anything."""

    min_value: float | None = None
    max_value: float | None = None
    choices: tuple[str, ...] = ()
    note: str = ""

    def check(self, value: Any) -> str | None:
        """Return a violation kind, or None when the value is inside."""
        if self.choices:
            if value not in self.choices:
                return "not_in_choices"
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if self.min_value is not None and value < self.min_value:
            return "below_min"
        if self.max_value is not None and value > self.max_value:
            return "above_max"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_value": self.min_value,
            "max_value": self.max_value,
            "choices": list(self.choices),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidityDomain:
        return cls(
            min_value=data.get("min_value"),
            max_value=data.get("max_value"),
            choices=tuple(data.get("choices", ())),
            note=data.get("note", ""),
        )


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Declaration of one bindable parameter at a stable path."""

    path: str  # RFC 6901 pointer into the params doc
    unit: str  # "mm", "N/m", "1" (dimensionless), "" (non-numeric)
    kind: ParamKind = "float"
    domain: ValidityDomain | None = None
    provenance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "unit": self.unit,
            "kind": self.kind,
            "domain": self.domain.to_dict() if self.domain is not None else None,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParamSpec:
        domain = data.get("domain")
        return cls(
            path=data["path"],
            unit=data.get("unit", ""),
            kind=data.get("kind", "float"),
            domain=ValidityDomain.from_dict(domain) if domain is not None else None,
            provenance=data.get("provenance", ""),
        )


@dataclass(frozen=True, slots=True)
class Component:
    id: str  # stable id (brief part name for imported briefs)
    kind: str = "custom"  # custom | purchased
    quantity: int = 1
    ports: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "quantity": self.quantity,
            "ports": list(self.ports),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Component:
        return cls(
            id=data["id"],
            kind=data.get("kind", "custom"),
            quantity=data.get("quantity", 1),
            ports=tuple(data.get("ports", ())),
        )


@dataclass(frozen=True, slots=True)
class InterfaceDecl:
    id: str  # "<part_a>.<port_a>__<part_b>.<port_b>"
    a: tuple[str, str]  # (component_id, port)
    b: tuple[str, str]
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "a": list(self.a), "b": list(self.b), "kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InterfaceDecl:
        return cls(
            id=data["id"],
            a=(data["a"][0], data["a"][1]),
            b=(data["b"][0], data["b"][1]),
            kind=data.get("kind", ""),
        )


@dataclass(frozen=True, slots=True)
class StructureDoc:
    name: str
    components: tuple[Component, ...] = ()
    interfaces: tuple[InterfaceDecl, ...] = ()
    frames: dict[str, Any] = field(default_factory=dict)
    materials: dict[str, str] = field(default_factory=dict)
    param_specs: tuple[ParamSpec, ...] = ()
    schema: str = STRUCTURE_SCHEMA

    def spec_for(self, path: str) -> ParamSpec | None:
        for spec in self.param_specs:
            if spec.path == path:
                return spec
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "components": [c.to_dict() for c in self.components],
            "interfaces": [i.to_dict() for i in self.interfaces],
            "frames": self.frames,
            "materials": self.materials,
            "param_specs": [p.to_dict() for p in self.param_specs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructureDoc:
        return cls(
            name=data["name"],
            components=tuple(Component.from_dict(c) for c in data.get("components", ())),
            interfaces=tuple(InterfaceDecl.from_dict(i) for i in data.get("interfaces", ())),
            frames=data.get("frames", {}),
            materials=data.get("materials", {}),
            param_specs=tuple(ParamSpec.from_dict(p) for p in data.get("param_specs", ())),
            schema=data.get("schema", STRUCTURE_SCHEMA),
        )


@dataclass(frozen=True, slots=True)
class Revision:
    structure_hash: str
    params_hash: str
    parent: str | None = None
    schema: str = REVISION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "structure_hash": self.structure_hash,
            "params_hash": self.params_hash,
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Revision:
        return cls(
            structure_hash=data["structure_hash"],
            params_hash=data["params_hash"],
            parent=data.get("parent"),
            schema=data.get("schema", REVISION_SCHEMA),
        )


def compute_revision_id(revision: Revision) -> str:
    return hash_doc(revision.to_dict())
