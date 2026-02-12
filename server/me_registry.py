from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any

import yaml

from server.paths import data_path


class MERegistryError(ValueError):
    """Raised when ME registry files are malformed or missing required data."""


@lru_cache(maxsize=1)
def _load_index() -> dict[str, Any]:
    path = data_path("me_knowledge", "index.yml")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MERegistryError("me_knowledge/index.yml must parse to a mapping")
    return payload


@lru_cache(maxsize=1)
def _load_domain_tag_list() -> list[dict[str, Any]]:
    index = _load_index()
    registry = index.get("registry")
    if not isinstance(registry, dict):
        raise MERegistryError("me_knowledge/index.yml missing registry mapping")

    rel_path = registry.get("domain_tags")
    if not isinstance(rel_path, str):
        raise MERegistryError("registry.domain_tags must be a string path")

    path = data_path("me_knowledge", rel_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tags"), list):
        raise MERegistryError("domain_tags.yml must contain a tags list")

    tags: list[dict[str, Any]] = []
    for entry in payload["tags"]:
        if not isinstance(entry, dict):
            raise MERegistryError("Each domain tag entry must be a mapping")
        tag_id = entry.get("id")
        if not isinstance(tag_id, str) or not tag_id:
            raise MERegistryError("Each domain tag requires non-empty id")
        tags.append(entry)
    return sorted(tags, key=lambda t: str(t.get("id", "")))


@lru_cache(maxsize=1)
def _load_archetype_index() -> dict[str, str]:
    index = _load_index()
    registry = index.get("registry")
    if not isinstance(registry, dict):
        raise MERegistryError("me_knowledge/index.yml missing registry mapping")

    entries = registry.get("archetypes")
    if not isinstance(entries, list):
        raise MERegistryError("registry.archetypes must be a list")

    out: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise MERegistryError("Each archetype index entry must be a mapping")
        archetype_id = entry.get("id")
        rel_path = entry.get("path")
        if not isinstance(archetype_id, str) or not archetype_id:
            raise MERegistryError("Archetype index entry missing id")
        if not isinstance(rel_path, str) or not rel_path:
            raise MERegistryError(f"Archetype {archetype_id} missing path")
        out[archetype_id] = rel_path
    return out


@lru_cache(maxsize=1)
def _load_constraint_template_index() -> dict[str, str]:
    index = _load_index()
    registry = index.get("registry")
    if not isinstance(registry, dict):
        raise MERegistryError("me_knowledge/index.yml missing registry mapping")

    entries = registry.get("constraint_templates")
    if not isinstance(entries, list):
        raise MERegistryError("registry.constraint_templates must be a list")

    out: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise MERegistryError("Each template index entry must be a mapping")
        template_id = entry.get("id")
        rel_path = entry.get("path")
        if not isinstance(template_id, str) or not template_id:
            raise MERegistryError("Constraint template index entry missing id")
        if not isinstance(rel_path, str) or not rel_path:
            raise MERegistryError(f"Constraint template {template_id} missing path")
        out[template_id] = rel_path
    return out


@lru_cache(maxsize=8)
def _load_archetype_card_cached(archetype_id: str) -> dict[str, Any]:
    rel_path = _load_archetype_index().get(archetype_id)
    if rel_path is None:
        raise KeyError(f"Unknown archetype_id: {archetype_id}")
    path = data_path("me_knowledge", rel_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MERegistryError(f"Archetype file {rel_path} must parse to a mapping")

    file_archetype_id = payload.get("archetype_id")
    if file_archetype_id != archetype_id:
        raise MERegistryError(
            f"Archetype id mismatch in {rel_path}: expected {archetype_id!r}, got {file_archetype_id!r}"
        )
    return payload


@lru_cache(maxsize=8)
def _load_constraint_template_cached(template_id: str) -> dict[str, Any]:
    rel_path = _load_constraint_template_index().get(template_id)
    if rel_path is None:
        raise KeyError(f"Unknown constraint template id: {template_id}")
    path = data_path("me_knowledge", rel_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MERegistryError(f"Constraint template file {rel_path} must parse to a mapping")

    file_template_id = payload.get("template_id")
    if file_template_id != template_id:
        raise MERegistryError(
            f"Constraint template id mismatch in {rel_path}: expected {template_id!r}, got {file_template_id!r}"
        )
    return payload


@lru_cache(maxsize=1)
def _load_standards_sources_cached() -> dict[str, Any]:
    index = _load_index()
    registry = index.get("registry")
    if not isinstance(registry, dict):
        raise MERegistryError("me_knowledge/index.yml missing registry mapping")
    rel_path = registry.get("standards_sources")
    if not isinstance(rel_path, str):
        raise MERegistryError("registry.standards_sources must be a string path")

    path = data_path("me_knowledge", rel_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MERegistryError("standards_sources.yml must parse to a mapping")
    return payload


def list_domain_tags() -> list[dict[str, Any]]:
    """Return all domain tags sorted by id."""
    return deepcopy(_load_domain_tag_list())


def domain_tags_by_id() -> dict[str, dict[str, Any]]:
    """Return domain tags keyed by tag id."""
    return {str(t["id"]): t for t in list_domain_tags()}


def list_archetype_ids() -> list[str]:
    """Return known archetype ids sorted lexicographically."""
    return sorted(_load_archetype_index().keys())


def load_archetype_card(archetype_id: str) -> dict[str, Any]:
    """Load a single archetype card by id."""
    return deepcopy(_load_archetype_card_cached(archetype_id))


def load_constraint_template(template_id: str) -> dict[str, Any]:
    """Load a single constraint template by id."""
    return deepcopy(_load_constraint_template_cached(template_id))


def load_standards_sources() -> dict[str, Any]:
    """Load source authority and citation policy metadata."""
    return deepcopy(_load_standards_sources_cached())


def clear_caches() -> None:
    """Clear cached registry payloads (for tests)."""
    _load_index.cache_clear()
    _load_domain_tag_list.cache_clear()
    _load_archetype_index.cache_clear()
    _load_constraint_template_index.cache_clear()
    _load_archetype_card_cached.cache_clear()
    _load_constraint_template_cached.cache_clear()
    _load_standards_sources_cached.cache_clear()
