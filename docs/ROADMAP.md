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

## Status refresh — 2026-06-26

Since the previous draft, tickets B/C/D/E landed and materially changed the
inner-loop picture. The headline: **the inner loop now closes end-to-end on its
first part class.** The foam-dart launcher's `latch_sear` walks all nine steps —
build a deliberately under-dimensioned latch → screen → typed `FailureMode`
diagnosis → pick a fix → apply → re-screen → assert improvement — with **no human
between diagnosis and re-check** (`tests/test_iteration_loop_foam_dart_e2e.py`,
plus the worked example under `examples/foam_dart_spring_launcher/`).

What landed, mapped to the priority stack and the step list:

- **Move 1 (partial):** `analysis.screen_stress` is built (`server/screen_stress.py`,
  `analysis.screen_stress` tool) — beam bending, SCF lookup, Euler buckling,
  returning a typed `AnalysisCheck`. **Not yet:** `screen_thermal` / `screen_aero`,
  and FEA is **not auto-gated** behind the screen (the two tools exist; escalation
  is still manual/documented, not wired inside `analysis.stress_check`).
- **Move 2 (done):** `FailureMode` enum + `ReflectExpectations` dataclass in
  `server/analysis_models.py`; `FieldResult` gained a `candidates` field.
- **Interpret (Step 6):** `decide.interpret` (`interpret_compare_to_expectations`,
  returns a typed `Comparison`) bridges Reflect expectations to results.
- **Decide micro (Step 7):** `decide.from_failure` turns a typed `AnalysisCheck`
  into a `FixProposal` (op / target / param / delta / rationale).
- **Learn (Step 9):** the persistence test landed
  (`tests/test_knowledge_persistence_e2e.py` — ingest → fresh process → recall);
  the foam-dart example auto-ingests its V2 finding.

Still open (tracked in the step sections and priority stack below): the
`screen_thermal` / `screen_aero` tiers and auto-gating FEA behind the screen; the
`part_class` Specify field; a **shared** failure-mode taxonomy under
`me_knowledge/failure_modes/` (today only the foam-dart example ships its own
`failure_modes.yaml`); structured `FixCandidate` objects (current `candidates`
are string labels); and the regression-recovery + cross-session-memory tests
(bar items 2 and 3). Inner-loop status moves **✗ → ◐: closed on one part class,
generalization to more classes pending.**

**Addendum — 2026-06-27 (foam-dart FEA hardening):** the example's Simulate
rung now drives a real CalculiX **mesh-convergence study** on the enriched
latch — V2 (filleted) converges and confirms the analytical screen (±25%); V1
(sharp) diverges (a singularity) and is rejected by the screen. This replaced a
misleading "FEA reproduces the SCF" claim. Running the real solver also exposed
that the project has **two duplicated FEA cores**, one of which
(`orchestrator.fea.run_l2_fea`) is broken against CalculiX 2.21. New **Move 4**
in the priority stack tracks unifying them on one convergence-aware core, a
real-backend CI lane, the clamp-singularity sub-model, and the latch load case.

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

## Two loops, not one

This repo has **two design loops**, and they operate at different scales.
Both are needed for the thesis to hold.

**The outer loop — orchestrator / SBCE / gate flow** — parallelizes
*across* subsystems and ranks *whole designs* against objectives. It's
implemented under `orchestrator/`:

- `orchestrator/spec.py`, `orchestrator/state.py`, `orchestrator/runner.py` —
  data model, state machine, and top-level API for a multi-worker run
- `orchestrator/normalizer.py`, `orchestrator/council.py`,
  `orchestrator/skeleton.py`, `orchestrator/interface_freeze.py` —
  Stages 0–3 (normalize goals → decompose → layout → freeze interfaces)
- `orchestrator/worker.py`, `orchestrator/worker_subprocess.py`,
  `orchestrator/worker_entry.py` — parallel worker dispatch via
  Claude Code's `Agent` tool, `claude --print` subprocess, or Docker
  containers (three execution modes)
- `orchestrator/validator.py`, `orchestrator/scorer.py`,
  `orchestrator/sbce.py` — Stages 5–6 (validate against frozen contracts,
  score candidates, run Set-Based Concurrent Engineering beam search)
- `orchestrator/release.py` — Stage 7 release packaging (BOM, ICDs,
  provenance)
- Gate flow **G0 → G7** with human approval between major transitions

Test coverage is substantial — ~170 tests across 11 orchestrator-
focused test files: `test_runner.py` (41), `test_preflight.py` (24),
`test_sbce.py` (19), `test_council.py` (16), `test_interface_freeze.py` (15),
`test_skeleton.py` (14), `test_release.py` (14), `test_normalizer.py` (10),
`test_dsm.py` (9), `test_worker.py` (5), `test_orchestrator_cli.py` (3),
plus `test_orchestrator_e2e.py` for end-to-end gate walking.

**Status — outer loop: ✓ closed on 5 part classes.** The state machine,
gate checkers, SBCE scorer, DSM dependency analysis, release packaging,
and preflight validation are all real code with real tests — and as of
the chunk-5–9 wiring work, the loop is provably closed against real
FreeCAD builds for **five** part classes: `sun_gear`, `planet_carrier`,
`quadrotor_arm`, `rc_car_chassis`, and `hexapod_leg`. Each class has
a per-part-class builder under `orchestrator/worker_builds/` that drives
real geometry through the addon socket; the orchestrator then independently
re-imports the produced STEP file and re-measures interface dimensions
via `orchestrator/measure.py`. A drift-detection test
(`tests/test_orchestrator_drift_e2e.py`) deliberately stomps a worker's
claimed `bore_dia` and asserts the validator catches the lie via
`FailureCode.MEASUREMENT_DRIFT`. The legacy mocked path
(`test_orchestrator_e2e.py`) remains for trust-mode CI coverage.

