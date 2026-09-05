"""Design graph revision store — commit/load/list over the artifact store.

A committed revision is three objects in the CAS (structure doc, params doc,
revision manifest) plus a ref ``refs/dg_revisions/<revision_id>`` whose record
carries the mutable metadata (created_at, free-text meta) that must stay out
of hashed payloads. The revision id IS the manifest's content hash, so the
ref name and the hash it points to coincide — the ref exists for enumeration
and metadata, not indirection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server import artifact_store as cas
from server.dg_models import PARAMS_SCHEMA, STRUCTURE_SCHEMA, Revision, compute_revision_id
from server.env_identity import environment_identity_hash

REVISION_NAMESPACE = "dg_revisions"


class DesignGraphError(ValueError):
    pass


def commit_revision(
    structure: dict[str, Any],
    params: dict[str, Any],
    *,
    parent: str | None = None,
    meta: dict[str, Any] | None = None,
    root: Path | None = None,
) -> str:
    """Commit a (structure, params) pair; return the revision id."""
    if structure.get("schema") != STRUCTURE_SCHEMA:
        raise DesignGraphError(f"Structure doc must have schema {STRUCTURE_SCHEMA!r}")
    if params.get("schema") != PARAMS_SCHEMA:
        raise DesignGraphError(f"Params doc must have schema {PARAMS_SCHEMA!r}")
    if parent is not None and not cas.exists(parent, root=root):
        raise DesignGraphError(f"Parent revision not found: {parent}")

    structure_hash = cas.put_json(structure, root=root)
    params_hash = cas.put_json(params, root=root)
    revision = Revision(structure_hash=structure_hash, params_hash=params_hash, parent=parent)
    revision_id = cas.put_json(revision.to_dict(), root=root)

    inputs = {"structure": structure_hash, "params": params_hash}
    if parent is not None:
        inputs["parent"] = parent
    cas.record_lineage(
        revision_id,
        op="commit_revision",
        inputs=inputs,
        env_identity_hash=environment_identity_hash(),
        root=root,
    )
    cas.set_ref(REVISION_NAMESPACE, revision_id, revision_id, meta=meta, root=root)
    return revision_id


def load_revision(
    revision_id: str, *, root: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any], Revision]:
    """Load (structure_doc, params_doc, manifest) for a revision id or ref name."""
    resolved = cas.resolve(revision_id, namespace=REVISION_NAMESPACE, root=root)
    manifest_doc = cas.get_json(resolved, root=root)
    try:
        revision = Revision.from_dict(manifest_doc)
    except (KeyError, TypeError) as e:
        raise DesignGraphError(f"Object {resolved} is not a revision manifest") from e
    if compute_revision_id(revision) != resolved:
        raise DesignGraphError(f"Object {resolved} is not a revision manifest")
    structure = cas.get_json(revision.structure_hash, root=root)
    params = cas.get_json(revision.params_hash, root=root)
    return structure, params, revision


def list_revisions(*, root: Path | None = None) -> list[dict[str, Any]]:
    """All committed revisions: [{revision_id, parent, created_at, meta}]."""
    out: list[dict[str, Any]] = []
    for record in cas.list_refs(REVISION_NAMESPACE, root=root):
        manifest = cas.get_json(record["hash"], root=root)
        out.append(
            {
                "revision_id": record["name"],
                "parent": manifest.get("parent"),
                "created_at": record.get("created_at", ""),
                "meta": record.get("meta", {}),
            }
        )
    return out
