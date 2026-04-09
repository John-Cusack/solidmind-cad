> **This is the authoritative orchestrator spec.** It supersedes `docs/orchestrator-plan.md` (the original v3 plan). Other `docs/orchestrator-*.md` files are earlier iterations kept for reference.

# CAD-First Rewrite Review for the Multi-Agent Orchestrator Plan

## Executive Verdict

The current v2 orchestrator plan is strong on orchestration determinism, but it is still too weak on mechanical product definition, assembly realism, and release readiness to act as a production CAD/ME execution spec. It already makes several good systems decisions; the rewrite below keeps those intact and changes the parts that still under-specify how real parts get assembled, verified, purchased, inspected, and released.

This rewrite is intentionally grounded in the schema and validation language that already exists in the repo:

- `orchestrator/spec.py` for orchestrator state, subsystem, interface, and worker-result concepts.
- `orchestrator/state.py` for explicit transitions and retry routes.
- `server/spec_draft.py` for manufacturing, inspection, and deliverable structure.
- `server/me_orchestrator.py` for pre-simulation ME heuristics and risk gates.
- `server/rules_cnc.py` and `server/rules_print_3d.py` for maturity-level expectations around tolerances, finish, and inspection.

The goal is to replace `docs/orchestrator-plan.md` with a CAD-first execution spec, not to write another general orchestration critique.

## What To Keep

The following v2 decisions are already correct and should remain unchanged:

1. Frozen interfaces remain contractual once they are approved.
2. Orchestrator-measured geometry remains authoritative; worker-reported measurements stay advisory.
3. Variant selection stays assembly-level, not local to each subsystem.
4. Complexity-aware runtime policy stays tied to subsystem difficulty rather than a single global timeout.
5. Human gates remain part of the process at architecture freeze, validation, and final decision points.
6. Cheap checks still gate expensive verification work.

## Required CAD/ME Revisions

| Current Gap | Proposed Change | Why It Matters |
|---|---|---|
| Worker generation starts before the assembly has a shared mechanical skeleton. | Add an `Assembly Skeleton / Layout Freeze` stage before worker generation. Freeze shared datums, shaft centerlines, bearing spans, center distances, reserved volumes, and package ownership. | Independent workers cannot safely design mating parts unless the assembly coordinate logic already exists. |
| Every subsystem is treated like a generated part. | Split subsystems into `generated`, `catalog`, and `standard` part kinds. Lock bearings, seals, fasteners, motors, and stock hardware by standard or supplier selection instead of regenerating them. | Real assemblies mix custom geometry with purchased hardware. Treating all parts as generated creates fake design freedom and weakens reproducibility. |
| Interface contracts are still mostly dimensional. | Expand interfaces into functional ICDs with datums, CTQs, fit class, runout/concentricity, preload/backlash, finish, inspection method, retention method, lubrication notes, and service constraints. | Assemblies fail on function long before they fail on a single nominal dimension. |
| Assembly validation focuses on collisions, envelope, and local ME checks. | Add assembly validation for insertion path, tool access, fastening sequence, bearing retention, service removal, stack-up risk, and thermal-growth allowance. | A model can be collision-free and still be impossible to assemble, inspect, or maintain. |
| Simulation is framed too broadly as the automated source of truth. | Replace that language with a verification stack: geometry and inspection own dimensional truth; analysis verifies named failure modes under explicit boundary conditions. | Simulation is only meaningful when loads, contacts, constraints, and fidelity are controlled. |
| Deliverables stop too early. | Add release-package outputs: BOM, purchased-part list, ICD set, drawing/inspection notes, revision manifest, and provenance bundle. | Manufacturable engineering output requires release artifacts, not only geometry and scores. |
| Build order reads like a greenfield system. | Reframe implementation around the repo reality: `orchestrator/spec.py`, `orchestrator/state.py`, `orchestrator/providers.py`, `server/spec_draft.py`, and `server/me_orchestrator.py` already exist and should be extended, not replaced. | The plan should reflect the actual baseline so delivery status is credible. |

## Replacement Lifecycle

### Stage 0: Requirements Normalization and Objective Sheet

- Inputs:
  - User goal, duty cycle, environment, hard constraints, preferred processes, and acceptance priorities.
- Actions:
  - Convert the request into a structured objective sheet with units, thresholds, and process assumptions.
  - Normalize missing but required engineering context: operating temperature, load cases, lifetime, maintenance expectation, and whether the design contains purchased components.
- Output:
  - `normalized_goal.yaml`
- Gate `G0`:
  - Pass only if every hard constraint is explicit and every objective has units plus either a threshold or a ranking weight.
- Failure route:
  - Stop and request clarification.
- Human approval:
  - `No`, unless contradictions remain unresolved.

### Stage 1: Architecture Council and Sizing Feasibility

- Inputs:
  - `normalized_goal.yaml`, relevant knowledge notes, and the current process/manufacturing rules.
- Actions:
  - Decompose the product into subsystems.
  - Separate generated geometry from catalog or standard components.
  - Run first-pass sizing and feasibility checks: mass rollup, package fit, kinematic consistency, and process sanity.