**The inner loop — the nine steps described in this document** —
happens *inside* a single worker. Each worker is responsible for
building one subsystem against its frozen interface contract. The
inner loop is how that worker would iterate if it were autonomous:
Specify → Synthesize → Reflect → Screen → Simulate → Interpret →
Decide → Act → Learn.

**These loops are complementary, not competing.** A fully autonomous
system needs both:

| | Outer loop | Inner loop |
|---|---|---|
| Scale | Multi-subsystem, whole-design | Single part, iterative refinement |
| Concurrency | Parallel workers | Sequential iterations |
| Decide surface | "Which of these N candidate designs wins?" (SBCE) | "Which repair do I try first for this failing part?" (empty) |
| State | G0 → G7 gate walk | 9-step cycle |
| Status | ✓ closed on 5 part classes | ◐ closed on 1 part class (foam-dart latch); generalization pending |
| Tests | ~170 tests across 11 files | loop-closure test green on 1 class + supporting e2e tests |

The rest of this roadmap focuses on the **inner loop** because that's
still the less-built half — though as of the 2026-06-26 refresh it is no
longer "mostly missing": the nine steps run end-to-end on the foam-dart
latch class, and the remaining work is generalizing past that first class
(more taxonomies, the missing screen tiers, structured fix candidates).
The outer-side move the older drafts flagged — wiring a real worker build
into the gate flow — has since landed (Move 3, below).

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

**The one latent gap.** The brief stores requirements, constraints,
and interfaces — but **not a `part_class` field** that the Reflect
step can dispatch on (hexapod_leg, planetary_gearbox_housing,
quadrotor_arm, rc_car_chassis, …). Right now the LLM has to infer the
part class from the brief name and description, which is exactly the
kind of informal step that gets skipped under context pressure. Small
fix: add an optional `part_class: str` to `design.save_brief` and
`design.add_part`, and make it required for parts that Reflect will
look up in the failure-mode taxonomy. Without this, the Reflect → Learn
feedback loop can't retrieve part-class-specific findings cleanly.

**What would move this forward:** just the `part_class` field — **still open as
of 2026-06-26.** `part_class` now exists as a concept in the Reflect layer
(`ReflectExpectations.part_class`), but `design.save_brief` / `design.add_part`
don't yet carry it, so the brief can't dispatch taxonomy lookups by class. This
is now the gating item for generalizing Reflect past the one wired part class.
Specify is otherwise well-served today.

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

### 3. Reflect ◐ (folklore step — the wedge landed, taxonomy still per-example)

**Status (refreshed 2026-06-26): the structured substrate now exists.**
`ReflectExpectations` (item 2 below) is implemented in
`server/analysis_models.py` and the analysis tools accept it; the analytical
screen (item 3) exists as `analysis.screen_stress`. The foam-dart example files
expectations per part class before screening and the loop-closure test asserts
on them. What's still ✗: a **shared** part-class failure-mode taxonomy (item 1
below) — today only the foam-dart example ships its own `failure_modes.yaml`,
there's no `me_knowledge/failure_modes/<part_class>.yaml` catalog — and the
`part_class` dispatch field on the brief (see the Specify gap). Until those land,
Reflect is ◐, not ✓: it happens reliably for the one wired part class but isn't
yet generalizable by lookup.

The original ✗ writeup is kept below for context.

**Original status: exists only as prompt rules. No tools. No structured data.
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

**What would move this from ✗ to ◐ — progress as of 2026-06-26:**

1. **Part-class failure-mode taxonomies.** ✗ **still open (the main remaining
   gap).** A small structured catalog per common part class: hexapod leg →
   `[fillet_stress_concentration, femur_buckling, tibia_tip_deflection,
   knee_fatigue]`. The foam-dart example proves the *format* with its own
   `failure_modes.yaml`, but there is no shared
   `me_knowledge/failure_modes/<part_class>.yaml` catalog yet. Should start with
   hand-curated entries for the part classes that already have project tests
   (hexapod leg, planetary gearbox, quadrotor, rc car).
2. **An expectations schema.** ✓ **done.** `ReflectExpectations` is implemented
   in `server/analysis_models.py`; the analysis/decide tools consume it and the
   loop-closure test requires it.
3. **An analytical-screen-first guard.** ◐ **half done.** `analysis.screen_stress`
   exists and resolves the question hand-calc-style. The *automatic* guard inside
   `analysis.stress_check` (screen first, skip FEA on clear margin) is not yet
   wired — escalation is currently manual, as the foam-dart example demonstrates.
4. **Tests.** ◐ The foam-dart loop-closure test asserts the Reflect step's
   expectations against the screened result for one part class. A general
   "trivial bracket → guard skips the solver" assertion still wants the
   auto-gate from item 3.

### 4. Screen ◐ — the motion/analysis asymmetry

**Status: half-covered. Motion has a real tier ladder; analysis
doesn't.** This is the most important structural observation in the
roadmap and the first item on the priority stack below.

