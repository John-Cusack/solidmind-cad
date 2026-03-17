"""Tests for orchestrator.validator."""
from __future__ import annotations

import unittest

from orchestrator.spec import (
    CoordinateFrame,
    FailureCode,
    Interface,
    MasterSpec,
    MatingSemantic,
    Subsystem,
    SubsystemKind,
    ValidationCheckPoint,
    ValidationMethod,
    WorkerResult,
)
from orchestrator.validator import (
    ValidationReport,
    check_gate_g5,
    validate_dimensions,
    validate_envelope,
    validate_mass,
    validate_worker_result,
)


def _make_spec() -> MasterSpec:
    spec = MasterSpec(name="test")
    spec.subsystems.append(Subsystem(
        id="s1", name="gear",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 10],
        mass_budget_kg=0.05,
        material="steel",
        interfaces=["ifc1"],
    ))
    spec.interfaces.append(Interface(
        id="ifc1", name="shaft_bore",
        subsystem_a="gear", subsystem_b="shaft",
        geometry={"diameter_mm": 8.0},
        frame_a=CoordinateFrame(origin_mm=[0, 0, 5]),
        mating=MatingSemantic(type="cylindrical_fit"),
        validation=ValidationMethod(check_points=[
            ValidationCheckPoint(feature="bore_dia", expected_mm=8.0, tolerance_mm=0.015),
            ValidationCheckPoint(feature="bore_depth", expected_mm=15.0, tolerance_mm=0.1),
        ]),
    ))
    return spec


class TestValidateDimensions(unittest.TestCase):
    def test_all_pass(self) -> None:
        spec = _make_spec()
        sub = spec.get_subsystem("gear")
        measurements = {
            "ifc1": {"bore_dia": 8.005, "bore_depth": 14.95},
        }
        checks = validate_dimensions(spec, sub, measurements)
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(c.passed for c in checks))

    def test_out_of_tolerance(self) -> None:
        spec = _make_spec()
        sub = spec.get_subsystem("gear")
        measurements = {
            "ifc1": {"bore_dia": 8.05, "bore_depth": 15.0},
        }
        checks = validate_dimensions(spec, sub, measurements)
        dia_check = next(c for c in checks if c.feature == "bore_dia")
        self.assertFalse(dia_check.passed)

    def test_missing_measurement(self) -> None:
        spec = _make_spec()
        sub = spec.get_subsystem("gear")
        measurements = {"ifc1": {"bore_dia": 8.0}}
        checks = validate_dimensions(spec, sub, measurements)
        depth_check = next(c for c in checks if c.feature == "bore_depth")
        self.assertFalse(depth_check.passed)
        self.assertIn("Missing", depth_check.error)


class TestValidateEnvelope(unittest.TestCase):
    def test_within_envelope(self) -> None:
        sub = Subsystem(name="gear", envelope_mm=[20, 20, 10])
        check = validate_envelope(sub, [19, 18, 9])
        self.assertTrue(check.passed)

    def test_exceeds_envelope(self) -> None:
        sub = Subsystem(name="gear", envelope_mm=[20, 20, 10])
        check = validate_envelope(sub, [25, 18, 9])
        self.assertFalse(check.passed)

    def test_no_constraint(self) -> None:
        sub = Subsystem(name="gear", envelope_mm=[])
        check = validate_envelope(sub, [100, 100, 100])
        self.assertTrue(check.passed)


class TestValidateMass(unittest.TestCase):
    def test_within_budget(self) -> None:
        sub = Subsystem(name="gear", mass_budget_kg=0.05)
        ok, _ = validate_mass(sub, 0.04)
        self.assertTrue(ok)

    def test_over_budget(self) -> None:
        sub = Subsystem(name="gear", mass_budget_kg=0.05)
        ok, _ = validate_mass(sub, 0.06)
        self.assertFalse(ok)

    def test_no_budget(self) -> None:
        sub = Subsystem(name="gear")
        ok, _ = validate_mass(sub, 1.0)
        self.assertTrue(ok)


