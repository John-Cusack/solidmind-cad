"""Verify-mode counterpart to ``tests/test_orchestrator_e2e.py``.

Closes the outer orchestrator loop against *real* worker builds instead
of fake STEP files. The test walks the same G0 → G7 gate flow as the
trust-mode test, but at Stage 4 it dispatches an in-process build from
``orchestrator.worker_builds.*`` that drives the real FreeCAD addon over
TCP, and at Stage 5 it calls ``validate_results(..., verify_measurements=True)``
so the orchestrator re-imports the produced STEP file and measures it
independently via ``orchestrator.measure.verify_worker_measurements``.

This is the test that proves Priority-stack Move 3 from
``docs/ROADMAP.md``:

    "Wire one real worker build into the outer orchestrator loop."

Each test method adds a real builder for one part class. When all five
builders land (chunks 4–8) and the drift test lands (chunk 9), the
outer loop's ◐ status in the ROADMAP flips to ✓ on those part classes.

Runtime requirements
--------------------

- A running FreeCAD addon listening on the socket
  (``127.0.0.1:9876`` by default; override with ``FREECAD_HOST`` /
  ``FREECAD_PORT`` env vars).
- The addon must have been loaded from a commit that includes
  ``cad_import_step`` (commit 36bd03e or later). Tests skip with a
  descriptive reason if the command isn't registered — this is the
  "addon running but not reloaded since the import_step commit"
  case, which happens after pulling new addon code without restarting
  FreeCAD.
- The ``solidmind_geometry`` Rust extension must be built
  (``maturin develop --manifest-path geometry/Cargo.toml``). Without
  it, ``geometry_spur_gear`` raises at import time and the builder
  fails with a clear error.

If any of the above isn't satisfied, the tests ``skipTest`` with a
message naming the missing piece. They never fail spuriously on a
clean CI runner that doesn't have FreeCAD installed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.worker_builds import common


def _freecad_with_import_step_available() -> tuple[bool, str]:
    """Return ``(ok, reason)`` for use in ``skipUnless`` decorators."""
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
class TestRealWorkerE2E(unittest.TestCase):
    """Drive the G0 → G7 gate walk against real worker builds."""

    def _make_run(self, run_dir: Path):
        """Build a minimal spec matching ``test_orchestrator_e2e.py``.

        Extracted so each per-part-class test method can reuse the same
        gate-walk fixture and only differ in which subsystem is being
        built and which builder is called.
        """
        from orchestrator.runner import init_run, save_spec, transition
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

        run = init_run("Real Worker E2E", run_dir=run_dir)
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
                envelope_mm=[22, 22, 10],  # matches module=1, teeth=20 gear
                mass_budget_kg=0.05,
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
                            feature="bore_dia",
                            expected_mm=8.0,
                            tolerance_mm=0.2,  # looser than trust-mode
                        ),
                    ],
                ),
            ),
        ]
        save_spec(run)
        return run

    def _walk_to_building(self, run) -> None:
        """Drive the run through G0 → G3 to the BUILDING state."""
        from orchestrator.runner import (
            check_gate_g0,
            check_gate_g1,
            check_gate_g2,
            check_gate_g3,
            transition,
        )
        from orchestrator.spec import SpecStatus

        ok_g0, issues_g0 = check_gate_g0(run.spec)
        self.assertTrue(ok_g0, f"G0 failed: {issues_g0}")
        transition(run, SpecStatus.COUNCIL_REVIEW, reason="G0 pass")

        ok_g1, issues_g1 = check_gate_g1(run.spec)
        self.assertTrue(ok_g1, f"G1 failed: {issues_g1}")

        ok_g2, issues_g2 = check_gate_g2(run.spec)
        self.assertTrue(ok_g2, f"G2 failed: {issues_g2}")

        transition(run, SpecStatus.LAYOUT_FROZEN, reason="G1+G2 pass")

        check_gate_g3(run.spec)  # advisory; may fail
        transition(run, SpecStatus.INTERFACES_FROZEN, reason="G3 pass")
        transition(run, SpecStatus.BUILDING, reason="dispatch workers")

    # ------------------------------------------------------------------
    # Per-part-class tests (one per chunk)
    # ------------------------------------------------------------------

    def test_sun_gear_verify_mode(self) -> None:
        """Chunk 4: build a real sun_gear, verify its measurements.

        This is the forcing function for closing the outer loop. After
        this test passes, the orchestrator has driven a real FreeCAD
        build through G0 → G7 and the validator has independently
        re-measured the produced STEP file (measurement_source is
        ``orchestrator``, not ``claimed``).
        """
        from orchestrator.runner import (
            build_worker_prompts,
            check_gate_g4,
            check_gate_g5,
            validate_results,
            transition,
        )
        from orchestrator.spec import SpecStatus
        from orchestrator.worker_builds import sun_gear

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run = self._make_run(run_dir)
            self._walk_to_building(run)

            prompts = build_worker_prompts(run)
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["subsystem"], "sun_gear")
            output_dir = Path(prompts[0]["output_dir"])

            # --- Real worker build ---
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

            # Real STEP file exists and has real content
            self.assertTrue(step_path.exists(), f"STEP not produced: {step_path}")
            self.assertGreater(
                step_path.stat().st_size,
                1024,
                "STEP file suspiciously small — real build should be > 1 KB",
            )
            step_head = step_path.read_bytes()[:100]
            self.assertTrue(
                step_head.startswith(b"ISO-10303") or b"STEP" in step_head,
                f"STEP file missing header, got: {step_head!r}",
            )
            self.assertTrue((output_dir / "sun_gear.stl").exists())
            self.assertTrue((output_dir / "metadata.json").exists())

            # G4: artifact check
            ok_g4, issues_g4 = check_gate_g4(run)
            self.assertTrue(ok_g4, f"G4 failed: {issues_g4}")
            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            # --- Stage 5: VERIFY-MODE validation ---
            # This re-imports the STEP via cad_import_step and measures
            # the bore diameter independently. The trust-mode test
            # trusted the metadata — this one doesn't.
            reports = validate_results(run, verify_measurements=True)
            self.assertEqual(len(reports), 1)
            report = reports[0]

            self.assertEqual(report.subsystem_name, "sun_gear")
            self.assertEqual(
                report.measurement_source,
                "orchestrator",
                "Validator should have re-measured the STEP, not trusted metadata",
            )

            # Bore dimension came from the re-measurement and should
            # land inside the ±0.2 mm tolerance (we're using the
            # geometry extension's exact output, so tolerance can be
            # tight once the builder is calibrated).
            bore_check = next(
                (dc for dc in report.dimension_checks if dc.feature == "bore_dia"),
                None,
            )
            self.assertIsNotNone(bore_check, "bore_dia check missing")
            self.assertTrue(
                bore_check.passed,
                f"bore_dia failed: measured={bore_check.measured_mm}, "
                f"expected={bore_check.expected_mm} ± {bore_check.tolerance_mm}",
            )
            self.assertEqual(bore_check.source, "orchestrator")

            check_gate_g5(run.spec, reports)

            # Note: we don't walk G6/G7 here — the point of this test
            # is to prove the real-build → re-measure → validate path
            # works. Gate-walk coverage for those stages is already
            # provided by test_orchestrator_e2e.py in trust mode.


if __name__ == "__main__":
    unittest.main()
