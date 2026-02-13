"""MCP tool implementations for parametric design optimization studies."""
from __future__ import annotations

import itertools
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("solidmind.tools_study")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))

from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
)
from server.study_solvers import get_solver
from server.study_store import (
    delete_study,
    list_studies,
    load_study,
    save_study,
    study_exists,
)


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def study_create(
    name: str,
    variables: list[dict[str, Any]],
    solver: dict[str, Any],
    objective: dict[str, Any],
    fixed_params: dict[str, Any] | None = None,
    geometry_script: str | None = None,
) -> dict[str, Any]:
    """Define a new parametric study. Returns study_id and execution plan.

    For solvers that need 3D geometry (openfoam), provide geometry_script —
    a FreeCAD Python script that reads params JSON from sys.argv[1] and
    exports STL to sys.argv[2]. The script is stored with the study.
    """
    if _TOOL_LOG:
        log.info("CALL study_create name=%r solver=%s vars=%d", name, solver.get("solver_type"), len(variables))
    t0 = time.monotonic()

    if not name:
        return _error_result("INVALID_INPUT", "Study name is required")
    if not variables:
        return _error_result("INVALID_INPUT", "At least one design variable is required")

    try:
        dvars = [DesignVariable.from_dict(v) for v in variables]
    except (KeyError, TypeError) as exc:
        return _error_result("INVALID_INPUT", f"Invalid variable definition: {exc}")

    try:
        solver_cfg = SolverConfig.from_dict(solver)
    except (KeyError, TypeError) as exc:
        return _error_result("INVALID_INPUT", f"Invalid solver config: {exc}")

    try:
        obj_cfg = ObjectiveConfig.from_dict(objective)
    except (KeyError, TypeError) as exc:
        return _error_result("INVALID_INPUT", f"Invalid objective config: {exc}")

    # Validate solver availability
    try:
        s = get_solver(solver_cfg.solver_type)
    except KeyError as exc:
        return _error_result("UNKNOWN_SOLVER", str(exc))

    # Validate params against solver
    errors = s.validate_params(
        params={},
        fixed=fixed_params or {},
        config_params=solver_cfg.params,
    )
    if errors:
        return _error_result("SOLVER_VALIDATION", "; ".join(errors))

    study = Study(
        id=Study.new_id(),
        name=name,
        variables=dvars,
        solver=solver_cfg,
        objective=obj_cfg,
        fixed_params=fixed_params or {},
    )

    # Save study first to create the directory
    save_study(study)

    # Store geometry script with the study if provided
    if geometry_script:
        from server.study_store import _root  # noqa: PLC0415
        script_path = _root() / study.id / "geometry.py"
        script_path.write_text(geometry_script)
        # Update solver config to point to the stored script
        study.solver = SolverConfig(
            solver_type=solver_cfg.solver_type,
            params={**solver_cfg.params, "geometry_script": str(script_path)},
            timeout_s=solver_cfg.timeout_s,
            geometry_script=str(script_path),
        )
        save_study(study)

    # Preview coarse variant count
    counts = [len(v.expand_coarse()) for v in dvars]
    coarse_count = 1
    for c in counts:
        coarse_count *= c

    # Estimate refined variant count (assuming ~5 fine steps per variable)
    refined_per_var = [min(5, c) for c in counts]
    refined_count = 1
    for r in refined_per_var:
        refined_count *= r

    # Time estimates
    est_per_variant = s.estimate_per_variant_s(solver_cfg.params)
    coarse_time_s = coarse_count * est_per_variant
    refined_time_s = refined_count * est_per_variant
    total_time_s = coarse_time_s + refined_time_s

    save_study(study)

    if _TOOL_LOG:
        log.info("OK   study_create %.3fs id=%s coarse=%d", time.monotonic() - t0, study.id, coarse_count)

    return {
        "ok": True,
        "study_id": study.id,
        "execution_plan": {
            "solver": solver_cfg.solver_type,
            "pipeline_per_variant": s.describe_pipeline(),
            "phase_1_coarse": {
                "variant_count": coarse_count,
                "est_per_variant_s": est_per_variant,
                "est_total_s": round(coarse_time_s, 1),
                "est_total_human": _format_duration(coarse_time_s),
            },
            "phase_2_refined": {
                "variant_count_estimate": refined_count,
                "est_per_variant_s": est_per_variant,
                "est_total_s": round(refined_time_s, 1),
                "est_total_human": _format_duration(refined_time_s),
                "note": "Refined count depends on coarse results — this is an estimate.",
            },
            "total_est_s": round(total_time_s, 1),
            "total_est_human": _format_duration(total_time_s),
        },
        "variable_expansions": {
            v.name: {"count": counts[i], "values": dvars[i].expand_coarse()}
            for i, v in enumerate(dvars)
        },
    }


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        mins = seconds / 60
        return f"{mins:.1f} min"
    hours = seconds / 3600
    mins = (seconds % 3600) / 60
    if mins < 1:
        return f"{hours:.1f} hrs"
    return f"{hours:.0f} hrs {mins:.0f} min"


