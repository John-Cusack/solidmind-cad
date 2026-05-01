# Examples

Sample specs and an extension pack you can use to explore SolidMind CAD.

## `planetary_gearbox/` — End-to-end orchestrator demo (start here)

Six worker builds in a single orchestrator run, producing a complete
5:1 planetary gearbox (sun + 3 planets + carrier + ring gear). Walks
G0 → G5 against a live FreeCAD addon, with the orchestrator
independently re-measuring every produced STEP and validating against
frozen interface-control documents. ~30s end-to-end.

This is the v0.2.0 flagship — the multi-part counterpart to the
per-class verify-mode tests in `tests/test_orchestrator_real_worker_e2e.py`.

```bash
PYTHONPATH=. python3 examples/planetary_gearbox/run.py --out /tmp/gearbox
```

See [`planetary_gearbox/README.md`](planetary_gearbox/README.md) for
the narrative + hyperparameters.

## `hexapod_robot/` — 7-worker orchestrator demo

The biggest end-to-end build the v0.2.0 outer loop has run. Seven
worker dispatches in a single orchestrator pass: 1 chassis + 6
multi-segment legs, 18 revolute-joint pivot bores when assembled.
20 dimension checkpoints, all measured by the orchestrator's
independent re-import. ~30s end-to-end.

Two distinct build paths in one run: chassis routes through
`_build_envelope`, legs route through the new `_build_leg` (chunk 8).

```bash
PYTHONPATH=. python3 examples/hexapod_robot/run.py --out /tmp/hexapod
```

See [`hexapod_robot/README.md`](hexapod_robot/README.md) for the
narrative + the v0.3.0+ follow-up paths (assembly, URDF, Isaac, RL).

## `cnc/` — CNC spec progression

Four manufacturing spec JSON files demonstrating the **maturity level**
pattern used by `spec.*` and `mfg.*` tools:

| File | Level | What it shows |
|------|-------|---------------|
| `L1.json` | L1 – Concept | Envelope + process choice only. Interfaces and tolerances are open. |
| `L2.json` | L2 – Design | Critical features and interfaces locked in; tolerances tightened. |
| `L3.json` | L3 – Production | Full GD&T, inspection points, surface finish, documentation. |
| `sensor_bracket_L2.json` | L2 | A concrete real-world part (sensor mount bracket) at the design-freeze level — useful to see what a "real" L2 spec looks like vs. the abstract examples. |

Load these with `spec.select_schema` + `spec.validate` to see how the
design loop assesses maturity and suggests next questions.

## `print_3d/` — 3D-printing spec progression

Same L1 → L2 → L3 progression, but for FFF / SLA / MJF parts. The
`manufacturing` block in each file highlights what changes between
processes: tolerance class, required fillets, minimum wall thickness,
and anisotropy considerations.

## `solidmind-example-pack/` — Extension pack template

A working **combined extension pack** (tool pack + knowledge pack) you
can fork as the starting point for your own pack. It exposes:

- A simple sheet-metal bend-allowance calculator as an MCP tool
- A single markdown knowledge file (`knowledge/bend_basics.md`) that
  gets indexed when the pack is installed
- The `[project.entry-points."solidmind.tool_packs"]` +
  `[project.entry-points."solidmind.knowledge_packs"]` wiring that
  SolidMind CAD discovers at startup

Install it locally to see the pack discovery flow:

```bash
pip install -e examples/solidmind-example-pack
# then restart the MCP server — the new tool and knowledge appear
```

See [`docs/creating-packs.md`](../docs/creating-packs.md) for the full
extension-pack guide, including solver packs and combined packs.
