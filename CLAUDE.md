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
                               │                             runs inside FreeCAD GUI
                               ├─ LanceDB (in-process, me_knowledge/lancedb/)
                               ├─ Docling (in-process, pip package)
                               └─ Ollama (optional, for GPU embeddings)
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
- `tools_me.py` — ME design-loop tools (deterministic validation, traceability, risk gates)
- `tools_knowledge.py` — Knowledge management tools (extract, ingest, search via LanceDB)
- `knowledge_store.py` — In-process knowledge store (LanceDB + Docling, module-level singleton)
- `prompts.py` — System prompts including `cad_copilot_system` for live co-pilot mode
- `models.py` — Finding, Severity, ToolError, ConversationSignals

### Tool groups

| Group | Tools | Purpose |
|-------|-------|---------|
| `cad.*` | new_document, new_body, sketch, pad, pocket, hole, fillet, chamfer, get_selection, get_model_tree, undo, export | Drive FreeCAD PartDesign |
| `mfg.*` | set_property, readiness_check, export_rfq | Manufacturing readiness (on-demand) |
| `me.*` | validate_constraints, build_traceability, apply_risk_gates, design_loop, list_validators | Deterministic ME preflight (validators + risk gates) |
| `knowledge.*` | extract, ingest, ingest_status, search, status | Knowledge base — hybrid search, PDF extraction, document ingestion (LanceDB + Docling) |

### Sketch element types

`cad.sketch` supports **all** element types: `rect`, `circle`, `line`, `arc`, **`spline`** (B-spline curves from control points with degree/weights/periodic options). Splines are fully implemented and must be used for smooth contours, airfoils, blade profiles, and organic shapes — never approximate with line segments.

### Interaction flow

1. User describes part → LLM decides whether ME preflight is needed.
2. If needed, LLM reads research notes + does web research, constructs a constraint dict, calls `me.design_loop(constraints)`.
3. LLM reviews validation findings + risk gates, then builds geometry with `cad.*` tools.
4. LLM calls `cad.new_document` → `cad.new_body` → `cad.sketch` → `cad.pad`
5. User clicks face/edge in FreeCAD → LLM calls `cad.get_selection` → sees geometry context
6. User says "add holes here" → LLM calls `cad.hole` with face reference
7. User says "check manufacturing readiness" → LLM calls `mfg.readiness_check`
8. User provides a PDF → LLM calls `knowledge.extract(file_path)` to read it, then `knowledge.ingest(path)` to index for future sessions
9. LLM researches a topic → `knowledge.search(query)` first, then WebSearch/WebFetch for gaps

### Automatic ME preflight policy

- Use `me.*` only when the model judges it necessary.
- Skip ME preflight for simple geometry (spacers, simple brackets, simple blocks/plates) unless the user asks.
- Trigger ME preflight for high-risk/specialized requests (rotors/turbines/gears, high-temp service, explicit signoff/traceability, ambiguous critical constraints).
- Do not rerun `me.design_loop` after every edit; rerun only when requirements materially change.
- The LLM constructs constraint dicts from its own engineering knowledge + research notes, then passes them to `me.design_loop(constraints)` or `me.validate_constraints(constraint_sheet)` for deterministic validation.
- ME constraints inform geometry decisions but don't dictate them — the LLM applies its own engineering knowledge to translate constraints into CAD operations.
- For unfamiliar or specialized geometry, check `me_knowledge/notes/` for existing research, then search for real engineering references (NASA technical reports, textbook descriptions, design guidelines) and read them before building.
- Write research findings to `me_knowledge/notes/<topic_slug>.md` so they persist across sessions and can be reused.

### Self-assessment and visual verification

- After building complex geometry, critically examine the verification screenshots.
- Compare what you see to what the part should actually look like.
- Be explicit about confidence levels: "I'm confident about X" vs "I'm uncertain about Y".
- For specialized parts (turbines, gears, airfoils), acknowledge uncertainty and ask for feedback.
- Do not declare a complex part "complete" without examining the result and inviting user feedback.

## Critical Conventions

### Knowledge backend (LanceDB + Docling)

The knowledge backend runs fully in-process — no Docker required. **LanceDB** provides hybrid search (vector + Tantivy FTS), **Docling** (pip) handles PDF/DOCX extraction, and embeddings come from **Ollama** (GPU) or **sentence-transformers** (CPU fallback). When dependencies are missing, `knowledge.*` tools gracefully fall back to listing local `me_knowledge/notes/` files.

```bash
# Batch ingest files
python scripts/ingest_knowledge.py me_knowledge/notes/
python scripts/ingest_knowledge.py ~/some-pdfs/
```

Optional environment variables:
- `OLLAMA_URL` — Ollama base URL for GPU-accelerated embeddings (e.g., `http://localhost:11434`)
- `EMBEDDING_MODEL` — Model name (default: `nomic-embed-text` for Ollama, `all-MiniLM-L6-v2` for sentence-transformers)
- `KNOWLEDGE_DB_PATH` — Override LanceDB storage path (default: `me_knowledge/lancedb/`)

## Critical Conventions

- **Style:** 4-space indent, type hints everywhere, `from __future__ import annotations` at top of modules. `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants. Frozen dataclasses with `__slots__` for models.
- **Testing:** `unittest` framework. Unit tests in `tests/test_*.py`. CAD tools tested with mocked FreeCAD client.
- **Error handling:** CAD tools return `{"ok": false, "error": {"code": "...", "message": "..."}}` on failure. Connection errors and command errors are caught and wrapped.
- **Socket protocol:** Newline-delimited JSON. Commands: `{"cmd": "...", "args": {...}}`. Responses: `{"ok": true/false, "result": ..., "error": "..."}`.
- **Commits:** Short imperative subjects; optional scope prefixes (`server:`, `addon:`, `schemas:`).
