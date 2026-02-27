# SolidMind CAD — Architecture Reference (v0.5)

## 1. System Overview

SolidMind CAD is an MCP bridge server that lets an LLM drive FreeCAD while keeping core engineering checks deterministic.

Primary runtime entrypoint: `server/main.py`

Primary runtime domains:

- Live CAD operations (`cad.*`)
- Manufacturing checks (`mfg.*`)
- Deterministic spec flow (`spec.*`)
- Deterministic ME preflight (`me.*`)
- Knowledge extraction/search (`knowledge.*`)
- Parametric geometry generators (`geometry.*`)
- Parametric design studies (`study.*`)
- Motion validation pipeline (`motion.*`)

## 2. Process Topology

### 2.1 Core Bridge (Always-On)

```text
MCP Host (LLM app)
  -> stdio JSON-RPC
server/main.py
  -> TCP 127.0.0.1:9876
freecad_addon/socket_server.py (inside FreeCAD GUI)
```

This is the primary two-process architecture for live CAD work.

### 2.2 Optional Sidecars

- `study.run` starts a background subprocess: `python -m server.study_runner <study_id>`.
- `motion.simulate` can connect to optional Chrono daemon (`127.0.0.1:9877`), Isaac bridge (`127.0.0.1:9878`), or Gazebo bridge (`127.0.0.1:9879`).

If sidecars are absent, base CAD/ME/spec workflows remain available.

## 3. Transport and RPC Semantics

### 3.1 MCP Host <-> Bridge

`server/main.py` supports both:

- LSP/MCP framing (`Content-Length` headers)
- newline-delimited JSON fallback (for manual/debug use)

Reference implementation path: `server/main.py` message reader (`_read_message`).

### 3.2 Bridge <-> FreeCAD Addon

Socket protocol is newline-delimited JSON:

- Request: `{"cmd":"pad","args":{...}}`
- Response: `{"ok":true,"result":{...}}` or `{"ok":false,"error":"..."}`

Client: `server/freecad_client.py`
Server: `freecad_addon/socket_server.py`

### 3.3 Bridge <-> Chrono Daemon (Optional)

TCP newline-delimited JSON client in `server/chrono_client.py`.
Default endpoint: `127.0.0.1:9877`.

## 4. MCP Protocol Surface

### 4.1 Tools (81 total)

| Family | Count | Module |
|---|---:|---|
| `cad.*` | 33 | `server/tools_cad.py` |
| `mfg.*` | 3 | `server/tools_mfg.py` |
| `spec.*` | 10 | `server/tools.py` |
| `me.*` | 5 | `server/tools_me.py` |
| `knowledge.*` | 5 | `server/tools_knowledge.py` |
| `geometry.*` | 5 | `server/tools_geometry.py` |
| `study.*` | 7 | `server/tools_study.py` |
| `motion.*` | 13 | `server/tools_motion.py` |

#### 4.1.1 `cad.*` (33)

`cad.new_document`, `cad.new_body`, `cad.sketch`, `cad.pad`, `cad.revolution`, `cad.polar_pattern`, `cad.pocket`, `cad.sweep`, `cad.helix`, `cad.loft`, `cad.hole`, `cad.fillet`, `cad.chamfer`, `cad.get_selection`, `cad.get_model_tree`, `cad.get_dimensions`, `cad.get_body_topology`, `cad.find_edges`, `cad.define_selection`, `cad.resolve_selection`, `cad.list_selections`, `cad.delete_selection`, `cad.screenshot`, `cad.set_camera`, `cad.get_camera`, `cad.undo`, `cad.export`, `cad.delete_objects`, `cad.set_placement`, `cad.set_visibility`, `cad.animate`, `cad.animate_stop`, `cad.freecad_info`.

#### 4.1.2 `mfg.*` (3)

`mfg.set_property`, `mfg.readiness_check`, `mfg.export_rfq`.

#### 4.1.3 `spec.*` (10)

`spec.select_schema`, `spec.apply_answer`, `spec.validate`, `spec.next_question`, `spec.finalize`, `spec.export_brief`, `spec.export_rfq_summary`, `spec.assess_design_path`, `spec.generate_cad`, `spec.plan_geometry`.

#### 4.1.4 `me.*` (5)

`me.validate_constraints`, `me.build_traceability`, `me.apply_risk_gates`, `me.design_loop`, `me.list_validators`.

#### 4.1.5 `knowledge.*` (5)

`knowledge.extract`, `knowledge.ingest`, `knowledge.ingest_status`, `knowledge.search`, `knowledge.status`.

#### 4.1.6 `geometry.*` (5)

