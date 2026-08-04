"""Import-boundary guards for the engine extraction.

Engines are applications, not libraries (``docs/engine-integration-architecture.md``
Principle 1).  Each engine package is destined for its own sibling repository
with its own interpreter, so nothing inside one may import ``server.*`` — not at
module level, not lazily inside a function.  These tests are the standing check
that the boundary severed in step 2 stays severed; a new reverse import fails
here rather than at split time.

The scan is static (``ast``), so it catches imports in code paths that never run
in CI — the Isaac and Gazebo real-runtime branches, for instance.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Engine packages.  After the split these live in sibling repositories, so the
# scan finds nothing here — which is the point.  The names stay listed because
# a checkout mid-migration (or a stray copy) must still be caught.
_ENGINE_PACKAGES = ("chrono_bridge", "gazebo_bridge", "isaac_bridge", "rl_training")

# Core packages an engine package may never import.
_FORBIDDEN_ROOTS = ("server",)


def _python_files(package: str) -> list[Path]:
    return sorted((_REPO_ROOT / package).rglob("*.py"))


def _imported_roots(path: Path) -> set[tuple[str, int]]:
    """Return ``(root_module, lineno)`` for every import in *path*.

    Relative imports resolve inside the package and are ignored.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — stays in-package
                continue
            if node.module:
                found.add((node.module.split(".")[0], node.lineno))
    return found


class TestEnginePackagesDoNotImportCore(unittest.TestCase):
    """No ``server.*`` imports anywhere in an engine package."""

    def test_no_core_imports(self) -> None:
        violations: list[str] = []
        for package in _ENGINE_PACKAGES:
            for path in _python_files(package):
                for root, lineno in _imported_roots(path):
                    if root in _FORBIDDEN_ROOTS:
                        rel = path.relative_to(_REPO_ROOT)
                        violations.append(f"{rel}:{lineno} imports '{root}'")
        self.assertEqual(
            violations,
            [],
            "Engine packages must not import core.  Move the dependency into the "
            "engine package or reach core over the contract instead:\n  "
            + "\n  ".join(sorted(violations)),
        )

    def test_engine_packages_are_gone_from_core(self) -> None:
        """The split's end state: core holds no engine package at all."""
        present = [p for p in _ENGINE_PACKAGES if (_REPO_ROOT / p).is_dir()]
        self.assertEqual(
            present,
            [],
            "engine packages still in core — they live in sibling repositories now:\n  "
            + "\n  ".join(present),
        )


class TestCoreDoesNotImportEngines(unittest.TestCase):
    """Core never imports an engine package — not even lazily.

    The last exception went away with the CLI parity path: ``rl.*`` now drives
    the RL pipeline as a subprocess on its own interpreter, the same way core
    drives an engine over the contract.  Nothing in ``server/`` imports an
    engine package at all.
    """

    _ALLOWED_LAZY: set[tuple[str, str]] = set()

    def test_no_module_level_engine_imports(self) -> None:
        violations: list[str] = []
        for path in sorted((_REPO_ROOT / "server").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:  # module level only
                roots: list[str] = []
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    roots = [node.module.split(".")[0]]
                for root in roots:
                    if root in _ENGINE_PACKAGES:
                        rel = path.relative_to(_REPO_ROOT)
                        violations.append(f"{rel}:{node.lineno} imports '{root}'")
        self.assertEqual(violations, [], "\n  ".join(sorted(violations)))

    def test_lazy_engine_imports_are_declared(self) -> None:
        """Any nested core→engine import must be on the allow-list above."""
        violations: list[str] = []
        for path in sorted((_REPO_ROOT / "server").rglob("*.py")):
            rel = str(path.relative_to(_REPO_ROOT))
            for root, lineno in _imported_roots(path):
                if root in _ENGINE_PACKAGES and (rel, root) not in self._ALLOWED_LAZY:
                    violations.append(f"{rel}:{lineno} imports '{root}'")
        self.assertEqual(violations, [], "\n  ".join(sorted(violations)))


if __name__ == "__main__":
    unittest.main()
