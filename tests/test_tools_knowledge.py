"""Tests for server.tools_knowledge — mocked OpenRAG client."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.openrag_client import (
    ExtractResult,
    IngestResult,
    SearchResult,
    reset_openrag_client,
)
from server.tools_knowledge import (
    knowledge_extract,
    knowledge_ingest,
    knowledge_ingest_status,
    knowledge_search,
    knowledge_status,
)


def _mock_client() -> MagicMock:
    """Create a MagicMock posing as an OpenRAGClient."""
    m = MagicMock()
    m.health_check.return_value = True
    return m


class TestKnowledgeExtract(unittest.TestCase):
    def setUp(self):
        reset_openrag_client()

    def test_unavailable_returns_error(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENRAG_URL", None)
            reset_openrag_client()
            result = knowledge_extract(file_path="/tmp/test.pdf")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "openrag_unavailable")

    def test_file_not_found(self):
        with patch("server.tools_knowledge.get_openrag_client", return_value=_mock_client()):
            result = knowledge_extract(file_path="/nonexistent/path.pdf")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "file_not_found")

    def test_extract_success(self):
        mock = _mock_client()
        mock.extract_file.return_value = ExtractResult(
            text="Extracted content here",
            filename="test.pdf",
            pages=10,
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(b"fake pdf")
            f.flush()
            with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
                result = knowledge_extract(file_path=f.name)
                self.assertTrue(result["ok"])
                self.assertEqual(result["pages"], 10)
                self.assertIn("Extracted", result["text"])

    def test_extract_error(self):
        mock = _mock_client()
        mock.extract_file.side_effect = Exception("parse failed")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(b"fake pdf")
            f.flush()
            with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
                result = knowledge_extract(file_path=f.name)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "extract_error")


class TestKnowledgeIngest(unittest.TestCase):
    def setUp(self):
        reset_openrag_client()

    def test_unavailable(self):
        with patch("server.tools_knowledge.get_openrag_client", return_value=None):
            result = knowledge_ingest(path="/tmp/test.pdf")
            self.assertFalse(result["ok"])

    def test_single_file(self):
        mock = _mock_client()
        mock.ingest_file.return_value = IngestResult(
            task_id="t-123", filename="test.md", status="submitted",
        )
        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# Notes")
            f.flush()
            with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
                result = knowledge_ingest(path=f.name)
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "single")
                self.assertEqual(result["task_id"], "t-123")

    def test_directory(self):
        mock = _mock_client()
        mock.ingest_directory.return_value = [
            IngestResult(task_id="t-1", filename="a.md", status="submitted"),
            IngestResult(task_id="t-2", filename="b.pdf", status="submitted"),
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
                result = knowledge_ingest(path=td)
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "directory")
                self.assertEqual(result["files_submitted"], 2)

    def test_path_not_found(self):
        mock = _mock_client()
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_ingest(path="/nonexistent/path")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "path_not_found")


class TestKnowledgeIngestStatus(unittest.TestCase):
    def test_single_task(self):
        mock = _mock_client()
        mock.ingest_status.return_value = {
            "task_id": "t-123", "status": "complete",
        }
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_ingest_status(task_id="t-123")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["statuses"]), 1)
            self.assertEqual(result["statuses"][0]["status"], "complete")

    def test_multiple_tasks(self):
        mock = _mock_client()
        mock.ingest_status.side_effect = [
            {"task_id": "t-1", "status": "complete"},
            {"task_id": "t-2", "status": "processing"},
        ]
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_ingest_status(task_ids=["t-1", "t-2"])
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["statuses"]), 2)

    def test_missing_param(self):
        mock = _mock_client()
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_ingest_status()
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "missing_param")


class TestKnowledgeSearch(unittest.TestCase):
    def test_search_success(self):
        mock = _mock_client()
        mock.search.return_value = [
            SearchResult(content="blade angle 60 deg", source="nasa.pdf", score=0.9),
        ]
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_search(query="turbine blade angle")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "openrag")
            self.assertEqual(result["result_count"], 1)

    def test_search_fallback_when_unavailable(self):
        with patch("server.tools_knowledge.get_openrag_client", return_value=None):
            result = knowledge_search(query="anything")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "local_fallback")

    def test_search_fallback_on_error(self):
        mock = _mock_client()
        mock.search.side_effect = Exception("connection refused")
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_search(query="anything")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "local_fallback")


class TestKnowledgeStatus(unittest.TestCase):
    def test_not_configured(self):
        with patch("server.tools_knowledge.get_openrag_client", return_value=None):
            result = knowledge_status()
            self.assertTrue(result["ok"])
            self.assertFalse(result["openrag_available"])

    def test_healthy(self):
        mock = _mock_client()
        mock.list_documents.return_value = [{"id": "1"}, {"id": "2"}]
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_status()
            self.assertTrue(result["ok"])
            self.assertTrue(result["openrag_available"])
            self.assertEqual(result["document_count"], 2)

    def test_unhealthy(self):
        mock = _mock_client()
        mock.health_check.return_value = False
        with patch("server.tools_knowledge.get_openrag_client", return_value=mock):
            result = knowledge_status()
            self.assertTrue(result["ok"])
            self.assertFalse(result["openrag_available"])


if __name__ == "__main__":
    unittest.main()
