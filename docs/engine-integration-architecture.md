# Engine Integration Architecture

**Status:** design approved — pre-implementation
**Date:** 2026-08-03
**Scope:** simulation engines (Chrono, Gazebo, Isaac), RL training, and the contract binding them to core

This document is the source of truth for the engine-extraction change. Implementation
prompts derive from §7 (Migration); everything before §7 is the reasoning those prompts
assume. When an implementation detail conflicts with a principle in §2, the principle wins
or this document gets amended first.

---

## 0. The change in one paragraph

Core stops containing, importing, or speaking the dialects of simulation engines. Engines
become **separately-cloned applications** in sibling repositories, each wrapping its bridge
around one **published, versioned contract** owned by core: a sim package on disk (data at
rest), an NDJSON/TCP protocol with a capability handshake (data in motion), a pinned result
schema, and a field-snapshot format for multi-physics. Core keeps the contract, the
orchestration, one generic client, a reference engine, and a conformance suite (TCK).
Anyone — including strangers — can add an engine by implementing four verbs and two file
formats in any language, passing the TCK, and dropping a descriptor file. Nobody asks
permission, and no engine ever knows another engine exists.

---

## 1. Why: the problem, with evidence

All figures verified against this branch on 2026-08-03.

### 1.1 Footprint

Engine-specific code is ~34k lines, >20% of the repo, shipped to every user:

| Location | Lines | Content |
|---|---|---|
| `isaac_bridge/` | 6,286 | Isaac Sim bridge, controllers, IK, teleop |
| `rl_training/` | 3,754 | Isaac Lab + RSL-RL pipeline |
| `gazebo_bridge/` | 1,237 | Gazebo bridge, PX4 integration |
| `chrono_daemon/` | 999 | C++ MBS daemon |
| `server/` engine-coupled modules | ~8,800 | adapters, 3 clients, engine manager, PX4, mavlink, sim_export, sim_verify, spec builder |
| `tests/` engine tests | ~15,000 | ~35 modules |

### 1.2 The growth is in core, not the bridges

File-touches over the last 6 months: `server/` 473 + `tests/` 578, versus `isaac_bridge/`
53, `rl_training/` 42, `gazebo_bridge/` 20, `chrono_daemon/` 18. The bridges are quiet
appliances. What grows is core's **outbound translators** (SDF writer + motor/sensor
plugins at `sim_export.py:1399`, `px4_airframe_generator.py`, `mavlink_controller.py`,
`simulation_spec_builder.py`, `verify_urdf_vs_isaac` at `sim_verify.py:357`) and
**vocabulary** (backend tables at `tools_motion.py:50`, tool-description text in
`main.py`, guidance at `prompts.py:364`, vendor-named tools `motion.isaac_launch/stop/
screenshot`, Isaac-only `rl.*`). Moving bridges to new repos fixes neither; inverting
translation does.

### 1.3 Structural facts that force the design

- **Interpreter sovereignty.** Zero of the three engine runtimes execute in core's venv
  today: Isaac runs under `ISAAC_PYTHON` (its own bundled interpreter), Gazebo real mode
  under system Python with `gz` bindings, Chrono is a C++ binary (`chrono_daemon/run.sh`
  sets `LD_LIBRARY_PATH`). An import-based plugin API is impossible for 2 of 3 engines.
- **The protocol already exists, unversioned.** All three engines *and* the FreeCAD addon
  speak the same envelope — `{"cmd","args"}` → `{"ok","result"|"error"}`, newline-delimited
  JSON over TCP. No version field, no handshake, anywhere.
- **The manifest never lands on disk.** `export_sim_package`
  (`freecad_addon/commands.py:2644`) builds the body manifest in memory and returns it
  over MCP. Only meshes + URDF/SDF are written. A non-Python engine has nothing to read.
- **Bidirectional imports** block any split: `isaac_bridge/lifecycle.py:20` →
  `server.isaac_client`; `gazebo_bridge/runtime_gazebo.py:196` →
  `server.mavlink_controller`; `rl_training/env_configurator.py:13` →
  `server.urdf_analyzer`. *(All three severed in step 2; a static guard in
  `tests/test_import_boundaries.py` keeps them severed.)*
