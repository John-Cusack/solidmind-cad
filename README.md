# SolidMind CAD

FreeCAD-integrated MCP CAD co-pilot for turning plain-language ideas into buildable mechanical designs.

## Goal

Make advanced CAD workflows accessible while keeping engineering logic deterministic, inspectable, and testable.

## Runtime Surface (Current)

The MCP server currently exposes **83 tools** across 8 families:

| Family | Count | Module |
|---|---:|---|
| `cad.*` | 35 | `server/tools_cad.py` |
| `mfg.*` | 3 | `server/tools_mfg.py` |
| `spec.*` | 10 | `server/tools.py` |
| `me.*` | 5 | `server/tools_me.py` |
| `knowledge.*` | 5 | `server/tools_knowledge.py` |
| `geometry.*` | 5 | `server/tools_geometry.py` |
| `study.*` | 7 | `server/tools_study.py` |
| `motion.*` | 13 | `server/tools_motion.py` |

## What It Supports

1. Live FreeCAD co-pilot modeling (`cad.*`) including sketches, solids, selection stability, cameras/screenshots, and visibility controls.
2. Manufacturing readiness and RFQ export (`mfg.*`).
3. Deterministic spec interview/finalization and policy-driven geometry planning (`spec.*`).
4. Deterministic ME preflight loop (`me.*`) with validators, traceability, and risk notices.
5. Knowledge extraction/ingestion/search with graceful local fallback (`knowledge.*`).
6. Parametric geometry generators (gears/involutes/planetary layouts) via handle-based transfer (`geometry.*`).
7. Background parametric studies (`study.*`) with coarse/refined sweeps and solver adapters.
8. Motion validation pipeline (`motion.*`) spanning analytical, kinematic, and dynamic tiers.

## Architecture At A Glance

```mermaid
flowchart LR
    U[User in FreeCAD + MCP host] --> H[LLM host]
    H -->|JSON-RPC over stdio| S[Bridge server: server/main.py]
    S -->|TCP localhost:9876| F[FreeCAD addon]
    F --> G[FreeCAD API operations]

    S -->|subprocess| R[server.study_runner]
    R --> V[Study solvers]

    S -->|TCP localhost:9877 optional| C[Chrono daemon]
    S -->|TCP localhost:9878 optional| I[Isaac bridge sidecar]
```

Core modeling remains the two-process bridge (`server/main.py` <-> `freecad_addon`).
`study.run` adds a background runner subprocess, and `motion.simulate` uses an optional Chrono sidecar daemon.

## Simulation Stack

- **Tier 1 (analytical)**: `motion.validate`, `motion.propagate_motion`, `motion.check_gear_train`.
- **Tier 2 (kinematic in FreeCAD Assembly)**: `motion.create_assembly`, `motion.drive_joint`, `motion.check_interference`.
- **Tier 3 (dynamic backend selection)**:
  - `motion.simulate` with `backend=isaac|chrono` (default: `isaac`)
  - Isaac teleop lifecycle: `motion.teleop_start`, `motion.teleop_command`, `motion.teleop_state`, `motion.teleop_stop`
  - Isaac bridge v1 supports joint types: `revolute`, `prismatic`, `fixed`
  - Unsupported for Isaac bridge v1: `gear_mesh`, `belt_chain`, `cam`, `planar` (returns `UNSUPPORTED_JOINT_TYPE`)

## LLM Interaction Contract

- `spec.apply_answer` uses JSON-pointer addressing with deterministic ops: `set`, `append`, `remove`.
- Bulk geometry is exchanged via **handles** (`geometry_ref`) rather than large arrays in model text.
- `cad.sketch` resolves `geometry_ref` server-side and uses batched `sketch_populate` for one-recompute sketch creation.
- Modeling responses include structured spatial feedback for reasoning and self-check:
  - `face_map`
  - `operation_summary`
  - verification images/views
  - `selection_drift` signals for topology-sensitive selectors

## Policy-Driven Planning (V1)

`spec.plan_geometry` supports `planning_mode=legacy|policy_v1` with process/archetype-aware planning and deterministic checkpoints (`BASE`, `INTERFACES`, `STRUCTURE`, `PATTERNS`, `FINISH`).

## Requirements

- Python `>= 3.12`
- FreeCAD `>= 1.0` (required for live `cad.*` and Tier 2 motion; FreeCAD 0.21 is **not** supported)

Optional/conditional components:

- Chrono daemon binary (required for `motion.simulate` and `study` `chrono` solver runs)
- Isaac bridge sidecar (required for Tier 3 `backend=isaac` simulation/teleop)
- OpenFOAM + `FreeCADCmd` (required for OpenFOAM study pipeline)
- Rust toolchain + maturin build path for `solidmind_geometry` extension (if missing, `geometry.*` tools return availability errors)
- LanceDB/Docling/embedding runtime for full knowledge store mode (tools degrade to local-note fallback when unavailable)

## Getting Started

### Required: core setup

Install dependencies and run the core test suite:

```bash
python3 -m pip install -e .
python3 -m unittest
```

Start the MCP server over stdio:

```bash
python3 -m server.main
# or
solidmind-cad
```

### Optional: live FreeCAD CAD workflow

Install the FreeCAD addon symlink for auto-start on FreeCAD launch:

```bash
scripts/install_freecad_addon.sh
```

### Optional: geometry extension (`geometry.*` tools)

`geometry.*` tools require the `solidmind_geometry` Rust extension.
If unavailable, those tools return availability errors while the rest of the server still works.

### Optional: simulation validation (additional)

Optional lightweight simulation validation (no external daemons):

```bash
python3 -m unittest tests.test_tools_motion tests.test_motion_isaac_integration tests.test_simulation_spec_builder tests.test_chrono_client
```

Optional runtime-backed simulation validation:

```bash
SOLIDMIND_RUN_ISAAC_E2E=1 python3 -m unittest tests.test_isaac_bridge_real_runtime
```

Optional Chrono backend runtime validation:

```bash
chrono_daemon/run.sh
```

Then in your MCP client/host, run a short manual path:

1. Call `motion.define_mechanism` with a small mechanism payload.
2. Call `motion.simulate` with `backend="chrono"` and the returned `mechanism_id`.

Optional Isaac bridge sidecar (manual runtime path for `backend=isaac`):

```bash
scripts/run_isaac_bridge.sh --host 127.0.0.1 --port 9878
```

Optional Isaac bridge env overrides:

- `SOLIDMIND_ISAAC_HOST`
- `SOLIDMIND_ISAAC_PORT`
- `SOLIDMIND_ISAAC_CONNECT_TIMEOUT_S`
- `SOLIDMIND_ISAAC_READ_TIMEOUT_S`

Optional transcript replay:

```bash
python3 scripts/replay_transcript.py tests/transcripts/cnc_L2.yml
```

## Documentation

- `ARCHITECTURE.md`: architecture and protocol surface
- `docs/isaac_bridge_protocol.md`: Isaac bridge TCP command protocol
- `docs/adr/0001-runtime-module-contracts.md`: runtime source-of-truth contract
- `SPEC_GUIDE.md`: spec interview/finalization guidance
- `schemas/planning_policy.schema.json`: planning policy schema
- `schemas/planning_plan.schema.json`: planning artifact schema
