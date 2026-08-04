# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **The quadrotor example reported a successful flight for a drone that never
  left the ground.** It sent `MAV_CMD_NAV_TAKEOFF`, which PX4 v1.17 acks and
  then ignores — the vehicle sat armed until PX4 gave up with "Disarmed by
  preflight inaction". `MavlinkController.takeoff_via_mode` exists for exactly
  this and documents it; the example was not using it. The altitude wait then
  fell through silently on timeout, so `"Landed."` and `"✓ Flight pipeline
  complete"` both printed against a stationary vehicle. Altitude is now
  measured, and failing to reach it raises instead of passing.
- **Engines no longer outlive the server that started them.**
  `sim_engine_manager.shutdown_all` has always been written and tested, and its
  docstring has always said "Call at server exit" — but nothing called it. Every
  engine survived the MCP server; three Isaac bridges were found orphaned for 18
  hours holding 16 GB between them. It compounds, because engine handles live
  only in memory: a restarted server cannot reach the previous instance's
  engines, so it pings them, sees them answering, and reports `already_running`,
  quietly binding a fresh session to a stale simulation. Now wired to `atexit`
  and to SIGTERM/SIGINT, which still exit with the signal's status.
- **The quadrotor example no longer leaks its Gazebo world.** PX4 starts the
  server from a transient shell that exits, so it is reparented to init in a
  different process group and no signal aimed at PX4 can reach it. The next run
  then inherits that world silently — `px4-rc.gzsim` attaches to the first
  `/world/*/clock` it finds. That is not benign: PX4 sources `gz_env.sh` only
  when starting a *new* server, and its `default.sdf` has no `<plugin>` tags at
  all, so an inherited world can have no IMU, GPS or barometer while PX4
  attaches, spawns the model, and never receives a sample. The example now reaps
  any pre-existing world before launching and stops Gazebo on the way out.
- **Docs: `ss -ulnp | grep 14540` never showed what it claimed.** PX4 binds
  14580 and *sends to* 14540, so nothing is listening there until a ground
  station binds it.

### Added
- `examples/quadrotor_camera_drone/sim_processes.py` — PX4/Gazebo process
  reaper, shared by `run.py` and `flight_lab.py` rather than implemented twice.
  It reads `ps` and drops its own PID instead of using `pgrep -f`, which matches
  the reaper's own command line and has already caused a false "already
  listening" reading and a kill that hit nothing.
- Docs: how to read an arming denial out of the ulog instead of guessing, and
  why force-arm cannot bypass preflight over MAVLink.

## [0.4.0] — 2026-08-04

One contract addition, and the tooling that goes with it. Additive within
contract v1.x — nothing an existing client or engine has to change.

### Added
- **`runtime_mode` gains a third value: `unavailable`** (contract §2, additive
  within v1.x). It says the bridge speaks the contract but the thing it drives
  is not there — not installed, not built, not reachable. The honest answer was
  previously unsayable: a shim with no backend is not driving an engine, so not
  `real`, and has no in-memory implementation either, so not `stub`. Chrono's
  shim reported `real` with no daemon built while answering `ENGINE_ERROR` to
  everything.
  - An engine reporting it **MUST NOT** return a successful `simulate`, and the
    TCK checks exactly that — claiming `unavailable` and then producing numbers
    is the fabrication the field exists to prevent.
  - A daemonless Chrono is now **conformant**: its bridge is correct, it says
    plainly what is missing, and it refuses rather than substituting. It used
    to be non-conformant with a failure about gear ratios, which had nothing to
    do with the actual problem.
  - Clients testing `mode == "real"` are unaffected — `unavailable` is not
    `real`, which is the answer they needed.

### Changed
- **`scripts/verify_engine_repos.sh` looks where the descriptors point.** It
  resolved the engine repositories as core's siblings — where a fresh
  `split_engines.sh` drops them — while `engines.d/*.toml` names `~/repos`,
  which is what `sim.start_engine` actually launches. With a single checkout in
  the place the descriptors name, the script reported every repository missing.
  It now prefers the siblings when they are there, falls back to `~/repos`, and
  prints which it chose. `--dest` still overrides both.

## [0.3.1] — 2026-08-04

