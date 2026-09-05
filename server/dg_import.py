"""One-way importer: ``design.brief/v1`` → design graph revision.

Maps the committed brief fixture (and any brief with the same schema) into the
structure/params document pair, declaring a ``ParamSpec`` for every bindable
leaf. The live ``design.*`` store is deliberately not imported — this reads
brief JSON files only.

Two parameters are synthesized for the foam-dart latch because the brief does
not carry them but the analytic stress model requires them; each is marked
with an explicit ``synthesized:`` provenance:

- ``/parts/latch_sear/specs/root_mm`` = 1.0 (run.py ``LATCH_V1`` baseline)
- ``/scenario_defaults/latch_impact_factor`` = 2.0 (run.py ``LATCH_IMPACT_FACTOR``)

Importing the same brief file twice yields the identical revision id.

CLI: ``python -m server.dg_import <brief.json> [--root PATH]``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from server.dg_models import (
    PARAMS_SCHEMA,
    Component,
    InterfaceDecl,
    ParamSpec,
    StructureDoc,
    ValidityDomain,
)
from server.dg_store import commit_revision

BRIEF_SCHEMA = "design.brief/v1"
BRIEF_PROVENANCE = BRIEF_SCHEMA

# Suffix → unit. Order matters: longer/more specific suffixes first.
_UNIT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_n_per_m", "N/m"),
    ("_mm", "mm"),
    ("_deg", "deg"),
    ("_kg", "kg"),
    ("_ft", "ft"),
    ("_g", "g"),
    ("_m", "m"),
)


class BriefImportError(ValueError):
    pass


def _unit_for(key: str) -> str:
    for suffix, unit in _UNIT_SUFFIXES:
        if key.endswith(suffix):
            return unit
    return "1"


def _kind_for(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    raise BriefImportError(f"Unsupported brief value type: {type(value).__name__}")


def _escape_token(token: str) -> str:
    # RFC 6901 escaping for a single path token.
    return token.replace("~", "~0").replace("/", "~1")


class _DocBuilder:
    """Accumulates params-doc leaves and their ParamSpec declarations."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {"schema": PARAMS_SCHEMA}
        self.specs: list[ParamSpec] = []

    def add_leaf(
        self,
        tokens: list[str],
        value: Any,
        *,
        provenance: str = BRIEF_PROVENANCE,
        domain: ValidityDomain | None = None,
    ) -> None:
        if value is None:
            return  # nothing to bind; skip nulls entirely
        kind = _kind_for(value)
        unit = _unit_for(tokens[-1]) if kind in ("int", "float", "array") else ""
        path = "/" + "/".join(_escape_token(t) for t in tokens)
        cursor = self.params
        for token in tokens[:-1]:
            cursor = cursor.setdefault(token, {})
        cursor[tokens[-1]] = {"value": value, "unit": unit}
        self.specs.append(
            ParamSpec(path=path, unit=unit, kind=kind, domain=domain, provenance=provenance)
        )

    def add_raw(self, tokens: list[str], value: Any) -> None:
        """Store a nested value with no ParamSpec (not bindable)."""
        cursor = self.params
        for token in tokens[:-1]:
            cursor = cursor.setdefault(token, {})
        cursor[tokens[-1]] = value