- Outputs:
  - `master_spec_draft.yaml`
  - `feasibility_report.json`
- Gate `G1`:
  - Pass only if budgets are coherent and the architecture is mechanically plausible at a high level.
- Failure route:
  - Repair in Stage 1 with bounded retries, then stop if feasibility still fails.
- Human approval:
  - `Yes`

### Stage 2: Assembly Skeleton / Layout Freeze

- Inputs:
  - `master_spec_draft.yaml`
- Actions:
  - Freeze the shared assembly skeleton before part generation:
    - primary datums and global reference frames
    - shaft axes and center distances
    - bearing spans and mounting planes
    - reserved volumes for purchased parts
    - keep-out zones, access zones, and ownership of shared package space
- Outputs:
  - `assembly_layout.yaml`
  - updated `master_spec_layout_frozen.yaml`
- Gate `G2`:
  - Pass only if every subsystem attaches to a shared datum scheme and all purchased-component reservations resolve cleanly.
- Failure route:
  - Back to Stage 1 for re-budgeting or re-architecture.
- Human approval:
  - `Yes`

### Stage 3: Interface and Purchased-Component Freeze

- Inputs:
  - `master_spec_layout_frozen.yaml`
- Actions:
  - Freeze all functional interfaces and lock catalog components by standard or supplier part.
  - Complete the ICD for each interface, including dimensional, functional, inspection, and service requirements.
- Outputs:
  - `interface_contracts.yaml`
  - `purchased_parts.yaml`
  - `master_spec_frozen.yaml`
- Gate `G3`:
  - Pass only if every interface is complete and every non-generated part is identified by a standard or supplier part number plus quantity.
- Failure route:
  - Back to Stage 2 for layout issues or Stage 1 for architecture issues.
- Human approval:
  - `Yes`

### Stage 4: Worker Generation for Generated Parts Only

- Inputs:
  - `master_spec_frozen.yaml`
  - `interface_contracts.yaml`
  - `assembly_layout.yaml`
- Actions:
  - Launch workers only for subsystems marked `generated`.
  - Inject deterministic run context: `run_id`, `worker_id`, `spec_hash`, `prompt_hash`, and tool version manifest.
  - Require workers to generate only the custom geometry they own; purchased parts are referenced, not redesigned.
- Outputs:
  - part geometry artifacts
  - worker metadata and provenance bundle
- Gate `G4`:
  - Pass only if all required artifacts, manifests, and checksums exist and match the frozen spec.
- Failure route:
  - Retry the affected subsystem within its runtime policy; fail the run only after retry budget exhaustion.
- Human approval:
  - `No`

### Stage 5: Deterministic Geometry and Assembly Validation

- Inputs:
  - worker artifacts from Stage 4 plus frozen purchased-part definitions
- Actions:
  - Reimport generated geometry and authoritative purchased-part geometry.
  - Recompute mass, envelope, interface dimensions, and assembly relationships.
  - Validate:
    - interface dimensions and tolerances
    - collisions and clearances
    - insertion/removal paths
    - wrench and tool access
    - fastening sequence and retention logic
    - bearing seat and support conditions
    - service access and replaceability
    - tolerance-stack risk flags
    - thermal-growth allowance where applicable
    - existing ME heuristic gates
- Outputs:
  - `geometry_validation_report.json`
  - `assembly_validation_report.json`
- Gate `G5`:
  - Pass only if the design is dimensionally compliant and physically assemble-able, inspectable, and serviceable within the declared process assumptions.
- Failure route:
  - Interface or local geometry failures go to Stage 4.
  - Skeleton or access failures go to Stage 2.
  - Architecture failures go to Stage 1.
- Human approval:
  - `Yes`, at least on the first complete validation pass.

### Stage 6: Targeted Verification Ladder

- Inputs:
  - validated assembly candidates from Stage 5
- Actions:
  - Run verification in increasing cost order:
    - analytic checks and handbook equations
    - proxy or coarse simulation where needed
    - higher-fidelity simulation only for specific remaining risks
  - Tie every analysis to a named failure mode, boundary condition set, and acceptance criterion.
  - Keep geometry truth separate from analysis results.
- Outputs:
  - `verification_report.json`
  - candidate ranking and Pareto report
- Gate `G6`:
  - Pass only if at least one assembly candidate satisfies all hard thresholds with traceable evidence.
- Failure route:
  - Back to Stage 5 if the issue is validation setup or measurement scope.
  - Back to Stage 1 if the architecture fundamentally misses the objectives.
- Human approval:
  - `Conditional Yes` before high-cost or high-fidelity verification, otherwise `No`.

### Stage 7: Release Package and Decision Report

- Inputs:
  - winning candidate plus all authoritative measured and verified outputs
- Actions:
  - Build the release package:
    - released BOM and purchased-part list
    - interface control documents
    - manufacturing notes and process assumptions
    - inspection method and CTQ list
    - provenance and revision manifest
    - final decision report with rationale and residual risks
