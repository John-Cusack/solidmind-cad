# ME Preflight Policy

- Skip for simple geometry (spacers, brackets, blocks) unless user asks.
- Trigger for high-risk/specialized: rotors, turbines, gears, high-temp, explicit signoff needed.
- Don't rerun `me.design_loop` after every edit — only when requirements materially change.
- LLM constructs constraint dicts from engineering knowledge + research, passes to `me.validate_constraints`.
- For unfamiliar geometry: check `me_knowledge/notes/`, then search engineering references.
- Write findings to `me_knowledge/notes/<topic_slug>.md` for future sessions.
