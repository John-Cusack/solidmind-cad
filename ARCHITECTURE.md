# SolidMind CAD — Architecture Reference (v0.3)

## 1. What This System Does

SolidMind CAD is a **FreeCAD co-pilot** powered by MCP (Model Context Protocol).
It operates in two modes:

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

- route request to archetype (`me.route_request`)
- instantiate constraint sheet (`me.instantiate_constraint_sheet`)
- run proxy validators (`me.validate_constraint_sheet`)
- generate traceability matrix (`me.build_traceability`)
- compute risk/signoff notices (`me.apply_risk_gates`)

This flow is also available as a single call via `me.design_loop`.

Current runtime policy (as of 2026-02-12):
- `spec.generate_cad` treats low coverage as a warning (notify-only), not a hard error.
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

### Tools (43 total)

#### CAD Tools (21) — `cad.*` in `server/tools_cad.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `cad.new_document` | Create FreeCAD document | name |
| `cad.new_body` | Create PartDesign Body | name, doc |
| `cad.sketch` | Create sketch with geometry + constraints | body, plane, elements[], constraints[] |
| `cad.pad` | Extrude sketch to solid | sketch, length, reversed, symmetric |
| `cad.revolution` | Revolve sketch around axis | sketch, axis (V/H/Base_X/Y/Z), angle |
| `cad.polar_pattern` | Circular pattern of features | features[], axis, occurrences, angle |
| `cad.pocket` | Cut pocket from sketch | sketch, length, pocket_type |
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
| `cad.undo` | Undo last operation | doc |
| `cad.export` | Export to STEP/STL/FCStd | format, path, doc |

#### Manufacturing Tools (3) — `mfg.*` in `server/tools_mfg.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `mfg.set_property` | Set manufacturing properties on model | properties dict |
| `mfg.readiness_check` | Run process-specific readiness checks | process (cnc/fdm/sla/sls/print_3d) |
| `mfg.export_rfq` | Generate RFQ summary from model + properties | properties dict |

#### Specification Tools (9) — `spec.*` in `server/tools.py`

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

#### ME Design Loop Tools (10) — `me.*` in `server/tools_me.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `me.list_domain_tags` | List ME controlled vocabulary | — |
| `me.list_archetypes` | List available archetype IDs | — |
| `me.get_archetype_card` | Load archetype card | archetype_id |
| `me.route_request` | Route request text to archetype + tags | request_text |
| `me.instantiate_constraint_sheet` | Instantiate template with overrides | archetype_id, overrides, assumptions |
| `me.validate_constraint_sheet` | Run deterministic Tier 0/1 proxy validators | constraint_sheet |
| `me.build_traceability` | Build requirement-to-evidence matrix | constraint_sheet, validation_report |
| `me.apply_risk_gates` | Assign risk class + signoff notices (notify-only) | constraint_sheet, validation_report |
| `me.design_loop` | Full ME flow (route → constrain → validate → trace → notices) | request_text, overrides, assumptions |
| `me.get_knowledge_policy` | Return standards/material source policy | — |

### Prompts (1)

| Prompt | Target |
|--------|--------|
| `cad_copilot_system` | Live FreeCAD co-pilot persona — drives PartDesign interactively |

### Resources (12)

| Category | Count | URIs |
|----------|-------|------|
| ME Patterns | 7 | `resource://me_patterns/index.yml`, `resource://me_patterns/brackets/{mounting_bracket,l_bracket}.yml`, `resource://me_patterns/enclosures/rectangular_box.yml`, `resource://me_patterns/fastening/simple_gear.yml`, `resource://me_patterns/guides/{design_for_cnc,design_for_fdm}.yml` |
| ME Knowledge | 5 | `resource://me_knowledge/index.yml`, `resource://me_knowledge/domain_tags.yml`, `resource://me_knowledge/archetypes/turbocharger_turbine_wheel_v1.yml`, `resource://me_knowledge/constraint_templates/turbocharger_turbine_wheel_v1.yml`, `resource://me_knowledge/standards_sources.yml` |

---

## 4. Live Co-pilot Interaction Flow

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                        MCP HOST (Claude LLM)                        │
 │                                                                     │
 │  1. User describes part                                             │
 │     → cad.new_document → cad.new_body                              │
 │     → cad.sketch (rect/circle/line/arc + constraints)              │
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
 │  6. User says "check manufacturing readiness"                       │
 │     → mfg.set_property(material_family="aluminum", ...)            │
 │     → mfg.readiness_check(process="cnc")                           │
 │                                                                     │
 │  7. User says "export for vendor"                                   │
 │     → cad.export(format="step")                                    │
 │     → mfg.export_rfq(...)                                          │
 └─────────────────────────────────────────────────────────────────────┘
