# Council Decomposition Prompt

You are decomposing the design into subsystems using a Design Structure Matrix (DSM).

## Inputs
- Normalized goal: `{goal_yaml}`
- Known engineering constraints and manufacturing rules

## Process

### Step 1: Enumerate Components
List every functional component needed:
- Custom geometry parts (gears, shafts, housings, brackets)
- Purchased parts (bearings, seals, motors)
- Standard hardware (fasteners, O-rings, dowel pins)

### Step 2: Build the N-Squared Matrix
For each component pair, score interaction strength (0.0–1.0):
- **1.0** = shared geometry or tight tolerance coupling (gear mesh, press fit)
- **0.7** = load path or thermal coupling (bearing mount, heat sink)
- **0.3** = spatial proximity only (adjacent parts, wire routing)
- **0.0** = no interaction

### Step 3: Classify Each Component
| Condition | Kind | Action |
|-----------|------|--------|
| Custom geometry needed | `generated` | Worker builds it |
| Specific supplier part | `catalog` | Lock by part number |
| Off-the-shelf standard | `standard` | Lock by standard designation |

### Step 4: Cluster
Group tightly-coupled components (threshold ≥ 0.5) into subsystems.
Each cluster becomes one worker package. Loosely-coupled cross-cluster
interactions become frozen interface contracts.

### Step 5: Size Each Subsystem
For each `generated` subsystem, determine:
- `envelope_mm`: bounding box [W, H, D]
- `mass_budget_kg`: allocated mass
- `material`: material specification
- `complexity_class`: S (simple), M (moderate), L (complex)

For `catalog`/`standard` subsystems:
- `supplier_part` or `standard` designation
- `quantity`

## Output Format

Return YAML with:
```yaml
subsystems:
  - name: <unique_name>
    kind: generated | catalog | standard
    description: <what it does>
    envelope_mm: [W, H, D]
    mass_budget_kg: <float>
    material: <string>
    complexity_class: S | M | L
    specs: {<domain-specific key-value pairs>}
    # For catalog:
    supplier_part: <part number>
    # For standard:
    standard: <standard designation>
    quantity: <int>

dsm_entries:
  - component_a: <name>
    component_b: <name>
    interaction_type: <gear_mesh|cylindrical_fit|bolt_pattern|thermal|assembly_sequence>
    strength: <0.0-1.0>

clusters:
  - [<component_a>, <component_b>]
  - [<component_c>]
```