class TestValidateWorkerResult(unittest.TestCase):
    def test_full_pass(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(
            spec, result,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
        )
        self.assertTrue(report.overall_pass)
        self.assertEqual(report.failure_codes, [])

    def test_dimension_failure(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(
            spec, result,
            measurements={"ifc1": {"bore_dia": 9.0, "bore_depth": 15.0}},
        )
        self.assertFalse(report.overall_pass)
        self.assertIn(FailureCode.INTERFACE_DIM_MISMATCH, report.failure_codes)


class TestCheckGateG5(unittest.TestCase):
    def test_all_pass(self) -> None:
        spec = _make_spec()
        reports = [ValidationReport(subsystem_name="gear", overall_pass=True)]
        ok, issues = check_gate_g5(spec, reports)
        self.assertTrue(ok)

    def test_failure(self) -> None:
        spec = _make_spec()
        reports = [ValidationReport(
            subsystem_name="gear", overall_pass=False,
            failure_codes=[FailureCode.INTERFACE_DIM_MISMATCH],
        )]
        ok, issues = check_gate_g5(spec, reports)
        self.assertFalse(ok)
        self.assertGreater(len(issues), 0)


class TestNoChecksFailsOverall(unittest.TestCase):
    """Phase 2a: Empty checks → overall_pass is False."""

    def test_no_checks_fails(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(spec, result)
        # No measurements, no bbox, no mass → should fail
        self.assertFalse(report.overall_pass)
        self.assertTrue(any("No checks performed" in n for n in report.notes))


class TestMeasurementSource(unittest.TestCase):
    """Phase 2b: measurement_source tracking."""

    def test_source_orchestrator(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(
            spec, result,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
            measurement_source="orchestrator",
        )
        self.assertEqual(report.measurement_source, "orchestrator")
        for dc in report.dimension_checks:
            self.assertEqual(dc.source, "orchestrator")

    def test_source_claimed(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(
            spec, result,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
            measurement_source="claimed",
        )
        self.assertEqual(report.measurement_source, "claimed")
        for dc in report.dimension_checks:
            self.assertEqual(dc.source, "claimed")
        self.assertTrue(any("claimed" in n for n in report.notes))


class TestSkeletonConstraintValidation(unittest.TestCase):
    """Phase 4b: Skeleton constraint checks in validation."""

    def test_part_exceeds_reserved_volume(self) -> None:
        from orchestrator.spec import AssemblySkeleton
        spec = _make_spec()
        spec.skeleton = AssemblySkeleton(
            reserved_volumes={
                "gear": {"origin": [0, 0, 0], "size": [20, 20, 10]},
            },
        )
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(
            spec, result,
            actual_bbox_mm=[25, 25, 15],  # exceeds 20x20x10
            actual_mass_kg=0.04,
        )
        self.assertFalse(report.overall_pass)
        self.assertTrue(any(
            not sc.passed for sc in report.skeleton_checks
        ))
        self.assertIn(FailureCode.SKELETON_CONFLICT, report.failure_codes)

    def test_part_fits_reserved_volume(self) -> None:
        from orchestrator.spec import AssemblySkeleton
        spec = _make_spec()
        spec.skeleton = AssemblySkeleton(
            reserved_volumes={
                "gear": {"origin": [0, 0, 0], "size": [20, 20, 10]},
            },
        )
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0", status="success")
        report = validate_worker_result(
            spec, result,
            actual_bbox_mm=[18, 18, 9],  # fits
            actual_mass_kg=0.04,
        )
        # Should pass skeleton check (may still fail on dimensions if no measurements)
        skeleton_fails = [
            sc for sc in report.skeleton_checks if not sc.passed
        ]
        self.assertEqual(len(skeleton_fails), 0)


if __name__ == "__main__":
    unittest.main()
