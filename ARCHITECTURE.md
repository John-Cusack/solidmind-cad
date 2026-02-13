# SolidMind CAD — Architecture Reference (v0.4)

## 1. What This System Does

SolidMind CAD is a **FreeCAD co-pilot** powered by MCP (Model Context Protocol).
It operates in two modes:

### Runtime Source Of Truth

The active runtime modules for planning/verification are:
- `server/geometry_planning.py` (legacy + `policy_v1` planning path)
- `server/me_orchestrator.py` (deterministic ME validators/traceability/risk gates)
- `feature_support/geometry_capabilities.yml`
- `feature_support/verification_policy.yml`
- `feature_support/planning_policy.yml`

Historical references in some design documents to registry modules such as
`server/me_registry.py` and `me_knowledge/index.yml` are not active runtime
dependencies in the current codebase.

### Primary Mode: Live CAD Co-pilot

The LLM drives FreeCAD's PartDesign workbench directly through MCP tools while
the user sees the model updating live. Manufacturing readiness checks are
available on-demand. ME design-loop preflight checks are also available on-demand.

```
 User (in FreeCAD + Claude Code CLI)
   │
   │  "Make a 120×60×20 mm bracket
   │   with four M6 holes on top"
   │
   ▼
┌──────────────────────────────────────────┐
│         MCP Host  (Claude LLM)           │
│  Translates natural language into        │
│  cad.* tool calls in real-time.          │
└────────────────┬─────────────────────────┘
                 │  JSON-RPC / stdio
                 ▼
┌──────────────────────────────────────────┐
│       MCP Bridge Server                  │
│  server/main.py                          │
│  Forwards commands to FreeCAD addon.     │
└────────────────┬─────────────────────────┘
                 │  TCP socket (localhost:9876)
                 ▼
┌──────────────────────────────────────────┐
│       FreeCAD Addon                      │
│  freecad_addon/                          │
│  Executes FreeCAD Python API.            │
│  User sees geometry update live.         │
└──────────────────────────────────────────┘
```

### ME Design-Loop Mode

A deterministic mechanical-engineering loop can run before or during CAD
modeling:

- validate constraints (`me.validate_constraints`)
- build traceability matrix (`me.build_traceability`)
- compute risk/signoff notices (`me.apply_risk_gates`)

This flow is also available as a single call via `me.design_loop`.

Current runtime policy (as of 2026-02-12):
- `me.apply_risk_gates` returns advisory risk/signoff notices and required actions; it does not hard-block generation.

---

## 2. Two-Process Architecture

The system runs as two processes connected by TCP:

```
┌─────────────────────────────┐     TCP socket     ┌─────────────────────────────┐
│  MCP Bridge Server          │◄───────────────────►│  FreeCAD Addon              │
│  (server/main.py)           │  localhost:9876      │  (freecad_addon/)           │
│                             │  newline-delimited   │                             │
│  • Launched by Claude Code  │  JSON protocol       │  • Runs inside FreeCAD GUI  │
│  • stdio MCP transport      │                      │  • Background thread server │
│  • Registers all MCP tools  │                      │  • Executes FreeCAD API     │
│  • Connects to addon on     │                      │  • Selection observer tracks │
│    first tool call          │                      │    user clicks on geometry  │
└─────────────────────────────┘                      └─────────────────────────────┘
```

**Socket protocol** (`freecad_addon/protocol.py`):
- Commands: `{"cmd": "pad", "args": {"sketch": "Sketch", "length": 10}}`
- Responses: `{"ok": true, "result": {...}}` or `{"ok": false, "error": "..."}`
- Newline-delimited JSON, one message per line

**Connection** (`server/freecad_client.py`):
- TCP client with retry/reconnect logic
- Default: `127.0.0.1:9876` (configurable via `constants.py`)

---

## 3. MCP Protocol Surface

### Tools (44 total)

