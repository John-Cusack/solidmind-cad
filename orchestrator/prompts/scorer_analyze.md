# Scorer Analysis Prompt

You are scoring worker-built variants for the **{assembly_name}** assembly.

## Objectives
{objectives_text}

## Scoring Rules

For each variant:
1. Extract measured values from worker metadata
2. Compare against objective thresholds (hard gates)
3. Compute weighted score using direction and weight
4. Flag any interface dimension mismatches

## Output Format

Return a structured scoring report:
```yaml
variants:
  - subsystem: <name>
    variant_index: <int>
    scores:
      <objective_name>: <float>
    pass: true | false
    issues: []
ranking: [<variant_id>, ...]
```
