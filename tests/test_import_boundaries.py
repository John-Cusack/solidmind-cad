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

# Packages that leave core at split time.
_ENGINE_PACKAGES = ("gazebo_bridge", "isaac_bridge", "rl_training")

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

    def test_scan_actually_sees_files(self) -> None:
        """Guard the guard — an empty scan would pass vacuously."""
        for package in _ENGINE_PACKAGES:
            self.assertGreater(len(_python_files(package)), 0, f"no files scanned in {package}")


class TestCoreDoesNotImportEngines(unittest.TestCase):
    """Core never imports a bridge package.

    ``server.tools_rl`` is the one allowed exception and only lazily: the
    ``rl.*`` tools orchestrate the RL pipeline, and step 7 of the migration
    replaces those calls with the CLI parity path.  Module-level imports are
    forbidden even there, so importing core never requires the RL package.
    """

    _ALLOWED_LAZY = {("server/tools_rl.py", "rl_training")}

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
