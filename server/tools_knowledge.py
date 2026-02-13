"""knowledge.* MCP tools — semantic search, document extraction, and ingestion.

Three interaction modes:
  A. Extract — parse a file and return text immediately (no indexing).
  B. Ingest  — submit file(s) for async indexing; poll with ingest_status.
  C. Search  — semantic search across all ingested documents.

Falls back to local ``me_knowledge/notes/`` listing when OpenRAG is unavailable.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from server.openrag_client import get_openrag_client

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
    client = get_openrag_client()
    if client is None:
        return {
            "ok": False,
            "error": {
                "code": "openrag_unavailable",
                "message": (
                    "OpenRAG is not configured. Set OPENRAG_URL to enable "
                    "document extraction, or read the file directly."
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
        result = client.extract_file(p)
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
# Mode B — Ingest (async submit + poll)
# ---------------------------------------------------------------------------

def knowledge_ingest(
    *,
    path: str,
    extensions: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a file or directory for ingestion.

    Single files return a ``task_id``. Directories are walked recursively
    for matching files — returns a list of ``task_id``s.
    """
    client = get_openrag_client()
    if client is None:
        return {
            "ok": False,
            "error": {
                "code": "openrag_unavailable",
                "message": "OpenRAG is not configured. Set OPENRAG_URL to enable ingestion.",
            },
        }

    p = Path(path).expanduser().resolve()
    exts = tuple(extensions) if extensions else (".pdf", ".docx", ".md")

    if p.is_file():
        try:
            result = client.ingest_file(p)
            return {
                "ok": True,
                "mode": "single",
                "task_id": result.task_id,
                "filename": result.filename,
                "status": result.status,
            }
        except Exception as e:
            return {"ok": False, "error": {"code": "ingest_error", "message": str(e)}}

    if p.is_dir():
        try:
            results = client.ingest_directory(p, extensions=exts)
            return {
                "ok": True,
                "mode": "directory",
                "files_submitted": len(results),
                "tasks": [
                    {
                        "task_id": r.task_id,
                        "filename": r.filename,
                        "status": r.status,
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
    """Poll ingestion status for one or more task IDs."""
    client = get_openrag_client()
    if client is None:
        return {
            "ok": False,
            "error": {
                "code": "openrag_unavailable",
                "message": "OpenRAG is not configured.",
            },
        }

    ids = task_ids or ([task_id] if task_id else [])
    if not ids:
        return {
            "ok": False,
            "error": {"code": "missing_param", "message": "Provide task_id or task_ids."},
        }

    statuses: list[dict[str, Any]] = []
    for tid in ids:
        try:
            statuses.append(client.ingest_status(tid))
        except Exception as e:
            statuses.append({"task_id": tid, "status": "error", "message": str(e)})

    return {"ok": True, "statuses": statuses}


# ---------------------------------------------------------------------------
# Mode C — Search
# ---------------------------------------------------------------------------

def knowledge_search(
    *,
    query: str,
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Semantic search across all ingested documents.

    Falls back to listing local ``me_knowledge/notes/`` files when OpenRAG
    is unavailable — the LLM can then Read the files directly.
    """
    client = get_openrag_client()
    if client is None:
        local_notes = _local_note_listing()
        return {
            "ok": True,
            "source": "local_fallback",
            "message": (
                "OpenRAG is not configured. Listing local research notes "
                "instead — use Read tool to access them."
            ),
            "local_notes": local_notes,
            "results": [],
        }

    try:
        results = client.search(query, top_k=top_k, filters=filters)
        return {
            "ok": True,
            "source": "openrag",
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
            "message": f"OpenRAG search failed ({e}). Listing local notes instead.",
            "local_notes": local_notes,
            "results": [],
        }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def knowledge_status() -> dict[str, Any]:
    """Check OpenRAG health, document count, index info."""
    client = get_openrag_client()
    if client is None:
        local_notes = _local_note_listing()
        return {
            "ok": True,
            "openrag_available": False,
            "message": "OpenRAG is not configured. Local notes are available.",
            "local_note_count": len(local_notes),
        }

    healthy = client.health_check()
    if not healthy:
        return {
            "ok": True,
            "openrag_available": False,
            "message": "OpenRAG is configured but not responding.",
            "local_note_count": len(_local_note_listing()),
        }

    try:
        docs = client.list_documents()
        doc_count = len(docs)
    except Exception:
        doc_count = -1

    return {
        "ok": True,
        "openrag_available": True,
        "document_count": doc_count,
        "local_note_count": len(_local_note_listing()),
    }
