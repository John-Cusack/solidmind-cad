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

## Where the loop model comes from

The iteration loop below is a **nine-step model**. Six of the nine steps map
directly onto textbook mechanical engineering design processes. The other
three are senior-engineer folklore — habits that experienced MEs apply by
reflex but that aren't described at this granularity in any single
canonical text.

The canonical textbooks agree on the *macro phases* of mechanical design —
Pahl & Beitz call them Task Clarification → Conceptual Design →
Embodiment Design → Detail Design; Shigley calls them Identification of
Need → Definition → Synthesis → Analysis & Optimization → Evaluation →
Presentation; Ullman breaks each phase into a Generate → Evaluate →
Decide triplet; Dieter & Schmidt lay out an eight-step prescriptive
process. They all acknowledge that iteration is fundamental and that
activities "often need to be revisited several times as new information
becomes available" ([Wikipedia][wiki-edp]). But none of them spell out
the *inner* loop an engineer runs when they have a part in front of them
and need to make it work.

That inner loop — the micro-cycle inside Embodiment and Detail Design —
is what this document formalizes. We borrow the vocabulary from the
textbook macro phases where we can, and label the folklore steps
honestly where we can't.

### The nine steps, with pedigree

| # | Step | Nearest canonical equivalent | Kind |
|---|---|---|---|
| 1 | **Specify** | Pahl & Beitz *Task Clarification*; Shigley *Definition of Problem*; Dieter Ch. 5 | Textbook |
| 2 | **Synthesize** | Ullman *Generate*; Shigley *Synthesis*; Dieter Ch. 6 | Textbook |
| 3 | **Reflect** | — (senior-engineer folklore; implicit in FMEA practice) | Folklore |
| 4 | **Screen** | Implicit in Shigley *Analysis* (hand calc / SCF lookup subset); Dieter Ch. 8 embodiment analysis | Textbook (under-emphasized) |
| 5 | **Simulate** | Shigley *Analysis & Optimization* (FEA subset) | Textbook |
| 6 | **Interpret** | Ullman *Evaluate*; Shigley *Evaluation*; Dieter Ch. 7 *Decision Making and Concept Selection* | Textbook |
| 7 | **Decide** | Ullman *Decide*; Dieter Ch. 7 | Textbook |
| 8 | **Act** | Back into step 2 (Synthesize) — textbooks model this as "return to Synthesis" | Textbook (via return arrow) |
| 9 | **Learn** | — (senior-engineer folklore; closest formal analog is Design-for-Six-Sigma *DMADV Verify* feedback) | Folklore |

**Observation is not a stage.** Watching what happened — reading
screenshots, measuring dimensions, querying topology — isn't a separate
step in any textbook model, and it shouldn't be one here. Observation is
the cross-cutting capability that every step depends on. SolidMind CAD
has it well covered (`cad.screenshot`, `cad.get_body_topology`,
`cad.get_dimensions`, `face_map`, etc.) and it's a background assumption
in everything that follows.

**The folklore steps are the interesting ones.** Steps 3 (Reflect) and 9
(Learn) are what separates a senior engineer from a student who knows
how to push the FEA button. The textbooks assume these happen; the
textbooks don't teach them as discrete actions. Codifying them in tools
and data structures — so the LLM is *forced* to do them instead of
skipping them — is the main architectural bet of this roadmap.

## The loop, step by step

### 1. Specify ✓

**Status: covered.** Task clarification, requirements capture, interface
definition.

- `design.save_brief`, `design.add_part`, `design.add_interface`,
  `design.update_brief` — phased brief pipeline with intent → sizing →
  layout → build gates
- `spec.select_schema`, `spec.next_question`, `spec.apply_answer`,
  `spec.validate`, `spec.finalize` — interview-driven maturity-level
  spec refinement

**Textbook anchor:** Pahl & Beitz *Task Clarification*; Shigley
*Definition of Problem*; Dieter Ch. 5. All three canonical models put
this step first and treat it as the foundation everything else rests on.

**Tool source:** `server/tools_design.py`, `server/tools_spec.py`.

