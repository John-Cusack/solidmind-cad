from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from server.paths import data_path


@dataclass(frozen=True, slots=True)
class Resource:
    uri: str
    path: Path
    name: str
    mime_type: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "mimeType": self.mime_type,
            "description": self.description,
        }


@lru_cache(maxsize=1)
def _registry() -> dict[str, Resource]:
    items = [
        # -- ME pattern library --
        Resource(
            uri="resource://me_patterns/index.yml",
            path=data_path("me_patterns", "index.yml"),
            name="ME patterns index",
            mime_type="text/yaml",
            description="Index of all mechanical engineering design patterns.",
        ),
        Resource(
            uri="resource://me_patterns/brackets/mounting_bracket.yml",
            path=data_path("me_patterns", "brackets", "mounting_bracket.yml"),
            name="Mounting bracket pattern",
            mime_type="text/yaml",
            description="Design pattern for wall/panel-mounted brackets with fastener holes.",
        ),
        Resource(
            uri="resource://me_patterns/brackets/l_bracket.yml",
            path=data_path("me_patterns", "brackets", "l_bracket.yml"),
            name="L-bracket pattern",
            mime_type="text/yaml",
            description="Design pattern for right-angle L-brackets.",
        ),
        Resource(
            uri="resource://me_patterns/enclosures/rectangular_box.yml",
            path=data_path("me_patterns", "enclosures", "rectangular_box.yml"),
            name="Rectangular box enclosure pattern",
            mime_type="text/yaml",
            description="Design pattern for rectangular electronics/sensor enclosures.",
        ),
        Resource(
            uri="resource://me_patterns/fastening/simple_gear.yml",
            path=data_path("me_patterns", "fastening", "simple_gear.yml"),
            name="Simple spur gear pattern",
            mime_type="text/yaml",
            description="Design pattern for involute spur gears.",
        ),
        Resource(
            uri="resource://me_patterns/guides/design_for_cnc.yml",
            path=data_path("me_patterns", "guides", "design_for_cnc.yml"),
            name="Design for CNC guide",
            mime_type="text/yaml",
            description="CNC machining design guidelines and constraints.",
        ),
        Resource(
            uri="resource://me_patterns/guides/design_for_fdm.yml",
            path=data_path("me_patterns", "guides", "design_for_fdm.yml"),
            name="Design for FDM guide",
            mime_type="text/yaml",
            description="FDM 3D printing design guidelines and constraints.",
        ),
        # -- ME design intelligence registry --
        Resource(
            uri="resource://me_knowledge/index.yml",
            path=data_path("me_knowledge", "index.yml"),
            name="ME knowledge index",
            mime_type="text/yaml",
            description="Index of ME domain tags, archetypes, templates, and source policies.",
        ),
        Resource(
            uri="resource://me_knowledge/domain_tags.yml",
            path=data_path("me_knowledge", "domain_tags.yml"),
            name="ME domain tags registry",
            mime_type="text/yaml",
            description="Controlled vocabulary for ME request routing, validation, and retrieval triggers.",
        ),
        Resource(
            uri="resource://me_knowledge/archetypes/turbocharger_turbine_wheel_v1.yml",
            path=data_path("me_knowledge", "archetypes", "turbocharger_turbine_wheel_v1.yml"),
            name="Turbocharger turbine wheel archetype card",
            mime_type="text/yaml",
            description="Archetype card for radial turbocharger turbine wheel v1.",
        ),
        Resource(
            uri="resource://me_knowledge/constraint_templates/turbocharger_turbine_wheel_v1.yml",
            path=data_path("me_knowledge", "constraint_templates", "turbocharger_turbine_wheel_v1.yml"),
            name="Turbocharger turbine wheel constraint template",
            mime_type="text/yaml",
            description="Constraint sheet template for radial turbocharger turbine wheel v1.",
        ),
        Resource(
            uri="resource://me_knowledge/standards_sources.yml",
            path=data_path("me_knowledge", "standards_sources.yml"),
            name="ME standards and sources policy",
            mime_type="text/yaml",
            description="Authority ranking and provenance policy for engineering sources.",
        ),
    ]
    return {r.uri: r for r in items}


def list_resources() -> list[dict[str, Any]]:
    return [r.to_dict() for r in _registry().values()]


def read_resource(uri: str) -> dict[str, Any]:
    reg = _registry()
    r = reg.get(uri)
    if r is None:
        raise KeyError(f"Unknown resource uri: {uri}")
    text = r.path.read_text(encoding="utf-8")
    return {"uri": uri, "mimeType": r.mime_type, "text": text}
