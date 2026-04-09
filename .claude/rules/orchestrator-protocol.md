# Orchestrator Protocol (for Claude Code sessions)

When the user asks you to design a complex assembly (3+ parts, mechanisms, multi-body), you ARE the orchestrator. Use the `orchestrator.runner` module for deterministic logic and your own Agent tool for parallel worker dispatch.

## Execution Modes

| Mode | When | How |
|------|------|-----|
| **subagent** (MVP) | Interactive session, Max plan | You dispatch Agent tool calls |
| **claude_code** | Headless/CI, `python -m orchestrator` | `claude --print` subprocesses |
| **docker** | Isolated builds | Containers with A2A protocol |

## How to Orchestrate (subagent mode)

### Phase 1: Council (you do this directly)
```python
import orchestrator.runner as orch

# Initialize run
run = orch.init_run("Assembly Name", description="...")
orch.transition(run, SpecStatus.NORMALIZING, reason="starting")
```

- Normalize requirements into objectives (direction, unit, threshold)
- Decompose into subsystems using engineering reasoning + `geometry.*` tools
- Build the MasterSpec programmatically
- Run gate checks: `orch.check_gate_g0(run.spec)`, `orch.check_gate_g1(run.spec)`
- Present to user for approval at each gate

### Phase 2: Layout + Interface Freeze (you do this directly)
- Define assembly skeleton (datums, axes, volumes)
- Dimension every interface with frames, tolerances, validation
- Run `orch.check_gate_g3(run.spec)` to verify ICDs complete
- Present to user for approval

### Phase 3: Worker Dispatch (use Agent tool)
```python
# Get the prompts
prompts = orch.build_worker_prompts(run)
orch.transition(run, SpecStatus.BUILDING, reason="dispatching workers")
```

Then launch **parallel Agent tool calls** — one per prompt:

```
# For each prompt in prompts, launch an Agent:
Agent(
    description=prompt["description"],
    prompt=prompt["prompt"],
    run_in_background=True,  # parallel execution
)
```

IMPORTANT: Launch ALL workers in a SINGLE message with multiple Agent tool
calls. This runs them in parallel. Do not launch them sequentially.

### Phase 4: Validation (you do this directly)
```python
results = orch.collect_worker_results(run)
gate_ok, issues = orch.check_gate_g4(run)
```

- Import STEP files, measure interfaces with `cad_measure_between`
- Check clearances with `cad_check_clearance`
- Run `cad_assembly_audit` for overall health
- Present validation report to user

### Phase 5: Report
- Score candidates, rank by objectives
- Present Pareto frontier to user
- User selects winner

## Key Rules

1. **You are the orchestrator** — don't ask the user to run Python scripts
2. **Workers are Agent subagents** — they get their own context and MCP access
3. **Gates require human approval** — present findings, wait for user OK
4. **Spec is the contract** — persist to disk with `orch.save_spec(run)`
5. **Measure, don't trust** — always verify worker output with orchestrator-side measurement
