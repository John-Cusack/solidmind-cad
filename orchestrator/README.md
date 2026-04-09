# orchestrator/

Deterministic scaffolding for multi-worker assembly orchestration. Claude Code (or a headless CLI) drives the state machine; workers are parallel subagents that build individual parts against frozen interface contracts.

## Module Map

| Module | Purpose |
|--------|---------|
| `spec.py` | Data model — `MasterSpec`, `Subsystem`, `Interface`, `WorkerResult`, `SpecStatus` enum |
| `state.py` | State machine — valid transitions, failure-code retry routing |
| `runner.py` | Top-level API — `init_run`, `save_spec`, `build_worker_prompts`, gate checks, stage wrappers |
| `normalizer.py` | Stage 0 — normalize user goals into structured objectives |
| `council.py` | Stage 1 — architecture decomposition and feasibility |
| `skeleton.py` | Stage 2 — assembly skeleton / layout freeze, G2 check |
| `interface_freeze.py` | Stage 3 — ICD completeness + purchased-part lock, G3 check |
| `worker.py` | Stage 4 — worker task planning and dispatch (`dispatch_all`) |
| `worker_subprocess.py` | Worker prompt formatting and `claude --print` subprocess driver |
| `validator.py` | Stage 5 — geometry + assembly validation against frozen contracts, G5 check |
| `scorer.py` | Stage 6 — SBCE scoring, Pareto frontier, G6 check |
| `sbce.py` | Set-Based Concurrent Engineering — candidate enumeration and beam search |
| `release.py` | Stage 7 — release package (BOM, ICDs, provenance), G7 check |
| `config.py` | Runtime configuration (worker mode, parallelism, timeouts) |
| `cost.py` | Token/cost tracking for worker runs |
| `dsm.py` | Design Structure Matrix for subsystem dependency analysis |
| `knowledge.py` | Knowledge integration helpers |
| `providers.py` | Worker provider abstraction (subagent, claude_code, docker) |
| `__main__.py` | CLI entry point — `python -m orchestrator` |

## Gate System

| Gate | Stage | What it checks | Module |
|------|-------|---------------|--------|
| G0 | Requirements | Every objective has direction + unit | `runner.py` |
| G1 | Council | Mass budgets coherent, no dangling refs | `runner.py` |
| G2 | Skeleton | Datums, volumes, keepouts complete | `skeleton.py` |
| G3 | ICD Freeze | Interfaces complete, purchased parts locked | `interface_freeze.py` |
| G4 | Workers | STEP artifacts exist for all generated subsystems | `runner.py` |
| G5 | Validation | Geometry + assembly dimensionally compliant | `validator.py` |
| G6 | Scoring | At least one candidate meets all hard thresholds | `scorer.py` |
| G7 | Release | Release package has BOM, ICDs, provenance | `release.py` |

## Entry Points

**Programmatic (interactive session):** Claude Code imports `orchestrator.runner` directly:

```python
from orchestrator.runner import init_run, save_spec, build_worker_prompts, transition

run = init_run("Assembly Name", run_dir="runs/001")
# ... build spec, run gates, dispatch workers via Agent tool ...
```

**CLI (headless/CI):**

```bash
python -m orchestrator "design goal"                    # new run
python -m orchestrator --spec runs/001/spec.yaml        # resume
python -m orchestrator --dry-run --spec runs/001/spec.yaml  # preview prompts
```

## Further Reading

- [`.claude/rules/orchestrator-protocol.md`](../.claude/rules/orchestrator-protocol.md) — how Claude Code drives this module (dispatches workers via the `Agent` tool)
- [`docs/orchestrator-plan-cad-rewrite.md`](../docs/orchestrator-plan-cad-rewrite.md) — authoritative pipeline spec
- [`docs/orchestrator-7-arch-fixes.md`](../docs/orchestrator-7-arch-fixes.md) — architecture review
