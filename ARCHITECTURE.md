# SolidMind CAD — Architecture Reference (v0.2)

## 1. What This System Does

SolidMind CAD is a **FreeCAD co-pilot** powered by MCP (Model Context Protocol).
It operates in two modes:

### Primary Mode: Live CAD Co-pilot

The LLM drives FreeCAD's PartDesign workbench directly through MCP tools while
the user sees the model updating live. Manufacturing readiness checks are
available on-demand.

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

### Legacy Mode: Spec Interview

A structured interview gathers part requirements, then generates an envelope
CAD model via CadQuery/OCCT. The `spec.*` tools are kept for backward
compatibility.

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

### Tools (23 total)

#### CAD Tools (12) — `cad.*` in `server/tools_cad.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `cad.new_document` | Create FreeCAD document | name |
| `cad.new_body` | Create PartDesign Body | name, doc |
| `cad.sketch` | Create sketch with geometry + constraints | body, plane, elements[], constraints[] |
| `cad.pad` | Extrude sketch to solid | sketch, length, reversed, symmetric |
| `cad.pocket` | Cut pocket from sketch | sketch, length, pocket_type |
| `cad.hole` | Add hole on a face | face, diameter, depth, hole_type |
| `cad.fillet` | Fillet (round) edges | edges[], radius |
| `cad.chamfer` | Chamfer edges | edges[], size |
| `cad.get_selection` | Get user's GUI selection | — |
| `cad.get_model_tree` | Get feature tree + bounding boxes | doc |
| `cad.undo` | Undo last operation | doc |
| `cad.export` | Export to STEP/STL/FCStd | format, path, doc |

#### Manufacturing Tools (3) — `mfg.*` in `server/tools_mfg.py`

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `mfg.set_property` | Set manufacturing properties on model | properties dict |
| `mfg.readiness_check` | Run process-specific readiness checks | process (cnc/fdm/sla/sls/print_3d) |
| `mfg.export_rfq` | Generate RFQ summary from model + properties | properties dict |

#### Legacy Spec Tools (8) — `spec.*` in `server/tools.py`

| Tool | Purpose | Key I/O |
|------|---------|---------|
| `spec.select_schema` | Initialize interview | process, maturity → schema_id, threshold |
| `spec.next_question` | Get next question | spec_draft, signals → question_id, text |
| `spec.apply_answer` | Mutate spec | spec_draft, op, path, value → updated draft |
| `spec.validate` | Check readiness | spec_draft → valid, coverage, blockers |
| `spec.finalize` | Freeze & hash | spec_draft → clean spec, hash, provenance |
| `spec.export_brief` | Design summary | spec → markdown |
| `spec.export_rfq_summary` | Vendor summary | spec → markdown |
| `spec.generate_cad` | Produce geometry | spec, format → file + metadata |

### Prompts (5)

| Prompt | Target |
|--------|--------|
| `cad_copilot_system` | Live FreeCAD co-pilot persona — drives PartDesign interactively |
| `spec_interviewer_system` | General CNC + 3D print spec interviewer persona |
| `spec_interviewer_system_print_3d` | FDM-specific spec interviewer persona |
| `spec_summary_formatter` | Formats spec for user confirmation |
| `rfq_writer` | Writes RFQ-ready vendor summaries |

### Resources (11)

