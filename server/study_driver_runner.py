"""Driver-mode study runner: durable job + evaluator subprocesses.

Invoked by ``server.study_runner.main`` when a study carries a ``driver``
(the public ``study.run`` spawn argv is unchanged). Each design point becomes
one ``python -m server.evaluator`` subprocess; completed points replay from
the evaluation cache, which is what makes restart-after-crash cheap: a re-run
repeats the sweep, but every already-evaluated point is a cache hit.

Cancellation reaches the work: SIGTERM (or the job's cancel flag) stops the
sweep between points AND terminates the in-flight evaluator child with the
same SIGTERM → wait → SIGKILL escalation the sim engine manager uses.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from server import jobs
from server.study_drivers import EvalOutcome, get_driver
from server.study_models import Study, StudyStatus, Variant
from server.study_store import _root as studies_root
from server.study_store import load_study, save_study

log = logging.getLogger("solidmind.study_driver_runner")

JOB_KIND = "study_driver"

_CANCELLED = False


def _handle_sigterm(signum: int, frame: Any) -> None:
    global _CANCELLED  # noqa: PLW0603
    log.info("Received signal %d, cancelling driver study", signum)
    _CANCELLED = True


class SubprocessEvaluatorBackend:
    """Runs each design point as an evaluator subprocess with file I/O."""

    def __init__(
        self,
        study: Study,
        evals_dir: Path,
        *,
        artifacts_root: Path | None = None,
    ) -> None:
        self.study = study
        self.evals_dir = evals_dir
        self.artifacts_root = artifacts_root
        self.current_proc: subprocess.Popen | None = None

    def _request_doc(self, bindings: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "eval.request/v1",
            "revision": self.study.revision,
            "bindings": [
                {"path": path, "value": value} for path, value in sorted(bindings.items())
            ],
            "scenario": self.study.scenario or "latch_hold",
            "models": self.study.models or ["analytic_latch"],
            "seed": 0,
        }

    def terminate_current(self) -> None:
        proc = self.current_proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def evaluate(self, bindings: dict[str, Any], tag: str) -> EvalOutcome:
        work = self.evals_dir / tag
        work.mkdir(parents=True, exist_ok=True)
        request_path = work / "request.json"
        result_path = work / "result.json"
        request_path.write_text(json.dumps(self._request_doc(bindings), indent=2))

        argv = [
            sys.executable,
            "-m",
            "server.evaluator",
            str(request_path),
            str(result_path),
        ]
        if self.artifacts_root is not None:
            argv += ["--artifacts-root", str(self.artifacts_root)]

        t0 = time.monotonic()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.current_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=self.study.solver.timeout_s)
        except subprocess.TimeoutExpired:
            self.terminate_current()
            return EvalOutcome(
                ok=False,
                error=f"evaluator timeout after {self.study.solver.timeout_s}s",
                duration_s=time.monotonic() - t0,
            )
        finally:
            self.current_proc = None
        duration = time.monotonic() - t0

        result_hash: str | None = None
        for line in stdout.splitlines():
            if line.startswith("result_sha256="):
                result_hash = line.split("=", 1)[1].strip()

        payload: dict[str, Any] | None = None
        if result_path.exists():
            try:
                payload = json.loads(result_path.read_text())
            except json.JSONDecodeError:
                payload = None

        if proc.returncode == 0 and payload is not None and payload.get("ok"):
            metrics = {name: entry["value"] for name, entry in payload.get("metrics", {}).items()}
            return EvalOutcome(
                ok=True,
                metrics=metrics,
                result_hash=result_hash,
                flags=dict(payload.get("flags", {})),
                duration_s=duration,
            )

        if payload is not None and payload.get("failure"):
            failure = payload["failure"]
            error = f"{failure.get('class')}: {failure.get('message')}"
        else:
            error = f"evaluator exited {proc.returncode}: {stderr.strip()[:500]}"
        return EvalOutcome(ok=False, error=error, duration_s=duration)


def run_driver_study(
    study_id: str,
    *,
    root: Path | None = None,
    artifacts_root: Path | None = None,
    jobs_root: Path | None = None,
) -> None:
    """Execute a driver-mode study under a durable job record."""
    global _CANCELLED  # noqa: PLW0603
    _CANCELLED = False
    signal.signal(signal.SIGTERM, _handle_sigterm)

    jobs.recover_stale(JOB_KIND, root=jobs_root)

    study = load_study(study_id, root=root)
    if not study.driver:
        raise ValueError(f"Study {study_id} has no driver configured")

    job = jobs.create_job(
        JOB_KIND,
        [sys.executable, "-m", "server.study_runner", study_id],
        {"study_id": study_id},
        root=jobs_root,
    )
    jobs.mark_running(job.job_id, os.getpid(), root=jobs_root)
    study.job_id = job.job_id
    study.pid = os.getpid()
    study.started_at = time.time()
    save_study(study, root=root)

    backend = SubprocessEvaluatorBackend(
        study,
        studies_root(root) / study.id / "evals",
        artifacts_root=artifacts_root,
    )

    def cancelled() -> bool:
        if _CANCELLED:
            return True
        return jobs.cancel_requested(job.job_id, root=jobs_root)

    def on_variant(variant: Variant | None) -> None:
        save_study(study, root=root)
        if variant is not None:
            try:
                jobs.heartbeat(job.job_id, root=jobs_root)
            except jobs.JobError:
                pass

    try:
        driver = get_driver(study.driver)
        driver.run(study, backend, on_variant=on_variant, cancelled=cancelled)
    except Exception as exc:
        backend.terminate_current()
        study.status = StudyStatus.FAILED
        study.error = f"driver crashed: {exc}"
        study.finished_at = time.time()
        save_study(study, root=root)
        jobs.finish(job.job_id, jobs.JobStatus.FAILED, error=str(exc), root=jobs_root)
        raise

    if _CANCELLED or study.status is StudyStatus.CANCELLED:
        backend.terminate_current()

    study.finished_at = time.time()
    save_study(study, root=root)

    job_status = {
        StudyStatus.COMPLETE: jobs.JobStatus.COMPLETE,
        StudyStatus.CANCELLED: jobs.JobStatus.CANCELLED,
    }.get(study.status, jobs.JobStatus.FAILED)
    jobs.finish(job.job_id, job_status, error=study.error or "", root=jobs_root)
