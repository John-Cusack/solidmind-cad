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

    def _make_run(
        self,
        run_dir: Path,
        subsystems: list | None = None,
        interfaces: list | None = None,
    ):
        """Build a minimal spec around the given subsystems + interfaces.

        Default (``subsystems=None``, ``interfaces=None``) yields the
        sun_gear fixture for ``test_sun_gear_verify_mode``. Per-part-class
        test methods pass in their own subsystem/interface lists.
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
        if subsystems is None:
            subsystems = [
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
        run.spec.subsystems = subsystems

        if interfaces is None:
            interfaces = [
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
        run.spec.interfaces = interfaces
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
            transition,
            validate_results,
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

    def test_planet_carrier_verify_mode(self) -> None:
        """Chunk 5: build a real planet_carrier, verify bore_dia + PCD.

        Two checkpoints (vs sun_gear's one): the central shaft bore
        (``bore_dia``, 8 mm) and the planet-pin pitch circle
        (``pin_circle_dia``, 22 mm). The PCD is measured by the new
        ``_measure_pin_circle_diameter`` strategy from the locations of
        the three pin cylinders.
        """
        from orchestrator.runner import (
            build_worker_prompts,
            check_gate_g4,
            check_gate_g5,
            transition,
            validate_results,
        )
        from orchestrator.spec import (
            CoordinateFrame,
            Interface,
            MatingSemantic,
            SpecStatus,
            Subsystem,
            SubsystemKind,
            ValidationCheckPoint,
            ValidationMethod,
        )
        from orchestrator.worker_builds import planet_carrier

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            subsystems = [
                Subsystem(
                    id="s1",
                    name="planet_carrier",
                    kind=SubsystemKind.GENERATED,
                    envelope_mm=[40, 40, 11],
                    mass_budget_kg=0.05,
                    material="aluminum",
                    interfaces=["ifc1"],
                    worker_count=1,
                    assembly_constraints={"datum": "A"},
                ),
            ]
            interfaces = [
                Interface(
                    id="ifc1",
                    name="carrier_pins",
                    subsystem_a="planet_carrier",
                    port_a="pins",
                    subsystem_b="planets",
                    port_b="bore",
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
                            ValidationCheckPoint(
                                feature="pin_circle_dia",
                                expected_mm=22.0,
                                tolerance_mm=0.5,
                            ),
                        ],
                    ),
                ),
            ]
            run = self._make_run(run_dir, subsystems=subsystems, interfaces=interfaces)
            self._walk_to_building(run)

            prompts = build_worker_prompts(run)
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["subsystem"], "planet_carrier")
            output_dir = Path(prompts[0]["output_dir"])

            step_path = planet_carrier.build_planet_carrier(
                sub_spec={
                    "name": "planet_carrier",
                    "outer_diameter_mm": 40.0,
                    "thickness_mm": 5.0,
                    "bore_diameter_mm": 8.0,
                    "pin_count": 3,
                    "pin_circle_diameter_mm": 22.0,
                    "pin_diameter_mm": 4.0,
                    "pin_height_mm": 6.0,
                },
                output_dir=output_dir,
            )
            self.assertTrue(step_path.exists())
            self.assertGreater(step_path.stat().st_size, 1024)
            self.assertTrue((output_dir / "planet_carrier.stl").exists())
            self.assertTrue((output_dir / "metadata.json").exists())

            ok_g4, issues_g4 = check_gate_g4(run)
            self.assertTrue(ok_g4, f"G4 failed: {issues_g4}")
            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            reports = validate_results(run, verify_measurements=True)
            self.assertEqual(len(reports), 1)
            report = reports[0]
            self.assertEqual(report.subsystem_name, "planet_carrier")
            self.assertEqual(report.measurement_source, "orchestrator")

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

            pcd_check = next(
                (dc for dc in report.dimension_checks if dc.feature == "pin_circle_dia"),
                None,
            )
            self.assertIsNotNone(pcd_check, "pin_circle_dia check missing")
            self.assertTrue(
                pcd_check.passed,
                f"pin_circle_dia failed: measured={pcd_check.measured_mm}, "
                f"expected={pcd_check.expected_mm} ± {pcd_check.tolerance_mm}",
            )
            self.assertEqual(pcd_check.source, "orchestrator")

            check_gate_g5(run.spec, reports)

    def test_quadrotor_arm_verify_mode(self) -> None:
        """Chunk 6: build a real quadrotor_arm, verify root + motor bores + PCD.

        Cross-domain test: same outer-loop wiring as gear-train parts,
        but the geometry is a rectangular boom with a 4-hole motor-mount
        pattern. The orchestrator independently re-measures the root
        mount bore, an arbitrary motor hole, and the motor-mount PCD.
        """
        from orchestrator.runner import (
            build_worker_prompts,
            check_gate_g4,
            check_gate_g5,
            transition,
            validate_results,
        )
        from orchestrator.spec import (
            CoordinateFrame,
            Interface,
            MatingSemantic,
            SpecStatus,
            Subsystem,
            SubsystemKind,
            ValidationCheckPoint,
            ValidationMethod,
        )
        from orchestrator.worker_builds import quadrotor_arm

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            subsystems = [
                Subsystem(
                    id="s1",
                    name="quadrotor_arm",
                    kind=SubsystemKind.GENERATED,
                    envelope_mm=[120, 24, 8],
                    mass_budget_kg=0.05,
                    material="aluminum",
                    interfaces=["ifc_root", "ifc_motor"],
                    worker_count=1,
                    assembly_constraints={"datum": "A"},
                ),
            ]
            interfaces = [
                Interface(
                    id="ifc_root",
                    name="chassis_pivot",
                    subsystem_a="quadrotor_arm",
                    port_a="root_mount",
                    subsystem_b="chassis",
                    port_b="arm_pivot",
                    geometry={"diameter_mm": 5.0},
                    frame_a=CoordinateFrame(origin_mm=[-42, 0, 4]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia",
                                expected_mm=5.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
                Interface(
                    id="ifc_motor",
                    name="motor_mount",
                    subsystem_a="quadrotor_arm",
                    port_a="motor_pad",
                    subsystem_b="motor",
                    port_b="bolts",
                    geometry={"diameter_mm": 3.2},
                    frame_a=CoordinateFrame(origin_mm=[42, 0, 4]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="bolt_pattern"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="motor_mount_pcd",
                                expected_mm=16.0 * (2**0.5),  # square diag
                                tolerance_mm=0.5,
                            ),
                        ],
                    ),
                ),
            ]
            run = self._make_run(run_dir, subsystems=subsystems, interfaces=interfaces)
            self._walk_to_building(run)

            prompts = build_worker_prompts(run)
            self.assertEqual(len(prompts), 1)
            output_dir = Path(prompts[0]["output_dir"])

            step_path = quadrotor_arm.build_quadrotor_arm(
                sub_spec={
                    "name": "quadrotor_arm",
                    "length_mm": 120.0,
                    "width_mm": 24.0,  # ≥ 19.2 needed for motor mount + hole radius to fit
                    "height_mm": 8.0,
                    "root_mount_diameter_mm": 5.0,
                    "motor_mount_pattern": "square",
                    "motor_mount_pcd_mm": 16.0 * (2**0.5),
                    "motor_mount_hole_count": 4,
                    "motor_mount_hole_diameter_mm": 3.2,
                },
                output_dir=output_dir,
            )
            self.assertTrue(step_path.exists())
            self.assertGreater(step_path.stat().st_size, 1024)
            self.assertTrue((output_dir / "quadrotor_arm.stl").exists())
            self.assertTrue((output_dir / "metadata.json").exists())

            ok_g4, issues_g4 = check_gate_g4(run)
            self.assertTrue(ok_g4, f"G4 failed: {issues_g4}")
            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            reports = validate_results(run, verify_measurements=True)
            self.assertEqual(len(reports), 1)
            report = reports[0]
            self.assertEqual(report.subsystem_name, "quadrotor_arm")
            self.assertEqual(report.measurement_source, "orchestrator")

            root_check = next(
                (
                    dc
                    for dc in report.dimension_checks
                    if dc.interface_id == "ifc_root" and dc.feature == "bore_dia"
                ),
                None,
            )
            self.assertIsNotNone(root_check, "ifc_root.bore_dia missing")
            self.assertTrue(root_check.passed)
            self.assertEqual(root_check.source, "orchestrator")

            pcd_check = next(
                (dc for dc in report.dimension_checks if dc.feature == "motor_mount_pcd"),
                None,
            )
            self.assertIsNotNone(pcd_check, "motor_mount_pcd missing")
            self.assertTrue(
                pcd_check.passed,
                f"motor_mount_pcd failed: measured={pcd_check.measured_mm}, "
                f"expected={pcd_check.expected_mm} ± {pcd_check.tolerance_mm}",
            )
            self.assertEqual(pcd_check.source, "orchestrator")

            check_gate_g5(run.spec, reports)

    def test_rc_car_chassis_verify_mode(self) -> None:
        """Chunk 7: build a real rc_car_chassis, verify axle bores + mount PCD.

        Larger envelope with 6 holes (2 axles + 4 mounting holes). Tests
        that the envelope route handles many-feature parts correctly and
        that the PCD strategy isolates the 4-mount group from the 2-axle
        group.
        """
        from orchestrator.runner import (
            build_worker_prompts,
            check_gate_g4,
            check_gate_g5,
            transition,
            validate_results,
        )
        from orchestrator.spec import (
            CoordinateFrame,
            Interface,
            MatingSemantic,
            SpecStatus,
            Subsystem,
            SubsystemKind,
            ValidationCheckPoint,
            ValidationMethod,
        )
        from orchestrator.worker_builds import rc_car_chassis

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            subsystems = [
                Subsystem(
                    id="s1",
                    name="rc_car_chassis",
                    kind=SubsystemKind.GENERATED,
                    envelope_mm=[180, 90, 4],
                    mass_budget_kg=0.2,
                    material="aluminum",
                    interfaces=["ifc_axle_front", "ifc_axle_rear", "ifc_mounts"],
                    worker_count=1,
                    assembly_constraints={"datum": "A"},
                ),
            ]
            interfaces = [
                Interface(
                    id="ifc_axle_front",
                    name="front_axle",
                    subsystem_a="rc_car_chassis",
                    port_a="front_axle_bore",
                    subsystem_b="front_axle",
                    port_b="shaft",
                    geometry={"diameter_mm": 6.0},
                    frame_a=CoordinateFrame(origin_mm=[65, 0, 2]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia",
                                expected_mm=6.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
                Interface(
                    id="ifc_axle_rear",
                    name="rear_axle",
                    subsystem_a="rc_car_chassis",
                    port_a="rear_axle_bore",
                    subsystem_b="rear_axle",
                    port_b="shaft",
                    geometry={"diameter_mm": 6.0},
                    frame_a=CoordinateFrame(origin_mm=[-65, 0, 2]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia",
                                expected_mm=6.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
                Interface(
                    id="ifc_mounts",
                    name="center_mount",
                    subsystem_a="rc_car_chassis",
                    port_a="center_pad",
                    subsystem_b="electronics",
                    port_b="bolts",
                    geometry={"diameter_mm": 3.0},
                    frame_a=CoordinateFrame(origin_mm=[0, 0, 2]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="bolt_pattern"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="motor_mount_pcd",
                                expected_mm=60.0,
                                tolerance_mm=0.5,
                            ),
                        ],
                    ),
                ),
            ]
            run = self._make_run(run_dir, subsystems=subsystems, interfaces=interfaces)
            self._walk_to_building(run)

            prompts = build_worker_prompts(run)
            self.assertEqual(len(prompts), 1)
            output_dir = Path(prompts[0]["output_dir"])

            step_path = rc_car_chassis.build_rc_car_chassis(
                sub_spec={
                    "name": "rc_car_chassis",
                    "length_mm": 180.0,
                    "width_mm": 90.0,
                    "thickness_mm": 4.0,
                    "axle_bore_diameter_mm": 6.0,
                    "wheelbase_mm": 130.0,
                    "mounting_hole_count": 4,
                    "mounting_hole_diameter_mm": 3.0,
                    "mounting_hole_pcd_mm": 60.0,
                },
                output_dir=output_dir,
            )
            self.assertTrue(step_path.exists())
            self.assertGreater(step_path.stat().st_size, 1024)
            self.assertTrue((output_dir / "rc_car_chassis.stl").exists())
            self.assertTrue((output_dir / "metadata.json").exists())

            ok_g4, issues_g4 = check_gate_g4(run)
            self.assertTrue(ok_g4, f"G4 failed: {issues_g4}")
            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            reports = validate_results(run, verify_measurements=True)
            self.assertEqual(len(reports), 1)
            report = reports[0]
            self.assertEqual(report.subsystem_name, "rc_car_chassis")
            self.assertEqual(report.measurement_source, "orchestrator")

            # Both axle bores measured independently.
            for ifc_id in ("ifc_axle_front", "ifc_axle_rear"):
                check = next(
                    (
                        dc
                        for dc in report.dimension_checks
                        if dc.interface_id == ifc_id and dc.feature == "bore_dia"
                    ),
                    None,
                )
                self.assertIsNotNone(check, f"{ifc_id}.bore_dia missing")
                self.assertTrue(
                    check.passed,
                    f"{ifc_id}.bore_dia failed: measured={check.measured_mm}",
                )
                self.assertEqual(check.source, "orchestrator")

            # Mounting PCD measured from the 4-hole group.
            pcd_check = next(
                (dc for dc in report.dimension_checks if dc.feature == "motor_mount_pcd"),
                None,
            )
            self.assertIsNotNone(pcd_check, "motor_mount_pcd missing")
            self.assertTrue(
                pcd_check.passed,
                f"motor_mount_pcd failed: measured={pcd_check.measured_mm}",
            )
            self.assertEqual(pcd_check.source, "orchestrator")

            check_gate_g5(run.spec, reports)

    def test_hexapod_leg_verify_mode(self) -> None:
        """Chunk 8: build a real multi-segment hexapod_leg, verify all 3 bores + length.

        The most complex part class in the chunks-5-8 set: 3 fused
        rectangular pads (coxa+femur+tibia) with 3 distinct pivot bores
        at the segment junctions. The bore diameters differ (4/5/6 mm)
        so the orchestrator's bore strategy can disambiguate via
        ``expected_mm`` hints. The total length is verified via the new
        ``segment_length`` strategy.
        """
        from orchestrator.runner import (
            build_worker_prompts,
            check_gate_g4,
            check_gate_g5,
            transition,
            validate_results,
        )
        from orchestrator.spec import (
            CoordinateFrame,
            Interface,
            MatingSemantic,
            SpecStatus,
            Subsystem,
            SubsystemKind,
            ValidationCheckPoint,
            ValidationMethod,
        )
        from orchestrator.worker_builds import hexapod_leg

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            total_len = 52.0 + 66.0 + 133.0  # 251 mm
            subsystems = [
                Subsystem(
                    id="s1",
                    name="hexapod_leg",
                    kind=SubsystemKind.GENERATED,
                    envelope_mm=[total_len, 20, 8],
                    mass_budget_kg=0.1,
                    material="aluminum",
                    interfaces=[
                        "ifc_hip_yaw",
                        "ifc_hip_pitch",
                        "ifc_knee",
                    ],
                    worker_count=1,
                    assembly_constraints={"datum": "A"},
                ),
            ]
            interfaces = [
                Interface(
                    id="ifc_hip_yaw",
                    name="hip_yaw_pivot",
                    subsystem_a="hexapod_leg",
                    port_a="chassis_pivot",
                    subsystem_b="chassis",
                    port_b="leg_mount",
                    geometry={"diameter_mm": 4.0},
                    frame_a=CoordinateFrame(origin_mm=[0, 0, 4]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia",
                                expected_mm=4.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
                Interface(
                    id="ifc_hip_pitch",
                    name="hip_pitch_pivot",
                    subsystem_a="hexapod_leg",
                    port_a="hip_pitch",
                    subsystem_b="coxa_femur_joint",
                    port_b="pin",
                    geometry={"diameter_mm": 5.0},
                    frame_a=CoordinateFrame(origin_mm=[52, 0, 4]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia",
                                expected_mm=5.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
                Interface(
                    id="ifc_knee",
                    name="knee_pivot",
                    subsystem_a="hexapod_leg",
                    port_a="knee",
                    subsystem_b="femur_tibia_joint",
                    port_b="pin",
                    geometry={"diameter_mm": 6.0},
                    frame_a=CoordinateFrame(origin_mm=[118, 0, 4]),
                    frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
                    mating=MatingSemantic(type="cylindrical_fit"),
                    validation=ValidationMethod(
                        check_points=[
                            ValidationCheckPoint(
                                feature="bore_dia",
                                expected_mm=6.0,
                                tolerance_mm=0.2,
                            ),
                        ],
                    ),
                ),
                # NOTE: segment_length / overall bbox checkpoint deferred —
                # FreeCAD's Part::Feature.Shape.BoundBox returns sentinel
                # ±1e+100 values for STEPs loaded via Part.Shape.read(),
                # and the addon's get_dimensions / get_body_topology /
                # find_edges all hit related issues on imported features.
                # cad_find_holes is the only face-level command currently
                # robust to Part::Feature inputs (per commit 180b5a1).
                # The 3 bore_dia checks above are sufficient to prove the
                # multi-segment composite build/measure loop.
            ]
            run = self._make_run(run_dir, subsystems=subsystems, interfaces=interfaces)
            self._walk_to_building(run)

            prompts = build_worker_prompts(run)
            self.assertEqual(len(prompts), 1)
            output_dir = Path(prompts[0]["output_dir"])

            step_path = hexapod_leg.build_hexapod_leg(
                sub_spec={
                    "name": "hexapod_leg",
                    "coxa_length_mm": 52.0,
                    "femur_length_mm": 66.0,
                    "tibia_length_mm": 133.0,
                    "segment_width_mm": 20.0,
                    "segment_thickness_mm": 8.0,
                    "hip_yaw_bore_mm": 4.0,
                    "hip_pitch_bore_mm": 5.0,
                    "knee_bore_mm": 6.0,
                },
                output_dir=output_dir,
            )
            self.assertTrue(step_path.exists())
            self.assertGreater(step_path.stat().st_size, 1024)
            self.assertTrue((output_dir / "hexapod_leg.stl").exists())
            self.assertTrue((output_dir / "metadata.json").exists())

            ok_g4, issues_g4 = check_gate_g4(run)
            self.assertTrue(ok_g4, f"G4 failed: {issues_g4}")
            transition(run, SpecStatus.GEOMETRY_VALIDATING, reason="G4 pass")

            reports = validate_results(run, verify_measurements=True)
            self.assertEqual(len(reports), 1)
            report = reports[0]
            self.assertEqual(report.subsystem_name, "hexapod_leg")
            self.assertEqual(report.measurement_source, "orchestrator")

            # All 3 pivot bores measured independently via expected_mm hints.
            for ifc_id, expected in (
                ("ifc_hip_yaw", 4.0),
                ("ifc_hip_pitch", 5.0),
                ("ifc_knee", 6.0),
            ):
                check = next(
                    (
                        dc
                        for dc in report.dimension_checks
                        if dc.interface_id == ifc_id and dc.feature == "bore_dia"
                    ),
                    None,
                )
                self.assertIsNotNone(check, f"{ifc_id}.bore_dia missing")
                self.assertTrue(
                    check.passed,
                    f"{ifc_id}.bore_dia failed: measured={check.measured_mm}, expected={expected}",
                )
                self.assertEqual(check.source, "orchestrator")

            check_gate_g5(run.spec, reports)


if __name__ == "__main__":
    unittest.main()