#### CAD Tools (26) — `cad.*` in `server/tools_cad.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `cad.new_document` | Create FreeCAD document | name |
| `cad.new_body` | Create PartDesign Body | name, doc |
| `cad.sketch` | Create sketch with geometry + constraints | body, plane, elements[], constraints[] |
| `cad.pad` | Extrude sketch to solid | sketch, length, reversed, symmetric |
| `cad.revolution` | Revolve sketch around axis | sketch, axis (V/H/Base_X/Y/Z), angle |
| `cad.polar_pattern` | Circular pattern of features | features[], axis, occurrences, angle |
| `cad.pocket` | Cut pocket from sketch | sketch, length, pocket_type |
| `cad.sweep` | Sweep profile along spine | profile_sketch, spine_sketch, subtractive |
| `cad.helix` | Helical sweep (threads, springs) | sketch, pitch, height, turns, mode, axis |
| `cad.loft` | Loft between profiles | sketches[], ruled, closed, subtractive |
| `cad.hole` | Add hole on a face | face, diameter, depth, hole_type |
| `cad.fillet` | Fillet (round) edges | edges[] or selection, radius |
| `cad.chamfer` | Chamfer edges | edges[] or selection, size |
| `cad.get_selection` | Get user's GUI selection | — |
| `cad.get_model_tree` | Get feature tree + bounding boxes | doc |
| `cad.get_dimensions` | Bounding box, volume, surface area, topology counts | object_name |
| `cad.get_body_topology` | All faces/edges with geometric properties | body |
| `cad.find_edges` | Find edges by geometric criteria | axis, curve_type, convexity, on_face, near_point, length bounds |
| `cad.define_selection` | Define named edge selection query with invariants | name, query, invariants |
| `cad.resolve_selection` | Re-resolve named selection against current geometry | name |
| `cad.list_selections` | List all defined selection sets | — |
| `cad.delete_selection` | Remove a named selection set | name |
| `cad.screenshot` | Take screenshot with smart camera targeting | target (preset/face/feature/point), direction, distance |
| `cad.set_camera` | Set camera position and orientation | position, target, up, near_clip |
| `cad.get_camera` | Get current camera state | doc |
| `cad.undo` | Undo last operation | doc |
| `cad.export` | Export to STEP/STL/FCStd | format, path, doc |

#### Manufacturing Tools (3) — `mfg.*` in `server/tools_mfg.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `mfg.set_property` | Set manufacturing properties on model | properties dict |
| `mfg.readiness_check` | Run process-specific readiness checks | process (cnc/fdm/sla/sls/print_3d) |
| `mfg.export_rfq` | Generate RFQ summary from model + properties | properties dict |

#### Specification Tools (10) — `spec.*` in `server/tools.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `spec.select_schema` | Select process/maturity schema and coverage threshold | process, maturity_level, spec_version |
| `spec.apply_answer` | Apply deterministic draft mutation | spec_draft, op, path, value, question_id, source |
| `spec.validate` | Validate shape + coverage + blocker/warning rules | spec_draft |
| `spec.next_question` | Select next best interview question | spec_draft, conversation_signals |
| `spec.finalize` | Finalize spec + deterministic hash/provenance | spec_draft |
| `spec.export_brief` | Export human-readable design brief | spec |
| `spec.export_rfq_summary` | Export process-specific RFQ summary | spec |
| `spec.assess_design_path` | Classify basic_box vs spec_driven | spec_draft |
| `spec.generate_cad` | Generate CAD from finalized spec with precondition notices (notify-only) | spec, output_format, output_path, options |
| `spec.plan_geometry` | Plan geometry from spec (read-only, returns GIR/EIR) | spec |

#### ME Design Loop Tools (5) — `me.*` in `server/tools_me.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `me.validate_constraints` | Run deterministic Tier 0/1 proxy validators | constraint_sheet |
| `me.build_traceability` | Build requirement-to-evidence matrix | constraint_sheet, validation_report |
| `me.apply_risk_gates` | Assign risk class + signoff notices (notify-only) | constraint_sheet, validation_report |
| `me.design_loop` | Full ME flow (validate → trace → risk gates) | constraints |
| `me.list_validators` | List available validators with metadata | — |

### Prompts (2)

| Prompt | Target |
|--------|--------|
| `cad_copilot_system` | Live FreeCAD co-pilot persona — drives PartDesign interactively |
| `knowledge_research_workflow` | Self-directed research workflow for specialized engineering topics |

