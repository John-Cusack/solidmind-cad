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
                               ├─ TCP socket :9878 ──▶ Isaac Bridge (isaac_bridge/)
                               │                      GPU physics sim + teleop (optional)
                               ├─ TCP socket :9879 ──▶ Gazebo Bridge (gazebo_bridge/)
                               │                      CPU physics + ROS/PX4 (optional)
                               ├─ LanceDB (in-process, me_knowledge/lancedb/)
                               ├─ Docling (in-process, pip package)
                               └─ Ollama (optional, for GPU embeddings)
```

**MCP bridge server** (`server/main.py`): Launched by Claude Code via stdio. Connects to FreeCAD addon over TCP socket (localhost:9876). Translates MCP tool calls into FreeCAD commands and FreeCAD selection into MCP responses.

**FreeCAD addon** (`freecad_addon/`): Runs inside FreeCAD's GUI process. Socket server in a background thread accepts JSON commands, executes FreeCAD Python API, returns results. Selection observer tracks user clicks on geometry.

**Chrono daemon** (`chrono_daemon/`): Optional standalone C++ binary for Tier 3 dynamic simulation via Project Chrono. TCP socket server on localhost:9877 (same JSON protocol as FreeCAD addon). Builds Chrono multibody systems from mechanism definitions, runs time-domain simulations, returns time-series results. Only needed for `motion.simulate` — Tier 1 analytical validation works without it. See `chrono_daemon/README.md` for build instructions.

**Isaac bridge** (`isaac_bridge/`): Optional Python TCP sidecar on localhost:9878 for Tier 3 `backend=isaac` simulation and teleop lifecycle. Exposes newline-delimited JSON commands (`ping`, `simulate`, `teleop_*`). Start with `scripts/run_isaac_bridge.sh`. v1 supported joints: `revolute`, `prismatic`, `fixed`; unsupported joints return `UNSUPPORTED_JOINT_TYPE`. Teleop uses a `Controller` protocol for pluggable actuation — currently `HexapodTripodController` (1-DOF tripod gait with slew filtering, yaw differential, height offset). Profile keys configure the controller (amplitude, stride frequency, slew rates, etc.).

**Gazebo bridge** (`gazebo_bridge/`): Optional Python TCP sidecar on localhost:9879 for Tier 3 `backend=gazebo` simulation and teleop lifecycle. Same newline-delimited JSON protocol as Isaac bridge. Start with `scripts/run_gazebo_bridge.sh`. Best for drones (PX4 SITL integration in phase 3), wheeled vehicles, and CPU-only environments. Teleop commands support 5-DOF velocity (`vx_mps`, `vy_mps`, `vz_mps`, `yaw_rate_rps`, `body_height_m`) vs Isaac's 3-DOF (`vx_mps`, `yaw_rate_rps`, `body_height_m`).

### Key modules

**`freecad_addon/`:**
- `__init__.py` — Package init, `start()` / `stop()` entry points
- `protocol.py` — Newline-delimited JSON command/response protocol
- `socket_server.py` — TCP server on localhost:9876, background thread, command dispatch
- `selection_observer.py` — FreeCADGui.Selection observer, tracks clicked faces/edges/vertices
- `commands.py` — FreeCAD API command handlers (document, body, sketch, pad, pocket, hole, fillet, chamfer, mirror, linear_pattern, thickness, draft, export, undo, selection, model tree)

**`server/`:**
- `main.py` — MCP JSON-RPC stdio server, registers cad.*, mfg.*, me.*, design.* tools
- `freecad_client.py` — TCP socket client connecting to FreeCAD addon, retry/reconnect logic
- `tools_cad.py` — CAD MCP tool implementations (cad.new_document, cad.sketch, cad.pad, cad.pocket, cad.hole, cad.fillet, cad.chamfer, cad.mirror, cad.linear_pattern, cad.thickness, cad.draft, cad.get_selection, cad.get_model_tree, cad.measure_between, cad.undo, cad.export, cad.assembly_audit, cad.register_placement_plan). `get_model_tree` returns position, rotation, and world bounding box per body for spatial overview. `assembly_audit` detects CLUSTER/ISOLATED/OVERLAP/DRIFT anomalies in multi-body assemblies (auto-uses registered placement plan for DRIFT). `set_placement` returns `plan_check` when a placement plan is registered.
- `tools_mfg.py` — Manufacturing readiness tools (mfg.set_property, mfg.readiness_check, mfg.export_rfq)
- `tools_me.py` — ME design-loop tools (deterministic validation, traceability, risk gates)
- `tools_study.py` — Parametric study tools (create, run, status, results, cancel, list, get_variant)
- `tools_motion.py` — Motion validation tools (Tier 1: define_mechanism, validate, propagate_motion, check_gear_train; Tier 1.5: check_joint_connectivity; Tier 2: create_assembly, drive_joint, check_interference; Tier 3: simulate, verify_sim_package, teleop_start/command/state/stop)
- `motion_models.py` — Mechanism data models (PartNode, JointEdge, DriveCondition, Mechanism)
- `motion_store.py` — Session-scoped mechanism storage (UUID handles)
- `motion_validators.py` — Analytical validators (gear ratio, speed/torque propagation, DOF, Grashof, power conservation)
- `chrono_client.py` — TCP client to Chrono daemon on localhost:9877 (Tier 3 dynamic simulation)
- `isaac_client.py` — TCP client to Isaac bridge on localhost:9878 (Tier 3 dynamic simulation + teleop)
- `gazebo_client.py` — TCP client to Gazebo bridge on localhost:9879 (Tier 3 dynamic simulation + teleop)
- `gazebo_adapter.py` — Error-wrapping adapter for Gazebo bridge (GAZEBO_* error codes)
- `study_models.py` — Study data models (Study, DesignVariable, Variant, SolverConfig, ObjectiveConfig)
- `study_store.py` — JSON-file persistence for studies in `studies/<study_id>/`
- `study_runner.py` — Background subprocess runner (coarse sweep → refine → rank)
- `study_solvers.py` — Solver adapters (MockSolver, BEMTXfoilSolver stub, OpenFOAMSolver stub, ChronoSolver)
- `tools_knowledge.py` — Knowledge management tools (extract, ingest, search via LanceDB)
- `knowledge_store.py` — In-process knowledge store (LanceDB + Docling, module-level singleton)
- `prompts.py` — System prompts including `cad_copilot_system` for live co-pilot mode
- `models.py` — Finding, Severity, ToolError, ConversationSignals
- `design_models.py` — DesignBrief, PartEntry, InterfaceEntry dataclasses (frozen, __slots__)
- `design_store.py` — Session-scoped design brief storage (module-level dict, token_hex handles, part/interface mutations)
- `tools_design.py` — Design brief MCP tools (design.save_brief, design.get_brief, design.update_brief, design.add_part, design.update_part, design.get_part, design.add_interface, design.list_briefs, design.verify_build)
- `fastener_data.py` — ISO metric fastener dimension tables (M2-M24, 5 head types, clearance holes, counterbores, tap drills)
- `tools_fastener.py` — Fastener lookup MCP tool (cad.fastener_spec)

**`isaac_bridge/`:**
- `bridge_server.py` — TCP server + main-thread pump loop (Kit event loop, teleop ticking)
- `runtime_isaac.py` — Isaac runtime: URDF import, simulation, teleop lifecycle, tick_teleop, DOF mapping
- `models.py` — `TeleopConfig` (validated profile), `TeleopState`, `Controller` protocol, `SimulationSession`, `URDFImportConfig`
- `controllers.py` — `HexapodTripodController` (1-DOF tripod gait), `PolicyController` (RL residual blending), `create_controller()` registry, `clamp_targets()` utility
- `keyboard_teleop.py` — `KeyboardTeleopMapper` (key→command mapping, no Isaac dependency)

**`gazebo_bridge/`:**
- `bridge_server.py` — TCP server on localhost:9879 (no main-thread pump needed)
- `runtime_gazebo.py` — Gazebo runtime: stub command handlers, session tracking for teleop
- `models.py` — `GazeboConfig` (frozen dataclass), `GazeboSession` (mutable session state with 5-DOF teleop)

**`scripts/`** (simulation-related):
- `run_isaac_bridge.sh` — Launch the Isaac bridge sidecar
- `run_gazebo_bridge.sh` — Launch the Gazebo bridge sidecar (optional `--launch-gz` flag)
- `isaac_keyboard_teleop.py` — Standalone keyboard teleop client (W/A/S/D/Q/E, 20 Hz, raw terminal)
- `smoke_test_isaac.py` — TCP smoke test client for the Isaac bridge

### Tool groups

| Group | Tools | Purpose |
|-------|-------|---------|
| `cad.*` | new_document, new_body, sketch, pad, pocket, hole, fillet, chamfer, mirror, linear_pattern, thickness, draft, create_primitive, create_primitives, get_selection, get_model_tree, undo, export, measure_between, fastener_spec, assembly_audit, register_placement_plan, clear_placement_plan | Drive FreeCAD PartDesign + measurement + fastener dimension lookup + spatial audit + placement plan validation |
| `mfg.*` | set_property, readiness_check, export_rfq | Manufacturing readiness (on-demand) |
| `me.*` | validate_constraints, build_traceability, apply_risk_gates, design_loop, list_validators | Deterministic ME preflight (validators + risk gates) |
| `knowledge.*` | extract, ingest, ingest_status, search, status | Knowledge base — hybrid search, PDF extraction, document ingestion (LanceDB + Docling) |
| `study.*` | create, run, status, results, cancel, list, get_variant | Parametric design optimization (sweep variables, run solvers, rank results) |
| `motion.*` | define_mechanism, list_mechanisms, validate, propagate_motion, check_gear_train, check_joint_connectivity, create_assembly, drive_joint, check_interference, simulate, verify_sim_package, teleop_start, teleop_command, teleop_state, teleop_stop | Motion validation pipeline — analytical (Tier 1), check_joint_connectivity (Tier 1.5, pre-export), kinematic via FreeCAD Assembly (Tier 2), dynamic via Isaac/Gazebo/Chrono (Tier 3), sim package verification |
| `design.*` | save_brief, get_brief, update_brief, add_part, update_part, get_part, add_interface, list_briefs, verify_build | Design brief pipeline — phased assembly design with parts decomposition, interface tracking, and build verification |
| `geometry.*` | spur_gear, tooth_slot, gear_params, planetary_layout, involute_points, propeller_blade, epicycloidal_tooth_slot, spiral, spoke_pattern, ratchet_tooth, gear_train_solver, keyway_profile, oring_groove, section_properties, belt_drive, bevel_gear, worm_gear, thread_profile, helical_spring, cam_profile, four_bar | Parametric geometry generators — involute/bevel/worm gears, epicycloidal gears, planetary layouts, propeller blades, spirals, spoke patterns, ratchets, gear train solving, keyways, O-ring grooves, section properties, belt/chain drives, threads, springs, cams, four-bar linkages (Rust + Python) |

### Sketch element types

`cad.sketch` supports **all** element types: `rect`, `circle`, `line`, `arc`, **`spline`** (B-spline curves from control points with degree/weights/periodic options), `external_ref` (project edges from existing features), `sketch_fillet` (round sketch vertices), and `sketch_chamfer` (chamfer sketch vertices). Splines are fully implemented and must be used for smooth contours, airfoils, blade profiles, and organic shapes — never approximate with line segments. Any element can have `"construction": true` to make it a reference line/circle. Constraints use partial recovery — a single failed constraint won't abort the sketch.

### Interaction flow

1. User describes what they want → LLM decides complexity level.
2. **Simple parts** (single-body, dimensions given or obvious): skip to step 4.
3. **Assemblies and complex parts** (multi-body, needs research, purchased components involved): use the phased design pipeline (see below).
4. LLM calls `cad.new_document` → `cad.new_body` → `cad.sketch` → `cad.pad` / `cad.pocket` → finishing features.
5. Each step returns verification images — LLM examines them to confirm geometry.
6. User clicks face/edge in FreeCAD → LLM calls `cad.get_selection` → uses references in follow-up commands.
7. User provides a PDF → `knowledge.extract(file_path)` to read it, then `knowledge.ingest(path)` to index.
8. User asks to optimize → LLM uses `study.*` tools to sweep design space, then builds the winner.

### Design pipeline (phased approach)

The `design.*` tools support a **phased design process** that mirrors how an experienced CAD designer works: understand intent, size the system, define the layout, then build parts. Each phase has a user gate — the LLM presents its work and the user confirms before moving on.

**When to use the phased pipeline:**
- Multi-body assemblies (robots, drones, mechanisms, vehicles)
- Designs involving purchased components (motors, fasteners, electronics)
- The design requires research or engineering calculations before geometry
- Multiple parts that must interface with each other

**Skip the pipeline when:**
- Simple single-body parts where the user gives all dimensions
- Quick modifications to existing models
- User explicitly says "just build it"

#### Phase 1: Intent

Clarify what's being built and why. The LLM asks targeted questions:
- **What** is it? (5" racing quad, 6-DOF robot arm, two-stage gearbox)
- **What for?** (racing, photography, precision assembly)
- **Hard constraints?** (must use specific motors, max weight, must fit in a box)

No tools needed — just conversation. Output: a clear intent summary.

```
design.save_brief(name="Racing Quadcopter", parameters={
    "intent": "5-inch racing quadcopter, lightweight, acro-capable",
    "constraints": {"max_auw_g": 600, "prop_size_in": 5}
}, status="intent")
```

Gate: LLM presents its understanding → user confirms → move to sizing.

#### Phase 2: Sizing

Engineering calculations and component selection. The pattern is always **requirements → candidate components → check if the numbers close → iterate**.

Example (drone):
```
target AUW ~500g → need ~2kg thrust (4:1) → 4 motors @ 500g each
→ search for "2306 motor 5 inch prop thrust data"
→ select Emax 2306: 680g thrust on 5x4.3, M3 16mm bolt pattern, 33g
→ battery: 4S 1300mAh, 180g
→ revised AUW: 520g, thrust margin 5.2:1 ✓
```

**Ask the user about purchased parts.** Don't guess — ask: "Do you have specific motors, props, or electronics in mind?" Then research the specs:
- `knowledge.search("Emax 2306 motor specs")` or web search for datasheets
- Extract interface dimensions: bolt patterns, shaft sizes, mounting holes
- These become hard constraints that flow into every connected custom part

Register each component:
```
design.add_part(brief_id, name="motor", kind="purchased", quantity=4,
    specs={"model": "Emax 2306", "mass_g": 33, "mount_pattern": "M3_16mm_square",
           "shaft_mm": 5, "max_thrust_g": 680})
