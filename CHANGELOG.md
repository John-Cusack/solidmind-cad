# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
- Bundled knowledge content under `me_knowledge/notes` and `me_knowledge/sim_changes` from source control; repository now tracks placeholders only.
