# CLAUDE.md

SolidMind CAD — a FreeCAD co-pilot powered by MCP. The LLM drives FreeCAD's PartDesign workbench directly through MCP tools while the user sees the model updating live. Python >= 3.12.

## Commands

```bash
python3 -m pip install -e .          # Install (editable)
python3 -m server.main               # Run MCP server (bridge) over stdio
python3 -m unittest                  # Run all tests
python3 -m unittest tests.test_tools_cad  # Run a single test module
```

## Architecture

```
Claude Code CLI ──stdio──▶ MCP Bridge Server ──TCP socket──▶ FreeCAD Addon
                           (server/main.py)     :9876         (freecad_addon/)
                               ├─ TCP :9877 ──▶ Chrono Daemon (optional, C++ MBS)
                               ├─ TCP :9878 ──▶ Isaac Bridge  (optional, GPU sim)
                               ├─ TCP :9879 ──▶ Gazebo Bridge (optional, CPU sim)
                               ├─ Field solvers  ──▶ CalculiX (structural FEA, subprocess)
                               │                    SU2 (RANS CFD, subprocess)
                               │                    DUST (vortex particle, subprocess)
                               │                    + solver packs (pip-installable)
                               ├─ LanceDB (in-process knowledge store)
                               └─ Docling (PDF/DOCX extraction)
```

## IMPORTANT: Design Pipeline Gate

**BEFORE building any multi-body design (3+ bodies), you MUST use the phased design pipeline.**

1. `design.save_brief` → define intent and constraints
2. `design.add_part` → register every part (custom + purchased) with specs
3. `design.add_interface` → define how parts connect (mesh pairs, bolt patterns, arbor paths)
4. `design.update_brief` → add layout with positions, Z layers, clearances
5. Present layout to user → get approval
6. ONLY THEN start building with `cad.new_body`

Skipping this pipeline leads to spatial conflicts, missing connections, and costly rework.
Skip ONLY for: single-body parts, quick modifications, or when user says "just build it."

See @.claude/rules/design-pipeline.md for the full phased process.

## Tool Groups

| Group | Purpose |
|-------|---------|
| `cad.*` | Drive FreeCAD PartDesign + measurement + spatial audit |
| `design.*` | Phased assembly design (briefs, parts, interfaces, verification) |
| `mfg.*` | Manufacturing readiness (on-demand, not forced) |
| `me.*` | Deterministic ME preflight (validators + risk gates) |
| `motion.*` | Motion validation (analytical → kinematic → dynamic) |
| `study.*` | Parametric design optimization (sweep, solve, rank) |
| `geometry.*` | Parametric generators (gears, springs, cams, linkages) |
| `knowledge.*` | Knowledge base (hybrid search, PDF extraction, ingestion) |
| `analysis.*` | Field-problem solvers (structural FEA, CFD aero/hydro, simulation coupling) |
| `sim.*` | Simulation engine lifecycle (start/stop/status for Chrono, Gazebo, Isaac) |

## Interaction Flow

1. User describes what they want → decide complexity.
2. **Simple** (1-2 bodies, dimensions given): `cad.new_document` → `cad.new_body` → sketch → pad → detail.
3. **Complex** (3+ bodies, mechanisms, assemblies): **use design pipeline** (see gate above).
4. Each step returns verification images — examine them to confirm geometry.
5. User clicks in FreeCAD → `cad.get_selection` → use references in follow-up commands.
6. User provides PDF → `knowledge.extract` → `knowledge.ingest` to index.
7. For structural validation: `analysis.stress_check` or `analysis.stress_from_simulation` (feeds forces from motion.* into FEA).
8. For aerodynamic/hydrodynamic validation: `analysis.aero_check` with flow conditions and optional rotors.

## Sketch Elements

All types supported: `rect`, `circle`, `line`, `arc`, `spline`, `external_ref`, `sketch_fillet`, `sketch_chamfer`. Use splines for smooth contours — never approximate with line segments. Any element can have `"construction": true`. Constraints use partial recovery.

## Style

- 4-space indent, type hints everywhere, `from __future__ import annotations`
- `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants
- Frozen dataclasses with `__slots__` for models
- `unittest` framework, tests in `tests/test_*.py`
- CAD tools return `{"ok": false, "error": {"code": "...", "message": "..."}}` on failure
- Socket protocol: newline-delimited JSON
- Commits: short imperative subjects, optional scope prefixes (`server:`, `addon:`)

## Policies (see .claude/rules/ for details)

- **ME preflight**: Skip for simple parts. Trigger for high-risk geometry. @.claude/rules/me-preflight.md
- **Motion validation**: User-initiated, human-gated. @.claude/rules/motion-validation.md
- **Parametric studies**: Only when user asks to optimize/explore. @.claude/rules/study-policy.md
- **Self-assessment**: Critically examine screenshots. Acknowledge uncertainty. @.claude/rules/self-assessment.md
- **Analysis (FEA/CFD)**: Skip for simple geometry. Trigger for load-bearing or aero-critical parts. @.claude/rules/analysis-policy.md
- **Sim engines**: Start on demand, persist across validation runs. @.claude/rules/sim-engine-policy.md
