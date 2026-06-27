from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.me_orchestrator import (
    VALIDATORS,
    _check_centrifugal_stress,
    _check_manufacturability,
    _check_mass_properties,
    _check_min_thickness,
    _check_sharp_edge,
    _check_symmetry,
    _find_relevant_notes,
    apply_risk_gates,
    build_traceability_matrix,
    list_validators,
    run_design_loop,
    validate_constraint_sheet,
)


class TestMinThicknessCheck(unittest.TestCase):
    def test_passes_when_above_floor(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 2.0},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        blocker_ids = [b["rule_id"] for b in report["blockers"]]
        self.assertNotIn("me.min_thickness_check", blocker_ids)

    def test_blocks_when_below_floor(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 0.6},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        blocker_ids = [b["rule_id"] for b in report["blockers"]]
        self.assertIn("me.min_thickness_check", blocker_ids)

    def test_warns_when_close_to_floor(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 1.05},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        warn_ids = [w["rule_id"] for w in report["warnings"]]
        self.assertIn("me.min_thickness_check", warn_ids)

    def test_accepts_blade_thickness_alias(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"min_blade_thickness_mm": 2.0},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        blocker_ids = [b["rule_id"] for b in report["blockers"]]
        self.assertNotIn("me.min_thickness_check", blocker_ids)


class TestSymmetryCheck(unittest.TestCase):
    def test_passes_propeller_blade_count(self) -> None:
        """2 blades should pass for a propeller (min_blade_count=2)."""
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"blade_count": 2},
                "balance_rotor": {"symmetry_required": True},
                "thresholds": {"min_blade_count": 2},
            }
        )
        blocker_ids = [b["rule_id"] for b in report["blockers"]]
        warn_ids = [w["rule_id"] for w in report["warnings"]]
        self.assertNotIn("me.symmetry_check", blocker_ids)
        self.assertNotIn("me.symmetry_check", warn_ids)

    def test_warns_below_threshold(self) -> None:
        """Blade count below the LLM-specified threshold should warn."""
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"blade_count": 6},
                "balance_rotor": {"symmetry_required": True},
                "thresholds": {"min_blade_count": 8},
            }
        )
        warn_ids = [w["rule_id"] for w in report["warnings"]]
        self.assertIn("me.symmetry_check", warn_ids)

    def test_default_min_blade_count_is_2(self) -> None:
        """Without thresholds, default min is 2 (any rotating part)."""
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"blade_count": 2},
                "balance_rotor": {"symmetry_required": True},
            }
        )
        warn_ids = [w["rule_id"] for w in report["warnings"]]
        self.assertNotIn("me.symmetry_check", warn_ids)


class TestCentrifugalStressProxy(unittest.TestCase):
    def test_custom_safety_factor_thresholds(self) -> None:
        """LLM can set min_safety_factor and preferred_safety_factor."""
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"outer_diameter_mm": 254},
                "operating_envelope": {"max_rpm": 12000},
                "material": {"density_kg_m3": 1400, "yield_strength_at_temp_mpa": 80},
                "thresholds": {"min_safety_factor": 3.0, "preferred_safety_factor": 5.0},
            }
        )
        # Safety factor ~6.73, above preferred 5.0 → should pass
        results = {r["validator"]: r for r in report["results"]}
        self.assertEqual(results["centrifugal_stress_proxy"]["status"], "pass")

    def test_accepts_exducer_diameter_alias(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"exducer_diameter_mm": 60},
                "operating_envelope": {"max_rpm": 100000},
                "material": {"density_kg_m3": 8000, "yield_strength_at_temp_mpa": 500},
            }
        )
        results = {r["validator"]: r for r in report["results"]}
        self.assertIn("centrifugal_stress_proxy", results)
        self.assertIsNotNone(results["centrifugal_stress_proxy"]["measured"]["sigma_mpa"])


class TestMassProperties(unittest.TestCase):
    def test_warns_when_exceeds_max_mass(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {
                    "outer_diameter_mm": 254,
                    "hub_diameter_mm": 20,
                    "min_wall_thickness_mm": 3.0,
                },
                "material": {"density_kg_m3": 8000},
                "thresholds": {"max_mass_kg": 0.1},
            }
        )
        warn_ids = [w["rule_id"] for w in report["warnings"]]
        self.assertIn("me.mass_properties", warn_ids)

    def test_passes_without_max_mass(self) -> None:
        """Without max_mass_kg threshold, any positive mass passes."""
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {
                    "outer_diameter_mm": 254,
                    "hub_diameter_mm": 20,
                    "min_wall_thickness_mm": 1.5,
                },
                "material": {"density_kg_m3": 1400},
            }
        )
        blocker_ids = [b["rule_id"] for b in report["blockers"]]
        self.assertNotIn("me.mass_properties", blocker_ids)