| Category | Count | URIs |
|----------|-------|------|
| Schemas | 2 | `resource://schemas/cnc.schema.json`, `resource://schemas/print_3d.schema.json` |
| Question banks | 2 | `resource://question_bank/cnc.yml`, `resource://question_bank/print_3d.yml` |
| Examples | 6 | `resource://examples/{cnc,print_3d}/{L1,L2,L3}.json` |
| Glossary | 1 | `resource://glossary.yml` |

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
 │  2. User clicks face/edge in FreeCAD                                │
 │     → cad.get_selection → sees Face6, Edge3, etc.                  │
 │                                                                     │
 │  3. User says "add holes here"                                      │
 │     → cad.hole(face="Face6", diameter=6.6, depth=20)               │
 │                                                                     │
 │  4. User says "round these edges"                                   │
 │     → cad.fillet(edges=["Edge1","Edge3"], radius=2.0)              │
 │                                                                     │
 │  5. User says "check manufacturing readiness"                       │
 │     → mfg.set_property(material_family="aluminum", ...)            │
 │     → mfg.readiness_check(process="cnc")                           │
 │                                                                     │
 │  6. User says "export for vendor"                                   │
 │     → cad.export(format="step")                                    │
 │     → mfg.export_rfq(...)                                          │
 └─────────────────────────────────────────────────────────────────────┘
```

---

## 5. FreeCAD Addon Architecture

### Command Handlers (`freecad_addon/commands.py`)

The addon exposes 21 command handlers via the `COMMAND_HANDLERS` dict:

| Category | Commands |
|----------|----------|
| Document | `new_document`, `new_body`, `get_model_tree`, `undo`, `redo` |
| Sketcher | `new_sketch`, `sketch_rect`, `sketch_circle`, `sketch_line`, `sketch_arc`, `sketch_constrain`, `close_sketch` |
| PartDesign | `pad`, `pocket`, `hole`, `fillet`, `chamfer` |
| Query | `get_selection`, `get_dimensions` |
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

## 6. Supported Manufacturing Processes

| Process    | Schema                      | Question Bank          | Rules Engine        | CAD Generator        |
|------------|-----------------------------|------------------------|---------------------|----------------------|
| **CNC**    | `schemas/cnc.schema.json`   | `question_bank/cnc.yml`| `rules_cnc.py`      | `BoxCadGenerator`    |
| **3D Print (FDM)** | `schemas/print_3d.schema.json` | `question_bank/print_3d.yml` | `rules_print_3d.py` | `BoxCadGenerator` |

Both processes share the same legacy CAD generator (envelope box + holes + fillets).
Process-specific differences live in the **rules** and **question banks**.

---

## 7. Module Dependency Map

```
                          main.py
                        MCP Server
                  (JSON-RPC stdio loop)
          ┌───────────┼──────────┬──────────────┐
          │           │          │              │
     tools_cad.py  tools_mfg.py tools.py   prompts.py  resources.py
     (12 cad.*     (3 mfg.*    (8 spec.*   (5 prompts) (11 resources)
      tools)        tools)      tools)
          │                     │
          │          ┌──────────┼───────┬──────────┐
          │          │          │       │          │
          │     question  validation  spec   json     jcs.py
          │     _bank.py    .py      _draft  _pointer  RFC 8785
          │     (YAML     (JSON      .py     .py
          │      loader,   Schema    (deep   (RFC 6901
          │      coverage) + rules   copy,   get/set/
          │                dispatch) defaults,remove)
          │                │        strip)
          │           ┌────┴────┐
          │           │         │
          │        rules_    rules_
          │        cnc.py    print_3d.py
          │        (8 rules) (8 rules)
          │
  freecad_client.py          cad_gen.py
  (TCP socket client,        (dispatcher, interface
   retry/reconnect)           parser, base64, STEP
          │                   timestamp normalizer)
          │                        │
          │                  cad_gen_box.py
          │                (BoxCadGenerator)
          │                CadQuery / OCCT
          │                STEP, STL, FCStd
          │
  ┌───────┴──────────────────┐
  │  FreeCAD Addon            │
  │  socket_server.py         │  TCP server (localhost:9876)
  │  commands.py              │  FreeCAD API handlers (21 commands)
  │  selection_observer.py    │  GUI selection tracking
  │  protocol.py              │  Newline-delimited JSON
  └───────────────────────────┘

  ┌───────────────────────────┐
  │  Shared utilities          │
  │  models.py                 │  Finding, ToolError, ConversationSignals, Severity
  │  constants.py              │  thresholds, process lists, hash algo, socket defaults
  │  timeutil.py               │  deterministic timestamps from _counter
  │  paths.py                  │  repo_root(), data_path()
  │  jsonutil.py               │  orjson with stdlib fallback
  └────────────────────────────┘
