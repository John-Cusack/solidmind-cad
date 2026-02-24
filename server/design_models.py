"""Data models for the design brief pipeline.

DesignBrief — the core artifact.  Frozen dataclass with __slots__
for consistency with the rest of the codebase.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any


@dataclass(frozen=True, slots=True)
class DesignBrief:
    """A design brief — structured parameters the user approves before building.

    ``parameters`` is an open dict — the LLM can store whatever
    parameters it extracts from user specs, research, or conversation.
    """
    brief_id: str
    name: str
    parameters: dict[str, Any] = dc_field(default_factory=dict)
    status: str = "draft"          # draft | proposed | approved | building | done
    research_notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "name": self.name,
            "parameters": dict(self.parameters),
            "status": self.status,
            "research_notes": self.research_notes,
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
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )
