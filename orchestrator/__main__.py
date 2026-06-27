"""CLI entry point: python -m orchestrator "design goal"

Runs the full pipeline in headless/CI mode (claude_code worker mode).
For interactive use, Claude Code IS the orchestrator (subagent mode)
and calls orchestrator.runner directly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from orchestrator.config import load_config
from orchestrator.runner import (
    OrchestratorRun,
    check_gate_g0,
    check_gate_g1,
    check_gate_g2,
    check_gate_g3,
    check_gate_g4,
    init_run,
    save_spec,
    transition,
)
from orchestrator.spec import MasterSpec, SpecStatus, WorkerMode

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Multi-agent CAD orchestrator",
    )
    parser.add_argument(
        "goal",
        nargs="?",
        help="Design goal (natural language)",
    )
    parser.add_argument(
        "--spec",
        "-s",
        type=Path,
        help="Path to an existing master_spec.yaml to resume from",
    )
    parser.add_argument(
        "--run-dir",
        "-d",
        type=Path,
        help="Run directory (default: auto-generated under runs/)",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        help="Path to orchestrator config YAML",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate worker prompts without dispatching",
    )
    parser.add_argument(
        "--max-parallel",
        "-j",
        type=int,
        default=4,
        help="Max parallel workers (default: 4)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.spec:
        return _resume_from_spec(args)
    elif args.goal:
        return _new_run(args)
    else:
        parser.print_help()
        return 1


def _new_run(args: argparse.Namespace) -> int:
    """Start a new orchestration run from a design goal."""
    cfg = load_config(args.config)

    run = init_run(
        args.goal,
        run_dir=args.run_dir,
        description=args.goal,
    )
    run.spec.worker_mode = WorkerMode(cfg.worker_mode)
    save_spec(run)

    print(f"Initialized run: {run.run_dir}")
    print(f"Worker mode: {run.spec.worker_mode.value}")
    print()
    print("Run is initialized but spec is empty.")
    print("In headless mode, populate the spec YAML manually, then resume with:")
    print(f"  python -m orchestrator --spec {run.spec_path}")
    return 0


def _resume_from_spec(args: argparse.Namespace) -> int:
    """Resume from an existing spec YAML — run gates and dispatch."""
    from orchestrator.runner import (
        dry_run,
        load_run,
    )

    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"Spec not found: {spec_path}", file=sys.stderr)
        return 1

    # Load or create run
    if spec_path.parent.name != "runs" and (spec_path.parent / "state.json").exists():
        run = load_run(spec_path.parent)
    else:
        run = init_run(
            "Resumed run",
            run_dir=args.run_dir,
        )
        run.spec = MasterSpec.load(spec_path)
        save_spec(run)

    print(f"Loaded spec: {run.spec.name} (status: {run.spec.status.value})")
    print(f"Subsystems: {len(run.spec.subsystems)}")
    print(f"Interfaces: {len(run.spec.interfaces)}")
    print()

    # Run gates
    gates = [
        ("G0 (Requirements)", check_gate_g0),
        ("G1 (Feasibility)", check_gate_g1),
        ("G2 (Skeleton)", check_gate_g2),
        ("G3 (ICD)", check_gate_g3),
    ]

    for gate_name, gate_fn in gates:
        ok, issues = gate_fn(run.spec)
        status = "PASS" if ok else "FAIL"
        print(f"  {gate_name}: {status}")
        if not ok:
            for issue in issues:
                print(f"    - {issue}")
            print(f"\nBlocked at {gate_name}. Fix the spec and retry.")
            return 1

    if args.dry_run:
        prompts_dir = dry_run(run)
        print(f"\nDry run: prompts written to {prompts_dir}")
        return 0

    # Dispatch workers
    if run.spec.worker_mode == WorkerMode.CLAUDE_CODE:
        return asyncio.run(_dispatch_headless(run, args.max_parallel))
    else:
        print(
            f"\nWorker mode '{run.spec.worker_mode.value}' requires interactive Claude Code session."
        )
        print("Use: python -m orchestrator --dry-run --spec ... to preview prompts.")
        return 0


async def _dispatch_headless(run: OrchestratorRun, max_parallel: int) -> int:
    """Run workers in headless mode using claude --print."""
    from orchestrator.worker import assess_results, dispatch_all, plan_tasks

    transition(run, SpecStatus.BUILDING, reason="dispatching workers")

    tasks = plan_tasks(run.spec, run.run_dir)
    print(f"\nDispatching {len(tasks)} worker(s)...")

    results = await dispatch_all(
        run.spec,
        tasks,
        run_dir=run.run_dir,
        mode=WorkerMode.CLAUDE_CODE,
        max_parallel=max_parallel,
    )

    ok, issues = assess_results(results)
    if ok:
        print("\nAll workers completed successfully.")
        transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="workers complete")
    else:
        print("\nWorker failures:")
        for issue in issues:
            print(f"  - {issue}")
        transition(run, SpecStatus.FAILED, reason="worker failures")
        return 1

    # G4 check
    ok_g4, g4_issues = check_gate_g4(run)
    if ok_g4:
        print("  G4 (Artifacts): PASS")
    else:
        print("  G4 (Artifacts): FAIL")
        for issue in g4_issues:
            print(f"    - {issue}")
        return 1

    # --- Stage 5: Geometry + Assembly Validation ---
    from orchestrator.runner import build_release, score_results, validate_results
    from orchestrator.validator import check_gate_g5, save_validation_report

    print("\n--- Stage 5: Validation ---")
    validation_reports = validate_results(run)

    ok_g5, g5_issues = check_gate_g5(run.spec, validation_reports)
    if ok_g5:
        print("  G5 (Validation): PASS")
    else:
        print("  G5 (Validation): FAIL")
        for issue in g5_issues:
            print(f"    - {issue}")
        # Save report for debugging even on failure
        save_validation_report(validation_reports, run.run_dir / "validation_report.json")
        transition(run, SpecStatus.FAILED, reason="validation failures")
        return 1

    save_validation_report(validation_reports, run.run_dir / "validation_report.json")
    transition(run, SpecStatus.SCORING, reason="validation passed")

    # --- Stage 6: Scoring + SBCE ---
    from orchestrator.scorer import check_gate_g6, save_scoring_report

    print("\n--- Stage 6: Scoring ---")
    scoring_report = score_results(run, validation_reports)

    ok_g6, g6_issues = check_gate_g6(run.spec, scoring_report)
    if ok_g6:
        print("  G6 (Scoring): PASS")
    else:
        print("  G6 (Scoring): FAIL")
        for issue in g6_issues:
            print(f"    - {issue}")

    save_scoring_report(scoring_report, run.run_dir / "scoring_report.json")

    if scoring_report.winner_index is not None:
        print(f"  Winner: candidate #{scoring_report.winner_index}")
        print(f"  Frontier: {len(scoring_report.frontier)} candidate(s)")

    transition(run, SpecStatus.RELEASE_PACKAGING, reason="scoring complete")

    # --- Stage 7: Release Package ---
    from orchestrator.release import check_gate_g7

    print("\n--- Stage 7: Release ---")
    package = build_release(
        run,
        scoring_report=scoring_report,
        validation_reports=validation_reports,
    )

    ok_g7, g7_issues = check_gate_g7(package, spec=run.spec)
    if ok_g7:
        print("  G7 (Release): PASS")
    else:
        print("  G7 (Release): FAIL")
        for issue in g7_issues:
            print(f"    - {issue}")

    transition(run, SpecStatus.AWAITING_HUMAN, reason="release package ready")

    print(f"\nPipeline complete. Release package: {package.package_dir}")
    print(f"Run directory: {run.run_dir}")
    print("\nAwaiting human review. Accept with manual state transition to DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