`geometry.spur_gear`, `geometry.tooth_slot`, `geometry.gear_params`, `geometry.planetary_layout`, `geometry.involute_points`.

#### 4.1.7 `study.*` (7)

`study.create`, `study.run`, `study.status`, `study.results`, `study.cancel`, `study.list`, `study.get_variant`.

#### 4.1.8 `motion.*` (13)

`motion.define_mechanism`, `motion.list_mechanisms`, `motion.validate`, `motion.propagate_motion`, `motion.check_gear_train`, `motion.create_assembly`, `motion.drive_joint`, `motion.check_interference`, `motion.simulate`, `motion.teleop_start`, `motion.teleop_command`, `motion.teleop_state`, `motion.teleop_stop`.

### 4.2 Prompts (2)

- `cad_copilot_system`
- `knowledge_research_workflow`

## 5. FreeCAD Addon Command Surface

Command registry in `freecad_addon/commands.py` includes:

- Document: `new_document`, `new_body`, `get_model_tree`, `undo`, `redo`
- Sketcher: `new_sketch`, `sketch_rect`, `sketch_circle`, `sketch_line`, `sketch_arc`, `sketch_bspline`, `sketch_constrain`, `sketch_populate`, `close_sketch`
- PartDesign and helpers: `pad`, `resolve_pocket_direction`, `pocket`, `revolution`, `polar_pattern`, `hole`, `sweep`, `helix`, `loft`, `fillet`, `chamfer`
- Query and selection: `get_selection`, `get_dimensions`, `get_body_topology`, `find_edges`, `define_selection`, `resolve_selection`, `list_selections`, `delete_selection`
- Visualization/visibility: `screenshot`, `set_camera`, `get_camera`, `set_visibility`
- Export: `export`, `export_body`
- Assembly: `assembly_create`, `assembly_add_part`, `assembly_add_joint`, `assembly_solve`, `assembly_drive_joint`, `assembly_check_interference`, `assembly_get_placements`

## 6. Motion Validation Pipeline

Implementation modules:

- Models: `server/motion_models.py`
- Store: `server/motion_store.py`
- Validators: `server/motion_validators.py`
- MCP wrapper tools: `server/tools_motion.py`
- Optional dynamic clients: `server/chrono_client.py`, `server/isaac_client.py`
- Isaac sidecar runtime: `isaac_bridge/bridge_server.py`, `isaac_bridge/runtime_isaac.py`, `isaac_bridge/protocol.py`

Tiered behavior:

1. Tier 0 mechanism definition
- `motion.define_mechanism` validates shape, stores mechanism, returns `mechanism_id`.

2. Tier 1 analytical checks (no FreeCAD/Chrono required)
- Ratio consistency, speed/torque propagation, power, DOF, planetary/linkage heuristics, expected-output matching.

3. Tier 2 kinematic FreeCAD Assembly checks
- Builds assembly links/joints, solves constraints, drives joints, checks interference.

4. Tier 3 dynamic simulation via selected backend
- `motion.simulate` supports `backend=chrono|isaac|gazebo` (default `isaac`).
- `motion.simulate` supports `mode=batch|teleop` (teleop for Isaac and Gazebo; Chrono is batch-only).
- `motion.simulate` accepts optional `profile` for Isaac runtime overrides.
- For Gazebo backend, `urdf_path` or `sdf_path` is required (SDF preferred for drone workflows).
- If requested backend is absent, returns deterministic `BACKEND_UNAVAILABLE_CHOOSE` with explicit retry choices (no implicit fallback).
- Teleop lifecycle is exposed via `motion.teleop_start`, `motion.teleop_command`, `motion.teleop_state`, `motion.teleop_stop`.
- Gazebo teleop controller contract: `profile.controller_type in {multirotor_direct, px4_offboard}`.
- Isaac bridge v1 supported joints: `revolute`, `prismatic`, `fixed`.
- Isaac bridge v1 returns deterministic `UNSUPPORTED_JOINT_TYPE` for `gear_mesh`, `belt_chain`, `cam`, `planar`.

## 7. Study Pipeline

Core modules:

- API wrappers: `server/tools_study.py`
- Models: `server/study_models.py`
- Persistence: `server/study_store.py`
- Runner: `server/study_runner.py`
- Solvers: `server/study_solvers.py`

Execution model:

1. `study.create`
- Validates variables/objective/solver.
- Stores study under `studies/<study_id>/study.json`.
- Returns coarse/refined variant count estimates and ETA.

2. `study.run`
- Spawns `server.study_runner` subprocess.