A bug fix for anyone running an engine with a native process underneath it,
and two pieces of CI hygiene. No API changes.

### Fixed
- **Engines with a child process died shortly after starting.**
  `sim.start_engine` spawned every engine with `stdout=PIPE, stderr=PIPE` and
  read those pipes only if the process died during startup — on the success
  path nobody drained them. Two consequences: an engine chatty enough to fill
  the 64 KB pipe buffer blocks forever on its next write, and when the
  launching process exits the read ends close, so anything still writing gets
  `SIGPIPE`. Python ignores `SIGPIPE`, so a bridge survives; a native daemon
  under it does not. Chrono reported `ready` and was a zombie by the next
  command, with `simulate` answering *"Cannot reach the Chrono daemon"*.
  Output now goes to `.solidmind/logs/engine-<backend>.log`, and the crash and
  timeout errors read its tail — so they name the log file rather than losing
  detail. Gazebo and Isaac were unaffected: their bridges are pure Python.
- **A rate-tripwire test was timing-dependent.** It fired 300 messages and
  expected the >100 msg/s warning, which only holds if the loop finishes
  inside the tripwire's one-second window — true locally, not on a loaded
  runner, where the burst lands at 101 msg/s, one event either side of the
  threshold it is testing. The clock is frozen now.

### Changed
- **ruff is pinned to an exact version** in both the workflow and the `dev`
  extra, rather than `>=0.6,<1`. A release moved CI to 0.16 mid-stream, which
  turned on formatting of Python inside Markdown fences and reddened two docs
  files nobody had touched, while a local run disagreed. The two pins must
  move together.

## [0.3.0] — 2026-08-04

The simulation engines move out of core and into four sibling repositories,
joined to it only by a published contract. If you drove Isaac, Gazebo, Chrono
or the RL pipeline by importing their packages from this one, that no longer
works — install the engine you want (`sim.engine_status` names each one's
install hint) and talk to it through `sim.*` and `motion.*` as before, or use
the bundled `reference_engine`, which needs nothing. Everything else in this
release is what that move required, and what verifying it turned up.

### Added
- **The engines were verified against real installations, and that is what
  found everything under Fixed.** The TCK now has runs on record for Gazebo Harmonic
  driving a live headless world and for Isaac Sim 5.1 on a GPU
  (`runtime_mode: "real"`, sessions and teleop included), alongside the
  reference engine and Chrono's built daemon. Both engine repos gained
  real-runtime suites — 5 tests spawning and stepping an actual Gazebo world,
  13 against real Isaac physics — and each refuses to run unless the bridge
  reports `runtime_mode: "real"`, because a green end-to-end that silently fell
  back to a stub is worse than a skipped one. Finally, all four repositories
  were cloned fresh from GitHub, started through `sim.start_engine` and driven
  through `motion.simulate`; that is where the model-format gap surfaced.
- **`docs/engine-extraction-verification.md`** — the nine acceptance criteria
  from §9 with evidence for each, and what remains open.
- **Verification is tests, not prose, wherever it can be.**
  `tests/test_split_acceptance.py` asserts the wheel excludes every engine
  package, that importing core loads none of them at runtime, and that no
  comparison in `server/` branches on an engine name (with the one recorded
  exception, the chrono *study* solver). `tests/test_pipeline_e2e.py` walks
  mechanism → package → URDF → RL env config, crossing every boundary the
  split introduced.
- **`pyproject.toml` gained explicit maturin include/exclude lists.** There
  were none, so the wheel would have shipped whatever top-level packages a
  checkout happened to contain — including the engines.
- **The engine repositories exist.** `scripts/split_engines.sh` cuts
  `solidmind-engine-isaac`, `solidmind-engine-gazebo`,
  `solidmind-engine-chrono` and `solidmind-rl` out of core with
  `git subtree split`, so each keeps the commits that touched its own files
  rather than arriving as a squashed import. Every repo gets packaging for its
  *own* interpreter, a README, CI (own tests + import guard + TCK), and a
  vendored copy of the conformance kit. `scripts/verify_engine_repos.sh` proves
  the claim rather than asserting it: each repo's tests run from its own
  directory with core nowhere on the path, and the Isaac and Gazebo bridges
  answer the TCK as conformant standalone. Step 6 of
  `docs/engine-integration-architecture.md` §7.
