# Engine Integration Contract v1

**Contract version:** 1 (umbrella semver `1.0.0`)
**Status:** normative — this document plus `schemas/*.schema.json` is the contract
**Design rationale:** `docs/engine-integration-architecture.md` (§2 principles, §3 contract)

A simulation engine is an **application**, not a library. It runs in its own
interpreter or as a native binary, in its own process, and talks to core over a
socket while exchanging bulky data through files. This document is everything an
engine author needs: implement four verbs, read two file formats, answer `hello`
truthfully, and the engine is integrable — in any language, without touching core.

The contract is **data, not code**. There is deliberately no shared
`solidmind-contract` package: a shared library would re-couple environments and
exclude non-Python engines. The ~60 lines of NDJSON framing and the version
literals in this document are duplicated per bridge on purpose.

---

## 1. Envelope & framing

Transport is **TCP**, payload is **newline-delimited JSON** (NDJSON). One JSON
object per line, UTF-8, no embedded newlines. The engine listens; core (or any
client) dials. Engines never dial core.

**Request:**

```json
{"cmd": "simulate", "args": {"duration_s": 1.0}, "request_id": "abc-123"}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `cmd` | string | yes | Verb name, non-empty |
| `args` | object | no (default `{}`) | Verb arguments; must be an object when present |
| `request_id` | string | no | Opaque correlation token |

**Response:**

```json
{"ok": true, "result": {...}, "request_id": "abc-123"}
{"ok": false, "error": {"code": "UNSUPPORTED_COMMAND", "message": "..."}, "request_id": "abc-123"}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `ok` | boolean | yes | Success flag |
| `result` | any | when `ok` is true | Verb-specific payload |
| `error` | object | when `ok` is false | `{code, message, details?}` |
| `request_id` | string | conditional | See below |

**`request_id` rules.** The token is **opaque** — engines MUST NOT parse,
validate, or generate it. When a request carries `request_id`, the response
MUST echo it **verbatim**. When a request omits it, the response MUST omit it
(not `null`, not `""`). This holds for error responses too, including framing
errors, whenever the field could be recovered from the malformed line. An engine
that cannot recover the token from an unparseable line MAY omit it.

Connections are long-lived and may carry many requests. Responses on a single
connection are returned in request order; `request_id` exists so that clients
that pipeline or multiplex can correlate without relying on ordering. An engine
MAY process requests concurrently, and MAY therefore respond out of order **only
if** every response carries the `request_id` of its request.

Framing errors (unparseable JSON, non-object payload, missing `cmd`) are
answered with an error response, not a dropped connection.

---

## 2. `hello` — the capability handshake

`hello` is a **required verb**. It takes no arguments and has no side effects,
so it is safe to call at any time. It is the first thing core sends after
connecting, and its answer is how core learns what the engine can do — nothing
about an engine is hardcoded in core.

**Response `result`:**

