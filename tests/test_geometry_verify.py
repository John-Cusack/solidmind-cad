from __future__ import annotations

import unittest

from server.geometry_executor import ExecutionStep, ExecutionTrace
from server.geometry_ir import (
    GIR,
    CompiledOp,
    GIRBuilder,
    Quantity,
)
from server.geometry_verify import VerificationEngine


def _make_trace(ops_status: list[tuple[str, str]] | None = None) -> ExecutionTrace:
    """Create a simple ExecutionTrace for testing."""
    if ops_status is None:
        ops_status = [("pad", "completed")]

    steps = []
    for i, (op_type, status) in enumerate(ops_status):
        op = CompiledOp(id=f"OP{i}", op_type=op_type, inputs={})
        steps.append(ExecutionStep(op=op, status=status, output={"status": "success"} if status == "completed" else {"status": "error"}))

    return ExecutionTrace(steps=steps)


def _make_gir(holes: list[float] | None = None, fillets: list[float] | None = None) -> GIR:
    """Create a GIR with optional holes and fillets."""
    builder = GIRBuilder()
    builder.add_global_frame()
    builder.add_primitive("box", {
        "length": Quantity(100.0, "mm"),
        "width": Quantity(50.0, "mm"),
        "height": Quantity(20.0, "mm"),
    })

    if holes:
        from server.geometry_ir import Point3D
        for d in holes:
            builder.add_hole_intent(
                diameter=Quantity(d, "mm"),
                depth=Quantity(10.0, "mm"),
                hole_type="simple",
                location=Point3D(
                    x=Quantity(0.0, "mm"),
                    y=Quantity(0.0, "mm"),
                    z=Quantity(0.0, "mm"),
                ),
            )

    if fillets:
        for r in fillets:
            builder.add_blend_intent(
                blend_type="fillet",
                edge_references=["Edge1"],
                radius=Quantity(r, "mm"),
            )

    return builder.build()


