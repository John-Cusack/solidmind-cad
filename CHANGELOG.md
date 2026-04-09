# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `docs/ROADMAP.md` — per-step gap analysis of the autonomous iteration loop against its textbook pedigree. The loop is now modeled as nine steps: Specify → Synthesize → Reflect → Screen → Simulate → Interpret → Decide → Act → Learn. Six of the nine map directly onto Shigley / Pahl & Beitz / Ullman / Dieter; three (Reflect, Screen-as-first-class, Learn) are senior-engineer folklore the textbooks assume rather than teach. Each step has a status marker, tool inventory, test coverage summary, and concrete "move ◐ to ✓" actions.
- `tests/test_iteration_loop_e2e.py` — skipped placeholder for the end-to-end loop-closure test. The docstring walks the nine steps on a deliberately under-dimensioned hip bracket and lists the four dependencies that have to land before the test can unskip.
- README now leads with the autonomous-iteration thesis (LLM builds → sims → fixes → repeats) and includes an honest "Where it's going" section built around the nine-step loop table.
- "What it does today" section replaces the old linear Demo walkthrough with an iteration-cycle walkthrough (v1 build → sim failure → fix → re-sim → stress check → teleop).
- FreeCAD 1.1 support. `compat.IS_V1_1_PLUS` flag for future version-specific branches. Joint type indices verified against FreeCAD 1.1's `JointObject.JointTypes` (exact match with existing `_JOINT_TYPE_INDEX`).
- `pyproject.toml` metadata for public release: `authors`, `keywords`, `classifiers`, `[project.urls]`, plus `orchestrator` and expanded `dev` extras. Conservative `[tool.ruff]` lint config.
- `.github/` scaffolding: bug / feature / config issue templates, pull request template, Dependabot config.
- CI: Ruff lint job (non-blocking for now), Python version matrix scaffold, `pydantic` added to test deps.
- README CI / License / Python / FreeCAD badges.
- Docker E2E tests now skip cleanly when the optional `httpx` extra is missing (`pip install -e .[orchestrator]`).

### Changed
- FreeCAD 1.1 is now the recommended runtime (1.0.2 remains supported via the existing compat layer). README and CONTRIBUTING install steps updated.
- Security reporting now points at GitHub Security Advisories instead of a placeholder `security@solidmind.dev` email. Same change in `CODE_OF_CONDUCT.md`.
- `.gitignore` tightened to catch `*.AppImage`, `*.mp4`, `docs/demo_clips/`, `training_runs/**`, `analyses/`, `watch_*anim*.json`, `type_prompt.sh`, CalculiX solver run artifacts (`*.cvg`, `*.dat`, `*.sta`, `--version.*`), and `requirements-backup.txt`. Added `!docs/images/*.png` exception so README illustrations can be committed.

### Removed
- Bundled knowledge content under `me_knowledge/notes` and `me_knowledge/sim_changes` from source control; repository now tracks placeholders only.
