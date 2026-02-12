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
- `main.py` — MCP JSON-RPC stdio server, registers cad.*, mfg.*, and me.* tools
- `freecad_client.py` — TCP socket client connecting to FreeCAD addon, retry/reconnect logic
- `tools_cad.py` — CAD MCP tool implementations (cad.new_document, cad.sketch, cad.pad, cad.pocket, cad.hole, cad.fillet, cad.chamfer, cad.get_selection, cad.get_model_tree, cad.undo, cad.export)
- `tools_mfg.py` — Manufacturing readiness tools (mfg.set_property, mfg.readiness_check, mfg.export_rfq)
- `tools_me.py` — ME design-loop tools (routing, constraint sheets, deterministic validation, traceability, risk gates)
- `prompts.py` — System prompts including `cad_copilot_system` for live co-pilot mode
- `models.py` — Finding, Severity, ToolError, ConversationSignals

### Tool groups

| Group | Tools | Purpose |
|-------|-------|---------|
| `cad.*` | new_document, new_body, sketch, pad, pocket, hole, fillet, chamfer, get_selection, get_model_tree, undo, export | Drive FreeCAD PartDesign |
| `mfg.*` | set_property, readiness_check, export_rfq | Manufacturing readiness (on-demand) |
| `me.*` | list_domain_tags, list_archetypes, get_archetype_card, route_request, instantiate_constraint_sheet, validate_constraint_sheet, build_traceability, apply_risk_gates, design_loop, get_knowledge_policy | Deterministic ME preflight loop |

### Interaction flow

1. User describes part → LLM decides whether ME preflight is needed.
2. If needed, LLM calls `me.design_loop` once, then uses constraints/findings to guide CAD.
3. LLM calls `cad.new_document` → `cad.new_body` → `cad.sketch` → `cad.pad`
4. User clicks face/edge in FreeCAD → LLM calls `cad.get_selection` → sees geometry context
5. User says "add holes here" → LLM calls `cad.hole` with face reference
6. User says "check manufacturing readiness" → LLM calls `mfg.readiness_check`

### Automatic ME preflight policy

- Use `me.*` only when the model judges it necessary.
- Skip ME preflight for simple geometry (spacers, simple brackets, simple blocks/plates) unless the user asks.
- Trigger ME preflight for high-risk/specialized requests (rotors/turbines/gears, high-temp service, explicit signoff/traceability, ambiguous critical constraints).
- If `me.design_loop` returns no archetype match, proceed with `cad.*` and ask focused clarification questions.
- Do not rerun `me.design_loop` after every edit; rerun only when requirements materially change.

## Critical Conventions

- **Style:** 4-space indent, type hints everywhere, `from __future__ import annotations` at top of modules. `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants. Frozen dataclasses with `__slots__` for models.
- **Testing:** `unittest` framework. Unit tests in `tests/test_*.py`. CAD tools tested with mocked FreeCAD client.
- **Error handling:** CAD tools return `{"ok": false, "error": {"code": "...", "message": "..."}}` on failure. Connection errors and command errors are caught and wrapped.
- **Socket protocol:** Newline-delimited JSON. Commands: `{"cmd": "...", "args": {...}}`. Responses: `{"ok": true/false, "result": ..., "error": "..."}`.
- **Commits:** Short imperative subjects; optional scope prefixes (`server:`, `addon:`, `schemas:`).
