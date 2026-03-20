"""Knowledge volume preparation — distribute knowledge to workers."""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.spec import KnowledgeConfig, MasterSpec, Subsystem

log = logging.getLogger(__name__)


@dataclass(slots=True)
class KnowledgeVolume:
    """Prepared knowledge directory for a worker."""

    global_dir: Path | None = None
    project_dir: Path | None = None
    subsystem_notes: list[Path] = field(default_factory=list)


def prepare_worker_knowledge(
    spec: MasterSpec,
    subsystem: Subsystem,
    *,
    run_dir: Path,
    worker_index: int = 0,
) -> KnowledgeVolume:
    """Prepare knowledge directories for a single worker.

    Copies or symlinks relevant knowledge based on ``spec.knowledge.share_mode``:
    - ``full``: worker gets all global knowledge paths
    - ``project_slice``: worker gets global + subsystem-relevant notes
    - ``none``: no knowledge shared
    """
    vol = KnowledgeVolume()
    cfg = spec.knowledge

    if cfg.share_mode == "none":
        return vol

    worker_dir = run_dir / f"{subsystem.name}_{worker_index}"
    knowledge_dir = worker_dir / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # Global knowledge
    for gp in cfg.global_paths:
        src = Path(gp)
        if src.exists() and src.is_dir():
            dst = knowledge_dir / "global" / src.name
            if not dst.exists():
                _copy_or_link(src, dst)
            vol.global_dir = knowledge_dir / "global"

    # Project-level context
    if cfg.project_path:
        src = Path(cfg.project_path)
        if src.exists():
            dst = knowledge_dir / "project"
            if not dst.exists():
                _copy_or_link(src, dst)
            vol.project_dir = dst

    # Subsystem-specific notes (project_slice mode)
    if cfg.share_mode == "project_slice":
        vol.subsystem_notes = _find_relevant_notes(subsystem, cfg)

    return vol


def prepare_all_knowledge(
    spec: MasterSpec,
    *,
    run_dir: Path,
) -> dict[str, KnowledgeVolume]:
    """Prepare knowledge for all GENERATED subsystems."""
    from orchestrator.spec import SubsystemKind

    volumes: dict[str, KnowledgeVolume] = {}
    for sub in spec.subsystems:
        if sub.kind != SubsystemKind.GENERATED:
            continue
        for i in range(sub.worker_count):
            key = f"{sub.name}_{i}"
            volumes[key] = prepare_worker_knowledge(
                spec, sub, run_dir=run_dir, worker_index=i,
            )
    return volumes


def knowledge_context_for_prompt(vol: KnowledgeVolume) -> str:
    """Format knowledge volume paths as prompt context."""
    lines: list[str] = []
    if vol.global_dir:
        lines.append(f"Global knowledge: {vol.global_dir}")
    if vol.project_dir:
        lines.append(f"Project context: {vol.project_dir}")
    if vol.subsystem_notes:
        lines.append("Relevant notes:")
        for note in vol.subsystem_notes:
            lines.append(f"  - {note}")
    return "\n".join(lines) if lines else "(no knowledge available)"


def _find_relevant_notes(
    subsystem: Subsystem,
    cfg: KnowledgeConfig,
) -> list[Path]:
    """Find knowledge notes relevant to a subsystem by keyword matching."""
    keywords = _extract_keywords(subsystem)
    found: list[Path] = []

    for gp in cfg.global_paths:
        notes_dir = Path(gp) / "notes"
        if not notes_dir.exists():
            continue
        for note_path in notes_dir.glob("*.md"):
            stem = note_path.stem.lower()
            if any(kw in stem for kw in keywords):
                found.append(note_path)

    return found


def _extract_keywords(subsystem: Subsystem) -> list[str]:
    """Extract search keywords from subsystem name, description, and specs."""
    keywords: list[str] = []
    if subsystem.name:
        keywords.extend(subsystem.name.lower().replace("_", " ").split())
    if subsystem.material:
        keywords.append(subsystem.material.lower())
    if subsystem.manufacturing.process:
        keywords.append(subsystem.manufacturing.process.lower())
    return keywords


def _copy_or_link(src: Path, dst: Path) -> None:
    """Copy a directory tree, falling back to symlink if copy fails."""
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except (OSError, shutil.Error):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src.resolve())
        except OSError:
            log.warning("Cannot copy or link %s → %s", src, dst)
