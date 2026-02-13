"""Tests for server.openrag_client — mocked HTTP, no real OpenRAG needed."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from server.openrag_client import (
    ExtractResult,
    IngestResult,
    OpenRAGClient,
    SearchResult,
    get_openrag_client,
    reset_openrag_client,
)


class _FakeResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=MagicMock(), response=self,
            )


class TestOpenRAGClientHealth(unittest.TestCase):
    def test_health_check_success(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.get.return_value = _FakeResponse({}, 200)
        self.assertTrue(client.health_check())

    def test_health_check_failure(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.get.side_effect = httpx.ConnectError("refused")
        self.assertFalse(client.health_check())


class TestOpenRAGClientSearch(unittest.TestCase):
    def test_search_returns_results(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.post.return_value = _FakeResponse({
            "results": [
                {"content": "blade angle 60 deg", "source": "nasa-tr.pdf", "score": 0.92},
                {"content": "hub ratio 0.4", "source": "handbook.pdf", "score": 0.85},
            ],
        })
        results = client.search("radial turbine blade angle")
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], SearchResult)
        self.assertEqual(results[0].source, "nasa-tr.pdf")
        self.assertAlmostEqual(results[0].score, 0.92)

    def test_search_empty(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.post.return_value = _FakeResponse({"results": []})
        results = client.search("nonexistent topic")
        self.assertEqual(results, [])


class TestOpenRAGClientExtract(unittest.TestCase):
    def test_extract_file(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.post.return_value = _FakeResponse({
            "text": "Chapter 1: Turbine Design...",
            "filename": "handbook.pdf",
            "pages": 42,
        })
        tmp = Path("/tmp/test_extract.pdf")
        tmp.write_bytes(b"fake pdf content")
        try:
            result = client.extract_file(tmp)
            self.assertIsInstance(result, ExtractResult)
            self.assertEqual(result.pages, 42)
            self.assertIn("Turbine", result.text)
        finally:
            tmp.unlink(missing_ok=True)


class TestOpenRAGClientIngest(unittest.TestCase):
    def test_ingest_file(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.post.return_value = _FakeResponse({
            "task_id": "task-abc-123",
            "filename": "notes.md",
            "status": "submitted",
        })
        tmp = Path("/tmp/test_ingest.md")
        tmp.write_text("# Research notes")
        try:
            result = client.ingest_file(tmp)
            self.assertIsInstance(result, IngestResult)
            self.assertEqual(result.task_id, "task-abc-123")
            self.assertEqual(result.status, "submitted")
        finally:
            tmp.unlink(missing_ok=True)

    def test_ingest_text(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.post.return_value = _FakeResponse({
            "task_id": "task-text-456",
            "filename": "research.md",
            "status": "submitted",
        })
        result = client.ingest_text("Some research content", "research.md")
        self.assertEqual(result.task_id, "task-text-456")

    def test_ingest_status(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.get.return_value = _FakeResponse({
            "task_id": "task-abc-123",
            "status": "complete",
            "document_id": "doc-789",
        })
        status = client.ingest_status("task-abc-123")
        self.assertEqual(status["status"], "complete")

    def test_ingest_directory(self):
        import tempfile
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.post.return_value = _FakeResponse({
            "task_id": "task-dir-1",
            "filename": "test.md",
            "status": "submitted",
        })
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "a.md").write_text("note a")
            (p / "b.pdf").write_bytes(b"fake pdf")
            (p / "c.txt").write_text("skip me")  # not in extensions
            results = client.ingest_directory(p)
            self.assertEqual(len(results), 2)  # .md and .pdf only


class TestOpenRAGClientDocuments(unittest.TestCase):
    def test_list_documents(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.get.return_value = _FakeResponse({
            "documents": [{"id": "d1", "filename": "a.pdf"}],
        })
        docs = client.list_documents()
        self.assertEqual(len(docs), 1)

    def test_delete_document(self):
        client = OpenRAGClient.__new__(OpenRAGClient)
        client._http = MagicMock()
        client._http.delete.return_value = _FakeResponse({"ok": True})
        result = client.delete_document("d1")
        self.assertTrue(result["ok"])


class TestSingleton(unittest.TestCase):
    def setUp(self):
        reset_openrag_client()

    def tearDown(self):
        reset_openrag_client()

    def test_returns_none_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENRAG_URL", None)
            reset_openrag_client()
            client = get_openrag_client()
            self.assertIsNone(client)

    def test_returns_client_when_configured(self):
        with patch.dict(os.environ, {"OPENRAG_URL": "http://localhost:8080"}):
            reset_openrag_client()
            client = get_openrag_client()
            self.assertIsNotNone(client)
            self.assertIsInstance(client, OpenRAGClient)
            # Second call returns same instance
            self.assertIs(get_openrag_client(), client)


if __name__ == "__main__":
    unittest.main()