- **Three near-duplicate clients** (`isaac_client.py`, `gazebo_client.py`,
  `chrono_client.py`, ~420 lines each) prove the protocol is uniform and collapse into one
  generic client.
- **CI never runs a real engine** (`ci.yml` skips heavy deps; `requires_*_real` markers
  are local-only). The "Gazebo stub" (`python3 -m gazebo_bridge.bridge_server --runtime
  stub`) is the de facto reference engine wearing a vendor's name.
- **Tier 3.5 is a schema dependency**: `analysis_sim_coupling.py` reads
  `summary.peak_joint_forces` and `time_series[].joint_efforts` from simulate results.
- **Measured protocol budget** (stub bridge, localhost): persistent ping p50 = 33 µs
  (~30k msg/s ceiling); +32 KB payload p50 = 73 µs; new-connection-per-message p50 =
  259 µs. Fastest real interaction (teleop) is ≤10 Hz → 3,000× headroom.
- **Synchrony audit**: engines never dial core (Chrono is accept-only; Gazebo's only
  outbound connection is to PX4). Every per-timestep loop already runs engine-side
  (gait/residual controllers in the bridges, mavlink setpoint stream in the bridge
  process, RL stepping inside Isaac). PX4 teleop uses hold-last-setpoint semantics.

---

## 2. Philosophy: ten principles

1. **Engines are applications, not libraries.** Libraries get import contracts
   (`FieldSolver`, entry points — correct for solvers). Applications get process
   contracts: launch/attach, wire protocol, files. FreeCAD is already integrated this way;
   the engines get the FreeCAD treatment. This is the root correction the whole change
   flows from.

2. **The contract is data, not code.** Spec document + JSON Schemas + descriptor files +
   a conformance suite. No shared `solidmind-contract` pip package — a shared library
   would re-couple environments and exclude non-Python engines. Chrono (C++) is the
   standing proof the contract must be implementable from the spec alone. Trivial helpers
   (~60 lines of NDJSON framing) are duplicated per bridge, deliberately.

3. **Core emits canonical; engines translate inbound.** Core writes one neutral package;
   each engine compiles it to its native form (SDF, MJCF, Chrono spec) on its own side of
   the boundary. Rule of thumb: if an artifact contains a vendor plugin namespace or a
   vendor parameter file, core does not emit it. URDF is the one declared courtesy
   dialect (consumed by 2+ engines and most third parties), generated from the same
   canonical data.

4. **Star topology: engines conform to the contract, never to each other.** Composition
   is core's job, expressed as data (a coupling spec + orchestration), not integration
   code in engines. N engines = N conformances, not N² bridges. Elmer emits a temperature
   field without knowing CalculiX exists; CalculiX accepts one without knowing Elmer
   exists.

5. **Core is never inside a physics-rate loop.** One protocol round trip costs ~3% of a
   1 kHz timestep before serialization — per-step exchange through core is a rule-out,
   not a tuning problem. Decisions needed between timesteps ship to the engine
   (controller specs, policy files), get declared up front (profiles, schedules), or —
   last resort — become a v2 streaming capability. Core may orchestrate loops at *batch*
   cadence (convergence iterations, sweeps); never at physics cadence.

6. **Files are the blackboard; sockets carry intent; no brokers.** Model data and bulky
   results live on disk (language-agnostic, inspectable, survives restarts — and ideal
   for an LLM copilot that screens artifacts between steps). Commands and small results
   ride the socket with latest-value semantics where control is involved. A broker adds
   latency, queue semantics that are wrong for control, and a shared stateful service —
   to solve fan-out/buffering problems this traffic does not have. Heuristic: *who must
   receive this message without the sender knowing their address?* One known process →
   socket. Many unknown → pub/sub. Pull-workers → queue. Today every answer is "one,
   known."

7. **Capabilities are negotiated, not assumed.** `hello` tells core what an engine can do;
   backend enums, install hints, tool descriptions, and prompt guidance are *generated*
   from the registry + handshake, never hardcoded. Partial implementations are
   first-class: a batch-only engine with no teleop is a valid engine.

