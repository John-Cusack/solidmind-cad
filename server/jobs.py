"""Durable job records for driver-runner processes.

One shared module used by the study driver runner — the four pre-existing job
systems (study PID tracking, sim engines, RL training, orchestrator runs) are
untouched.

Design points:

- **Process identity beyond PID**: alongside the PID we record the process
  start time (``/proc/<pid>/stat`` field 22, in clock ticks). ``is_alive``
  only believes a PID whose start ticks still match, so a recycled PID is
  never mistaken for the original process. Non-Linux fallback: signal 0.
- **Atomic writes**: ``job.json`` is written via tmp + ``os.replace``.
- **Single writer**: after ``mark_running`` the runner process is the sole
  writer of ``job.json``. Other processes only create jobs and write the
  ``cancel`` flag — a separate file — so there is no read-modify-write race.
- **Enforced transitions**: ``PENDING → RUNNING → {COMPLETE, FAILED,
  CANCELLED}`` (plus ``PENDING → FAILED/CANCELLED`` for jobs whose runner
  never started). Anything else raises.
- **Restart recovery**: ``recover_stale`` marks RUNNING jobs with dead
  processes as FAILED("orphaned"). Re-running the study is then cheap by
  construction — completed evaluations replay from the result cache.
"""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from server.paths import data_path


class JobError(ValueError):
    pass


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.COMPLETE: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


@dataclass(slots=True)
class JobRecord:
    job_id: str
    kind: str
    argv: list[str] = field(default_factory=list)
    pid: int | None = None
    pid_start_ticks: int | None = None
    status: JobStatus = JobStatus.PENDING
    created_at: float = 0.0
    heartbeat_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "argv": self.argv,
            "pid": self.pid,
            "pid_start_ticks": self.pid_start_ticks,
            "status": self.status.value,
            "created_at": self.created_at,
            "heartbeat_at": self.heartbeat_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRecord:
        return cls(
            job_id=data["job_id"],
            kind=data["kind"],
            argv=list(data.get("argv", ())),
            pid=data.get("pid"),
            pid_start_ticks=data.get("pid_start_ticks"),
            status=JobStatus(data.get("status", "pending")),
            created_at=data.get("created_at", 0.0),
            heartbeat_at=data.get("heartbeat_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error", ""),
            detail=dict(data.get("detail", {})),
        )


def jobs_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    env = os.environ.get("SOLIDMIND_JOBS_ROOT", "")
    if env:
        return Path(env)
    return data_path("jobs")


def _job_dir(job_id: str, root: Path | None) -> Path:
    return jobs_root(root) / job_id


def _write(record: JobRecord, root: Path | None) -> None:
    job_dir = _job_dir(record.job_id, root)
    job_dir.mkdir(parents=True, exist_ok=True)
    tmp = job_dir / f".job.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(record.to_dict(), indent=2))
    os.replace(tmp, job_dir / "job.json")


def _read_start_ticks(pid: int) -> int | None:
    """Field 22 (starttime) of /proc/<pid>/stat; None off-Linux or on error."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm (field 2) may contain spaces/parens; parse after the last ')'.
        rest = stat.rpartition(")")[2].split()
        # rest[0] is field 3 (state); field 22 is rest[19].
        return int(rest[19])
    except (OSError, IndexError, ValueError):
        return None


def create_job(
    kind: str,
    argv: list[str],
    detail: dict[str, Any] | None = None,
    *,
    root: Path | None = None,
) -> JobRecord:
    record = JobRecord(
        job_id=f"job_{uuid.uuid4().hex[:12]}",
        kind=kind,
        argv=list(argv),
        created_at=time.time(),
        detail=detail or {},
    )
    _write(record, root)
    return record


def load_job(job_id: str, *, root: Path | None = None) -> JobRecord:
    path = _job_dir(job_id, root) / "job.json"
    if not path.exists():
        raise JobError(f"Job not found: {job_id}")
    return JobRecord.from_dict(json.loads(path.read_text()))


def _transition(record: JobRecord, to: JobStatus) -> None:
    if to not in _ALLOWED_TRANSITIONS[record.status]:
        raise JobError(f"Illegal job transition {record.status.value} -> {to.value}")
    record.status = to


def mark_running(job_id: str, pid: int, *, root: Path | None = None) -> JobRecord:
    record = load_job(job_id, root=root)
    _transition(record, JobStatus.RUNNING)
    record.pid = pid
    record.pid_start_ticks = _read_start_ticks(pid)
    record.heartbeat_at = time.time()
    _write(record, root)
    return record


def heartbeat(job_id: str, *, root: Path | None = None) -> None:
    record = load_job(job_id, root=root)
    if record.status is not JobStatus.RUNNING:
        raise JobError(f"Cannot heartbeat a {record.status.value} job")
    record.heartbeat_at = time.time()
    _write(record, root)


def finish(
    job_id: str,
    status: JobStatus,
    *,
    error: str = "",
    root: Path | None = None,
) -> JobRecord:
    record = load_job(job_id, root=root)
    _transition(record, status)
    record.error = error
    record.finished_at = time.time()
    _write(record, root)
    return record


def is_alive(record: JobRecord) -> bool:
    """True only for a live process that is *the same* process we recorded."""
    if record.pid is None:
        return False
    try:
        os.kill(record.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else — still alive
    if record.pid_start_ticks is not None:
        return _read_start_ticks(record.pid) == record.pid_start_ticks
    return True


def cancel_flag_path(job_id: str, *, root: Path | None = None) -> Path:
    return _job_dir(job_id, root) / "cancel"


def request_cancel(job_id: str, *, root: Path | None = None) -> None:
    """Write the cancel flag, then nudge the runner with SIGTERM if alive."""
    record = load_job(job_id, root=root)
    cancel_flag_path(job_id, root=root).touch()
    if record.status is JobStatus.RUNNING and is_alive(record) and record.pid is not None:
        try:
            os.kill(record.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def cancel_requested(job_id: str, *, root: Path | None = None) -> bool:
    return cancel_flag_path(job_id, root=root).exists()


def recover_stale(kind: str, *, root: Path | None = None) -> list[JobRecord]:
    """Mark RUNNING jobs of this kind whose process is gone as FAILED."""
    base = jobs_root(root)
    if not base.is_dir():
        return []
    recovered: list[JobRecord] = []
    for job_file in sorted(base.glob("*/job.json")):
        try:
            record = JobRecord.from_dict(json.loads(job_file.read_text()))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if record.kind != kind or record.status is not JobStatus.RUNNING:
            continue
        if not is_alive(record):
            recovered.append(finish(record.job_id, JobStatus.FAILED, error="orphaned", root=root))
    return recovered