design.add_part(brief_id, name="frame_plate", kind="custom", quantity=1,
    specs={"material": "CF 2mm", "role": "central structure"})
design.add_part(brief_id, name="arm", kind="custom", quantity=4,
    specs={"material": "CF tube 10mm OD", "role": "motor support"})
design.add_part(brief_id, name="motor_mount", kind="custom", quantity=4,
    specs={"role": "connects motor to arm tip"})
```

Gate: LLM presents component table + weight budget → user confirms → move to layout.

#### Phase 3: Layout

Define spatial relationships — where everything goes and how parts connect. Dimensions are **derived from sizing**, not guessed:
- Prop clearance (15mm min) + prop diameter (5") → minimum arm length
- Motor mount bolt pattern → from motor datasheet, not LLM arithmetic
- FC stack mount → standard 30.5mm or 20mm pattern

Define interfaces between parts:
```
design.add_interface(brief_id,
    part_a="motor_mount", port_a="top",
    part_b="motor", port_b="base",
    spec={"pattern": "M3_16mm_square", "bolt_size": "M3"})
design.add_interface(brief_id,
    part_a="motor_mount", port_a="bottom",
    part_b="arm", port_b="tip",
    spec={"type": "clamp", "tube_od_mm": 10})
design.add_interface(brief_id,
    part_a="arm", port_a="root",
    part_b="frame_plate", port_b="arm_slot",
    spec={"pattern": "M3_bolt_pair", "spacing_mm": 15})
