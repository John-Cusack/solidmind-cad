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

## Skills
A skill is a set of local instructions to follow that is stored in a `SKILL.md` file. Below is the list of skills that can be used. Each entry includes a name, description, and file path so you can open the source for full instructions when using a specific skill.
### Available skills
- skill-creator: Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Codex's capabilities with specialized knowledge, workflows, or tool integrations. (file: /home/john/.codex/skills/.system/skill-creator/SKILL.md)
- skill-installer: Install Codex skills into $CODEX_HOME/skills from a curated list or a GitHub repo path. Use when a user asks to list installable skills, install a curated skill, or install a skill from another repo (including private repos). (file: /home/john/.codex/skills/.system/skill-installer/SKILL.md)
### How to use skills
- Discovery: The list above is the skills available in this session (name + description + file path). Skill bodies live on disk at the listed paths.
- Trigger rules: If the user names a skill (with `$SkillName` or plain text) OR the task clearly matches a skill's description shown above, you must use that skill for that turn. Multiple mentions mean use them all. Do not carry skills across turns unless re-mentioned.
- Missing/blocked: If a named skill isn't in the list or the path can't be read, say so briefly and continue with the best fallback.
- How to use a skill (progressive disclosure):
  1) After deciding to use a skill, open its `SKILL.md`. Read only enough to follow the workflow.
  2) When `SKILL.md` references relative paths (e.g., `scripts/foo.py`), resolve them relative to the skill directory listed above first, and only consider other paths if needed.
  3) If `SKILL.md` points to extra folders such as `references/`, load only the specific files needed for the request; don't bulk-load everything.
  4) If `scripts/` exist, prefer running or patching them instead of retyping large code blocks.
  5) If `assets/` or templates exist, reuse them instead of recreating from scratch.
- Coordination and sequencing:
  - If multiple skills apply, choose the minimal set that covers the request and state the order you'll use them.
  - Announce which skill(s) you're using and why (one short line). If you skip an obvious skill, say why.
- Context hygiene:
  - Keep context small: summarize long sections instead of pasting them; only load extra files when needed.
  - Avoid deep reference-chasing: prefer opening only files directly linked from `SKILL.md` unless you're blocked.
  - When variants exist (frameworks, providers, domains), pick only the relevant reference file(s) and note that choice.
- Safety and fallback: If a skill can't be applied cleanly (missing files, unclear instructions), state the issue, pick the next-best approach, and continue.