8. **Extending the system includes extending the LLM's awareness.** The copilot chooses
   backends, so a third-party engine must become *recommendable*, not just callable:
   descriptors carry `when_to_use` guidance that flows into generated prompts, and engine
   repos may ship knowledge packs. Registration without model-awareness is half an
   integration here.

9. **The N+1 rule is the standing acceptance test.** Adding the (N+1)th engine changes
   zero lines in core and zero lines in any other engine. If it ever doesn't, stop and
   fix the contract, not the engine.

10. **Stability is a kept promise.** Within contract v1.x: additive-only changes,
    deprecations get one minor version of warning, the TCK never breaks. The handshake
    enforces the mechanics; the maintainer enforces the discipline. Third parties are
    burned by churn exactly once.

---

## 3. The Engine Integration Contract v1

Published as `docs/engine-contract.md` + `schemas/*.schema.json`. Umbrella semver; the
handshake negotiates.

### 3.1 Sim Package — data at rest

A directory written by `cad.export_sim_package`:

```
<package_dir>/
  manifest.json          # canonical model description (schema-versioned)
  meshes/*.stl           # per-body meshes
  <name>.urdf            # courtesy dialect, generated from the same data
```

`manifest.json` (schema: `schemas/sim_package.schema.json`):

- `schema_version`, `name`, `units` block (explicit; recorded decision: manifest values
  SI — meters, kilograms, seconds, radians; each mesh entry declares its native unit and
  scale factor, since FreeCAD exports mm)
- `links[]`: name, mass_kg, inertia, mesh ref, collision shape, world pose
- `joints[]`: type, parent, child, origin (xyz m / rpy rad), axis, limits, effort, velocity
- `actuators[]`: **abstract** specs — rotor `{position, direction, max_thrust_N,
  moment_constant}`, servo `{joint, stiffness, damping}` — never plugin XML
- `sensors[]`: presence + mounting (imu, gps, barometer, magnetometer)
- coordinate conventions stated explicitly (Z-up, link-local origin definition)

This manifest is the "model info API": a versioned document, not an endpoint. Engines,
RL, and third parties all consume the same artifact.

### 3.2 Wire protocol — data in motion

NDJSON over TCP. Envelope: `{"cmd","args","request_id?"}` →
`{"ok",("result"|"error"),"request_id?"}`.

- **`hello`** (new, required): returns `protocol_version`, `engine`, `engine_version`,
  `contract_versions_supported`, `capabilities` — `modes: [batch|session|teleop]`,
  `formats: [package|urdf|sdf|...]`, `features: [diagnose|screenshot|...]`,
  `fields: {emits: [...], accepts: [...]}`.
- **Required verbs**: `hello`, `ping`, `simulate`, `shutdown`. This floor is frozen
  (Principle 10); everything else is capability-gated: session verbs
  (`simulate_start/status/stop`), teleop verbs, `diagnose`, `screenshot`,
  `load_environment`.
- **Error taxonomy** (shared codes): `INVALID_REQUEST`, `INVALID_JSON`,
  `UNSUPPORTED_COMMAND`, `UNSUPPORTED_CAPABILITY`, `PACKAGE_INVALID`,
  `SESSION_NOT_FOUND`, `CONTRACT_MISMATCH`, `ENGINE_ERROR`.
- **Session semantics**: sessions are engine-owned, survive client disconnects, may be
  expired after an engine-declared idle TTL; `shutdown` drains them.
- **`diagnose` is normalized**: generic joint-type counts and DOF — vendor scene-graph
  vocabulary (e.g. `PhysicsRevoluteJoint`) stays engine-side. Core keeps *one* generic
  urdf-vs-diagnose verification, replacing `verify_urdf_vs_isaac`.

### 3.3 Result schema

`{time_series[], summary{}}` with `summary.simulation_time_s`, `dt_s`, `engine_mode`,
and — pinned for Tier 3.5 — `summary.peak_joint_forces` and `time_series[].joint_efforts`.
Large telemetry may spill to files in the session/package directory with paths returned.
Schema: `schemas/sim_result.schema.json`.