```

Add layout positions to the brief parameters:
```
design.update_brief(brief_id, parameters={
    "layout": {
        "arm_length_mm": 110,
        "arm_angles_deg": [45, 135, 225, 315],
        "motor_positions": [[77.8, 77.8, 8], [77.8, -77.8, 8], ...],
        "center_body": {"width": 36, "length": 45}
    }
})
```

Gate: LLM presents the layout (arm lengths, positions, interface summary) → user approves → move to building.

**Part decomposition for articulated mechanisms:**

For robots, hexapods, arms, and any design with joints, decompose parts by
**kinematic segment** — the rigid portion between two joints — not by component type.

Anti-pattern (creates overlapping bodies with seams):
```
design.add_part(brief_id, name="coxa_servo", kind="custom", quantity=6, ...)
design.add_part(brief_id, name="coxa_arm", kind="custom", quantity=6, ...)
# Result: 12 separate bodies that overlap at every servo-arm junction
```

Correct pattern (one body per kinematic segment):
```
design.add_part(brief_id, name="coxa_servo", kind="purchased", quantity=6,
    specs={"model": "SG90", "body_mm": [23, 12.2, 22], ...})
design.add_part(brief_id, name="coxa_segment", kind="custom", quantity=6,
    specs={"role": "rigid link: coxa joint → femur joint",
           "integrates": ["coxa_servo pocket", "structural arm"],
           "profile": "L-shaped composite"})