- **`docs/engines.md`** — the curated engine list and the four-step guide to
  adding your own (implement four verbs, read two formats, pass the TCK, drop a
  descriptor).
- **The repositories are published and core's copies are gone.**
  `scripts/remove_engines_from_core.sh` has been run. The four repos are public
  at `github.com/John-Cusack/solidmind-engine-{isaac,gazebo,chrono}` and
  `solidmind-rl`, each with its own history, and core holds no engine package —
  `tests/test_import_boundaries.py` asserts their absence rather than merely
  scanning them for `server.*` imports. Every test that reached into core has
  been ported to its repo's own equivalents; no `tests/needs_porting/`
  directory remains anywhere.
- **Reference engine + TCK — conformance is now runnable.** `reference_engine/`
  is core's own implementation of the contract: pure Python, no install, with
  analytic physics (gear-ratio propagation, pendulum period, free fall) so it
  can answer the physics tier honestly. It is a fresh engine rather than the
  Gazebo stub renamed — the stub stays Gazebo's, this one is the worked example
  an engine author clones. Step 5 of
  `docs/engine-integration-architecture.md` §7.
- **`tck/` — the Engine Integration Contract Test Kit.** `python3 -m tck --port
  N` runs six tiers (protocol, package, results, sessions/teleop, physics
  sanity, latency) against any engine in any language and prints a per-check
  report; exit code 0 means conformant. It imports nothing from `server`, so an
  engine repository can vendor it. Capability honesty is checked both ways: an
  advertised verb must work and an unadvertised one must answer
  `UNSUPPORTED_COMMAND`. Partial engines pass with skips, not failures. A CI
  job runs it against the reference engine with zero engines installed, and
  `tests/test_sim_cross_backend.py` is retired into tier 3.
- **All four engines verified conformant locally** — and the run found real
  gaps, now fixed: the Gazebo bridge emitted `GAZEBO_PROTOCOL_ERROR` where the
  contract says `INVALID_JSON`/`INVALID_REQUEST` and routed a missing `cmd` to
  the unknown-verb path; both bridges used vendor-prefixed session codes instead
  of `SESSION_NOT_FOUND`; and Isaac omitted the required `summary.dt_s`.
- **Engine Integration Contract v1 — published spec, schemas, and handshake.**
  `docs/engine-contract.md` is now the normative contract between core and any
  simulation engine: NDJSON envelope with an opaque `request_id`, a required
  `hello` capability handshake, a frozen four-verb floor
  (`hello`/`ping`/`simulate`/`shutdown`), a shared error taxonomy, session
  semantics, and the versioning promise (additive-only within v1.x). Three
  schemas back it — `schemas/sim_package.schema.json` (manifest),
  `sim_result.schema.json` (simulate results, with `summary.peak_joint_forces`
  and `time_series[].joint_efforts` shape-pinned for Tier 3.5), and
  `field_snapshot.schema.json` (multi-physics sidecar, no producer yet).
  All three bridges answer `hello` with capabilities derived from their actual
  dispatch, echo `request_id` verbatim when sent, and return
  `UNSUPPORTED_COMMAND` for unknown verbs (was `UNKNOWN_COMMAND` on
  gazebo/isaac, a bare string on chrono). The chrono daemon's error envelope is
  now `{"code", "message"}` like the others, and its summary carries `dt_s` +
  `engine_mode` so results validate. First step of the engine-extraction
  migration (`docs/engine-integration-architecture.md` §7); everything is
  additive (`tests/test_engine_contract.py`).
- **`cad.export_sim_package` writes `manifest.json`.** The canonical model
  description used to exist only in memory and travel back over MCP — a
  non-Python engine had nothing to read. New `server/sim_package_manifest.py`
  (pure functions, no FreeCAD dependency) serializes it beside the meshes in SI
  units, in two schema-valid modes: **full** (mechanism supplied — links with
  mass/inertia/collision, joints, plus abstract rotor `actuators` and `sensors`
  from a `drone_config`) and **reduced** (bodies only — pose + mesh). Each mesh
  declares its native unit (`mm`, `scale_to_m: 0.001`) and vertex frame
  (`link_local` vs `world`). The tool result gains `manifest_path`.
