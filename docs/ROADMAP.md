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

**Status — outer loop: ◐ well-built but workers are stubbed.** The
state machine, gate checkers, SBCE scorer, DSM dependency analysis,
release packaging, and preflight validation are all real code with
real tests. What's missing: `test_orchestrator_e2e.py:131` writes a
*fake STEP file* where a real worker build should go. The outer loop
can walk G0→G7 on mocked worker output; it hasn't been wired to a real
per-worker `cad.*` build yet. Closing that gap is one of the top
priorities (see §Priority stack below).

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
| Status | ◐ well-built but workers stubbed | ✗ mostly missing (what this roadmap describes) |
| Tests | ~170 tests across 11 files | 1 skipped placeholder |

The rest of this roadmap focuses on the **inner loop** because that's
the less-built half. But the priority stack (bottom of the document)
calls out the one change needed on the outer side — wiring a real
worker build into `test_orchestrator_e2e.py` — because it's
comparable in leverage to the inner-loop changes.

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

**What would move this forward:** just the `part_class` field. Specify
is otherwise well-served today.

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

**`analysis.*` has no tier ladder at all.**

- `analysis.stress_check` goes straight to CalculiX FEA.
- `analysis.thermal_check` goes straight to the thermal solver.
- `analysis.aero_check` goes straight to SU2 or DUST.
- There is no `analysis.screen_stress` that says "for this beam with
  this load, σ = Mc/I = 42 MPa, handbook SCF at the fillet is ~2.3,
  expected peak around 97 MPa, comfortably under yield, no FEA
  needed."

So Screen is ◐ because **one half of it is ✓ strong and the other
half is ✗ missing.** The fix isn't "invent a Screen tool group" — it's
**"copy motion.*'s tier pattern into analysis.*."** The template is
in-repo and proven to work.

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
on the generic side. **No test coverage for analytical screens of
stress, thermal, or aero** because the tools don't exist.

**What would move this from ◐ to ✓:**

1. **Bring `analysis.*` up to `motion.*`'s tier structure.** Add:
   - `analysis.screen_stress` — beam bending (rectangular / circular
     cross-section), stress concentration lookup for common features
     (fillet, hole, notch), deflection bound via beam theory, Euler
     critical buckling load for columns, fastener preload and pullout
     estimates. Uses `geometry.section_properties` (already in-repo)
     for I, J, c.
   - `analysis.screen_thermal` — first-principles thermal bound (lumped
     capacitance, Biot number, basic conduction/convection resistance
     networks).
   - `analysis.screen_aero` — first-principles aero coefficient
     estimates (BEMT for rotors, basic lift curve slope for wings).
2. **Gate Tier 3 behind Tier 1.** `analysis.stress_check` calls
   `analysis.screen_stress` first; if the screen resolves the question
   with clear margin (Reflect-step expected bounds satisfied by a
   wide factor), return the screen result and skip FEA. Mirror the
   escalation rule already documented in `.claude/rules/analysis-policy.md`.
3. **Return the same `AnalysisCheck` shape from both Screen and
   Simulate** so the Interpret step can compare Screen predictions
   against FEA results consistently — if they disagree by more than
   expected tolerance, the solver setup is probably wrong (mesh, BCs,
   load case).
4. **Tests.** Known-good hand-calc reference cases (cantilever bending,
   Hertzian contact, thermal expansion of a rod, lumped-capacitance
   cooling) with assertions against textbook values. These are the
   same reference cases used in intro ME courses, so they're
   well-documented.

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

### 7. Decide — macro scale ◐, micro scale ✗

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

**Micro scale — ✗ bare-bones.** Nothing in the toolset turns a single
failing `AnalysisCheck` into a ranked list of repair candidates. What
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

## Priority stack

The previous draft of this roadmap named a single "highest-leverage
first move" — add `FailureMode` enum to `AnalysisCheck`. Re-evaluating
the project against the nine-step model and the two-loop architecture
surfaced three moves that are each comparable in leverage. They can
land in parallel; they each unblock a different piece of the loop;
and together they convert the bulk of `.claude/rules/*.md` from
prompt rules into enforceable structured substrate.

### Move 1 — Bring `analysis.*` up to `motion.*`'s tier structure

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

### Move 2 — Paired wedge: `FailureMode` enum + `ReflectExpectations` dataclass

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

