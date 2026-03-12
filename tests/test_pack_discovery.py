"""Tests for extension pack discovery (tool packs + knowledge packs)."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, module: types.ModuleType) -> None:
        self.name = name
        self._module = module

    def load(self) -> types.ModuleType:
        return self._module


# ---------------------------------------------------------------------------
# Tool pack discovery
# ---------------------------------------------------------------------------

class TestToolPackDiscovery(unittest.TestCase):
    """Verify _discover_tool_packs() loads TOOLS + DISPATCH from entry points."""

    def _make_pack_module(
        self,
        tools: list[dict] | None = None,
        dispatch: dict | None = None,
    ) -> types.ModuleType:
        mod = types.ModuleType("fake_pack")
        if tools is not None:
            mod.TOOLS = tools  # type: ignore[attr-defined]
        if dispatch is not None:
            mod.DISPATCH = dispatch  # type: ignore[attr-defined]
        return mod

    @patch("importlib.metadata.entry_points")
    def test_loads_tools_and_dispatch(self, mock_eps: MagicMock) -> None:
        handler = lambda **kw: {"ok": True}
        mod = self._make_pack_module(
            tools=[{"name": "geometry.test_tool", "description": "A test"}],
            dispatch={"geometry.test_tool": handler},
        )
        mock_eps.return_value = [_FakeEntryPoint("testpack", mod)]

        from server.main import _discover_tool_packs

        tools, dispatch = _discover_tool_packs()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "geometry.test_tool")
        self.assertIn("geometry.test_tool", dispatch)
        self.assertIs(dispatch["geometry.test_tool"], handler)

    @patch("importlib.metadata.entry_points")
    def test_empty_when_no_packs(self, mock_eps: MagicMock) -> None:
        mock_eps.return_value = []

        from server.main import _discover_tool_packs

        tools, dispatch = _discover_tool_packs()
        self.assertEqual(tools, [])
        self.assertEqual(dispatch, {})

    @patch("importlib.metadata.entry_points")
    def test_broken_pack_skipped(self, mock_eps: MagicMock) -> None:
        ep = _FakeEntryPoint("broken", types.ModuleType("broken"))
        ep.load = lambda: (_ for _ in ()).throw(ImportError("boom"))  # type: ignore[assignment]
        mock_eps.return_value = [ep]

        from server.main import _discover_tool_packs

        tools, dispatch = _discover_tool_packs()
        self.assertEqual(tools, [])
        self.assertEqual(dispatch, {})

    @patch("importlib.metadata.entry_points")
    def test_duplicate_tool_warns(self, mock_eps: MagicMock) -> None:
        handler_a = lambda **kw: "a"
        handler_b = lambda **kw: "b"
        mod_a = self._make_pack_module(
            tools=[{"name": "geometry.dup"}],
            dispatch={"geometry.dup": handler_a},
        )
        mod_b = self._make_pack_module(
            tools=[{"name": "geometry.dup"}],
            dispatch={"geometry.dup": handler_b},
        )
        mock_eps.return_value = [
            _FakeEntryPoint("pack_a", mod_a),
            _FakeEntryPoint("pack_b", mod_b),
        ]

        from server.main import _discover_tool_packs

        tools, dispatch = _discover_tool_packs()
        # Both tool schemas are added (schemas are append-only);
        # dispatch keeps the last writer (update semantics)
        self.assertEqual(len(tools), 2)

    @patch("importlib.metadata.entry_points")
    def test_module_without_attributes(self, mock_eps: MagicMock) -> None:
        """A pack module missing TOOLS/DISPATCH should not crash."""
        mod = types.ModuleType("empty_pack")
        mock_eps.return_value = [_FakeEntryPoint("empty", mod)]

        from server.main import _discover_tool_packs

        tools, dispatch = _discover_tool_packs()
        self.assertEqual(tools, [])
        self.assertEqual(dispatch, {})


# ---------------------------------------------------------------------------
# Knowledge pack discovery
# ---------------------------------------------------------------------------

class TestKnowledgePackDiscovery(unittest.TestCase):

    def _make_knowledge_module(
        self, kdir: Path, domain: str = "test", version: str = "1.0.0",
    ) -> types.ModuleType:
        mod = types.ModuleType("fake_kpack")
        mod.KNOWLEDGE_DIR = kdir  # type: ignore[attr-defined]
        mod.DOMAIN = domain  # type: ignore[attr-defined]
        mod.VERSION = version  # type: ignore[attr-defined]
        return mod

    @patch("importlib.metadata.entry_points")
    def test_discovers_knowledge_pack(self, mock_eps: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kdir = Path(tmpdir) / "knowledge"
            kdir.mkdir()
            (kdir / "test_topic.md").write_text("## Test\nSome knowledge.")

            mod = self._make_knowledge_module(kdir, "testdomain", "1.0.0")
            mock_eps.return_value = [_FakeEntryPoint("testknowledge", mod)]

            from server.knowledge_store import _discover_knowledge_packs

            packs = _discover_knowledge_packs()
            self.assertEqual(len(packs), 1)
            domain, version, path = packs[0]
            self.assertEqual(domain, "testdomain")
            self.assertEqual(version, "1.0.0")
            self.assertEqual(path, kdir)

    @patch("importlib.metadata.entry_points")
    def test_skips_missing_directory(self, mock_eps: MagicMock) -> None:
        mod = types.ModuleType("bad_kpack")
        mod.KNOWLEDGE_DIR = Path("/nonexistent/dir")  # type: ignore[attr-defined]
        mod.DOMAIN = "bad"  # type: ignore[attr-defined]
        mod.VERSION = "1.0.0"  # type: ignore[attr-defined]
        mock_eps.return_value = [_FakeEntryPoint("bad", mod)]

        from server.knowledge_store import _discover_knowledge_packs

        packs = _discover_knowledge_packs()
        self.assertEqual(packs, [])


# ---------------------------------------------------------------------------
# Local note listing includes knowledge packs
# ---------------------------------------------------------------------------

class TestLocalNoteListingWithPacks(unittest.TestCase):

    @patch("server.tools_knowledge._discover_knowledge_packs")
    def test_includes_pack_files(self, mock_discover: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kdir = Path(tmpdir)
            (kdir / "blade_design.md").write_text("## Blade design\nContent.")
            (kdir / "materials.md").write_text("## Materials\nContent.")

            mock_discover.return_value = [("turbo", "1.0.0", kdir)]

            from server.tools_knowledge import _local_note_listing

            notes = _local_note_listing()
            pack_notes = [n for n in notes if n.startswith("[turbo]")]
            self.assertEqual(len(pack_notes), 2)
            self.assertIn("[turbo] blade_design.md", pack_notes)
            self.assertIn("[turbo] materials.md", pack_notes)


# ---------------------------------------------------------------------------
# ensure_packs_ingested version tracking
# ---------------------------------------------------------------------------

class TestEnsurePacksIngested(unittest.TestCase):

    @patch("server.knowledge_store._discover_knowledge_packs")
    def test_version_marker_prevents_reingestion(self, mock_discover: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kdir = Path(tmpdir) / "knowledge"
            kdir.mkdir()
            (kdir / "topic.md").write_text("## Topic\nContent.")

            db_path = Path(tmpdir) / "db"
            db_path.mkdir()

            mock_discover.return_value = [("testdomain", "1.0.0", kdir)]

            # Create a mock store with tracking
            store = MagicMock()
            store._db_path = str(db_path)
            store.ingest_file = MagicMock()

            # Bind the real method
            from server.knowledge_store import KnowledgeStore
            store.ensure_packs_ingested = KnowledgeStore.ensure_packs_ingested.__get__(store)

            # First call — should ingest
            store.ensure_packs_ingested()
            self.assertEqual(store.ingest_file.call_count, 1)

            # Second call — version matches, should skip
            store.ingest_file.reset_mock()
            store.ensure_packs_ingested()
            self.assertEqual(store.ingest_file.call_count, 0)

            # Verify marker file
            marker = db_path / ".pack_versions.json"
            self.assertTrue(marker.exists())
            versions = json.loads(marker.read_text())
            self.assertEqual(versions["testdomain"], "1.0.0")

    @patch("server.knowledge_store._discover_knowledge_packs")
    def test_version_bump_triggers_reingestion(self, mock_discover: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kdir = Path(tmpdir) / "knowledge"
            kdir.mkdir()
            (kdir / "topic.md").write_text("## Topic\nContent.")

            db_path = Path(tmpdir) / "db"
            db_path.mkdir()

            # Pre-seed marker with old version
            marker = db_path / ".pack_versions.json"
            marker.write_text(json.dumps({"testdomain": "0.9.0"}))

            mock_discover.return_value = [("testdomain", "1.0.0", kdir)]

            store = MagicMock()
            store._db_path = str(db_path)
            store.ingest_file = MagicMock()

            from server.knowledge_store import KnowledgeStore
            store.ensure_packs_ingested = KnowledgeStore.ensure_packs_ingested.__get__(store)

            store.ensure_packs_ingested()
            self.assertEqual(store.ingest_file.call_count, 1)

            # Marker updated
            versions = json.loads(marker.read_text())
            self.assertEqual(versions["testdomain"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
