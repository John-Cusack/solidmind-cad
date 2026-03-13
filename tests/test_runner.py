"""Tests for orchestrator.runner — init, prompts, collection, gates."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.runner import (
    OrchestratorRun,
    build_worker_prompts,
    check_gate_g0,
    check_gate_g1,
    check_gate_g3,
    check_gate_g4,
    dry_run,
    collect_worker_results,
    format_dispatch_instructions,
    init_run,
    load_run,
    save_spec,
    transition,
)
from orchestrator.spec import (
    CoordinateFrame,
    Interface,
    ManufacturingSpec,
    MasterSpec,
    MatingSemantic,
    Objective,
    SpecStatus,
    Subsystem,
    SubsystemKind,
    ToleranceSchema,
    ValidationCheckPoint,
    ValidationMethod,
)


def _make_two_cube_spec() -> MasterSpec:
    """Build a minimal spec: two cubes that mate via a boss/hole interface."""
    spec = MasterSpec(
        name="Two Cube Test",
        description="Cube A has a boss, Cube B has a matching hole",
        global_constraints={"max_mass_kg": 0.5, "max_envelope_mm": [50, 50, 50]},
        objectives=[
            Objective(name="mass", direction="minimize", unit="kg", weight=1.0),
        ],
    )
    ifc = Interface(
        id="ifc_boss_hole",
        name="Boss-hole mate",
        subsystem_a="cube_a",
        port_a="boss",
        subsystem_b="cube_b",
        port_b="hole",
        geometry={"type": "cylinder", "diameter_mm": 10, "depth_mm": 5},
        frame_a=CoordinateFrame(origin_mm=[0, 0, 10]),
        frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
        mating=MatingSemantic(type="cylindrical_fit", engagement_length_mm=5),
        tolerances=ToleranceSchema(
            fit_class="H7/h6",
            dimensional={"diameter_mm": {"nominal": 10, "upper": 0.015, "lower": 0}},
        ),
        validation=ValidationMethod(
            check_points=[
                ValidationCheckPoint(feature="boss_diameter", expected_mm=10.0, tolerance_mm=0.015),
                ValidationCheckPoint(feature="boss_height", expected_mm=5.0, tolerance_mm=0.1),
            ],
        ),
        datum_scheme="A-B",
        ctqs=["boss_diameter", "boss_height"],
    )
    spec.interfaces.append(ifc)

    cube_a = Subsystem(
        id="s_cube_a",
        name="cube_a",
        description="20x20x10 cube with a Ø10 boss on top",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 15],
        mass_budget_kg=0.1,
        material="aluminum",
        interfaces=["ifc_boss_hole"],
        specs={"base_size_mm": 20, "base_height_mm": 10, "boss_diameter_mm": 10, "boss_height_mm": 5},
        worker_count=2,
        manufacturing=ManufacturingSpec(process="CNC_milling", min_feature_size_mm=0.5, min_wall_mm=1.0),
    )
    cube_b = Subsystem(
        id="s_cube_b",
        name="cube_b",
        description="20x20x10 cube with a Ø10 hole on top",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 10],
        mass_budget_kg=0.1,
        material="aluminum",
        interfaces=["ifc_boss_hole"],
        specs={"base_size_mm": 20, "base_height_mm": 10, "hole_diameter_mm": 10, "hole_depth_mm": 5},
        worker_count=1,
        manufacturing=ManufacturingSpec(process="CNC_milling", min_feature_size_mm=0.5, min_wall_mm=1.0),
    )
    bolt = Subsystem(
        id="s_bolt",
        name="bolt_m5",
        description="M5x20 socket head cap screw",
        kind=SubsystemKind.STANDARD,
        standard="ISO 4762 M5x20",
    )
    spec.subsystems.extend([cube_a, cube_b, bolt])
    return spec


class TestInitRun(unittest.TestCase):
    def test_creates_run_dir_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            run = init_run("Test", run_dir=run_dir)
            self.assertTrue(run.spec_path.exists())
            self.assertTrue(run.state_path.exists())
            self.assertEqual(run.spec.name, "Test")
            self.assertEqual(run.state.current, SpecStatus.DRAFT)

    def test_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            run = init_run("Test Assembly", run_dir=run_dir, description="desc")
            run.spec = _make_two_cube_spec()
            save_spec(run)

            loaded = load_run(run_dir)
            self.assertEqual(loaded.spec.name, "Two Cube Test")
            self.assertEqual(len(loaded.spec.subsystems), 3)
            self.assertEqual(loaded.state.current, SpecStatus.DRAFT)


class TestTransition(unittest.TestCase):
    def test_transition_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = init_run("Test", run_dir=Path(td) / "run")
            transition(run, SpecStatus.NORMALIZING, reason="go")
            self.assertEqual(run.state.current, SpecStatus.NORMALIZING)
            self.assertEqual(run.spec.status, SpecStatus.NORMALIZING)

            # Verify persisted
            loaded = load_run(run.run_dir)
            self.assertEqual(loaded.state.current, SpecStatus.NORMALIZING)

    def test_invalid_transition_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = init_run("Test", run_dir=Path(td) / "run")
            with self.assertRaises(ValueError):
                transition(run, SpecStatus.BUILDING)


class TestBuildWorkerPrompts(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Test", run_dir=Path(self.td) / "run")
        self.run.spec = _make_two_cube_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_generates_prompts_for_generated_only(self) -> None:
        prompts = build_worker_prompts(self.run)
        # cube_a has worker_count=2, cube_b has 1, bolt is STANDARD (skipped)
        self.assertEqual(len(prompts), 3)
        names = [p["subsystem"] for p in prompts]
        self.assertIn("cube_a", names)
        self.assertIn("cube_b", names)
        self.assertNotIn("bolt_m5", names)

    def test_prompt_contains_interface_specs(self) -> None:
        prompts = build_worker_prompts(self.run)
        cube_a_prompt = next(p for p in prompts if p["subsystem"] == "cube_a" and p["variant_index"] == 0)
        text = cube_a_prompt["prompt"]
        self.assertIn("Boss-hole mate", text)
        self.assertIn("cylindrical_fit", text)
        self.assertIn("H7/h6", text)
        self.assertIn("boss_diameter", text)
        self.assertIn("A-B", text)  # datum_scheme

    def test_prompt_contains_specs(self) -> None:
        prompts = build_worker_prompts(self.run)
        cube_a_prompt = next(p for p in prompts if p["subsystem"] == "cube_a")
        text = cube_a_prompt["prompt"]
        self.assertIn("base_size_mm", text)
        self.assertIn("aluminum", text)
        self.assertIn("CNC_milling", text)

    def test_prompt_contains_output_dir(self) -> None:
        prompts = build_worker_prompts(self.run)
        for p in prompts:
            self.assertIn(p["output_dir"], p["prompt"])

    def test_output_dirs_created(self) -> None:
        prompts = build_worker_prompts(self.run)
        for p in prompts:
            self.assertTrue(Path(p["output_dir"]).exists())

    def test_variant_indices(self) -> None:
        prompts = build_worker_prompts(self.run)
        cube_a_prompts = [p for p in prompts if p["subsystem"] == "cube_a"]
        self.assertEqual(len(cube_a_prompts), 2)
        indices = {p["variant_index"] for p in cube_a_prompts}
        self.assertEqual(indices, {0, 1})

    def test_descriptions(self) -> None:
        prompts = build_worker_prompts(self.run)
        for p in prompts:
            self.assertIn("Build", p["description"])
            self.assertIn(p["subsystem"], p["description"])


class TestCollectWorkerResults(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Test", run_dir=Path(self.td) / "run")
        self.run.spec = _make_two_cube_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_missing_outputs(self) -> None:
        """No output dirs → all status=missing."""
        results = collect_worker_results(self.run)
        # 3 workers (cube_a×2 + cube_b×1), bolt skipped
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r["status"], "missing")

    def test_incomplete_output(self) -> None:
        """Output dir exists but no STEP file → incomplete."""
        output_dir = self.run.run_dir / "cube_a_0" / "output"
        output_dir.mkdir(parents=True)
        (output_dir / "metadata.json").write_text('{"subsystem": "cube_a"}')

        results = collect_worker_results(self.run)
        r = next(r for r in results if r["subsystem"] == "cube_a" and r["variant_index"] == 0)
        self.assertEqual(r["status"], "incomplete")
        self.assertIn("metadata", r)

    def test_complete_output(self) -> None:
        """STEP + metadata → complete."""
        output_dir = self.run.run_dir / "cube_a_0" / "output"
        output_dir.mkdir(parents=True)
        (output_dir / "cube_a.step").write_text("dummy step")
        (output_dir / "cube_a.stl").write_text("dummy stl")
        (output_dir / "cube_a.png").write_bytes(b"\x89PNG")
        metadata = {
            "subsystem": "cube_a",
            "claimed_mass_kg": 0.05,
            "interface_actuals": {"ifc_boss_hole": {"boss_diameter": 10.002}},
        }
        (output_dir / "metadata.json").write_text(json.dumps(metadata))

        results = collect_worker_results(self.run)
        r = next(r for r in results if r["subsystem"] == "cube_a" and r["variant_index"] == 0)
        self.assertEqual(r["status"], "complete")
        self.assertEqual(len(r["step_files"]), 1)
        self.assertEqual(len(r["stl_files"]), 1)
        self.assertEqual(len(r["screenshots"]), 1)
        self.assertAlmostEqual(r["metadata"]["claimed_mass_kg"], 0.05)


class TestGateG0(unittest.TestCase):
    def test_passes_with_objectives_and_constraints(self) -> None:
        spec = _make_two_cube_spec()
        ok, issues = check_gate_g0(spec)
        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_fails_no_objectives(self) -> None:
        spec = MasterSpec(name="empty", global_constraints={"x": 1})
        ok, issues = check_gate_g0(spec)
        self.assertFalse(ok)
        self.assertIn("No objectives defined", issues)

    def test_fails_no_constraints(self) -> None:
        spec = MasterSpec(
            name="no_constraints",
            objectives=[Objective(name="mass", direction="minimize", unit="kg")],
        )
        ok, issues = check_gate_g0(spec)
        self.assertFalse(ok)
        self.assertIn("No global constraints defined", issues)

    def test_fails_objective_missing_direction(self) -> None:
        spec = MasterSpec(
            name="bad_obj",
            global_constraints={"x": 1},
            objectives=[Objective(name="mass", direction="", unit="kg")],
        )
        ok, issues = check_gate_g0(spec)
        self.assertFalse(ok)
        self.assertTrue(any("missing direction" in i for i in issues))

    def test_fails_objective_missing_unit(self) -> None:
        spec = MasterSpec(
            name="bad_obj",
            global_constraints={"x": 1},
            objectives=[Objective(name="mass", direction="minimize", unit="")],
        )
        ok, issues = check_gate_g0(spec)
        self.assertFalse(ok)
        self.assertTrue(any("missing unit" in i for i in issues))


class TestGateG1(unittest.TestCase):
    def test_passes_good_spec(self) -> None:
        spec = _make_two_cube_spec()
        ok, issues = check_gate_g1(spec)
        self.assertTrue(ok)

    def test_fails_no_subsystems(self) -> None:
        spec = MasterSpec(name="empty")
        ok, issues = check_gate_g1(spec)
        self.assertFalse(ok)
        self.assertIn("No subsystems defined", issues)

    def test_fails_mass_over_budget(self) -> None:
        spec = _make_two_cube_spec()
        spec.global_constraints["max_mass_kg"] = 0.01  # too small
        for sub in spec.subsystems:
            if sub.mass_budget_kg:
                sub.mass_budget_kg = 0.1
        ok, issues = check_gate_g1(spec)
        self.assertFalse(ok)
        self.assertTrue(any("Mass budget" in i for i in issues))

    def test_fails_dangling_interface_ref(self) -> None:
        spec = _make_two_cube_spec()
        spec.subsystems[0].interfaces.append("nonexistent_ifc")
        ok, issues = check_gate_g1(spec)
        self.assertFalse(ok)
        self.assertTrue(any("Dangling" in i for i in issues))


class TestGateG3(unittest.TestCase):
    def test_passes_complete_interfaces(self) -> None:
        spec = _make_two_cube_spec()
        ok, issues = check_gate_g3(spec)
        self.assertTrue(ok)

    def test_fails_incomplete_interface(self) -> None:
        spec = _make_two_cube_spec()
        # Add a bare interface with no geometry/frames/mating/validation
        spec.interfaces.append(Interface(id="ifc_bare", name="bare"))
        ok, issues = check_gate_g3(spec)
        self.assertFalse(ok)
        self.assertTrue(any("Incomplete" in i for i in issues))


class TestGateG4(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Test", run_dir=Path(self.td) / "run")
        self.run.spec = _make_two_cube_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_fails_no_artifacts(self) -> None:
        ok, issues = check_gate_g4(self.run)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 3)  # 3 generated workers

    def test_passes_all_artifacts_present(self) -> None:
        # Plant STEP files for all 3 workers
        for name, count in [("cube_a", 2), ("cube_b", 1)]:
            for i in range(count):
                out = self.run.run_dir / f"{name}_{i}" / "output"
                out.mkdir(parents=True)
                (out / f"{name}.step").write_text("dummy")
        ok, issues = check_gate_g4(self.run)
        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_partial_failure(self) -> None:
        # Only cube_a_0 has artifacts
        out = self.run.run_dir / "cube_a_0" / "output"
        out.mkdir(parents=True)
        (out / "cube_a.step").write_text("dummy")
        ok, issues = check_gate_g4(self.run)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 2)  # cube_a_1 and cube_b_0 missing


class TestFormatDispatchInstructions(unittest.TestCase):
    def test_output_format(self) -> None:
        prompts = [
            {"description": "Build sun (variant 0)", "output_dir": "/tmp/sun_0/output", "prompt": "x" * 100},
            {"description": "Build carrier (variant 0)", "output_dir": "/tmp/carrier_0/output", "prompt": "y" * 200},
        ]
        text = format_dispatch_instructions(prompts)
        self.assertIn("2 worker(s)", text)
        self.assertIn("Build sun", text)
        self.assertIn("Build carrier", text)
        self.assertIn("parallel Agent tool calls", text)


class TestDryRun(unittest.TestCase):
    """Option 4: Dry-run mode writes prompts to disk for inspection."""

    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Dry Run Test", run_dir=Path(self.td) / "run")
        self.run.spec = _make_two_cube_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_creates_prompts_dir(self) -> None:
        prompts_dir = dry_run(self.run)
        self.assertTrue(prompts_dir.exists())
        self.assertTrue(prompts_dir.is_dir())

    def test_writes_one_file_per_worker(self) -> None:
        prompts_dir = dry_run(self.run)
        md_files = list(prompts_dir.glob("*.md"))
        # 3 workers + INDEX.md = 4 files
        self.assertEqual(len(md_files), 4)

    def test_prompt_files_contain_interface_specs(self) -> None:
        prompts_dir = dry_run(self.run)
        cube_a_prompt = (prompts_dir / "cube_a_0.md").read_text()
        self.assertIn("Boss-hole mate", cube_a_prompt)
        self.assertIn("cylindrical_fit", cube_a_prompt)
        self.assertIn("H7/h6", cube_a_prompt)

    def test_index_file_lists_all_workers(self) -> None:
        prompts_dir = dry_run(self.run)
        index = (prompts_dir / "INDEX.md").read_text()
        self.assertIn("cube_a_0.md", index)
        self.assertIn("cube_a_1.md", index)
        self.assertIn("cube_b_0.md", index)
        self.assertIn("Total workers: 3", index)

    def test_skips_standard_parts(self) -> None:
        prompts_dir = dry_run(self.run)
        # bolt_m5 is STANDARD, should not have a prompt file
        self.assertFalse((prompts_dir / "bolt_m5_0.md").exists())

    def test_prompt_files_are_nonempty(self) -> None:
        prompts_dir = dry_run(self.run)
        for f in prompts_dir.glob("*.md"):
            if f.name == "INDEX.md":
                continue
            self.assertGreater(f.stat().st_size, 100,
                               f"{f.name} is suspiciously small")


if __name__ == "__main__":
    unittest.main()