class TestManufacturabilityHeuristics(unittest.TestCase):
    def test_fdm_passes(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 2.0},
                "manufacturing": {"process": "fdm", "min_feature_size_mm": 0.8},
            }
        )
        results = {r["validator"]: r for r in report["results"]}
        self.assertEqual(results["manufacturability_heuristics"]["status"], "pass")

    def test_fdm_warns_thin_wall(self) -> None:
        report = validate_constraint_sheet(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 0.3},
                "manufacturing": {
                    "process": "fdm",
                    "min_feature_size_mm": 0.8,
                    "min_wall_thickness_mm": 0.8,
                },
            }
        )
        warn_ids = [w["rule_id"] for w in report["warnings"]]
        self.assertIn("me.manufacturability_heuristics", warn_ids)

    def test_unknown_process_passes(self) -> None:
        """Unknown processes should pass (no heuristics, not a warning)."""
        report = validate_constraint_sheet(
            {
                "manufacturing": {"process": "laser_sintering"},
            }
        )
        results = {r["validator"]: r for r in report["results"]}
        self.assertEqual(results["manufacturability_heuristics"]["status"], "pass")


class TestTraceabilityMatrix(unittest.TestCase):
    def test_traces_requirement_to_validator(self) -> None:
        sheet = {
            "requirements": [
                {
                    "requirement_id": "REQ-001",
                    "statement": "Wall thickness above process min",
                    "validator": "min_thickness_check",
                },
            ],
            "geometry_interfaces": {"min_wall_thickness_mm": 2.0},
            "manufacturing": {"min_feature_size_mm": 1.0},
        }
        report = validate_constraint_sheet(sheet)
        trace = build_traceability_matrix(sheet, report)
        self.assertTrue(trace["ok"])
        self.assertEqual(len(trace["traceability_matrix"]), 1)
        self.assertEqual(trace["traceability_matrix"][0]["status"], "pass")


class TestRiskGates(unittest.TestCase):
    def test_high_rpm_high_temp(self) -> None:
        sheet = {"operating_envelope": {"max_rpm": 180000, "gas_temp_max_c": 1050}}
        report = validate_constraint_sheet(sheet)
        gates = apply_risk_gates(sheet, report)
        self.assertTrue(gates["ok"])
        self.assertIn(gates["risk_class"], {"high", "critical"})
        self.assertTrue(gates["requires_signoff"])

    def test_low_risk(self) -> None:
        sheet = {"operating_envelope": {"max_rpm": 5000}}
        report = validate_constraint_sheet(sheet)
        gates = apply_risk_gates(sheet, report)
        self.assertEqual(gates["risk_class"], "low")


class TestMEDesignLoop(unittest.TestCase):
    def test_design_loop_with_constraints(self) -> None:
        constraints = {
            "geometry_interfaces": {"min_wall_thickness_mm": 2.0, "outer_diameter_mm": 60},
            "manufacturing": {"min_feature_size_mm": 1.0},
            "operating_envelope": {"max_rpm": 100000},
        }
        out = run_design_loop(constraints)
        self.assertTrue(out["ok"])
        self.assertIn("validation", out)
        self.assertIn("traceability", out)
        self.assertIn("risk_gates", out)
        self.assertIn("notes_available", out)
        self.assertIsInstance(out["notes_available"], list)
        self.assertIn("summary", out)

    def test_design_loop_empty_constraints(self) -> None:
        out = run_design_loop({})
        self.assertTrue(out["ok"])


