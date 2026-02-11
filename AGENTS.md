# Repository Guidelines

## Project Structure & Module Organization
- `server/`: MCP stdio JSON-RPC server (`server/main.py`) and deterministic tool layer (`server/tools.py`).
- `schemas/`: JSON Schema files used for shape validation.
- `question_bank/`: YAML question bank (`cnc.yml`) and glossary.
- `tests/`: `unittest` suite; golden workflows in `tests/transcripts/`.
- `scripts/`: developer utilities (transcript replay, optional FreeCAD export).
- `examples/`: sample finalized specs (e.g., `examples/cnc/L2.json`).
- `SPEC_GUIDE*.md`: design/spec documentation.

## Build, Test, and Development Commands
Requires Python >= 3.12.

- Install (editable, adds `mcp-spec-gatherer` entrypoint):
  `python3 -m pip install -e .`
- Run the MCP server over stdio:
  `python3 -m server.main` (or `mcp-spec-gatherer` after install)
- Run unit tests:
  `python3 -m unittest`
- Replay a golden transcript:
  `python3 scripts/replay_transcript.py tests/transcripts/cnc_L2.yml`
- Optional: generate a minimal CAD stub from a finalized spec (requires FreeCAD):
  `python3 scripts/freecad_from_spec.py --spec examples/cnc/L2.json --out /tmp/part.step`

## Coding Style & Naming Conventions
- 4-space indentation, type hints, and `from __future__ import annotations` are the norm.
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Preserve determinism (hashes, ordering, timestamps). If tool outputs change, update tests and transcripts.

## Testing Guidelines
- Framework: `unittest` (run via `python3 -m unittest`).
- Naming: `tests/test_*.py`. Add focused unit tests for rules/validation and transcripts for end-to-end tool behavior.

## Commit & Pull Request Guidelines
- Git history is currently minimal and informal ("first", "Initial commit"); no enforced convention yet.
- Prefer short, imperative subjects; optional scope prefixes like `server:`, `schemas:`, `scripts:`.
- PRs should include: what changed and why, test results, and (when outputs change) updated `tests/transcripts/*.yml` plus a compatibility note.

