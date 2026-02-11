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
        Resource(
            uri="resource://question_bank/cnc.yml",
            path=data_path("question_bank", "cnc.yml"),
            name="CNC question bank",
            mime_type="text/yaml",
            description="Question bank used for coverage scoring and next-question selection.",
        ),
        Resource(
            uri="resource://schemas/cnc.schema.json",
            path=data_path("schemas", "cnc.schema.json"),
            name="CNC JSON Schema",
            mime_type="application/json",
            description="Shape schema for CNC specs (draft and final).",
        ),
        Resource(
            uri="resource://examples/cnc/L1.json",
            path=data_path("examples", "cnc", "L1.json"),
            name="CNC L1 example",
            mime_type="application/json",
            description="Example finalized spec for CNC L1.",
        ),
        Resource(
            uri="resource://examples/cnc/L2.json",
            path=data_path("examples", "cnc", "L2.json"),
            name="CNC L2 example",
            mime_type="application/json",
            description="Example finalized spec for CNC L2.",
        ),
        Resource(
            uri="resource://examples/cnc/L3.json",
            path=data_path("examples", "cnc", "L3.json"),
            name="CNC L3 example",
            mime_type="application/json",
            description="Example finalized spec for CNC L3.",
        ),
        Resource(
            uri="resource://glossary.yml",
            path=data_path("question_bank", "glossary.yml"),
            name="Glossary",
            mime_type="text/yaml",
            description="Small glossary of manufacturing terms used by prompts and the interviewer.",
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