3. `study_runner`
- Executes coarse cartesian sweep.
- Refines near best coarse point.
- Ranks feasible variants and sets `best_variant_id`.

4. `study.status` / `study.results`
- Poll progress, ETA, and ranked outputs.

Solver registry currently includes:

- `mock`: deterministic test solver.
- `bemt_xfoil`: scaffolded (not yet implemented).
- `openfoam`: partial pipeline (geometry stage implemented; solve stage currently stubbed).
- `chrono`: dynamic metrics via Chrono daemon.

## 8. Knowledge Subsystem

Modules:

- Tool layer: `server/tools_knowledge.py`
- Store: `server/knowledge_store.py`

Modes:

- Extract: parse a file and return text (`knowledge.extract`).
- Ingest: synchronous extract/chunk/embed/store (`knowledge.ingest`).
- Search: semantic/hybrid search (`knowledge.search`).
- Status: store/index health (`knowledge.status`).

Fallback behavior:

- If store dependencies are unavailable, tools degrade to local `me_knowledge/notes` listing where applicable.

## 9. LLM Communication Semantics

### 9.1 Deterministic spec mutation contract

`spec.apply_answer` uses JSON pointer paths with operations:

- `set`
- `append`
- `remove`

This is intentionally smaller and more LLM-robust than full RFC6902 patch arrays.

### 9.2 Handle-based bulk geometry transfer

`geometry.*` tools return `geometry_ref` handles; raw element arrays stay server-side in `server/geometry_store.py`.
`cad.sketch` resolves these handles server-side before calling FreeCAD.

### 9.3 Batched sketch creation

`cad.sketch` is a compound call:

- `new_sketch`
- single batched `sketch_populate`
- `close_sketch`

This avoids per-element RPC chatter and recompute storms.

### 9.4 Structured spatial feedback

Feature operations return structured data used by the LLM for grounded reasoning:

- `face_map`
- `operation_summary`
- digest/delta metadata
- verification images
- `selection_drift` status for named selectors

## 10. Geometry Planning and Verification Pipeline

Spec-driven geometry path:

```text
spec -> GIR -> strategy/planning policy -> EIR -> compiler/executor -> CAD operations
```

Primary modules:

- `server/geometry_ir.py`
- `server/geometry_planning.py`
- `server/geometry_planner.py`
- `server/geometry_compiler_freecad.py`
- `server/geometry_executor.py`
- `server/geometry_constraints.py`
- `server/geometry_references.py`
- `server/geometry_verify.py`

Policy/capability files:

- `feature_support/geometry_capabilities.yml`
- `feature_support/verification_policy.yml`
- `feature_support/planning_policy.yml`

## 11. Determinism and Reliability Guarantees

- Stable sorting and deterministic thresholds in ME/motion validators.
- Deterministic spec finalization and hashing via canonicalization (`server/jcs.py`).
- Session-scoped deterministic handle stores (`geometry_store`, `motion_store`).
- Explicit error codes for dependency absence (FreeCAD connection errors, Chrono availability errors, tool-level structured errors).
- Unit-test coverage across tool surfaces, models, stores, and runners.

## 12. Module Dependency Map

```text
                              server/main.py
                                  |
      ---------------------------------------------------------------
      |        |       |      |        |         |        |         |
   cad.*     mfg.*   spec.*  me.*  knowledge.* geometry.* study.* motion.*
      |                                        |         |         |
 freecad_client.py                        geometry_store  study_runner chrono_client
      |
 freecad_addon (socket_server + commands)
```

## 13. File Map (Key Runtime Files)

- `server/main.py`: MCP server, tool schemas, dispatch, framing support
- `server/tools_cad.py`: CAD MCP wrappers
- `server/tools.py`: spec tools
- `server/tools_me.py`: ME deterministic loop wrappers
- `server/tools_knowledge.py`: knowledge workflows
- `server/tools_geometry.py`: geometry generators
- `server/tools_study.py`: study API wrappers
- `server/tools_motion.py`: motion tool wrappers
- `server/study_runner.py`: background study execution engine
- `server/study_solvers.py`: solver adapters/registry
- `server/chrono_client.py`: optional Chrono TCP client
- `freecad_addon/commands.py`: FreeCAD-side command handlers
- `freecad_addon/socket_server.py`: FreeCAD-side TCP server

## 14. Design Tradeoffs

1. Keep the core CAD path simple and always available (two-process bridge).
2. Add specialized simulation capabilities as opt-in sidecars (study runner, Chrono).
3. Keep high-value engineering logic deterministic while using LLMs for intent translation and orchestration.
4. Use handle-based dataflow and structured operation feedback to reduce LLM token load and ambiguity.
