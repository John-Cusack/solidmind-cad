"""Worker lifecycle manager — dispatches work via the configured mode.

Unifies subagent, claude_code (subprocess), and docker (A2A) execution
behind a single interface. The runner calls this module; it delegates
to the right backend.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.spec import (
    FailureCode,
    Interface,
    MasterSpec,
    Subsystem,
    SubsystemKind,
    WorkerMode,
    WorkerResult,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker task descriptor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerTask:
    """A single build task to dispatch."""

    subsystem: Subsystem
    interfaces: list[Interface]
    variant_index: int
    output_dir: Path
    prompt: str = ""


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def plan_tasks(spec: MasterSpec, run_dir: Path) -> list[WorkerTask]:
    """Create WorkerTask descriptors for all GENERATED subsystems."""
    from orchestrator.worker_subprocess import build_worker_prompt

    tasks: list[WorkerTask] = []
    for sub in spec.subsystems:
        if sub.kind != SubsystemKind.GENERATED:
            continue
        interfaces = spec.interfaces_for(sub.name)
        for vi in range(sub.worker_count):
            output_dir = run_dir / f"{sub.name}_{vi}" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prompt = build_worker_prompt(spec, sub, interfaces, str(output_dir))
            tasks.append(WorkerTask(
                subsystem=sub,
                interfaces=interfaces,
                variant_index=vi,
                output_dir=output_dir,
                prompt=prompt,
            ))
    return tasks


async def dispatch_all(
    spec: MasterSpec,
    tasks: list[WorkerTask],
    *,
    run_dir: Path,
    mode: WorkerMode = WorkerMode.SUBAGENT,
    max_parallel: int = 4,
    **kwargs: Any,
) -> list[WorkerResult]:
    """Dispatch all tasks via the configured worker mode.

    Args:
        spec: The master spec.
        tasks: Planned worker tasks.
        run_dir: Run directory.
        mode: Execution mode.
        max_parallel: Max concurrent workers.
        **kwargs: Mode-specific options (claude_binary, docker_image, etc.).

    Returns:
        List of WorkerResult for each task.
    """
    if mode == WorkerMode.SUBAGENT:
        return _dispatch_subagent(tasks)
    elif mode == WorkerMode.CLAUDE_CODE:
        return await _dispatch_claude_code(spec, tasks, run_dir=run_dir, max_parallel=max_parallel, **kwargs)
    elif mode == WorkerMode.DOCKER:
        return await _dispatch_docker(spec, tasks, run_dir=run_dir, max_parallel=max_parallel, **kwargs)
    else:
        raise ValueError(f"Unsupported worker mode: {mode}")


def _dispatch_subagent(tasks: list[WorkerTask]) -> list[WorkerResult]:
    """Subagent mode: return prompts for Claude Code to dispatch via Agent tool.

    In subagent mode, the orchestrator doesn't execute workers directly.
    It returns WorkerResults with status="pending" and the prompt stored
    so Claude Code can dispatch them as Agent tool calls.
    """
    results: list[WorkerResult] = []
    for task in tasks:
        results.append(WorkerResult(
            subsystem_name=task.subsystem.name,
            worker_id=f"{task.subsystem.name}_{task.variant_index}",
            status="pending",
        ))
    return results


async def _dispatch_claude_code(
    spec: MasterSpec,
    tasks: list[WorkerTask],
    *,
    run_dir: Path,
    max_parallel: int = 4,
    **kwargs: Any,
) -> list[WorkerResult]:
    """Claude Code mode: launch `claude --print` subprocesses."""
    from orchestrator.worker_subprocess import dispatch_workers

    subprocess_results = await dispatch_workers(
        spec,
        run_dir=run_dir,
        claude_binary=kwargs.get("claude_binary", "claude"),
        max_parallel=max_parallel,
    )

    results: list[WorkerResult] = []
    for sr in subprocess_results:
        status = "timeout" if sr.timed_out else ("success" if sr.returncode == 0 else "failed")
        failure_code = FailureCode.WORKER_TIMEOUT if sr.timed_out else (
            FailureCode.WORKER_TOOL_ERROR if sr.returncode != 0 else None
        )
        result = WorkerResult(
            subsystem_name=sr.subsystem_name,
            worker_id=f"{sr.subsystem_name}_{sr.worker_index}",
            status=status,
            error=sr.stderr if sr.returncode != 0 else None,
            failure_code=failure_code,
        )
        # Check for artifacts
        step_files = list(sr.output_dir.glob("*.step"))
        stl_files = list(sr.output_dir.glob("*.stl"))
        screenshots = list(sr.output_dir.glob("*.png"))
        if step_files:
            result.step_file = step_files[0]
        result.stl_files = stl_files
        result.screenshots = screenshots
        results.append(result)

    return results


async def _dispatch_docker(
    spec: MasterSpec,
    tasks: list[WorkerTask],
    *,
    run_dir: Path,
    max_parallel: int = 4,
    **kwargs: Any,
) -> list[WorkerResult]:
    """Docker mode: launch containers with A2A protocol."""
    from orchestrator.a2a_client import A2AClient

    worker_port = kwargs.get("worker_port", 8080)
    kwargs.get("docker_image", "solidmind-worker:latest")

    client = A2AClient(timeout_sec=900)
    sem = asyncio.Semaphore(max_parallel)

    async def _run_task(task: WorkerTask) -> WorkerResult:
        async with sem:
            worker_url = f"http://localhost:{worker_port + task.variant_index}"
            try:
                a2a_task = await client.submit_and_wait(
                    worker_url,
                    sub_spec=MasterSpec._sub_to_dict(task.subsystem),
                    interfaces=[MasterSpec._ifc_to_dict(i) for i in task.interfaces],
                    output_dir=task.output_dir,
                )
                status = "success" if a2a_task.status == "completed" else "failed"
                return WorkerResult(
                    subsystem_name=task.subsystem.name,
                    worker_id=f"{task.subsystem.name}_{task.variant_index}",
                    status=status,
                    error=a2a_task.error,
                )
            except Exception as exc:
                return WorkerResult(
                    subsystem_name=task.subsystem.name,
                    worker_id=f"{task.subsystem.name}_{task.variant_index}",
                    status="failed",
                    error=str(exc),
                    failure_code=FailureCode.WORKER_TOOL_ERROR,
                )

    coros = [_run_task(t) for t in tasks]
    results = list(await asyncio.gather(*coros))
    await client.close()
    return results


# ---------------------------------------------------------------------------
# Result assessment
# ---------------------------------------------------------------------------


def assess_results(results: list[WorkerResult]) -> tuple[bool, list[str]]:
    """Quick assessment: did all workers succeed?"""
    issues: list[str] = []
    for r in results:
        if r.status != "success":
            issues.append(f"{r.worker_id}: {r.status} — {r.error or 'unknown'}")
    return len(issues) == 0, issues