- **Part-class taxonomy: `part_class` field + shared failure-mode catalog.**
  The Reflect step can now look up a part's characteristic failure modes by
  class instead of inferring them from a brief's free-text name. `design.save_brief`,
  `design.add_part`, and `design.update_part` carry an optional `part_class`
  (persisted on `PartEntry`/`DesignBrief`); `server.failure_modes.load_taxonomy` /
  `expectations_for` load a hand-curated catalog under `me_knowledge/failure_modes/`
  (`structural.yaml` + `foam_dart_launcher.yaml`) into typed `ReflectExpectations`.
  Seeded with the four part classes that already have project builds (hexapod leg,
  planetary gearbox, quadrotor arm, rc-car chassis) plus the promoted foam-dart
  classes; the foam-dart example now reads the shared catalog (local
  `failure_modes.yaml` kept as an offline override). Loading degrades gracefully on
  a missing dir / absent PyYAML / one malformed entry. Closes the ROADMAP's named
  Specify + Reflect gating items (`tests/test_failure_modes.py`).
- **Foam-dart spring launcher example — sim-to-real validation rig.** New
  `examples/foam_dart_spring_launcher/` walks the full nine-step inner loop
  (Specify → Synthesize → Reflect → Screen → Simulate → Interpret → Decide →
  Act → Learn) on a single-shot spring-plunger launcher. A deliberately
  under-dimensioned latch FAILs the analytical `analysis.screen_stress`
  tier on `stress_concentration`; `decide.from_failure` proposes a root
  fillet; the V2 re-screen passes (peak 68 → 6 MPa). A real Chrono run
  validates the spring→plunger energy delivery against the calibration-first
  `physics_model.py` (0% residual), and `--calibrate-from-shot` fits the lumped
  efficiency from one measured shot to predict the other pullbacks. First
  example to close the autonomous iteration test for a part class
  (`tests/test_iteration_loop_foam_dart_e2e.py`). `--smoke` runs solver-free
  for CI.
- **Foam-dart launcher: real structural FEA + kinematic Tier-2 rungs.** The
  Simulate step now drives a real `analysis.stress_check` (CalculiX)
  **mesh-convergence study** on an enriched latch — the builder grows a real
  cantilever tooth with a V1 sharp / V2 filleted root
  (`orchestrator/worker_builds/foam_dart_launcher.py`
  `build_latch_variant`/`latch_profile`). Each root is solved at two mesh
  densities: the filleted root (V2) **converges** and its value confirms the
  analytical screen (±25%), while the sharp root (V1) **diverges** — a stress
  singularity FEA cannot resolve, so the analytical screen's FAIL is the
  operative rejection (the report notes that an idealized clamp edge is itself
  singular, deferring exact root stress to sub-modeling). A motion Tier-2 rung
  reports plunger travel, binding, and moving clearance (analytical from the
  brief, with a best-effort FreeCAD geometric confirmation). Both rungs report
  `SKIPPED` when a backend is absent and emit nothing under `--smoke`. New
  guarded e2e tests (V2 converges + confirms screen, V1 diverges) plus CI-safe
  unit coverage for the profile, face selection, screen-vs-FEA and
  mesh-convergence classification (`tests/test_foam_dart_fea_e2e.py`,
  `tests/test_foam_dart_kinematics_e2e.py`).
- **Analytical structural screening tier (`analysis.screen_stress`).** Beam
  bending (σ=Mc/I) + handbook stress-concentration-factor lookup + Euler
  buckling bound, returning an `AnalysisCheck` that gates Tier-3 FEA — the
  structural analogue of the `motion.*` analytical rung.
- **Typed failure modes + Reflect/Decide/Interpret primitives.** New
  `FailureMode` enum and `ReflectExpectations` dataclass on
  `server/analysis_models.py`; `decide.from_failure` / `decide.interpret`
  tools (`server/decide.py`) turn a failing check into a typed fix proposal and
  compare results against pre-sim expectations.
