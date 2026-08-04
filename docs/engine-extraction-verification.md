# Engine extraction — verification

Step 7 of `docs/engine-integration-architecture.md` §7: check the nine
acceptance criteria in §9, with evidence rather than assertion.

Everything mechanically checkable is a test —
`tests/test_split_acceptance.py` and `tests/test_import_boundaries.py` — so a
change that quietly re-couples core to an engine fails CI instead of a review.
What needs a real engine, a GPU or a published remote is recorded here with
what was actually run.

**Verified:** 2026-08-03, on this branch — after the split was published and
the engines removed from core.

---

## 1. N+1 test — a new engine requires zero core edits

**Status: verified, and mechanized.**

`tests/test_engine_registry.py::TestThirdPartyDescriptors` drops a `mujoco.toml`
into a temporary `engines.d` and asserts the engine appears in core's backend
vocabulary, resolves its port, and builds its launch command — with no core
change. The same descriptor data feeds the MCP tool enums
(`_engine_enum_property` in `server/main.py`) and the model-facing selection
guidance (`_backend_selection_guidance` in `server/prompts.py`), so a
third-party engine becomes both callable and *recommendable* by adding a file.

What a stranger does, end to end, is `docs/engines.md`: implement four verbs,
read two file formats, pass the TCK, drop a descriptor.

## 2. Core CI green with zero engines installed

**Status: verified.**

- `tests/test_split_acceptance.py::TestCoreRunsWithoutEngines` imports
  `server.main` in a subprocess and asserts no engine package was loaded.
- The `tck` CI job installs `jsonschema` and nothing else, starts the
  reference engine, and runs the conformance kit.
- The reference engine is pure stdlib, so "zero engines installed" still
  leaves core with a working engine to exercise.

## 3. TCK passes for reference + isaac + gazebo + chrono locally

**Status: verified locally, 2026-08-03.**

| Engine | Result | Notes |
|---|---|---|
| reference | conformant | all six tiers, physics included |
| gazebo (stub runtime) | conformant | physics tier skipped — reports `runtime_mode: "stub"` |
| gazebo (**real**, Gazebo Harmonic) | conformant | against a live headless world; physics skipped — Gazebo does not advertise `mechanism` |
| isaac (no Isaac Sim installed) | conformant | physics skipped (stub mode), package skipped (`urdf` only) |
| isaac (**real**, Isaac Sim 5.1 / RTX 3090) | conformant | `runtime_mode: "real"`; sessions and teleop pass against GPU physics |
| chrono (shim + built C++ daemon) | conformant | gear-ratio physics **passes** against the real daemon |

Reproduce with `python3 -m tck --port <engine port>`. The run found and fixed
six real gaps: Gazebo's error taxonomy (`INVALID_JSON` / `INVALID_REQUEST`),
its missing-`cmd` handling, both bridges' session codes, Isaac's absent
`summary.dt_s`, and two found by the first real-runtime run:

- **Gazebo fabricated its results.** `RealGazeboRuntime.handle_simulate`
  delegated to the stub and relabelled the summary `gazebo_real`, so a real run
  returned the stub's synthetic ramp — 120 rpm for every part, 5 N·m for every
  joint — whatever the world actually did. Core cannot tell an invented number
  from a measured one, which is why the gear ratio came back as exactly 1.0.
  The real runtime now steps in `output_interval` chunks, reads link poses back
  off `dynamic_pose/info`, derives speeds from the rotation over the final
  interval, and **omits** any quantity it did not measure.
- **The TCK judged an engine on a format it never claimed.** The physics
  scenarios are in-band `mechanism` dicts; Gazebo advertises `package`, `sdf`
  and `urdf`. The tier now skips when `mechanism` is not advertised, the same
  way tiers 2 and 4 already skip on `package` and `session`.

## 4. Hexapod e2e (export → import → RL config) across the split layout

**Status: partially verified — the core→RL half runs; the Isaac half needs a GPU.**

`tests/test_pipeline_e2e.py` walks a mechanism through the real pipeline in
the split layout: build the sim model, write the canonical package
(`manifest.json` + meshes), write the courtesy URDF, then hand that URDF to
`rl.configure_environment`, which shells out to the RL pipeline's CLI. That
covers every boundary crossing except the engine import itself.

