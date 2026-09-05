"""MCP tools for the design graph — import briefs, inspect revisions.

Minimal observability surface for the content-addressed design-graph store:
the MCP client can create revision zero from a committed brief and inspect
what a revision contains. Search runs through ``study.*`` driver mode; the
evaluator is a CLI, not a tool.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from server.artifact_store import ArtifactError
from server.dg_import import BriefImportError, import_brief_file
from server.dg_store import DesignGraphError, list_revisions, load_revision

log = logging.getLogger("solidmind.tools_dgraph")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def dgraph_import_brief(brief_path: str) -> dict[str, Any]:
    """Import a design.brief/v1 JSON file as design-graph revision zero."""
    if _TOOL_LOG:
        log.info("CALL dgraph.import_brief path=%s", brief_path)
    t0 = time.monotonic()

    path = Path(brief_path)
    if not path.is_file():
        return _error_result("NOT_FOUND", f"Brief file not found: {brief_path}")
    try:
        revision_id = import_brief_file(path)
        structure, _params, _manifest = load_revision(revision_id)
    except (BriefImportError, json.JSONDecodeError) as exc:
        return _error_result("INVALID_INPUT", f"Not a valid design brief: {exc}")
    except (ArtifactError, DesignGraphError, OSError) as exc:
        return _error_result("STORE_ERROR", str(exc))

    if _TOOL_LOG:
        log.info("OK   dgraph.import_brief %.3fs rev=%s", time.monotonic() - t0, revision_id)
    return {
        "ok": True,
        "revision_id": revision_id,
        "name": structure.get("name", ""),
        "components": len(structure.get("components", ())),
        "interfaces": len(structure.get("interfaces", ())),
        "param_count": len(structure.get("param_specs", ())),
    }


def dgraph_get_revision(revision: str) -> dict[str, Any]:
    """Load a revision: manifest, structure summary, and the full params doc."""
    if _TOOL_LOG:
        log.info("CALL dgraph.get_revision rev=%s", revision)
    try:
        structure, params, manifest = load_revision(revision)
    except (ArtifactError, DesignGraphError) as exc:
        return _error_result("NOT_FOUND", str(exc))

    return {
        "ok": True,
        "revision_id": revision,
        "parent": manifest.parent,
        "name": structure.get("name", ""),
        "components": [c["id"] for c in structure.get("components", ())],
        "interfaces": [i["id"] for i in structure.get("interfaces", ())],
        "materials": structure.get("materials", {}),
        "param_specs": structure.get("param_specs", []),
        "params": params,
    }


def dgraph_list_revisions() -> dict[str, Any]:
    """List all committed design-graph revisions."""
    if _TOOL_LOG:
        log.info("CALL dgraph.list_revisions")
    try:
        revisions = list_revisions()
    except (ArtifactError, DesignGraphError) as exc:
        return _error_result("STORE_ERROR", str(exc))
    return {"ok": True, "revisions": revisions}
