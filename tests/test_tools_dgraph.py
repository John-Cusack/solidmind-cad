"""Tests for the dgraph.* MCP tool group."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.paths import repo_root
from server.tools_dgraph import dgraph_get_revision, dgraph_import_brief, dgraph_list_revisions

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"


class ToolsDgraphBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env_patcher = patch.dict(
            "os.environ",
            {"SOLIDMIND_ARTIFACTS_ROOT": str(Path(self._tmp.name) / "artifacts")},
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)


class TestDgraphTools(ToolsDgraphBase):
    def test_import_and_inspect(self) -> None:
        out = dgraph_import_brief(str(FIXTURE))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["components"], 11)
        self.assertEqual(out["interfaces"], 6)
        self.assertEqual(len(out["revision_id"]), 64)

        rev = dgraph_get_revision(out["revision_id"])
        self.assertTrue(rev["ok"])
        self.assertIsNone(rev["parent"])
        self.assertIn("latch_sear", rev["components"])
        self.assertEqual(rev["params"]["parts"]["latch_sear"]["specs"]["fillet_mm"]["value"], 0.0)

        listed = dgraph_list_revisions()
        self.assertTrue(listed["ok"])
        self.assertEqual([r["revision_id"] for r in listed["revisions"]], [out["revision_id"]])

    def test_import_missing_file(self) -> None:
        out = dgraph_import_brief("/nonexistent/brief.json")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "NOT_FOUND")

    def test_import_invalid_brief(self) -> None:
        bad = Path(self._tmp.name) / "bad.json"
        bad.write_text('{"schema": "bogus"}')
        out = dgraph_import_brief(str(bad))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")

    def test_get_unknown_revision(self) -> None:
        out = dgraph_get_revision("f" * 64)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "NOT_FOUND")


class TestRegistration(unittest.TestCase):
    def test_dgraph_tools_registered(self) -> None:
        from server.main import _DGRAPH_DISPATCH, _tool_list

        names = {t["name"] for t in _tool_list()}
        for tool in ("dgraph.import_brief", "dgraph.get_revision", "dgraph.list_revisions"):
            self.assertIn(tool, names)
            self.assertIn(tool, _DGRAPH_DISPATCH)


if __name__ == "__main__":
    unittest.main()
