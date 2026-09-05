# SolidMind-CAD: Target Architecture (Final)

Finalized August 9, 2026 after a three-round adversarial review. Round-3 changes marked **[R3-n]**. Addendum A (`target-architecture-addendum-a.md`) resolves the two closure-check punch-list items and is part of this contract.

The artifact/function algebra is closed under everything the system claims to do. That was the property worth three rounds of review. **Build tranche 1 next, not a fourth review.**

---

## The organizing principle

**Content-addressed artifacts, pure functions between them.** Every artifact immutable and hashed; every process takes hashed inputs and produces hashed outputs. Lineage, reproducibility, attribution, caching, honest demos, and scorable edits through one mechanism.

### Determinism is manufactured, not assumed

- **Cache keys include environment identity** — container digest, solver build, thread and domain decomposition. A hash omitting this is stale-result reasoning disguised as reproducibility.
- **Pinned digests, lockstep stepping, fixed decomposition or serial runs** on the design-critical path.

**Equality policy — what it's actually for. [R3-4]** It does *not* apply to cache hits; a hash match is exact by construction. It exists for two other jobs:

1. **Reproducibility audit** — run the same key twice, confirm purity holds. Standard here is **bit-exact, or documented bounded divergence.** With pinned containers, lockstep, and fixed seeds, the world tier *should* be reproducible; blessing "statistically equivalent" here is permission to shrug at the nondeterminism this discipline exists to remove. **If an engine's divergence can't be bounded, it can't sit on the design-critical path.**
2. **Cross-version comparison** — is a new Gazebo digest's output compatible with results cached under the old one? **This** is where statistical equivalence belongs.

---

## Six artifacts

**1. Design graph revision** — what is being designed. Native, versioned, from `MasterSpec` + GIR/EIR. Typed parameters at stable paths, frames, materials, interfaces. **Node and sensor placements live here** — placement is the design variable of a detection mesh.

**2. Scenario** — the world. Trajectories, atmospheric state and turbulence statistics, terrain, target signatures, environmental distributions. A data artifact, not a running process. Visibility timelines are *derived*, computed inside `evaluate` and cached on hash(terrain, placements, trajectory).

> **The scenario/design boundary is a per-project role assignment, not a fixed property. [R3-5]** Target signature is scenario content for the counter-drone project and a *design variable* the day SolidMind designs a quiet drone — at which point the detection mesh becomes scenario. Every model and quantity carries a **role tag per project**; the taxonomy assigns roles, it doesn't hard-code them.

**3. Study definition** — variables bound to parameter paths, distributions, objectives, **bounds**, constraints, seed, trial count. Common random numbers across design points.

**4. Model registry and calibrated model revisions** — physics model *classes* (structural, uncalibrated) and their *calibrated revisions* (class + fitted parameters + validity domain + lineage to the reference data consumed).

> **Validity domains are computed, not declared. [R3-5]** `calibrate` derives the envelope from the coverage of the reference data it consumed. Human-declared envelopes will be generous, and a generous envelope is an extrapolation license — the hack surface again. The register's confidence culture extends to machine-derived envelopes.

**5. Evaluation result** — metrics with units, failure budget, per-sensor detail, applied bindings, artifact hashes, telemetry references, seed, environment identity, **model identity**, validity-domain hits, unchanged-fingerprint flags, failure classification.

**6. Reference data** — analytical closed-form cases, public datasets, and your own measurement campaign.

---

## Eight functions — signatures corrected [R3-2]

```
materialize (revision, params)                              → artifacts + fingerprints
calibrate   (model class, reference data)                   → calibrated model revision
evaluate    (revision, params, scenario, models, seed)      → result
drive       (revision, scenario, study, models)             → optimum + sensitivity
diagnose    (results, study)                                → diagnosis
prescribe   (diagnosis, revision, scenario, study, models)  → typed action
validate    (results, reference data)                       → trust or reject
view        (results)                                       → demo
```