```

---

## 8. Legacy Spec Interview Flow

Used by `spec.*` tools. The host LLM drives a structured interview, then
generates CAD from the finalized spec via CadQuery.

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                        MCP HOST (Claude LLM)                        │
 │                                                                     │
 │  1. Call spec.select_schema(process, maturity)                      │
 │     → schema_id, question_bank_id, coverage_threshold              │
 │                                                                     │
 │  2. LOOP ─────────────────────────────────────────────────────────  │
 │     │                                                               │
 │     │  a. spec.next_question(spec_draft, signals)                   │
 │     │     → question_id, question_text, field_paths, rationale      │
 │     │                                                               │
 │     │  b. Present question to user, get answer                      │
 │     │                                                               │
 │     │  c. spec.apply_answer(spec_draft, "set", path, value, qid)   │
 │     │     → updated spec_draft (with _audit entry)                  │
 │     │                                                               │
 │     │  d. spec.validate(spec_draft)                                 │
 │     │     → shape_valid, coverage_score, blockers[], warnings[]     │
 │     │                                                               │
 │     │  e. If sufficient → EXIT LOOP                                 │
 │     │     Else → continue to (a)                                    │
 │     │                                                               │
 │  ───┘                                                               │
 │                                                                     │
 │  3. spec.finalize(spec_draft)                                       │
 │     → clean spec, SHA-256 hash, changelog, provenance               │
 │                                                                     │
 │  4. spec.export_brief(spec)        → Markdown design brief          │
 │     spec.export_rfq_summary(spec)  → Markdown RFQ for vendors       │
 │                                                                     │
 │  5. spec.generate_cad(spec, format, path)                           │
 │     → STEP / STL / .FCStd file  +  base64 inline (if small)        │
 │                                                                     │
 │  6. User opens file in FreeCAD, reviews/edits geometry              │
 └─────────────────────────────────────────────────────────────────────┘
```

### The Specification Data Model

A `spec_draft` is a single JSON dict that evolves through the interview.

```
spec_draft
├── meta
│   ├── spec_version    "1.0.0"
│   ├── created_at      ISO 8601
│   ├── process         "cnc" | "print_3d"
│   ├── maturity_level  "L1" | "L2" | "L3"
│   └── units           "mm" | "in"
├── part
│   ├── name / description
│   ├── quantity        (int ≥ 1)
│   ├── envelope        { x, y, z }          ← drives CAD box
│   ├── interfaces[]    free-text strings     ← parsed into holes
│   └── critical_features[]
├── manufacturing
│   ├── process_notes
│   ├── material        { family, grade }
│   ├── tolerances      { general, critical[] }
│   │   ┌─ CNC only ──────────────────────┐
│   ├── │ surface_finish  { ra_um, coating }│
│   ├── │ cosmetics       { visible_surfaces}│
│   │   └──────────────────────────────────┘
│   │   ┌─ 3D Print only ─────────────────┐
│   ├── │ technology       "fdm"           │
│   ├── │ output_target    vendor/in_house │
│   ├── │ appearance       { color, finish,│
│   │   │   support_marks_ok, cosmetic_surfaces }
│   ├── │ post_processing[]                │
│   └── │ in_house_settings { layer_height,│
│       │   nozzle_dia, walls, infill, … } │
│       └──────────────────────────────────┘
├── inspection  { ctq[], method, requirements[] }
├── deliverables { cad_formats[], drawing_required }
├── open_questions[]
├── assumptions[]
│
│── _interview          ← INTERNAL, stripped on finalize
│   ├── answered  { qid → timestamp }
│   ├── skipped   { qid → timestamp }
│   └── _counter  (monotonic int for deterministic timestamps)
└── _audit[]            ← INTERNAL, stripped on finalize
    └── { ts, op, path, value, source, question_id }
```

