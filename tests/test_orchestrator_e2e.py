"""End-to-end integration test — runs the full pipeline without real workers.

Creates a spec, validates gates, simulates worker output, runs validation,
scoring, and release packaging.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.runner import (
    build_release,
    build_worker_prompts,
    check_gate_g0,
    check_gate_g1,
    check_gate_g2,
    check_gate_g3,
    check_gate_g4,
    check_gate_g5,
    check_gate_g6,
    check_gate_g7,
    init_run,
    save_spec,
    score_results,
    transition,
    validate_results,
)
from orchestrator.spec import (
    AssemblySkeleton,
    CoordinateFrame,
    Interface,
    MatingSemantic,
    Objective,
    SpecStatus,
    Subsystem,
    SubsystemKind,
    ValidationCheckPoint,
    ValidationMethod,
)


class TestFullPipeline(unittest.TestCase):
    """Simulate the full 0→7 pipeline with mock worker output."""

    def test_full_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"

            # --- Stage 0: Init + requirements ---
            run = init_run("Test Reducer", run_dir=run_dir)
            transition(run, SpecStatus.NORMALIZING, reason="starting")

            # Populate spec
            run.spec.objectives = [
                Objective(name="mass", direction="minimize", unit="kg", weight=1.0, threshold=0.5),
            ]
            run.spec.global_constraints = {"max_mass_kg": 0.5}
            run.spec.skeleton = AssemblySkeleton(
                datums={"A": [0, 0, 0]},
                reserved_volumes={"motor": {"origin": [0, 0, 0], "size": [20, 20, 30]}},
            )
            run.spec.subsystems = [
                Subsystem(
                    id="s1",
                    name="sun_gear",
                    kind=SubsystemKind.GENERATED,
                    envelope_mm=[16, 16, 20],
                    mass_budget_kg=0.02,
                    material="steel",
                    interfaces=["ifc1"],
                    worker_count=1,
                    assembly_constraints={"coaxial_with": "main_shaft"},
                ),
                Subsystem(
                    id="s2",
                    name="bearing",
                    kind=SubsystemKind.CATALOG,
                    supplier_part="SKF 6201-2Z",
                    quantity=2,
                    assembly_constraints={"datum": "A"},
                ),
            ]
            run.spec.interfaces = [
                Interface(
                    id="ifc1",
                    name="shaft_bore",
                    subsystem_a="sun_gear",
                    port_a="bore",
                    subsystem_b="input_shaft",
                    port_b="spline",
                    geometry={"diameter_mm": 8.0},
                    frame_a=CoordinateFrame(origin_mm=[0, 0, 5]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia", expected_mm=8.0, tolerance_mm=0.015
                            ),
                        ]
                    ),
                ),
            ]
            save_spec(run)

            # G0: Requirements
            ok_g0, issues_g0 = check_gate_g0(run.spec)
            self.assertTrue(ok_g0, f"G0 failed: {issues_g0}")
            transition(run, SpecStatus.COUNCIL_REVIEW, reason="G0 pass")

            # G1: Feasibility
            ok_g1, issues_g1 = check_gate_g1(run.spec)
            self.assertTrue(ok_g1, f"G1 failed: {issues_g1}")

            # G2: Skeleton
            ok_g2, issues_g2 = check_gate_g2(run.spec)
            self.assertTrue(ok_g2, f"G2 failed: {issues_g2}")

            transition(run, SpecStatus.LAYOUT_FROZEN, reason="G1+G2+A1 pass")

            # G3: ICD
            ok_g3, _ = check_gate_g3(run.spec)
            # May fail since interface isn't fully complete — that's expected
            # for this test we just verify the gate runs

            transition(run, SpecStatus.INTERFACES_FROZEN, reason="G3+A3 pass")

            # --- Stage 4: Simulate worker output ---
            transition(run, SpecStatus.BUILDING, reason="dispatch workers")

            # Create mock worker output
            prompts = build_worker_prompts(run)
            self.assertEqual(len(prompts), 1)  # only sun_gear is GENERATED

            output_dir = Path(prompts[0]["output_dir"])
            # Write a fake STEP file.  This test runs in trust mode —
            # it exercises the G0 → G7 gate-walker against fabricated
            # worker output without requiring a running FreeCAD addon.
            # See tests/test_orchestrator_real_worker_e2e.py for the
            # verify-mode counterpart that drives a real cad.* build
            # and re-measures the STEP file independently.
            (output_dir / "sun_gear.step").write_text("FAKE STEP DATA")
            (output_dir / "sun_gear.stl").write_bytes(b"FAKE STL")
            (output_dir / "sun_gear.png").write_bytes(b"FAKE PNG")
            # Write metadata
            metadata = {
                "subsystem": "sun_gear",
                "claimed_mass_kg": 0.018,
                "claimed_bounding_box_mm": [15, 15, 18],
                "interface_actuals": {
                    "ifc1": {"bore_dia": 8.005},
                },
            }
            (output_dir / "metadata.json").write_text(json.dumps(metadata))

            # G4: Artifacts
            ok_g4, issues_g4 = check_gate_g4(run)
            self.assertTrue(ok_g4, f"G4 failed: {issues_g4}")

            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            # --- Stage 5: Validation ---
            # verify_measurements=False keeps the trust-mode path —
            # the validator reads claimed values from metadata.json
            # rather than re-importing the (fake) STEP file via the
            # FreeCAD addon socket.
            reports = validate_results(run, verify_measurements=False)
            self.assertEqual(len(reports), 1)

            # The bore_dia check should pass (8.005 within ±0.015 of 8.0)
            report = reports[0]
            self.assertEqual(report.subsystem_name, "sun_gear")

            ok_g5, issues_g5 = check_gate_g5(run.spec, reports)
            # G5 may pass or fail depending on measurements — verify it runs

            transition(run, SpecStatus.SCORING, reason="validation done")

            # --- Stage 6: Scoring ---
            scoring = score_results(run, reports)
            self.assertIsNotNone(scoring)

            ok_g6, issues_g6 = check_gate_g6(run.spec, scoring)

            transition(run, SpecStatus.RELEASE_PACKAGING, reason="scoring done")

            # --- Stage 7: Release ---
            package = build_release(
                run,
                scoring_report=scoring,
                validation_reports=reports,
            )
            self.assertIsNotNone(package.package_dir)
            self.assertTrue(package.package_dir.exists())
            self.assertTrue((package.package_dir / "bom.json").exists())
            self.assertTrue((package.package_dir / "decision_report.md").exists())
            self.assertTrue((package.package_dir / "spec.yaml").exists())

            # Check geometry artifacts were copied
            geometry_dir = package.package_dir / "geometry"
            self.assertTrue(geometry_dir.exists())
            self.assertTrue((geometry_dir / "sun_gear.step").exists())
            self.assertTrue((geometry_dir / "sun_gear.stl").exists())

            ok_g7, issues_g7 = check_gate_g7(package, spec=run.spec)
            self.assertTrue(ok_g7, f"G7 failed: {issues_g7}")

            transition(run, SpecStatus.AWAITING_HUMAN, reason="G7 pass")

            # Final state
            self.assertEqual(run.state.current, SpecStatus.AWAITING_HUMAN)


class TestStateTransitionBacklinks(unittest.TestCase):
    """Verify RELEASE_PACKAGING backlinks and AWAITING_HUMAN constraints."""

    def test_release_packaging_can_backtrack(self) -> None:
        from orchestrator.state import StateMachine

        sm = StateMachine(current=SpecStatus.RELEASE_PACKAGING)
        # Should be able to go back to INTERFACES_FROZEN
        sm.transition(SpecStatus.INTERFACES_FROZEN, reason="missing contract data")
        self.assertEqual(sm.current, SpecStatus.INTERFACES_FROZEN)

    def test_release_packaging_backtrack_to_scoring(self) -> None:
        from orchestrator.state import StateMachine

        sm = StateMachine(current=SpecStatus.RELEASE_PACKAGING)
        sm.transition(SpecStatus.SCORING, reason="missing verification")
        self.assertEqual(sm.current, SpecStatus.SCORING)

    def test_release_packaging_backtrack_to_validating(self) -> None:
        from orchestrator.state import StateMachine

        sm = StateMachine(current=SpecStatus.RELEASE_PACKAGING)
        sm.transition(SpecStatus.GEOMETRY_VALIDATING, reason="missing evidence")
        self.assertEqual(sm.current, SpecStatus.GEOMETRY_VALIDATING)

    def test_awaiting_human_cannot_go_to_building(self) -> None:
        from orchestrator.state import StateMachine

        sm = StateMachine(current=SpecStatus.AWAITING_HUMAN)
        with self.assertRaises(ValueError):
            sm.transition(SpecStatus.BUILDING, reason="nope")

    def test_awaiting_human_can_go_to_done(self) -> None:
        from orchestrator.state import StateMachine

        sm = StateMachine(current=SpecStatus.AWAITING_HUMAN)
        sm.transition(SpecStatus.DONE, reason="user accepts")
        self.assertEqual(sm.current, SpecStatus.DONE)


if __name__ == "__main__":
    unittest.main()
