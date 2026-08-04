# Implementation Prompt — Step 1: Contract + Handshake

> Paste everything below this line into a fresh Claude Code session on a clean branch
> cut from this one.

---

You are implementing **Step 1 of 7** of the engine-extraction migration for
solidmind-cad. The governing design is `docs/engine-integration-architecture.md` — read
it in full before writing any code, especially §2 (principles), §3 (contract), and §7
(migration). If you must deviate from it, amend that document in the same commit and say
so explicitly in your final report. Also read `CLAUDE.md` for style and commands.

## Goal

Publish Engine Integration Contract v1 as documents + schemas, add the `hello`
handshake and `request_id` passthrough to all three engine bridges, and make
`cad.export_sim_package` write the canonical `manifest.json` to disk. Everything is
**additive** — zero behavior changes to existing commands, all existing tests keep
passing.

## Out of scope — do NOT do these (they are later steps)

- Severing bridge→core imports (step 2). Do not add new ones either.
- Moving SDF/PX4/mavlink/spec-builder code (step 3).
- Registry, descriptors, engines.d, client or `sim_engine_manager` changes, deleting
  backend tables, prompt/tool-text generation (step 4).
- TCK extraction (step 5), repo split (step 6).
- **No changes to `freecad_addon/`** (requires manual FreeCAD restart to test — keep
  step 1 fully server/bridge-side).
- No changes to `server/main.py` tool schemas (the result dict may gain keys; the
  declared inputSchema does not change).

## Deliverables

### 1. `docs/engine-contract.md` — the normative spec

Sections (shapes below are normative; the architecture doc §3 is your outline):

1. Envelope & framing — NDJSON over TCP; `{"cmd","args","request_id"?}` →
   `{"ok",("result"|"error"),"request_id"?}`. `request_id` is an opaque string, echoed
   verbatim when present, omitted when absent.
2. `hello` — required verb, response `result` shape:
   ```json
   {
     "protocol_version": "1.0.0",
     "contract_versions_supported": ["1"],
     "engine": "gazebo",
     "engine_version": "<engine or bridge version string>",
     "runtime_mode": "stub",
     "capabilities": {
       "modes": ["batch", "teleop"],
       "formats": ["sdf", "urdf"],
       "features": ["diagnose", "spawn_model", "px4"],
       "fields": {"emits": [], "accepts": []}
     }
   }
   ```
3. Required verbs (`hello`, `ping`, `simulate`, `shutdown`) vs capability-gated verb
   groups (session, teleop, `diagnose`, `screenshot`, ...). Document the frozen-floor
   rule (architecture doc, Principle 10).
4. Error taxonomy — target codes: `INVALID_REQUEST`, `INVALID_JSON`,
   `UNSUPPORTED_COMMAND`, `UNSUPPORTED_CAPABILITY`, `PACKAGE_INVALID`,
   `SESSION_NOT_FOUND`, `CONTRACT_MISMATCH`, `ENGINE_ERROR`. Document the codes bridges
   emit today and their mapping; in code, only align the unknown-command path to
   `UNSUPPORTED_COMMAND` — do not mass-rename existing codes in this step.
5. Session semantics — engine-owned; survive client disconnect; engines MAY expire idle
   sessions (default: no expiry); `shutdown` drains.
6. Sim package format (§3.1 of the architecture doc, plus the manifest spec below).
7. Result schema (§3.3) — including the file-spill convention for large telemetry.
8. Field snapshots (§3.4) — format + sidecar; note no producer exists yet.
9. Lifecycle & discovery (§3.5) — descriptor format documented now, implemented in
   step 4.
10. Versioning & stability promise (§3.7 / Principle 10).

Version literals (`"1.0.0"`) are **duplicated in each bridge on purpose** (contract is
data, not code — Principle 2). Do not create a shared constants import between `server/`
and any bridge.

### 2. Three JSON Schemas in `schemas/`

Follow the house style of the existing files there (`common.schema.json`,
`gir.schema.json`, ...) — same draft, `$id` conventions, and shared-definitions usage.

- `sim_package.schema.json` — validates `manifest.json` (spec below).
- `sim_result.schema.json` — validates simulate results. Required:
  `time_series` (array), `summary` with `simulation_time_s`, `dt_s`, `engine_mode`.
  Optional but shape-pinned when present: `summary.peak_joint_forces`,
  `time_series[].joint_efforts` (these are consumed by
  `server/analysis_sim_coupling.py:103` — check the real consumer before writing the
  schema). `additionalProperties: true` — engines may extend.
- `field_snapshot.schema.json` — sidecar only: `schema_version`, `quantity` (enum:
  `temperature`, `heat_flux`, `pressure`, `displacement`, `force`,
  `stress_von_mises`), `units`, `mesh_ref`, `time_s` (nullable for steady-state),
  `data_file`.

### 3. `hello` + `request_id` in all three bridges

