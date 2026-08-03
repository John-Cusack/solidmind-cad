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
| isaac (no Isaac Sim installed) | conformant | physics skipped (stub mode), package skipped (`urdf` only) |
| chrono (shim + built C++ daemon) | conformant | gear-ratio physics **passes** against the real daemon |

Reproduce with `python3 -m tck --port <engine port>`. The run found and fixed
four real gaps: Gazebo's error taxonomy (`INVALID_JSON` / `INVALID_REQUEST`),
its missing-`cmd` handling, both bridges' session codes, and Isaac's absent
`summary.dt_s`.

## 4. Hexapod e2e (export → import → RL config) across the split layout

**Status: partially verified — the core→RL half runs; the Isaac half needs a GPU.**

`tests/test_pipeline_e2e.py` walks a mechanism through the real pipeline in
the split layout: build the sim model, write the canonical package
(`manifest.json` + meshes), write the courtesy URDF, then hand that URDF to
`rl.configure_environment`, which shells out to the RL pipeline's CLI. That
covers every boundary crossing except the engine import itself.

The Isaac leg (`import_urdf` → `diagnose` → RL config from a live scene) needs
Isaac Sim; `tests/test_isaac_urdf_integration.py` covers it and skips here.
It has **not** been run against real Isaac on this branch.

## 5. Engine repos import nothing from `server.*`

**Status: verified, and enforced two ways.**

- In core: `tests/test_import_boundaries.py` statically scans every engine
  package for `server.*` imports — module level or nested — and finds none.
- In each split repo: a generated `tests/test_import_boundary.py` does the
  same from the other side, and runs in that repo's CI.
- `scripts/verify_engine_repos.sh` runs each repo's suite from its own
  directory with core off the path: isaac 75 tests, gazebo 68, chrono 5, rl 4.

Tests that still reach into core are filed in each repo's
`tests/needs_porting/` (11 modules total) with a note on what to swap them
to. Core keeps its copies until they are ported.

## 6. Wheel ships no engine code

**Status: verified at the packaging level.**

`pyproject.toml`'s `[tool.maturin]` now carries explicit `include`/`exclude`
lists — previously there were none, and maturin swept in every top-level
package it found. `tests/test_split_acceptance.py::TestWheelShipsNoEngineCode`
asserts every engine package is excluded, none is included, and core's own
packages still are.

Checked at configuration level deliberately: it holds in a working checkout
where the engine directories are still present, and building a wheel needs
Rust plus several minutes. **A built-wheel inspection has not been run**; the
first `maturin build` after the engines are removed should confirm it.

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

## What remains

| Item | Status |
|---|---|
| Publish the four sibling repos | **done** — `github.com/John-Cusack/solidmind-engine-{isaac,gazebo,chrono}` and `solidmind-rl`, public, with history |
| Remove the engines from core | **done** — core holds no engine package; `tests/test_import_boundaries.py` asserts it |
| Port the deferred test modules | open, in the engine repos (`tests/needs_porting/`) |
| Built-wheel inspection | open — `maturin build` now that the packages are gone |
| Isaac and Gazebo real-runtime legs | open — needs a GPU host and a Gazebo install |