After finalize: same structure minus `_interview` and `_audit`. A `coverage_score`
is added to `meta`. The spec is hashed (SHA-256 over RFC 8785 JCS canonical JSON).

### Maturity Levels & Coverage Gating

| Level | Name       | Coverage Threshold | What It Means                       |
|-------|------------|-------------------|-------------------------------------|
| L1    | Concept    | 60%               | Envelope box only — enough to quote |
| L2    | Prototype  | 80%               | Holes, fillets, material decided    |
| L3    | Production | 90%               | Full tolerances, inspection, finish |

### Question Selection Algorithm

Priority order (deterministic, with lexical tiebreak on `question_id`):

1. **Blockers** — rule violations that produce `Severity.BLOCK` findings,
   sorted by `priority` descending. These must be resolved first.
2. **Required unanswered** — questions marked `required=true` for the
   current maturity level, not yet in `_interview.answered` or `.skipped`.
3. **Highest-weight unanswered** — remaining questions sorted by
   `weight` descending at current maturity.
4. **Done** — all questions answered or skipped; returns `null`.

Skipped questions are excluded unless `allow_revisit_skipped=true` in
conversation signals.

---

## 9. Legacy CAD Generation Pipeline

Used by `spec.generate_cad` (requires CadQuery). Not used in live co-pilot mode.

```
  Finalized spec
       │
       ▼
  generate() dispatcher  (cad_gen.py)
  • Selects generator by process (BoxCadGenerator)
  • Sanitizes filename from part name
  • Creates output directory (temp or specified)
  • Lazy-imports cadquery
       │
       ▼
  BoxCadGenerator.generate()  (cad_gen_box.py)
  P0: Envelope Box  →  cq.Workplane("XY").box(x, y, z)
  P1: Interface Holes  →  parse free-text interfaces, cut cylinders
  P1: Edge Fillets  →  CNC: 0.2mm default, 3D Print: none
  P2: Warnings  →  critical_features, appearance, post_processing
  Export: STEP / STL / FCStd
       │
       ▼
  GenerateResult { file_path, cad_data (base64), metadata, warnings[] }
```

### Interface String Parsing

```
"4x M6 clearance holes on 50×30 pattern"
  │    │     │                │
  │    │     │                └─ pattern_x=50, pattern_y=30
  │    │     └─ hole_type=clearance → dia=6.6mm (ISO 273)
  │    └─ thread=M6
  └─ count=4 → rectangular grid
```

Supported hole types: `clearance`, `tapped`, `through`, `counterbore`,
`countersink`, `heat-set`, `press-fit`

| Type | Lookup | Fallback |
|------|--------|----------|
| Clearance | ISO 273 table (M2.5-M12) | thread_dia + 0.4mm |
| Tapped | Nominal thread diameter | -- |
| Heat-set | Insert bore table | thread_dia + 1.0mm |
| Press-fit | Nominal thread diameter | -- |
| Through | Same as clearance | thread_dia + 0.4mm |

---

## 10. Validation Architecture

Used by `spec.validate` in the legacy interview flow.

```
                    spec_draft
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
   ┌────────────┐ ┌──────────┐ ┌────────────┐
   │   Shape    │ │ Coverage │ │   Rules    │
   │ Validation │ │ Scoring  │ │ Execution  │
   │            │ │          │ │            │
   │ JSON Schema│ │ Question │ │ Python fns │
   │ Draft 2020 │ │ bank     │ │ per-process│
   │ -12        │ │ weights  │ │            │
   └─────┬──────┘ └────┬─────┘ └─────┬──────┘
         │             │             │
         ▼             ▼             ▼
    shape_valid   coverage_score  blockers[]
    errors[]      (0.0 - 1.0)    warnings[]
         │             │             │
         └─────────────┼─────────────┘
                       ▼
            Sufficient? = shape_valid
                       AND blockers == []
                       AND coverage >= threshold
```