### 3.4 Field snapshots — the multi-physics IR

Standardized on-disk field data: VTU (via `meshio`, already a dep) + JSON sidecar
`{quantity, units, mesh_ref, time_s, schema_version}`. Fixed quantity vocabulary
(`temperature`, `heat_flux`, `pressure`, `displacement`, `force`,
`stress_von_mises`, ...). Engines/solvers declare `fields.emits` / `fields.accepts`;
core type-checks coupling chains at plan time and owns the one mesh-to-mesh mapping
utility (nearest-neighbor first, RBF later).

### 3.5 Lifecycle & discovery

Descriptors, not imports. Core ships defaults for the three engines as **data**; users
add third-party engines the same way:

```toml
# ~/.solidmind/engines.d/isaac.toml
name = "isaac"
launch = ["${ISAAC_PYTHON}", "-m", "isaac_bridge.bridge_server", "--headless"]
cwd = "~/repos/solidmind-engine-isaac"
port = 9878
install_hint = "git clone …engine-isaac && $ISAAC_PYTHON -m pip install -e ."
when_to_use = "Legged robots, articulated arms; GPU contact physics."
```

Readiness = TCP accept + `hello`. Health = `ping`. Stop = `shutdown` → SIGTERM → SIGKILL.
Core owns processes it spawns; attach-only (`host:port`, no `launch`) supports user-run
daemons and remote engines (`SOLIDMIND_SIM_HOST`). Repos live as **siblings** (the
existing `../isaacsim`, `../IsaacLab` convention) — never nested clones, never submodules
(submodules pin SHAs in core = lockstep coupling reintroduced).

### 3.6 Conformance — the TCK

Runnable standalone against any `host:port`; distilled from `test_sim_cross_backend.py`.
Tiers:

1. **Protocol**: handshake, framing, error taxonomy, capability honesty (advertised verbs
   work; unadvertised return `UNSUPPORTED_COMMAND`).
2. **Package**: ingest the golden fixture package.
3. **Results**: schema validation.
4. **Sessions/teleop**: only if advertised.
5. **Physics sanity**: golden scenarios with analytic solutions (pendulum period, gear
   ratio propagation, falling box settle) within tolerance — the tier that separates
   "speaks the protocol" from "simulates correctly."
6. **Latency report** (informational): RTT distribution, from
   `scripts/bench_protocol_rtt.py`.

The TCK is the support boundary: engine bug reports start with "attach your TCK output."

### 3.7 Instrumentation tripwires

The generic client counts messages/sec per command and records an RTT histogram; sustained
>100 msg/s on any command logs a warning — it only fires if someone builds a per-timestep
interaction (Principle 5), converting the tight-loop audit from a one-time review into a
permanent property check.

---

## 4. Topology after the change

```
solidmind-cad  (core — the hub)
├─ contract: docs/engine-contract.md + schemas/ + TCK + reference engine
├─ orchestration: motion.* / sim.* / rl.* / analysis.* tool façades,
│   coupling chains + field mapper, registry, engine manager, ONE generic client
├─ model pipeline: build_sim_model, package exporter, URDF writer + validators
└─ engines.d/ default descriptors (data)

siblings (applications; own interpreter, own cadence, own README, own CI):
├─ solidmind-engine-isaac    ISAAC_PYTHON   bridge, controllers, IK, teleop
├─ solidmind-engine-gazebo   system python  bridge + package→SDF compiler (motor/sensor
│                                           plugins) + PX4 init scripts + mavlink
├─ solidmind-engine-chrono   C++ (+ shim)   daemon + canonical→Chrono-spec bridge shim
└─ solidmind-rl              ISAAC_PYTHON   rl_training + env_configurator + urdf_analyzer
```

**Core keeps forever:** mechanism model & stores, exporter + manifest, URDF courtesy
writer + `validate_urdf`/`validate_urdf_fk`, generic client, engine manager
(spawn/attach/health), registry + descriptors, reference engine, TCK, schemas, tool
façades, coupling orchestrator + field mapping, drone *sizing* (`server/airframes/` — it
populates manifest actuators; PX4 *artifact emission* goes to engine-gazebo).

