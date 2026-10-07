"""Regression tests for R1/R2 verify-mode and required-check fixes.

R1: ``validate_results(..., verify_measurements=True)`` must not silently
fall back to worker-claimed values when independent STEP verification
fails — the report must fail with non-claimed verification evidence.

R2: required dimension checkpoints and mass budgets that were never
measured must fail the report, and gate G5 must reject empty or
incomplete report sets.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.measure import MeasurementVerification
from orchestrator.runner import build_worker_prompts, init_run, validate_results
from orchestrator.spec import (
    FailureCode,
    MasterSpec,
    Subsystem,
    SubsystemKind,
    WorkerResult,
)
from orchestrator.validator import (
    check_gate_g5,
    validate_worker_result,
)
from tests.test_validator import _make_spec


def _write_worker_output(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "gear.step").write_text("deliberately fake; failure injection")
    (output_dir / "metadata.json").write_text(
        json.dumps(
            {
                "claimed_bounding_box_mm": [19, 19, 9],
                "claimed_mass_kg": 0.04,
                "interface_actuals": {
                    "ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}
                },
            }
        )
    )


def _make_two_subsystem_spec() -> MasterSpec:
    spec = _make_spec()
    spec.subsystems.append(
        Subsystem(
            id="s2",
            name="bracket",
            kind=SubsystemKind.GENERATED,
            envelope_mm=[10, 10, 10],
            material="steel",
            interfaces=[],
        )
    )
    return spec


class TestVerifyFailureBlocksSuccess(unittest.TestCase):
    """R1: verify requested but unavailable must fail, not trust claims."""

    def test_step_load_failure_fails_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = init_run("verification-failure", run_dir=Path(td) / "run")
            run.spec = _make_spec()
            out = Path(build_worker_prompts(run)[0]["output_dir"])
            _write_worker_output(out)
            with patch(
                "orchestrator.measure.verify_worker_measurements",
                return_value=MeasurementVerification(
                    step_load_ok=False, error="seeded import failure"
                ),
            ):
                reports = validate_results(run, verify_measurements=True)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertFalse(
            report.overall_pass,
            "overall_pass must be False when STEP verification fails",
        )
        self.assertNotEqual(
            report.measurement_source,
            "claimed",
            "a failed verification must not be labelled as claimed data",
        )
        self.assertTrue(
            report.failure_codes,
            "a failed verification must carry failure evidence",
        )
        self.assertTrue(
            any("verif" in n.lower() or "step" in n.lower() for n in report.notes),
            f"notes must record the verification failure; got {report.notes}",
        )

    def test_step_load_failure_fails_gate_g5(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = init_run("verification-failure", run_dir=Path(td) / "run")
            run.spec = _make_spec()
            out = Path(build_worker_prompts(run)[0]["output_dir"])
            _write_worker_output(out)
            with patch(
                "orchestrator.measure.verify_worker_measurements",
                return_value=MeasurementVerification(
                    step_load_ok=False, error="seeded import failure"
                ),
            ):
                reports = validate_results(run, verify_measurements=True)
        ok, issues = check_gate_g5(run.spec, reports)
        self.assertFalse(ok)
        self.assertGreater(len(issues), 0)

    def test_trust_mode_still_available_explicitly(self) -> None:
        """verify_measurements=False remains an explicit (non-production) choice."""
        with tempfile.TemporaryDirectory() as td:
            run = init_run("trust-mode", run_dir=Path(td) / "run")
            run.spec = _make_spec()
            out = Path(build_worker_prompts(run)[0]["output_dir"])
            _write_worker_output(out)
            reports = validate_results(run, verify_measurements=False)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.measurement_source, "claimed")
        self.assertTrue(report.overall_pass)


class TestMissingRequiredChecksFail(unittest.TestCase):
    """R2: unmeasured required checks must fail, not pass by omission."""

    def test_bbox_only_fails_with_required_bores_and_mass(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(
            spec, result, actual_bbox_mm=[19, 19, 9]
        )
        self.assertFalse(
            report.overall_pass,
            "required bore checkpoints and mass budget unmeasured → fail",
        )
        self.assertEqual(len(report.dimension_checks), 2)
        self.assertTrue(
            all(not dc.passed for dc in report.dimension_checks),
            "every unmeasured required checkpoint must fail",
        )
        self.assertFalse(report.mass_ok)
        self.assertIn(FailureCode.INTERFACE_DIM_MISMATCH, report.failure_codes)

    def test_missing_mass_with_budget_fails(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(
            spec,
            result,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=None,
        )
        self.assertFalse(
            report.overall_pass,
            "mass budget present but mass unmeasured → fail",
        )
        self.assertFalse(report.mass_ok)

    def test_zero_evidence_keeps_no_checks_note(self) -> None:
        """No evidence at all keeps the 'No checks performed' report."""
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(spec, result)
        self.assertFalse(report.overall_pass)
        self.assertTrue(
            any("No checks performed" in n for n in report.notes),
            f"zero-evidence report must note no checks ran; got {report.notes}",
        )

    def test_full_evidence_still_passes(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(
            spec,
            result,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
        )
        self.assertTrue(report.overall_pass)
        self.assertEqual(report.failure_codes, [])


class TestGateG5ReportCompleteness(unittest.TestCase):
    """R2: G5 must reject empty or incomplete report sets."""

    def test_empty_reports_fail_g5(self) -> None:
        spec = _make_spec()
        ok, issues = check_gate_g5(spec, [])
        self.assertFalse(ok)
        self.assertGreater(len(issues), 0)

    def test_incomplete_reports_fail_g5(self) -> None:
        spec = _make_two_subsystem_spec()
        gear = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        gear_report = validate_worker_result(
            spec,
            gear,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
        )
        self.assertTrue(gear_report.overall_pass)
        ok, issues = check_gate_g5(spec, [gear_report])
        self.assertFalse(ok)
        self.assertTrue(
            any("bracket" in i for i in issues),
            f"issues must name the missing subsystem; got {issues}",
        )

    def test_complete_passing_reports_pass_g5(self) -> None:
        spec = _make_two_subsystem_spec()
        gear_report = validate_worker_result(
            spec,
            WorkerResult(subsystem_name="gear", worker_id="gear_0"),
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
        )
        bracket_report = validate_worker_result(
            spec,
            WorkerResult(subsystem_name="bracket", worker_id="bracket_0"),
            actual_bbox_mm=[9, 9, 9],
        )
        self.assertTrue(bracket_report.overall_pass)
        ok, issues = check_gate_g5(spec, [gear_report, bracket_report])
        self.assertTrue(ok, f"G5 failed: {issues}")


if __name__ == "__main__":
    unittest.main()
