# Roadmap — Toward the Autonomous Iteration Loop

## Vision

The bet behind SolidMind CAD is that with enough simulation in the loop, an
LLM can iterate on its own mechanical designs — build a part, watch it break
in physics, fix it, and repeat — until the thing works. The repo today is the
co-pilot version of that vision: the LLM drives FreeCAD, runs simulation,
sees the result, and can modify geometry — but a human is still in the loop
at every gate, and the LLM needs human prompting to diagnose failures and
pick fixes.

This document is the honest map of where each piece of the loop stands,
what would move each gap forward, and how we'd know the loop has actually
closed on a given class of part.

## The loop, stage by stage

Seven stages make up one turn of the autonomous loop:

1. **BUILD** — create geometry from a goal
2. **OBSERVE** — see what was built
3. **SIMULATE** — put it in physics / field analysis
4. **DIAGNOSE** — turn raw sim output into "here's what's wrong and where"
5. **DECIDE** — pick a fix action (widen this feature, add a fillet, change material)
6. **ACT** — apply the fix back to geometry
7. **LEARN** — remember so the same mistake isn't repeated

### Stage 1 — BUILD ✓

**Status: dense.** Over 40 tools drive FreeCAD PartDesign directly.

- `cad.*` primitives: `new_document`, `new_body`, `sketch`, `pad`, `pocket`, `hole`, `revolution`, `sweep`, `loft`, `helix`, `fillet`, `chamfer`, `draft`, `thickness`, `mirror`, `linear_pattern`, `polar_pattern`
- 23 parametric generators in `geometry.*` — spur/worm/bevel/planetary gears, involute profiles, helical springs, cam profiles, four-bar linkages, thread profiles, propeller blades, keyways, O-ring grooves…
- `design.*` phased brief pipeline — intent → sizing → layout → build, with explicit interfaces between parts
- `spec.*` interview flow that turns "I want a sensor mount" into a validated spec before any geometry is drawn

Tool source: `freecad_addon/commands.py`, `server/tools_cad.py`, `server/tools_design.py`, `server/tools_spec.py`, `server/tools_geometry.py`.

Test coverage: `tests/test_tools_cad.py` (144 test methods), `tests/test_tools_design.py`, `tests/test_geometry.py` (162 tests), plus stub-mode integration tests in `tests/test_e2e_cad_flow.py`.

**What would move this forward:** nothing urgent. BUILD is the expensive part and it's done.

### Stage 2 — OBSERVE ✓

**Status: solid.** The LLM can see what it built at every level.

- **Pixels:** `cad.screenshot` (PNG via Coin3D), `cad.set_camera` / `cad.get_camera`
- **Geometry:** `cad.get_body_topology`, `cad.get_dimensions`, `cad.find_edges`, `cad.find_holes`, `cad.measure_between`
- **Tree:** `cad.get_model_tree`, `design.get_brief`, `design.get_part`
- **Face identity:** structured `face_map` returned on every modeling op so the LLM can refer back to specific faces by name across subsequent calls

Tool source: same as BUILD plus the `face_map` plumbing in `freecad_addon/commands.py`.

**What would move this forward:** richer spatial semantics on top of `face_map` (e.g. "the face that formed the top of the last pad", "the cylindrical face shared with Body_B"). The raw observation data is complete; higher-level reasoning over it is where the gap is, and that's really a DIAGNOSE problem.

### Stage 3 — SIMULATE ✓

**Status: strong.** Three dynamic backends, three analysis domains, full tier ladder.

