"""Orchestrator runner — step-by-step execution that Claude Code drives.

This module provides the deterministic scaffolding for orchestration.
Claude Code (the main session) calls these functions via Python, then
uses its own Agent tool to dispatch workers as subagents.

Typical flow from Claude Code's perspective:

    # 1. Initialize
    run = orchestrator.runner.init_run("2-Stage Planetary Reducer", run_dir="runs/001")

    # 2. Council phase — Claude Code does this itself (reasoning + geometry.* tools)
    #    Then saves the spec:
    orchestrator.runner.save_spec(run, spec)

    # 3. Get worker prompts — deterministic, returns structured prompts
    prompts = orchestrator.runner.build_worker_prompts(run)
    #    Returns: [{"subsystem": "sun_gear", "prompt": "...", "description": "..."}, ...]

    # 4. Claude Code dispatches workers via Agent tool (parallel subagents)
    #    Each prompt becomes an Agent() call — Claude Code does this natively

    # 5. Validate results — deterministic
    report = orchestrator.runner.validate_results(run)

    # 6. Score and rank — deterministic + Claude Code reasoning
    ranking = orchestrator.runner.score_results(run, report)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.interface_freeze import freeze_interfaces as check_gate_g3
from orchestrator.release import check_gate_g7
from orchestrator.scorer import check_gate_g6
from orchestrator.skeleton import check_gate_g2
from orchestrator.spec import (
    MasterSpec,
    SpecStatus,
    SubsystemKind,
)
from orchestrator.state import StateMachine
from orchestrator.validator import check_gate_g5

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrchestratorRun:
    """Tracks a single orchestration run on disk."""

    run_id: str
    run_dir: Path
    spec: MasterSpec = field(default_factory=MasterSpec)
    state: StateMachine = field(default_factory=StateMachine)

    @property
    def spec_path(self) -> Path:
        return self.run_dir / "spec.yaml"

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"


# ---------------------------------------------------------------------------
# 1. Initialize
# ---------------------------------------------------------------------------


def init_run(
    name: str,
    *,
    run_dir: str | Path | None = None,
    description: str = "",
) -> OrchestratorRun:
    """Create a new orchestration run directory and return the run context."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if run_dir is None:
        run_dir = Path(f"runs/{run_id}")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = MasterSpec(name=name, description=description)
    run = OrchestratorRun(run_id=run_id, run_dir=run_dir, spec=spec)
    save_spec(run)
    _save_state(run)
    log.info("Initialized run %s at %s", run_id, run_dir)
    return run


def load_run(run_dir: str | Path) -> OrchestratorRun:
    """Load an existing run from disk."""
    run_dir = Path(run_dir)
    spec = MasterSpec.load(run_dir / "spec.yaml")
    state_path = run_dir / "state.json"
    state = StateMachine()
    if state_path.exists():
        state_data = json.loads(state_path.read_text())
        state.current = SpecStatus(state_data.get("current", "draft"))
    return OrchestratorRun(
        run_id=run_dir.name,
        run_dir=run_dir,
        spec=spec,
        state=state,
    )


# ---------------------------------------------------------------------------
# 2. Spec management
# ---------------------------------------------------------------------------


def save_spec(run: OrchestratorRun) -> Path:
    """Save the current spec to disk."""
    run.spec.save(run.spec_path)
    return run.spec_path


def transition(run: OrchestratorRun, to: SpecStatus, *, reason: str = "") -> None:
    """Advance the state machine and persist."""
    run.state.transition(to, reason=reason)
    run.spec.status = to
    save_spec(run)
    _save_state(run)


# ---------------------------------------------------------------------------
# 3. Worker prompt generation
# ---------------------------------------------------------------------------


def build_worker_prompts(run: OrchestratorRun) -> list[dict[str, Any]]:
    """Generate prompts for all GENERATED subsystems.

    Returns a list of dicts, each with:
        - subsystem: name
        - variant_index: 0-based
        - prompt: the full prompt string
        - description: short description for the Agent tool
        - output_dir: where artifacts should go

    Claude Code should dispatch each as an Agent() tool call.
    Multiple prompts can be dispatched in parallel.
    """
    from orchestrator.worker_subprocess import build_worker_prompt

    prompts = []
    for sub in run.spec.subsystems:
        if sub.kind != SubsystemKind.GENERATED:
            continue
        interfaces = run.spec.interfaces_for(sub.name)
        for variant_idx in range(sub.worker_count):
            output_dir = run.run_dir / f"{sub.name}_{variant_idx}" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            prompt = build_worker_prompt(run.spec, sub, interfaces, str(output_dir))
            prompts.append({
                "subsystem": sub.name,
                "variant_index": variant_idx,
                "prompt": prompt,
                "description": f"Build {sub.name} (variant {variant_idx})",
                "output_dir": str(output_dir),
            })
    return prompts