class TestSolidValidity(unittest.TestCase):
    """Test solid validity verification check."""

    def test_all_completed_passes(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        solid_checks = [r for r in report.results if r.check_type == "solid_validity"]
        self.assertTrue(all(r.passed for r in solid_checks))

    def test_failed_step_fails_validity(self) -> None:
        trace = _make_trace([("pad", "failed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        solid_checks = [r for r in report.results if r.check_type == "solid_validity"]
        self.assertTrue(any(not r.passed for r in solid_checks))


class TestPositiveVolume(unittest.TestCase):
    """Test positive volume check."""

    def test_pad_completed_passes(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        vol_checks = [r for r in report.results if r.check_type == "positive_volume"]
        self.assertTrue(all(r.passed for r in vol_checks))

    def test_no_solid_creating_op_fails(self) -> None:
        trace = _make_trace([("fillet", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        vol_checks = [r for r in report.results if r.check_type == "positive_volume"]
        self.assertTrue(any(not r.passed for r in vol_checks))


class TestWallThickness(unittest.TestCase):
    """Test wall thickness check."""

    def test_thick_wall_passes(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
        }
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, spec)

        wt_checks = [r for r in report.results if r.check_type == "wall_thickness"]
        self.assertTrue(all(r.passed for r in wt_checks))

    def test_thin_wall_fails(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 0.5, "unit": "mm"},  # Below threshold
            },
        }
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, spec)

        wt_checks = [r for r in report.results if r.check_type == "wall_thickness"]
        failed = [r for r in wt_checks if not r.passed]
        self.assertTrue(len(failed) > 0)
        self.assertEqual(failed[0].measured_value, 0.5)


class TestHoleDiameter(unittest.TestCase):
    """Test hole diameter check."""

    def test_large_holes_pass(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir(holes=[5.0, 8.0])
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        hd_checks = [r for r in report.results if r.check_type == "hole_diameter"]
        self.assertTrue(all(r.passed for r in hd_checks))

    def test_small_hole_fails(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir(holes=[1.0])  # Below 3mm default threshold
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        hd_checks = [r for r in report.results if r.check_type == "hole_diameter"]
        failed = [r for r in hd_checks if not r.passed]
        self.assertTrue(len(failed) > 0)


class TestFeatureCount(unittest.TestCase):
    """Test feature count check."""

    def test_features_executed(self) -> None:
        trace = _make_trace([("pad", "completed"), ("hole", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        fc_checks = [r for r in report.results if r.check_type == "feature_count"]
        self.assertTrue(all(r.passed for r in fc_checks))


class TestReportHash(unittest.TestCase):
    """Test report hash stability."""

    def test_same_inputs_same_hash(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()

        report1 = engine.verify(trace, gir, {})
        report2 = engine.verify(trace, gir, {})

        self.assertEqual(report1.report_hash, report2.report_hash)

    def test_report_hash_not_empty(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, {})

        self.assertTrue(len(report.report_hash) > 0)


class TestNotifyOnlyBehavior(unittest.TestCase):
    """Test that default mode is notify-only (no blocking)."""

    def test_warnings_dont_block(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 0.5, "unit": "mm"},
            },
        }
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine(strict=False)
        report = engine.verify(trace, gir, spec)

        # Even with thin wall warning, report should pass in notify-only mode
        self.assertTrue(report.passed)

    def test_strict_mode_blocks_on_error(self) -> None:
        trace = _make_trace([("pad", "failed")])
        gir = _make_gir()
        engine = VerificationEngine(strict=True)
        report = engine.verify(trace, gir, {})

        # Failed solid validity (error severity) should block in strict mode
        self.assertFalse(report.passed)


class TestInternalRadius(unittest.TestCase):
    """Test internal radius verification."""

    # Use cnc_aluminum spec to get internal_radius check from policy
    _ALUMINUM_SPEC: dict = {
        "process": "cnc",
        "material": {"family": "aluminum"},
    }

    def test_large_fillet_passes(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir(fillets=[3.0])
        engine = VerificationEngine()
        report = engine.verify(trace, gir, self._ALUMINUM_SPEC)

        ir_checks = [r for r in report.results if r.check_type == "internal_radius"]
        self.assertTrue(all(r.passed for r in ir_checks))

    def test_small_fillet_fails(self) -> None:
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir(fillets=[0.3])  # Below 1mm threshold
        engine = VerificationEngine()
        report = engine.verify(trace, gir, self._ALUMINUM_SPEC)

        ir_checks = [r for r in report.results if r.check_type == "internal_radius"]
        failed = [r for r in ir_checks if not r.passed]
        self.assertTrue(len(failed) > 0)


class TestCncDepthRatioChecks(unittest.TestCase):
    """Test CNC-specific depth ratio checks."""

    def test_pocket_depth_ratio_fails_above_threshold(self) -> None:
        spec = {
            "process": "cnc",
            "geometry": {
                "pocket_features": [
                    {
                        "depth": {"value": 25, "unit": "mm"},
                        "width": {"value": 5, "unit": "mm"},
                    }
                ]
            },
        }
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, spec)

        checks = [r for r in report.results if r.check_type == "pocket_depth_ratio"]
        self.assertTrue(checks)
        self.assertTrue(any(not r.passed for r in checks))

    def test_hole_depth_ratio_fails_above_threshold(self) -> None:
        spec = {"process": "cnc"}
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir(holes=[0.5])  # depth=10 -> ratio=20
        engine = VerificationEngine()
        report = engine.verify(trace, gir, spec)

        checks = [r for r in report.results if r.check_type == "hole_depth_ratio"]
        self.assertTrue(checks)
        self.assertTrue(any(not r.passed for r in checks))


class TestFdmOverhangBridgeChecks(unittest.TestCase):
    """Test FDM overhang and bridge checks."""

    _PLA_SPEC_BASE: dict = {
        "process": "print_3d",
        "material": {"family": "pla"},
    }

    def test_overhang_angle_fails_above_threshold(self) -> None:
        spec = dict(self._PLA_SPEC_BASE)
        spec["planning"] = {"max_overhang_angle_deg": 60}
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, spec)

        checks = [r for r in report.results if r.check_type == "overhang_angle"]
        self.assertTrue(checks)
        self.assertTrue(any(not r.passed for r in checks))

    def test_bridge_span_fails_above_threshold(self) -> None:
        spec = dict(self._PLA_SPEC_BASE)
        spec["planning"] = {"max_bridge_span_mm": 15}
        trace = _make_trace([("pad", "completed")])
        gir = _make_gir()
        engine = VerificationEngine()
        report = engine.verify(trace, gir, spec)

        checks = [r for r in report.results if r.check_type == "bridge_span"]
        self.assertTrue(checks)
        self.assertTrue(any(not r.passed for r in checks))


if __name__ == "__main__":
    unittest.main()
