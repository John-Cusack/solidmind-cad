"""Data models for the design brief pipeline.

DesignBrief — the core artifact.  PartEntry and InterfaceEntry track
individual parts and their connections within an assembly design.
All frozen dataclasses with __slots__ for consistency with the codebase.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any


@dataclass(frozen=True, slots=True)
class PartEntry:
    """A single part within a design brief.

    ``kind`` is 'custom' (designed in CAD) or 'purchased' (off-the-shelf
    component whose specs constrain the custom parts around it).
    """
    name: str
    kind: str = "custom"        # custom | purchased
    quantity: int = 1
    specs: dict[str, Any] = dc_field(default_factory=dict)
    status: str = "pending"     # pending | building | built
    body_label: str = ""        # set after cad.new_body creates it

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "quantity": self.quantity,
            "specs": dict(self.specs),
            "status": self.status,
            "body_label": self.body_label,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PartEntry:
        return cls(
            name=d["name"],
            kind=d.get("kind", "custom"),
            quantity=d.get("quantity", 1),
            specs=dict(d.get("specs", {})),
            status=d.get("status", "pending"),
            body_label=d.get("body_label", ""),
        )


@dataclass(frozen=True, slots=True)
class InterfaceEntry:
    """A connection between two parts.

    Tracks which port on part_a connects to which port on part_b,
    and the physical spec of the connection (bolt pattern, press fit, etc.).
    """
    part_a: str
    port_a: str
    part_b: str
    port_b: str
    spec: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_a": self.part_a,
            "port_a": self.port_a,
            "part_b": self.part_b,
            "port_b": self.port_b,
            "spec": dict(self.spec),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InterfaceEntry:
        return cls(
            part_a=d["part_a"],
            port_a=d.get("port_a", ""),
            part_b=d["part_b"],
            port_b=d.get("port_b", ""),
            spec=dict(d.get("spec", {})),
        )


@dataclass(frozen=True, slots=True)
class DesignBrief:
    """A design brief — structured parameters the user approves before building.

    ``parameters`` is an open dict — the LLM can store whatever
    parameters it extracts from user specs, research, or conversation.
    ``parts`` and ``interfaces`` track the assembly decomposition.
    """
    brief_id: str
    name: str
    parameters: dict[str, Any] = dc_field(default_factory=dict)
    status: str = "intent"         # intent | sizing | layout | approved | building | done
    research_notes: str = ""
    parts: list[PartEntry] = dc_field(default_factory=list)
    interfaces: list[InterfaceEntry] = dc_field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "name": self.name,
            "parameters": dict(self.parameters),
            "status": self.status,
            "research_notes": self.research_notes,
            "parts": [p.to_dict() for p in self.parts],
            "interfaces": [i.to_dict() for i in self.interfaces],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DesignBrief:
        return cls(
            brief_id=d["brief_id"],
            name=d.get("name", "Untitled"),
            parameters=dict(d.get("parameters", {})),
            status=d.get("status", "draft"),
            research_notes=d.get("research_notes", ""),
            parts=[PartEntry.from_dict(p) for p in d.get("parts", [])],
            interfaces=[InterfaceEntry.from_dict(i) for i in d.get("interfaces", [])],
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

    def get_part(self, name: str) -> PartEntry | None:
        """Find a part by name (case-sensitive)."""
        for p in self.parts:
            if p.name == name:
                return p
        return None

    def get_interfaces_for(self, part_name: str) -> list[InterfaceEntry]:
        """Return all interfaces involving a given part."""
        return [
            i for i in self.interfaces
            if i.part_a == part_name or i.part_b == part_name
        ]