def import_brief(brief: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure mapping: brief dict → (structure_doc_dict, params_doc_dict)."""
    if brief.get("schema") != BRIEF_SCHEMA:
        raise BriefImportError(f"Expected schema {BRIEF_SCHEMA!r}, got {brief.get('schema')!r}")

    builder = _DocBuilder()
    parameters = brief.get("parameters", {})

    if isinstance(parameters.get("intent"), str):
        builder.add_leaf(["intent"], parameters["intent"])

    # Constraints and physical defaults.
    for group in ("constraints", "physical_defaults"):
        for key, value in parameters.get(group, {}).items():
            domain = None
            if key == "spring_k_n_per_m":
                domain = ValidityDomain(min_value=0.0)
            builder.add_leaf([group, key], value, domain=domain)

    # Layout: numeric/string leaves become params; nested dicts stay raw and
    # z_layers additionally lands in structure.frames.
    frames: dict[str, Any] = {}
    for key, value in parameters.get("layout", {}).items():
        if isinstance(value, dict):
            builder.add_raw(["layout", key], value)
            frames[key] = value
        else:
            builder.add_leaf(["layout", key], value)

    # Parts → components + spec leaves.
    components: list[Component] = []
    part_names: set[str] = set()
    ports_by_part: dict[str, list[str]] = {}
    for iface in brief.get("interfaces", []):
        ports_by_part.setdefault(iface["part_a"], []).append(iface["port_a"])
        ports_by_part.setdefault(iface["part_b"], []).append(iface["port_b"])

    for part in brief.get("parts", []):
        name = part["name"]
        part_names.add(name)
        ports = tuple(sorted(set(ports_by_part.get(name, []))))
        components.append(
            Component(
                id=name,
                kind=part.get("kind", "custom"),
                quantity=part.get("quantity", 1),
                ports=ports,
            )
        )
        for key, value in part.get("specs", {}).items():
            builder.add_leaf(["parts", name, "specs", key], value)

    # Synthesized latch parameters (fixture-specific; see module docstring).
    latch_specs = next(
        (p.get("specs", {}) for p in brief.get("parts", []) if p["name"] == "latch_sear"), None
    )
    latch_root_mm: float | None = None
    if latch_specs is not None and "root_mm" not in latch_specs:
        latch_root_mm = 1.0
        builder.add_leaf(
            ["parts", "latch_sear", "specs", "root_mm"],
            latch_root_mm,
            provenance="synthesized:run.py:LATCH_V1",
        )
    if latch_specs is not None:
        builder.add_leaf(
            ["scenario_defaults", "latch_impact_factor"],
            2.0,
            provenance="synthesized:run.py:LATCH_IMPACT_FACTOR",
        )

    # Interfaces → declarations + spec leaves.
    interfaces: list[InterfaceDecl] = []
    for iface in brief.get("interfaces", []):
        iface_id = f"{iface['part_a']}.{iface['port_a']}__{iface['part_b']}.{iface['port_b']}"
        interfaces.append(
            InterfaceDecl(
                id=iface_id,
                a=(iface["part_a"], iface["port_a"]),
                b=(iface["part_b"], iface["port_b"]),
                kind=str(iface.get("spec", {}).get("type", "")),
            )
        )
        for key, value in iface.get("spec", {}).items():
            builder.add_leaf(["interfaces", iface_id, key], value)

    # Fillet domain is bounded by the (possibly synthesized) root thickness.
    if latch_root_mm is not None:
        builder.specs = [
            ParamSpec(
                path=s.path,
                unit=s.unit,
                kind=s.kind,
                domain=ValidityDomain(min_value=0.0, max_value=latch_root_mm),
                provenance=s.provenance,
            )
            if s.path == "/parts/latch_sear/specs/fillet_mm"
            else s
            for s in builder.specs
        ]

    materials: dict[str, str] = {}
    constraints = parameters.get("constraints", {})
    if isinstance(constraints.get("material_default"), str):
        materials["default"] = constraints["material_default"]
    if isinstance(constraints.get("material_option"), str):
        materials["option"] = constraints["material_option"]

    structure = StructureDoc(
        name=brief.get("name", ""),
        components=tuple(components),
        interfaces=tuple(interfaces),
        frames=frames,
        materials=materials,
        param_specs=tuple(builder.specs),
    )
    return structure.to_dict(), builder.params


def import_brief_file(
    path: Path, *, root: Path | None = None, meta: dict[str, Any] | None = None
) -> str:
    """Import a brief JSON file and commit it as a parentless revision."""
    brief = json.loads(Path(path).read_text())
    structure, params = import_brief(brief)
    ref_meta = {"source": Path(path).name, "brief_name": brief.get("name", "")}
    if meta:
        ref_meta.update(meta)
    return commit_revision(structure, params, parent=None, meta=ref_meta, root=root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a design.brief/v1 JSON as a revision")
    parser.add_argument("brief", type=Path)
    parser.add_argument("--root", type=Path, default=None, help="artifact store root override")
    args = parser.parse_args(argv)
    try:
        revision_id = import_brief_file(args.brief, root=args.root)
    except (OSError, json.JSONDecodeError, BriefImportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(revision_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