### Rules by Process & Maturity

**CNC Rules** (8 rules, `rules_cnc.py`):

| Rule ID | Maturity | Priority | What It Checks |
|---------|----------|----------|----------------|
| `cnc.envelope.required` | L1+ | 1000 | x,y,z all > 0 |
| `cnc.quantity.positive` | L1+ | 950 | quantity >= 1 |
| `cnc.material.grade.required` | L2+ | 900 | grade not blank |
| `cnc.interfaces.required` | L2+ | 850 | interfaces populated |
| `cnc.deliverables.cad_formats.required` | L2+ | 800 | at least one format |
| `cnc.tolerances.general.required` | L3 | 780 | tolerance scheme set |
| `cnc.surface_finish.required` | L3 | 770 | Ra > 0 |
| `cnc.inspection.method.required` | L3 | 760 | method not blank |

**3D Print Rules** (8 rules, `rules_print_3d.py`):

| Rule ID | Maturity | Priority | What It Checks |
|---------|----------|----------|----------------|
| `print_3d.envelope.required` | L1+ | 1000 | x,y,z all > 0 |
| `print_3d.quantity.positive` | L1+ | 950 | quantity >= 1 |
| `print_3d.material.grade.required` | L2+ | 850 | grade not blank |
| `print_3d.interfaces.required` | L2+ | 820 | interfaces populated |
| `print_3d.deliverables.cad_formats.required` | L2+ | 800 | at least one format |
| `print_3d.in_house_settings.required` | L2+ | 790 | settings when in-house |
| `print_3d.tolerances.fit.required` | L3 | 780 | fit tolerance notes |
| `print_3d.appearance.required` | L3 | 770 | color + finish both set |

---

## 11. Determinism Guarantees (Legacy Spec Tools)

| Concern | Mechanism |
|---------|-----------|
| Timestamps | Monotonic counter in `_interview._counter` + base `created_at` |
| Hashing | RFC 8785 JCS canonicalization -> SHA-256 |
| Question order | Priority + weight sort with lexical tiebreak on `question_id` |
| Field order | JSON Schema + JCS ensure consistent key ordering |
| STEP files | Post-process to normalize `FILE_NAME` timestamp |
| Golden tests | YAML transcripts replayed and asserted against expected output |

---

## 12. File Map