- **Motion tiers:** Tier 1 analytical (`motion.validate`, `motion.check_gear_train`, `motion.propagate_motion`), Tier 2 kinematic in FreeCAD Assembly (`motion.create_assembly`, `motion.drive_joint`, `motion.check_interference`), Tier 3 dynamic (`motion.simulate` with `backend={isaac,gazebo,chrono}`)
- **Field analysis:** `analysis.stress_check` (CalculiX), `analysis.thermal_check`, `analysis.aero_check` (SU2 / DUST)
- **Coupling:** `analysis.stress_from_simulation` pipes peak joint forces from `motion.simulate` into a CalculiX stress check — this is the Tier 3.5 flow, the existing proof that "sim output → analysis input" is already a path the LLM can take
- **Engine lifecycle:** `sim.start_engine`, `sim.stop_engine`, `sim.engine_status` manage real backends as subprocesses
- **RL:** `rl.configure_environment`, `rl.start_training`, `rl.monitor_training` run the Isaac Lab + RSL-RL pipeline

Tool source: `server/tools_motion.py`, `server/tools_analysis.py`, `server/tools_sim.py`, `server/tools_rl.py`, `isaac_bridge/`, `gazebo_bridge/`, `chrono_daemon/`.

Test coverage: `tests/test_tools_motion.py`, `tests/test_motion_validators.py`, `tests/test_full_pipeline_e2e.py` (10 test methods exercising Tier 1 → Tier 3.5), plus per-backend tests gated behind `requires_chrono` / `requires_elmer` / `requires_isaac` decorators.

**What would move this forward:** see DIAGNOSE. The simulations run fine; the problem is that their *output* isn't shaped for an LLM to act on.

### Stage 4 — DIAGNOSE ◐ (the first hard gap)

**Status: structured data exists, but it isn't actionable.**

The good news: `server/analysis_models.py:191` defines `AnalysisCheck`:

```python
@dataclass(frozen=True, slots=True)
class AnalysisCheck:
    name: str              # e.g. "yield_check"
    status: CheckStatus    # PASS / WARN / FAIL
    message: str           # human-readable
    measured: float        # e.g. 0.9 (factor of safety)
    limit: float           # e.g. 1.5
    face_group: str        # "Face7", "top_face", …
    suggestion: str        # "Add fillet"
```

This is already better than raw numerical output. Motion validators in
`server/motion_validators.py` return similarly structured
`errors` / `warnings` / `notes` lists.

The bad news:

1. **`name` is a free-form string.** There is no `FailureMode` enum like
   `STRESS_CONCENTRATION` / `BUCKLING` / `CONTACT_STRESS` / `YIELD` /
   `FATIGUE`. The LLM has to infer the mode from the string, and so does
   any downstream tool that would want to dispatch on it.
2. **`suggestion` is a generic string.** "Add fillet" tells the LLM
   *something* is wrong at `face_group="Face7"`, but not what size, not
   which direction, not what tradeoff. Compare the ideal:
   *"Stress concentration at Face7 under 12 N·m hip load. Candidates:
   add 0.5 mm fillet (cheapest), widen section by 2 mm (better margin),
   switch to 7075-T6 (weight cost)."*
3. **Nothing links diagnosis to the next action.** The LLM sees
   `suggestion="Add fillet"` and `face_group="Face7"` and has to
   manually construct a `cad_fillet(radius=?, edges_of_face="Face7")`
   call. There's no tool that says "here, I've prepared three fix
   candidates for you" with the call arguments pre-computed.

Test coverage: `tests/test_analysis_models.py` round-trips `AnalysisCheck`
instances. No test asserts that an LLM (or a programmatic harness)
consumes `suggestion` and picks the right follow-up tool call.

**What would move this from ◐ to ✓:**

1. **Add `FailureMode` enum to `AnalysisCheck`.** A small, enumerated
   vocabulary — maybe a dozen values covering the common structural /
   motion / thermal failure modes. Dispatching downstream tooling on a
   free-form string is a footgun.
2. **Add a `candidates` list** alongside `suggestion`. Each candidate
   is a structured `{tool, args, estimated_improvement, cost_note}` so
   the LLM picks from options instead of translating a sentence.
3. **Test the parse→act path.** Give the test harness a known-bad
   geometry, call `analysis.stress_check`, assert that the returned
   `FailureMode` and `candidates` list are what a subsequent
   `cad.*` call would consume verbatim.