### Resources (7)

| Category | Count | URIs |
|----------|-------|------|
| ME Patterns | 7 | `resource://me_patterns/index.yml`, `resource://me_patterns/brackets/{mounting_bracket,l_bracket}.yml`, `resource://me_patterns/enclosures/rectangular_box.yml`, `resource://me_patterns/fastening/simple_gear.yml`, `resource://me_patterns/guides/{design_for_cnc,design_for_fdm}.yml` |

---

## 4. Live Co-pilot Interaction Flow

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                        MCP HOST (Claude LLM)                        │
 │                                                                     │
 │  1. User describes part                                             │
 │     → cad.new_document → cad.new_body                              │
 │     → cad.sketch (rect/circle/line/arc/spline + constraints)       │
 │     → cad.pad (extrude to solid)                                   │
 │                                                                     │
 │  2. User says "add a cylindrical boss"                              │
 │     → cad.sketch (circle on face) → cad.revolution(axis="V")      │
 │                                                                     │
 │  3. User clicks face/edge in FreeCAD                                │
 │     → cad.get_selection → sees Face6, Edge3, etc.                  │
 │                                                                     │
 │  4. User says "add holes here"                                      │
 │     → cad.hole(face="Face6", diameter=6.6, depth=20)               │
 │     → cad.polar_pattern(features=["Hole"], occurrences=6)          │
 │                                                                     │
 │  5. User says "round the vertical edges"                            │
 │     → cad.find_edges(axis="Z", convexity="convex")                 │
 │     → cad.define_selection(name="outer_verticals", query={...})    │
 │     → cad.fillet(selection="outer_verticals", radius=2.0)          │
 │                                                                     │
 │  6. User says "take a screenshot"                                   │
 │     → cad.screenshot(target="iso")                                  │
 │                                                                     │
 │  7. User says "check manufacturing readiness"                       │
 │     → mfg.set_property(material_family="aluminum", ...)            │
 │     → mfg.readiness_check(process="cnc")                           │
 │                                                                     │
 │  8. User says "export for vendor"                                   │
 │     → cad.export(format="step")                                    │
 │     → mfg.export_rfq(...)                                          │
 └─────────────────────────────────────────────────────────────────────┘
