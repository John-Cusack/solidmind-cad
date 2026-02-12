from __future__ import annotations

import unittest

from server.me_orchestrator import (
    apply_risk_gates,
    build_traceability_matrix,
    instantiate_constraint_sheet,
    route_request,
    run_design_loop,
    validate_constraint_sheet,
)


class TestMERouting(unittest.TestCase):
    def test_route_turbocharger_wheel(self) -> None:
        out = route_request("Build me a turbocharger turbine wheel for high rpm use")
        self.assertTrue(out["ok"])
        self.assertEqual(out["archetype_id"], "turbine_wheel.turbocharger.radial.v1")
        self.assertGreater(out["confidence"], 0.0)


class TestMEConstraintAndValidation(unittest.TestCase):
    def test_instantiate_and_validate_default_template(self) -> None:
        inst = instantiate_constraint_sheet("turbine_wheel.turbocharger.radial.v1")
        self.assertTrue(inst["ok"])
        self.assertIn("constraint_sheet", inst)
        self.assertGreater(len(inst["tbd_fields"]), 0)

        report = validate_constraint_sheet(inst["constraint_sheet"])
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["blockers"]), 0)

    def test_thickness_blocker_triggers(self) -> None:
        inst = instantiate_constraint_sheet(
            "turbine_wheel.turbocharger.radial.v1",
            overrides={
                "geometry_interfaces": {"min_blade_thickness_mm": 0.6},
                "manufacturing": {"min_feature_size_mm": 1.0},
            },
        )
        report = validate_constraint_sheet(inst["constraint_sheet"])
        blocker_ids = [b["rule_id"] for b in report["blockers"]]
        self.assertIn("me.min_thickness_check", blocker_ids)

    def test_traceability_matrix(self) -> None:
        inst = instantiate_constraint_sheet("turbine_wheel.turbocharger.radial.v1")
        report = validate_constraint_sheet(inst["constraint_sheet"])
        trace = build_traceability_matrix(inst["constraint_sheet"], report)
        self.assertTrue(trace["ok"])
        self.assertGreater(len(trace["traceability_matrix"]), 0)
        self.assertIn("pass", trace["status_counts"])

    def test_risk_gate_classification(self) -> None:
        inst = instantiate_constraint_sheet(
            "turbine_wheel.turbocharger.radial.v1",
            overrides={"operating_envelope": {"max_rpm": 180000, "gas_temp_max_c": 1050}},
        )
        report = validate_constraint_sheet(inst["constraint_sheet"])
        gates = apply_risk_gates(inst["constraint_sheet"], report)
        self.assertTrue(gates["ok"])
        self.assertIn(gates["risk_class"], {"high", "critical"})
        self.assertTrue(gates["requires_signoff"])


class TestMEDesignLoop(unittest.TestCase):
    def test_full_design_loop(self) -> None:
        out = run_design_loop("Please design a turbocharger turbine wheel")
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"]["archetype_id"], "turbine_wheel.turbocharger.radial.v1")
        self.assertIn("validation", out)
        self.assertIn("traceability", out)
        self.assertIn("risk_gates", out)


if __name__ == "__main__":
    unittest.main()
