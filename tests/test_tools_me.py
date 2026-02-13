from __future__ import annotations

import unittest

from server.tools_me import (
    me_apply_risk_gates,
    me_build_traceability,
    me_design_loop,
    me_list_validators,
    me_validate_constraints,
)


class TestMETools(unittest.TestCase):
    def test_validate_constraints(self) -> None:
        sheet = {
            "geometry_interfaces": {"min_blade_thickness_mm": 2.0},
            "manufacturing": {"min_feature_size_mm": 1.0},
        }
        out = me_validate_constraints(sheet)
        self.assertTrue(out["ok"])
        self.assertGreater(len(out["validators_run"]), 0)

    def test_validate_constraints_invalid_input(self) -> None:
        out = me_validate_constraints("not a dict")  # type: ignore[arg-type]
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")

    def test_build_traceability(self) -> None:
        sheet = {
            "requirements": [
                {
                    "requirement_id": "REQ-001",
                    "statement": "test",
                    "validator": "min_thickness_check",
                },
            ],
            "geometry_interfaces": {"min_blade_thickness_mm": 2.0},
            "manufacturing": {"min_feature_size_mm": 1.0},
        }
        report = me_validate_constraints(sheet)
        trace = me_build_traceability(sheet, report)
        self.assertTrue(trace["ok"])

    def test_apply_risk_gates(self) -> None:
        sheet = {"operating_envelope": {"max_rpm": 180000, "gas_temp_max_c": 1050}}
        report = me_validate_constraints(sheet)
        gates = me_apply_risk_gates(sheet, report)
        self.assertTrue(gates["ok"])
        self.assertIn(gates["risk_class"], {"high", "critical"})

    def test_design_loop(self) -> None:
        constraints = {
            "geometry_interfaces": {"min_blade_thickness_mm": 2.0},
            "manufacturing": {"min_feature_size_mm": 1.0},
            "operating_envelope": {"max_rpm": 100000},
        }
        out = me_design_loop(constraints)
        self.assertTrue(out["ok"])
        self.assertIn("summary", out)
        self.assertIn("validation", out)
        self.assertIn("risk_gates", out)

    def test_design_loop_invalid_input(self) -> None:
        out = me_design_loop("not a dict")  # type: ignore[arg-type]
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")

    def test_list_validators(self) -> None:
        out = me_list_validators()
        self.assertTrue(out["ok"])
        self.assertIsInstance(out["validators"], list)
        self.assertGreater(len(out["validators"]), 0)
        names = {v["name"] for v in out["validators"]}
        self.assertIn("min_thickness_check", names)
        self.assertIn("centrifugal_stress_proxy", names)


if __name__ == "__main__":
    unittest.main()