- Outputs:
  - `release_package/`
  - `decision_report.md`
- Gate `G7`:
  - Pass only if the selected candidate has complete release-facing artifacts, not only geometry and ranking data.
- Failure route:
  - Back to Stage 3 for missing contract data, Stage 5 for missing validation evidence, or Stage 6 for missing verification evidence.
- Human approval:
  - `Yes`

## Schema / Interface Deltas

### `MasterSpec.subsystems`

Keep the existing subsystem concept in `orchestrator/spec.py`, but extend it so it can model real assemblies instead of only worker-owned custom parts.

Required additions:

- `kind`: `generated | catalog | standard`
- `standard`: standard family or governing spec when applicable
- `supplier_part`: locked vendor or manufacturer part identifier
- `qty`: required quantity in the released assembly
- `assembly_constraints`:
  - `reserved_volume_mm`
  - `keepout_zones`
  - `install_direction`
  - `tool_access_faces`
  - `service_clearance_mm`
- `release_requirements`:
  - `drawing_required`
  - `inspection_required`
  - `bom_line_type`
  - `revision_controlled`

Manufacturing fields should be expanded toward the language already used in `server/spec_draft.py` rather than inventing a second schema:

- `process`
- `process_notes`
- `material`
- `tolerances.general`
- `tolerances.critical`
- `surface_finish.ra_um`
- `coating` where relevant

Rule:

- Only `generated` subsystems are sent to workers.
- `catalog` and `standard` subsystems must resolve to released part selections before Stage 4.

### `MasterSpec.interfaces`

Keep the existing interface object, but promote it from a geometric contract to a functional ICD.

Required additions:

- `datum_scheme`
- `ctqs`
- `fit`
- `runout_or_concentricity`
- `preload`
- `backlash`
- `surface_requirements`
- `inspection`
  - `method`
  - `requirements`
  - `sampling`
- `retention`
- `lubrication`
- `service_requirements`
- `thermal_allowance`

Rule:

- An interface is not freeze-ready unless it is dimensionally defined, functionally defined, and inspectable.
- For CNC-grade maturity, general tolerance scheme, surface finish, and inspection method are mandatory, matching the expectations already expressed in `server/rules_cnc.py`.

### `WorkerResult`

Keep claimed versus measured outputs separate, as already established in `orchestrator/spec.py`, and add the missing release and provenance fields.

Required additions:

- `provenance_manifest`
  - `run_id`
  - `worker_id`
  - `spec_hash`
  - `prompt_hash`
  - `image_digest`
  - `tool_versions`
- `artifact_manifest`
  - `path`
  - `sha256`
  - `size_bytes`
  - `created_at`
- `release_artifacts`
  - expected downstream artifacts or placeholders tied to the part

Rule:

- Scoring reads authoritative measured data only.
- Release packaging reads provenance plus measured data, never worker narrative alone.

### State Model

Preferred change:

- Add `layout_frozen` and `release_packaging` to the `SpecStatus` enum and route them explicitly in `orchestrator/state.py`.

Fallback if enum expansion is deferred:

- Treat layout freeze as a mandatory sub-stage inside `INTERFACES_FROZEN`.
- Treat release packaging as a mandatory sub-stage after `AWAITING_HUMAN` and before `DONE`.

The explicit-state option is better because it makes retries, audits, and operator visibility deterministic.

## Build Order Reframed Around Repo Reality

Implementation should not be described as greenfield work.

Start from the foundation that already exists:

1. Extend `orchestrator/spec.py` rather than redefining the spec model elsewhere.
2. Extend `orchestrator/state.py` for the additional lifecycle states and routes.
3. Keep `orchestrator/providers.py` focused on provider abstraction; do not use it to hide worker-mode constraints that are still real in MVP.
4. Reuse `server/me_orchestrator.py` for pre-simulation risk gates and heuristic validation patterns.
5. Reuse `server/spec_draft.py`, `server/rules_cnc.py`, and `server/rules_print_3d.py` for manufacturing, inspection, and maturity terminology.
6. Treat council, worker, validator, scorer, and release-packaging modules as new implementation layers on top of that existing base.

## Acceptance Scenarios

1. A purchased bearing and a generated housing share a frozen bore/shaft interface, including fit, finish, and inspection method, and the candidate passes Stage 5.
2. A fastener head or wrench-access conflict fails assembly validation even though collision and envelope checks pass.
3. Two locally strong subsystem variants lose to a globally better assembly because backlash, clearance, or stack-up behavior makes the local winners inferior in combination.
4. Missing inspection method or surface finish blocks freeze for a CNC-grade deliverable.
5. Re-running the same frozen spec with the same purchased-part selections yields traceable artifacts and the same released component choices.
6. The final output is rejected if it contains only geometry and score reports but no BOM-level release artifacts.

## Closing Position

This rewrite keeps the current plan's strongest orchestration ideas and makes the missing mechanical engineering decisions explicit. The result is a better fit for real CAD work because it treats layout, purchased parts, interfaces, assembly validation, verification, and release output as first-class engineering concerns rather than as side effects of geometry generation.
