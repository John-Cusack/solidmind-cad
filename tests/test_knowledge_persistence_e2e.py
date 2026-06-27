"""TICKET-D: knowledge findings survive a fresh process (the Learn step).

Ingests a finding, tears the store singleton down (simulating process exit),
re-opens a fresh store pointing at the same on-disk path, and confirms the
finding is still searchable. Without this, step 9 (Learn) of the inner loop
could pass accidentally.

Embeddings are forced off so the test is fast and offline — retrieval rides on
LanceDB's FTS (keyword) index, which is independent of the vector backend.
Skipped when LanceDB/pyarrow aren't installed.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import server.knowledge_store as ks


def _lancedb_available() -> bool:
    try:
        import lancedb  # noqa: F401
        import pyarrow  # noqa: F401

        return True
    except Exception:
        return False


@unittest.skipUnless(_lancedb_available(), "lancedb/pyarrow not installed")
class TestKnowledgePersistenceE2E(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("KNOWLEDGE_DB_PATH")
        os.environ["KNOWLEDGE_DB_PATH"] = os.path.join(self._tmp.name, "lancedb")
        ks.reset_knowledge_store()

    def tearDown(self) -> None:
        ks.reset_knowledge_store()
        if self._prev is None:
            os.environ.pop("KNOWLEDGE_DB_PATH", None)
        else:
            os.environ["KNOWLEDGE_DB_PATH"] = self._prev
        self._tmp.cleanup()

    @patch.object(ks, "_make_embedding_fn", return_value=None)
    def test_finding_survives_fresh_store(self, _mock_embed) -> None:
        finding = (
            "Adding a 0.5 mm root fillet at the servo mount relieved the stress "
            "concentration on the hexapod hip bracket and raised the factor of safety."
        )

        # --- session 1: ingest, then tear the store down (process exit) ---
        store = ks.get_knowledge_store()
        if store is None:
            self.skipTest("knowledge store unavailable in this environment")
        ingest = store.ingest_text(finding, "hip_bracket_finding.md")
        self.assertEqual(ingest.status, "complete")
        self.assertGreater(ingest.chunks, 0)
        ks.reset_knowledge_store()

        # --- session 2: fresh store, same path on disk ---
        store2 = ks.get_knowledge_store()
        self.assertIsNotNone(store2)
        self.assertIsNot(store2, store)  # genuinely a new instance
        hits = store2.search("hip bracket fillet stress concentration", top_k=3)
        self.assertTrue(
            any("fillet" in h.content.lower() for h in hits),
            f"ingested finding not found after reopen; hits={[h.content[:40] for h in hits]}",
        )


if __name__ == "__main__":
    unittest.main()