```
solidmind-cad/
├── server/
│   ├── main.py              MCP JSON-RPC stdio server (registers all tool groups)
│   ├── freecad_client.py    TCP socket client for FreeCAD addon
│   ├── tools_cad.py         12 cad.* tool implementations
│   ├── tools_mfg.py         3 mfg.* tool implementations
│   ├── tools.py             8 legacy spec.* tool implementations
│   ├── prompts.py           5 LLM prompt definitions
│   ├── resources.py         MCP resource registry (11 resources)
│   ├── validation.py        Schema + coverage + rule dispatch
│   ├── rules_cnc.py         CNC blocker/warning rules
│   ├── rules_print_3d.py    3D print blocker/warning rules
│   ├── question_bank.py     YAML loader + coverage scoring
│   ├── json_pointer.py      RFC 6901 get/set/remove
│   ├── jcs.py               RFC 8785 canonical JSON
│   ├── spec_draft.py        Deep copy, defaults, strip internals
│   ├── cad_gen.py           Legacy CAD dispatcher + interface parser
│   ├── cad_gen_box.py       Legacy envelope-box generator (CadQuery/OCCT)
│   ├── models.py            Finding, ToolError, Severity, ConversationSignals
│   ├── constants.py         Thresholds, process lists, hash algo, socket defaults
│   ├── timeutil.py          Deterministic timestamp generation
│   ├── paths.py             repo_root(), data_path()
│   └── jsonutil.py          orjson with stdlib fallback
├── freecad_addon/
│   ├── __init__.py          Package init, start() / stop() entry points
│   ├── InitGui.py           FreeCAD workbench integration
│   ├── commands.py          FreeCAD API command handlers (21 commands)
│   ├── protocol.py          Newline-delimited JSON protocol
│   ├── selection_observer.py Selection tracking (singleton)
│   └── socket_server.py     TCP server on localhost:9876
├── schemas/
│   ├── common.schema.json   Shared definitions (envelope3)
│   ├── cnc.schema.json      CNC spec shape (Draft 2020-12)
│   └── print_3d.schema.json 3D print spec shape
├── question_bank/
│   ├── cnc.yml              10 questions, per-maturity gating
│   ├── print_3d.yml         11 questions, conditional in_house
│   └── glossary.yml         Domain terms (CTQ, Ra, GD&T, ...)
├── examples/
│   ├── cnc/                 L1, L2, L3 finalized specs + sensor_bracket_L2
│   └── print_3d/            L1, L2, L3 finalized specs
├── tests/
│   ├── helpers.py            make_base_spec_draft()
│   ├── test_validation.py    Schema + rule tests
│   ├── test_next_question.py Question selection logic
│   ├── test_apply_answer.py  Mutation + audit tests
│   ├── test_finalize.py      Hash + provenance tests
│   ├── test_process_routing.py Process dispatch tests
│   ├── test_cad_gen.py       Interface parser + geometry tests
│   ├── test_transcripts.py   Golden transcript replay
│   ├── test_tools_cad.py     CAD tool tests (mocked FreeCAD client)
│   ├── test_tools_mfg.py     Manufacturing readiness tool tests
│   ├── test_freecad_client.py TCP client tests (echo server)
│   ├── test_protocol.py      Protocol serialization tests
│   └── transcripts/          6 golden YAML transcripts
│       ├── cnc_L1.yml
│       ├── cnc_L2.yml
│       ├── cnc_L3.yml
│       ├── print_3d_L1.yml
│       ├── print_3d_L2.yml
│       └── print_3d_L3.yml
├── scripts/
│   ├── replay_transcript.py      Golden test runner
│   ├── freecad_from_spec.py      Generate CAD from spec file
│   └── install_freecad_addon.sh  Addon installation helper
├── pyproject.toml                Dependencies + entry points
├── CLAUDE.md                     AI assistant instructions
└── ARCHITECTURE.md               <- THIS FILE
```

---

## 13. Key Design Tradeoffs

### Why Two Processes?

FreeCAD's Python API is only accessible from within the FreeCAD process. The
MCP server runs as a separate process (launched by Claude Code via stdio).
The TCP socket bridge lets the MCP server send commands to FreeCAD without
needing to be embedded in FreeCAD's process. This keeps the MCP server
lightweight and testable independently.

### Why Live Co-pilot Over Interview?

The v0.1 spec interview approach (gather requirements -> generate CAD) works
for quoting but creates a disconnect: the user doesn't see geometry until the
end. The v0.2 live co-pilot approach lets the user see geometry appear in
real-time as they describe the part, enabling an iterative design loop:
describe -> see -> refine.

Manufacturing readiness checks moved from interview gates to on-demand checks
(`mfg.readiness_check`) because engineers want to design first and validate
later.

### Why Keep Legacy Spec Tools?

The `spec.*` tools remain for cases where a structured interview is preferred
(e.g., vendor quoting workflows where all requirements must be captured
upfront). They also serve as the foundation for the question bank and
validation infrastructure that `mfg.readiness_check` builds on.

### Fillet Strategy (Legacy CAD Gen)

- **CNC**: Default 0.2mm, 0.3mm when deburring is specified.
- **3D Print**: Default 0.0mm (none) unless explicitly overridden.

### FreeCAD Export Path (Legacy CAD Gen)

CadQuery can't write `.FCStd` directly. The workaround:
```
CadQuery solid -> temp STEP file -> FreeCAD importPart() -> save as .FCStd
```
This preserves full B-rep fidelity because STEP is the native OCCT exchange format.
