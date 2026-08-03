"""Acceptance criteria for the engine extraction (architecture doc §9).

Step 7 is verification, and verification that only lives in a document rots.
Everything here that can be mechanically checked, is — so a future change that
quietly re-couples core to an engine fails a test rather than a review.

The criteria needing real engines or a published remote are recorded in
``docs/engine-extraction-verification.md`` with their evidence.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Packages that leave core at split time.
_ENGINE_PACKAGES = (
    "chrono_bridge",
    "chrono_daemon",
    "gazebo_bridge",
    "isaac_bridge",
    "rl_training",
)


class TestWheelShipsNoEngineCode(unittest.TestCase):
    """Criterion 6: the wheel ships no engine code.

    Checked at the configuration level so it holds in a working checkout,
    where the engine directories are still present. Building a wheel needs
    Rust and several minutes; the packaging config is what decides the
    contents either way.
    """

    @classmethod
    def setUpClass(cls) -> None:
        with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
            cls.maturin = tomllib.load(fh)["tool"]["maturin"]

    def _paths(self, key: str) -> list[str]:
        return [entry["path"] for entry in self.maturin.get(key, [])]

    def test_every_engine_package_is_excluded(self) -> None:
        excluded = " ".join(self._paths("exclude"))
        for package in _ENGINE_PACKAGES:
            self.assertIn(package, excluded, f"{package} is not excluded from the wheel")

    def test_no_engine_package_is_included(self) -> None:
        included = self._paths("include")
        for package in _ENGINE_PACKAGES:
            for path in included:
                self.assertFalse(
                    path.startswith(package),
                    f"{package} is included via {path!r}",
                )

    def test_core_is_included(self) -> None:
        """The exclusion must not have thrown out what core needs to run."""
        included = " ".join(self._paths("include"))
        for needed in ("server/", "reference_engine/", "tck/", "engines.d/", "schemas/"):
            self.assertIn(needed, included, f"{needed} missing from the wheel")

    def test_tests_and_examples_stay_out(self) -> None:
        excluded = " ".join(self._paths("exclude"))
        self.assertIn("tests/", excluded)
        self.assertIn("examples/", excluded)


class TestCoreRunsWithoutEngines(unittest.TestCase):
    """Criterion 2: core is usable with zero engines installed."""

    def test_importing_core_touches_no_engine_package(self) -> None:
        """Import the MCP server in a subprocess and see what it loaded.

        A lazy import inside a rarely-run branch would not show up here, which
        is what the static guard in test_import_boundaries.py is for; this is
        the runtime half of the same claim.
        """
        code = (
            "import sys; import server.main; "
            "loaded = sorted(m for m in sys.modules "
            f"if m.split('.')[0] in {_ENGINE_PACKAGES!r}); "
            "print(','.join(loaded))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=120,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        loaded = [name for name in proc.stdout.strip().split(",") if name]
        self.assertEqual(loaded, [], f"importing core pulled in {loaded}")

    def test_the_reference_engine_is_always_registered(self) -> None:
        from server.engine_registry import engine_names, reset_cache

        reset_cache()
        self.assertIn("reference", engine_names())


class TestNoVendorNamesInControlFlow(unittest.TestCase):
    """Criterion 7: no engine names in core's control flow.

    Names in strings, comments and docstrings are fine — descriptors, log
    messages and prose all name engines. What must not exist is a *branch* on
    one: ``if backend == "isaac"`` and friends.
    """

    _KNOWN_EXCEPTIONS = {
        # A parametric-study solver keyed by name in the SOLVERS registry —
        # a different subsystem from engine selection, and next in line.
        "server/study_solvers.py",
    }

    def test_no_comparison_against_an_engine_name(self) -> None:
        engine_literals = {"isaac", "gazebo", "chrono"}
        violations: list[str] = []

        for path in sorted((_REPO_ROOT / "server").rglob("*.py")):
            rel = str(path.relative_to(_REPO_ROOT))
            if rel in self._KNOWN_EXCEPTIONS:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                for operand in [node.left, *node.comparators]:
                    if isinstance(operand, ast.Constant) and operand.value in engine_literals:
                        violations.append(f"{rel}:{node.lineno} branches on {operand.value!r}")
        self.assertEqual(
            violations,
            [],
            "Core must select engines from the registry and handshake, not by name:\n  "
            + "\n  ".join(violations),
        )


class TestContractArtifactsArePresent(unittest.TestCase):
    """The contract is what the whole split rests on — it ships with core."""

    def test_spec_schemas_and_kit(self) -> None:
        for artifact in (
            "docs/engine-contract.md",
            "docs/engines.md",
            "schemas/sim_package.schema.json",
            "schemas/sim_result.schema.json",
            "schemas/field_snapshot.schema.json",
            "tck/runner.py",
            "tck/README.md",
            "reference_engine/bridge_server.py",
        ):
            self.assertTrue((_REPO_ROOT / artifact).is_file(), f"missing {artifact}")

    def test_every_registered_engine_has_a_descriptor_file(self) -> None:
        from server.engine_registry import engine_names, get_descriptor, reset_cache

        reset_cache()
        for name in engine_names():
            descriptor = get_descriptor(name)
            self.assertTrue(descriptor.source.endswith(".toml"), name)
            self.assertTrue(Path(descriptor.source).is_file(), descriptor.source)


class TestSplitToolingIsRunnable(unittest.TestCase):
    """Criterion for step 6: the split is reproducible, not a one-off."""

    def test_scripts_exist_and_parse(self) -> None:
        for script in (
            "scripts/split_engines.sh",
            "scripts/verify_engine_repos.sh",
            "scripts/remove_engines_from_core.sh",
        ):
            path = _REPO_ROOT / script
            self.assertTrue(path.is_file(), f"missing {script}")
            proc = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, f"{script}: {proc.stderr}")

    def test_removal_script_defaults_to_a_dry_run(self) -> None:
        """It deletes the only local copy of the engines — it must not fire by accident."""
        proc = subprocess.run(
            ["bash", str(_REPO_ROOT / "scripts/remove_engines_from_core.sh")],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Dry run", proc.stdout)


if __name__ == "__main__":
    unittest.main()
