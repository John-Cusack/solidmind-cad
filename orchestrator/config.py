"""Orchestrator configuration — optional YAML config with sensible defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class OrchestratorConfig:
    """Top-level orchestrator configuration."""

    worker_mode: str = "subagent"  # subagent | claude_code | docker | api
    providers: dict[str, Any] = field(
        default_factory=lambda: {
            "default": "anthropic",
            "model": "claude-sonnet-4-20250514",
        }
    )
    cost_policy: dict[str, Any] = field(
        default_factory=lambda: {
            "max_run_cost_usd": 50.0,
            "max_stage_cost_usd": 20.0,
            "warn_at_pct": 80,
        }
    )
    a2a: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "host": "localhost",
            "port": 8080,
        }
    )
    run_dir: str = "runs"
    knowledge_paths: list[str] = field(default_factory=lambda: ["me_knowledge/"])


_SEARCH_PATHS = [
    Path("orchestrator.yaml"),
    Path("orchestrator.yml"),
    Path.home() / ".config" / "solidmind" / "orchestrator.yaml",
    Path.home() / ".config" / "solidmind" / "orchestrator.yml",
]


def load_config(path: str | Path | None = None) -> OrchestratorConfig:
    """Load config from *path*, search standard locations, or return defaults.

    Returns defaults if no config file is found — the system works without one.
    """
    if path is not None:
        p = Path(path)
        if p.exists():
            return _parse(p)
        raise FileNotFoundError(f"Config not found: {p}")

    for candidate in _SEARCH_PATHS:
        if candidate.exists():
            return _parse(candidate)

    return OrchestratorConfig()


def _parse(path: Path) -> OrchestratorConfig:
    """Parse a YAML config file into an OrchestratorConfig."""
    data = yaml.safe_load(path.read_text()) or {}
    cfg = OrchestratorConfig()
    if "worker_mode" in data:
        cfg.worker_mode = data["worker_mode"]
    if "providers" in data:
        cfg.providers = data["providers"]
    if "cost_policy" in data:
        cfg.cost_policy = data["cost_policy"]
    if "a2a" in data:
        cfg.a2a = data["a2a"]
    if "run_dir" in data:
        cfg.run_dir = data["run_dir"]
    if "knowledge_paths" in data:
        cfg.knowledge_paths = data["knowledge_paths"]
    return cfg