# ---------------------------------------------------------------------------
# 4. Result collection
# ---------------------------------------------------------------------------


def collect_worker_results(run: OrchestratorRun) -> list[dict[str, Any]]:
    """Scan run directory for worker outputs and return summary.

    Each worker variant directory should contain output/metadata.json,
    STEP files, STL files, and screenshots.
    """
    results = []
    for sub in run.spec.subsystems:
        if sub.kind != SubsystemKind.GENERATED:
            continue
        for variant_idx in range(sub.worker_count):
            variant_dir = run.run_dir / f"{sub.name}_{variant_idx}"
            output_dir = variant_dir / "output"
            result: dict[str, Any] = {
                "subsystem": sub.name,
                "variant_index": variant_idx,
                "output_dir": str(output_dir),
                "status": "missing",
            }
            if output_dir.exists():
                step_files = list(output_dir.glob("*.step"))
                stl_files = list(output_dir.glob("*.stl"))
                metadata_path = output_dir / "metadata.json"
                result["step_files"] = [str(f) for f in step_files]
                result["stl_files"] = [str(f) for f in stl_files]
                result["screenshots"] = [str(f) for f in output_dir.glob("*.png")]
                if metadata_path.exists():
                    result["metadata"] = json.loads(metadata_path.read_text())
                result["status"] = "complete" if step_files else "incomplete"
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# 5. Gate checks (deterministic)
# ---------------------------------------------------------------------------


def check_gate_g0(spec: MasterSpec) -> tuple[bool, list[str]]:
    """G0: Requirements completeness — every objective has direction + unit."""
    issues = []
    if not spec.objectives:
        issues.append("No objectives defined")
    for obj in spec.objectives:
        if not obj.direction:
            issues.append(f"Objective '{obj.name}' missing direction")
        if not obj.unit:
            issues.append(f"Objective '{obj.name}' missing unit")
    if not spec.global_constraints:
        issues.append("No global constraints defined")
    return len(issues) == 0, issues


def check_gate_g1(spec: MasterSpec) -> tuple[bool, list[str]]:
    """G1: Feasibility — budgets consistent, no dangling refs."""
    issues = []
    ok_mass, msg = spec.check_mass_budget()
    if not ok_mass:
        issues.append(f"Mass budget: {msg}")
    ok_refs, dangling = spec.check_dangling_refs()
    if not ok_refs:
        issues.append(f"Dangling interface refs: {dangling}")
    if not spec.subsystems:
        issues.append("No subsystems defined")
    return len(issues) == 0, issues


def check_gate_g4(run: OrchestratorRun) -> tuple[bool, list[str]]:
    """G4: Artifacts exist — STEP files present for all generated subsystems."""
    issues = []
    results = collect_worker_results(run)
    for r in results:
        if r["status"] != "complete":
            issues.append(
                f"{r['subsystem']}_{r['variant_index']}: "
                f"status={r['status']}"
            )
    return len(issues) == 0, issues


# ---------------------------------------------------------------------------
# Stage 5-7 wrappers (convenience for Claude Code / CLI callers)
# ---------------------------------------------------------------------------