# Servo is purchased (for specs/mass), segment is custom (gets built as one body)
```

Interfaces for articulated mechanisms define joints between segments:
```
design.add_interface(brief_id,
    part_a="chassis", port_a="coxa_pivot",
    part_b="coxa_segment", port_b="proximal",
    spec={"type": "revolute", "axis": [0,0,1], "servo": "coxa_servo"})
design.add_interface(brief_id,
    part_a="coxa_segment", port_a="distal",
    part_b="femur_segment", port_b="proximal",
    spec={"type": "revolute", "axis": [0,1,0], "servo": "femur_servo"})
```

#### Phase 4: Build (micro pipeline per part)

Now build each custom part. The build process depends on whether the design is articulated.

**For static assemblies** (drones, enclosures, brackets — no revolute/prismatic joints between custom parts):

For each part:
1. `design.get_part(brief_id, "motor_mount")` — pull its interfaces and specs
2. `cad.new_body(label="motor_mount")` — create the body
3. Build geometry where **hole patterns, bore sizes, and mating surfaces come from the interface spec**
4. Verify with screenshots — confirm dimensions match interface spec
5. Move to next part

Build order follows dependencies: frame first, then arms, then motor mounts, then accessories.

**For articulated mechanisms** (robots, hexapods, arms, linkages — has revolute/prismatic joints):

Step 0 — Identify kinematic segments:
Walk the joint tree from chassis to leaf. Each rigid group between two joints = ONE body.
Purchased servos/motors are NOT separate bodies — their form becomes pockets in the segment body.

Step 1 — Build each segment as ONE composite body:
1. `design.get_part(brief_id, "coxa_segment")` — pull specs + interfaces
2. `cad.new_body(label="coxa_segment_L1")` — one body for the whole segment
3. `cad.sketch` with composite profile:
   - Wide rectangular section matching servo body dimensions (from purchased part specs)
   - Narrower arm section extending from the servo housing
   - Result: L-shaped, T-shaped, or stepped outline — one closed contour
4. `cad.pad` the composite profile to segment thickness
5. `cad.pocket` to cut the servo cavity (sized from purchased part specs)
6. `cad.hole` for joint pivot holes at each end, servo horn slot, wire routing
7. `cad.fillet` transitions between wide and narrow sections
8. Verify with screenshots — confirm continuous solid, no floating geometry

Step 2 — Verify segment integrity:
After all segments: confirm each kinematic segment is exactly one body, no separate
servo/motor bodies exist, joint pivot points are correctly placed.

Do NOT use `cad.create_primitives` for the final build of articulated mechanisms — it
creates separate overlapping bodies. Use it only for early layout visualization.

```python
design.update_brief(brief_id, status="building")

