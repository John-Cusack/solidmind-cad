# Contributing to SolidMind CAD

Thanks for your interest in contributing! This guide covers the basics.

## Development Setup

1. Clone the repository
2. Create a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`
3. Install in editable mode: `pip install -e .`
4. Copy `.mcp.json.example` to `.mcp.json` and adjust paths for your system

## Running Tests

```bash
# All tests
python3 -m unittest

# Single test module
python3 -m unittest tests.test_tools_cad
```

## Code Style

- 4-space indentation
- Type hints everywhere
- `from __future__ import annotations` at the top of every module
- `snake_case` for functions/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants
- Frozen dataclasses with `__slots__` for data models

## Making Changes

1. Create a feature branch from `main`
2. Make your changes, ensuring tests pass
3. Write clear commit messages with short imperative subjects (e.g., `server: add timeout to socket reads`)
4. Open a pull request with a description of what and why

## Architecture Overview

See `CLAUDE.md` for a detailed architecture description, tool groups, and module layout.

## Runtime Components

- **[FreeCAD 1.0.2](https://github.com/FreeCAD/FreeCAD/releases/tag/1.0.2) AppImage** — the foundation; all CAD modeling runs inside FreeCAD (see README for install)

Optional simulation/knowledge backends:

- **Isaac bridge** — requires NVIDIA Isaac Sim
- **Gazebo bridge** — requires Gazebo Harmonic
- **Chrono daemon** — requires Project Chrono (C++ build)
- **Knowledge backend** — requires LanceDB + Docling (pip installable)

Unit tests use a mocked FreeCAD client, so most development doesn't require FreeCAD running — but any live modeling or integration testing does.
