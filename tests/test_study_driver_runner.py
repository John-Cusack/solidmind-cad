"""End-to-end tests for the driver-mode study runner (real evaluator subprocesses)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from server import jobs
from server.dg_import import import_brief_file
from server.paths import repo_root
from server.study_driver_runner import run_driver_study
from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
)
from server.study_store import load_study, save_study

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"


def make_study(revision: str, *, coarse_step: float = 0.3, timeout_s: float = 60.0) -> Study:
    return Study(
        id=Study.new_id(),
        name="latch fillet sweep",
        variables=[
            DesignVariable(
                name="fillet_mm",
                var_type="continuous",
                min_val=0.0,
                max_val=0.6,
                coarse_step=coarse_step,
                path=FILLET,
            )
        ],
        solver=SolverConfig(solver_type="evaluator", timeout_s=timeout_s),
        objective=ObjectiveConfig(primary_metric="latch_fos", direction="maximize"),
        driver="grid",
        revision=revision,
        scenario="latch_hold",
        models=["analytic_latch"],
    )


class RunnerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.studies_root = base / "studies"
        self.artifacts_root = base / "artifacts"
        self.jobs_root = base / "jobs"
        self.revision = import_brief_file(FIXTURE, root=self.artifacts_root)


class TestRunDriverStudy(RunnerBase):
    def test_full_sweep_completes(self) -> None:
        study = make_study(self.revision)
        save_study(study, root=self.studies_root)
        run_driver_study(
            study.id,
            root=self.studies_root,
            artifacts_root=self.artifacts_root,
            jobs_root=self.jobs_root,
        )
        done = load_study(study.id, root=self.studies_root)
        self.assertEqual(done.status, StudyStatus.COMPLETE)
        self.assertIsNotNone(done.best_variant_id)
        all_variants = done.coarse_variants + done.refined_variants
        best = next(v for v in all_variants if v.variant_id == done.best_variant_id)
        # latch_fos increases with fillet; the sweep is capped at 0.6.
        self.assertAlmostEqual(best.params["fillet_mm"], 0.6)
        for v in all_variants:
            self.assertEqual(v.status, "done", v.error)
            self.assertIn("latch_fos", v.metrics)
            assert v.result_hash is not None
            self.assertEqual(len(v.result_hash), 64)
        # Durable job closed out.
        assert done.job_id is not None
        job = jobs.load_job(done.job_id, root=self.jobs_root)
        self.assertEqual(job.status, jobs.JobStatus.COMPLETE)
        self.assertEqual(job.detail, {"study_id": study.id})

    def test_rerun_replays_from_cache(self) -> None:
        study = make_study(self.revision)
        save_study(study, root=self.studies_root)
        run_driver_study(
            study.id,
            root=self.studies_root,
            artifacts_root=self.artifacts_root,
            jobs_root=self.jobs_root,
        )
        first = load_study(study.id, root=self.studies_root)
        first_hashes = {
            v.variant_id: v.result_hash for v in first.coarse_variants + first.refined_variants
        }

        # Reset as study_run does, then re-run: every point is a cache hit and
        # every result hash is identical.
        first.status = StudyStatus.DRAFT
        first.coarse_variants = []
        first.refined_variants = []
        first.best_variant_id = None
        save_study(first, root=self.studies_root)
        t0 = time.monotonic()
        run_driver_study(
            study.id,
            root=self.studies_root,
            artifacts_root=self.artifacts_root,
            jobs_root=self.jobs_root,
        )
        second = load_study(study.id, root=self.studies_root)
        second_hashes = {
            v.variant_id: v.result_hash for v in second.coarse_variants + second.refined_variants
        }
        self.assertEqual(first_hashes, second_hashes)
        self.assertEqual(second.status, StudyStatus.COMPLETE)
        del t0  # timing intentionally not asserted; identity of hashes is the proof

    def test_binding_failure_fails_variants_not_runner(self) -> None:
        study = make_study(self.revision)
        study.variables = [
            DesignVariable(
                name="ghost",
                var_type="continuous",
                min_val=0.0,
                max_val=1.0,
                coarse_step=0.5,
                path="/parts/ghost/specs/x_mm",
            )
        ]
        save_study(study, root=self.studies_root)
        run_driver_study(
            study.id,
            root=self.studies_root,
            artifacts_root=self.artifacts_root,
            jobs_root=self.jobs_root,
        )
        done = load_study(study.id, root=self.studies_root)
        self.assertEqual(done.status, StudyStatus.FAILED)  # no feasible variants
        for v in done.coarse_variants:
            self.assertEqual(v.status, "failed")
            self.assertIn("binding_error", v.error or "")


class TestSigtermCancellation(RunnerBase):
    def test_sigterm_cancels_study_and_job(self) -> None:
        study = make_study(self.revision, coarse_step=0.05)  # 13 points: time to interrupt
        save_study(study, root=self.studies_root)

        env = dict(os.environ)
        env["SOLIDMIND_ARTIFACTS_ROOT"] = str(self.artifacts_root)
        env["SOLIDMIND_JOBS_ROOT"] = str(self.jobs_root)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "server.study_runner",
                study.id,
                "--root",
                str(self.studies_root),
            ],
            cwd=repo_root(),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait until at least one variant is done, then interrupt.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    current = load_study(study.id, root=self.studies_root)
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.05)
                    continue
                done_count = sum(1 for v in current.coarse_variants if v.status == "done")
                if done_count >= 1:
                    break
                time.sleep(0.05)
            else:
                self.fail("runner never completed a variant")

            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=30)  # also drains and closes the stderr pipe

            final = load_study(study.id, root=self.studies_root)
            self.assertEqual(final.status, StudyStatus.CANCELLED)
            self.assertLess(
                sum(1 for v in final.coarse_variants if v.status == "done"),
                len(final.coarse_variants),
            )
            assert final.job_id is not None
            job = jobs.load_job(final.job_id, root=self.jobs_root)
            self.assertEqual(job.status, jobs.JobStatus.CANCELLED)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