# For each kinematic segment:
part = design.get_part(brief_id, "coxa_segment")
# part.specs has servo dimensions, arm length, profile type
# part.interfaces has joint positions, pivot hole specs
# → build composite body from those values
design.update_part(brief_id, "coxa_segment", body_label="coxa_segment_L1", status="built")
```

After all parts: `design.update_brief(brief_id, status="done")`

#### Phase lifecycle

```
intent → sizing → layout → approved → building → verify → done
         ↑ user    ↑ user    ↑ user              ↑ auto
         gate      gate      gate                 design.verify_build
```

Each gate is a natural conversation point where the LLM presents its work and the user confirms, adjusts, or redirects. The verify gate is automatic — `design.verify_build` checks that all planned parts exist before marking done.

#### Adapts to any domain

The phases are universal — only the sizing calculations change:

| Domain | Sizing focus |
|--------|-------------|
| Drone | weight budget, thrust-to-weight, prop clearance |
| Gearbox | gear ratios, shaft torque, bearing loads |
| Robot arm | joint torques, servo selection, link lengths |
| Articulated mechanism | kinematic segment decomposition, servo pocket integration, joint placement |
| Enclosure | component clearances, thermal, IP rating |

The LLM applies its engineering knowledge at each phase. The tools provide structure and memory — the LLM provides the engineering judgment.

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
   - `backend=isaac` — GPU physics via Isaac Sim (`scripts/run_isaac_bridge.sh`). Best for legged robots (hexapods, bipeds), articulated mechanisms, and anything needing GPU-accelerated contact. Teleop supports `vx_mps`, `yaw_rate_rps`, `body_height_m`.
   - `backend=gazebo` — CPU physics via Gazebo (`scripts/run_gazebo_bridge.sh`). Best for drones (PX4 SITL in phase 3), wheeled/tracked vehicles, and CPU-only environments. Teleop supports 5-DOF: adds `vy_mps` (lateral) and `vz_mps` (vertical) for flight.
   - `backend=chrono` — C++ multibody via Project Chrono (batch only, no teleop). Best for gear trains, linkages, and mechanisms where analytical torque/speed propagation matters.

   **Backend selection heuristic:**
   - Drone / multirotor / fixed-wing → `gazebo` (PX4 ecosystem, 5-DOF teleop)
   - Wheeled vehicle / rover → `gazebo` (ROS ecosystem, lateral velocity)
   - Legged robot / hexapod / biped → `isaac` (GPU contact, existing tripod controller)
   - Articulated arm / manipulator → `isaac` (GPU physics, joint-level control)
   - Gear train / linkage / cam → `chrono` (analytical MBS, batch validation)
   - User has no GPU / CPU-only → `gazebo`
   - User explicitly requests a backend → use what they ask for

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
