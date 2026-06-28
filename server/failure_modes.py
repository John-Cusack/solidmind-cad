"""Shared part-class failure-mode taxonomy loader (the Reflect step's lookup).

The inner loop's Reflect step asks, before running an ``analysis.*`` check:
*what kind of part is this, and what are its characteristic failure modes?*
The answer lives in hand-curated YAML catalogs under
``me_knowledge/failure_modes/`` — one ``part_class`` key per entry, listing the
failure modes to check, the expected hotspot, and a plausible peak-stress band.
This module turns a ``part_class`` string (the field now carried on
``design.save_brief`` / ``design.add_part``) into a typed
:class:`~server.analysis_models.ReflectExpectations`.

The format is the one the foam-dart example proved; see
``me_knowledge/failure_modes/README.md``.  Loading is tolerant: a missing
directory, a missing PyYAML, or a single malformed entry degrades to a warning
rather than crashing the Reflect path — mirroring ``_parse_failure_mode`` in
``analysis_models``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from server.analysis_models import FailureMode, ReflectExpectations

log = logging.getLogger("solidmind.failure_modes")

# me_knowledge/failure_modes/ — sibling of the repo's server/ package.
DEFAULT_TAXONOMY_DIR = Path(__file__).resolve().parent.parent / "me_knowledge" / "failure_modes"


def _coerce_entry(part_class: str, spec: dict[str, Any]) -> ReflectExpectations | None:
    """Build a ``ReflectExpectations`` from one catalog entry, or None if invalid.

    Unknown failure-mode strings are dropped (with a warning) rather than raising,
    so a stale enum value in one entry can't poison the whole catalog.
    """
    try:
        modes: list[FailureMode] = []
        for raw in spec["failure_modes_to_check"]:
            try:
                modes.append(FailureMode(raw))
            except ValueError:
                log.warning(
                    "failure_modes: part_class '%s' lists unknown mode '%s' — skipping it",
                    part_class,
                    raw,
                )
        lo, hi = spec["expected_peak_stress_mpa"]
        return ReflectExpectations(
            part_class=part_class,
            failure_modes_to_check=tuple(modes),
            expected_hotspot=str(spec["expected_hotspot"]),
            expected_peak_stress_mpa=(float(lo), float(hi)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("failure_modes: skipping malformed entry '%s': %s", part_class, exc)
        return None


def _load_dir(directory: Path) -> dict[str, ReflectExpectations]:
    """Load and merge every ``*.yaml`` in one directory.  Returns {} if absent."""
    if not directory.is_dir():
        return {}
    try:
        import yaml
    except ImportError:
        log.warning("failure_modes: PyYAML not installed — taxonomy unavailable")
        return {}

    out: dict[str, ReflectExpectations] = {}
    # Sorted so the merge order is deterministic (later files win within a dir).
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("failure_modes: could not read %s: %s", path, exc)
            continue
        for part_class, spec in (data.get("part_classes") or {}).items():
            entry = _coerce_entry(str(part_class), spec)
            if entry is not None:
                out[entry.part_class] = entry
    return out


def load_taxonomy(extra_dirs: Iterable[Path] | None = None) -> dict[str, ReflectExpectations]:
    """Return the merged ``part_class -> ReflectExpectations`` catalog.

    Loads the built-in ``me_knowledge/failure_modes/`` directory first, then any
    ``extra_dirs`` (e.g. an example's local override) in order — later entries
    win on key collision, so a caller can shadow a shared default.
    """
    catalog = _load_dir(DEFAULT_TAXONOMY_DIR)
    for directory in extra_dirs or ():
        catalog.update(_load_dir(Path(directory)))
    return catalog


def expectations_for(
    part_class: str,
    extra_dirs: Iterable[Path] | None = None,
) -> ReflectExpectations | None:
    """Look up one part class.  Returns None if it isn't in the catalog."""
    if not part_class:
        return None
    return load_taxonomy(extra_dirs).get(part_class)


def known_part_classes(extra_dirs: Iterable[Path] | None = None) -> list[str]:
    """Sorted list of part-class keys the catalog knows about."""
    return sorted(load_taxonomy(extra_dirs))