- **Chrono spring force on prismatic joints.** `JointEdge` gains optional
  spring parameters; `simulation_spec_builder` emits a `spring` object and the
  Chrono daemon applies it via `ChLinkTSDA`, making spring-loaded sliders a
  real dynamic case.
- **Outer-loop wiring closed against five real part classes.** New
  `orchestrator/worker_builds/` package with per-part-class builders
  (`sun_gear`, `planet_carrier`, `quadrotor_arm`, `rc_car_chassis`,
  `hexapod_leg`) that drive the FreeCAD addon over TCP to produce
  real STEP geometry. Each builder dispatches through
  `worker_entry._build_*` (gear / carrier / envelope / new `leg`
  route) and post-processes its `metadata.json` so `interface_actuals`
  is keyed by the design-friendly feature names that
  `ValidationCheckPoint`s reference. `common.dispatch_and_rewrite`
  collapses the build + rewrite pattern to a single call.
- Three new measurement strategies in `orchestrator/measure.py`:
  `_measure_pin_circle_diameter` (PCD from N hole centroids, used by
  planet_carrier + motor-mount patterns), `_measure_pocket_depth`
  (top face minus pocket-floor face), `_measure_segment_length`
  (max of bbox X/Y). Registered with aliases (`pcd_diameter`,
  `motor_mount_pcd`, `coxa_length`, `femur_length`, `tibia_length`,
  `axle_bore_dia`, `hip_yaw_bore_dia`, etc.) so spec
  `ValidationCheckPoint.feature` keys can be design-friendly without
  the strategies having to know about every part class.
- `tests/test_orchestrator_real_worker_e2e.py` extended with
  verify-mode tests for `planet_carrier`, `quadrotor_arm`,
  `rc_car_chassis`, `hexapod_leg`. Each walks G0→G5, builds via the
  real FreeCAD addon, and asserts `report.measurement_source ==
  "orchestrator"` plus all checkpoints pass.
- `tests/test_orchestrator_drift_e2e.py` — deliberately stomps the
  worker's claimed `bore_dia` after a real `sun_gear` build using
  `common.override_claimed_measurements`, then asserts
  `validate_results(verify_measurements=True)` returns
  `FailureCode.MEASUREMENT_DRIFT` with `overall_pass=False`. Proves
  the self-verifying measurement path actually catches lies — not
  just passes them through.
- `_build_envelope` now accepts `sub_spec["envelope_holes"]` — a list
  of `{cx, cy, diameter_mm, depth_mm, type}` dicts — so chunk-6 and
  chunk-7 builders can place patterned holes at non-origin positions
  without needing custom dispatchers. Backwards-compatible with the
  legacy "one centered hole per interface" path.
- `_build_leg` helper in `orchestrator/worker_entry.py` for chunk 8:
  three rectangular pads laid end-to-end (coxa+femur+tibia) sharing
  edges, fused into one continuous body, with three pivot bores at
  the segment junctions. Routed via `build_type="leg"` in
  `_build_geometry`'s dispatch.
- `orchestrator/worker_builds/common.py` extended with
  `rewrite_interface_actuals` and `dispatch_and_rewrite` helpers so
  the metadata-rewrite pattern (translating auto-measured
  `diameter_mm` keys into design-friendly `bore_dia` /
  `pin_circle_dia` / etc.) doesn't have to be duplicated across
  every builder.