A *screen* is a cheap analytical check that resolves a design question
without running a solver. Examples: bending-moment hand calc, stress
concentration factor lookup from a handbook, first-principles
thermal-rise estimate, Euler buckling bound. They're fast, cheap, and
correct on routine problems — which is most problems. Running full
FEA on a spacer block is cargo-cult engineering.

**`motion.*` has the Screen step built as a first-class tier.**

- Tier 1 (analytical) — `motion.validate`,
  `motion.check_gear_train`, `motion.propagate_motion`,
  `motion.check_joint_connectivity`. Hand-calc-equivalent, runs in
  milliseconds, no solver required. Covers gear ratios, DOF count,
  Grashof criteria, speed/torque propagation, joint connectivity,
  power conservation.
- Tier 2 (kinematic) — `motion.create_assembly`,
  `motion.drive_joint`, `motion.check_interference`. Quasi-static in
  FreeCAD's Assembly workbench.
- Tier 3 (dynamic) — `motion.simulate` with `backend={isaac,gazebo,
  chrono}`.
- Tier 3.5 (coupled) — `analysis.stress_from_simulation`.

Plus the rule file (`.claude/rules/motion-validation.md`) that
specifies when to escalate each tier. This is a *proven in-repo
pattern* for how Screen → Simulate should look.

**`analysis.*` now has the first rung of a tier ladder (refreshed 2026-06-26).**

- `analysis.screen_stress` ✓ **landed** — `server/screen_stress.py`. It does
  exactly the "σ = Mc/I, handbook SCF at the fillet, expected peak, comfortably
  under yield, no FEA needed" reasoning the old draft asked for, and returns a
  typed `AnalysisCheck` so Interpret can cross-check it against FEA.
- `analysis.thermal_check` ✗ still goes straight to the thermal solver — no
  `screen_thermal`.
- `analysis.aero_check` ✗ still goes straight to SU2 / DUST — no `screen_aero`.
- The **auto-gate** (`analysis.stress_check` calls the screen first and skips FEA
  on clear margin) is ✗ not yet wired — `screen_stress` and `stress_check` are
  separate tools and escalation is manual.

So Screen stays ◐, but for a narrower reason than before: the structural screen
tier exists, and what remains is (a) the thermal/aero screen tiers and (b) wiring
the Tier-3-behind-Tier-1 auto-escalation. The fix is still **"copy motion.*'s
tier pattern into analysis.*"** — now one-third complete.

**Textbook anchor:** Shigley treats analytical methods as a subset of
"Analysis & Optimization" (Shigley, chapter sections on load analysis,
stress analysis, deflection) but doesn't separate "hand calc first,
solver if needed." Dieter Ch. 8 (Embodiment Design) discusses analysis
at this level but in prose, not as a discrete workflow step. The
textbooks implicitly assume engineers screen before simulating; they
don't model the escalation explicitly. Motion's tier ladder makes it
explicit.

**Tool source:** `server/tools_motion.py` (Tier 1 tools already done),
`server/motion_validators.py`, `server/tools_me.py`,
`server/me_orchestrator.py`.

**Test coverage:** `tests/test_tools_motion.py`,
`tests/test_motion_validators.py` on the motion side;
`tests/test_tools_me.py` (~20 tests of static constraint validation)
on the generic side. **Analytical stress screening is now covered**
(`tests/test_screen_stress.py`, added with `analysis.screen_stress`); thermal and
aero screens remain untested because those screen tiers don't exist yet.

**What would move this from ◐ to ✓ — progress as of 2026-06-26:**

1. **Bring `analysis.*` up to `motion.*`'s tier structure.** ◐ one of three:
   - `analysis.screen_stress` ✓ **done** — beam bending (rectangular / circular),
     stress concentration lookup (fillet, hole, notch), deflection bound, Euler
     buckling, fastener preload/pullout, via `geometry.section_properties`.
   - `analysis.screen_thermal` ✗ — lumped capacitance, Biot number, resistance
     networks. Not started.
   - `analysis.screen_aero` ✗ — BEMT for rotors, lift curve slope for wings. Not
     started.
2. **Gate Tier 3 behind Tier 1.** ✗ **still open.** `analysis.stress_check` does
   not yet call `analysis.screen_stress` first and short-circuit on clear margin;
   the two are separate tools. Mirror `.claude/rules/analysis-policy.md`.
3. **Return the same `AnalysisCheck` shape from both Screen and Simulate.** ✓
   **done** — `screen_stress` returns an `AnalysisCheck` and `stress_check`
   stamps a typed `failure_mode` on its `FieldResult`, so Interpret can compare
   the two. (The foam-dart example does exactly this screen-vs-FEA comparison.)
4. **Tests.** ◐ `tests/test_screen_stress.py` covers the analytical cases;
   thermal/aero screen reference cases follow when those tiers land.

This single change — analysis tiering — is arguably the highest-
leverage move in the whole roadmap because it **(a) makes Screen a
first-class step**, **(b) unblocks the Reflect step's "do I even
need to simulate?" decision**, **(c) dramatically speeds up
iteration** by keeping the solver out of obvious cases, **(d) gives
Interpret a second data point to compare against FEA output**, and
**(e) copies a proven in-repo pattern instead of inventing a new one.**
See the Priority stack at the bottom.

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
gated behind `requires_chrono` / `requires_elmer` / `requires_isaac` /
`requires_cholmod` / `requires_cudss` decorators.