def validate_results(
    run: OrchestratorRun,
    measurements: dict[str, dict[str, dict[str, float]]] | None = None,
) -> list:
    """Stage 5: Validate worker results against frozen contracts.

    Args:
        run: The orchestrator run.
        measurements: Optional dict of {worker_id: {ifc_id: {feature: mm}}}.
            If None, reads from metadata.json files in output dirs.

    Returns:
        List of ValidationReport objects.
    """
    from orchestrator.validator import validate_worker_result, ValidationReport

    reports: list[ValidationReport] = []
    results_data = collect_worker_results(run)

    for rd in results_data:
        if rd["status"] != "complete":
            continue
        from orchestrator.spec import WorkerResult
        worker_id = f"{rd['subsystem']}_{rd['variant_index']}"
        wr = WorkerResult(
            subsystem_name=rd["subsystem"],
            worker_id=worker_id,
            status="success",
        )
        worker_measurements = (measurements or {}).get(worker_id)
        measurement_source = "orchestrator" if worker_measurements is not None else "unknown"
        if worker_measurements is None:
            worker_measurements = {}

        # Try loading measurements from metadata.json
        metadata = rd.get("metadata", {})
        if not worker_measurements and metadata:
            worker_measurements = metadata.get("interface_actuals", {})
            if worker_measurements:
                measurement_source = "claimed"

        actual_bbox = metadata.get("claimed_bounding_box_mm") if metadata else None
        actual_mass = metadata.get("claimed_mass_kg") if metadata else None

        report = validate_worker_result(
            run.spec, wr,
            measurements=worker_measurements,
            actual_bbox_mm=actual_bbox,
            actual_mass_kg=actual_mass,
            measurement_source=measurement_source,
        )
        reports.append(report)

    return reports


def score_results(
    run: OrchestratorRun,
    validation_reports: list,
    *,
    beam_width: int = 5,
) -> object:
    """Stage 6: Score variants and rank assembly candidates via SBCE.

    Returns a ScoringReport with ranked candidates and Pareto frontier.
    """
    from orchestrator.scorer import score_run
    return score_run(run.spec, validation_reports, beam_width=beam_width, run_dir=run.run_dir)


def build_release(
    run: OrchestratorRun,
    *,
    scoring_report: object | None = None,
    validation_reports: list | None = None,
) -> object:
    """Stage 7: Build the release package.

    Returns a ReleasePackage.
    """
    from orchestrator.release import build_release_package
    return build_release_package(
        run.spec, run.run_dir,
        scoring_report=scoring_report,
        validation_reports=validation_reports,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _save_state(run: OrchestratorRun) -> None:
    """Persist state machine to disk."""
    data = {
        "current": run.state.current.value,
        "history": [
            {
                "timestamp": e.timestamp,
                "from_state": e.from_state,
                "to_state": e.to_state,
                "reason": e.reason,
                "failure_code": e.failure_code,
            }
            for e in run.state.history
        ],
        "retry_counts": run.state.retry_counts,
    }
    run.state_path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Summary for Claude Code
# ---------------------------------------------------------------------------


def dry_run(run: OrchestratorRun) -> Path:
    """Write worker prompts to disk for inspection without dispatching.

    Creates a prompts/ directory in the run dir with one markdown file
    per worker. Returns the prompts directory path.
    """
    prompts = build_worker_prompts(run)
    prompts_dir = run.run_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    index_lines = ["# Dry-Run Worker Prompts", ""]
    for p in prompts:
        filename = f"{p['subsystem']}_{p['variant_index']}.md"
        prompt_path = prompts_dir / filename
        prompt_path.write_text(p["prompt"])

        index_lines.append(f"- **{p['description']}**: [{filename}]({filename})")
        index_lines.append(f"  Output dir: `{p['output_dir']}`")
        index_lines.append(f"  Prompt length: {len(p['prompt'])} chars")
        index_lines.append("")

    index_lines.append(f"\nTotal workers: {len(prompts)}")
    (prompts_dir / "INDEX.md").write_text("\n".join(index_lines))

    log.info("Dry-run: wrote %d prompts to %s", len(prompts), prompts_dir)
    return prompts_dir


def format_dispatch_instructions(prompts: list[dict[str, Any]]) -> str:
    """Format instructions telling Claude Code how to dispatch workers.

    Returns a markdown string that Claude Code can follow to launch
    Agent tool calls.
    """
    lines = [
        f"## Worker Dispatch: {len(prompts)} worker(s) to launch",
        "",
        "Launch these as **parallel Agent tool calls**. Each agent needs "
        "access to all `mcp__solidmind-cad__*` tools.",
        "",
    ]
    for p in prompts:
        lines.append(f"### {p['description']}")
        lines.append(f"- Output dir: `{p['output_dir']}`")
        lines.append(f"- Prompt length: {len(p['prompt'])} chars")
        lines.append("")
    lines.append(
        "After all workers complete, run: "
        "`orchestrator.runner.collect_worker_results(run)` to gather results."
    )
    return "\n".join(lines)