**Venv story:** there isn't one to solve. No repo imports another; each engine repo needs
itself importable only to *its own* interpreter (editable install — the existing IsaacLab
workflow). Only sockets, files, and launch commands cross repo boundaries.

**Deleted from core:** `_SIM_BACKENDS`/`_TELEOP_BACKENDS`/`_DEFAULT_PORTS` tables and
backend if/elif chains (→ registry + handshake); hardcoded install hints (→ descriptors);
`motion.isaac_launch`/`isaac_stop` (fold into `sim.*`); `motion.isaac_screenshot` (→
capability-gated `motion.screenshot`); three per-engine clients (→ one generic);
`write_sdf` + plugin/sensor emission, `px4_airframe_generator`, `mavlink_controller`
(→ engine-gazebo); `simulation_spec_builder` (→ engine-chrono shim);
`verify_urdf_vs_isaac` (→ normalized `diagnose` + one generic check). Backend guidance in
`prompts.py`/tool text becomes generated. Uninstalled engines' tools vanish from the LLM
tool schema — smaller prompt surface for free.

---

## 5. Multi-physics composition

| Tier | Pattern | Cadence | Orchestrator |
|---|---|---|---|
| **A — Chained** | solver A completes → fields become B's BCs | once per solve | core (exists today at scalar level: Tier 3.5, `coupled_check`) |
| **B — Iterated weak** | A ⇄ B to convergence | sec–min per iteration | core — batch cadence is allowed |
| **C — Tight transient** | exchange every timestep | physics rate | **never core** — one native multiphysics solver (Elmer does thermal+elastic+EM monolithically) or a composite engine |

A **composite engine** wraps a coupled group (e.g. preCICE + CalculiX + SU2 — both have
upstream preCICE adapters) behind one contract face: `hello` advertises
`coupled: [...]`; core launches it like any engine. Engines never couple peer-to-peer
(Principle 4). Timescale separation is why chaining covers nearly all engineering cases:
even a railgun decomposes into one small tightly-coupled lumped ODE model
(circuit + motion with back-EMF) feeding Tier-A field chains (heat deposition → transient
thermal → thermal stress + magnetic pressure), with Tier B only if rail-deflection
feedback ever matters. Every handoff is an inspectable artifact — the copilot can screen
each link, aligned with the ROADMAP's screens-before-sims ethos.

---

## 6. The third-party engine story

The point of the whole design: *a stranger adds their engine without ever talking to us.*

- **Before**: fork core, touch 6+ files (`tools_motion`, `sim_engine_manager`, `main.py`,
  `prompts.py`, …), Python only, wait for review.
- **After**: implement 4 verbs + read 2 file formats (any language) → run the TCK →
  write a descriptor TOML → done. For a URDF-capable engine (MuJoCo, Drake, Genesis),
  the minimum viable bridge is a few hundred lines — a weekend project. That threshold is
  load-bearing: the required surface is frozen at v1's four verbs (Principle 10).

| A third party needs… | Provided by |
|---|---|
| what to build | contract doc + schemas |
| a working example | the reference engine (clone it, swap the physics) |
| proof they built it right | TCK, per-capability pass/fail + physics-sanity tier |
| to build only a subset | capabilities — batch-only engines are first-class |
| to plug in | descriptor file; no core registration |
| to be chosen by the LLM | `when_to_use` guidance + optional knowledge pack |

Discoverability starts as a curated `docs/engines.md` list; a registry is future work if
ever needed.

---

## 7. The migration: one large change, ordered

Ordering is **no-regret**: stopping after any step leaves the repo strictly better.
Estimated 2–3 weeks focused. Each step below becomes one implementation prompt.

1. **Contract + handshake.** Write `docs/engine-contract.md`;
   `schemas/sim_package.schema.json` + `sim_result.schema.json` +
   `field_snapshot.schema.json`; add `hello` (+ `request_id` passthrough) to all three
   bridges and the stub; make `export_sim_package` write `manifest.json` to disk.
   *Done when:* schemas validate real exports; all bridges answer `hello`. **— done.**
