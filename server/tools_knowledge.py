"""knowledge.* MCP tools — semantic search, document extraction, and ingestion.

Three interaction modes:
  A. Extract — parse a file and return text immediately (no indexing).
  B. Ingest  — extract + chunk + embed + store (synchronous, in-process).
  C. Search  — hybrid search (vector + FTS) across all ingested documents.

Falls back to local ``me_knowledge/notes/`` listing when LanceDB is unavailable.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from server.knowledge_store import get_knowledge_store

logger = logging.getLogger("solidmind.tools_knowledge")

_NOTES_DIR = Path(__file__).resolve().parent.parent / "me_knowledge" / "notes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_note_listing() -> list[str]:
    """List markdown files in me_knowledge/notes/ as a fallback."""
    if not _NOTES_DIR.is_dir():
        return []
    return sorted(p.name for p in _NOTES_DIR.glob("*.md"))


# ---------------------------------------------------------------------------
# Mode A — Extract (synchronous)
# ---------------------------------------------------------------------------

def knowledge_extract(*, file_path: str) -> dict[str, Any]:
    """Parse a file via Docling and return extracted text. No indexing."""
    store = get_knowledge_store()
    if store is None:
        return {
            "ok": False,
            "error": {
                "code": "store_unavailable",
                "message": (
                    "Knowledge store is not available (lancedb/docling not installed). "
                    "Read the file directly instead."
                ),
            },
        }

    p = Path(file_path).expanduser().resolve()
    if not p.is_file():
        return {
            "ok": False,
            "error": {"code": "file_not_found", "message": f"File not found: {p}"},
        }

    try:
        result = store.extract_file(p)
        return {
            "ok": True,
            "filename": result.filename,
            "pages": result.pages,
            "text": result.text,
            "metadata": result.metadata,
        }
    except Exception as e:
        logger.warning("knowledge.extract failed for %s: %s", p, e)
        return {
            "ok": False,
            "error": {"code": "extract_error", "message": str(e)},
        }


# ---------------------------------------------------------------------------
# Mode B — Ingest (synchronous — no polling needed)
# ---------------------------------------------------------------------------

def knowledge_ingest(
    *,
    path: str,
    extensions: list[str] | None = None,
) -> dict[str, Any]:
    """Ingest a file or directory into the knowledge store.

    Ingestion is synchronous and in-process — no task_id polling needed.
    """
    store = get_knowledge_store()
    if store is None:
        return {
            "ok": False,
            "error": {
                "code": "store_unavailable",
                "message": "Knowledge store is not available (lancedb not installed).",
            },
        }

    p = Path(path).expanduser().resolve()
    exts = tuple(extensions) if extensions else (".pdf", ".docx", ".md")

    if p.is_file():
        try:
            result = store.ingest_file(p)
            return {
                "ok": True,
                "mode": "single",
                "task_id": result.task_id,
                "filename": result.filename,
                "status": result.status,
                "chunks": result.chunks,
            }
        except Exception as e:
            return {"ok": False, "error": {"code": "ingest_error", "message": str(e)}}

    if p.is_dir():
        try:
            results = store.ingest_directory(p, extensions=exts)
            return {
                "ok": True,
                "mode": "directory",
                "files_submitted": len(results),
                "tasks": [
                    {
                        "task_id": r.task_id,
                        "filename": r.filename,
                        "status": r.status,
                        "chunks": r.chunks,
                    }
                    for r in results
                ],
            }
        except Exception as e:
            return {"ok": False, "error": {"code": "ingest_error", "message": str(e)}}

    return {
        "ok": False,
        "error": {"code": "path_not_found", "message": f"Path not found: {p}"},
    }


def knowledge_ingest_status(
    *,
    task_ids: list[str] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Return ingestion status — always 'complete' since ingestion is synchronous."""
    ids = task_ids or ([task_id] if task_id else [])
    if not ids:
        return {
            "ok": False,
            "error": {"code": "missing_param", "message": "Provide task_id or task_ids."},
        }
    return {
        "ok": True,
        "statuses": [
            {"task_id": tid, "status": "complete"}
            for tid in ids
        ],
    }


# ---------------------------------------------------------------------------
# Mode C — Search
# ---------------------------------------------------------------------------

def knowledge_search(
    *,
    query: str,
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hybrid search across all ingested documents.

    Falls back to listing local ``me_knowledge/notes/`` files when the
    knowledge store is unavailable.
    """
    store = get_knowledge_store()
    if store is None:
        local_notes = _local_note_listing()
        return {
            "ok": True,
            "source": "local_fallback",
            "message": (
                "Knowledge store is not available. Listing local research notes "
                "instead — use Read tool to access them."
            ),
            "local_notes": local_notes,
            "results": [],
        }

    try:
        results = store.search(query, top_k=top_k, filters=filters)
        return {
            "ok": True,
            "source": "lancedb",
            "query": query,
            "result_count": len(results),
            "results": [
                {
                    "content": r.content,
                    "source": r.source,
                    "score": r.score,
                    "metadata": r.metadata,
                }
                for r in results
            ],
        }
    except Exception as e:
        logger.warning("knowledge.search failed, falling back to local: %s", e)
        local_notes = _local_note_listing()
        return {
            "ok": True,
            "source": "local_fallback",
            "message": f"Knowledge search failed ({e}). Listing local notes instead.",
            "local_notes": local_notes,
            "results": [],
        }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def knowledge_status() -> dict[str, Any]:
    """Check knowledge store health, document count, index info."""
    store = get_knowledge_store()
    if store is None:
        local_notes = _local_note_listing()
        return {
            "ok": True,
            "store_available": False,
            "message": "Knowledge store is not available. Local notes are available.",
            "local_note_count": len(local_notes),
        }

    store_status = store.status()
    return {
        "ok": True,
        "store_available": store_status.get("available", False),
        "db_path": store_status.get("db_path", ""),
        "document_count": store_status.get("document_count", 0),
        "chunk_count": store_status.get("chunk_count", 0),
        "local_note_count": len(_local_note_listing()),
    }
