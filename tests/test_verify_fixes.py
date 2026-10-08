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
from orchestrator.scorer import build_variants
from orchestrator.spec import (
    AssemblySkeleton,
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


def _write_worker_output(output_dir: Path, *, claim_bbox: bool = True) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "gear.step").write_text("deliberately fake; failure injection")
    metadata: dict = {
        "claimed_mass_kg": 0.04,
        "interface_actuals": {"ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}},
    }
    if claim_bbox:
        metadata["claimed_bounding_box_mm"] = [19, 19, 9]
    (output_dir / "metadata.json").write_text(json.dumps(metadata))


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
        self.assertEqual(
            report.failure_codes,
            [FailureCode.VERIFICATION_FAILED],
            "a failed verification must carry verification-failure evidence",
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


class TestClaimedFieldsLabelledInVerifyMode(unittest.TestCase):
    """Verify mode must label worker-claimed bbox/mass as claimed, per field."""

    def _validate_with(self, verification: MeasurementVerification):
        with tempfile.TemporaryDirectory() as td:
            run = init_run("claimed-fields", run_dir=Path(td) / "run")
            run.spec = _make_spec()
            out = Path(build_worker_prompts(run)[0]["output_dir"])
            _write_worker_output(out)
            with patch(
                "orchestrator.measure.verify_worker_measurements",
                return_value=verification,
            ):
                reports = validate_results(run, verify_measurements=True)
        self.assertEqual(len(reports), 1)
        return reports[0]

    def test_sentinel_without_fallback_fails_verify(self) -> None:
        """Sentinel bbox with no fallback measurement fails, not claims.

        Verify mode never substitutes the worker's claimed bbox/mass:
        unmeasured required bbox and mass fail the report with
        VERIFICATION_FAILED evidence.
        """
        report = self._validate_with(
            MeasurementVerification(
                step_load_ok=True,
                bbox_measured_mm=[],
                mass_measured_kg=None,
                interface_actuals_measured={"ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}},
            )
        )
        self.assertEqual(report.measurement_source, "orchestrator")
        self.assertEqual(report.bbox_source, "unknown")
        self.assertEqual(report.mass_source, "unknown")
        self.assertEqual(report.envelope_check.actual_bbox_mm, [])
        self.assertIsNone(report.mass_kg)
        self.assertFalse(report.overall_pass)
        self.assertEqual(report.failure_codes, [FailureCode.VERIFICATION_FAILED])
        self.assertFalse(
            any("worker-claimed" in n for n in report.notes),
            f"verify mode must not use claims; got {report.notes}",
        )

    def test_measured_bbox_and_mass_labelled_orchestrator(self) -> None:
        report = self._validate_with(
            MeasurementVerification(
                step_load_ok=True,
                bbox_measured_mm=[19, 18, 9],
                mass_measured_kg=0.04,
                interface_actuals_measured={"ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}},
            )
        )
        self.assertEqual(report.bbox_source, "orchestrator")
        self.assertEqual(report.mass_source, "orchestrator")
        self.assertEqual(report.envelope_check.actual_bbox_mm, [19, 18, 9])
        self.assertEqual(report.mass_kg, 0.04)
        self.assertFalse(any("bounding box is worker-claimed" in n for n in report.notes))
        self.assertFalse(any("mass is worker-claimed" in n for n in report.notes))
        self.assertTrue(report.overall_pass)


class TestMissingRequiredChecksFail(unittest.TestCase):
    """R2: unmeasured required checks must fail, not pass by omission."""

    def test_bbox_only_fails_with_required_bores_and_mass(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(spec, result, actual_bbox_mm=[19, 19, 9])
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
        self.assertEqual(report.failure_codes, [FailureCode.VERIFICATION_FAILED])

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
        self.assertEqual(report.failure_codes, [FailureCode.VERIFICATION_FAILED])

    def test_mass_over_budget_keeps_over_budget_code(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(
            spec,
            result,
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.1,
        )
        self.assertFalse(report.overall_pass)
        self.assertEqual(report.failure_codes, [FailureCode.MASS_OVER_BUDGET])

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


class TestMeasuredDimensionMismatch(unittest.TestCase):
    """A measured out-of-tolerance dimension keeps INTERFACE_DIM_MISMATCH."""

    def test_measured_mismatch_keeps_dim_code(self) -> None:
        spec = _make_spec()
        result = WorkerResult(subsystem_name="gear", worker_id="gear_0")
        report = validate_worker_result(
            spec,
            result,
            measurements={"ifc1": {"bore_dia": 9.0, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 18, 9],
            actual_mass_kg=0.04,
        )
        self.assertFalse(report.overall_pass)
        self.assertEqual(report.failure_codes, [FailureCode.INTERFACE_DIM_MISMATCH])


class TestUnmeasuredBboxChecksFail(unittest.TestCase):
    """R2: envelope and skeleton checks with no bbox fail as unmeasured."""

    def test_no_bbox_fails_envelope_through_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = init_run("no-bbox", run_dir=Path(td) / "run")
            run.spec = _make_spec()
            out = Path(build_worker_prompts(run)[0]["output_dir"])
            _write_worker_output(out, claim_bbox=False)
            with patch(
                "orchestrator.measure.verify_worker_measurements",
                return_value=MeasurementVerification(
                    step_load_ok=True,
                    bbox_measured_mm=[],
                    interface_actuals_measured={"ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}},
                ),
            ):
                reports = validate_results(run, verify_measurements=True)
            ok, _ = check_gate_g5(run.spec, reports)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.bbox_source, "unknown")
        self.assertFalse(report.overall_pass)
        self.assertIsNotNone(report.envelope_check)
        self.assertFalse(report.envelope_check.passed)
        self.assertEqual(report.failure_codes, [FailureCode.VERIFICATION_FAILED])
        self.assertFalse(ok)

    def test_no_bbox_fails_skeleton_checks(self) -> None:
        spec = _make_spec()
        spec.skeleton = AssemblySkeleton(
            reserved_volumes={"gear": {"origin": [0, 0, 0], "size": [20, 20, 10]}},
            keepout_zones=[{"name": "motor", "origin": [50, 50, 0], "size": [5, 5, 5]}],
        )
        report = validate_worker_result(
            spec,
            WorkerResult(subsystem_name="gear", worker_id="gear_0"),
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_mass_kg=0.04,
        )
        self.assertFalse(report.overall_pass)
        self.assertEqual(
            {(sc.check, sc.passed, sc.measured) for sc in report.skeleton_checks},
            {("reserved_volume", False, False), ("keepout_zone", False, False)},
        )
        self.assertEqual(report.failure_codes, [FailureCode.VERIFICATION_FAILED])

    def test_bbox_outside_envelope_and_volume_keeps_violation_codes(self) -> None:
        spec = _make_spec()
        spec.skeleton = AssemblySkeleton(
            reserved_volumes={"gear": {"origin": [0, 0, 0], "size": [20, 20, 10]}},
        )
        report = validate_worker_result(
            spec,
            WorkerResult(subsystem_name="gear", worker_id="gear_0"),
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[25, 25, 15],
            actual_mass_kg=0.04,
        )
        self.assertFalse(report.overall_pass)
        self.assertEqual(
            report.failure_codes,
            [FailureCode.ENVELOPE_VIOLATION, FailureCode.SKELETON_CONFLICT],
        )


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


class TestSentinelFallbackMeasurement(unittest.TestCase):
    """Sentinel import bbox triggers a geometry fallback, never claims."""

    def _verify(self, import_result, *, topology=None, dimensions=None, material="steel"):
        import dataclasses

        from orchestrator.measure import verify_worker_measurements

        spec = _make_spec()
        sub = spec.get_subsystem("gear")
        if material != sub.material:
            sub = dataclasses.replace(sub, material=material)
        with (
            patch(
                "orchestrator.measure.measure_worker_step",
                return_value={"ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}},
            ),
            patch("server.tools_cad.cad_import_step", return_value=import_result) as imp,
            patch("server.tools_cad.cad_get_body_topology", return_value=topology or {}) as topo,
            patch(
                "server.tools_cad.cad_get_dimensions", return_value=dimensions or {}
            ) as dims,
        ):
            verification = verify_worker_measurements(
                step_path=Path("gear.step"),
                claimed={"ifc1": {"bore_dia": 8.0, "bore_depth": 15.0}},
                subsystem=sub,
                interfaces=list(spec.interfaces_for("gear")),
            )
        return verification, imp, topo, dims

    def test_sentinel_triggers_fallback_bbox(self) -> None:
        verification, _imp, topo, _dims = self._verify(
            {
                "doc": "step_import",
                "object": "VerifyBbox_gear",
                "volume_mm3": 5095.5,
                "bbox_mm": [1e100, 1e100, 1e100],
            },
            dimensions={"bounding_box": {"x_len": 19.0, "y_len": 18.0, "z_len": 9.0}},
        )
        self.assertTrue(verification.step_load_ok)
        self.assertEqual(verification.bbox_measured_mm, [19.0, 18.0, 9.0])
        topo.assert_called_once()

    def test_sane_import_bbox_needs_no_fallback(self) -> None:
        verification, _imp, topo, _dims = self._verify(
            {
                "doc": "step_import",
                "object": "VerifyBbox_gear",
                "volume_mm3": 5095.5,
                "bbox_mm": [19.0, 18.0, 9.0],
            },
        )
        self.assertEqual(verification.bbox_measured_mm, [19.0, 18.0, 9.0])
        topo.assert_not_called()

    def test_fallback_failure_leaves_bbox_unmeasured(self) -> None:
        with (
            patch(
                "orchestrator.measure.measure_worker_step",
                return_value={"ifc1": {"bore_dia": 8.0}},
            ),
            patch(
                "server.tools_cad.cad_import_step",
                return_value={
                    "doc": "step_import",
                    "object": "VerifyBbox_gear",
                    "volume_mm3": 5095.5,
                    "bbox_mm": [1e100, 1e100, 1e100],
                },
            ),
            patch(
                "server.tools_cad.cad_get_body_topology",
                side_effect=RuntimeError("socket down"),
            ),
        ):
            from orchestrator.measure import verify_worker_measurements

            spec = _make_spec()
            verification = verify_worker_measurements(
                step_path=Path("gear.step"),
                claimed={},
                subsystem=spec.get_subsystem("gear"),
                interfaces=list(spec.interfaces_for("gear")),
            )
        self.assertEqual(verification.bbox_measured_mm, [])
        # Volume is independent of the bbox sentinel, so mass still measures.
        self.assertAlmostEqual(verification.mass_measured_kg, 0.04, places=6)

    def test_mass_is_volume_times_density(self) -> None:
        verification, _imp, _topo, _dims = self._verify(
            {
                "doc": "step_import",
                "object": "VerifyBbox_gear",
                "volume_mm3": 5095.5,  # steel: 5095.5e-9 m³ × 7850 kg/m³ ≈ 0.04 kg
                "bbox_mm": [19.0, 18.0, 9.0],
            },
        )
        self.assertAlmostEqual(verification.mass_measured_kg, 0.04, places=6)

    def test_mass_unmeasured_for_unknown_material(self) -> None:
        verification, _imp, _topo, _dims = self._verify(
            {
                "doc": "step_import",
                "object": "VerifyBbox_gear",
                "volume_mm3": 5095.5,
                "bbox_mm": [19.0, 18.0, 9.0],
            },
            material="unobtainium",
        )
        self.assertIsNone(verification.mass_measured_kg)

    def test_mass_unmeasured_for_zero_volume(self) -> None:
        verification, _imp, _topo, _dims = self._verify(
            {
                "doc": "step_import",
                "object": "VerifyBbox_gear",
                "volume_mm3": 0.0,
                "bbox_mm": [19.0, 18.0, 9.0],
            },
        )
        self.assertIsNone(verification.mass_measured_kg)


class TestScorerUsesMeasuredValues(unittest.TestCase):
    """build_variants must not store claimed mass/bbox under measured."""

    def _report(self, *, bbox_source: str, mass_source: str):
        spec = _make_spec()
        return spec, validate_worker_result(
            spec,
            WorkerResult(subsystem_name="gear", worker_id="gear_0"),
            measurements={"ifc1": {"bore_dia": 8.005, "bore_depth": 15.0}},
            actual_bbox_mm=[19, 19, 9],
            actual_mass_kg=0.04,
            measurement_source="orchestrator",
            bbox_source=bbox_source,
            mass_source=mass_source,
        )

    def test_claimed_mass_and_bbox_excluded(self) -> None:
        spec, report = self._report(bbox_source="claimed", mass_source="claimed")
        variants = build_variants(spec, [report])
        variant = variants["gear"][0]
        self.assertNotIn("mass_kg", variant.measured)
        self.assertNotIn("mass", variant.scores)
        self.assertNotIn("volume_mm3", variant.measured)
        self.assertNotIn("volume_mm3", variant.scores)

    def test_measured_mass_and_bbox_included(self) -> None:
        spec, report = self._report(bbox_source="orchestrator", mass_source="orchestrator")
        variants = build_variants(spec, [report])
        variant = variants["gear"][0]
        self.assertEqual(variant.measured["mass_kg"], 0.04)
        self.assertEqual(variant.scores["mass"], 0.04)
        self.assertEqual(variant.measured["volume_mm3"], 19 * 19 * 9)
        self.assertEqual(variant.scores["volume_mm3"], 19 * 19 * 9)


if __name__ == "__main__":
    unittest.main()