2. **Sever the three reverse imports.** `isaac_bridge/lifecycle.py` (client dep — moves
   with the bridge or gets its own), `gazebo_bridge/runtime_gazebo.py` (satisfied
   in-package once mavlink moves), `rl_training/env_configurator.py` (urdf_analyzer moves
   to RL). Add "no `server.*` imports" guards.
   *Done when:* bridges import nothing from core. **— done:** the isaac bridge got its
   own `isaac_bridge/client.py`; `mavlink_controller` moved to `gazebo_bridge/` and
   `urdf_analyzer` to `rl_training/` (ahead of step 3's wider mavlink move); the `rl.*`
   tools import the RL pipeline lazily.
3. **Dialect inversion.** SDF + motor/sensor plugins + PX4 artifacts + mavlink →
   gazebo-side package→SDF compilation at load time (manifest actuators/sensors carry the
   abstract specs — the biggest behavioral migration; drone e2e tests gate it).
   `simulation_spec_builder` → chrono bridge shim. `verify_urdf_vs_isaac` → normalized
   `diagnose`. *Done when:* core emits only manifest + meshes + URDF. **— done:**
   `gazebo_bridge/package_to_sdf.py` + `gazebo_bridge/px4_airframe.py` compile SDF and
   PX4 params from the manifest at load time; `chrono_bridge/` is a contract server in
   front of the C++ daemon; `verify_urdf_vs_diagnose` reads the normalized report.
   Not verified: the drone SITL e2e gate needs Gazebo + PX4, neither installed here.
4. **Registry-driven vocabulary.** `engines.d/` descriptors + registry; delete backend
   tables/if-elifs; generate tool text + prompts from registry + handshake; fold
   vendor-named tools; collapse three clients into one generic client with the msg-rate
   tripwire. *Done when:* `grep -r "isaac\|gazebo\|chrono" server/` finds no
   control-flow references (data/descriptors only). **— done:** `server/engine_registry.py`
   + `engines.d/*.toml`; one `server/engine_client.py` (with the tripwire) and one
   `server/sim_adapter.py` replace three clients and two adapters; `motion.isaac_*`
   folded into `sim.*` and a capability-gated `motion.screenshot`; session-vs-batch
   simulation is chosen from `hello`. Remaining name references in `server/` are the
   chrono *study solver* and `rl_training`'s `ISAAC_PYTHON` launch, both scheduled for
   later steps.
5. **Reference engine + TCK.** Promote the stub to a named in-core reference engine;
   extract the TCK (protocol/package/results/sessions/physics-sanity tiers) from
   `test_sim_cross_backend.py` + golden fixtures; core CI = reference engine only.
   *Done when:* TCK passes against reference + all three real bridges locally.
   **— done:** `reference_engine/` is its own engine (not the Gazebo stub renamed)
   with analytic gear/pendulum/free-fall physics; `tck/` runs standalone against any
   `host:port` with six tiers and a golden package; a CI job runs it against the
   reference engine with zero engines installed. All four engines verified conformant
   locally — the run found and fixed three real gaps (gazebo and isaac error
   taxonomy, isaac's missing `summary.dt_s`).
6. **The split.** `git subtree split` each of `isaac_bridge/`, `gazebo_bridge/`,
   `chrono_daemon/`, `rl_training/` into sibling repos with history; per-repo CI (TCK +
   own tests + import guard); default descriptors in core; engine tests leave core.
   **— done, minus publication:** `scripts/split_engines.sh` builds
   `solidmind-engine-{isaac,gazebo,chrono}` and `solidmind-rl` with per-package history,
   scaffolding, vendored TCK and import guards; `scripts/verify_engine_repos.sh` proves
   each stands alone (own tests green, TCK conformant, no `server.*` on the path).
   Core still holds its copies: `scripts/remove_engines_from_core.sh` is the deliberate
   last step, to be run only once the siblings are pushed. Tests that reach into core
   land in each repo's `tests/needs_porting/` — enumerated work, not lost coverage.
7. **Verification.** Wheel contains no engine code (maturin contents — currently
   unverified); hexapod pipeline e2e (export → Isaac → RL config) across the split
   layout; `rl.*` tools drive solidmind-rl via the CLI parity path
   (`docs/simulation-and-rl.md`). **— done:** all nine §9 criteria checked in
   `docs/engine-extraction-verification.md`, with the mechanizable ones as tests
   (`tests/test_split_acceptance.py`, `tests/test_pipeline_e2e.py`). `rl.*` now shells
   out to `rl_training.cli`, so core imports no engine package at all; maturin gained
   the explicit include/exclude that keeps engines out of the wheel. Left open there:
   publication of the sibling repos, the built-wheel inspection that follows removal,
   and the Isaac/Gazebo real-runtime legs.

**Known long poles:** step 3's SDF move from export-time to load-time (drone/PX4 flow is
the most entangled consumer), and chrono's canonical ingest (recorded decision: Python
bridge shim first; C++ ingest later if ever needed).