- ROADMAP now models SolidMind CAD as a **two-loop** system: an outer `orchestrator/*` loop (G0 → G7 gate walk + SBCE macro-scale Decide) that's well-built but has stubbed workers, and the nine-step inner loop that runs inside each worker. Previous drafts only described the inner loop; the ~170 tests across 11 orchestrator test files deserved to be credited. The outer loop's biggest gap is that `test_orchestrator_e2e.py:131` writes a fake STEP file where a real `cad.*` worker build should go.
- **Priority stack** replaces the single "highest-leverage first move." Three parallel independent changes: (1) bring `analysis.*` up to `motion.*`'s tier structure (Tier 1 analytical screens before Tier 3 FEA), (2) the paired `FailureMode` enum + `ReflectExpectations` wedge, (3) wire one real worker build into the outer orchestrator loop. They can be worked concurrently by different contributors without merge conflicts.
- ROADMAP explicitly credits the `motion.*` tier ladder (Tier 1 analytical → Tier 2 kinematic → Tier 3 dynamic) as the proven in-repo pattern that `analysis.*` should copy for its Screen step. The motion/analysis asymmetry is now called out as the most important structural observation.
- ROADMAP "Why this is mostly a refactor" section mapping each `.claude/rules/*.md` file onto a corresponding loop step and noting that `motion-validation.md` is the only rule whose tool-layer equivalent already exists — proving the rule-to-tool refactor pattern works.
- `docs/ROADMAP.md` — per-step gap analysis of the autonomous iteration loop against its textbook pedigree. The loop is modeled as nine steps: Specify → Synthesize → Reflect → Screen → Simulate → Interpret → Decide → Act → Learn. Six of the nine map directly onto Shigley / Pahl & Beitz / Ullman / Dieter; three (Reflect, Screen-as-first-class, Learn) are senior-engineer folklore the textbooks assume rather than teach. Each step has a status marker, tool inventory, test coverage summary, and concrete "move ◐ to ✓" actions.
- `tests/test_iteration_loop_e2e.py` — skipped placeholder for the end-to-end loop-closure test. The docstring walks the nine steps on a deliberately under-dimensioned hip bracket and lists the four dependencies that have to land before the test can unskip.
- README now leads with the autonomous-iteration thesis (LLM builds → sims → fixes → repeats) and includes an honest "Where it's going" section built around the nine-step loop table.
- "What it does today" section replaces the old linear Demo walkthrough with an iteration-cycle walkthrough (v1 build → sim failure → fix → re-sim → stress check → teleop).
- FreeCAD 1.1 support. `compat.IS_V1_1_PLUS` flag for future version-specific branches. Joint type indices verified against FreeCAD 1.1's `JointObject.JointTypes` (exact match with existing `_JOINT_TYPE_INDEX`).
- `pyproject.toml` metadata for public release: `authors`, `keywords`, `classifiers`, `[project.urls]`, plus `orchestrator` and expanded `dev` extras. Conservative `[tool.ruff]` lint config.
- `.github/` scaffolding: bug / feature / config issue templates, pull request template, Dependabot config.
- CI: Ruff lint job (non-blocking for now), Python version matrix scaffold, `pydantic` added to test deps.
- README CI / License / Python / FreeCAD badges.
- Docker E2E tests now skip cleanly when the optional `httpx` extra is missing (`pip install -e .[orchestrator]`).

### Changed
- **Engines are registry data, not a table in core.** Descriptors in
  `engines.d/*.toml` (and `~/.solidmind/engines.d/` for third parties) now
  supply every port, launch command, install hint and piece of model-facing
  guidance core used to hardcode — step 4 of
  `docs/engine-integration-architecture.md` §7. Dropping in a descriptor adds a
  backend to the MCP tool enums, the prompt guidance and `sim.start_engine`
  with no core edit; a descriptor without a `launch` command is attach-only
  (a user-run daemon or a remote engine).
  - **One client, one adapter.** `server/engine_client.py` replaces
    `isaac_client`/`gazebo_client`/`chrono_client`, and `server/sim_adapter.py`
    replaces `isaac_adapter`/`gazebo_adapter` — about 2,100 lines of
    near-duplicate code gone. The client caches each engine's `hello` and
    carries the **msg-rate tripwire** (architecture doc §3.7): sustained
    >100 msg/s on any command logs a warning, so core can't quietly end up
    inside a control loop.
  - **Behaviour comes from the handshake.** `motion.simulate` picks
    session-based or single-call simulation from the engine's advertised
    `modes`; teleop is refused when an engine doesn't advertise it; and a new
    `teleop_dofs` capability (additive, v1.x) means commanding an axis an
    engine doesn't support is an error instead of a silently dropped setpoint.
  - **Vendor-named tools folded**: `motion.isaac_launch`/`motion.isaac_stop`
    are covered by `sim.start_engine`/`sim.stop_engine`, and
    `motion.isaac_screenshot` becomes capability-gated `motion.screenshot`.
    `motion.verify_sim_package`'s `check_isaac` is now `check_engine`.
  - Still name-bound and scheduled for later steps: the chrono *study* solver
    in `server/study_solvers.py`, and `rl_training`'s `ISAAC_PYTHON` launch.
