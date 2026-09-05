"""Tests for the evaluator CLI — determinism, exit codes, layered binding."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server import artifact_store as cas
from server.dg_binding import Binding
from server.dg_import import import_brief_file
from server.eval_models import EvalRequest
from server.evaluator import (
    EXIT_BINDING,
    EXIT_INVALID,
    EXIT_OK,
    RESULT_NAMESPACE,
    evaluate_request,
)
from server.paths import repo_root

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"


class EvaluatorBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"
        self.workdir = Path(self._tmp.name) / "work"
        self.workdir.mkdir()
        self.revision = import_brief_file(FIXTURE, root=self.root)

    def request(self, **overrides) -> EvalRequest:
        kwargs = {"revision": self.revision, "scenario": "latch_hold"}
        kwargs.update(overrides)
        return EvalRequest(**kwargs)


class TestEvaluateRequest(EvaluatorBase):
    def test_ok_result_complete(self) -> None:
        result, code, cache_hit = evaluate_request(self.request(), root=self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(cache_hit)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.metrics["peak_stress_mpa"].value, 67.5)
        self.assertEqual(
            set(result.artifact_hashes),
            {"structure", "params", "bound_params", "materialized"},
        )
        self.assertEqual(result.model_identity, {"analytic_latch": "analytic_latch/v1"})
        self.assertIn("identity_hash", result.environment)
        # The result ref is named by the request hash (itself 64-hex, so
        # get_ref — not resolve() — is the correct lookup) and its target
        # object carries lineage.
        ref = cas.get_ref(RESULT_NAMESPACE, result.request_hash, root=self.root)
        assert ref is not None
        self.assertIsNotNone(cas.get_lineage(ref["hash"], root=self.root))

    def test_second_call_is_cache_hit(self) -> None:
        r1, _, hit1 = evaluate_request(self.request(), root=self.root)
        r2, code, hit2 = evaluate_request(self.request(), root=self.root)
        self.assertFalse(hit1)
        self.assertTrue(hit2)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_different_binding_different_fingerprints(self) -> None:
        r1, _, _ = evaluate_request(self.request(bindings=(Binding(FILLET, 0.2),)), root=self.root)
        r2, _, _ = evaluate_request(self.request(bindings=(Binding(FILLET, 0.3),)), root=self.root)
        self.assertNotEqual(r1.artifact_hashes["materialized"], r2.artifact_hashes["materialized"])
        self.assertNotEqual(r1.request_hash, r2.request_hash)
        self.assertGreater(r2.metrics["latch_fos"].value, r1.metrics["latch_fos"].value)

    def test_unconsumed_binding_flags_unchanged_fingerprint(self) -> None:
        # The barrel wall is a declared, bindable parameter — but the latch
        # stage never reads it, so the materialized fingerprint is unchanged.
        result, code, _ = evaluate_request(
            self.request(bindings=(Binding("/parts/barrel/specs/wall_mm", 5.0),)),
            root=self.root,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(result.flags["unchanged_fingerprint"])

    def test_consumed_binding_does_not_flag(self) -> None:
        result, _, _ = evaluate_request(
            self.request(bindings=(Binding(FILLET, 0.4),)), root=self.root
        )
        self.assertFalse(result.flags["unchanged_fingerprint"])

    def test_no_bindings_does_not_flag(self) -> None:
        result, _, _ = evaluate_request(self.request(), root=self.root)
        self.assertFalse(result.flags["unchanged_fingerprint"])

    def test_unknown_binding_hard_fails(self) -> None:
        result, code, _ = evaluate_request(
            self.request(bindings=(Binding("/parts/nope/specs/x_mm", 1.0),)),
            root=self.root,
        )
        self.assertEqual(code, EXIT_BINDING)
        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure["class"], "binding_error")
        self.assertIn("BINDING_PATH_UNKNOWN", result.failure["message"])

    def test_out_of_domain_recorded_not_enforced(self) -> None:
        result, code, _ = evaluate_request(
            self.request(bindings=(Binding(FILLET, 2.0),)), root=self.root
        )
        self.assertEqual(code, EXIT_OK)
        hits = [h for h in result.validity_domain_hits if h["path"] == FILLET]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "above_max")

    def test_unknown_revision(self) -> None:
        result, code, _ = evaluate_request(EvalRequest(revision="f" * 64), root=self.root)
        # A hex digest passes resolve() but the object does not exist.
        self.assertEqual(code, EXIT_INVALID)
        self.assertFalse(result.ok)

    def test_unknown_model(self) -> None:
        result, code, _ = evaluate_request(self.request(models=("warp_drive",)), root=self.root)
        self.assertEqual(code, EXIT_INVALID)
        assert result.failure is not None
        self.assertEqual(result.failure["class"], "invalid_request")

    def test_seed_echoed(self) -> None:
        result, _, _ = evaluate_request(self.request(seed=42), root=self.root)
        self.assertEqual(result.seed, 42)

    def test_scenario_in_cache_key(self) -> None:
        r1, _, _ = evaluate_request(self.request(scenario="latch_hold"), root=self.root)
        r2, _, hit = evaluate_request(self.request(scenario="latch_catch"), root=self.root)
        self.assertFalse(hit)
        self.assertNotEqual(r1.request_hash, r2.request_hash)


class TestCli(EvaluatorBase):
    def run_cli(
        self, request_doc: dict, name: str = "r"
    ) -> tuple[subprocess.CompletedProcess, Path]:
        request_path = self.workdir / f"{name}.request.json"
        result_path = self.workdir / f"{name}.result.json"
        request_path.write_text(json.dumps(request_doc))
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "server.evaluator",
                str(request_path),
                str(result_path),
                "--artifacts-root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            cwd=repo_root(),
        )
        return proc, result_path

    def test_cli_ok_and_deterministic(self) -> None:
        doc = self.request(bindings=(Binding(FILLET, 0.3),)).to_dict()
        proc1, path1 = self.run_cli(doc, "a")
        proc2, path2 = self.run_cli(doc, "b")
        self.assertEqual(proc1.returncode, EXIT_OK, proc1.stderr)
        self.assertEqual(proc2.returncode, EXIT_OK, proc2.stderr)
        self.assertTrue(proc1.stdout.startswith("result_sha256="))
        # Identical requests reproduce exactly: same announced hash, same bytes.
        self.assertEqual(proc1.stdout, proc2.stdout)
        self.assertEqual(path1.read_bytes(), path2.read_bytes())

    def test_cli_binding_error_exit_2(self) -> None:
        doc = self.request(bindings=(Binding("/bogus/path", 1.0),)).to_dict()
        proc, result_path = self.run_cli(doc)
        self.assertEqual(proc.returncode, EXIT_BINDING)
        payload = json.loads(result_path.read_text())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure"]["class"], "binding_error")

    def test_cli_malformed_request_exit_3(self) -> None:
        request_path = self.workdir / "bad.request.json"
        request_path.write_text("{not json")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "server.evaluator",
                str(request_path),
                str(self.workdir / "bad.result.json"),
                "--artifacts-root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            cwd=repo_root(),
        )
        self.assertEqual(proc.returncode, EXIT_INVALID)


if __name__ == "__main__":
    unittest.main()
