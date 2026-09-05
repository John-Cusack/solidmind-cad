"""Tests for driver-mode study.* tool routing (create validation, dispatch, schema)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import tools_study
from server.dg_import import import_brief_file
from server.paths import repo_root
from server.study_store import load_study, save_study

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"


def driver_variables() -> list[dict]:
    return [
        {
            "name": "fillet_mm",
            "var_type": "continuous",
            "min_val": 0.0,
            "max_val": 0.6,
            "coarse_step": 0.2,
            "path": FILLET,
        }
    ]


class ToolsBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.studies_root = base / "studies"
        self.artifacts_root = base / "artifacts"
        env = {
            "SOLIDMIND_ARTIFACTS_ROOT": str(self.artifacts_root),
            "SOLIDMIND_JOBS_ROOT": str(base / "jobs"),
        }
        env_patcher = patch.dict("os.environ", env)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.revision = import_brief_file(FIXTURE, root=self.artifacts_root)

        # Standard store-patch pattern: thread the tmp root through the names
        # imported into the tool module.
        self._patchers = [
            patch(
                "server.tools_study.save_study",
                side_effect=lambda s, **kw: save_study(s, root=self.studies_root),
            ),
            patch(
                "server.tools_study.load_study",
                side_effect=lambda sid, **kw: load_study(sid, root=self.studies_root),
            ),
            patch(
                "server.tools_study.study_exists",
                side_effect=lambda sid, **kw: (self.studies_root / sid / "study.json").exists(),
            ),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def create(self, **overrides):
        kwargs = {
            "name": "latch sweep",
            "variables": driver_variables(),
            "solver": {"solver_type": "evaluator"},
            "objective": {"primary_metric": "latch_fos", "direction": "maximize"},
            "driver": "grid",
            "revision": self.revision,
            "scenario": "latch_hold",
            "models": ["analytic_latch"],
        }
        kwargs.update(overrides)
        return tools_study.study_create(**kwargs)


class TestDriverModeCreate(ToolsBase):
    def test_create_ok(self) -> None:
        out = self.create()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["execution_plan"]["driver"], "grid")
        study = load_study(out["study_id"], root=self.studies_root)
        self.assertEqual(study.driver, "grid")
        self.assertEqual(study.revision, self.revision)  # stored resolved
        self.assertEqual(study.scenario, "latch_hold")
        self.assertEqual(study.models, ["analytic_latch"])

    def test_unknown_driver(self) -> None:
        out = self.create(driver="annealing")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")

    def test_missing_revision(self) -> None:
        out = self.create(revision=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")

    def test_unknown_revision(self) -> None:
        out = self.create(revision="f" * 64)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "NOT_FOUND")

    def test_missing_variable_path(self) -> None:
        variables = driver_variables()
        del variables[0]["path"]
        out = self.create(variables=variables)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")

    def test_unknown_binding_path_fails_at_create(self) -> None:
        variables = driver_variables()
        variables[0]["path"] = "/parts/ghost/specs/x_mm"
        out = self.create(variables=variables)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "BINDING_PATH_UNKNOWN")

    def test_legacy_create_unchanged(self) -> None:
        out = tools_study.study_create(
            name="legacy",
            variables=[
                {
                    "name": "x",
                    "var_type": "continuous",
                    "min_val": 0.0,
                    "max_val": 10.0,
                    "coarse_step": 5.0,
                }
            ],
            solver={"solver_type": "mock"},
            objective={"primary_metric": "objective"},
        )
        self.assertTrue(out["ok"], out)
        self.assertNotIn("driver", out["execution_plan"])
        study = load_study(out["study_id"], root=self.studies_root)
        self.assertIsNone(study.driver)
        self.assertIsNone(study.revision)


class TestRunnerDispatch(ToolsBase):
    def test_main_dispatches_driver_studies(self) -> None:
        from server import study_runner

        out = self.create()
        with patch("server.study_driver_runner.run_driver_study") as mock_run:
            study_runner.main([out["study_id"], "--root", str(self.studies_root)])
        mock_run.assert_called_once_with(out["study_id"], root=self.studies_root)

    def test_main_keeps_legacy_path(self) -> None:
        from server import study_runner

        out = tools_study.study_create(
            name="legacy",
            variables=[
                {
                    "name": "x",
                    "var_type": "continuous",
                    "min_val": 0.0,
                    "max_val": 10.0,
                    "coarse_step": 5.0,
                }
            ],
            solver={"solver_type": "mock"},
            objective={"primary_metric": "objective"},
        )
        with patch("server.study_driver_runner.run_driver_study") as mock_run:
            study_runner.main([out["study_id"], "--root", str(self.studies_root)])
        mock_run.assert_not_called()
        study = load_study(out["study_id"], root=self.studies_root)
        self.assertEqual(study.status.value, "complete")  # legacy mock sweep ran


class TestSchema(unittest.TestCase):
    def test_study_create_schema_gained_driver_props(self) -> None:
        from server.main import _study_tool_list

        create = next(t for t in _study_tool_list() if t["name"] == "study.create")
        props = create["inputSchema"]["properties"]
        self.assertIn("driver", props)
        self.assertIn("revision", props)
        self.assertIn("scenario", props)
        self.assertIn("models", props)
        self.assertIn("path", props["variables"]["items"]["properties"])
        self.assertIn("evaluator", props["solver"]["properties"]["solver_type"]["enum"])
        # driver stays optional — legacy calls are untouched.
        self.assertEqual(
            create["inputSchema"]["required"], ["name", "variables", "solver", "objective"]
        )


if __name__ == "__main__":
    unittest.main()
