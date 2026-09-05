# Addendum A — Patch Gates and Benchmark Answer Key

Appends to `target-architecture.md`. Resolves the two closure-check punch-list items. Implementation-level; neither blocks tranche 1.

---

## A.1 — Patch gates

`patch` targets four artifacts. Gate strength tracks how much the artifact defines **the scoring** rather than **the solution**.

| Patch target | Gate | Why |
|---|---|---|
| Design graph | **Free** | The solution. This is what the loop exists to change. |
| Study — *variables, bounds* | **Free** | Where to look, not what counts. "Optimum pinned at a bound → widen it" is the canonical checklist diagnosis; gating it stops the loop. |
| Model registry | **Calibration-gated** | Enters with empty validity domain plus a measurement or calibration requirement. Blocks smuggling in uncalibrated physics. |
| Study — *objectives, constraints* | **Human-approval-gated** | Defines success. An LLM loosening a constraint or redefining the objective is editing the exam. |
| Scenario | **Human-approval-gated** | Encodes threat assumptions and requirements. Same reason. |

### Why split the study artifact by field class

The one-gate-per-artifact version would require human approval for bound-widening — the single most common legitimate reframing action. Splitting by field class preserves the distinction that actually matters:

> **What counts as success is gated. Where to look is free.**

The residual risk of free bound-widening is pushing into regions where a model extrapolates. That's controlled by **validity domains**, which are enforced independently and machine-derived from reference-data coverage. Gates are the wrong instrument for that risk; validity domains are the right one.

### `escalate` now reserves cleanly

With objectives patchable-but-gated, `escalate` is for **value judgments no patch can express** — competing objectives with no principled tradeoff, a requirement whose intent is ambiguous, a decision with consequences outside the model's scope. Not "the objective is wrong," which is now a gated `patch(study.objectives)`.

---

## A.2 — Benchmark answer key

Prescription is scored by executing the typed action — but three of four action types don't execute into a re-run. Without a per-class answer key the confusion matrix has undefined cells exactly where false-alarm accounting lives, and that's the number the decision gate turns on.

| Defect class | Seeded defect | Correct action | Scored by |
|---|---|---|---|
| **Control** | None | `no_action` | The action itself. **This row carries the false-alarm rate** — the most important cell in the matrix. |
| **Missing design variable** | A parameter that matters isn't in the study's variable set | `patch(study.variables)` — free gate | Re-run improvement |
| **Inadequate topology** | The parameterization can't express a better structure | `patch(design graph)`, typically introducing a parametric generator | Re-run improvement |
| **Missing mechanism** | The model chain lacks a term the optimizer exploits | `patch(model registry)` — calibration-gated — and/or `request_measurement` | Re-run improvement (see construction note) |
| **Wrong objective** | The objective doesn't measure what matters | `patch(study.objectives)` (gated) or `escalate` | The action itself; both accepted |

### Construction note: make the missing-mechanism class executable

Seeded naively, this class isn't scoreable on improvement — a newly proposed model class enters uncalibrated, so the re-run is blocked pending calibration, and you're left grading the plausibility of a proposal.

**Instead, seed it by ablating a mechanism from an already-calibrated model chain.** Remove the coherence-loss term from a chain you've already fitted. The correct patch restores it, calibration data already exists, the re-run executes immediately, and improvement is measurable. Same defect class, now fully scoreable.

This also makes the class more honest: you know the ground-truth correct answer exactly, because you removed it.

**Open construction requirement (from the final closure check):** hide the ablated model class from the registry visible to the benchmark arms, and match open-vocabulary proposals against the held-out class by mechanism identity (not string match). Otherwise defect class 3 becomes multiple-choice — "spot the registered-but-unused mechanism" — instead of open-vocabulary proposal, inflating the LLM arm on exactly the moat class the decision gate turns on. The checklist and human arms see the same hidden registry, so comparability holds.

### Partial credit

Score diagnosis and prescription separately. An arm that correctly identifies "coherence-limited, mechanism missing" but proposes the wrong model class has succeeded at diagnosis and failed at prescription — and that distinction is exactly what tells you whether the LLM's contribution is understanding or knowledge.

---

## Status

The review has converged. Next step is tranche 1: design graph, layered binding check, fingerprinting, and the evaluator CLI proven on the foam-dart latch — with the acceptance criterion that an unknown binding hard-fails and an unchanged fingerprint flags.