The Isaac leg has now run. `tests/test_isaac_urdf_integration.py` and
`tests/test_isaac_bridge_real_runtime.py` live in solidmind-engine-isaac and
pass against Isaac Sim 5.1 on an RTX 3090 — 13 tests covering `import_urdf`,
`diagnose`, `reload`, `screenshot`, 0.5 s of GPU physics, and the session and
teleop lifecycles. Reproduce with:

```
SOLIDMIND_RUN_ISAAC_E2E=1 ISAAC_PYTHON=<isaacsim>/python.sh \
  python3 -m unittest tests.test_isaac_urdf_integration tests.test_isaac_bridge_real_runtime
```

Getting there needed three fixes, all found by the first real run:

- **The suites could not run at all.** Both drove the bridge in-process on a
  background thread; against real Isaac that segfaults, because Kit's event
  loop must be pumped from the main thread and the test runner owns it.
  `IsaacLifecycle` now spawns the bridge the way its `__main__` does and waits
  on `hello` rather than on the port — Isaac binds early, then spends up to a
  minute building a SimulationApp and fetching a remote environment USD.
- **One timeout poisoned every later call.** The bridge's own client sent no
  `request_id` and returned whatever line arrived next, so a late response came
  back as the *following* command's result — an `import_urdf` answered with a
  `reload`'s payload, `ok: true` and all. It now correlates and discards stale
  replies.
- **A stale assertion, never exercised.** `test_simulate_simple_2body` expected
  `samples[].time_s`, the bridge's pre-contract names, where the contract pins
  `time_series[].t`. The engine was right; the test had simply never run.

Each suite now refuses to run unless the bridge reports `runtime_mode: "real"`,
so a fallback to the in-process reference path fails loudly instead of
reporting a green e2e that never touched a GPU.

## 5. Engine repos import nothing from `server.*`

**Status: verified, and enforced two ways.**

- In core: `tests/test_import_boundaries.py` now asserts the stronger thing —
  no engine package is present in core at all — and still scans for `server.*`
  imports if one reappears mid-migration.
- In each split repo: a generated `tests/test_import_boundary.py` does the
  same from the other side, and runs in that repo's CI.
- `scripts/verify_engine_repos.sh` runs each repo's suite from its own
  directory with core off the path: isaac 259 tests, gazebo 110, chrono 64,
  rl 5.

The deferred tests are **done**. Every module that used to sit in a repo's
`tests/needs_porting/` now runs in that repo's suite, and the quarantine
directories are gone. Porting meant swapping core imports for local
equivalents — `server.motion_models` → the bridge's own view types,
`server.engine_client` → `tck.client.TckClient` or the bridge's client,
`server.sim_package_manifest` → checked-in package fixtures.

Two modules were not simply moved:

- `test_sim_real_backends` was cross-engine. Its Gazebo half became
  `test_gazebo_real_world.py`; its Isaac and Chrono halves were dropped in
  favour of those repos' own real-runtime suites, and the parts driving core's
  `sim_engine_manager` stayed in core, which tests the manager directly.
- `test_rl_deploy_fixes` split: the `DirectPolicyController` cases went to
  solidmind-engine-isaac with the code they exercise, and the env-config case
  stayed in solidmind-rl.

Porting also surfaced a real defect: `AppliedForceView.frame` defaulted to
`"world"` in the Chrono bridge while core defaults to `"body"`, so a mechanism
that omitted the field silently changed how its forces were applied.

## 6. Wheel ships no engine code

**Status: verified — a wheel was built and inspected.**

`pyproject.toml`'s `[tool.maturin]` now carries explicit `include`/`exclude`
lists; previously there were none, and maturin swept in every top-level
package it found. `tests/test_split_acceptance.py::TestWheelShipsNoEngineCode`
asserts the config, and `maturin build --release` confirms the artifact:

```
top-level entries in solidmind_cad-0.2.0-cp312-cp312-manylinux_2_34_x86_64.whl
  engines.d        4     reference_engine   4     server   106
  feature_support  4     schemas           13     tck        9
  solidmind_geometry 2
engine code: none          total files: 148
```

