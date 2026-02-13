# SolidMind CAD

FreeCAD-integrated MCP CAD co-pilot for turning plain-language ideas into real, buildable designs.

## Goal

Make it possible for any person, not only CAD experts, to use CAD software to design real things.

## Design Philosophy

1. Start from intent, not jargon.
The system asks structured questions in plain language and translates answers into engineering-ready fields.

2. Make the LLM mechanically grounded.
It uses deterministic constraint validation, ME knowledge resources, and a source policy (`me_knowledge/standards_sources.yml`) to tie decisions to engineering context.

3. Convert ideas into explicit constraints.
Before geometry, the system builds a concrete list of requirements: function, materials, dimensions, interfaces, tolerances, manufacturing constraints, and risks.

4. Validate before building.
The ME loop runs deterministic checks, creates traceability, and emits risk notices so unresolved issues are visible without hard blocking generation.

5. Build to the constraints.
For constrained workflows, the CAD flow executes `cad.*` operations after ME preflight; for fast iteration, live `cad.*` co-pilot operation is also available directly.

## Design Process Flow

```mermaid
flowchart TD
    A[User describes the part goal in plain language<br/>Why: start from intent, not CAD jargon] --> B{Choose workflow mode<br/>Why: balance speed vs engineering rigor}
    B -->|Fast iteration| C[Build and adjust geometry live in CAD<br/>Why: immediate visual feedback speeds learning and design]
    C --> D[Run manufacturability checks and prepare exports<br/>Why: confirm the part can actually be built and quoted]
    B -->|Constraint-first| E[Ask guided intake questions<br/>Why: remove ambiguity before modeling]
    E --> F[Capture requirements assumptions and unknowns<br/>Why: make decisions explicit and auditable]
    F --> G[Match request to relevant engineering domain pattern<br/>Why: apply domain-specific thinking early]
    G --> H[Create a structured constraint sheet<br/>Why: design against real limits like loads materials tolerances and process constraints]
    H --> I[Validate constraints build traceability and assess risk<br/>Why: catch failures early and justify decisions]
    I --> J{Are key unknowns or risks still present?<br/>Why: surface uncertainty while still allowing iteration}
    J -->|Yes| K[Ask targeted follow-up questions and update constraints<br/>Why: close gaps instead of guessing]
    K --> F
    J -->|No| C
    D --> L[Deliver final outputs<br/>Why: provide production-ready CAD files and vendor-facing summary]
```

## Architecture At A Glance

```mermaid
flowchart LR
    U[User in FreeCAD plus MCP host] --> H[LLM host]
    H -->|JSON-RPC over stdio| S[Bridge server: server/main.py]
    S -->|TCP localhost:9876| F[FreeCAD addon]
    F --> G[FreeCAD Python API operations]
```

## What It Supports

1. Live CAD co-pilot in FreeCAD (`cad.*` tools) — pad, pocket, revolution, sweep, loft, helix, polar pattern, fillet, chamfer, hole, screenshot, and more.
2. Manufacturing readiness and RFQ export (`mfg.*` tools).
3. ME preflight design loop (`me.*` tools) with constraint validation, traceability, and risk gates.
4. LLM-managed spec interview and finalization (`spec.*` tools).

Current policy:
- Coverage and ME risk outputs are notify-only by default.
- Geometry generation continues while warnings/required actions are returned for review.

## Requirements

- Python `>= 3.12`
- FreeCAD (optional, required for live `cad.*` operations)

## Install

```bash
python3 -m pip install -e .
```

## Run

Start MCP server over stdio:

```bash
python3 -m server.main
```

Run unit tests:

```bash
python3 -m unittest
```

## ME Design Loop Quick Flow

Typical sequence:

1. `me.validate_constraints` — run deterministic validators over a constraint dict
2. `me.build_traceability` — build requirement-to-evidence traceability matrix
3. `me.apply_risk_gates` — assign risk class and signoff gates

Or run all at once with `me.design_loop`.

Use `me.list_validators` to discover available validators and what fields they read.

## Documentation

- `ARCHITECTURE.md`: system architecture and protocol surface