class TestFindRelevantNotes(unittest.TestCase):
    def test_returns_empty_when_dir_missing(self) -> None:
        with patch("server.me_orchestrator.Path") as mock_path:
            mock_notes_dir = mock_path.return_value.resolve.return_value.parent.parent.__truediv__.return_value.__truediv__.return_value
            mock_notes_dir.is_dir.return_value = False
            result = _find_relevant_notes()
            self.assertEqual(result, [])

    def test_returns_md_files_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notes_dir = Path(tmpdir) / "me_knowledge" / "notes"
            notes_dir.mkdir(parents=True)
            (notes_dir / "turbine_blades.md").write_text("# Turbine Blades\n")
            (notes_dir / "gear_geometry.md").write_text("# Gear Geometry\n")
            (notes_dir / ".gitkeep").write_text("")

            with patch("server.me_orchestrator.Path") as mock_path:
                mock_path.return_value.resolve.return_value.parent.parent = Path(tmpdir)
                result = _find_relevant_notes()
                self.assertEqual(result, ["gear_geometry.md", "turbine_blades.md"])


class TestDirectValidatorFunctions(unittest.TestCase):
    """Test individual validator functions independently."""

    def test_min_thickness_pass(self) -> None:
        vr = _check_min_thickness(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 2.0},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        self.assertEqual(vr.status, "pass")
        self.assertEqual(vr.name, "min_thickness_check")
        self.assertEqual(vr.priority, 910)

    def test_min_thickness_fail(self) -> None:
        vr = _check_min_thickness(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 0.5},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        self.assertEqual(vr.status, "fail")

    def test_sharp_edge_pass(self) -> None:
        vr = _check_sharp_edge(
            {
                "geometry_interfaces": {"min_fillet_radius_mm": 2.0},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        self.assertEqual(vr.status, "pass")
        self.assertEqual(vr.name, "sharp_edge_check")

    def test_sharp_edge_fail(self) -> None:
        vr = _check_sharp_edge(
            {
                "geometry_interfaces": {"min_fillet_radius_mm": 0.1},
                "manufacturing": {"min_feature_size_mm": 1.0},
            }
        )
        self.assertEqual(vr.status, "fail")

    def test_symmetry_pass(self) -> None:
        vr = _check_symmetry(
            {
                "geometry_interfaces": {"blade_count": 4},
                "balance_rotor": {"symmetry_required": True},
            }
        )
        self.assertEqual(vr.status, "pass")

    def test_symmetry_warn_no_flag(self) -> None:
        vr = _check_symmetry({})
        self.assertEqual(vr.status, "warn")

    def test_centrifugal_stress_pass(self) -> None:
        vr = _check_centrifugal_stress(
            {
                "geometry_interfaces": {"outer_diameter_mm": 254},
                "operating_envelope": {"max_rpm": 12000},
                "material": {"density_kg_m3": 1400, "yield_strength_at_temp_mpa": 80},
            }
        )
        self.assertEqual(vr.status, "pass")
        self.assertEqual(vr.name, "centrifugal_stress_proxy")

    def test_centrifugal_stress_missing_data(self) -> None:
        vr = _check_centrifugal_stress({})
        self.assertEqual(vr.status, "warn")

    def test_mass_properties_pass(self) -> None:
        vr = _check_mass_properties(
            {
                "geometry_interfaces": {
                    "outer_diameter_mm": 100,
                    "hub_diameter_mm": 20,
                    "min_wall_thickness_mm": 2.0,
                },
                "material": {"density_kg_m3": 2700},
            }
        )
        self.assertEqual(vr.status, "pass")
        self.assertIn("mass_kg", vr.measured)

    def test_manufacturability_fdm_pass(self) -> None:
        vr = _check_manufacturability(
            {
                "geometry_interfaces": {"min_wall_thickness_mm": 2.0},
                "manufacturing": {"process": "fdm"},
            }
        )
        self.assertEqual(vr.status, "pass")

    def test_manufacturability_unknown_process(self) -> None:
        vr = _check_manufacturability({"manufacturing": {"process": "laser_sintering"}})
        self.assertEqual(vr.status, "pass")


class TestListValidators(unittest.TestCase):
    def test_returns_all_validators(self) -> None:
        result = list_validators()
        self.assertEqual(len(result), len(VALIDATORS))
        names = {v["name"] for v in result}
        self.assertEqual(names, set(VALIDATORS.keys()))

    def test_metadata_fields(self) -> None:
        result = list_validators()
        for entry in result:
            self.assertIn("name", entry)
            self.assertIn("description", entry)
            self.assertIn("reads", entry)
            self.assertIn("thresholds", entry)
            self.assertIn("priority", entry)
            self.assertIsInstance(entry["reads"], list)
            self.assertIsInstance(entry["priority"], int)


if __name__ == "__main__":
    unittest.main()