def study_run(study_id: str) -> dict[str, Any]:
    """Spawn the background study runner subprocess. Returns PID."""
    if _TOOL_LOG:
        log.info("CALL study_run id=%s", study_id)
    if not study_exists(study_id):
        return _error_result("NOT_FOUND", f"Study {study_id!r} not found")

    study = load_study(study_id)
    if study.status not in (StudyStatus.DRAFT, StudyStatus.FAILED, StudyStatus.CANCELLED):
        return _error_result(
            "INVALID_STATE",
            f"Study is in state {study.status.value!r}, cannot start",
        )

    # Reset status for re-run
    study.status = StudyStatus.DRAFT
    study.coarse_variants = []
    study.refined_variants = []
    study.best_variant_id = None
    study.error = None
    save_study(study)

    proc = subprocess.Popen(
        [sys.executable, "-m", "server.study_runner", study_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Record PID
    study.pid = proc.pid
    save_study(study)

    if _TOOL_LOG:
        log.info("OK   study_run pid=%d", proc.pid)
    return {"ok": True, "study_id": study_id, "pid": proc.pid}


def study_status(study_id: str) -> dict[str, Any]:
    """Poll study progress with elapsed time and ETA."""
    if _TOOL_LOG:
        log.info("CALL study_status id=%s", study_id)
    if not study_exists(study_id):
        return _error_result("NOT_FOUND", f"Study {study_id!r} not found")

    study = load_study(study_id)
    coarse_total = len(study.coarse_variants)
    refined_total = len(study.refined_variants)
    coarse_done = sum(1 for v in study.coarse_variants if v.status in ("done", "failed"))
    refined_done = sum(1 for v in study.refined_variants if v.status in ("done", "failed"))

    result: dict[str, Any] = {
        "ok": True,
        "study_id": study.id,
        "name": study.name,
        "status": study.status.value,
        "coarse_progress": f"{coarse_done}/{coarse_total}",
        "refined_progress": f"{refined_done}/{refined_total}",
        "best_variant_id": study.best_variant_id,
    }

    # Timing info
    if study.started_at is not None:
        now = study.finished_at or time.time()
        elapsed = now - study.started_at
        result["elapsed_s"] = round(elapsed, 1)
        result["elapsed_human"] = _format_duration(elapsed)

        # Compute avg time per variant and ETA from completed variants
        done_variants = [
            v for v in study.coarse_variants + study.refined_variants
            if v.status == "done" and v.solver_time_s > 0
        ]
        if done_variants:
            avg_s = sum(v.solver_time_s for v in done_variants) / len(done_variants)
            result["avg_per_variant_s"] = round(avg_s, 2)

            # Estimate remaining work
            remaining = 0
            if study.status == StudyStatus.RUNNING_COARSE:
                remaining = (coarse_total - coarse_done) + refined_total
            elif study.status == StudyStatus.RUNNING_REFINED:
                remaining = refined_total - refined_done

            if remaining > 0:
                eta_s = remaining * avg_s
                result["eta_s"] = round(eta_s, 1)
                result["eta_human"] = _format_duration(eta_s)

    if study.error:
        result["error"] = study.error
    if study.pid is not None:
        result["pid"] = study.pid
    return result


def study_results(
    study_id: str,
    top_n: int = 10,
    phase: str | None = None,
    sort_by: str | None = None,
) -> dict[str, Any]:
    """Get ranked study results."""
    if _TOOL_LOG:
        log.info("CALL study_results id=%s top_n=%d phase=%s", study_id, top_n, phase)
    if not study_exists(study_id):
        return _error_result("NOT_FOUND", f"Study {study_id!r} not found")

    study = load_study(study_id)

    variants = []
    if phase in (None, "coarse"):
        variants.extend(study.coarse_variants)
    if phase in (None, "refined"):
        variants.extend(study.refined_variants)

    # Filter to completed
    completed = [v for v in variants if v.status == "done"]

    # Sort
    metric = sort_by or study.objective.primary_metric
    reverse = study.objective.direction == "maximize"
    completed.sort(
        key=lambda v: v.metrics.get(metric, float("-inf") if reverse else float("inf")),
        reverse=reverse,
    )

    top = completed[:top_n]
    return {
        "ok": True,
        "study_id": study.id,
        "status": study.status.value,
        "total_variants": len(variants),
        "completed_variants": len(completed),
        "best_variant_id": study.best_variant_id,
        "results": [v.to_dict() for v in top],
    }


def study_cancel(study_id: str) -> dict[str, Any]:
    """Cancel a running study by sending SIGTERM to its runner process."""
    if _TOOL_LOG:
        log.info("CALL study_cancel id=%s", study_id)
    if not study_exists(study_id):
        return _error_result("NOT_FOUND", f"Study {study_id!r} not found")

    study = load_study(study_id)
    if study.pid is None:
        return _error_result("NO_PROCESS", "Study has no runner PID recorded")

    try:
        os.kill(study.pid, signal.SIGTERM)
    except ProcessLookupError:
        # Process already exited
        pass

    return {"ok": True, "study_id": study_id, "signal_sent": "SIGTERM", "pid": study.pid}


def study_list() -> dict[str, Any]:
    """List all studies with summary status."""
    if _TOOL_LOG:
        log.info("CALL study_list")
    return {"ok": True, "studies": list_studies()}


def study_get_variant(study_id: str, variant_id: str) -> dict[str, Any]:
    """Get full details for a single variant."""
    if _TOOL_LOG:
        log.info("CALL study_get_variant id=%s variant=%s", study_id, variant_id)
    if not study_exists(study_id):
        return _error_result("NOT_FOUND", f"Study {study_id!r} not found")

    study = load_study(study_id)
    for v in itertools.chain(study.coarse_variants, study.refined_variants):
        if v.variant_id == variant_id:
            return {"ok": True, "variant": v.to_dict()}

    return _error_result("NOT_FOUND", f"Variant {variant_id!r} not found in study {study_id!r}")