- **Dialect inversion: core emits only manifest + meshes + URDF.** Every vendor
  format is now compiled by the engine that consumes it, at load time, from the
  canonical package (step 3 of `docs/engine-integration-architecture.md` §7).
  - **Gazebo**: new `gazebo_bridge/package_to_sdf.py` compiles `manifest.json`
    into SDF — motor plugins, sensors, primitive-vs-mesh collisions and all —
    when `spawn_model`/`simulate`/`teleop_start` are given a `package_path`.
    `server/sim_export.py` loses `write_sdf`/`validate_sdf` and the plugin and
    sensor emitters; `cad.export_sim_package` loses `emit_sdf`.
  - **PX4**: `server/px4_airframe_generator.py` moved to
    `gazebo_bridge/px4_airframe.py` and is manifest-driven. Pass `px4=true` to
    `motion.simulate`/`motion.teleop_start` alongside `package_path`; core's
    airframe specs expose `to_drone_config()` instead of
    `to_px4_airframe_params()`. Rotor positions are now taken relative to the
    root link, so a chassis away from the origin no longer skews CA_ROTOR arms.
  - **Chrono**: new `chrono_bridge/` package — a contract server that compiles
    the neutral mechanism into Chrono's native spec (moved
    `simulation_spec_builder`) and drives the C++ daemon underneath it.
    `sim.start_engine("chrono")` starts the pair as one process. Core no longer
    builds Chrono specs or post-processes planetary speeds.
  - **`diagnose` is normalized**: engines report generic joint-type counts, DOF
    and per-joint connectivity (`isaac_bridge/diagnose_normalize.py`), so
    `server/sim_verify.verify_urdf_vs_isaac` becomes one engine-agnostic
    `verify_urdf_vs_diagnose`.
  - Manifest actuators gained `motor_constant`, `max_rot_velocity_rad_s` and
    `min_rot_velocity_rad_s` (additive, schema v1.x), because `k` and `ω` are
    not recoverable from thrust alone.
  - **Not verified here**: the drone SITL end-to-end gate needs Gazebo Harmonic
    and PX4, neither installed in this environment.
- **Engine packages no longer import core.** The three reverse imports that
  blocked splitting the engines into sibling repositories are gone (step 2 of
  `docs/engine-integration-architecture.md` §7). `isaac_bridge` ships its own
  contract client (`isaac_bridge/client.py`) instead of borrowing
  `server.isaac_client`; `server/mavlink_controller.py` moved to
  `gazebo_bridge/mavlink_controller.py`; `server/urdf_analyzer.py` moved to
  `rl_training/urdf_analyzer.py`. The `rl.*` tools now import the RL pipeline
  lazily and return `RL_PIPELINE_UNAVAILABLE` when it is absent, so starting the
  MCP server never requires it. `tests/test_import_boundaries.py` statically
  enforces both directions: no `server.*` import anywhere in an engine package,
  and no module-level core→engine import.
- **The TCK skips the physics tier when an engine does not advertise
  `mechanism`.** Those scenarios are in-band mechanism dicts, so running them
  against a package-only engine measured nothing and then failed it for the
  result — the kit's error, not the engine's. It now skips with an explicit
  reason, the same way tiers 2 and 4 already skip on `package` and `session`.
  Capability honesty runs both ways: an engine must implement what it
  advertises, and must not be judged on what it doesn't.
- **`rl.*` drives the RL pipeline as a subprocess — core imports no engine code
  at all.** The pipeline runs on Isaac Sim's bundled interpreter, which core's
  venv cannot import from, so `rl.configure_environment` and `rl.deploy_policy`
  now shell out to a new `rl_training.cli` (`configure`, `analyze`, `export`)
  and parse one JSON object back, the same way training already worked. With
  that, `tests/test_import_boundaries.py` drops its last allowed exception:
  nothing in `server/` imports an engine package, lazily or otherwise. Step 7
  of `docs/engine-integration-architecture.md` §7.
