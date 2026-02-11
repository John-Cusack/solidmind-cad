# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MCP Spec Gatherer — a deterministic, stateless MCP (Model Context Protocol) server for gathering CNC part specifications. The server exposes 7 tools over stdio JSON-RPC that walk users through a question-driven interview, validate specs against JSON Schema, and produce finalized, hashed specifications. Python >= 3.12.

## Commands

```bash
# Install (editable, creates mcp-spec-gatherer entrypoint)
python3 -m pip install -e .

# Run MCP server over stdio
python3 -m server.main

# Run all tests
python3 -m unittest

# Run a single test module
python3 -m unittest tests.test_validation

# Replay a golden transcript
python3 scripts/replay_transcript.py tests/transcripts/cnc_L2.yml
```

## Architecture

**Stateless tool layer:** The host passes the full `spec_draft` on every tool call — the server holds no session state.

**Core flow:** `select_schema` → (`next_question` → user answer → `apply_answer` → `validate`) loop → `finalize` → `export_brief`/`export_rfq_summary`

**Key modules in `server/`:**
- `main.py` — MCP JSON-RPC stdio server (Content-Length framing)
- `tools.py` — All 7 tool implementations (`spec.select_schema`, `spec.apply_answer`, `spec.validate`, `spec.next_question`, `spec.finalize`, `spec.export_brief`, `spec.export_rfq_summary`)
- `validation.py` — JSON Schema (Draft 2020-12) validation + coverage scoring + rule execution
- `question_bank.py` — YAML question bank loader and next-question selection
- `rules_cnc.py` — CNC-specific validation rules returning `Finding` objects (blockers vs warnings)
- `json_pointer.py` — RFC 6901 JSON Pointer (get/set/remove) used by `apply_answer`
- `jcs.py` — RFC 8785 JSON Canonicalization for deterministic SHA-256 hashing
- `spec_draft.py` — Deep copy, default injection, internal field stripping

**Data files:**
- `schemas/cnc.schema.json` — CNC spec shape validation schema
- `question_bank/cnc.yml` — 10 questions with per-maturity required/weight maps
- `examples/cnc/` — Finalized spec examples at L1/L2/L3
- `tests/transcripts/` — Golden YAML transcripts for end-to-end determinism checks

**Maturity levels:** L1 (concept, 60% coverage), L2 (prototype, 80%), L3 (production, 90%). Questions, rules, and thresholds are gated by maturity.

## Critical Conventions

- **Determinism is paramount.** All tools are pure functions. Hashes, field ordering, timestamps, and question selection must be reproducible. If tool outputs change, update both tests and golden transcripts.
- **Style:** 4-space indent, type hints everywhere, `from __future__ import annotations` at top of modules. `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants. Frozen dataclasses with `__slots__` for models.
- **Testing:** `unittest` framework. Unit tests in `tests/test_*.py`, end-to-end behavior validated via golden transcripts in `tests/transcripts/`. When tool behavior changes, update transcripts and note compatibility impact.
- **Spec mutations:** Always via JSON Pointer (`spec.apply_answer`), never direct dict mutation. All mutations are logged in `_audit`.
- **Commits:** Short imperative subjects; optional scope prefixes (`server:`, `schemas:`, `scripts:`). PRs should include updated transcripts when outputs change.