### Stage 5 — DECIDE ✗ (the second hard gap)

**Status: bare-bones.** Nothing in the current toolset actually *chooses
a fix*. What exists:

- `me.validate_constraints` / `me.apply_risk_gates` / `me.build_traceability` /
  `me.design_loop` — these are great for static constraint checking and
  "is this design ready for release" gating, but they operate on a
  constraint-dict model, not on a failure → remediation flow.
- `study.*` — parametric sweeps. This is the closest thing to "decide" we
  have, because a study over fillet radius ∈ [0.5, 5.0] mm will find the
  smallest radius that satisfies FoS ≥ 1.5. But the LLM has to already
  know the right variable to sweep. There is no tool that converts
  *"Add fillet at Face7"* into *"Study: sweep fillet radius on Face7
  edge set, objective: maximize minimum FoS, budget: 8 samples."*

Tool source: `server/tools_me.py`, `server/tools_study.py`.

Test coverage: `tests/test_tools_me.py` (static constraint validation),
`tests/test_tools_study.py` and `tests/test_study_*.py` (mock solvers).

**What would move this from ✗ to ◐:**

1. **A `decide_from_failure` tool** that takes an `AnalysisCheck` with a
   `FailureMode` and returns a ranked list of candidate actions — each
   action being a concrete tool call the LLM can execute. Starting
   surface: the four most common mechanical failure modes
   (stress concentration, buckling, deflection, contact stress), each
   with one or two canned fix strategies.
2. **Auto-study fallback.** When the decider doesn't have a canned
   strategy, fall through to "run a bounded parametric study on the
   nearest geometric variable." This turns DECIDE into "pick from the
   playbook, and if no play fits, run a sweep."
3. **Safety-factor budget awareness.** The decider should know the
   target FoS from the design brief, so it stops iterating once the
   margin is comfortably above threshold instead of chasing asymptotes.

### Stage 6 — ACT ◐

**Status: the tools exist, but they're the same tools as BUILD.**

There's no dedicated "apply this fix" layer. The LLM takes a DECIDE
output and calls `cad.fillet(radius=0.5, face="Face7")` directly. That's
fine today, but it means:

- Every fix action is a fresh tool call with no memory of why it was
  called. A failed fix doesn't tell the decider "tried that, didn't
  work."
- The fix history for a single design session lives only in
  conversation context, not in structured form.

**What would move this from ◐ to ✓:**

1. **Structured fix log.** Record every `(failure, chosen_action, result)`
   triple during a session. At minimum, append to the current
   `design.*` brief under a new `iterations` key.
2. **Loop-aware dispatch.** A thin wrapper that, when called with a
   candidate action from DECIDE, executes the action, re-runs the
   analysis that found the failure, and returns the before/after delta
   as one atomic result. This collapses "build → re-analyze → compare"
   into a single call the orchestrator can chain.

### Stage 7 — LEARN ◐ (infrastructure only)

**Status: the store exists, it's empty, and nothing tests recall.**

- `knowledge.ingest`, `knowledge.search`, `knowledge.extract`,
  `knowledge.status` — all implemented. LanceDB backend with
  sentence-transformers embeddings, fallback to filesystem listing
  when dependencies are missing.
- `me_knowledge/notes/` ships as placeholders only. Real content is
  gitignored by design (users build their own corpus).
- `.claude/rules/study-policy.md` says: *"Learning cycle (mandatory):
  write findings to `me_knowledge/notes/<part_type>_study_<date>.md`.
  `knowledge.ingest(path=…)` to index findings for future sessions."*
  **No automated test confirms this actually happens.**
- `tests/test_knowledge.py` has 4 tests, all exercising path
  construction. Zero tests exercise ingest → close session → new
  session → `knowledge.search` → recall.

Tool source: `server/tools_knowledge.py`, `server/knowledge_store.py`.