Capabilities must be **derived from each bridge's actual dispatch** — enumerate the
`cmd` handlers and report truthfully (verify the lists below against code; fix the doc
if I'm wrong):

| Bridge | Where | modes | formats | features |
|---|---|---|---|---|
| gazebo (stub+real share dispatch) | `gazebo_bridge/bridge_server.py:174` | `batch`, `teleop` | `sdf`, `urdf` | `diagnose`, `spawn_model`, `px4` |
| isaac | `isaac_bridge/bridge_server.py:174` + `isaac_bridge/protocol.py` helpers | `batch`, `session`, `teleop` | `urdf` | `diagnose`, `screenshot`, `import_urdf`, `load_environment`, `reload` |
| chrono (C++) | `chrono_daemon/main.cpp` + `protocol.h` | `batch` | `chrono_spec` | (enumerate from its handlers) |

Notes:
- Gazebo reports `runtime_mode: "stub"|"real"` from its `--runtime` flag.
- `fields: {emits: [], accepts: []}` everywhere in step 1.
- `chrono_spec` is a declared transitional format; step 3 replaces it with `package`.
- Chrono is C++: follow its existing JSON handling patterns exactly. `hello` returning a
  static capability blob + `request_id` echo. If echoing `request_id` is awkward in its
  parser, implement `hello` and record the `request_id` gap in your report + the
  contract doc's conformance notes.
- Unknown command responses: align code to `UNSUPPORTED_COMMAND` in all three.

### 4. `manifest.json` written by `cad.export_sim_package`

New module `server/sim_package_manifest.py` (pure functions, no FreeCAD dependency —
unit-testable), called from `cad_export_sim_package` (`server/tools_cad.py:1053`) after
the existing export/URDF logic. The addon is untouched: the server already receives the
in-memory body manifest (`result["bodies"]`, built at `freecad_addon/commands.py:2644`)
and `output_dir` — serialize from that.

Manifest content (all SI — meters, kg, seconds, radians):

- `schema_version: "1.0.0"`, `name`, `generator` (solidmind-cad version),
  `units: {"length": "m", "mass": "kg", "angle": "rad"}` plus per-mesh native-unit
  declarations
- `links[]`: `name`; `mesh: {file: "<relative path>", unit: "mm", scale_to_m: 0.001}`;
  `world_pose: {xyz_m, quat_wxyz}` (convert the addon's mm placements);
  `mass_kg`/`inertia`/`collision` **only when a mechanism is present** (take them from
  the `SimModel` built by `build_sim_model`, `server/sim_export.py:599` — it is already
  SI). **Verify the actual mesh units and scale factor against what `write_urdf` emits
  for `<mesh>` tags and record what is true, not what this prompt assumes.**
- `joints[]`: from `SimModel` (type, parent, child, `origin_xyz_m`, `origin_rpy_rad`,
  axis, limits, effort, velocity). Empty array for bodies-only exports.
- `actuators[]`: from `drone_config` rotors when provided — abstract spec only
  (`{type: "rotor", position_m, direction, max_thrust_N, moment_constant}`); servo
  actuators can wait for a real producer.
- `sensors[]`: presence entries (`imu`, `gps`, `barometer`, `magnetometer`) when drone
  mode emits them.

Two modes, both schema-valid: full (mechanism_id present → links+joints+inertia) and
reduced (bodies-only → links with pose+mesh only). Keep the current flat mesh layout
(`<output_dir>/Body.stl`) — the schema references meshes by relative path; do not
restructure into `meshes/` in this step. The tool's result dict gains
`"manifest_path"`.

### 5. Tests

- New `tests/test_engine_contract.py`:
  - `hello` against the stub bridge (use `GazeboStubBridge` from `tests/conftest.py:594`
    or the raw-socket helper pattern from `tests/test_sim_cross_backend.py`): response
    validates against the documented shape; `request_id` echoed when sent, absent when
    not; unknown cmd → `UNSUPPORTED_COMMAND`.
  - Stub `simulate` response validates against `sim_result.schema.json` (ties the schema
    to reality — if the stub violates the schema, fix the schema or flag it, don't relax
    silently without noting it).
  - Manifest builder unit tests: fixture body manifests (+ `mechanism_factory` from
    conftest) → manifest dict → validates against `sim_package.schema.json`; assert SI
    conversion (100 mm → 0.1 m); bodies-only mode validates; joints populated from a
    mechanism; rotor actuators from a `drone_config`.
- Extend `tests/test_isaac_bridge_protocol.py` with `hello` + `request_id` cases
  (follow its existing mock-runtime pattern).
- Extend `tests/test_chrono_client.py` with a `hello` test marked
  `requires_chrono_real`. Build the daemon locally if the toolchain is present
  (`cd chrono_daemon && mkdir -p build && cd build && cmake .. && make`); if you cannot
  build it, say so in your report — do not claim the C++ path verified.
- All existing tests keep passing: `python3 -m unittest`.

## Done when

- [ ] `docs/engine-contract.md` complete per the section list
- [ ] Three schemas exist, follow house style, and load
- [ ] All three bridges + stub answer `hello` truthfully; `request_id` echoed;
      unknown-cmd → `UNSUPPORTED_COMMAND`
- [ ] `cad.export_sim_package` writes schema-valid `manifest.json` (both modes) and
      returns `manifest_path`
- [ ] New + existing tests pass (`python3 -m unittest`); `ruff check` and
      `ruff format --check` clean
- [ ] `CHANGELOG.md` entry added
- [ ] No new imports across the bridge↔server boundary in either direction
- [ ] Final report lists: capability tables as actually implemented, any deviations
      from this prompt or the architecture doc, and anything left unverified (e.g.
      chrono build)

Commit as a small series with scope prefixes (`contract:`, `server:`, `bridges:`,
`chrono:`), short imperative subjects, per `CLAUDE.md`.
