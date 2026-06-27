"""JSON-file persistence for parametric studies in studies/<study_id>/."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from server.study_models import Study

log = logging.getLogger("solidmind.study_store")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))

# Default root directory for study data
_DEFAULT_ROOT = Path("studies")


def _root(root: Path | None = None) -> Path:
    return root if root is not None else _DEFAULT_ROOT


def save_study(study: Study, *, root: Path | None = None) -> Path:
    """Persist a study to studies/<study_id>/study.json. Returns the file path."""
    d = _root(root) / study.id
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "study.json"
    fp.write_text(json.dumps(study.to_dict(), indent=2))
    if _TOOL_LOG:
        log.debug("save %s status=%s", study.id, study.status.value)
    return fp


def load_study(study_id: str, *, root: Path | None = None) -> Study:
    """Load a study from disk. Raises FileNotFoundError if missing."""
    fp = _root(root) / study_id / "study.json"
    if _TOOL_LOG:
        log.debug("load %s from %s", study_id, fp)
    data = json.loads(fp.read_text())
    return Study.from_dict(data)


def list_studies(*, root: Path | None = None) -> list[dict[str, Any]]:
    """Return summary dicts for all studies on disk."""
    r = _root(root)
    if not r.is_dir():
        if _TOOL_LOG:
            log.debug("list_studies: no studies dir at %s", r)
        return []
    summaries: list[dict[str, Any]] = []
    for entry in sorted(r.iterdir()):
        fp = entry / "study.json"
        if fp.is_file():
            try:
                data = json.loads(fp.read_text())
                summaries.append(
                    {
                        "id": data["id"],
                        "name": data["name"],
                        "status": data.get("status", "draft"),
                        "coarse_count": len(data.get("coarse_variants", [])),
                        "refined_count": len(data.get("refined_variants", [])),
                        "best_variant_id": data.get("best_variant_id"),
                    }
                )
            except (json.JSONDecodeError, KeyError):
                continue
    return summaries


def study_exists(study_id: str, *, root: Path | None = None) -> bool:
    """Check whether a study exists on disk."""
    return (_root(root) / study_id / "study.json").is_file()


def delete_study(study_id: str, *, root: Path | None = None) -> bool:
    """Delete a study directory. Returns True if deleted."""
    import shutil

    d = _root(root) / study_id
    if d.is_dir():
        shutil.rmtree(d)
        return True
    return False
