#!/usr/bin/env python3
"""Write the scaffolding a split-out engine repository needs to stand alone.

Called by ``scripts/split_engines.sh``.  Kept in Python because the templates
carry enough prose that heredocs in shell become unreadable.

What every engine repo gets:

* ``pyproject.toml`` — installable on *its own* interpreter (Isaac's bundled
  Python, system Python for Gazebo), never core's.
* ``README.md`` — what the engine is, how to run it, how to prove conformance.
* ``.github/workflows/ci.yml`` — its own tests, the import guard, and the TCK.
* ``tests/test_import_boundary.py`` — the standing check that this repo never
  imports ``server.*`` again.
"""

from __future__ import annotations

import argparse
from pathlib import Path

PYPROJECT = """[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "0.1.0"
description = "{description}"
requires-python = ">=3.12"
dependencies = []

[project.optional-dependencies]
# The conformance kit's schema tier is the only thing that needs a dependency.
tck = ["jsonschema>=4.10,<5"]
dev = ["ruff>=0.6,<1"]

[tool.setuptools]
packages = [{packages_toml}]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP"]
ignore = ["E501"]
"""

README = """# {name}

{description}

Split out of [solidmind-cad](https://github.com/John-Cusack/solidmind-cad) with
its history intact. This repository is an **application**, not a library: it
speaks the [Engine Integration Contract](https://github.com/John-Cusack/solidmind-cad/blob/main/docs/engine-contract.md)
over a TCP socket and shares nothing with core but that contract.

## Running it

```bash
python3 -m {primary}.bridge_server --port {port}{launch_args}
```

Core finds it through a descriptor — no registration, no core edit:

```toml
# ~/.solidmind/engines.d/{engine}.toml
name = "{engine}"
port = {port}
launch = ["python3", "-m", "{primary}.bridge_server", "--port", "${{PORT}}"]
cwd = "~/repos/{name}"
when_to_use = "…what this engine is good at, in one line for the copilot…"
```

## Proving conformance

The TCK is vendored here so this repo's CI can run it without core:

```bash
python3 -m {primary}.bridge_server --port {port}{launch_args} &
python3 -m tck --port {port}
```

Exit code 0 means conformant. Attach its output to any bug report — that is
the support boundary between this engine and core.

Re-vendor `tck/` and `schemas/` from core whenever the contract moves; the
handshake's `contract_versions_supported` is what tells you it has.

## Boundaries

* This repo must never import `server.*` — `tests/test_import_boundary.py`
  enforces it, because a single reverse import re-couples the two interpreters.
* Core emits only the canonical sim package, meshes and a courtesy URDF. Any
  vendor dialect this engine needs (SDF, MJCF, autopilot params) is compiled
  **here**, at load time.
* Nothing in this repo may assume another engine exists.

## Tests

```bash
python3 -m unittest
```

Tests that need the real engine installed skip themselves when it isn't.
"""

CI = """name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  tests:
    name: Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[tck]"
      - name: Unit tests
        run: python -m unittest --verbose

  conformance:
    name: Contract conformance (TCK)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[tck]"
      - name: Start the engine
        run: |
          python -m {primary}.bridge_server --port {port}{launch_args} &
          python - <<'READY'
          import socket, sys, time
          deadline = time.monotonic() + 30
          while time.monotonic() < deadline:
              try:
                  socket.create_connection(("127.0.0.1", {port}), timeout=0.2).close()
                  sys.exit(0)
              except OSError:
                  time.sleep(0.2)
          sys.exit("engine did not start")
          READY
      - name: Run the TCK
        run: python -m tck --port {port}

  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: python -m pip install "ruff>=0.6,<1"
      - run: ruff check .
      - run: ruff format --check .
"""

IMPORT_GUARD = '''"""This engine must never import core.

The whole point of the split is two repositories with two interpreters and one
contract between them.  A single ``import server.…`` re-couples the
environments and un-does it, so the check is static and runs in CI.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGES = ({packages_tuple})
_FORBIDDEN = ("server",)


def _imported_roots(path: Path) -> set[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add((node.module.split(".")[0], node.lineno))
    return found


class TestNoCoreImports(unittest.TestCase):
    def test_no_server_imports(self) -> None:
        violations: list[str] = []
        for package in _PACKAGES:
            for path in sorted((_REPO_ROOT / package).rglob("*.py")):
                for root, lineno in _imported_roots(path):
                    if root in _FORBIDDEN:
                        violations.append(f"{{path.relative_to(_REPO_ROOT)}}:{{lineno}} imports {{root!r}}")
        self.assertEqual(
            violations,
            [],
            "This engine must not import core:\\n  " + "\\n  ".join(violations),
        )

    def test_the_scan_saw_something(self) -> None:
        """Guard the guard — but a C++ package legitimately has no Python."""
        scanned = [p for pkg in _PACKAGES for p in (_REPO_ROOT / pkg).rglob("*.py")]
        self.assertTrue(scanned, f"no python files scanned in {{_PACKAGES}}")


if __name__ == "__main__":
    unittest.main()
'''

GITIGNORE = """__pycache__/
*.py[cod]
.venv/
build/
dist/
*.egg-info/
.ruff_cache/
"""

#: Contract ports, matching core's default descriptors.
PORTS = {
    "isaac_bridge": 9878,
    "gazebo_bridge": 9879,
    "chrono_daemon": 9877,
    "chrono_bridge": 9877,
    "rl_training": 0,
}


def _drop_tck(target: Path) -> None:
    """The RL pipeline speaks no contract, so it carries no conformance kit."""
    import shutil

    shutil.rmtree(target / "tck", ignore_errors=True)
    shutil.rmtree(target / "schemas", ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a split-out engine repo")
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--packages", required=True, help="space-separated package names")
    parser.add_argument("--primary", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument(
        "--launch-args",
        default="",
        help="Extra argv the bridge needs in CI (e.g. Gazebo's --runtime stub)",
    )
    args = parser.parse_args()

    packages = args.packages.split()
    engine = args.name.replace("solidmind-engine-", "").replace("solidmind-", "")
    port = PORTS.get(args.primary, 9880)

    fields = {
        "name": args.name,
        "description": args.description,
        "primary": args.primary,
        "engine": engine,
        "port": port,
        "launch_args": (" " + args.launch_args) if args.launch_args else "",
        "packages_toml": ", ".join(f'"{p}"' for p in packages),
        "packages_tuple": ", ".join(f'"{p}"' for p in packages)
        + ("," if len(packages) == 1 else ""),
    }

    target: Path = args.target
    (target / "pyproject.toml").write_text(PYPROJECT.format(**fields))
    (target / "README.md").write_text(README.format(**fields))
    (target / ".gitignore").write_text(GITIGNORE)
    (target / "tests").mkdir(exist_ok=True)
    # unittest discovery needs the package marker core's tests/ also carries.
    (target / "tests" / "__init__.py").write_text("")
    (target / "tests" / "test_import_boundary.py").write_text(IMPORT_GUARD.format(**fields))

    # The RL pipeline is not an engine — no bridge server, so no conformance
    # job.  It still gets tests, lint and the import guard.
    workflows = target / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    ci = CI.format(**fields)
    if args.primary == "rl_training":
        head, _, tail = ci.partition("  conformance:")
        ci = head + "  lint:" + tail.partition("  lint:")[2]
        (target / "tck").exists() and _drop_tck(target)
    (workflows / "ci.yml").write_text(ci)

    print(f"    scaffolded {args.name} (port {port})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