`drive` needs `models` to pass through to `evaluate`. `diagnose` needs `study` to know where the bounds are. `prescribe` needs everything it might patch. Each omission was the lineage principle leaking at the interface the document calls the product.

### The binding check, layered

- **Binding not applied** (path doesn't exist, never reached the source document) — checkable at revision level before materialization. **Hard fail.**
- **Binding applied, fingerprint unchanged** — often legitimate (sub-tolerance finite-difference steps, export rounding, parameters in disabled branches). **Returns a flag**, consumed by `diagnose` as a zero-sensitivity signal. A *cluster* of flags is the no-op detector.

### Evaluate: tiered stages

| Tier | Runs | Cost | Audit standard |
|---|---|---|---|
| Analytic | In-process — bearing variance, link budgets, GDOP | ms–s | bit-exact |
| Field | Gmsh → CalculiX/Elmer/code_aster via preCICE | minutes | bit-exact or bounded |
| World | Gazebo/PX4, waveform synthesis, ns-3 | minutes | bit-exact or bounded |

**World-tier timing requirements. [R3-5]** Demotion to validation tier does not relax them — a sloppy validator produces noise rather than truth. Two bind whenever waveform synthesis runs: **single clock authority** across engines (one physics step of scheduler jitter, 1–4 ms, is ~50× the per-degree TDOA timing budget), and **C¹ fractional-delay interpolation** of source motion (piecewise-linear position steps the Doppler and spreads harmonic tracks with sidebands).

### Prescribe: typed actions, and patches target any artifact [R3-1]

Four legal forms. Prose stays banned.

- **`patch`** — an executable, schema-validated edit to **any versioned artifact**: design graph, scenario, study definition, or **model registry**
- **`request_measurement`** — "the model is unfalsifiable here; go measure X"
- **`escalate`** — "the objective is wrong; a human must decide"
- **`no_action`** — "nothing is wrong; don't touch it"

**Model-registry patches carry a mandatory consequence:** a new model class enters with an **empty validity domain** plus a measurement or calibration requirement. The LLM can propose mechanisms; it cannot smuggle in uncalibrated physics and then optimize against it.

This is load-bearing for the decision gate. Restricting patches to the design graph would make benchmark defect class 3 (*missing mechanism the optimizer exploits*) unanswerable except by `escalate` — scoring the prescription arm on "ask a human" and leaving open-vocabulary model extension, one of the three claimed moat capabilities, with no legal output form at all.

---

## Ownership

**Owns:** design graph, binding and fingerprinting, evaluator harness, result schema, model registry and calibration, diagnosis, prescription interface, viewer.

**The one physics exception:** no adopted tool provides outdoor turbulence-coherence propagation. You implement an Ostashev–Wilson / von Kármán-class model and **calibrate its parameters against measurement** — physics model with measured parameters, not a bare empirical fit whose validity domain equals the dataset's conditions.

**Adopts:** Dakota / OpenMDAO, preCICE, FreeCAD, Gmsh, CalculiX / Elmer / code_aster / OpenFOAM, Gazebo + PX4 SITL (validation only), BELLHOP via UnderwaterAcoustics.jl, ns-3.

**Never writes:** coupling algorithms, FEA solvers, network simulators, optimizers, underwater propagation.

---

## Interactive mode — two decisions, not a reassurance [R3-3]

Dakota and the LLM edit the design graph and materialization flows forward. **The human edits the output of materialize** — dragging geometry in FreeCAD — and de-materializing is not a well-defined inverse: a dragged fillet might be a parameter change, a new parameter, or a structural edit. Bidirectional CAD↔model sync is a graveyard. So this is decided explicitly:

**(a) The design graph is the single source of truth; FreeCAD is a view.** Human edits in a live session are captured as **proposed patches through the same typed-action schema as the LLM.** The human is literally a third prescriber, exactly as they are a third driver. This preserves the natural-language CAD co-pilot experience while keeping one truth.

**(b) There is an explicit commit boundary** — a defined point where a live session's state becomes a revision. Before it, session state is scratch; after it, it's hashed and immutable.

Without both, the first interactive session forks the truth and the fingerprinting guarantee quietly dies.

---

## Assumptions register [FIX 5]

Load-bearing numbers, with honest confidence. Architectural commitments derived from these inherit their confidence — they are not facts.

| Assumption | Confidence | Resolves when |
|---|---|---|
| Coherence lengths ~1–10 m at few-hundred Hz over few-hundred m, daytime convective | **Medium** | Measurement campaign |
| Intra-node apertures 0.5–2 m retain partial coherence | **Medium** | Measurement campaign |
| Therefore cross-node TDOA unviable; bearing intra-node, triangulate across | **Medium** (inherited) | Measurement campaign |
| Turbulence coherence time ~0.1–1 s caps integration | Medium | Measurement campaign |
| Surrogate Sobol at ~300–500 evaluations | High on arithmetic, medium on PCE with categoricals | Tranche 2 |
| ~~RWDA ships meteorological data~~ | **NO** (per public documentation) | Resolved |
| ~~RWDA ships ground-truth trajectories~~ | **NO** (per public documentation) | Resolved |

**Consequence: the measurement campaign is critical path, not insurance. [R3]** RWDA's annotations are drone model, manoeuvre, and estimated SNR; array geometry is a diagram with unspecified spacing. No met covariates, no GPS truth. It therefore cannot yield coherence-vs-separation as a function of atmospheric conditions, nor TDOA-anomaly ground truth.

RWDA remains genuinely useful for what it is: **source spectra in real backgrounds, a domain-shift testbed, and weak detection-threshold labels.**

The campaign — two nodes at design-relevant separation, one drone, collocated anemometer and thermometer, GPS truth — becomes a **scheduled tranche-3 item with the same first-class status as `calibrate`**, because tranche 4's acoustic tier has a hard data dependency on it.

---

## Build order

**Tranche 1 — the spine.** Design graph + layered binding + fingerprinting + evaluator CLI, proven on the foam-dart latch. `StudyDriver` seam + Dakota adapter. Result contract, artifact store, durable jobs. Determinism discipline and the three-container split scheduled here.

**Tranche 2 — is the thesis true?** Deterministic diagnosis checklist, plus a **literature-parameter acoustic analytic stage** so the benchmark spans a domain where the checklist should suffice *and* one where open-world knowledge is the claimed moat.

Then the **seeded-defect benchmark**: 20–30 problems across four defect classes plus no-defect controls; three arms (LLM, checklist, human); diagnosis scored as a confusion matrix where **false-alarm rate on controls outweighs hit rate**; prescription scored by executing the typed action and measuring whether the re-run improves. Defect class 3 requires model-registry patches — which now exist.

> **Decision gate.** If the LLM doesn't beat the checklist at an acceptable false-alarm rate and its executed actions don't improve outcomes, ship the checklist with a Dakota front-end. That's a good product. Week six beats month eight.

**Tranche 3 — trust.** Validity domains enforced and machine-derived. `calibrate` implemented. **Measurement campaign run** — critical path. Rank-agreement tripwire. Robustness regularization against simulator nuisance parameters. Adversarial red-team pass.

**Tranche 4 — domains.** preCICE composite jobs. Acoustic analytic tier with calibrated coherence and the anomaly/threshold model. Web viewer and first demo. World tier for scenario generation and periodic validation. **Sonar as the platform test.**

---

## What this costs

The unified running world, and emergent couplings you didn't think to model. Recorded scenarios return only what you thought to record; the world tier runs periodically so surprises surface in validation rather than never. A deliberate trade.

## The claim this defends

The LLM earns its place by compiling open-world engineering knowledge into executable, validity-checked edits — to the design, to the scenario, to the study, and to the model chain itself. Fingerprinted lineage, machine-derived validity domains, calibrated models, reference-data anchors, rank-agreement tripwires, and seeded benchmarks are what separate this from a Dakota front-end with a story attached.