**Test coverage:** `tests/test_tools_design.py` (61 tests),
`tests/test_tools_spec.py`, `tests/test_interface_freeze.py` covering
phase transitions and interface locking.

**What would move this forward:** nothing urgent. Specify is well-served
today.

### 2. Synthesize ✓

**Status: dense.** Over 40 tools drive FreeCAD PartDesign directly.

- `cad.*` primitives: `new_document`, `new_body`, `sketch`, `pad`,
  `pocket`, `hole`, `revolution`, `sweep`, `loft`, `helix`, `fillet`,
  `chamfer`, `draft`, `thickness`, `mirror`, `linear_pattern`,
  `polar_pattern`
- 23 parametric generators in `geometry.*` — spur/worm/bevel/planetary
  gears, involute profiles, helical springs, cam profiles, four-bar
  linkages, thread profiles, propeller blades, keyways, O-ring grooves…

**Textbook anchor:** Ullman *Generate* (inside every phase); Shigley
*Synthesis*; Dieter Ch. 6 *Concept Generation*.

**Tool source:** `freecad_addon/commands.py`, `server/tools_cad.py`,
`server/tools_geometry.py`.

**Test coverage:** `tests/test_tools_cad.py` (144 test methods),
`tests/test_geometry.py` (162 tests), stub-mode integration in
`tests/test_e2e_cad_flow.py`.

**What would move this forward:** nothing urgent. Synthesize is the
expensive substrate and it's done.

### 3. Reflect ✗ (folklore step, the first hard gap)

**Status: exists only as prompt rules. No tools. No structured data.
No tests.**

This is the "stop and think before you run the solver" step. What a
senior ME does by reflex:

1. *What kind of part is this, and what are its characteristic failure
   modes?* For a hexapod hip bracket under walking torque: yield at the
   fillet, fatigue at the fillet over millions of cycles, deflection if
   it's a stiffness element, fastener pullout.
2. *What do I expect the analysis to show?* "Max stress should be at
   the fillet, maybe 1.5× nominal, so around 60 MPa. If I see something
   very different, the mesh is bad or my mental model is wrong."
3. *Do I actually need a simulation, or will an analytical screen
   suffice?* Most routine parts don't need FEA. A hand calc plus an
   SCF handbook lookup resolves 70% of routine design questions.
4. *If I do simulate, what's the right load case and BCs?* Where's
   the fixity, where's the load, static or cyclic, worst case or
   nominal?

None of this lives in code today. It lives in `.claude/rules/analysis-policy.md`
as prompt advice: *"Skip for simple geometry. Trigger for load-bearing /
aero-critical / specialized parts."* The model is expected to remember
to consult the rule. In a long session under context pressure, that's
fragile.

**Textbook anchor:** none at this granularity. The macro phase models
assume reflection happens inside every phase but don't describe it.
The closest formal practice is **FMEA** (Failure Mode and Effects
Analysis), which prescribes cataloging failure modes before testing —
but FMEA is a heavyweight deliverable, not a pre-check habit.

**Current tool surface:**

- `.claude/rules/analysis-policy.md`, `.claude/rules/me-preflight.md`,
  `.claude/rules/self-assessment.md` — prompt-level rules, no
  enforcement
