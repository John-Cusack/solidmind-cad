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
                           (server/main.py)     :9876         (freecad_addon/)
                               │                             runs inside FreeCAD GUI
                               ├─ TCP socket :9877 ──▶ Chrono Daemon (chrono_daemon/)
                               │                      C++ MBS simulation (optional)
                               ├─ LanceDB (in-process, me_knowledge/lancedb/)
                               ├─ Docling (in-process, pip package)
                               └─ Ollama (optional, for GPU embeddings)
```

**MCP bridge server** (`server/main.py`): Launched by Claude Code via stdio. Connects to FreeCAD addon over TCP socket (localhost:9876). Translates MCP tool calls into FreeCAD commands and FreeCAD selection into MCP responses.

**FreeCAD addon** (`freecad_addon/`): Runs inside FreeCAD's GUI process. Socket server in a background thread accepts JSON commands, executes FreeCAD Python API, returns results. Selection observer tracks user clicks on geometry.

**Chrono daemon** (`chrono_daemon/`): Optional standalone C++ binary for Tier 3 dynamic simulation via Project Chrono. TCP socket server on localhost:9877 (same JSON protocol as FreeCAD addon). Builds Chrono multibody systems from mechanism definitions, runs time-domain simulations, returns time-series results. Only needed for `motion.simulate` — Tier 1 analytical validation works without it. See `chrono_daemon/README.md` for build instructions.

**Isaac bridge** (`isaac_bridge/`): Optional Python TCP sidecar on localhost:9878 for Tier 3 `backend=isaac` simulation and teleop lifecycle. Exposes newline-delimited JSON commands (`ping`, `simulate`, `teleop_*`). Start with `scripts/run_isaac_bridge.sh`. v1 supported joints: `revolute`, `prismatic`, `fixed`; unsupported joints return `UNSUPPORTED_JOINT_TYPE`. Teleop uses a `Controller` protocol for pluggable actuation — currently `HexapodTripodController` (1-DOF tripod gait with slew filtering, yaw differential, height offset). Profile keys configure the controller (amplitude, stride frequency, slew rates, etc.).

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
- `tools_study.py` — Parametric study tools (create, run, status, results, cancel, list, get_variant)
- `tools_motion.py` — Motion validation tools (Tier 1: define_mechanism, validate, propagate_motion, check_gear_train; Tier 2: create_assembly, drive_joint, check_interference; Tier 3: simulate, teleop_start/command/state/stop)
- `motion_models.py` — Mechanism data models (PartNode, JointEdge, DriveCondition, Mechanism)
- `motion_store.py` — Session-scoped mechanism storage (UUID handles)
- `motion_validators.py` — Analytical validators (gear ratio, speed/torque propagation, DOF, Grashof, power conservation)
- `chrono_client.py` — TCP client to Chrono daemon on localhost:9877 (Tier 3 dynamic simulation)
- `isaac_client.py` — TCP client to Isaac bridge on localhost:9878 (Tier 3 dynamic simulation + teleop)
- `study_models.py` — Study data models (Study, DesignVariable, Variant, SolverConfig, ObjectiveConfig)
- `study_store.py` — JSON-file persistence for studies in `studies/<study_id>/`
- `study_runner.py` — Background subprocess runner (coarse sweep → refine → rank)
- `study_solvers.py` — Solver adapters (MockSolver, BEMTXfoilSolver stub, OpenFOAMSolver stub, ChronoSolver)
- `tools_knowledge.py` — Knowledge management tools (extract, ingest, search via LanceDB)
- `knowledge_store.py` — In-process knowledge store (LanceDB + Docling, module-level singleton)
- `prompts.py` — System prompts including `cad_copilot_system` for live co-pilot mode
- `models.py` — Finding, Severity, ToolError, ConversationSignals

**`isaac_bridge/`:**
- `bridge_server.py` — TCP server + main-thread pump loop (Kit event loop, teleop ticking)
- `runtime_isaac.py` — Isaac runtime: URDF import, simulation, teleop lifecycle, tick_teleop, DOF mapping
- `models.py` — `TeleopConfig` (validated profile), `TeleopState`, `Controller` protocol, `SimulationSession`, `URDFImportConfig`
- `controllers.py` — `HexapodTripodController` (1-DOF tripod gait), `PolicyController` (RL residual blending), `create_controller()` registry, `clamp_targets()` utility
- `keyboard_teleop.py` — `KeyboardTeleopMapper` (key→command mapping, no Isaac dependency)

**`scripts/`** (teleop-related):
- `run_isaac_bridge.sh` — Launch the Isaac bridge sidecar
- `isaac_keyboard_teleop.py` — Standalone keyboard teleop client (W/A/S/D/Q/E, 20 Hz, raw terminal)
- `smoke_test_isaac.py` — TCP smoke test client for the Isaac bridge

### Tool groups

| Group | Tools | Purpose |
|-------|-------|---------|
| `cad.*` | new_document, new_body, sketch, pad, pocket, hole, fillet, chamfer, create_primitive, create_primitives, get_selection, get_model_tree, undo, export | Drive FreeCAD PartDesign |
| `mfg.*` | set_property, readiness_check, export_rfq | Manufacturing readiness (on-demand) |
| `me.*` | validate_constraints, build_traceability, apply_risk_gates, design_loop, list_validators | Deterministic ME preflight (validators + risk gates) |
| `knowledge.*` | extract, ingest, ingest_status, search, status | Knowledge base — hybrid search, PDF extraction, document ingestion (LanceDB + Docling) |
| `study.*` | create, run, status, results, cancel, list, get_variant | Parametric design optimization (sweep variables, run solvers, rank results) |
| `motion.*` | define_mechanism, list_mechanisms, validate, propagate_motion, check_gear_train, create_assembly, drive_joint, check_interference, simulate, teleop_start, teleop_command, teleop_state, teleop_stop | Motion validation pipeline — analytical (Tier 1), kinematic via FreeCAD Assembly (Tier 2), dynamic via Isaac/Chrono (Tier 3) |

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
10. User asks to optimize a design → LLM uses `study.*` tools to sweep design space, then builds the winner

### Parametric study policy

The `study.*` tools automate design space exploration — sweeping variables, running solvers, and ranking results. The key distinction is **intent**: is the user asking to EXPLORE/OPTIMIZE or to BUILD a specific thing?

**Use `study.*` when:**
- User explicitly asks to optimize, sweep, compare, or explore designs
- Task has performance targets (thrust, efficiency, lift/drag) with unknown geometry parameters
- User says "find the best", "parametric study", "what blade angle is optimal"
- Multiple design variables interact and the best combination isn't obvious

**Skip `study.*` when:**
- User wants ONE specific thing built ("make me a pen holder", "design a bracket")
- Geometry is fully specified — dimensions, shape, and features are all given
- Simple functional parts with no performance targets
- Quick modifications to existing models

**When ambiguous** (e.g., "design a drone propeller"): ask the user if they want to explore the design space or build a specific configuration.

**Workflow:** Plan → `study.create` → `study.run` → `study.status` → `study.results` → **learn** → build winner with `cad.*`

**Planning stage (step 1 — before defining any study):**
1. `knowledge.search('<part_type> study')` — look for prior study notes FIRST
2. If prior notes exist: use their optimal ranges for tighter bounds, their winners as `pinned_values`, drop variables they found insensitive, add variables they flagged for future exploration
3. If no prior notes: `knowledge.search` + WebSearch for engineering references to identify variables and starting ranges
4. State reasoning: "Prior study found angle 8-12 deg optimal, narrowing from 0-45"

**Learning cycle (mandatory after every study):**
After `study.results`, distill findings into `me_knowledge/notes/<part_type>_study_<date>.md`:
- What variables were swept and what ranges
- Which parameters had the biggest effect (sensitivity)
- Optimal ranges found and the winning design's params
- Constraint interactions that shaped the feasible region
- What to do differently next time

Then `knowledge.ingest(path=...)` to index it. In future sessions, `knowledge.search` surfaces prior study findings BEFORE defining new studies — so `pinned_values` and variable ranges start from prior learnings instead of from scratch. Each study makes the next one smarter.

### Motion validation policy

Motion validation is **user-initiated and human-gated**. It applies only to mechanisms
with moving parts (gears, linkages, cams, belt drives). Skip for static parts
(brackets, enclosures, pen holders, spacers).

**When the user asks to validate a mechanism:**
1. Research expected performance via knowledge.search + engineering knowledge
2. Define mechanism with motion.define_mechanism (derive expected_outputs from research)
3. Run Tier 1 (analytical): motion.validate + motion.propagate_motion
4. Report results to user. Wait for user approval before escalating.
5. If user requests Tier 2: motion.create_assembly + motion.drive_joint + motion.check_interference (requires FreeCAD Assembly workbench)
6. If user requests Tier 3: motion.simulate with backend selection:
   - `backend=isaac` requires Isaac bridge sidecar (`scripts/run_isaac_bridge.sh`)
   - `backend=chrono` requires Chrono daemon

**When to suggest validation (but always let user decide):**
- After building a gear train, linkage, or cam mechanism
- When the user specifies performance targets (ratio, torque, speed)
- When the mechanism is complex enough that errors aren't visually obvious

**Never auto-run motion validation.** Always present it as a suggestion:
"I've built the gearbox. Would you like me to validate the gear ratios and torque?"

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