---

## 8. Alternatives considered and rejected

| Alternative | Why rejected |
|---|---|
| Status quo monorepo | All growth vectors continue; wheel ships engines to everyone; vendor names in the LLM tool surface; CI doesn't exercise real engines anyway |
| Modular monolith as end-state | ~80% of the value and the natural intermediate state, but delivers neither repo relief, clone-able engine repos, nor the third-party story |
| Pip packs per engine | pip delivers into core's venv, where no engine runs; meaningless for C++; degenerates into this design with extra ceremony. Entry points remain for pure-Python tool/knowledge packs |
| Broker (Redis/RabbitMQ) | Adds a hop + a shared stateful service; queue semantics wrong for control (stale setpoints); no fan-out/buffering/work-queue need exists; robotics transports (DDS, ZeroMQ, MAVLink) are brokerless for the same reasons |
| Pairwise engine integration / per-pair adapter layers | N² integrations; the star + field vocabulary makes composition data, not code |
| One monolithic multiphysics platform | Unnecessary (timescale separation; §5) and a black box that defeats the inspect-every-artifact copilot ethos |
| Submodules / nested clones | SHA-pinning = lockstep coupling reintroduced; tooling confusion |

---

## 9. Acceptance criteria

1. **N+1 test** (thought experiment, documented): a new engine requires zero core edits.
2. Core CI green with **zero engines installed** (reference engine only).
3. TCK passes for reference + isaac + gazebo + chrono locally.
4. Hexapod e2e (export → Isaac import → RL config) works across the split layout.
5. Engine repos import nothing from `server.*` (CI-enforced).
6. Wheel ships no engine code.
7. No vendor names in core control flow or the static MCP tool list; backend guidance is
   generated; uninstalled engines contribute zero tools.
8. Tier 3.5 (`stress_from_simulation`) works unchanged against contract results.
9. Msg-rate tripwire live in the generic client.

---

## 10. Open questions & recorded decisions

**Recorded decisions**
- Manifest units: SI throughout; per-mesh native-unit + scale declaration (FreeCAD
  exports mm).
- Chrono ingest: Python bridge shim in engine-chrono (symmetry: every engine repo ships a
  bridge — contract on the front, native on the back).
- Drone split: sizing stays core (`server/airframes/` feeds manifest actuators); SDF
  plugins + PX4 init scripts + mavlink go to engine-gazebo.
- URDF stays a core courtesy artifact; SDF/MJCF/USD are engine-side.
- `rl.*` MCP tools stay in core, driving solidmind-rl as a subprocess/CLI.
- Contract versioning: single umbrella semver, additive-only within v1.x.

**Open**
- Wheel contents under maturin (no explicit package list in `pyproject.toml`) — verify in
  step 7; affects nothing before then.
- Exact idle-TTL/session-expiry defaults — finalize while writing the contract doc.
- Repo naming (`solidmind-engine-*` vs `solidmind-*-engine`) — bikeshed at split time.
- Gazebo real-mode interpreter resolution in its descriptor (system `python3` vs
  configured path) — finalize in engine-gazebo's README at split time.
