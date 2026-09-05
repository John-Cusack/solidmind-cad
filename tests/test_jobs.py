"""Tests for durable job records — identity, transitions, cancel, recovery."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import jobs
from server.jobs import JobError, JobStatus


class JobsBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "jobs"


class TestLifecycle(JobsBase):
    def test_create_load_round_trip(self) -> None:
        record = jobs.create_job("study_driver", ["cmd"], {"study_id": "s1"}, root=self.root)
        loaded = jobs.load_job(record.job_id, root=self.root)
        self.assertEqual(loaded.kind, "study_driver")
        self.assertEqual(loaded.detail, {"study_id": "s1"})
        self.assertEqual(loaded.status, JobStatus.PENDING)

    def test_mark_running_captures_identity(self) -> None:
        record = jobs.create_job("study_driver", [], root=self.root)
        running = jobs.mark_running(record.job_id, os.getpid(), root=self.root)
        self.assertEqual(running.status, JobStatus.RUNNING)
        self.assertEqual(running.pid, os.getpid())
        self.assertIsNotNone(running.pid_start_ticks)  # Linux CI

    def test_complete_flow(self) -> None:
        record = jobs.create_job("study_driver", [], root=self.root)
        jobs.mark_running(record.job_id, os.getpid(), root=self.root)
        jobs.heartbeat(record.job_id, root=self.root)
        finished = jobs.finish(record.job_id, JobStatus.COMPLETE, root=self.root)
        self.assertEqual(finished.status, JobStatus.COMPLETE)
        self.assertIsNotNone(finished.finished_at)

    def test_illegal_transitions_raise(self) -> None:
        record = jobs.create_job("study_driver", [], root=self.root)
        with self.assertRaises(JobError):
            jobs.finish(record.job_id, JobStatus.COMPLETE, root=self.root)  # PENDING -> COMPLETE
        jobs.mark_running(record.job_id, os.getpid(), root=self.root)
        jobs.finish(record.job_id, JobStatus.FAILED, error="x", root=self.root)
        with self.assertRaises(JobError):
            jobs.finish(record.job_id, JobStatus.COMPLETE, root=self.root)  # terminal
        with self.assertRaises(JobError):
            jobs.heartbeat(record.job_id, root=self.root)

    def test_pending_can_fail(self) -> None:
        record = jobs.create_job("study_driver", [], root=self.root)
        failed = jobs.finish(record.job_id, JobStatus.FAILED, error="spawn", root=self.root)
        self.assertEqual(failed.status, JobStatus.FAILED)

    def test_load_missing_raises(self) -> None:
        with self.assertRaises(JobError):
            jobs.load_job("job_missing", root=self.root)


class TestIsAlive(JobsBase):
    def test_current_process_alive(self) -> None:
        record = jobs.create_job("k", [], root=self.root)
        running = jobs.mark_running(record.job_id, os.getpid(), root=self.root)
        self.assertTrue(jobs.is_alive(running))

    def test_dead_pid_not_alive(self) -> None:
        record = jobs.create_job("k", [], root=self.root)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        running = jobs.mark_running(record.job_id, proc.pid, root=self.root)
        self.assertFalse(jobs.is_alive(running))

    def test_recycled_pid_not_alive(self) -> None:
        record = jobs.create_job("k", [], root=self.root)
        running = jobs.mark_running(record.job_id, os.getpid(), root=self.root)
        # Same PID, different start ticks — a recycled PID must not count.
        with patch(
            "server.jobs._read_start_ticks", return_value=(running.pid_start_ticks or 0) + 1
        ):
            self.assertFalse(jobs.is_alive(running))


class TestCancelAndRecovery(JobsBase):
    def test_cancel_flag_and_signal_reach_child(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            record = jobs.create_job("k", [], root=self.root)
            jobs.mark_running(record.job_id, proc.pid, root=self.root)
            self.assertFalse(jobs.cancel_requested(record.job_id, root=self.root))
            jobs.request_cancel(record.job_id, root=self.root)
            self.assertTrue(jobs.cancel_requested(record.job_id, root=self.root))
            # SIGTERM reached the child: default handler terminates it.
            self.assertIsNotNone(proc.wait(timeout=10))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_recover_stale_marks_orphans(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        orphan = jobs.create_job("study_driver", [], root=self.root)
        jobs.mark_running(orphan.job_id, dead.pid, root=self.root)
        live = jobs.create_job("study_driver", [], root=self.root)
        jobs.mark_running(live.job_id, os.getpid(), root=self.root)
        other_kind = jobs.create_job("other", [], root=self.root)
        jobs.mark_running(other_kind.job_id, dead.pid, root=self.root)

        recovered = jobs.recover_stale("study_driver", root=self.root)
        self.assertEqual([r.job_id for r in recovered], [orphan.job_id])
        self.assertEqual(jobs.load_job(orphan.job_id, root=self.root).status, JobStatus.FAILED)
        self.assertEqual(jobs.load_job(orphan.job_id, root=self.root).error, "orphaned")
        self.assertEqual(jobs.load_job(live.job_id, root=self.root).status, JobStatus.RUNNING)
        self.assertEqual(jobs.load_job(other_kind.job_id, root=self.root).status, JobStatus.RUNNING)

    def test_heartbeat_updates(self) -> None:
        record = jobs.create_job("k", [], root=self.root)
        jobs.mark_running(record.job_id, os.getpid(), root=self.root)
        first = jobs.load_job(record.job_id, root=self.root).heartbeat_at
        time.sleep(0.01)
        jobs.heartbeat(record.job_id, root=self.root)
        second = jobs.load_job(record.job_id, root=self.root).heartbeat_at
        assert first is not None and second is not None
        self.assertGreater(second, first)


if __name__ == "__main__":
    unittest.main()