**One caveat to the ✓.** The real-backend tests skip by default in CI
— they only run when the local environment has the binary built
(Chrono daemon) or the solver installed (CHOLMOD, cuDSS, Elmer, Isaac
Sim, Gazebo Harmonic). That's correct for CI hygiene but it means we
don't have continuous confidence that refactors don't silently break
real-mode execution paths. For a project whose thesis rests on
"simulation in the loop," it's worth noting that our day-to-day signal
on solver correctness comes from stub-mode integration tests, not from
real solver runs. Mitigation: a manual "real backends smoke test"
checklist in `docs/simulation-and-rl.md` plus GitHub Actions matrix
runs with one or two of the real backends installed via apt/conda.

**What would move this forward:** see Interpret. The solvers run fine;
the problem is that their *output* isn't shaped for the LLM to reason
against failure-mode expectations. Also see the Screen section — the
asymmetry between `motion.*` (has a tier ladder) and `analysis.*`
(doesn't) is the structural gap that matters most here.

### 6. Interpret ◐ → nearly ✓ (the second hard gap, mostly closed)

**Status (refreshed 2026-06-26): the two missing pieces landed.** `AnalysisCheck`
and `FieldResult` now carry a typed `failure_mode: FailureMode`, and
`decide.interpret` (`interpret_compare_to_expectations`) takes a `FieldResult` +
`ReflectExpectations` and returns a typed `Comparison` — exactly the
result-vs-expectation bridge this section asked for. What keeps it ◐ rather than
✓: `candidates` on `FieldResult` is still a tuple of **string labels**, not the
structured `FixCandidate {tool, args, estimated_improvement, cost_note}` objects
item 2 below describes. The original ◐ writeup is kept for context.

**Original status: structured data exists, but it isn't typed against failure
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

Motion validators in `server/motion_validators.py` return
`errors` / `warnings` / `notes` lists — free-form strings inside, but
**the category itself (error vs warning vs note) is typed**. That's one
rung above `AnalysisCheck`'s flat `status` field on the Interpret
maturation curve. Parallel to the Screen asymmetry above, **motion's
Interpret layer is further along than analysis's.** Both need
typed failure modes, but the motion side already has structured
severity categories and the analysis side doesn't.

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

**What would move this from ◐ to ✓ — progress as of 2026-06-26:**

1. **Add `FailureMode` enum to `AnalysisCheck`.** ✓ **done** — implemented on
   both `AnalysisCheck` and `FieldResult`, populated on FAIL across the analysis
   and screen tools.
2. **Add `candidates: list[FixCandidate]` alongside `suggestion`.** ◐ **partial**
   — `FieldResult.candidates` exists but holds string labels, not the structured
   `{tool, args, estimated_improvement, cost_note}` objects. This is the remaining
   work to flip Interpret to ✓ (and it pairs with the micro-Decide gap in Step 7).
3. **Add an `interpret.compare_to_expectations` tool.** ✓ **done** —
   `decide.interpret` / `interpret_compare_to_expectations` returns a typed
   `Comparison` (hot-spot match, magnitude-in-range, gap, within-tolerance).
4. **Test the parse→act path.** ✓ **done for one part class** —
   `tests/test_decide.py` plus `tests/test_iteration_loop_foam_dart_e2e.py` assert
   the `FailureMode` + comparison drive the next fix with no human in between.

### 7. Decide — macro scale ◐, micro scale ◐ (was ✗; `decide.from_failure` landed)

**Status: split between two scales, and the project is much further
along on one than the other.**

**Macro scale — ◐ well-built.** The outer orchestrator loop has
industrial-grade Decide machinery for ranking whole design variants:

- `orchestrator/sbce.py` — Set-Based Concurrent Engineering (Toyota's
  approach — keep multiple alternatives alive, eliminate via results,
  converge late). Candidate enumeration and beam search.
- `orchestrator/scorer.py` — SBCE scoring with Pareto frontier
  analysis, the G6 gate check.
- `orchestrator/validator.py` — geometry and assembly validation
  against frozen interface contracts, the G5 gate check.
- `orchestrator/runner.py` — G0 → G7 gate walk orchestration.
- `orchestrator/dsm.py` — Design Structure Matrix for subsystem
  dependency analysis.

Tests: `tests/test_sbce.py` (19), `tests/test_runner.py` (41),
`tests/test_dsm.py` (9), plus the rest of the orchestrator test suite
(~170 tests total). This is a substantial Decide-at-macro-scale
capability that's been underweighted in previous drafts of this
roadmap.

**Micro scale — ◐ (refreshed 2026-06-26; was ✗).** `decide.from_failure`
(`server/decide.py`) now turns a single typed `AnalysisCheck` into a structured
`FixProposal` (op / target / param / delta / rationale) — item 1 below, for the
common structural failure modes. The foam-dart loop uses it to pick the latch fix
autonomously. What keeps it ◐ rather than ✓: the auto-study fallback (item 2) and
FoS-budget awareness (item 3) aren't wired, and the proposal is a single best fix,
not yet a *ranked list* of candidates. The original ✗ writeup follows for context.

**Original micro-scale status: ✗ bare-bones.** Nothing in the toolset turns a
single failing `AnalysisCheck` into a ranked list of repair candidates. What
exists:

- `me.validate_constraints` / `me.apply_risk_gates` /
  `me.build_traceability` / `me.design_loop` — great for static
  constraint checking and "is this design ready for release" gating,
  but they operate on a constraint-dict model, not on a failure →
  remediation flow.
- `study.*` — parametric sweeps. This is the closest thing to micro-
  scale Decide we have, because a study over fillet radius ∈ [0.5,
  5.0] mm will find the smallest radius that satisfies FoS ≥ 1.5. But
  the LLM has to already know the right variable to sweep. There is
  no tool that converts *"stress concentration at Face7"* into *"run
  a bounded study on fillet radius over the edge set of Face7,
  objective: minimize max stress, budget: 8 samples."*

**The scales are complementary.** The outer SBCE Decide chooses
between "Design A vs Design B vs Design C" based on whole-system
objectives. The inner (missing) Decide chooses "for this failing
fillet, do I widen it, add material behind it, or change the load
path?" Both are needed for the loop to close — SBCE picks the winner
across alternatives; the micro Decide keeps each individual alternative
from dying to preventable failures.

**Textbook anchor:** Ullman *Decide* (makes this a first-class step in
every phase); Dieter Ch. 7 *Decision Making and Concept Selection*. The
distinction Ullman draws between Evaluate and Decide is exactly the
distinction between "I know what's wrong" and "I've picked which fix to
try" — and it's the distinction our current tooling elides.

**Tool source:** `server/tools_me.py`, `server/tools_study.py`.

**Test coverage:** `tests/test_tools_me.py` (static constraint
validation), `tests/test_tools_study.py` and `tests/test_study_*.py`
(mock solvers).

**What would move this from ✗ to ◐ — progress as of 2026-06-26 (now ◐):**

1. **A `decide.from_failure` tool.** ✓ **done** — takes a typed `AnalysisCheck`
   and returns a `FixProposal` for the common structural failure modes. Remaining
   polish: return a *ranked list* of candidates rather than a single proposal.
2. **Auto-study fallback.** ✗ **still open.** When no canned strategy fits, fall
   through to a bounded `study.*` sweep on the nearest geometric variable.
3. **Safety-factor budget awareness.** ✗ **still open.** The decider should read
   the target FoS from the brief and stop once margin is comfortably above
   threshold. (Blocked partly on the `part_class`/brief plumbing in Specify.)

### 8. Act ◐

**Status (refreshed 2026-06-26): unchanged at ◐, but the loop now demonstrates
the fix-apply-recheck cycle.** The foam-dart example applies the Decide
proposal, re-runs the *same* screen that found the failure, and reports the
before/after delta — i.e. items 1–2 below happen *within the example*, but only
ad hoc. There is still no general structured fix-log on the brief and no reusable
loop-aware dispatch wrapper; those remain the work to reach ✓.

**Original status: the tools exist, but they're the same tools as Synthesize.**

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

### 9. Learn ◐ (folklore step — recall is now tested; auto-ingestion is demoed)

**Status (refreshed 2026-06-26): the recall gap is closed; corpus + automatic
feedback remain.** `tests/test_knowledge_persistence_e2e.py` ingests a finding,
tears the store down, reopens a fresh store at the same path, and asserts the
finding is still searchable — the "nothing tests recall" hole is filled (item 2
below). The foam-dart example also auto-ingests its V2 finding (item 3, in one
example). What keeps Learn at ◐: there's still no committed seed corpus under
`me_knowledge/notes/`, auto-ingestion isn't generalized beyond the one example,
and the Learn → Reflect feedback (item 4) isn't wired because the shared
taxonomy/`part_class` plumbing doesn't exist yet.

**Original status: the store exists, it's empty, and nothing tests recall.**

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
2. **Persistence test.** ✓ **done** — `tests/test_knowledge_persistence_e2e.py`
   ingests a note, tears down the store, reopens a fresh store at the same path,
   and asserts `knowledge.search` still returns the content.
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

**Refreshed 2026-06-26: test 1 now passes for the foam-dart latch class.**
`tests/test_iteration_loop_foam_dart_e2e.py` walks all nine steps on the
`latch_sear` part class with no human between diagnosis and re-check — the first
real instance of the loop closing. The original placeholder
`tests/test_iteration_loop_e2e.py` stays `@unittest.skip`-ped but now carries a
pointer to the foam-dart test as the concrete closure. **Tests 2
(regression-recovery) and 3 (cross-session memory) still don't exist** — test 3
in particular is blocked on the Learn → Reflect feedback wiring (shared taxonomy
+ `part_class`), and both want generalization past the single wired class.

## Honest verdict

*(Refreshed 2026-06-26 — every step except the two textbook-strong ones moved.)*

**Tools: the folklore steps now have substrate; the gap is generalization.**

- Steps 1, 2, 5 (Specify, Synthesize, Simulate) — the capital-T Textbook
  steps with strong canonical anchors — are dense/strong and well-tested.
  (Specify's one latent gap, the `part_class` field, is still open.)
- Step 6 (Interpret) — **nearly ✓.** The typed `FailureMode` vocabulary and the
  `decide.interpret` comparison-to-expectations step both landed; only the
  structured `FixCandidate` shape remains.
- Steps 3, 9 (Reflect, Learn) — the folklore steps — are now **both ◐** (Reflect
  was ✗). Reflect has `ReflectExpectations` and an analytical screen but no
  shared taxonomy; Learn now has a passing recall test and a demoed auto-ingest
  but no seed corpus or generalized feedback. The tooling makes them happen for
  one part class; making them *unskippable everywhere* is the remaining work.
- Steps 4, 7, 8 (Screen, Decide, Act) — still ◐, but less hollow: Screen has its
  structural tier (1 of 3 + auto-gate pending), micro-Decide has
  `decide.from_failure` (was ✗), and Act demonstrates the recheck cycle.

**Tests: the first loop-closure test is green.** Plenty of unit and
stub-integration coverage, plus — new since the last draft — a passing nine-step
loop-closure test on the foam-dart latch class and a knowledge-recall persistence
test. Still missing: regression-recovery and cross-session-memory tests, and
loop closure on any second part class.

**Progress toward the vision: the thesis is now demonstrated once, not just
scaffolded.** The expensive substrate — FreeCAD automation, three sim backends,
FEA coupling, RL training — was already built; what changed is that the
*cognitive* steps (Reflect, typed Interpret, micro-Decide, tested Learn) now
exist as code and the loop empirically closes on **one** part class. The
remaining work flips the claim from *"the loop closes on the foam-dart latch"* to
*"the loop closes on an arbitrary part class"*: the shared failure-mode taxonomy,
the `part_class` brief field, the thermal/aero screen tiers + FEA auto-gate,
structured fix candidates, and the regression/cross-session tests.

## Priority stack

The previous draft of this roadmap named a single "highest-leverage
first move" — add `FailureMode` enum to `AnalysisCheck`. Re-evaluating
the project against the nine-step model and the two-loop architecture
surfaced three moves that are each comparable in leverage. They can
land in parallel; they each unblock a different piece of the loop;
and together they convert the bulk of `.claude/rules/*.md` from
prompt rules into enforceable structured substrate.

### Move 1 — Bring `analysis.*` up to `motion.*`'s tier structure ◐ partly done

**Landed (2026-06-26):** `analysis.screen_stress` — the structural Tier-1 screen
(beam bending, SCF lookup, Euler buckling, fastener checks), returning a typed
`AnalysisCheck`. **Remaining:** `analysis.screen_thermal`, `analysis.screen_aero`,
and gating Tier-3 FEA behind the screen inside `analysis.stress_check` (today
`screen_stress` and `stress_check` are separate tools; escalation is manual). The
original rationale and full target set are kept below.

**Why first.** `motion.*` already has Tier 1 (analytical) / Tier 2
(kinematic) / Tier 3 (dynamic) as a proven in-repo pattern. `analysis.*`
has no equivalent. Most routine parts don't need FEA — a hand calc
plus an SCF lookup resolves 70%+ of real design questions. Today every
`analysis.stress_check` call jumps straight to CalculiX. Adding an
analytical screen tier:

- Makes the **Screen** step a first-class capability
- Unblocks the **Reflect** step's "do I even need to simulate?"
  decision
- Dramatically speeds up iteration by keeping the solver out of
  obvious cases
- Gives **Interpret** a second data point to cross-check against FEA
  output (if screen prediction and FEA disagree by >expected tolerance,
  the solver setup is probably wrong)
- Copies an in-repo pattern (motion's tier ladder) rather than
  inventing a new one

**Concrete starting set.**

- `analysis.screen_stress` — beam bending (rectangular / circular),
  stress concentration lookup for common features (fillet, hole,
  notch), deflection bound, Euler buckling, fastener preload/pullout.
  Uses `geometry.section_properties` (already in-repo) for I, J, c.
- `analysis.screen_thermal` — lumped capacitance, Biot number, simple
  conduction/convection resistance networks.
- `analysis.screen_aero` — BEMT for rotors, lift curve slope for
  wings, drag coefficient lookups for common shapes.
- **Gate Tier 3 behind Tier 1.** `analysis.stress_check` (and its
  siblings) calls the corresponding screen first; if the screen
  resolves the question with clear margin, return the screen result
  and skip FEA. Mirror the escalation rule documented in
  `.claude/rules/analysis-policy.md`.
- **Tests** against textbook reference cases (cantilever bending,
  Hertzian contact, lumped-capacitance cooling).

### Move 2 — Paired wedge: `FailureMode` enum + `ReflectExpectations` dataclass ✓ done

**What landed (2026-06-26).** Both halves are merged: `FailureMode` is on
`AnalysisCheck` and `FieldResult` and is populated on FAIL across the analysis
and screen tools; `ReflectExpectations` is implemented and consumed by
`decide.interpret` / the loop-closure test. Together they unblocked the typed
Interpret step (`decide.interpret`), micro-Decide (`decide.from_failure`), and the
foam-dart loop-closure test. The original rationale is kept below for context.

**Why.** This is the wedge that makes the Interpret step typed and
the Reflect step unskippable. Neither alone is sufficient.

1. **`FailureMode` enum on `AnalysisCheck`** in `server/analysis_models.py:191`.
   Ten to twelve enumerated values: `STRESS_CONCENTRATION`, `YIELD`,
   `BUCKLING`, `FATIGUE`, `DEFLECTION`, `CONTACT`, `RESONANCE`,
   `THERMAL`, `WEAR`, `CORROSION`. Every `analysis.*` tool (both
   Screen-tier and Simulate-tier from Move 1) populates it on FAIL.
   This unblocks Interpret, Decide, and the loop-closure test.
2. **`ReflectExpectations` dataclass** capturing "what failure modes
   am I checking, what do I expect the result to look like, what
   would make it suspicious." `analysis.*` tools accept it as an
   optional argument on day one, warn loudly if it's absent, and make
   it required in the loop-closure test harness. This unblocks
   Reflect.

The two are paired because neither is fully useful alone. `FailureMode`
without expectations lets the LLM say "it's a stress concentration"
but not "I was expecting a stress concentration here and it's 4×
bigger than I thought." Expectations without `FailureMode` lets the
LLM file a prediction but not compare it against a typed result.

Together they unblock every downstream step: Decide dispatches on
`FailureMode`, Act logs the `(expectations, failure, chosen_action,
result)` tuple, Learn indexes by failure mode and part class, and the
loop-closure test finally has a typed assertion it can make.

### Move 3 — Wire real worker builds into the outer loop ✓ done

**What landed.** The outer loop is now closed against real FreeCAD
builds for **five** part classes. The plumbing has three layers:

1. **`orchestrator/worker_builds/`** — per-part-class builders
   (`sun_gear`, `planet_carrier`, `quadrotor_arm`, `rc_car_chassis`,
   `hexapod_leg`) that compute geometry Python-side then dispatch
   through `worker_entry._build_*` to drive the FreeCAD addon over
   TCP. Shared post-processing lives in `common.dispatch_and_rewrite`
   so each builder collapses to ~50 lines.
2. **`orchestrator/measure.py`** — self-verifying measurement layer.
   After each worker build, `measure_worker_step` independently
   re-imports the STEP file via `cad_import_step` and re-measures
   interface dimensions. Strategies registered: `bore_diameter` /
   `bore_dia` (with expected-hint disambiguation for parts whose
   `find_holes` returns multiple candidates), `pin_circle_dia` /
   `motor_mount_pcd` (PCD from N hole centroids), `pocket_depth`
   (top-face minus floor-face), `segment_length` (max of bbox X/Y).
3. **`tests/test_orchestrator_real_worker_e2e.py`** — five verify-mode
   tests, one per part class, that walk G0→G5 against real builds.
   Each asserts `report.measurement_source == "orchestrator"` and
   that all checkpoints pass.

A drift-detection test
(`tests/test_orchestrator_drift_e2e.py`) deliberately stomps a
worker's claimed `bore_dia` value with `common.override_claimed_measurements`
and asserts the validator flags `FailureCode.MEASUREMENT_DRIFT`. This
is the proof that the self-verifying measurement path actually catches
worker lies — not just passes them through.

**Original starting point** (kept for context): pick hexapod hip
bracket (or any of the four part classes already in
`tests/test_project_*.py`). Take its build sequence, wrap it as a
worker, plumb it through `orchestrator.runner.dispatch_all`, and
assert that the resulting STEP file passes `validator.validate_worker_result`.

### Move 4 — Unify the two FEA pipelines on one convergence-aware core ✗ planned

**Why.** Surfaced while hardening the foam-dart example (2026-06-27). There are
**two** structural-FEA implementations, and they duplicate the same engine
(mesh → CalculiX deck → solve → parse):

- `analysis.stress_check` (`server/tools_analysis.py`) — the modern Tier-3 tool
  the inner loop and the foam-dart example stand on. Takes a *live FreeCAD body*
  (`cad_export_body`), single mesh, single solve. **Correct** against CalculiX
  2.21.
- `orchestrator.fea.run_l2_fea` (`orchestrator/fea.py`) — the outer-loop worker
  scorer's FEA (`orchestrator/scorer.py`). Takes a *STEP file* (headless/batch),
  does a **dual-density mesh + convergence + singularity detection**. Its
  CalculiX deck is **malformed for ccx 2.21** (`*ERROR reading *DENSITY`,
  `gen3delem: first thickness`) — latent because the tests only run when `ccx`
  is installed, which nothing in CI did until now.

They are **not redundant** — one is addon-coupled/interactive, the other
file-coupled/batch with convergence rigor. The liability is the *duplicated
core*: the unused copy silently rotted. And the rigor (convergence/singularity
detection) lives in the path the example *doesn't* use, while the clean
interface lives in the path that *lacks* the rigor.

**What to do.** Extract one shared engine — `mesh → deck → solve → parse →
FieldResult`, convergence-aware (the two-density study the foam-dart latch now
does inline belongs here) — and make both entry points thin adapters over it:

- adapter A: live body → STEP (`analysis.stress_check`),
- adapter B: STEP file + batch convergence loop (`run_l2_fea` / scorer).

One deck-builder to keep correct (the ccx-2.21 `*DENSITY`/element-card bug
disappears with the duplicate), one result type, two front doors. The
foam-dart example's `fea_latch` convergence logic is the reference behaviour to
fold into the shared core.

**Sub-tasks (the #2–#4 from the 2026-06-27 review):**

1. **Unify the core + fix the `run_l2_fea` ccx-2.21 deck bug.** Land the shared
   engine; delete the duplicated deck-gen. (#2)
2. **Real-backend CI lane.** The rot hid because no environment ran the real
   solver — every `skipUnless(ccx)` test skipped forever. Add a lane (or
   scheduled job) with `calculix-ccx` + `gmsh` + a headless FreeCAD addon so the
   guarded FEA/kinematic e2e tests (`tests/test_fea_integration.py`,
   `tests/test_foam_dart_fea_e2e.py::TestFeaReal`,
   `tests/test_foam_dart_kinematics_e2e.py::TestKinematicGeometric`) actually
   execute. Without it, Move 4 itself will re-rot. (#3)
3. **Clamp-singularity sub-model.** The foam-dart convergence study trusts the
   FEA only at the fillet-resolving mesh; refining further re-exposes the
   *idealized fixed-face clamp edge*, which is itself singular. Add a sub-model
   (or de-singularized BC: filleted clamp / bonded region) so the root stress is
   exact rather than capped at an engineering mesh. This is the rigorous way to
   make the "converged" claim hold in the limit. (surfaced by #1)
4. **Revisit the latch load case.** A sear's governing load is the force at the
   instant of *catch* (full spring force, possibly with impact), not the 9 N
   static hold. V2's FoS ≈ 9 is a hint the static case is too gentle to be
   design-driving. Confirm the controlling load case and set the BC accordingly.
   Small ME task, independent of the refactor. (#4)

**Done already (#1, 2026-06-27):** the example's FEA rung was reframed from a
misleading "FEA reproduces the screen's SCF" (it didn't — a 358% gap) to an
honest mesh-convergence study: V2 (filleted) converges and confirms the screen
(±25%); V1 (sharp) diverges (singular) and is rejected by the analytical screen.
See `examples/foam_dart_spring_launcher/run.py` (`fea_latch`, `fea_convergence`)
and `tests/test_foam_dart_fea_e2e.py`.

### Parallelizability

The three moves are independent:

- Move 1 touches `server/tools_analysis.py`, `server/analysis_models.py`,
  new screen modules, and new tests. No orchestrator changes.
- Move 2 touches `server/analysis_models.py` (just the `AnalysisCheck`
  dataclass) and adds a small `server/reflect.py`. No screen tooling
  required (though Move 1's outputs should also populate `FailureMode`
  when both land).
- Move 3 (done) touched `orchestrator/worker_entry.py` (extended
  `_build_envelope` with `envelope_holes`, added `_build_leg`),
  `orchestrator/measure.py` (three new strategies + aliases), the new
  `orchestrator/worker_builds/` package, and the two new e2e test
  files. No changes to `analysis.*` or `server/analysis_models.py`.

They can be worked in parallel by different contributors without
merge conflicts. They each independently move the project from
"substrate exists" toward "the loop empirically closes."

### After the priority stack

*(Refreshed 2026-06-26 — items 1 and 2 landed; the live frontier is items 3–4
plus the `part_class` field and the thermal/aero screen tiers.)*

1. **Unskip the loop-closure test** against a known part class. ✓ **done for the
   foam-dart latch** — `tests/test_iteration_loop_foam_dart_e2e.py` walks all nine
   steps and passes; the original `tests/test_iteration_loop_e2e.py` placeholder
   stays skipped with a pointer to it. Next: close the loop on a *second* class
   (a hexapod or gearbox part) to prove generalization.
2. **Knowledge persistence test.** ✓ **done** —
   `tests/test_knowledge_persistence_e2e.py`.
3. **Part-class failure-mode taxonomies** as hand-curated YAML under
   `me_knowledge/failure_modes/` — ✗ **still the highest-leverage next step.** The
   foam-dart example proves the format with its own `failure_modes.yaml`; promote
   it to a shared catalog seeded with the four part classes that already have
   project tests (hexapod leg, planetary gearbox, quadrotor, rc car), and add the
   `part_class` field to the brief so Reflect can look them up.
4. **Auto-ingestion** from `study.results` (and closed iterate cycles) into the
   knowledge corpus so Learn fills itself without manual curation. ◐ demoed once
   in the foam-dart example; not yet generalized.

### Why this is mostly a refactor, not new tools

Reading `.claude/rules/*.md` straight through, it's striking how much
of the 9-step design is *already specified* — just as prompt rules
instead of as code:

*(Status column refreshed 2026-06-26.)*

| Rule file | Step | Rule → Tool refactor | Status |
|---|---|---|---|
| `design-pipeline.md` | Specify | Mostly already tools; add `part_class` field | ◐ field still missing |
| `me-preflight.md` | Reflect | → `reflect`/expectations + failure-mode taxonomy | ◐ `ReflectExpectations` done; shared taxonomy missing |
| `analysis-policy.md` | Reflect + Simulate gating | → analysis tier ladder (Move 1) | ◐ `screen_stress` done; thermal/aero + auto-gate pending |
| `motion-validation.md` | Screen → Simulate tier escalation | → `motion.*` tiers | ✓ implemented |
| `self-assessment.md` | Interpret | → `decide.interpret` + `FailureMode` enum | ✓ both landed |
| `study-policy.md` | Decide + Learn | Mostly tools; add auto-ingestion | ◐ `decide.from_failure` done; auto-ingestion demoed |
| `sim-engine-policy.md` | Simulate engine lifecycle | `sim.*` tools | ✓ implemented |
| `orchestrator-protocol.md` | Outer loop | `orchestrator/*` | ✓ implemented |

**When the last draft was written, `motion-validation.md` was the *only* rule
file whose tool-layer equivalent existed.** As of this refresh, `self-assessment.md`
(Interpret) is also fully tooled, and three more (`analysis-policy`, `me-preflight`,
`study-policy`) are partway there. The remaining prompt-only rules — chiefly the
shared failure-mode taxonomy and the `part_class` field — are the substrate still
waiting to be converted from prompt instructions into code.

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
