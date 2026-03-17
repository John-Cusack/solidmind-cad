"""Golden-run integration test — plant known outputs and run the full gate pipeline.

This simulates a complete orchestration run:
1. Build a realistic spec (two mating cubes)
2. Walk the state machine through all gates
3. Plant known worker outputs (fake STEP files + metadata)
4. Verify gates G0→G4 pass on good data, fail on bad data
5. Verify result collection and summary
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.runner import (
    build_worker_prompts,
    check_gate_g0,
    check_gate_g1,
    check_gate_g3,
    check_gate_g4,
    collect_worker_results,
    init_run,
    save_spec,
    transition,
)
from orchestrator.spec import (
    AssemblySkeleton,
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


def _golden_spec() -> MasterSpec:
    """Realistic two-cube spec with skeleton and complete interfaces."""
    spec = MasterSpec(
        name="Boss-Hole Assembly",
        description="Integration test: cube A (boss) mates with cube B (hole)",
        global_constraints={
            "max_mass_kg": 0.5,
            "max_envelope_mm": [50, 50, 30],
        },
        objectives=[
            Objective(name="mass", direction="minimize", unit="kg", weight=1.0),
            Objective(
                name="interface_accuracy",
                direction="minimize",
                unit="mm",
                weight=0.5,
                threshold=0.02,
            ),
        ],
        skeleton=AssemblySkeleton(
            datums={"A": [0, 0, 0], "B": [25, 0, 0]},
            shaft_axes={"mating_axis": {"origin": [0, 0, 0], "direction": [0, 0, 1]}},
        ),
    )

    ifc = Interface(
        id="ifc_mate",
        name="Boss-hole cylindrical fit",
        subsystem_a="cube_a",
        port_a="boss_top",
        subsystem_b="cube_b",
        port_b="hole_bottom",
        geometry={"type": "cylinder", "diameter_mm": 10, "depth_mm": 5},
        frame_a=CoordinateFrame(origin_mm=[0, 0, 10]),
        frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
        mating=MatingSemantic(
            type="cylindrical_fit",
            engagement_length_mm=5.0,
            orientation_rule="axis_z aligned",
        ),
        runout_or_concentricity=0.01,
        tolerances=ToleranceSchema(
            fit_class="H7/h6",
            dimensional={
                "diameter_mm": {"nominal": 10.0, "upper": 0.015, "lower": 0.0},
            },
            geometric={"concentricity_mm": 0.01},
        ),
        validation=ValidationMethod(
            measurement_tool="cad_measure_between",
            check_points=[
                ValidationCheckPoint(
                    feature="boss_diameter",
                    expected_mm=10.0,
                    tolerance_mm=0.015,
                ),
                ValidationCheckPoint(
                    feature="boss_height",
                    expected_mm=5.0,
                    tolerance_mm=0.1,
                ),
            ],
            pass_rule="all checks within tolerance",
        ),
        datum_scheme="A-B",
        ctqs=["boss_diameter", "concentricity"],
        inspection={"method": "CMM", "frequency": "100%"},
    )
    spec.interfaces.append(ifc)

    cube_a = Subsystem(
        id="sa",
        name="cube_a",
        description="20×20×10 base with Ø10×5 boss on top face",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 15],
        mass_budget_kg=0.08,
        material="6061-T6",
        interfaces=["ifc_mate"],
        specs={
            "base_width_mm": 20,
            "base_depth_mm": 20,
            "base_height_mm": 10,
            "boss_diameter_mm": 10,
            "boss_height_mm": 5,
        },
        worker_count=2,  # 2 competing variants
        manufacturing=ManufacturingSpec(
            process="CNC_milling",
            min_feature_size_mm=0.5,
            min_wall_mm=1.0,
        ),
    )
    cube_b = Subsystem(
        id="sb",
        name="cube_b",
        description="20×20×10 base with Ø10×5 pocket on bottom face",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 10],
        mass_budget_kg=0.07,
        material="6061-T6",
        interfaces=["ifc_mate"],
        specs={
            "base_width_mm": 20,
            "base_depth_mm": 20,
            "base_height_mm": 10,
            "hole_diameter_mm": 10,
            "hole_depth_mm": 5,
        },
        worker_count=1,
        manufacturing=ManufacturingSpec(
            process="CNC_milling",
            min_feature_size_mm=0.5,
            min_wall_mm=1.0,
        ),
    )
    spec.subsystems.extend([cube_a, cube_b])
    return spec


def _plant_good_output(run_dir: Path, subsystem: str, variant: int) -> None:
    """Plant a complete set of worker output files."""
    out = run_dir / f"{subsystem}_{variant}" / "output"
    out.mkdir(parents=True, exist_ok=True)

    # Fake STEP file (just needs to exist and be non-empty)
    (out / f"{subsystem}.step").write_text(
        "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    )
    (out / f"{subsystem}.stl").write_text("solid dummy\nendsolid dummy\n")
    (out / f"{subsystem}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    metadata = {
        "subsystem": subsystem,
        "claimed_mass_kg": 0.045 if subsystem == "cube_a" else 0.038,
        "claimed_bounding_box_mm": [20, 20, 15] if subsystem == "cube_a" else [20, 20, 10],
        "interface_actuals": {
            "ifc_mate": {
                "boss_diameter": 10.003,
                "boss_height": 5.01,
            },
        },
        "screenshots": [f"{subsystem}.png"],
        "deviations": [],
        "notes": "Golden test fixture",
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))


def _plant_bad_output(run_dir: Path, subsystem: str, variant: int) -> None:
    """Plant incomplete output (no STEP file)."""
    out = run_dir / f"{subsystem}_{variant}" / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "metadata.json").write_text('{"subsystem": "' + subsystem + '", "notes": "build failed"}')


class TestGoldenRunHappyPath(unittest.TestCase):
    """Full pipeline with all gates passing."""

    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Golden Test", run_dir=Path(self.td) / "golden")
        self.run.spec = _golden_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_g0_requirements_complete(self) -> None:
        ok, issues = check_gate_g0(self.run.spec)
        self.assertTrue(ok, f"G0 failed: {issues}")

    def test_g1_feasibility(self) -> None:
        ok, issues = check_gate_g1(self.run.spec)
        self.assertTrue(ok, f"G1 failed: {issues}")

    def test_g3_interfaces_complete(self) -> None:
        ok, issues = check_gate_g3(self.run.spec)
        self.assertTrue(ok, f"G3 failed: {issues}")

    def test_state_machine_happy_path_to_building(self) -> None:
        """Walk the state machine from DRAFT to BUILDING."""
        transition(self.run, SpecStatus.NORMALIZING, reason="start")
        transition(self.run, SpecStatus.COUNCIL_REVIEW, reason="normalized")
        transition(self.run, SpecStatus.LAYOUT_FROZEN, reason="skeleton approved")
        transition(self.run, SpecStatus.INTERFACES_FROZEN, reason="ICDs frozen")
        transition(self.run, SpecStatus.BUILDING, reason="dispatching")
        self.assertEqual(self.run.state.current, SpecStatus.BUILDING)

    def test_prompt_generation(self) -> None:
        """Verify correct number and content of worker prompts."""
        prompts = build_worker_prompts(self.run)
        # cube_a × 2 variants + cube_b × 1 variant = 3
        self.assertEqual(len(prompts), 3)

        # Each prompt should reference the interface
        for p in prompts:
            self.assertIn("Boss-hole cylindrical fit", p["prompt"])
            self.assertIn("cylindrical_fit", p["prompt"])

    def test_g4_with_all_good_outputs(self) -> None:
        """Plant good outputs for all workers → G4 passes."""
        _plant_good_output(self.run.run_dir, "cube_a", 0)
        _plant_good_output(self.run.run_dir, "cube_a", 1)
        _plant_good_output(self.run.run_dir, "cube_b", 0)

        ok, issues = check_gate_g4(self.run)
        self.assertTrue(ok, f"G4 failed: {issues}")

    def test_result_collection_with_good_outputs(self) -> None:
        """Collect results and verify structure."""
        _plant_good_output(self.run.run_dir, "cube_a", 0)
        _plant_good_output(self.run.run_dir, "cube_a", 1)
        _plant_good_output(self.run.run_dir, "cube_b", 0)

        results = collect_worker_results(self.run)
        self.assertEqual(len(results), 3)

        for r in results:
            self.assertEqual(r["status"], "complete")
            self.assertIn("step_files", r)
            self.assertIn("metadata", r)
            self.assertIn("interface_actuals", r["metadata"])

    def test_full_pipeline_draft_to_scoring(self) -> None:
        """Walk the entire happy path through to SCORING."""
        # Gates
        ok, _ = check_gate_g0(self.run.spec)
        self.assertTrue(ok)

        transition(self.run, SpecStatus.NORMALIZING, reason="start")
        transition(self.run, SpecStatus.COUNCIL_REVIEW, reason="normalized")

        ok, _ = check_gate_g1(self.run.spec)
        self.assertTrue(ok)

        transition(self.run, SpecStatus.LAYOUT_FROZEN, reason="skeleton ok")
        transition(self.run, SpecStatus.INTERFACES_FROZEN, reason="ICDs ok")

        ok, _ = check_gate_g3(self.run.spec)
        self.assertTrue(ok)

        transition(self.run, SpecStatus.BUILDING, reason="dispatch")

        # Plant outputs
        _plant_good_output(self.run.run_dir, "cube_a", 0)
        _plant_good_output(self.run.run_dir, "cube_a", 1)
        _plant_good_output(self.run.run_dir, "cube_b", 0)

        ok, _ = check_gate_g4(self.run)
        self.assertTrue(ok)

        transition(self.run, SpecStatus.GEOMETRY_VALIDATING, reason="artifacts present")
        transition(self.run, SpecStatus.SCORING, reason="validation passed")

        self.assertEqual(self.run.state.current, SpecStatus.SCORING)
        self.assertEqual(len(self.run.state.history), 7)

    def test_spec_persists_through_pipeline(self) -> None:
        """Verify spec is loadable at every stage."""
        transition(self.run, SpecStatus.NORMALIZING, reason="start")
        save_spec(self.run)
        loaded = MasterSpec.load(self.run.spec_path)
        self.assertEqual(loaded.status, SpecStatus.NORMALIZING)
        self.assertEqual(loaded.skeleton.datums["A"], [0, 0, 0])


class TestGoldenRunFailurePaths(unittest.TestCase):
    """Test failure detection and routing."""

    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Failure Test", run_dir=Path(self.td) / "fail")
        self.run.spec = _golden_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_g4_fails_missing_outputs(self) -> None:
        """No outputs planted → G4 fails."""
        ok, issues = check_gate_g4(self.run)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 3)

    def test_g4_fails_partial_outputs(self) -> None:
        """Only one variant has output → G4 fails for the others."""
        _plant_good_output(self.run.run_dir, "cube_a", 0)
        ok, issues = check_gate_g4(self.run)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 2)

    def test_g4_fails_incomplete_output(self) -> None:
        """Metadata but no STEP → incomplete."""
        _plant_bad_output(self.run.run_dir, "cube_a", 0)
        _plant_bad_output(self.run.run_dir, "cube_a", 1)
        _plant_bad_output(self.run.run_dir, "cube_b", 0)
        ok, issues = check_gate_g4(self.run)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 3)

    def test_result_collection_mixed(self) -> None:
        """Mix of good, bad, and missing outputs."""
        _plant_good_output(self.run.run_dir, "cube_a", 0)
        _plant_bad_output(self.run.run_dir, "cube_a", 1)
        # cube_b_0 has no output dir at all

        results = collect_worker_results(self.run)
        self.assertEqual(len(results), 3)

        statuses = {(r["subsystem"], r["variant_index"]): r["status"] for r in results}
        self.assertEqual(statuses[("cube_a", 0)], "complete")
        self.assertEqual(statuses[("cube_a", 1)], "incomplete")
        self.assertEqual(statuses[("cube_b", 0)], "missing")

    def test_g0_fails_empty_spec(self) -> None:
        """Empty spec fails G0."""
        self.run.spec = MasterSpec(name="empty")
        ok, issues = check_gate_g0(self.run.spec)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(issues), 2)

    def test_g1_fails_over_budget(self) -> None:
        """Mass budgets exceed global constraint → G1 fails."""
        self.run.spec.global_constraints["max_mass_kg"] = 0.001
        ok, issues = check_gate_g1(self.run.spec)
        self.assertFalse(ok)

    def test_g3_fails_with_bare_interface(self) -> None:
        """Adding incomplete interface → G3 fails."""
        self.run.spec.interfaces.append(Interface(id="bare", name="bare"))
        ok, issues = check_gate_g3(self.run.spec)
        self.assertFalse(ok)


class TestGoldenRunMetadataIntegrity(unittest.TestCase):
    """Verify metadata.json content is correctly parsed and accessible."""

    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("Meta Test", run_dir=Path(self.td) / "meta")
        self.run.spec = _golden_spec()
        save_spec(self.run)
        _plant_good_output(self.run.run_dir, "cube_a", 0)
        _plant_good_output(self.run.run_dir, "cube_b", 0)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_metadata_fields_present(self) -> None:
        results = collect_worker_results(self.run)
        for r in results:
            if r["status"] != "complete":
                continue
            meta = r["metadata"]
            self.assertIn("claimed_mass_kg", meta)
            self.assertIn("interface_actuals", meta)
            self.assertIn("ifc_mate", meta["interface_actuals"])

    def test_interface_actuals_values(self) -> None:
        results = collect_worker_results(self.run)
        cube_a = next(r for r in results if r["subsystem"] == "cube_a" and r["status"] == "complete")
        actuals = cube_a["metadata"]["interface_actuals"]["ifc_mate"]
        self.assertAlmostEqual(actuals["boss_diameter"], 10.003)
        self.assertAlmostEqual(actuals["boss_height"], 5.01)

    def test_claimed_mass_reasonable(self) -> None:
        results = collect_worker_results(self.run)
        for r in results:
            if r["status"] != "complete":
                continue
            mass = r["metadata"]["claimed_mass_kg"]
            self.assertGreater(mass, 0)
            self.assertLess(mass, 1.0)


if __name__ == "__main__":
    unittest.main()
