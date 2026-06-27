"""JSON-file persistence for field analyses in analyses/<analysis_id>/."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from server.analysis_models import FieldResult

log = logging.getLogger("solidmind.analysis_store")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))

_DEFAULT_ROOT = Path("analyses")


def _root(root: Path | None = None) -> Path:
    return root if root is not None else _DEFAULT_ROOT


def save_result(result: FieldResult, *, root: Path | None = None) -> Path:
    """Persist a field result to analyses/<analysis_id>/result.json."""
    d = _root(root) / result.analysis_id
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "result.json"
    fp.write_text(json.dumps(result.to_dict(), indent=2))
    if _TOOL_LOG:
        log.debug("save %s status=%s", result.analysis_id, result.status.value)
    return fp


def load_result(analysis_id: str, *, root: Path | None = None) -> FieldResult:
    """Load a field result from disk. Raises FileNotFoundError if missing."""
    fp = _root(root) / analysis_id / "result.json"
    data = json.loads(fp.read_text())
    return FieldResult.from_dict(data)


def list_analyses(*, root: Path | None = None) -> list[dict[str, Any]]:
    """Return summary dicts for all analyses on disk."""
    r = _root(root)
    if not r.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for entry in sorted(r.iterdir()):
        fp = entry / "result.json"
        if fp.is_file():
            try:
                data = json.loads(fp.read_text())
                summaries.append(
                    {
                        "id": data.get("analysis_id", entry.name),
                        "status": data.get("status", "unknown"),
                        "safety_factor": data.get("safety_factor", 0),
                        "max_von_mises_mpa": data.get("max_von_mises_mpa", 0),
                        "solver_name": data.get("solver_name", ""),
                    }
                )
            except (json.JSONDecodeError, KeyError):
                continue
    return summaries
