# Normalizer Prompt

You are normalizing the user's design request into a structured objective sheet.

## Output Format

Return a YAML block with the following structure:

```yaml
objectives:
  - name: <metric_name>
    direction: minimize | maximize
    unit: <SI_unit>
    weight: <0.0–1.0>
    threshold: <hard_limit_or_null>

global_constraints:
  max_mass_kg: <number>
  max_envelope_mm: [W, H, D]
  # ... domain-specific constraints

process_assumptions:
  - "<assumption about manufacturing, materials, or environment>"

duty_cycle: "<operating conditions summary>"

notes: "<any clarifications or open questions>"
```

## Validation Rules

1. Every objective MUST have `name`, `direction`, and `unit`.
2. `direction` must be exactly `"minimize"` or `"maximize"`.
3. No two objectives may share the same `name`.
4. `global_constraints` must contain at least one entry.
5. `weight` defaults to 1.0 if omitted.
6. `threshold` is optional — when set, it defines a hard pass/fail gate.

## Example

```yaml
objectives:
  - name: mass
    direction: minimize
    unit: kg
    weight: 1.0
    threshold: 0.5
  - name: max_stress
    direction: minimize
    unit: MPa
    weight: 0.8

global_constraints:
  max_mass_kg: 0.5
  max_envelope_mm: [100, 100, 50]
  material_family: steel

process_assumptions:
  - CNC milling available
  - Surface finish Ra 1.6 µm achievable

duty_cycle: "Continuous rotation at 1500 RPM, ambient 20–40°C"
```
