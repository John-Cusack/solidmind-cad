"""In-process knowledge store backed by LanceDB + Docling.

Zero-infrastructure replacement for the former OpenRAG Docker stack:
- **LanceDB** for hybrid search (Tantivy FTS + vector) in-process
- **Docling** (pip) for PDF/DOCX extraction
- **Ollama** or **sentence-transformers** for embeddings

Storage lives at ``me_knowledge/lancedb/`` (git-ignorable).

Module-level singleton via ``get_knowledge_store()`` / ``reset_knowledge_store()``,
matching the pattern used by ``freecad_client.py``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import textwrap
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable

# Load .env from project root if present (python-dotenv is a transitive dep)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

logger = logging.getLogger("solidmind.knowledge_store")

_DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / "me_knowledge" / "lancedb"
)
_TABLE_NAME = "documents"

# Chunking defaults
_CHUNK_SIZE = 500  # target tokens (approx chars / 4)
_CHUNK_OVERLAP = 50


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
    """Result from ingesting a file (synchronous)."""
    task_id: str
    filename: str
    status: str  # "complete" or "failed"
    chunks: int = 0


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _make_embedding_fn() -> Any:
    """Pick embedding function based on environment.

    Preference order:
    1. Ollama (if OLLAMA_URL is set) — GPU-accelerated
    2. sentence-transformers — CPU fallback
    """
    try:
        from lancedb.embeddings import get_registry  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("lancedb not installed — knowledge store unavailable")
        return None

    ollama_url = os.environ.get("OLLAMA_URL", "").strip()
    if ollama_url:
        model_name = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
        try:
            ollama = get_registry().get("ollama").create(
                name=model_name,
                host=ollama_url,
            )
            logger.info("Using Ollama embeddings (%s) at %s", model_name, ollama_url)
            return ollama
        except Exception as e:
            logger.warning("Ollama embeddings failed (%s), falling back to sentence-transformers", e)

    # Fallback: sentence-transformers (GPU if available, else CPU)
    model_name = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    device = os.environ.get("EMBEDDING_DEVICE", "").strip()
    if not device:
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    try:
        st = get_registry().get("sentence-transformers").create(
            name=model_name,
            device=device,
            trust_remote_code=True,
        )
        logger.info("Using sentence-transformers embeddings (%s) on %s", model_name, device)
        return st
    except Exception as e:
        logger.warning("sentence-transformers embeddings failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^#{1,3}\s+", re.MULTILINE)


def _chunk_text(text: str, source: str) -> list[dict[str, Any]]:
    """Split text into chunks for embedding.

    Strategy:
    1. Split by markdown headers (## / ###) first
    2. If sections are too long, split by paragraph then by character count
    """
    # Split on markdown headers, keeping the header with its section
    sections = _HEADER_RE.split(text)
    # Re-attach headers
    header_matches = list(_HEADER_RE.finditer(text))
    parts: list[str] = []
    if sections and sections[0].strip():
        parts.append(sections[0].strip())
    for i, match in enumerate(header_matches):
        idx = i + 1
        if idx < len(sections) and sections[idx].strip():
            parts.append(match.group() + sections[idx].strip())

    if not parts:
        parts = [text]

    # Further split large sections
    chunks: list[dict[str, Any]] = []
    chunk_idx = 0
    max_chars = _CHUNK_SIZE * 4  # rough token-to-char ratio
    overlap_chars = _CHUNK_OVERLAP * 4

    for part in parts:
        if len(part) <= max_chars:
            chunks.append({
                "id": f"{source}:{chunk_idx}",
                "text": part,
                "source": source,
                "chunk_index": chunk_idx,
                "metadata": "{}",
            })
            chunk_idx += 1
        else:
            # Split by paragraphs first, then by size
            paragraphs = part.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) > max_chars and current:
                    chunks.append({
                        "id": f"{source}:{chunk_idx}",
                        "text": current.strip(),
                        "source": source,
                        "chunk_index": chunk_idx,
                        "metadata": "{}",
                    })
                    chunk_idx += 1
                    # Keep overlap
                    current = current[-overlap_chars:] + "\n\n" + para if overlap_chars else para
                else:
                    current = current + "\n\n" + para if current else para

            if current.strip():
                chunks.append({
                    "id": f"{source}:{chunk_idx}",
                    "text": current.strip(),
                    "source": source,
                    "chunk_index": chunk_idx,
                    "metadata": "{}",
                })
                chunk_idx += 1

    return chunks


# ---------------------------------------------------------------------------
# Docling extraction
# ---------------------------------------------------------------------------

def _extract_with_docling(file_path: Path) -> ExtractResult:
    """Extract text from a file using Docling (in-process)."""
    from docling.document_converter import DocumentConverter  # type: ignore[import-untyped]

    converter = DocumentConverter()
    result = converter.convert(str(file_path))
    md_text = result.document.export_to_markdown()
    # Count pages if available
    num_pages = 0
    if hasattr(result.document, "pages"):
        num_pages = len(result.document.pages)
    elif hasattr(result, "pages"):
        num_pages = len(result.pages)

    return ExtractResult(
        text=md_text,
        filename=file_path.name,
        pages=num_pages,
        metadata={},
    )


# ---------------------------------------------------------------------------
# KnowledgeStore
# ---------------------------------------------------------------------------

class KnowledgeStore:
    """In-process knowledge store backed by LanceDB."""

    def __init__(self, db_path: str, embedding_fn: Any) -> None:
        import lancedb  # type: ignore[import-untyped]

        self._db_path = db_path
        self._embedding_fn = embedding_fn
        self._db = lancedb.connect(db_path)
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create the documents table if it doesn't exist."""
        import pyarrow as pa  # type: ignore[import-untyped]

        existing = self._db.table_names()
        if _TABLE_NAME not in existing:
            # Create empty table with schema
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("source", pa.string()),
                pa.field("chunk_index", pa.int32()),
                pa.field("metadata", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._get_ndims())),
            ])
            self._db.create_table(_TABLE_NAME, schema=schema)
            logger.info("Created documents table at %s", self._db_path)

    def _get_ndims(self) -> int:
        """Get embedding dimensionality."""
        if self._embedding_fn is None:
            return 384  # default for all-MiniLM-L6-v2
        try:
            return self._embedding_fn.ndims()
        except Exception:
            return 384

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        if self._embedding_fn is None:
            # Return zero vectors as fallback
            ndims = self._get_ndims()
            return [[0.0] * ndims for _ in texts]
        try:
            return self._embedding_fn.compute_source_embeddings(texts)
        except Exception:
            try:
                return self._embedding_fn.compute_query_embedding(texts)
            except Exception:
                ndims = self._get_ndims()
                return [[0.0] * ndims for _ in texts]

    def _embed_query(self, text: str) -> list[float]:
        """Generate embedding for a single query."""
        if self._embedding_fn is None:
            return [0.0] * self._get_ndims()
        try:
            # LanceDB embedding functions use compute_query_embeddings (plural)
            result = self._embedding_fn.compute_query_embeddings(text)
            if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
                return result[0]
            return result
        except AttributeError:
            try:
                result = self._embedding_fn.compute_query_embedding(text)
                if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
                    return result[0]
                return result
            except Exception:
                return [0.0] * self._get_ndims()
        except Exception:
            return [0.0] * self._get_ndims()

    # -- Extract (Mode A) --------------------------------------------------

    def extract_file(self, file_path: Path) -> ExtractResult:
        """Parse a file via Docling and return extracted text. No indexing."""
        return _extract_with_docling(file_path)

    # -- Ingest (Mode B) ---------------------------------------------------

    def ingest_file(self, file_path: Path) -> IngestResult:
        """Extract, chunk, embed, and store a single file."""
        try:
            ext = file_path.suffix.lower()
            if ext == ".md":
                text = file_path.read_text(encoding="utf-8")
            elif ext in (".pdf", ".docx", ".doc", ".pptx", ".html", ".htm"):
                result = _extract_with_docling(file_path)
                text = result.text
            else:
                text = file_path.read_text(encoding="utf-8")

            return self.ingest_text(text, file_path.name)
        except Exception as e:
            logger.warning("Failed to ingest %s: %s", file_path, e)
            return IngestResult(
                task_id="",
                filename=file_path.name,
                status="failed",
                chunks=0,
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
                results.append(self.ingest_file(p))
        return results

    def ingest_text(self, text: str, source: str) -> IngestResult:
        """Chunk, embed, and store raw text content."""
        try:
            # Delete existing chunks for this source
            self._delete_by_source(source)

            chunks = _chunk_text(text, source)
            if not chunks:
                return IngestResult(
                    task_id=hashlib.md5(source.encode()).hexdigest(),
                    filename=source,
                    status="complete",
                    chunks=0,
                )

            # Generate embeddings
            texts = [c["text"] for c in chunks]
            vectors = self._embed(texts)
            for chunk, vec in zip(chunks, vectors):
                chunk["vector"] = vec

            table = self._db.open_table(_TABLE_NAME)
            table.add(chunks)

            # Create FTS index if not present (idempotent)
            try:
                table.create_fts_index("text", replace=True)
            except Exception:
                pass  # FTS index already exists or not supported

            task_id = hashlib.md5(source.encode()).hexdigest()
            return IngestResult(
                task_id=task_id,
                filename=source,
                status="complete",
                chunks=len(chunks),
            )
        except Exception as e:
            logger.warning("Failed to ingest text from %s: %s", source, e)
            return IngestResult(
                task_id="",
                filename=source,
                status="failed",
                chunks=0,
            )

    # -- Search (Mode C) ---------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Hybrid search (vector + FTS) across all ingested documents."""
        try:
            table = self._db.open_table(_TABLE_NAME)
        except Exception:
            return []

        results: list[dict[str, Any]] = []
        try:
            # Try FTS first (keyword search — fast and relevant)
            results = (
                table.search(query, query_type="fts")
                .limit(top_k)
                .to_list()
            )
        except Exception:
            pass

        if not results:
            try:
                # Fall back to pure vector search
                query_vec = self._embed_query(query)
                results = (
                    table.search(query_vec)
                    .limit(top_k)
                    .to_list()
                )
            except Exception:
                return []

        # Merge: if FTS returned results but we have vectors, also do
        # a vector search and combine (manual hybrid).
        if results and self._embedding_fn is not None:
            try:
                query_vec = self._embed_query(query)
                vec_results = (
                    table.search(query_vec)
                    .limit(top_k)
                    .to_list()
                )
                # Add vector results not already in FTS results
                seen_ids = {r.get("id") for r in results}
                for vr in vec_results:
                    if vr.get("id") not in seen_ids:
                        results.append(vr)
                        seen_ids.add(vr.get("id"))
                # Trim to top_k
                results = results[:top_k]
            except Exception:
                pass

        return [
            SearchResult(
                content=r.get("text", ""),
                source=r.get("source", ""),
                score=float(r.get("_relevance_score", r.get("_score", r.get("_distance", 0.0)))),
                metadata={},
            )
            for r in results
        ]

    # -- Status / management -----------------------------------------------

    def status(self) -> dict[str, Any]:
        """Report store health and stats."""
        try:
            table = self._db.open_table(_TABLE_NAME)
            row_count = table.count_rows()
            # Count unique sources using Arrow (no pandas needed)
            sources = len(set(
                r["source"] for r in table.search().select(["source"]).limit(10000).to_list()
            )) if row_count > 0 else 0
            return {
                "available": True,
                "db_path": self._db_path,
                "chunk_count": row_count,
                "document_count": sources,
            }
        except Exception:
            return {
                "available": True,
                "db_path": self._db_path,
                "chunk_count": 0,
                "document_count": 0,
            }

    def list_documents(self) -> list[dict[str, Any]]:
        """List all unique source documents."""
        try:
            table = self._db.open_table(_TABLE_NAME)
            rows = table.search().select(["source", "id"]).limit(100000).to_list()
            if not rows:
                return []
            # Group by source
            from collections import Counter
            source_counts = Counter(r["source"] for r in rows)
            return [
                {"source": src, "chunk_count": count}
                for src, count in sorted(source_counts.items())
            ]
        except Exception:
            return []

    def delete_document(self, source: str) -> None:
        """Delete all chunks for a given source document."""
        self._delete_by_source(source)

    def _delete_by_source(self, source: str) -> None:
        """Remove all rows matching a source name."""
        try:
            table = self._db.open_table(_TABLE_NAME)
            table.delete(f"source = '{source}'")
        except Exception:
            pass

    def close(self) -> None:
        """No-op for API compatibility."""
        pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store: KnowledgeStore | None = None
_configured: bool | None = None  # None = not yet checked


def get_knowledge_store() -> KnowledgeStore | None:
    """Get or create the global KnowledgeStore.

    Returns ``None`` if required dependencies (lancedb) are not installed,
    signalling that callers should fall back to local notes listing.
    """
    global _store, _configured

    if _configured is False:
        return None
    if _store is not None:
        return _store

    try:
        import lancedb  # type: ignore[import-untyped] # noqa: F401
    except ImportError:
        _configured = False
        logger.info("lancedb not installed — knowledge tools will fall back to local notes")
        return None

    db_path = os.environ.get("KNOWLEDGE_DB_PATH", "").strip() or _DEFAULT_DB_PATH
    embedding_fn = _make_embedding_fn()
    if embedding_fn is None:
        logger.warning("No embedding function available — knowledge store will use zero vectors")

    try:
        _store = KnowledgeStore(db_path=db_path, embedding_fn=embedding_fn)
        _configured = True
        logger.info("Knowledge store initialized at %s", db_path)
        return _store
    except Exception as e:
        _configured = False
        logger.warning("Failed to initialize knowledge store: %s", e)
        return None


def reset_knowledge_store() -> None:
    """Close and reset the global store (for testing)."""
    global _store, _configured
    if _store is not None:
        _store.close()
        _store = None
    _configured = None
