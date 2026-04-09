"""Tests for orchestrator.council."""
from __future__ import annotations

import unittest

from orchestrator.council import (
    apply_complexity_defaults,
    check_feasibility,
    classify_subsystem,
    validate_decomposition,
    validate_sizing,
)
from orchestrator.spec import (
    ComplexityClass,
    Interface,
    MasterSpec,
    Subsystem,
    SubsystemKind,
)


class TestValidateDecomposition(unittest.TestCase):
    def _make_spec(self) -> MasterSpec:
        spec = MasterSpec(name="test")
        spec.interfaces.append(Interface(id="ifc1", name="shaft_bore"))
        spec.subsystems.append(Subsystem(
            name="gear",
            kind=SubsystemKind.GENERATED,
            envelope_mm=[10, 10, 5],
            material="steel",
            interfaces=["ifc1"],
        ))
        return spec

    def test_valid_passes(self) -> None:
        ok, issues = validate_decomposition(self._make_spec())
        self.assertTrue(ok, issues)

    def test_missing_name_fails(self) -> None:
        spec = self._make_spec()
        spec.subsystems.append(Subsystem(name="", kind=SubsystemKind.GENERATED))
        ok, issues = validate_decomposition(spec)
        self.assertFalse(ok)
        self.assertTrue(any("empty name" in i for i in issues))

    def test_duplicate_name_fails(self) -> None:
        spec = self._make_spec()
        spec.subsystems.append(Subsystem(
            name="gear", kind=SubsystemKind.GENERATED,
            envelope_mm=[5, 5, 5], material="steel",
        ))
        ok, issues = validate_decomposition(spec)
        self.assertFalse(ok)
        self.assertTrue(any("Duplicate" in i for i in issues))

    def test_missing_envelope_fails(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(Subsystem(
            name="gear", kind=SubsystemKind.GENERATED, material="steel",
        ))
        ok, issues = validate_decomposition(spec)
        self.assertFalse(ok)
        self.assertTrue(any("envelope" in i for i in issues))

    def test_missing_material_fails(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(Subsystem(
            name="gear", kind=SubsystemKind.GENERATED, envelope_mm=[10, 10, 5],
        ))
        ok, issues = validate_decomposition(spec)
        self.assertFalse(ok)
        self.assertTrue(any("material" in i for i in issues))

    def test_dangling_interface_ref_fails(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(Subsystem(
            name="gear", kind=SubsystemKind.GENERATED,
            envelope_mm=[10, 10, 5], material="steel",
            interfaces=["nonexistent"],
        ))
        ok, issues = validate_decomposition(spec)
        self.assertFalse(ok)
        self.assertTrue(any("unknown interface" in i for i in issues))

    def test_no_subsystems_fails(self) -> None:
        spec = MasterSpec(name="test")
        ok, issues = validate_decomposition(spec)
        self.assertFalse(ok)

    def test_catalog_skips_envelope_check(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(Subsystem(
            name="bearing", kind=SubsystemKind.CATALOG,
            supplier_part="SKF 6201",
        ))
        ok, issues = validate_decomposition(spec)
        self.assertTrue(ok, issues)


class TestValidateSizing(unittest.TestCase):
    def test_mass_over_budget(self) -> None:
        spec = MasterSpec(
            name="test",
            global_constraints={"max_mass_kg": 0.5},
        )
        spec.subsystems.append(Subsystem(
            name="a", kind=SubsystemKind.GENERATED, mass_budget_kg=0.4,
        ))
        spec.subsystems.append(Subsystem(
            name="b", kind=SubsystemKind.GENERATED, mass_budget_kg=0.3,
        ))
        ok, issues = validate_sizing(spec)
        self.assertFalse(ok)
        self.assertTrue(any("budget" in i.lower() for i in issues))

    def test_missing_mass_budget(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(Subsystem(
            name="gear", kind=SubsystemKind.GENERATED,
        ))
        ok, issues = validate_sizing(spec)
        self.assertFalse(ok)
        self.assertTrue(any("mass_budget" in i for i in issues))


class TestCheckFeasibility(unittest.TestCase):
    def test_combines_checks(self) -> None:
        spec = MasterSpec(name="test")
        ok, issues = check_feasibility(spec)
        self.assertFalse(ok)
        self.assertGreater(len(issues), 0)


class TestClassifySubsystem(unittest.TestCase):
    def test_catalog(self) -> None:
        sub = Subsystem(name="brg", supplier_part="SKF 6201")
        self.assertEqual(classify_subsystem(sub), SubsystemKind.CATALOG)

    def test_standard(self) -> None:
        sub = Subsystem(name="bolt", standard="ISO 4762 M5x20")
        self.assertEqual(classify_subsystem(sub), SubsystemKind.STANDARD)

    def test_generated(self) -> None:
        sub = Subsystem(name="gear")
        self.assertEqual(classify_subsystem(sub), SubsystemKind.GENERATED)


class TestApplyComplexityDefaults(unittest.TestCase):
    def test_sets_policy(self) -> None:
        sub = Subsystem(name="gear", complexity_class=ComplexityClass.S)
        self.assertIsNone(sub.runtime_policy)
        apply_complexity_defaults(sub)
        self.assertIsNotNone(sub.runtime_policy)
        self.assertEqual(sub.runtime_policy.timeout_sec, 300)

    def test_does_not_overwrite(self) -> None:
        from orchestrator.spec import RuntimePolicy
        sub = Subsystem(
            name="gear",
            runtime_policy=RuntimePolicy(timeout_sec=999),
        )
        apply_complexity_defaults(sub)
        self.assertEqual(sub.runtime_policy.timeout_sec, 999)


if __name__ == "__main__":
    unittest.main()
