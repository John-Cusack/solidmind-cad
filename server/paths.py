from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    # server/paths.py -> server/ -> repo root
    return Path(__file__).resolve().parents[1]


def data_path(*parts: str) -> Path:
    return repo_root().joinpath(*parts)

