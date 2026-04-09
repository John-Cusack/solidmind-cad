# Examples

Sample specs and an extension pack you can use to explore SolidMind CAD.

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
