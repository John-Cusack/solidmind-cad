"""Tests for server.tools_knowledge — mocked KnowledgeStore."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.knowledge_store import (
    ExtractResult,
    IngestResult,
    SearchResult,
    reset_knowledge_store,
)
from server.tools_knowledge import (
    knowledge_extract,
    knowledge_ingest,
    knowledge_ingest_status,
    knowledge_search,
    knowledge_status,
)


def _mock_store() -> MagicMock:
    """Create a MagicMock posing as a KnowledgeStore."""
    m = MagicMock()
    return m


class TestKnowledgeExtract(unittest.TestCase):
    def setUp(self):
        reset_knowledge_store()

    def test_unavailable_returns_error(self):
        with patch("server.tools_knowledge.get_knowledge_store", return_value=None):
            result = knowledge_extract(file_path="/tmp/test.pdf")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "store_unavailable")

    def test_file_not_found(self):
        with patch("server.tools_knowledge.get_knowledge_store", return_value=_mock_store()):
            result = knowledge_extract(file_path="/nonexistent/path.pdf")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "file_not_found")

    def test_extract_success(self):
        mock = _mock_store()
        mock.extract_file.return_value = ExtractResult(
            text="Extracted content here",
            filename="test.pdf",
            pages=10,
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(b"fake pdf")
            f.flush()
            with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
                result = knowledge_extract(file_path=f.name)
                self.assertTrue(result["ok"])
                self.assertEqual(result["pages"], 10)
                self.assertIn("Extracted", result["text"])

    def test_extract_error(self):
        mock = _mock_store()
        mock.extract_file.side_effect = Exception("parse failed")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(b"fake pdf")
            f.flush()
            with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
                result = knowledge_extract(file_path=f.name)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "extract_error")


class TestKnowledgeIngest(unittest.TestCase):
    def setUp(self):
        reset_knowledge_store()

    def test_unavailable(self):
        with patch("server.tools_knowledge.get_knowledge_store", return_value=None):
            result = knowledge_ingest(path="/tmp/test.pdf")
            self.assertFalse(result["ok"])

    def test_single_file(self):
        mock = _mock_store()
        mock.ingest_file.return_value = IngestResult(
            task_id="t-123", filename="test.md", status="complete", chunks=3,
        )
        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# Notes")
            f.flush()
            with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
                result = knowledge_ingest(path=f.name)
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "single")
                self.assertEqual(result["task_id"], "t-123")
                self.assertEqual(result["chunks"], 3)

    def test_directory(self):
        mock = _mock_store()
        mock.ingest_directory.return_value = [
            IngestResult(task_id="t-1", filename="a.md", status="complete", chunks=2),
            IngestResult(task_id="t-2", filename="b.pdf", status="complete", chunks=5),
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
                result = knowledge_ingest(path=td)
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "directory")
                self.assertEqual(result["files_submitted"], 2)

    def test_path_not_found(self):
        mock = _mock_store()
        with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
            result = knowledge_ingest(path="/nonexistent/path")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "path_not_found")


class TestKnowledgeIngestStatus(unittest.TestCase):
    def test_single_task(self):
        result = knowledge_ingest_status(task_id="t-123")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["statuses"]), 1)
        self.assertEqual(result["statuses"][0]["status"], "complete")

    def test_multiple_tasks(self):
        result = knowledge_ingest_status(task_ids=["t-1", "t-2"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["statuses"]), 2)

    def test_missing_param(self):
        result = knowledge_ingest_status()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "missing_param")


class TestKnowledgeSearch(unittest.TestCase):
    def test_search_success(self):
        mock = _mock_store()
        mock.search.return_value = [
            SearchResult(content="blade angle 60 deg", source="nasa.pdf", score=0.9),
        ]
        with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
            result = knowledge_search(query="turbine blade angle")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "lancedb")
            self.assertEqual(result["result_count"], 1)

    def test_search_fallback_when_unavailable(self):
        with patch("server.tools_knowledge.get_knowledge_store", return_value=None):
            result = knowledge_search(query="anything")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "local_fallback")

    def test_search_fallback_on_error(self):
        mock = _mock_store()
        mock.search.side_effect = Exception("search failed")
        with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
            result = knowledge_search(query="anything")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "local_fallback")


class TestKnowledgeStatus(unittest.TestCase):
    def test_not_configured(self):
        with patch("server.tools_knowledge.get_knowledge_store", return_value=None):
            result = knowledge_status()
            self.assertTrue(result["ok"])
            self.assertFalse(result["store_available"])

    def test_healthy(self):
        mock = _mock_store()
        mock.status.return_value = {
            "available": True,
            "db_path": "/tmp/lancedb",
            "chunk_count": 10,
            "document_count": 2,
        }
        with patch("server.tools_knowledge.get_knowledge_store", return_value=mock):
            result = knowledge_status()
            self.assertTrue(result["ok"])
            self.assertTrue(result["store_available"])
            self.assertEqual(result["document_count"], 2)
            self.assertEqual(result["chunk_count"], 10)


if __name__ == "__main__":
    unittest.main()
