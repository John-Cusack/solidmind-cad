"""Tests for the Dakota file protocol — codecs and the fork bridge, no binary."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from server.dakota_io import (
    DakotaEvalFailed,
    DakotaFormatError,
    evaluator_fork,
    parse_params_in,
    parse_results_out,
    run_dakota_study,
    write_params_in,
    write_results_fail,
    write_results_out,
)
from server.dg_binding import Binding
from server.dg_import import import_brief_file
from server.eval_models import EvalRequest
from server.evaluator import evaluate_request
from server.paths import repo_root
from server.study_drivers import DakotaDriver
from server.study_models import (
    DesignVariable,
    ObjectiveConfig,
    SolverConfig,
    Study,
    StudyStatus,
)

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"


class TestCodecs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_params_round_trip(self) -> None:
        path = self.dir / "params.in"
        write_params_in(
            path,
            {"fillet_mm": 0.25, "root_mm": 1.5},
            eval_id=7,
            response_labels=["latch_fos"],
        )
        params, eval_id = parse_params_in(path)
        self.assertEqual(params, {"fillet_mm": 0.25, "root_mm": 1.5})
        self.assertEqual(eval_id, 7)

    def test_results_round_trip(self) -> None:
        path = self.dir / "results.out"
        write_results_out(path, [(0.888, "latch_fos"), (67.5, "peak_stress_mpa")])
        parsed = parse_results_out(path)
        self.assertEqual(len(parsed), 2)
        self.assertAlmostEqual(parsed[0][0], 0.888)
        self.assertEqual(parsed[0][1], "latch_fos")

    def test_fail_token(self) -> None:
        path = self.dir / "results.out"
        write_results_fail(path)
        with self.assertRaises(DakotaEvalFailed):
            parse_results_out(path)

    def test_malformed_params_rejected(self) -> None:
        path = self.dir / "params.in"
        path.write_text("no variables here\n")
        with self.assertRaises(DakotaFormatError):
            parse_params_in(path)


class TestForkBridge(unittest.TestCase):
    """A fake Dakota: loop fixed points through the file protocol and compare
    against direct evaluator calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.dir = base / "work"
        self.dir.mkdir()
        self.artifacts_root = base / "artifacts"
        self.revision = import_brief_file(FIXTURE, root=self.artifacts_root)

    def fork(self, fillet: float, eval_id: int) -> list[tuple[float, str]]:
        params_in = self.dir / f"params.in.{eval_id}"
        results_out = self.dir / f"results.out.{eval_id}"
        write_params_in(params_in, {"fillet_mm": fillet}, eval_id=eval_id)
        rc = evaluator_fork(
            params_in,
            results_out,
            revision=self.revision,
            scenario="latch_hold",
            models=["analytic_latch"],
            path_map={"fillet_mm": FILLET},
            response_metrics=["latch_fos", "peak_stress_mpa"],
            artifacts_root=self.artifacts_root,
        )
        self.assertEqual(rc, 0)
        return parse_results_out(results_out)

    def test_fake_dakota_matches_direct_evaluation(self) -> None:
        for eval_id, fillet in enumerate((0.0, 0.3, 0.5), start=1):
            results = self.fork(fillet, eval_id)
            direct, code, _ = evaluate_request(
                EvalRequest(
                    revision=self.revision,
                    bindings=(Binding(FILLET, fillet),),
                    scenario="latch_hold",
                ),
                root=self.artifacts_root,
            )
            self.assertEqual(code, 0)
            by_tag = dict((tag, value) for value, tag in results)
            self.assertAlmostEqual(by_tag["latch_fos"], direct.metrics["latch_fos"].value)
            self.assertAlmostEqual(
                by_tag["peak_stress_mpa"], direct.metrics["peak_stress_mpa"].value
            )

    def test_unmapped_variable_fails(self) -> None:
        params_in = self.dir / "params.in"
        results_out = self.dir / "results.out"
        write_params_in(params_in, {"unknown_var": 1.0}, eval_id=1)
        rc = evaluator_fork(
            params_in,
            results_out,
            revision=self.revision,
            scenario="latch_hold",
            models=["analytic_latch"],
            path_map={"fillet_mm": FILLET},
            response_metrics=["latch_fos"],
            artifacts_root=self.artifacts_root,
        )
        self.assertNotEqual(rc, 0)
        with self.assertRaises(DakotaEvalFailed):
            parse_results_out(results_out)

    def test_binding_error_writes_fail(self) -> None:
        params_in = self.dir / "params.in"
        results_out = self.dir / "results.out"
        write_params_in(params_in, {"ghost": 1.0}, eval_id=1)
        rc = evaluator_fork(
            params_in,
            results_out,
            revision=self.revision,
            scenario="latch_hold",
            models=["analytic_latch"],
            path_map={"ghost": "/parts/ghost/specs/x_mm"},
            response_metrics=["latch_fos"],
            artifacts_root=self.artifacts_root,
        )
        self.assertNotEqual(rc, 0)
        with self.assertRaises(DakotaEvalFailed):
            parse_results_out(results_out)

    def test_cli_fork(self) -> None:
        import subprocess

        params_in = self.dir / "params.in"
        results_out = self.dir / "results.out"
        write_params_in(params_in, {"fillet_mm": 0.4}, eval_id=3)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "server.dakota_io",
                "fork",
                str(params_in),
                str(results_out),
                "--revision",
                self.revision,
                "--map",
                f"fillet_mm={FILLET}",
                "--metrics",
                "latch_fos",
                "--artifacts-root",
                str(self.artifacts_root),
            ],
            capture_output=True,
            text=True,
            cwd=repo_root(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        parsed = parse_results_out(results_out)
        self.assertEqual(parsed[0][1], "latch_fos")


class TestDakotaDriverSeam(unittest.TestCase):
    def test_driver_execs_argv(self) -> None:
        study = Study(
            id="s1",
            name="x",
            variables=[
                DesignVariable(name="f", var_type="continuous", min_val=0, max_val=1, path=FILLET)
            ],
            solver=SolverConfig(solver_type="evaluator"),
            objective=ObjectiveConfig(primary_metric="latch_fos"),
            driver="dakota",
        )
        saves: list = []
        DakotaDriver(dakota_argv=[sys.executable, "-c", "pass"]).run(
            study, backend=None, on_variant=saves.append, cancelled=lambda: False
        )
        self.assertEqual(study.status, StudyStatus.COMPLETE)
        self.assertEqual(saves, [None])

    def test_driver_reports_binary_failure(self) -> None:
        study = Study(
            id="s2",
            name="x",
            variables=[],
            solver=SolverConfig(solver_type="evaluator"),
            objective=ObjectiveConfig(primary_metric="latch_fos"),
            driver="dakota",
        )
        run_dakota_study(
            study,
            None,
            dakota_argv=[sys.executable, "-c", "import sys; sys.exit(3)"],
            on_variant=lambda v: None,
            cancelled=lambda: False,
        )
        self.assertEqual(study.status, StudyStatus.FAILED)
        self.assertIn("dakota exited 3", study.error or "")


if __name__ == "__main__":
    unittest.main()