The wheel also *works* standalone: installed into a clean venv with only
`jsonschema` alongside it, `python -m reference_engine.bridge_server` starts
and `python -m tck` reports **conformant on all six tiers** — core shipping
its own engine and its own conformance kit, with no repository checkout
present.

## 7. No vendor names in core control flow; guidance is generated

**Status: verified, with one recorded exception.**

`tests/test_split_acceptance.py::TestNoVendorNamesInControlFlow` walks
`server/`'s AST and fails on any comparison against `"isaac"`, `"gazebo"` or
`"chrono"`. Names in descriptors, log messages and prose are fine — a branch
is not.

The one exception, listed in the test: `server/study_solvers.py`'s
`ChronoSolver`, a parametric-study solver keyed by name in the `SOLVERS`
registry. It drives the engine over the contract already; only its
registration is name-bound.

Backend enums, install hints and `when_to_use` guidance are generated from the
registry, so an uninstalled engine contributes no tools and an added one
contributes them without a core edit.

## 8. Tier 3.5 works unchanged against contract results

**Status: verified.**

`analysis.stress_from_simulation` reads `summary.peak_joint_forces` and
`time_series[].joint_efforts`, both shape-pinned in
`schemas/sim_result.schema.json` and checked by the TCK's results tier.
`tests/test_analysis_sim_coupling.py` exercises the coupling against
contract-shaped results.

## 9. Msg-rate tripwire live in the generic client

**Status: verified.**

`server/engine_client.py` counts messages per command and warns above a
sustained 100 msg/s. `tests/test_engine_registry.py::TestRateTripwire` asserts
it stays silent under normal traffic, warns exactly once (not per message)
under a burst, and tracks per command rather than per engine.

---

## 10. The engines work from a clean clone

**Status: verified.**

Not an acceptance criterion, but the question the criteria exist to answer.
All four repositories were cloned fresh from GitHub into `~/repos` — where the
descriptors point — started through core's own `sim.start_engine`, and driven
through `motion.simulate`.

| Engine | Result |
|---|---|
| reference | started, gear pair 20:40 → `gear_b = -300 rpm` (halved and reversed) |
| gazebo | started from the fresh clone; ingested a core-written sim package and compiled its own SDF |
| isaac | started from the fresh clone with real Isaac Sim |
| chrono | started, then failed with `CHRONO_CRASHED` naming the unbuilt daemon — correct, and its `install_hint` says how to build it |

This is where the **model-format gap** surfaced: core sent an in-band mechanism
to Gazebo, which advertises `package`, `sdf` and `urdf` and never claimed to
read a mechanism. Gazebo answered anyway, and the gear pair came back with both
gears at the same speed. Neither side enforced the contract. Fixed on both:
`server/tools_motion.py` refuses the call and names the formats the engine does
take, and the Gazebo runtime no longer fabricates results in real mode.

## What remains

| Item | Status |
|---|---|
| Publish the four sibling repos | **done** — `github.com/John-Cusack/solidmind-engine-{isaac,gazebo,chrono}` and `solidmind-rl`, public, with history |
| Remove the engines from core | **done** — core holds no engine package; `tests/test_import_boundaries.py` asserts it |
| Port the deferred test modules | **done** — no `needs_porting/` directory remains; §5 above |
| Built-wheel inspection | **done** — §6 above |
| Gazebo real-runtime leg | **done** — 5 tests spawn and step a live Gazebo Harmonic world, and the TCK runs against a real-runtime bridge; found the fabricated-results bug in §3 |
| Isaac real-runtime leg | **done** — 13 tests against Isaac Sim 5.1 on an RTX 3090, TCK conformant in `runtime_mode: "real"`; §4 |
| End-to-end from fresh clones | **done** — all four repos cloned from GitHub into `~/repos`, started through `sim.start_engine`, driven through `motion.simulate`; found the model-format gap below |
| Descriptor launch commands | **done** — the split left both `scripts/run_*_bridge.sh` wrappers behind in core, so `sim.start_engine` would have run a file that did not exist. Restored to their repos; `tests/test_engine_registry.py::TestShippedDescriptorsPointAtRealCommands` now checks every launch target wherever the engine is cloned |