### Move 3 — Wire one real worker build into `test_orchestrator_e2e.py`

**Why.** The outer loop is ◐ well-built but workers are stubbed.
`test_orchestrator_e2e.py:131` writes a *fake STEP file* where a real
worker should produce geometry. The gate walker (G0 → G7), the SBCE
scorer, the validator, the release packager, and all of the council/
skeleton/interface-freeze machinery are real code with real tests —
they just haven't been exercised against a real `cad.*` build because
the worker side is a stub.

Closing that one gap:

- Proves the outer loop end-to-end on one real part class
- Exercises the handoff between `orchestrator/worker.py` and the
  `cad.*` toolset (likely surfacing latent bugs)
- Gives us a real test that one worker can produce a geometry that
  passes `orchestrator/validator.py` against a frozen interface
  contract
- Becomes the foundation for running many workers in parallel
  later — you can't safely parallelize what you haven't closed
  sequentially

**Suggested starting point.** Pick hexapod hip bracket (or any of the
four part classes already in `tests/test_project_*.py`). Take its
build sequence, wrap it as a worker, plumb it through
`orchestrator.runner.dispatch_all`, and assert that the resulting
STEP file passes `validator.validate_worker_result`. Keep SBCE single-
candidate for this first pass — macro scoring and multi-candidate
pruning can come later.

### Parallelizability

The three moves are independent:

- Move 1 touches `server/tools_analysis.py`, `server/analysis_models.py`,
  new screen modules, and new tests. No orchestrator changes.
- Move 2 touches `server/analysis_models.py` (just the `AnalysisCheck`
  dataclass) and adds a small `server/reflect.py`. No screen tooling
  required (though Move 1's outputs should also populate `FailureMode`
  when both land).
- Move 3 touches `orchestrator/worker.py`, `orchestrator/worker_subprocess.py`
  or `orchestrator/worker_entry.py` (depending on execution mode), and
  `tests/test_orchestrator_e2e.py`. No changes to `analysis.*` or
  `server/analysis_models.py`.

They can be worked in parallel by different contributors without
merge conflicts. They each independently move the project from
"substrate exists" toward "the loop empirically closes."

### After the priority stack

Once the three top-priority moves land:

1. **Unskip `tests/test_iteration_loop_e2e.py`** against a known
   part class. This is the forcing function for the whole inner
   loop — wiring it up will expose whatever integration bugs are
   hiding in the handoff between Reflect, Screen, Simulate, and
   Interpret.
2. **Knowledge persistence test** — ingest a finding, close the
   knowledge volume, reopen in a fresh process, assert recall.
   Without this test the whole Learn step could silently break.
3. **Part-class failure-mode taxonomies** as hand-curated YAML under
   `me_knowledge/failure_modes/` — seeded with entries for the four
   part classes that already have project tests (hexapod leg,
   planetary gearbox, quadrotor, rc car).
4. **Auto-ingestion** from `study.results` into the knowledge corpus
   so Learn starts filling itself without manual curation.

### Why this is mostly a refactor, not new tools

Reading `.claude/rules/*.md` straight through, it's striking how much
of the 9-step design is *already specified* — just as prompt rules
instead of as code:

| Rule file | Step | Rule → Tool refactor |
|---|---|---|
| `design-pipeline.md` | Specify | Mostly already tools; add `part_class` field |
| `me-preflight.md` | Reflect | → `reflect.preflight()` + failure-mode taxonomy |
| `analysis-policy.md` | Reflect + Simulate gating | → analysis tier ladder (Move 1) |
| `motion-validation.md` | Screen → Simulate tier escalation | → **already implemented** as `motion.*` tiers |
| `self-assessment.md` | Interpret | → `interpret.compare_to_expectations()` + `FailureMode` enum |
| `study-policy.md` | Decide + Learn | Mostly already tools; add auto-ingestion |
| `sim-engine-policy.md` | Simulate engine lifecycle | Already implemented as `sim.*` tools |
| `orchestrator-protocol.md` | Outer loop | Already implemented as `orchestrator/*` |

**The `motion-validation.md` rule file is the only one whose
tool-layer equivalent already exists.** That's the proof the approach
works. Every other rule file is waiting for the same refactor from
prompt instructions into enforceable structured substrate. The
priority stack above turns the three most important of those rules
into code.

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