```json
{
  "protocol_version": "1.0.0",
  "contract_versions_supported": ["1"],
  "engine": "gazebo",
  "engine_version": "0.2.0",
  "runtime_mode": "stub",
  "capabilities": {
    "modes": ["batch", "teleop"],
    "formats": ["package", "sdf", "urdf"],
    "features": ["diagnose", "spawn_model", "px4"],
    "fields": {"emits": [], "accepts": []}
  }
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `protocol_version` | string (semver) | yes | Version of *this wire protocol* the engine speaks |
| `contract_versions_supported` | array of string | yes | Umbrella contract majors, e.g. `["1"]` |
| `engine` | string | yes | Engine identity slug, lowercase (`gazebo`, `isaac`, `chrono`) |
| `engine_version` | string | yes | Underlying engine version, or the bridge's version when the engine has none (stub modes) |
| `runtime_mode` | string | yes | `real`, `stub` or `unavailable` — see below |
| `capabilities` | object | yes | See below |

**`runtime_mode`** answers one question: can the caller trust numbers from
this engine?

| Value | Meaning |
|---|---|
| `real` | Driving an actual engine. Results are physics. |
| `stub` | An in-memory implementation standing in for one. It answers `simulate` and returns well-formed results, but they are not the engine's physics. |
| `unavailable` | The bridge speaks the contract; the thing it drives is not there — not installed, not built, not reachable. `simulate` **MUST** fail rather than return a substitute. |

`unavailable` exists because the honest answer was otherwise unsayable. A shim
whose backend is missing is not driving an engine, so it is not `real`; and it
has no in-memory implementation either, so it is not `stub`. Chrono's shim
reported `real` with no daemon built and answered `ENGINE_ERROR` to everything
— a claim the vocabulary gave it no way to correct.

An engine reporting `unavailable` **MUST NOT** return a successful `simulate`.
Reporting it and then producing numbers is the fabrication this field exists to
prevent, and the TCK checks for exactly that. Say why in the error message: the
caller usually has to install or build something, and the engine knows what.

This value is additive within v1.x. A client testing `mode == "real"` keeps
working — `unavailable` is not `real`, which is the answer it needed. Clients
that enumerate the vocabulary MUST tolerate values they do not know (§2).

**`capabilities`:**

| Field | Type | Vocabulary |
|---|---|---|
| `modes` | array of string | `batch` (one-shot `simulate`), `session` (`simulate_start`/`simulate_status`/`simulate_stop`), `teleop` (`teleop_*`) |
| `formats` | array of string | Model inputs the engine ingests: `package` (canonical sim package), `urdf`, `sdf`, `mechanism` (legacy in-band mechanism dict), `chrono_spec` (transitional, see §6) |
| `features` | array of string | Optional verbs and behaviours: `diagnose`, `screenshot`, `spawn_model`, `import_urdf`, `load_environment`, `reload`, `px4` |
| `teleop_dofs` | array of string | Teleop axes the engine honours (`vx_mps`, `vy_mps`, `vz_mps`, `yaw_rate_rps`, `body_height_m`). Core refuses a command on an axis you don't list rather than dropping it silently. Omit when the engine has no teleop |
| `fields` | object | `{"emits": [...], "accepts": [...]}` — field-snapshot quantities (§8). Empty arrays when the engine does no multi-physics coupling |

**Capability honesty is a contract requirement.** Every verb implied by an
advertised capability MUST work, and every verb *not* implied MUST answer
`UNSUPPORTED_COMMAND`. The TCK (step 5) tests both directions.

Unknown vocabulary entries are permitted and MUST be ignored by clients — that is
how a v1.x minor adds a capability without breaking older cores. Clients MUST
tolerate unknown keys in the `hello` result for the same reason.

---

## 3. Verbs

### 3.1 The required floor (frozen)

| Verb | Purpose |
|---|---|
| `hello` | Capability handshake (§2) |
| `ping` | Liveness. Result SHOULD include `{"pong": true}` |
| `simulate` | One-shot batch simulation → result schema (§7) |
| `shutdown` | Drain sessions and exit |

This floor is **frozen for the life of contract v1** (architecture doc,
Principle 10). No verb is ever added to it within v1.x. That guarantee is what
keeps a minimum viable bridge a weekend project.

`shutdown` returns a normal response *before* the process exits, drains or
cancels any live sessions, and closes listeners. Core follows it with SIGTERM
then SIGKILL if the process is still alive.

### 3.2 Capability-gated verb groups

| Capability | Verbs |
|---|---|
| `modes: session` | `simulate_start`, `simulate_status`, `simulate_stop` |
| `modes: teleop` | `teleop_start`, `teleop_command`, `teleop_state`, `teleop_stop` |
| `features: diagnose` | `diagnose` — see §3.3 |
| `features: screenshot` | `screenshot` |
| `features: spawn_model` | `spawn_model` |
| `features: import_urdf` | `import_urdf` |
| `features: load_environment` | `load_environment` |
| `features: reload` | `reload` |
| `features: px4` | `px4_start`, `px4_status`, `px4_stop` |

A batch-only engine that implements exactly the four floor verbs is a valid,
first-class engine.

### 3.3 Normalized `diagnose`

`diagnose` reports the scene an engine actually built, in **generic** terms —
vendor scene-graph vocabulary (USD prim types, Gazebo entity IDs) stays
engine-side. Core runs one urdf-vs-diagnose check against every engine.

```json
{
  "joint_counts": {"revolute": 18, "fixed": 1},
  "joint_total": 19,
  "dof_count": 18,
  "dof_names": ["hip_yaw_0", "..."],
  "joints": [
    {"name": "hip_yaw_0", "type": "revolute", "connected": true, "has_drive": true}
  ]
}
```

| Field | Meaning |
|---|---|
| `joint_counts` | Count per generic joint type: `revolute`, `prismatic`, `fixed`, `spherical`, `distance`, `other` |
| `joint_total` | Total joints the engine built |
| `dof_count` / `dof_names` | Articulation degrees of freedom, when the engine has an articulation concept. Omit rather than guess |
| `joints[]` | Per joint: `name`, generic `type`, `connected` (both bodies resolved), `has_drive` (non-zero stiffness or damping). Omit `has_drive` when the engine reports no drive data — absent is not the same as zero |
| `error` | Set when the engine could not inspect the scene at all |

Engines may include their raw scene detail alongside these keys; core ignores
it. The reference normalizer is `isaac_bridge/diagnose_normalize.py`.

### 3.4 Rate discipline

Core is never inside a physics-rate loop (Principle 5). Per-timestep exchange
through core is a rule-out, not a tuning problem: decisions needed between
timesteps ship to the engine as controller specs or policy files, or get declared
up front. Core's generic client counts messages per second per command and warns
above a sustained 100 msg/s.

---

## 4. Error taxonomy

Errors are `{"code": "...", "message": "...", "details": {...}}`. `code` is a
stable machine token; `message` is human-facing and unstable.

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | Envelope shape wrong (not an object, `cmd` missing/empty, `args` not an object) |
| `INVALID_JSON` | Line was not parseable JSON |
| `UNSUPPORTED_COMMAND` | Verb unknown to this engine, or not advertised in `hello` |
| `UNSUPPORTED_CAPABILITY` | Verb exists but the requested option within it is not supported |
| `PACKAGE_INVALID` | Sim package missing, unreadable, or schema-invalid |
| `SESSION_NOT_FOUND` | `session_id` unknown or already reaped |
| `CONTRACT_MISMATCH` | Client requested a contract major this engine does not support |
| `ENGINE_ERROR` | Failure inside the engine itself (solver divergence, GPU fault, CLI failure) |

### 4.1 Codes emitted today (conformance notes)

Step 1 of the migration aligned only the **unknown-command** path in all three
bridges. Existing per-engine codes are otherwise untouched to keep the change
additive; they are mapped here and will be renamed in a later step.

| Bridge | Emits today | Target code |
|---|---|---|
| gazebo | `UNSUPPORTED_COMMAND` (aligned) | `UNSUPPORTED_COMMAND` |
| gazebo | `GAZEBO_PROTOCOL_ERROR` (JSON parse) | `INVALID_JSON` |
| gazebo | `GAZEBO_PROTOCOL_ERROR` (`args` not an object) | `INVALID_REQUEST` |
| gazebo | `GAZEBO_SESSION_NOT_FOUND` | `SESSION_NOT_FOUND` |
| gazebo | `GAZEBO_SPAWN_FAILED`, `GAZEBO_NOT_CONNECTED`, `GAZEBO_PX4_NOT_READY`, `GAZEBO_COMMAND_ERROR`, `GAZEBO_INTERNAL_ERROR` | `ENGINE_ERROR` |
| gazebo | `INVALID_INPUT` | `INVALID_REQUEST` |
| isaac | `UNSUPPORTED_COMMAND` (aligned) | `UNSUPPORTED_COMMAND` |
| isaac | `INVALID_JSON`, `INVALID_REQUEST` | unchanged — already contract codes |
| isaac | `INVALID_ARGS` | `INVALID_REQUEST` |
| isaac | `INTERNAL_ERROR` and runtime-specific codes | `ENGINE_ERROR` |
| chrono | `UNSUPPORTED_COMMAND` (aligned) | `UNSUPPORTED_COMMAND` |
| chrono | `INVALID_REQUEST` (missing `cmd`) | unchanged |
| chrono | `ENGINE_ERROR` (simulation failure) | unchanged |

Chrono's error envelope was `{"ok": false, "error": "<string>"}` before step 1
and is now `{"ok": false, "error": {"code", "message"}}` like the others.

### 4.2 Other known gaps as of contract publication

These are recorded rather than fixed, because the fixes belong to later
migration steps:

- **`shutdown` is not implemented by the gazebo or isaac bridges.** Only chrono
  answers it. Core stops those two with SIGTERM/SIGKILL. Closing this gap is
  part of the registry/lifecycle step.
- **Core's best-effort shutdown sender uses the wrong envelope field**
  (`{"command": "shutdown"}` instead of `{"cmd": ...}`, `server/sim_engine_manager.py`),
  so no engine has ever received it. Fixed with the lifecycle work.
- **Package ingestion is engine-by-engine.** Gazebo compiles `manifest.json`
  into SDF at load time (`gazebo_bridge/package_to_sdf.py`) and advertises
  `package`. Chrono takes the canonical `mechanism` and compiles it into its
  native spec in the bridge shim. Isaac still consumes the courtesy URDF.

---

## 5. Session semantics

- Sessions are **engine-owned**. The engine mints `session_id` and owns all
  state behind it.
- Sessions **survive client disconnect**. A client may reconnect and resume
  polling `simulate_status` with the same `session_id`.
- Engines **MAY** expire idle sessions. The default is **no expiry**: an engine
  that does not declare a TTL must keep sessions until stopped explicitly or
  until `shutdown`. An engine that does expire sessions SHOULD report its idle
  TTL in `simulate_start`'s result (`idle_ttl_s`) and MUST answer
  `SESSION_NOT_FOUND` for a reaped session — never silently restart it.
- `shutdown` drains: running sessions are stopped, then the process exits.
- Session verbs are gated on `modes: session` / `modes: teleop`. An engine
  without them answers `UNSUPPORTED_COMMAND`.

---

## 6. Sim package — data at rest

A directory written by `cad.export_sim_package`, readable by any language:

```
<package_dir>/
  manifest.json          # canonical model description (schema-versioned)
  <Body>.stl             # per-body meshes, one per link
  <name>.urdf            # courtesy dialect, generated from the same data
