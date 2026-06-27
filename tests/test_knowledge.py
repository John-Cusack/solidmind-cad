"""Tests for orchestrator.knowledge."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.knowledge import (
    KnowledgeVolume,
    knowledge_context_for_prompt,
    prepare_worker_knowledge,
)
from orchestrator.spec import (
    KnowledgeConfig,
    MasterSpec,
    Subsystem,
    SubsystemKind,
)


class TestPrepareWorkerKnowledge(unittest.TestCase):
    def test_none_mode(self) -> None:
        spec = MasterSpec(
            name="test",
            knowledge=KnowledgeConfig(share_mode="none"),
        )
        sub = Subsystem(name="gear", kind=SubsystemKind.GENERATED)
        with tempfile.TemporaryDirectory() as td:
            vol = prepare_worker_knowledge(spec, sub, run_dir=Path(td))
            self.assertIsNone(vol.global_dir)
            self.assertIsNone(vol.project_dir)

    def test_with_global_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Create a mock knowledge directory
            knowledge_dir = Path(td) / "me_knowledge"
            knowledge_dir.mkdir()
            (knowledge_dir / "notes").mkdir()
            (knowledge_dir / "notes" / "gear_study.md").write_text("test")

            spec = MasterSpec(
                name="test",
                knowledge=KnowledgeConfig(
                    global_paths=[str(knowledge_dir)],
                    share_mode="project_slice",
                ),
            )
            sub = Subsystem(name="gear", kind=SubsystemKind.GENERATED)
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            vol = prepare_worker_knowledge(spec, sub, run_dir=run_dir)
            self.assertIsNotNone(vol.global_dir)
            # Should find gear_study.md as relevant
            self.assertGreater(len(vol.subsystem_notes), 0)


class TestKnowledgeContextForPrompt(unittest.TestCase):
    def test_empty_volume(self) -> None:
        vol = KnowledgeVolume()
        ctx = knowledge_context_for_prompt(vol)
        self.assertIn("no knowledge", ctx)

    def test_with_paths(self) -> None:
        vol = KnowledgeVolume(
            global_dir=Path("/knowledge/global"),
            subsystem_notes=[Path("/notes/gear.md")],
        )
        ctx = knowledge_context_for_prompt(vol)
        self.assertIn("Global", ctx)
        self.assertIn("gear.md", ctx)


if __name__ == "__main__":
    unittest.main()