- **ROADMAP outer-loop status flips from `◐ well-built but workers
  stubbed` to `✓ closed on 5 part classes`.** The two-loop table and
  the Move-3 priority-stack section are updated accordingly. Move 3
  is marked done with a "What landed" subsection covering the wiring
  work.
- `orchestrator/measure.py`'s `_measure_bbox_diagonal` now reads bbox
  dims from `cad_get_dimensions` instead of `cad_get_body_topology`
  (which doesn't return a `bounding_box` key — latent bug, fixed in
  passing).
- FreeCAD 1.1 is now the recommended runtime (1.0.2 remains supported via the existing compat layer). README and CONTRIBUTING install steps updated.
- Security reporting now points at GitHub Security Advisories instead of a placeholder `security@solidmind.dev` email. Same change in `CODE_OF_CONDUCT.md`.
- `.gitignore` tightened to catch `*.AppImage`, `*.mp4`, `docs/demo_clips/`, `training_runs/**`, `analyses/`, `watch_*anim*.json`, `type_prompt.sh`, CalculiX solver run artifacts (`*.cvg`, `*.dat`, `*.sta`, `--version.*`), and `requirements-backup.txt`. Added `!docs/images/*.png` exception so README illustrations can be committed.

### Removed
- **The simulation engines no longer ship with core.** `isaac_bridge/`,
  `gazebo_bridge/`, `chrono_bridge/`, `chrono_daemon/` and `rl_training/` are
  gone from this repository, along with `server/{isaac,gazebo,chrono}_client.py`
  and `server/{isaac,gazebo}_adapter.py`. They live in
  `github.com/John-Cusack/solidmind-engine-{isaac,gazebo,chrono}` and
  `solidmind-rl`, and are reached only over the Engine Integration Contract.
  **Install the engine you need** — `sim.engine_status` reports an
  `install_hint` for each — or use the bundled `reference_engine`, which is pure
  stdlib and needs nothing. Core's wheel now carries explicit maturin
  include/exclude lists, so it ships the contract, the orchestration, the
  reference engine and the conformance kit, and no engine code at all.
- Bundled knowledge content under `me_knowledge/notes` and `me_knowledge/sim_changes` from source control; repository now tracks placeholders only.

### Fixed
- **The MCP handshake reports the real version.** `serverInfo.version` carried
  its own hardcoded copy of the version string, so it still said `0.2.0`. It is
  now read from installed metadata, falling back to `pyproject.toml` because
  the server is usually run straight from a checkout.
- **`motion.simulate` no longer hands an engine a model format it never
  advertised.** With no `package_path`/`urdf_path`/`sdf_path`, the in-band
  mechanism *is* the model, so the engine has to advertise
  `formats: mechanism`. Core was sending one to Gazebo — which ingests
  packages, SDF and URDF and never claimed otherwise — and Gazebo answered: a
  20:40 gear pair came back with both gears at the same speed, a ratio of 1.0,
  plausible and entirely invented. Neither side enforced the contract. Such a
  call now returns **`UNSUPPORTED_MODEL_FORMAT`** naming the formats the engine
  does take, on both the batch and teleop paths. An unreachable engine is not
  second-guessed. *This turns a previously "successful" call into an error —
  export a sim package with `cad.export_sim_package` and pass `package_path`.*
- **`sim.start_engine` could not start Isaac or Gazebo.** Both descriptors
  launch `scripts/run_<engine>_bridge.sh`, and the split left those wrappers
  behind in core; core would have run a file that did not exist. Restored to
  their repositories, and `tests/test_engine_registry.py` now checks that every
  descriptor's launch target resolves wherever the engine is cloned.
- **The URDF pipeline rewrote its own test fixture.** `write_urdf` transforms
  meshes in place to link-local coordinates, and `TestURDFGenerationPipeline`
  handed it the checked-in fixture, so every run shifted `Arm.stl` down another
  50 mm. The tests passed either way, which was the problem.
- **An unbuilt `solidmind_geometry` now says so.** The bare
  `ModuleNotFoundError` named a module nobody writes by hand and took 15 test
  modules with it, reading like a broken suite rather than a missing build
  step. It now names the remedy.
