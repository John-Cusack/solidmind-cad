"""Chunk 9a: deliberate-drift detection end-to-end.

Proves the self-verifying measurement path actually catches worker
lies — not just passes them through. Builds a real ``sun_gear`` with
``bore_diameter_mm=8.0``, then uses
``common.override_claimed_measurements`` to stomp the metadata's
``bore_dia`` to 8.5 (a ~6% drift, well above the default 1%
``tolerance_rel``). The orchestrator's ``validate_results`` with
``verify_measurements=True`` must:

1. Re-import the STEP file via ``cad_import_step``.
2. Independently measure ``bore_dia`` (returning ~8.0 mm).
3. Detect that 8.0 disagrees with the claimed 8.5 by more than 1%.
4. Set ``measurement_source="orchestrator"`` (re-measurement was the
   source of truth).
5. Append ``FailureCode.MEASUREMENT_DRIFT`` to ``report.failure_codes``.
6. Set ``report.overall_pass = False``.

Skips cleanly without FreeCAD (same skip-guard pattern as the
sibling real-worker e2e tests).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.worker_builds import common


def _freecad_with_import_step_available() -> tuple[bool, str]:
    if not common.freecad_ready():
        return (
            False,
            f"FreeCAD addon not reachable on {common.fc_host()}:{common.fc_port()}",
        )
    if not common.freecad_ready_with_import_step():
        return (
            False,
            "FreeCAD addon is running but cad_import_step is not registered "
            "(reload the addon after pulling commit 36bd03e or later)",
        )
    return True, ""


_FC_OK, _FC_REASON = _freecad_with_import_step_available()


@unittest.skipUnless(_FC_OK, _FC_REASON)
class TestOrchestratorDriftE2E(unittest.TestCase):
    """Verify the orchestrator catches deliberately-stomped measurements."""

    def test_drift_caught_on_sun_gear(self) -> None:
        from orchestrator.runner import (
            build_worker_prompts,
            check_gate_g0,
            check_gate_g1,
            check_gate_g2,
            check_gate_g3,
            check_gate_g4,
            init_run,
            save_spec,
            transition,
            validate_results,
        )
        from orchestrator.spec import (
            AssemblySkeleton,
            CoordinateFrame,
            FailureCode,
            Interface,
            MatingSemantic,
            Objective,
            SpecStatus,
            Subsystem,
            SubsystemKind,
            ValidationCheckPoint,
            ValidationMethod,
        )
        from orchestrator.worker_builds import sun_gear

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run = init_run("Drift Detection E2E", run_dir=run_dir)
            transition(run, SpecStatus.NORMALIZING, reason="starting")

            run.spec.objectives = [
                Objective(
                    name="mass",
                    direction="minimize",
                    unit="kg",
                    weight=1.0,
                    threshold=0.5,
                ),
            ]
            run.spec.global_constraints = {"max_mass_kg": 0.5}
            run.spec.skeleton = AssemblySkeleton(
                datums={"A": [0, 0, 0]},
                reserved_volumes={
                    "motor": {"origin": [0, 0, 0], "size": [30, 30, 30]},
                },
            )
            run.spec.subsystems = [
                Subsystem(
                    id="s1",
                    name="sun_gear",
                    kind=SubsystemKind.GENERATED,
                    envelope_mm=[22, 22, 10],
                    mass_budget_kg=0.05,
                    material="steel",
                    interfaces=["ifc1"],
                    worker_count=1,
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
                                feature="bore_dia",
                                expected_mm=8.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
            ]
            save_spec(run)

            # Walk gates G0 -> G3 (BUILDING)
            ok_g0, issues_g0 = check_gate_g0(run.spec)
            self.assertTrue(ok_g0, f"G0 failed: {issues_g0}")
            transition(run, SpecStatus.COUNCIL_REVIEW, reason="G0 pass")
            ok_g1, _ = check_gate_g1(run.spec)
            self.assertTrue(ok_g1)
            ok_g2, _ = check_gate_g2(run.spec)
            self.assertTrue(ok_g2)
            transition(run, SpecStatus.LAYOUT_FROZEN, reason="G1+G2 pass")
            check_gate_g3(run.spec)
            transition(run, SpecStatus.INTERFACES_FROZEN, reason="G3 pass")
            transition(run, SpecStatus.BUILDING, reason="dispatch workers")

            prompts = build_worker_prompts(run)
            output_dir = Path(prompts[0]["output_dir"])

            # Real build (bore = 8.0 mm).
            step_path = sun_gear.build_sun_gear(
                sub_spec={
                    "name": "sun_gear",
                    "module": 1.0,
                    "teeth": 20,
                    "thickness_mm": 8.0,
                    "bore_diameter_mm": 8.0,
                },
                output_dir=output_dir,
            )
            self.assertTrue(step_path.exists())

            # --- Drift injection ---
            # Stomp claimed bore_dia from 8.0 to 8.5 (~6% drift, well above 1%).
            stomped = common.override_claimed_measurements(
                output_dir,
                {"ifc1": {"bore_dia": 8.5}},
            )
            self.assertEqual(stomped["interface_actuals"]["ifc1"]["bore_dia"], 8.5)

            # G4 still passes (artifacts exist; this gate doesn't measure).
            ok_g4, _ = check_gate_g4(run)
            self.assertTrue(ok_g4)
            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            # --- Verify-mode validation should detect the drift ---
            reports = validate_results(run, verify_measurements=True)
            self.assertEqual(len(reports), 1)
            report = reports[0]

            # Source of truth must be orchestrator's re-measurement,
            # not the stomped metadata.
            self.assertEqual(
                report.measurement_source,
                "orchestrator",
                "measurement_source should be 'orchestrator' even after "
                "metadata stomp — the re-measurement is authoritative",
            )

            # Drift must be flagged.
            self.assertIn(
                FailureCode.MEASUREMENT_DRIFT,
                report.failure_codes,
                f"MEASUREMENT_DRIFT should be in failure_codes; got "
                f"{report.failure_codes}",
            )
            self.assertFalse(
                report.overall_pass,
                "overall_pass must be False when drift is detected",
            )

            # Notes should mention the drift (human-readable diagnostics).
            joined_notes = " ".join(report.notes)
            self.assertIn(
                "MEASUREMENT_DRIFT",
                joined_notes,
                f"Notes should reference MEASUREMENT_DRIFT; got {report.notes}",
            )


if __name__ == "__main__":
    unittest.main()
