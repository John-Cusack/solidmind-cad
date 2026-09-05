"""Tranche-1 acceptance test: the full spine on the foam-dart latch.

The four acceptance criteria from docs/target-architecture.md:

1. Different parameter values produce different fingerprints, and metrics
   move correctly.
2. Identical requests reproduce exactly (bit-exact analytic tier).
3. An unknown binding hard-fails.
4. An unchanged fingerprint flags.

Exercised through the real path: brief fixture → revision zero →
``study.create`` (driver mode) → driver runner → evaluator subprocesses.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import tools_study
from server.dg_binding import Binding
from server.dg_import import import_brief_file
from server.eval_models import EvalRequest
from server.evaluator import EXIT_BINDING, evaluate_request
from server.paths import repo_root
from server.study_driver_runner import run_driver_study
from server.study_models import StudyStatus
from server.study_store import load_study, save_study

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"
TARGET_FOS = 2.0


class TestAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.studies_root = base / "studies"
        cls.artifacts_root = base / "artifacts"
        cls.jobs_root = base / "jobs"

        env = {
            "SOLIDMIND_ARTIFACTS_ROOT": str(cls.artifacts_root),
            "SOLIDMIND_JOBS_ROOT": str(cls.jobs_root),
        }
        cls._env_patch = patch.dict("os.environ", env)
        cls._env_patch.start()
        cls._store_patches = [
            patch(
                "server.tools_study.save_study",
                side_effect=lambda s, **kw: save_study(s, root=cls.studies_root),
            ),
            patch(
                "server.tools_study.load_study",
                side_effect=lambda sid, **kw: load_study(sid, root=cls.studies_root),
            ),
            patch(
                "server.tools_study.study_exists",
                side_effect=lambda sid, **kw: (cls.studies_root / sid / "study.json").exists(),
            ),
        ]
        for p in cls._store_patches:
            p.start()

        cls.revision = import_brief_file(FIXTURE, root=cls.artifacts_root)

        created = tools_study.study_create(
            name="latch fillet acceptance sweep",
            variables=[
                {
                    "name": "fillet_mm",
                    "var_type": "continuous",
                    "min_val": 0.0,
                    "max_val": 0.6,
                    "coarse_step": 0.1,
                    "path": FILLET,
                }
            ],
            solver={"solver_type": "evaluator"},
            objective={"primary_metric": "latch_fos", "direction": "maximize"},
            driver="grid",
            revision=cls.revision,
            scenario="latch_hold",
            models=["analytic_latch"],
        )
        assert created["ok"], created
        cls.study_id = created["study_id"]
        run_driver_study(
            cls.study_id,
            root=cls.studies_root,
            artifacts_root=cls.artifacts_root,
            jobs_root=cls.jobs_root,
        )
        cls.study = load_study(cls.study_id, root=cls.studies_root)
        cls.variants = cls.study.coarse_variants + cls.study.refined_variants

    @classmethod
    def tearDownClass(cls) -> None:
        for p in cls._store_patches:
            p.stop()
        cls._env_patch.stop()
        cls._tmp.cleanup()

    def coarse_by_fillet(self) -> dict[float, object]:
        return {v.params["fillet_mm"]: v for v in self.study.coarse_variants}

    def test_sweep_completed(self) -> None:
        self.assertEqual(self.study.status, StudyStatus.COMPLETE)
        for v in self.variants:
            self.assertEqual(v.status, "done", v.error)

    def test_metrics_move_correctly(self) -> None:
        # The seeded defect FAILs; FoS is monotone in fillet and crosses the
        # 2.0 target inside the sweep.
        by_fillet = self.coarse_by_fillet()
        self.assertLess(by_fillet[0.0].metrics["latch_fos"], 1.0)
        fillets = sorted(by_fillet)
        fos_series = [by_fillet[f].metrics["latch_fos"] for f in fillets]
        self.assertEqual(fos_series, sorted(fos_series))
        self.assertGreater(fos_series[-1], TARGET_FOS)
        crossing = [f for f in fillets if by_fillet[f].metrics["latch_fos"] >= TARGET_FOS]
        self.assertTrue(crossing, "FoS never crossed the target inside the sweep")
        self.assertEqual(self.study.best_variant_id is not None, True)

    def test_distinct_params_distinct_fingerprints(self) -> None:
        hashes = [v.result_hash for v in self.study.coarse_variants]
        self.assertEqual(len(hashes), len(set(hashes)), "result hashes must be distinct")
        materialized = set()
        for v in self.study.coarse_variants:
            result, code, _ = evaluate_request(
                EvalRequest(
                    revision=self.revision,
                    bindings=(Binding(FILLET, v.params["fillet_mm"]),),
                    scenario="latch_hold",
                ),
                root=self.artifacts_root,
            )
            self.assertEqual(code, 0)
            materialized.add(result.artifact_hashes["materialized"])
        self.assertEqual(len(materialized), len(self.study.coarse_variants))

    def test_identical_requests_reproduce_exactly(self) -> None:
        request = EvalRequest(
            revision=self.revision,
            bindings=(Binding(FILLET, 0.3),),
            scenario="latch_hold",
        )
        r1, _, _ = evaluate_request(request, root=self.artifacts_root)
        r2, _, hit = evaluate_request(request, root=self.artifacts_root)
        self.assertTrue(hit, "second identical request must replay from cache")
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_unknown_binding_hard_fails(self) -> None:
        result, code, _ = evaluate_request(
            EvalRequest(
                revision=self.revision,
                bindings=(Binding("/parts/ghost/specs/x_mm", 1.0),),
            ),
            root=self.artifacts_root,
        )
        self.assertEqual(code, EXIT_BINDING)
        assert result.failure is not None
        self.assertEqual(result.failure["class"], "binding_error")

    def test_unchanged_fingerprint_flags(self) -> None:
        result, code, _ = evaluate_request(
            EvalRequest(
                revision=self.revision,
                bindings=(Binding("/parts/barrel/specs/wall_mm", 9.9),),
            ),
            root=self.artifacts_root,
        )
        self.assertEqual(code, 0)
        self.assertTrue(result.flags["unchanged_fingerprint"])


if __name__ == "__main__":
    unittest.main()
