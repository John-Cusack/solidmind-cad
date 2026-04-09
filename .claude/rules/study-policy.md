# Parametric Study Policy

Use `study.*` tools only when the user asks to optimize, sweep, compare, or explore designs. Skip for specific builds with known dimensions.

## Workflow

1. `knowledge.search('<part_type> study')` — check for prior study notes FIRST
2. If prior notes: use optimal ranges, pin known-good values, drop insensitive variables
3. If none: research engineering references for variables and ranges
4. `study.create` → `study.run` → `study.status` → `study.results`
5. **Learning cycle (mandatory)**: write findings to `me_knowledge/notes/<part_type>_study_<date>.md`
6. `knowledge.ingest(path=...)` to index findings for future sessions
7. Build the winner with `cad.*`

## When Ambiguous

If user says "design a propeller" — ask: "Would you like to explore the design space or build a specific configuration?"