- `me.validate_constraints` — comes close; it's a pre-simulation
  constraint check, but it's static ("did you specify a yield?") not
  failure-mode-driven ("for this part class, are you worried about
  stress concentration at fillets?")

**What would move this from ✗ to ◐:**

1. **Part-class failure-mode taxonomies.** A small structured catalog
   per common part class: hexapod leg → `[fillet_stress_concentration,
   femur_buckling, tibia_tip_deflection, knee_fatigue]`. Starts with
   hand-curated entries for the part classes that already have project
   tests (hexapod leg, planetary gearbox, quadrotor, rc car). Lives
   under something like `server/failure_modes.py` or
   `me_knowledge/failure_modes/<part_class>.yaml`.
2. **An expectations schema.** A `ReflectExpectations` dataclass the
   LLM fills in before calling `analysis.*`: what failure modes are
   being checked, what numeric ranges are expected, what would make
   the result suspicious. The analysis tools require it as an argument
   (or warn loudly if absent).
3. **An analytical-screen-first guard.** Before `analysis.stress_check`
   runs FEA, call a cheap pre-screen: "for this geometry, what does a
   hand calc give? what does the SCF table say?" If the screen
   resolves the question with clear margin, skip the FEA entirely and
   return the screen result. This is `study.*` in miniature — a
   one-shot analytical check, not a sweep.
4. **Tests.** Given a geometry in a known part class, assert that the
   pre-sim Reflect step produces the expected failure-mode list and
   expectation ranges. Given a manifestly unnecessary FEA request
   (trivial bracket, nominal stress << yield), assert that the guard
   skips the solver and returns the analytical result.

### 4. Screen ◐

**Status: primitive.** Some static constraint checks exist; no
systematic analytical screen.

A *screen* is a cheap analytical check that resolves a design question
without running a solver. Examples: bending-moment hand calc, stress
concentration factor lookup from a handbook, first-principles
thermal-rise estimate, Euler buckling bound. They're fast, cheap, and
correct on routine problems — which is most problems.

Today:

- `me.validate_constraints` — static constraint checks against a
  declared constraint dict. Closer to a spec validator than an
  analytical screen.
- `me.design_loop` — orchestrates validation and risk gates but again
  against a constraint dict.
- `study.*` can be used as a bounded screen, but it's oriented at
  optimization sweeps, not one-shot analytical checks.

**Textbook anchor:** Shigley treats analytical methods as a subset of
"Analysis & Optimization" (Shigley, chapter sections on load analysis,
stress analysis, deflection) but doesn't separate "hand calc first,
solver if needed." Dieter Ch. 8 (Embodiment Design) discusses analysis
at this level but in prose, not as a discrete workflow step.

**Tool source:** `server/tools_me.py`, `server/me_orchestrator.py`.

**Test coverage:** `tests/test_tools_me.py` (~20 tests, static
constraint validation).

**What would move this from ◐ to ✓:**

1. **A `screen.*` tool group** that exposes cheap analytical checks as
   first-class MCP tools. Starting set: beam bending (rectangular /
   circular cross-section), stress concentration lookup for common
   features (fillet, hole, notch), deflection bound via beam theory,
   critical buckling load for columns, fastener preload and pullout
   estimates.
2. **Screen results that feed directly into the Reflect expectations
   schema.** When Screen says "nominal stress is 40 MPa, expected SCF
   at fillet is 2.3, so peak around 92 MPa" the Interpret step can
   compare that prediction against the FEA output and flag the
   solver setup if they disagree by more than the expected tolerance.
3. **Tests.** Known-good hand-calc reference cases (bending of a
   cantilever, Hertzian contact, thermal expansion of a rod) with
   assertions against textbook values.

### 5. Simulate ✓

**Status: strong.** Three dynamic backends, three analysis domains,
full tier ladder. This is the "press the solver button" step.

- **Motion tiers:** Tier 1 analytical (`motion.validate`,
  `motion.check_gear_train`, `motion.propagate_motion`), Tier 2 kinematic
  in FreeCAD Assembly (`motion.create_assembly`, `motion.drive_joint`,
  `motion.check_interference`), Tier 3 dynamic (`motion.simulate` with
  `backend={isaac,gazebo,chrono}`)
- **Field analysis:** `analysis.stress_check` (CalculiX),
  `analysis.thermal_check`, `analysis.aero_check` (SU2 / DUST)
- **Coupling:** `analysis.stress_from_simulation` pipes peak joint forces
  from `motion.simulate` into a CalculiX stress check — the Tier 3.5
  flow, the existing proof that "sim output → analysis input" is
  already a path the LLM can take
- **Engine lifecycle:** `sim.start_engine`, `sim.stop_engine`,
  `sim.engine_status` manage real backends as subprocesses
- **RL:** `rl.configure_environment`, `rl.start_training`,
  `rl.monitor_training` run the Isaac Lab + RSL-RL pipeline

**Textbook anchor:** Shigley *Analysis & Optimization* (with FEA as a
tool within it); Dieter Ch. 8. Note that Shigley doesn't single out
simulation — he treats it as one analytical technique among many. That
framing is correct and matches the Screen-before-Simulate ordering
above.

**Tool source:** `server/tools_motion.py`, `server/tools_analysis.py`,
`server/tools_sim.py`, `server/tools_rl.py`, `isaac_bridge/`,
`gazebo_bridge/`, `chrono_daemon/`.

**Test coverage:** `tests/test_tools_motion.py`,
`tests/test_motion_validators.py`, `tests/test_full_pipeline_e2e.py`
(10 test methods exercising Tier 1 → Tier 3.5), plus per-backend tests
gated behind `requires_chrono` / `requires_elmer` / `requires_isaac`
decorators.

**What would move this forward:** see Interpret. The solvers run fine;
the problem is that their *output* isn't shaped for the LLM to reason
against failure-mode expectations.

### 6. Interpret ◐ (the second hard gap)

**Status: structured data exists, but it isn't typed against failure
modes and isn't compared against Reflect-step expectations.**

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

Motion validators in `server/motion_validators.py` return similarly
structured `errors` / `warnings` / `notes` lists.

The bad news:

1. **`name` is a free-form string.** There is no `FailureMode` enum like
   `STRESS_CONCENTRATION` / `BUCKLING` / `YIELD` / `FATIGUE` / `CONTACT` /
   `DEFLECTION` / `RESONANCE` / `THERMAL` / `WEAR` / `CORROSION`. The
   LLM has to infer the mode from the string, and so does any downstream
   tool that would want to dispatch on it.
2. **`suggestion` is a generic string.** "Add fillet" tells the LLM
   *something* is wrong at `face_group="Face7"` but not what size, not
   which direction, not what tradeoff. Compare the ideal: *"Stress
   concentration at Face7 under 12 N·m hip load. Candidates: add 0.5 mm
   fillet (cheapest), widen section by 2 mm (better margin), switch to
   7075-T6 (weight cost)."*
3. **Nothing connects the result to the Reflect-step expectations.**
   The LLM wrote down "I expect max stress at the fillet around 60 MPa"
   before running the solver. The solver returned "max stress 287 MPa at
   Face7." Today there's no tool that takes those two inputs and says
   "that matches the expected location but is 4.8× higher than
   expected — either the load case is wrong, the mesh is bad, or the
   part is dramatically undersized." This is the comparison step the
   textbooks call *Evaluation* (Ullman, Shigley) and it's the most
   important cognitive action in the whole loop.

**Textbook anchor:** Ullman *Evaluate* (explicitly separates "I ran the
analysis" from "I judged the result"); Shigley *Evaluation*; Dieter
Ch. 7 *Decision Making and Concept Selection*. All three texts are
clear that this is a distinct step from running the analysis. The
codebase conflates them.

**Test coverage:** `tests/test_analysis_models.py` round-trips
`AnalysisCheck` instances. No test asserts that an LLM (or a programmatic
harness) consumes a result + expectations and produces a typed
diagnosis.

**What would move this from ◐ to ✓:**

1. **Add `FailureMode` enum to `AnalysisCheck`.** A small, enumerated
   vocabulary — maybe a dozen values covering the common structural,
   motion, thermal, and fluid failure modes. This is the single most
   important change in the whole roadmap because everything downstream
   (Decide, Act, Learn, the three-test bar) has to dispatch on *some*
   typed value and right now there isn't one.
2. **Add `candidates: list[FixCandidate]` alongside `suggestion`.** Each
   candidate is a structured `{tool, args, estimated_improvement,
   cost_note}` so the Decide step picks from options instead of
   translating a sentence.
3. **Add an `interpret.compare_to_expectations` tool.** Takes a
   `FieldResult` and a `ReflectExpectations` and returns a typed
   comparison: did the hot-spot land where expected, is the magnitude
   in the expected range, what's the gap, is the gap within mesh
   tolerance or is something fundamentally wrong? This is the bridge
   between Reflect and Interpret.
4. **Test the parse→act path.** Give the test harness a known-bad
   geometry, call `analysis.stress_check` with filed expectations,
   assert that the returned `FailureMode` and `candidates` list are
   what a subsequent `cad.*` call would consume verbatim.

### 7. Decide ✗ (the third hard gap)

**Status: bare-bones.** Nothing in the current toolset actually *chooses
a fix*. What exists:

- `me.validate_constraints` / `me.apply_risk_gates` /
  `me.build_traceability` / `me.design_loop` — great for static
  constraint checking and "is this design ready for release" gating,
  but they operate on a constraint-dict model, not on a failure →
  remediation flow.
- `study.*` — parametric sweeps. This is the closest thing to "decide"
  we have, because a study over fillet radius ∈ [0.5, 5.0] mm will find
  the smallest radius that satisfies FoS ≥ 1.5. But the LLM has to
  already know the right variable to sweep. There is no tool that
  converts *"stress concentration at Face7"* into *"run a bounded
  study on fillet radius over the edge set of Face7, objective:
  minimize max stress, budget: 8 samples."*

**Textbook anchor:** Ullman *Decide* (makes this a first-class step in
every phase); Dieter Ch. 7 *Decision Making and Concept Selection*. The
distinction Ullman draws between Evaluate and Decide is exactly the
distinction between "I know what's wrong" and "I've picked which fix to
try" — and it's the distinction our current tooling elides.

**Tool source:** `server/tools_me.py`, `server/tools_study.py`.

**Test coverage:** `tests/test_tools_me.py` (static constraint
validation), `tests/test_tools_study.py` and `tests/test_study_*.py`
(mock solvers).

**What would move this from ✗ to ◐:**

1. **A `decide.from_failure` tool** that takes an `AnalysisCheck` with
   a `FailureMode` and returns a ranked list of candidate actions —
   each action being a concrete tool call the LLM can execute. Starting
   surface: the five most common mechanical failure modes (stress
   concentration, yield/bulk overload, buckling, deflection, fatigue),
   each with one or two canned fix strategies tied to the specific
   part-class taxonomies from Reflect.
2. **Auto-study fallback.** When the decider doesn't have a canned
   strategy, fall through to "run a bounded `study.*` on the nearest
   geometric variable." This turns Decide into "pick from the playbook,
   and if no play fits, run a sweep."
3. **Safety-factor budget awareness.** The decider should know the
   target FoS from the Specify-step brief, so it stops iterating once
   the margin is comfortably above threshold instead of chasing
   asymptotes.

### 8. Act ◐

**Status: the tools exist, but they're the same tools as Synthesize.**

There's no dedicated "apply this fix" layer. The LLM takes a Decide
output and calls `cad.fillet(radius=0.5, face="Face7")` directly.
That's fine today, but it means:

- Every fix action is a fresh tool call with no memory of why it was
  called. A failed fix doesn't tell the decider "tried that, didn't
  work."
- The fix history for a single design session lives only in
  conversation context, not in structured form.

**Textbook anchor:** the canonical phase models treat Act as "return to
Synthesis," which is implicitly what happens here — the LLM goes back
to the Synthesize toolset. But the textbooks don't model the fix-log
explicitly, which is a gap at the loop level regardless of what the
textbooks say.

**What would move this from ◐ to ✓:**

1. **Structured fix log.** Record every
   `(failure, expectations, chosen_action, result)` tuple during a
   session. Append to the current `design.*` brief under a new
   `iterations` key.
2. **Loop-aware dispatch.** A thin wrapper that, when called with a
   candidate action from Decide, executes the action, re-runs the
   same Screen (or Simulate) that found the failure, and returns the
   before/after delta as one atomic result. This collapses
   "build → re-analyze → compare" into a single call.

### 9. Learn ◐ (folklore step, infrastructure only)

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

**Textbook anchor:** none at this granularity. The canonical phase
models don't describe cross-session learning; they assume the
organization's design review system captures it out-of-band. The
closest formal analog is the *Verify* step in Design for Six Sigma's
DMADV cycle, which feeds results back into the organization's design
knowledge. For an autonomous agent running many sessions against the
same part classes, learning has to be *in* the loop, not out-of-band.

**Tool source:** `server/tools_knowledge.py`, `server/knowledge_store.py`.

**What would move this from ◐ to ✓:**

1. **Seed content.** Commit a small corpus of real past-session
   findings under `me_knowledge/notes/` (5–10 short notes from the
   hexapod / gearbox / watch movement sessions that already happened).
   Gives `knowledge.search` something to return on day one and lets
   contributors see the expected format.
2. **Persistence test.** Add a test that ingests a note, closes the
   knowledge volume, reopens it in a fresh process, and asserts that
   `knowledge.search` returns the ingested content. Without this test
   the whole Learn step could silently break and nobody would notice.
3. **Automatic finding ingestion.** When an Iterate cycle closes with
   a successful fix, auto-write the triple `(failure_mode, part_class,
   winning_fix)` to `me_knowledge/notes/<part_class>_<date>.md` and
   `knowledge.ingest` it. The rules say to do it; the code should
   enforce it.
4. **Reflect-step integration.** When the Reflect step asks "what
   failure modes should I watch for on a hexapod leg?", it should
   consult the part-class taxonomy *and* `knowledge.search` for prior
   findings on the same part class. This closes the Learn → Reflect
   feedback loop.

## What "autonomous on a part class" would mean

Everything above is piecewise. The holistic question is: *how do we
know the loop has actually closed on, say, hexapod legs?*

Propose a three-test bar. On a given part class, all three must pass
before we claim the loop is autonomous on that class:

1. **Loop-closure test** (`tests/test_iteration_loop_e2e.py`, scaffolded
   as a skipped placeholder).
   - Specify the brief and target FoS
   - Synthesize a deliberately under-dimensioned geometry
   - Reflect: produce the part-class failure-mode list and expected
     numeric ranges
   - Screen: analytical first-pass — should flag the issue without FEA
     if the geometry is badly undersized
   - Simulate (if Screen didn't resolve it)
   - Interpret: typed `FailureMode` + comparison against Reflect
     expectations
   - Decide: pick a fix from the ranked candidate list
   - Act: apply the fix and re-check
   - Assert final FoS ≥ 1.5 with no human input between steps 3 and
     "apply the fix"

2. **Regression-recovery test.** Start from a known-good design in
   `me_knowledge/notes/`, apply a destructive edit (e.g. remove a
   fillet), and assert the loop recovers to the original FoS within
   N ≤ 5 iterations. This tests that Decide actually uses Learn.

3. **Cross-session memory test.** Run the loop-closure test twice in
   sequence. Assert that the second run retrieves the finding from the
   first run's `knowledge.ingest` via the Reflect step, and either
   reaches the fix faster or cites the prior finding in its reasoning
   trail.

These three tests don't exist yet. The first one is scaffolded as a
skipped placeholder at `tests/test_iteration_loop_e2e.py` so the
structural TODO is visible in CI output. The others should follow as
each dependency (Reflect taxonomy, FailureMode enum, Decide tool,
Learn persistence) lands.

## Honest verdict

**Tools: the textbook steps are solid, the folklore steps are missing.**

- Steps 1, 2, 5 (Specify, Synthesize, Simulate) — the capital-T Textbook
  steps with strong canonical anchors — are dense/strong and well-tested.
- Step 6 (Interpret) — also textbook, but the current tooling conflates
  it with Simulate. The data shape is there (`AnalysisCheck` is
  well-designed); what's missing is a typed `FailureMode` vocabulary
  and the comparison-to-expectations step.
- Steps 3, 9 (Reflect, Learn) — the folklore steps — are ✗ and ◐
  respectively. Nothing in the textbooks forces their existence, which
  is precisely why they're missing. The LLM will skip them unless the
  tooling makes them unskippable.
- Steps 4, 7, 8 (Screen, Decide, Act) — all ◐. Each has partial
  coverage from existing tools but none is a first-class loop step.

**Tests: ~50% of the way there.** Plenty of unit and stub-integration
coverage. Zero loop-closure tests. Zero cross-session learning tests.
The project tests (`tests/test_project_hexapod_leg.py` and siblings)
walk the pipeline but never iterate.

**Progress toward the vision: real but uneven.** The expensive
substrate — FreeCAD automation, three sim backends, FEA coupling, RL
training — is built and working. The missing pieces are smaller in code
volume but harder in design: forcing the Reflect step to happen,
typing the Interpret step against failure modes, and actually *using*
the Learn step to inform the next Reflect. Those changes would flip
the thesis from *"we have the pieces"* to *"the loop empirically
closes on these part classes."*

## The highest-leverage first move — paired tools

The old version of this roadmap named a single change: *"Add
`FailureMode` enum to `AnalysisCheck`."* That's still necessary, but
it's not sufficient. The right first move is a **pair** of changes
that land together:

1. **`FailureMode` enum on `AnalysisCheck`.** Ten or twelve enumerated
   values (`STRESS_CONCENTRATION`, `YIELD`, `BUCKLING`, `FATIGUE`,
   `DEFLECTION`, `CONTACT`, `RESONANCE`, `THERMAL`, `WEAR`,
   `CORROSION`). Every `analysis.*` tool populates it on FAIL. This
   unblocks Interpret, Decide, and the loop-closure test.
2. **`ReflectExpectations` dataclass and an enforcement point.** A
   structured record of "what failure modes am I checking, what do I
   expect the result to look like, what would make it suspicious."
   The `analysis.*` tools accept it as an optional argument on day
   one, warn loudly if it's absent, and make it required in the
   loop-closure test harness. This unblocks Reflect.

The two are paired because neither one is fully useful without the
other. `FailureMode` without expectations lets the LLM say "it's a
stress concentration" but not "I was expecting a stress concentration
here and it's 4× bigger than I thought." Expectations without
`FailureMode` lets the LLM file a prediction but not compare it
against a typed result.

Together, these two changes are the wedge that makes every downstream
step possible: Decide dispatches on `FailureMode`, Act logs the
`(expectations, failure, chosen_action, result)` tuple, Learn
indexes by failure mode and part class so Reflect can retrieve prior
findings, and the loop-closure test finally has a typed assertion it
can make.

After these land, the first worked end-to-end iteration transcript
— see the README's "How to push it closer" section — becomes the
highest-leverage contribution.

## Sources

Canonical texts and references behind the nine-step model:

- [Engineering design process — Wikipedia][wiki-edp]
- [Shigley's Mechanical Engineering Design, 10th ed. (full text PDF)](https://dl.icdst.org/pdfs/files3/ad7608c18e740b0e402c025fa3187de8.pdf)
- [Engineering Design: A Systematic Approach — Pahl, Beitz, Feldhusen, Grote](https://www.amazon.com/Engineering-Design-Systematic-Gerhard-Pahl/dp/1447160250)
- [The Mechanical Design Process, 6th ed. — David Ullman](https://www.davidullman.com/mechanical-design-process-6ed)
- [Engineering Design — George Dieter & Linda Schmidt (McGraw-Hill)](https://www.mheducation.com/highered/product/Engineering-Design-Dieter.html)
- [A Review of the Fundamentals of Systematic Engineering Design Process Models — Design Society](https://www.designsociety.org/download-publication/26782/a_review_of_the_fundamentals_of_systematic_engineering_design_process_models)
- [Modelling Iteration in Engineering Design — Design Society](https://www.designsociety.org/download-publication/25679/Modelling+Iteration+in+Engineering+Design)
- [A Taxonomy for Mechanical Design — Research in Engineering Design](https://link.springer.com/article/10.1007/BF01580519)
- [Design of Mechanical Systems: Iterative Process — McGill Engineering Design](https://www.mcgill.ca/engineeringdesign/step-step-design-process/design-phases-mechanical-engineering/design-mechanical-systems-iterative-process)

The Reflect and Learn steps are *folklore* — they come from lab
courses, senior-engineer mentoring, FMEA practice, and the Verify step
of Design for Six Sigma (DMADV), not from any single canonical ME
textbook.

[wiki-edp]: https://en.wikipedia.org/wiki/Engineering_design_process