```

`manifest.json` is validated by **`schemas/sim_package.schema.json`**. It is the
"model info API": a versioned document, not an endpoint. Engines, RL, and third
parties consume the same artifact.

**Units.** Manifest values are **SI throughout** — metres, kilograms, seconds,
radians. Meshes are the exception and declare their own native unit: FreeCAD
exports STL in millimetres, so each `links[].mesh` entry carries
`{"unit": "mm", "scale_to_m": 0.001}`. A consumer multiplies mesh vertices by
`scale_to_m`. This matches the `scale="0.001 0.001 0.001"` attribute the URDF
writer emits for the same meshes.

**Coordinate conventions.** Z-up, right-handed. Quaternions are `[w, x, y, z]`.
Joint origins follow the URDF convention: `origin_xyz_m` / `origin_rpy_rad`
express the child link frame **in the parent link frame**, at the joint's
zero configuration. Rest-pose angles are not baked into joint RPY — they belong
in an engine-side initial-joint-position config.

**Mesh vertex frame.** Each mesh declares `frame`:

| `frame` | Meaning |
|---|---|
| `link_local` | Vertices are relative to the link's own frame. The joint tree positions the link |
| `world` | Vertices are in the document's world frame. `world_pose` positions the link |

Both occur in practice: a **full** export (a mechanism was supplied) rewrites
meshes to link-local while building the kinematic tree; a **reduced** export
(bodies only) leaves them in world coordinates.

### 6.1 Two export modes

| Mode | Trigger | Contents |
|---|---|---|
| **full** | `mechanism_id` supplied | `links[]` with mass/inertia + `joints[]` + `actuators[]`/`sensors[]` when a drone config is supplied |
| **reduced** | bodies only | `links[]` with pose + mesh only; `joints[]` empty |

Both modes are schema-valid. A reduced package describes geometry and placement
but no kinematics; an engine that requires joints should answer
`PACKAGE_INVALID` rather than guess.

### 6.2 Manifest shape

```json
{
  "schema_version": "1.0.0",
  "name": "hexapod_18dof",
  "generator": "solidmind-cad 0.2.0",
  "mode": "full",
  "units": {"length": "m", "mass": "kg", "angle": "rad"},
  "links": [
    {
      "name": "chassis",
      "is_root": true,
      "mesh": {"file": "Body_Chassis.stl", "unit": "mm", "scale_to_m": 0.001,
               "frame": "link_local"},
      "world_pose": {"xyz_m": [0.0, 0.0, 0.0], "quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
      "mass_kg": 0.42,
      "inertia": {"ixx": 0.001, "ixy": 0.0, "ixz": 0.0,
                  "iyy": 0.001, "iyz": 0.0, "izz": 0.002},
      "collision": {"kind": "box", "size_m": [0.2, 0.2, 0.05]}
    }
  ],
  "joints": [
    {
      "name": "hip_yaw_0", "type": "revolute",
      "parent": "chassis", "child": "coxa_0",
      "origin_xyz_m": [0.05, 0.05, 0.0], "origin_rpy_rad": [0.0, 0.0, 0.785],
      "axis": [0.0, 0.0, 1.0],
      "limits": {"lower": -1.57, "upper": 1.57},
      "effort": 2.5, "velocity": 6.28
    }
  ],
  "actuators": [
    {"type": "rotor", "name": "rotor_0", "joint": "rotor_0_joint",
     "link": "rotor_0", "position_m": [0.15, 0.15, 0.02],
     "direction": "ccw", "max_thrust_N": 8.55, "moment_constant": 0.016}
  ],
  "sensors": [{"type": "imu", "link": "chassis"}]
}
```

Actuators are **abstract specs**, never plugin XML. `max_thrust_N` is the
physical thrust at maximum rotor speed; where it is derived from a motor
constant `k` and a maximum angular rate `ω_max` it is `k·ω_max²`. An engine
compiles these into its own vendor form (Gazebo motor plugins, PX4 airframe
parameters) on its own side of the boundary (Principle 3).

`sensors[]` records presence and mounting only: `imu`, `gps`, `barometer`,
`magnetometer`.

---

## 7. Result schema

`simulate` (and `simulate_status` on completion) returns:

```json
{
  "time_series": [{"t": 0.0, "parts": {...}, "joint_efforts": [0.0]}],
  "summary": {
    "simulation_time_s": 1.0,
    "dt_s": 0.001,
    "engine_mode": "stub",
    "peak_joint_forces": {"hip_yaw_0": 5.0}
  }
}
```

Validated by **`schemas/sim_result.schema.json`**. Required: `time_series`
(array) and `summary` with `simulation_time_s`, `dt_s`, `engine_mode`.

Two members are **pinned** because core consumes them for Tier 3.5
(`analysis.stress_from_simulation` → `server/analysis_sim_coupling.py`):

| Member | Shape | Use |
|---|---|---|
| `summary.peak_joint_forces` | object, joint id → number | Peak force/torque per joint, drives FEA boundary conditions |
| `time_series[].joint_efforts` | array of number, indexed by joint | Per-sample efforts; `joint_index` selects one |

Both are optional (a kinematics-only engine has neither) but shape-pinned when
present. `additionalProperties` is open throughout: engines may add their own
summary members freely, and core ignores what it does not know.

**File spill.** Large telemetry does not belong on the socket. An engine MAY
write it to the session or package directory and return paths instead:

```json
{"time_series": [], "summary": {...},
 "files": [{"role": "time_series", "path": "/abs/path/telemetry.jsonl",
            "format": "jsonl", "rows": 250000}]}
```

`role` is free-form; `path` is absolute or relative to the package directory.
When a run spills, `time_series` MAY be empty or downsampled — the `files` entry
is authoritative.

---

## 8. Field snapshots — the multi-physics IR

Field data (temperature, pressure, displacement, …) is exchanged **on disk**, not
on the socket: a mesh file plus a JSON sidecar validated by
**`schemas/field_snapshot.schema.json`**.

```json
{
  "schema_version": "1.0.0",
  "quantity": "temperature",
  "units": "K",
  "mesh_ref": "rotor_hub.vtu",
  "time_s": 0.25,
  "data_file": "rotor_hub_temperature_t0.25.vtu"
}
```

Fixed quantity vocabulary: `temperature`, `heat_flux`, `pressure`,
`displacement`, `force`, `stress_von_mises`. `time_s` is `null` for
steady-state results. Payloads are VTU (via `meshio`, already a core dependency).

Engines and solvers declare what they exchange in `hello`'s
`capabilities.fields.emits` / `.accepts`; core type-checks coupling chains at
plan time and owns the single mesh-to-mesh mapping utility. Engines never couple
peer-to-peer (Principle 4).

**No producer exists yet.** As of contract v1.0.0 every bridge reports
`fields: {"emits": [], "accepts": []}`. The format is published now so that
solver and engine authors can target it; the first producer arrives with the
coupling work.

---

## 9. Lifecycle & discovery

Engines are discovered through **descriptor files**, never imports. Core ships
defaults for its three engines as data; third parties add their own the same way
and register with nobody:

```toml
# ~/.solidmind/engines.d/isaac.toml
name = "isaac"
launch = ["${ISAAC_PYTHON}", "-m", "isaac_bridge.bridge_server", "--headless"]
cwd = "~/repos/solidmind-engine-isaac"
port = 9878
install_hint = "git clone …engine-isaac && $ISAAC_PYTHON -m pip install -e ."
when_to_use = "Legged robots, articulated arms; GPU contact physics."
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Engine slug; must equal `hello`'s `engine` |
| `port` | yes | TCP port |
| `host` | no | Defaults to `127.0.0.1` (`SOLIDMIND_SIM_HOST` overrides) |
| `launch` | no | Argv to spawn. Omit for attach-only (user-run or remote engines) |
| `cwd` | no | Working directory for `launch` |
| `install_hint` | no | Shown when the engine is absent |
| `when_to_use` | no | Guidance text that flows into generated LLM prompts (Principle 8) |

- **Readiness** = TCP accept + a successful `hello`.
- **Health** = `ping`.
- **Stop** = `shutdown` → SIGTERM → SIGKILL.
- Core owns only the processes it spawns.

**Implemented.** Core reads descriptors from `engines.d/` (its own defaults)
and `~/.solidmind/engines.d/` — or `$SOLIDMIND_ENGINES_D` — with user files
overriding core's by name. Everything core says about an engine comes from
there: the backend enum in the MCP tool schemas, the model-facing selection
guidance, install hints, ports and launch commands. A broken descriptor is
logged and skipped rather than taking the tool surface down.

Two conveniences beyond the fields above:

| Field | Meaning |
|---|---|
| `default` | Marks the engine chosen when a caller names none (`SOLIDMIND_DEFAULT_ENGINE` overrides) |
| `[variants.<name>]` | An alternate `launch` for the same engine — Gazebo's `real` runtime, Isaac with a GUI — so core needs no per-engine launch branch |

`${PORT}` in a launch command expands to the port core allocated; `${VAR}` and
`${VAR:-default}` read the environment. `${PYTHON}` is core's interpreter.

---

## 10. Versioning & stability

One umbrella semver covers the protocol, the package format, the result schema,
and the field-snapshot format. `hello` negotiates: `contract_versions_supported`
lists the **majors** an engine implements; `protocol_version` is the exact
version it speaks.

**Within v1.x (the promise):**

- Changes are **additive only**. New optional fields, new capability vocabulary,
  new verbs behind new capabilities.
- The **required verb floor never grows** — `hello`, `ping`, `simulate`,
  `shutdown`, forever.
- Required fields are never removed or retyped; enum members are never removed.
- Deprecations get **one minor version of warning** before a major.
- The TCK never breaks: an engine that passes v1.0 passes v1.x.

**Clients** MUST ignore unknown fields and unknown capability vocabulary.
**Engines** MUST ignore unknown `args` members. Both rules are what make additive
change safe.

A client that needs a contract major an engine does not list in
`contract_versions_supported` MUST fail with `CONTRACT_MISMATCH` rather than
proceed hopefully.

---

## 11. Conformance

The TCK runs standalone against any `host:port` and is the support boundary —
engine bug reports start with its output:

```bash
python3 -m tck --host 127.0.0.1 --port 9880     # 0 = conformant, 1 = not
```

It imports nothing from core, so an engine repository can vendor `tck/` and run
it with a stock Python. `tck/README.md` is the engine author's guide; the
reference engine (`reference_engine/`) is a complete worked example that passes
every tier with no install. Tiers:

1. **Protocol** — handshake, framing, error taxonomy, capability honesty.
2. **Package** — ingest the golden fixture package.
3. **Results** — schema validation.
4. **Sessions/teleop** — only if advertised.
5. **Physics sanity** — golden scenarios with analytic solutions (pendulum
   period, gear-ratio propagation, falling-box settle) within tolerance.
6. **Latency report** — informational RTT distribution.

Current in-repo conformance, verified by running the TCK against each engine:

| Engine | TCK verdict | Package ingest | Sessions / teleop | Physics tier |
|---|---|---|---|---|
| reference | conformant | ✅ `package` | ✅ both | ✅ all three scenarios |
| gazebo (stub) | conformant | ✅ `package` → SDF | teleop only | skipped (`runtime_mode: stub`) |
| isaac (no Isaac Sim installed) | conformant | ❌ `urdf` only | ✅ both | skipped (`runtime_mode: stub`) |
| chrono (bridge shim + daemon) | conformant | ❌ `mechanism` → spec | ❌ batch only | ✅ gear ratio |

`shutdown` remains unimplemented on the gazebo and isaac bridges (§4.2); the
TCK does not send it, since it would end the engine mid-run.
