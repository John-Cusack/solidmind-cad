# Analysis Policy (FEA / CFD)

## When to Run

**Skip** for:
- Simple geometry (spacers, brackets, blocks) with no load requirements
- Parts under trivial static loads well within material limits
- User explicitly says "skip analysis"

**Trigger** for:
- Load-bearing parts (shafts, frames, mounts) with specified forces/torques
- Aero-critical parts (wings, rotors, propellers, fairings)
- Parts operating near material limits or at elevated temperatures
- User requests stress check, FEA, CFD, or structural validation
- After motion.simulate reveals high joint forces (Tier 3.5 flow)

## Solver Selection

| Problem | Solver | Tool |
|---------|--------|------|
| Static stress / deflection | CalculiX | `analysis.stress_check` |
| Dynamic stress from sim forces | CalculiX | `analysis.stress_from_simulation` |
| External aerodynamics (RANS) | SU2 | `analysis.aero_check` |
| Rotor aerodynamics (vortex) | DUST | `analysis.aero_check` (rotor mode) |
| Coupled aero + structural | SU2/DUST → CalculiX | `analysis.coupled_check` |
| Custom / extension solvers | Solver packs | `analysis.stress_check` with solver override |

## Material Selection

- Use `analysis.list_materials` to show available materials before running.
- Default to steel (structural) or aluminum (aerospace) if user doesn't specify.
- Always confirm material choice with the user for critical parts.

## Safety Factor Interpretation

| Factor of Safety | Meaning |
|-----------------|---------|
| < 1.0 | **Failure** — redesign required |
| 1.0 – 1.5 | Marginal — acceptable only for weight-critical aerospace with well-known loads |
| 1.5 – 2.5 | Typical — good for most mechanical parts |
| 2.5 – 4.0 | Conservative — appropriate for uncertain loads or safety-critical parts |
| > 4.0 | Over-designed — consider weight/cost reduction unless required by spec |

## Rules

- **Never auto-run.** Suggest: "Would you like me to run a stress check on this part?"
- Present solver + material + boundary conditions to user before running.
- Report peak stress, safety factor, and deflection. Flag any FoS < 1.5.
- For unfamiliar load cases, check `me_knowledge/notes/` first.
