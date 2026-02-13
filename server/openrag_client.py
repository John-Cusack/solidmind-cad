"""Synchronous HTTP client for the OpenRAG knowledge backend.

Wraps OpenRAG's REST API with a module-level singleton (matching
freecad_client.py's pattern).  Returns ``None`` from
``get_openrag_client()`` when not configured — all callers handle
gracefully.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("solidmind.openrag_client")

DEFAULT_URL = "http://localhost:8080"
REQUEST_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SearchResult:
    """A single search hit from the knowledge base."""
    content: str
    source: str
    score: float
    metadata: dict[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """Parsed content returned from document extraction."""
    text: str
    filename: str
    pages: int
    metadata: dict[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Result from submitting a file for ingestion."""
    task_id: str
    filename: str
    status: str  # "submitted", "processing", "complete", "failed"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OpenRAGClient:
    """Synchronous HTTP client wrapping OpenRAG's REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        api_key: str = "",
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
        )

    # -- health --------------------------------------------------------------

    def health_check(self) -> bool:
        """Return True if OpenRAG is reachable and healthy."""
        try:
            resp = self._http.get("/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # -- search (Mode C) -----------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Semantic search across all ingested documents."""
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if filters:
            payload["filters"] = filters
        resp = self._http.post("/api/search", json=payload)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        return [
            SearchResult(
                content=r.get("content", ""),
                source=r.get("source", ""),
                score=float(r.get("score", 0.0)),
                metadata=r.get("metadata", {}),
            )
            for r in results
        ]

    # -- extract (Mode A) ----------------------------------------------------

    def extract_file(self, file_path: Path) -> ExtractResult:
        """Parse a file via Docling and return extracted text (no indexing)."""
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f)}
            resp = self._http.post("/api/extract", files=files)
        resp.raise_for_status()
        data = resp.json()
        return ExtractResult(
            text=data.get("text", ""),
            filename=data.get("filename", file_path.name),
            pages=int(data.get("pages", 0)),
            metadata=data.get("metadata", {}),
        )

    # -- ingest (Mode B) -----------------------------------------------------

    def ingest_file(self, file_path: Path) -> IngestResult:
        """Submit a single file for async ingestion. Returns task_id."""
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f)}
            resp = self._http.post("/api/ingest", files=files)
        resp.raise_for_status()
        data = resp.json()
        return IngestResult(
            task_id=data.get("task_id", ""),
            filename=data.get("filename", file_path.name),
            status=data.get("status", "submitted"),
        )

    def ingest_directory(
        self,
        dir_path: Path,
        extensions: tuple[str, ...] = (".pdf", ".docx", ".md"),
    ) -> list[IngestResult]:
        """Walk a directory recursively and ingest matching files."""
        results: list[IngestResult] = []
        for p in sorted(dir_path.rglob("*")):
            if p.is_file() and p.suffix.lower() in extensions:
                try:
                    results.append(self.ingest_file(p))
                except httpx.HTTPError as e:
                    logger.warning("Failed to ingest %s: %s", p, e)
                    results.append(IngestResult(
                        task_id="",
                        filename=p.name,
                        status="failed",
                    ))
        return results

    def ingest_text(self, text: str, filename: str) -> IngestResult:
        """Ingest raw text content under a given filename."""
        resp = self._http.post(
            "/api/ingest/text",
            json={"text": text, "filename": filename},
        )
        resp.raise_for_status()
        data = resp.json()
        return IngestResult(
            task_id=data.get("task_id", ""),
            filename=data.get("filename", filename),
            status=data.get("status", "submitted"),
        )

    def ingest_status(self, task_id: str) -> dict[str, Any]:
        """Poll the status of an async ingestion task."""
        resp = self._http.get(f"/api/ingest/status/{task_id}")
        resp.raise_for_status()
        return resp.json()

    # -- document management --------------------------------------------------

    def list_documents(self) -> list[dict[str, Any]]:
        """List all indexed documents."""
        resp = self._http.get("/api/documents")
        resp.raise_for_status()
        return resp.json().get("documents", [])

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        """Delete a document from the index."""
        resp = self._http.delete(f"/api/documents/{doc_id}")
        resp.raise_for_status()
        return resp.json()

    # -- cleanup --------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: OpenRAGClient | None = None
_configured: bool | None = None  # None = not yet checked


def get_openrag_client() -> OpenRAGClient | None:
    """Get or create the global OpenRAG client.

    Returns ``None`` if ``OPENRAG_URL`` is not set in the environment,
    signalling that OpenRAG is not configured and callers should fall back.
    """
    global _client, _configured

    if _configured is False:
        return None
    if _client is not None:
        return _client

    url = os.environ.get("OPENRAG_URL", "").strip()
    if not url:
        _configured = False
        logger.info("OPENRAG_URL not set — knowledge tools will fall back to local notes")
        return None

    api_key = os.environ.get("OPENRAG_API_KEY", "").strip()
    _client = OpenRAGClient(base_url=url, api_key=api_key)
    _configured = True
    logger.info("OpenRAG client configured at %s", url)
    return _client


def reset_openrag_client() -> None:
    """Close and reset the global client (for testing)."""
    global _client, _configured
    if _client is not None:
        _client.close()
        _client = None
    _configured = None
