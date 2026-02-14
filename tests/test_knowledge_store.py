"""Tests for server.knowledge_store — mocked LanceDB, no real deps needed."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from server.knowledge_store import (
    ExtractResult,
    IngestResult,
    KnowledgeStore,
    SearchResult,
    _chunk_text,
    get_knowledge_store,
    reset_knowledge_store,
)


class TestChunking(unittest.TestCase):
    """Test the text chunking logic (pure Python, no deps)."""

    def test_short_text_single_chunk(self):
        chunks = _chunk_text("Hello world", "test.md")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["text"], "Hello world")
        self.assertEqual(chunks[0]["source"], "test.md")
        self.assertEqual(chunks[0]["chunk_index"], 0)

    def test_markdown_header_split(self):
        text = "Intro paragraph.\n\n## Section One\nContent one.\n\n## Section Two\nContent two."
        chunks = _chunk_text(text, "doc.md")
        self.assertGreaterEqual(len(chunks), 2)
        # First chunk should contain intro
        sources = {c["source"] for c in chunks}
        self.assertEqual(sources, {"doc.md"})

    def test_chunk_indices_sequential(self):
        text = "## A\nFirst.\n\n## B\nSecond.\n\n## C\nThird."
        chunks = _chunk_text(text, "multi.md")
        indices = [c["chunk_index"] for c in chunks]
        self.assertEqual(indices, list(range(len(chunks))))

    def test_chunk_ids_contain_source(self):
        chunks = _chunk_text("Some text here", "notes.md")
        for c in chunks:
            self.assertTrue(c["id"].startswith("notes.md:"))

    def test_empty_text(self):
        chunks = _chunk_text("", "empty.md")
        # Empty string should produce no chunks (or one empty chunk)
        self.assertLessEqual(len(chunks), 1)

    def test_large_text_splits(self):
        # Create text with headers to trigger splitting
        text = "\n\n## Section 1\n" + ("Word " * 800) + "\n\n## Section 2\n" + ("Word " * 800)
        chunks = _chunk_text(text, "big.md")
        self.assertGreater(len(chunks), 1)


class TestSearchResult(unittest.TestCase):
    def test_frozen(self):
        r = SearchResult(content="test", source="a.md", score=0.9)
        with self.assertRaises(AttributeError):
            r.content = "changed"  # type: ignore[misc]

    def test_fields(self):
        r = SearchResult(content="blade angle", source="nasa.pdf", score=0.92, metadata={"page": 5})
        self.assertEqual(r.content, "blade angle")
        self.assertEqual(r.source, "nasa.pdf")
        self.assertAlmostEqual(r.score, 0.92)
        self.assertEqual(r.metadata, {"page": 5})


class TestExtractResult(unittest.TestCase):
    def test_fields(self):
        r = ExtractResult(text="content", filename="doc.pdf", pages=10)
        self.assertEqual(r.pages, 10)


class TestIngestResult(unittest.TestCase):
    def test_fields(self):
        r = IngestResult(task_id="abc", filename="notes.md", status="complete", chunks=5)
        self.assertEqual(r.chunks, 5)
        self.assertEqual(r.status, "complete")


class TestKnowledgeStoreUnit(unittest.TestCase):
    """Test KnowledgeStore methods with a mocked LanceDB backend."""

    def _make_store(self) -> KnowledgeStore:
        """Create a KnowledgeStore with mocked internals."""
        store = KnowledgeStore.__new__(KnowledgeStore)
        store._db_path = "/tmp/test_lancedb"
        store._embedding_fn = None  # will use zero vectors
        store._db = MagicMock()
        return store

    def test_search_returns_results(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table

        # Mock the search chain
        mock_search = MagicMock()
        mock_table.search.return_value = mock_search
        mock_search.limit.return_value = mock_search
        mock_search.to_list.return_value = [
            {"text": "blade angle 60 deg", "source": "nasa.pdf", "_relevance_score": 0.92},
            {"text": "hub ratio 0.4", "source": "handbook.pdf", "_relevance_score": 0.85},
        ]

        results = store.search("turbine blade angle")
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], SearchResult)
        self.assertEqual(results[0].source, "nasa.pdf")

    def test_search_empty(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table
        mock_search = MagicMock()
        mock_table.search.return_value = mock_search
        mock_search.limit.return_value = mock_search
        mock_search.to_list.return_value = []

        results = store.search("nonexistent")
        self.assertEqual(results, [])

    def test_search_handles_error(self):
        store = self._make_store()
        store._db.open_table.side_effect = Exception("table not found")
        results = store.search("anything")
        self.assertEqual(results, [])

    def test_ingest_text(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table

        result = store.ingest_text("Some research content about turbines", "research.md")
        self.assertIsInstance(result, IngestResult)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.filename, "research.md")
        self.assertGreater(result.chunks, 0)

        # Verify table.add was called
        mock_table.add.assert_called_once()

    def test_ingest_file_md(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table

        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("# Research Notes\nSome content here.")
            f.flush()
            try:
                result = store.ingest_file(Path(f.name))
                self.assertEqual(result.status, "complete")
                self.assertGreater(result.chunks, 0)
            finally:
                os.unlink(f.name)

    def test_ingest_directory(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table

        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "a.md").write_text("note a")
            (p / "b.md").write_text("note b")
            (p / "c.txt").write_text("skip me")  # not in extensions
            results = store.ingest_directory(p)
            self.assertEqual(len(results), 2)  # only .md files

    def test_status(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table
        mock_table.count_rows.return_value = 10

        # Mock the search().select().limit().to_list() chain
        mock_search = MagicMock()
        mock_table.search.return_value = mock_search
        mock_search.select.return_value = mock_search
        mock_search.limit.return_value = mock_search
        mock_search.to_list.return_value = [
            {"source": "a.md"}, {"source": "a.md"}, {"source": "b.pdf"},
        ]

        status = store.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["chunk_count"], 10)
        self.assertEqual(status["document_count"], 2)

    def test_list_documents(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table

        mock_search = MagicMock()
        mock_table.search.return_value = mock_search
        mock_search.select.return_value = mock_search
        mock_search.limit.return_value = mock_search
        mock_search.to_list.return_value = [
            {"source": "a.md", "id": "a:0"},
            {"source": "a.md", "id": "a:1"},
            {"source": "b.pdf", "id": "b:0"},
        ]

        docs = store.list_documents()
        self.assertEqual(len(docs), 2)
        sources = {d["source"] for d in docs}
        self.assertEqual(sources, {"a.md", "b.pdf"})

    def test_delete_document(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table
        store.delete_document("old.md")
        mock_table.delete.assert_called_once_with("source = 'old.md'")

    def test_delete_document_escapes_single_quotes(self):
        store = self._make_store()
        mock_table = MagicMock()
        store._db.open_table.return_value = mock_table
        store.delete_document("o'reilly.md")
        mock_table.delete.assert_called_once_with("source = 'o''reilly.md'")


class TestSingleton(unittest.TestCase):
    def setUp(self):
        reset_knowledge_store()

    def tearDown(self):
        reset_knowledge_store()

    def test_returns_none_when_lancedb_missing(self):
        with patch.dict("sys.modules", {"lancedb": None}):
            reset_knowledge_store()
            import importlib
            # Simulate ImportError for lancedb
            with patch("builtins.__import__", side_effect=_import_blocker("lancedb")):
                reset_knowledge_store()
                store = get_knowledge_store()
                self.assertIsNone(store)

    def test_returns_store_when_available(self):
        mock_lancedb = MagicMock()
        mock_db = MagicMock()
        mock_db.table_names.return_value = ["documents"]
        mock_lancedb.connect.return_value = mock_db

        mock_pa = MagicMock()
        mock_st = MagicMock()
        mock_st.ndims.return_value = 384

        with patch.dict("sys.modules", {"lancedb": mock_lancedb, "pyarrow": mock_pa}), \
             patch("server.knowledge_store._make_embedding_fn", return_value=mock_st):
            reset_knowledge_store()
            store = get_knowledge_store()
            self.assertIsNotNone(store)
            # Second call returns same instance
            self.assertIs(get_knowledge_store(), store)


def _import_blocker(blocked_module: str):
    """Create an import side_effect that blocks a specific module."""
    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
    def _blocker(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"Mocked: {name} not installed")
        return _real_import(name, *args, **kwargs)
    return _blocker


if __name__ == "__main__":
    unittest.main()