```

### Optional ME Preflight Flow

Before detailed CAD operations for complex components (e.g., rotating,
high-temperature parts), the host can run:

1. `me.design_loop(constraints)` — or individual steps:
   - `me.validate_constraints(constraint_sheet)`
   - `me.build_traceability(constraint_sheet, validation_report)`
   - `me.apply_risk_gates(constraint_sheet, validation_report)`
2. Review validation findings, traceability matrix, and risk notices
3. Iterate with updated constraints
4. Continue geometry execution with `cad.*`

This keeps engineering constraints and risk notices explicit while preserving
the interactive CAD workflow.

---

## 5. FreeCAD Addon Architecture

### Command Handlers (`freecad_addon/commands.py`)

The addon exposes command handlers via the `COMMAND_HANDLERS` dict:

| Category | Commands |
|----------|----------|
| Document | `new_document`, `new_body`, `get_model_tree`, `undo`, `redo` |
| Sketcher | `new_sketch`, `sketch_rect`, `sketch_circle`, `sketch_line`, `sketch_arc`, `sketch_bspline`, `sketch_constrain`, `close_sketch` |
| PartDesign | `pad`, `pocket`, `revolution`, `sweep`, `helix`, `loft`, `polar_pattern`, `hole`, `fillet`, `chamfer` |
| Query | `get_selection`, `get_dimensions`, `get_body_topology`, `find_edges` |
| Selection | `define_selection`, `resolve_selection`, `list_selections`, `delete_selection` |
| Visualization | `screenshot`, `set_camera`, `get_camera` |
| Manufacturing | `mfg_set_properties` |
| Export | `export` |

Note: The bridge's `cad.sketch` tool is a compound operation that calls
`new_sketch` → element commands → `sketch_constrain` → `close_sketch` in
sequence.

### Selection Observer (`freecad_addon/selection_observer.py`)

A singleton that hooks into `FreeCADGui.Selection`. When the user clicks on
geometry, it records the object name, sub-element type (Face/Edge/Vertex),
and geometric data (normals, positions). The `get_selection` command returns
this data.

### Socket Server (`freecad_addon/socket_server.py`)

`AddonSocketServer` runs in a background thread inside FreeCAD's GUI process.
It accepts TCP connections, reads newline-delimited JSON commands, dispatches
to `COMMAND_HANDLERS`, and returns JSON responses.

---

## 6. Edge Selection Architecture

Edge names in FreeCAD (e.g., `Edge1`, `Edge3`) are **unstable** — they change
whenever the model topology changes (adding a fillet renumbers all edges). The
named selection system solves this by storing **geometric queries** rather than
edge names.

### How It Works

1. **Query**: `cad.find_edges(axis="Z", convexity="convex")` returns edges
   matching geometric criteria (parallel to Z axis, outer corners).

2. **Define**: `cad.define_selection(name="outer_verticals", query={axis: "Z",
   convexity: "convex"}, invariants={expected_count: 4})` saves the query with
   optional invariants.

3. **Resolve**: `cad.resolve_selection(name="outer_verticals")` re-evaluates
   the query against the current geometry and returns current edge names.

4. **Use**: `cad.fillet(selection="outer_verticals", radius=2.0)` — fillet and
   chamfer accept a `selection` parameter that resolves the named query
   internally before applying the operation.

### Invariant Checking

Selections can include invariants (`expected_count`, `min_length`,
`max_length`) that are checked on every resolve. If the resolved edges don't
match the invariants (e.g., expected 4 edges but found 6), the system warns
about potential geometry drift.

### Edge Query Filters

| Filter | Description |
|--------|-------------|
| `axis` | Straight edges parallel to X, Y, or Z |
| `curve_type` | Line, Circle, BSplineCurve, Ellipse |
| `convexity` | `convex` (outer corners) or `concave` (inner corners) |
| `on_face` | Edges bounding a specific face |
| `near_point` | Edges within distance of a 3D point |
| `min_length` / `max_length` | Edge length bounds |

---

## 7. Geometry Pipeline (Phase 2)

The geometry pipeline converts specs into FreeCAD operations through
intermediate representations:

```
spec → GIR (intent) → EIR (execution plan) → Executor → FreeCAD
```

| Module | Purpose |
|--------|---------|
| `geometry_ir.py` | Core IR data structures (GIR, EIR, intents, references, invariants) |
| `geometry_planning.py` | Converts specs to GIR + EIR with notices |
| `geometry_planner.py` | Strategy selection based on GIR features and backend capabilities |
| `geometry_compiler_freecad.py` | Compiles GIR to FreeCAD MCP tool calls |
| `geometry_executor.py` | Executes operations and tracks execution trace |
| `geometry_constraints.py` | Constraint graph for mechanical relationships |
| `geometry_references.py` | Reference resolver with drift detection |
| `geometry_verify.py` | Verification engine consuming policy files |

Each stage produces deterministic hashes (GIR hash, EIR hash, execution trace
hash) for provenance tracking.

---

## 8. Supported Manufacturing Processes

`mfg.readiness_check` supports:

- `cnc`
- `fdm`
- `sla`
- `sls`
- `print_3d` (alias path for print workflows)

ME design-loop tooling is archetype-driven and currently ships with:
`turbine_wheel.turbocharger.radial.v1`.

---

## 9. Module Dependency Map

```
                          main.py
                        MCP Server
                  (JSON-RPC stdio loop)
         ┌─────────────┬─────────────┬─────────────┬──────────────┬──────────────┐
         │             │             │             │              │              │
   tools_cad.py   tools_mfg.py  tools_me.py   tools.py      prompts.py    resources.py
   (26 cad.*      (3 mfg.*      (5 me.*       (10 spec.*    (2 prompts)   (7 resources)
    tools)         tools)         tools)        tools)
         │             │             │
         │             │       me_orchestrator.py
         │             │             │
         │             │       me_knowledge/
         │             │ (tags/archetypes/templates/notes)
         │             │
   freecad_client.py
   (TCP socket client,
    retry/reconnect)
         │
  ┌──────┴────────────────────┐
  │  FreeCAD Addon            │
  │  socket_server.py         │  TCP server (localhost:9876)
  │  commands.py              │  FreeCAD API handlers
  │  selection_observer.py    │  GUI selection tracking
  │  protocol.py              │  newline-delimited JSON
  └───────────────────────────┘