```

### Optional ME Preflight Flow

Before detailed CAD operations for complex components (e.g., rotating,
high-temperature parts), the host can run:

1. `me.design_loop(request_text, overrides?, assumptions?)`
2. Review `summary`, `validation`, `traceability`, and `risk_gates` notices
3. Resolve `next_questions` / `TBD` fields as iteration guidance
4. Iterate with updated overrides
5. Continue geometry execution with `cad.*`

This keeps engineering constraints and risk notices explicit while preserving
the interactive CAD workflow.

---

## 5. FreeCAD Addon Architecture

### Command Handlers (`freecad_addon/commands.py`)

The addon exposes 28 command handlers via the `COMMAND_HANDLERS` dict:

| Category | Commands |
|----------|----------|
| Document | `new_document`, `new_body`, `get_model_tree`, `undo`, `redo` |
| Sketcher | `new_sketch`, `sketch_rect`, `sketch_circle`, `sketch_line`, `sketch_arc`, `sketch_constrain`, `close_sketch` |
| PartDesign | `pad`, `pocket`, `revolution`, `polar_pattern`, `hole`, `fillet`, `chamfer` |
| Query | `get_selection`, `get_dimensions`, `get_body_topology`, `find_edges` |
| Selection | `define_selection`, `resolve_selection`, `list_selections`, `delete_selection` |
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

## 7. Supported Manufacturing Processes

`mfg.readiness_check` supports:

- `cnc`
- `fdm`
- `sla`
- `sls`
- `print_3d` (alias path for print workflows)

ME design-loop tooling is archetype-driven and currently ships with:
`turbine_wheel.turbocharger.radial.v1`.

---

## 8. Module Dependency Map

```
                          main.py
                        MCP Server
                  (JSON-RPC stdio loop)
         ┌─────────────┬─────────────┬─────────────┬──────────────┬──────────────┐
         │             │             │             │              │              │
   tools_cad.py   tools_mfg.py  tools_me.py   prompts.py    resources.py
   (21 cad.*      (3 mfg.*      (10 me.*      (1 prompt)    (12 resources)
    tools)         tools)         tools)
         │             │             │              │
         │             │       me_registry.py       │
         │             │       me_orchestrator.py   │
         │             │             │              │
         │             │       me_knowledge/        │
         │             │ (tags/archetypes/templates)
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

## 9. Determinism Guarantees

| Concern | Mechanism |
|---------|-----------|
| ME routing/scoring | Deterministic lexical signal matching (no randomness) |
| ME validation outputs | Stable rule ordering, deterministic thresholds, sorted findings |
| ME traceability matrix | Requirement rows sorted by `requirement_id` |
| ME risk notices | Fixed score thresholds + deterministic notice decision logic |
| CAD command transport | Strict newline-delimited JSON request/response protocol |
| Selection queries | Deterministic geometric query filters + invariant checks |
| Golden tests | Stable `unittest` assertions over tool outputs and registries |

---

## 10. File Map

```
solidmind-cad/
├── server/
│   ├── main.py              MCP JSON-RPC stdio server
│   ├── freecad_client.py    TCP socket client for FreeCAD addon
│   ├── tools_cad.py         Live CAD tool implementations
│   ├── tools_mfg.py         Manufacturing readiness tool implementations
│   ├── tools_me.py          ME design-loop tool implementations
│   ├── me_registry.py       ME knowledge registry loader
│   ├── me_orchestrator.py   Deterministic ME flow orchestration
│   ├── prompts.py           LLM prompt definitions
│   ├── resources.py         MCP resource registry
│   ├── models.py            Finding, ToolError, Severity models
│   ├── constants.py         Socket defaults and shared constants
│   ├── paths.py             repo_root(), data_path()
│   └── jsonutil.py          orjson with stdlib fallback
├── freecad_addon/
│   ├── InitGui.py           FreeCAD workbench integration
│   ├── commands.py          FreeCAD API command handlers
│   ├── protocol.py          Newline-delimited JSON protocol
│   ├── selection_observer.py Selection tracking
│   └── socket_server.py     TCP server on localhost:9876
├── me_patterns/             ME pattern library
├── me_knowledge/            ME domain tags/archetypes/templates/sources
├── tests/                   Unit tests for CAD, MFG, ME, and protocol layers
└── ARCHITECTURE.md          This file
```

---

## 11. Key Design Tradeoffs

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

### Why Remove Legacy Spec Compatibility?

Keeping inactive compatibility surfaces increases maintenance and bug-hunting
cost. The active MCP surface is intentionally narrowed to live CAD, on-demand
manufacturing checks, and ME design-loop workflows.
