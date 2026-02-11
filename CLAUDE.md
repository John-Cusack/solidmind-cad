# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SolidMind CAD — a FreeCAD co-pilot powered by MCP (Model Context Protocol). The LLM drives FreeCAD's PartDesign workbench directly through MCP tools while the user sees the model updating live. Manufacturing readiness checks are available on-demand rather than through a forced interview. Python >= 3.12.

## Commands

```bash
# Install (editable)
python3 -m pip install -e .

# Run MCP server (bridge) over stdio
python3 -m server.main

# Run all tests
python3 -m unittest

# Run a single test module
python3 -m unittest tests.test_tools_cad

# Start FreeCAD addon (inside FreeCAD Python console)
import freecad_addon; freecad_addon.start()

# Replay a golden transcript (legacy)
python3 scripts/replay_transcript.py tests/transcripts/cnc_L2.yml
```

## Architecture

```
Claude Code CLI ──stdio──▶ MCP Bridge Server ──TCP socket──▶ FreeCAD Addon
                           (server/main.py)                  (freecad_addon/)
                                                             runs inside FreeCAD GUI
```

**MCP bridge server** (`server/main.py`): Launched by Claude Code via stdio. Connects to FreeCAD addon over TCP socket (localhost:9876). Translates MCP tool calls into FreeCAD commands and FreeCAD selection into MCP responses.

**FreeCAD addon** (`freecad_addon/`): Runs inside FreeCAD's GUI process. Socket server in a background thread accepts JSON commands, executes FreeCAD Python API, returns results. Selection observer tracks user clicks on geometry.

### Key modules

**`freecad_addon/`:**
- `__init__.py` — Package init, `start()` / `stop()` entry points
- `protocol.py` — Newline-delimited JSON command/response protocol
- `socket_server.py` — TCP server on localhost:9876, background thread, command dispatch
- `selection_observer.py` — FreeCADGui.Selection observer, tracks clicked faces/edges/vertices
- `commands.py` — FreeCAD API command handlers (document, body, sketch, pad, pocket, hole, fillet, chamfer, export, undo, selection, model tree)

**`server/`:**
- `main.py` — MCP JSON-RPC stdio server, registers cad.*, mfg.*, and legacy spec.* tools
- `freecad_client.py` — TCP socket client connecting to FreeCAD addon, retry/reconnect logic
- `tools_cad.py` — CAD MCP tool implementations (cad.new_document, cad.sketch, cad.pad, cad.pocket, cad.hole, cad.fillet, cad.chamfer, cad.get_selection, cad.get_model_tree, cad.undo, cad.export)
- `tools_mfg.py` — Manufacturing readiness tools (mfg.set_property, mfg.readiness_check, mfg.export_rfq)
- `tools.py` — Legacy spec interview tool implementations (spec.select_schema, etc.)
- `prompts.py` — System prompts including `cad_copilot_system` for live co-pilot mode
- `models.py` — Finding, Severity, ToolError, ConversationSignals
- `validation.py` — JSON Schema validation + coverage scoring + rule execution
- `rules_cnc.py` / `rules_print_3d.py` — Process-specific validation rules

### Tool groups

| Group | Tools | Purpose |
|-------|-------|---------|
| `cad.*` | new_document, new_body, sketch, pad, pocket, hole, fillet, chamfer, get_selection, get_model_tree, undo, export | Drive FreeCAD PartDesign |
| `mfg.*` | set_property, readiness_check, export_rfq | Manufacturing readiness (on-demand) |
| `spec.*` | select_schema, apply_answer, validate, next_question, finalize, export_brief, export_rfq_summary, generate_cad | Legacy spec interview |

### Interaction flow

1. User describes part → LLM calls `cad.new_document` → `cad.new_body` → `cad.sketch` → `cad.pad`
2. User clicks face/edge in FreeCAD → LLM calls `cad.get_selection` → sees geometry context
3. User says "add holes here" → LLM calls `cad.hole` with face reference
4. User says "check manufacturing readiness" → LLM calls `mfg.readiness_check`

## Critical Conventions

- **Style:** 4-space indent, type hints everywhere, `from __future__ import annotations` at top of modules. `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants. Frozen dataclasses with `__slots__` for models.
- **Testing:** `unittest` framework. Unit tests in `tests/test_*.py`. CAD tools tested with mocked FreeCAD client. End-to-end tests via golden transcripts in `tests/transcripts/` (for legacy spec tools).
- **Error handling:** CAD tools return `{"ok": false, "error": {"code": "...", "message": "..."}}` on failure. Connection errors and command errors are caught and wrapped.
- **Socket protocol:** Newline-delimited JSON. Commands: `{"cmd": "...", "args": {...}}`. Responses: `{"ok": true/false, "result": ..., "error": "..."}`.
- **Commits:** Short imperative subjects; optional scope prefixes (`server:`, `addon:`, `schemas:`).