```

---

## 10. Determinism Guarantees

| Concern | Mechanism |
|---------|-----------|
| ME validation outputs | Stable rule ordering, deterministic thresholds, sorted findings |
| ME traceability matrix | Requirement rows sorted by `requirement_id` |
| ME risk notices | Fixed score thresholds + deterministic notice decision logic |
| GIR/EIR hashing | Deterministic content hashing for provenance |
| CAD command transport | Strict newline-delimited JSON request/response protocol |
| Selection queries | Deterministic geometric query filters + invariant checks |
| Golden tests | Stable `unittest` assertions over tool outputs and registries |

---

## 11. File Map

```
solidmind-cad/
├── server/
│   ├── main.py                    MCP JSON-RPC stdio server
│   ├── freecad_client.py          TCP socket client for FreeCAD addon
│   ├── tools_cad.py               Live CAD tool implementations (26 tools)
│   ├── tools_mfg.py               Manufacturing readiness tools (3 tools)
│   ├── tools_me.py                ME design-loop tools (5 tools)
│   ├── tools.py                   Spec interview tools (10 tools)
│   ├── me_orchestrator.py         Deterministic ME flow orchestration
│   ├── geometry_ir.py             GIR/EIR data structures and builders
│   ├── geometry_planning.py       Spec → GIR/EIR conversion
│   ├── geometry_planner.py        Strategy selection for build approaches
│   ├── geometry_compiler_freecad.py  GIR → FreeCAD compiler
│   ├── geometry_executor.py       Execution engine with trace tracking
│   ├── geometry_constraints.py    Constraint graph for mechanical relationships
│   ├── geometry_references.py     Reference resolver with drift detection
│   ├── geometry_verify.py         Verification engine
│   ├── feature_support.py         Feature support matrix parsing
│   ├── prompts.py                 LLM prompt definitions
│   ├── resources.py               MCP resource registry
│   ├── models.py                  Finding, ToolError, Severity, ValidatorResult models
│   ├── jcs.py                     JSON Canonicalization Scheme (deterministic hashing)
│   ├── constants.py               Socket defaults and shared constants
│   ├── paths.py                   repo_root(), data_path()
│   └── jsonutil.py                orjson with stdlib fallback
├── freecad_addon/
│   ├── __init__.py                Package init, start()/stop() entry points
│   ├── InitGui.py                 FreeCAD workbench integration
│   ├── commands.py                FreeCAD API command handlers
│   ├── protocol.py                Newline-delimited JSON protocol
│   ├── selection_observer.py      Selection tracking
│   └── socket_server.py           TCP server on localhost:9876
├── me_patterns/                   ME pattern library (brackets, enclosures, gears, guides)
├── me_knowledge/                  ME domain tags, archetypes, constraint templates, research notes
├── schemas/                       JSON schemas (GIR, EIR, capabilities, verification policy)
├── tests/                         Unit tests for all layers
└── ARCHITECTURE.md                This file
```

---

## 12. Key Design Tradeoffs

### Why Two Processes?

FreeCAD's Python API is only accessible from inside the FreeCAD process. The
MCP server runs separately via stdio; a TCP bridge keeps the server testable
without embedding it into FreeCAD.

### Why Live Co-pilot?

Live geometry operations (`cad.*`) give immediate feedback and faster design
iteration than delayed, end-of-flow generation.

### Why Add ME Design-Loop Tools?

`me.*` tools provide deterministic constraint capture, early validation,
traceability, and explicit release/risk notices for high-consequence components.

### Why a Geometry IR Pipeline?

The GIR/EIR intermediate representations decouple design intent from execution,
enabling deterministic hashing for provenance, strategy selection across
backends, and structured verification.