**What would move this from ◐ to ✓:**

1. **Seed content.** Commit a small corpus of real past-session
   findings under `me_knowledge/notes/` (maybe 5–10 short notes from
   the hexapod / gearbox / watch movement sessions that already
   happened). Gives `knowledge.search` something to return on day one,
   and lets contributors see the expected format.
2. **Persistence test.** Add a test that ingests a note, closes the
   knowledge volume, reopens it in a fresh process, and asserts that
   `knowledge.search` returns the ingested content. Without this test
   the whole LEARN stage could silently break and nobody would notice.
3. **Automatic study-result ingestion.** When `study.results` completes
   a parametric sweep with a clear winner, write the finding to
   `me_knowledge/notes/<part>_study_<date>.md` and `knowledge.ingest`
   it automatically. The rules say to do it; the code should enforce
   it.

### What "autonomous on a part class" would mean

Everything above is piecewise. The holistic question is:
*how do we know the loop has actually closed on, say, hexapod legs?*

Propose a three-test bar. On a given part class, all three must pass
before we claim the loop is autonomous on that class:

1. **Loop-closure test** (`tests/test_iteration_loop_e2e.py`, scaffolded
   in this commit as a skipped placeholder).
   - Build a deliberately under-dimensioned geometry
   - Run `analysis.stress_check`; assert FoS < 1.5
   - Call the DECIDE stage; assert it returns at least one candidate
   - Execute the top candidate via ACT
   - Re-run `analysis.stress_check`; assert FoS ≥ 1.5
   - No human input between steps 2 and 5.

2. **Regression-recovery test.** Start from a known-good design in
   `me_knowledge/notes/`, apply a destructive edit (e.g. remove a
   fillet), and assert the loop recovers to the original FoS within
   N ≤ 5 iterations. This tests that DECIDE actually uses LEARN.

3. **Cross-session memory test.** Run the loop-closure test twice in
   sequence. Assert that the second run retrieves the finding from the
   first run's `knowledge.ingest` and either reaches the fix faster
   or cites the prior finding in its reasoning trail.

These three tests don't exist yet. The first one is scaffolded as a
skipped placeholder at `tests/test_iteration_loop_e2e.py` so the
structural TODO is visible in CI output. The others should follow as
each dependency (DECIDE, LEARN persistence) lands.

## Honest verdict

**Tools: ~70% of the way there.** BUILD, OBSERVE, and SIMULATE are
genuinely strong. DIAGNOSE has the right shape but underfilled payload.
DECIDE, ACT, and LEARN are thin.

**Tests: ~50% of the way there.** Plenty of unit and stub-integration
coverage. Zero loop-closure tests. Zero cross-session learning tests.
The project tests (`tests/test_project_hexapod_leg.py` and siblings)
walk the pipeline but never iterate.

**Progress toward the vision: real but uneven.** The expensive
substrate — FreeCAD automation, three sim backends, FEA coupling, RL
training — is built and working. The missing pieces are smaller in code
volume but harder in design: turning raw sim output into structured
"root cause + fix candidate" data, closing the loop in a test, and
actually *using* the knowledge store to avoid repeating mistakes.

Those three changes would flip the thesis from *"we have the pieces"*
to *"the loop empirically closes on these part classes."* That's the
real target.

## Pick one: the highest-leverage first move

If you can only do one thing next to push the vision forward, do this:

**Add `FailureMode` enum to `AnalysisCheck` and have `analysis.stress_check`
populate it on every FAIL.** It's a tiny code change (one enum, one
dispatch branch in each stress-check path), but it's the wedge that
makes DECIDE possible, which makes ACT automatable, which makes the
loop-closure test writable, which makes LEARN worth storing. Everything
downstream is blocked on having a typed failure mode to dispatch on.

Once that's in place, the first worked end-to-end iteration transcript
becomes the highest-leverage contribution — see the README's
"How to push it closer" section for the rest of the list.
