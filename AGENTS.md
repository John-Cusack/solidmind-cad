# Repository Guidelines

## Project Structure & Module Organization
- `server/`: MCP stdio JSON-RPC bridge server (`server/main.py`) and tool implementations (`tools_cad.py`, `tools_mfg.py`, `tools_me.py`, `tools_motion.py`, `tools_study.py`, `tools_design.py`, `tools_knowledge.py`, `tools_fastener.py`).
- `freecad_addon/`: FreeCAD GUI addon — socket server, command handlers, selection observer.
- `isaac_bridge/`: Optional Isaac Sim sidecar for GPU physics simulation and teleop.
- `gazebo_bridge/`: Optional Gazebo sidecar for CPU physics simulation and teleop.
- `chrono_daemon/`: Optional C++ Project Chrono daemon for multibody simulation.
- `rl_training/`: Reinforcement learning training pipeline (Isaac Lab + RSL-RL).
- `geometry/`: Rust-backed parametric geometry generators (gears, propellers).
- `tests/`: `unittest` suite (`tests/test_*.py`).
- `scripts/`: Developer and simulation utilities.
- `me_knowledge/`: Engineering research notes and knowledge base artifacts.

## Build, Test, and Development Commands
Requires Python >= 3.12.

- Install (editable): `python3 -m pip install -e .`
- Run the MCP server over stdio: `python3 -m server.main`
- Run all unit tests: `python3 -m unittest`
- Run a single test module: `python3 -m unittest tests.test_tools_cad`

## Coding Style & Naming Conventions
- 4-space indentation, type hints, and `from __future__ import annotations` are the norm.
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Frozen dataclasses with `__slots__` for data models.

## Testing Guidelines
- Framework: `unittest` (run via `python3 -m unittest`).
- Naming: `tests/test_*.py`. CAD tools tested with mocked FreeCAD client.

## Commit & Pull Request Guidelines
- Prefer short, imperative subjects; optional scope prefixes like `server:`, `addon:`, `scripts:`.
- PRs should include: what changed and why, and test results.

## Further Reading
See `CLAUDE.md` for detailed architecture, tool groups, interaction flows, and design pipeline documentation.
