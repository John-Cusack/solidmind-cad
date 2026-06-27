"""Tests for orchestrator.interface_freeze."""

from __future__ import annotations

import unittest

from orchestrator.interface_freeze import (
    freeze_interfaces,
    generate_icd_summary,
    is_interface_complete_extended,
    lock_purchased_parts,
    validate_purchased_lock,
)
from orchestrator.spec import (
    CoordinateFrame,
    Interface,
    MasterSpec,
    MatingSemantic,
    Subsystem,
    SubsystemKind,
    ValidationCheckPoint,
    ValidationMethod,
)


def _complete_interface(**overrides) -> Interface:
    """Build a baseline-complete interface with optional overrides."""
    defaults = dict(
        id="ifc1",
        name="shaft_bore",
        subsystem_a="gear",
        subsystem_b="shaft",
        geometry={"diameter_mm": 8.0},
        frame_a=CoordinateFrame(origin_mm=[0, 0, 5]),
        frame_b=CoordinateFrame(origin_mm=[0, 0, 0]),
        mating=MatingSemantic(type="cylindrical_fit"),
        validation=ValidationMethod(
            check_points=[
                ValidationCheckPoint(feature="bore_dia", expected_mm=8.0, tolerance_mm=0.01),
            ]
        ),
    )
    defaults.update(overrides)
    return Interface(**defaults)


class TestIsInterfaceCompleteExtended(unittest.TestCase):
    def test_complete_cylindrical_fit(self) -> None:
        ifc = _complete_interface(runout_or_concentricity=0.01)
        ok, issues = is_interface_complete_extended(ifc)
        self.assertTrue(ok, issues)

    def test_cylindrical_fit_missing_runout(self) -> None:
        ifc = _complete_interface()  # no runout_or_concentricity
        ok, issues = is_interface_complete_extended(ifc)
        self.assertFalse(ok)
        self.assertTrue(any("runout" in i for i in issues))

    def test_gear_mesh_missing_backlash(self) -> None:
        ifc = _complete_interface(
            mating=MatingSemantic(type="gear_mesh"),
        )
        ok, issues = is_interface_complete_extended(ifc)
        self.assertFalse(ok)
        self.assertTrue(any("backlash" in i for i in issues))

    def test_gear_mesh_with_backlash(self) -> None:
        ifc = _complete_interface(
            mating=MatingSemantic(type="gear_mesh"),
            backlash={"min_mm": 0.05, "max_mm": 0.10},
        )
        ok, issues = is_interface_complete_extended(ifc)
        self.assertTrue(ok, issues)

    def test_bolt_pattern_missing_preload(self) -> None:
        ifc = _complete_interface(
            mating=MatingSemantic(type="bolt_pattern"),
        )
        ok, issues = is_interface_complete_extended(ifc)
        self.assertFalse(ok)
        self.assertTrue(any("preload" in i for i in issues))

    def test_bolt_pattern_with_preload(self) -> None:
        ifc = _complete_interface(
            mating=MatingSemantic(type="bolt_pattern"),
            preload={"torque_nm": 5.0, "method": "torque_wrench"},
        )
        ok, issues = is_interface_complete_extended(ifc)
        self.assertTrue(ok, issues)

    def test_baseline_incomplete_fails(self) -> None:
        ifc = Interface(name="bare", mating=MatingSemantic(type="planar_contact"))
        ok, issues = is_interface_complete_extended(ifc)
        self.assertFalse(ok)
        self.assertTrue(any("baseline" in i for i in issues))


class TestValidatePurchasedLock(unittest.TestCase):
    def test_valid_catalog(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(
            Subsystem(
                name="bearing",
                kind=SubsystemKind.CATALOG,
                supplier_part="SKF 6201",
                quantity=2,
            )
        )
        ok, issues = validate_purchased_lock(spec)
        self.assertTrue(ok, issues)

    def test_catalog_missing_part(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(
            Subsystem(
                name="bearing",
                kind=SubsystemKind.CATALOG,
            )
        )
        ok, issues = validate_purchased_lock(spec)
        self.assertFalse(ok)
        self.assertTrue(any("supplier_part" in i for i in issues))

    def test_standard_missing_standard(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(
            Subsystem(
                name="bolt",
                kind=SubsystemKind.STANDARD,
            )
        )
        ok, issues = validate_purchased_lock(spec)
        self.assertFalse(ok)
        self.assertTrue(any("standard" in i.lower() for i in issues))

    def test_zero_quantity_fails(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(
            Subsystem(
                name="bolt",
                kind=SubsystemKind.STANDARD,
                standard="ISO 4762 M5x20",
                quantity=0,
            )
        )
        ok, issues = validate_purchased_lock(spec)
        self.assertFalse(ok)
        self.assertTrue(any("quantity" in i for i in issues))


class TestFreezeInterfaces(unittest.TestCase):
    def test_all_complete(self) -> None:
        spec = MasterSpec(name="test")
        spec.interfaces.append(
            _complete_interface(
                mating=MatingSemantic(type="planar_contact"),
            )
        )
        ok, issues = freeze_interfaces(spec)
        self.assertTrue(ok, issues)

    def test_incomplete_interface_fails(self) -> None:
        spec = MasterSpec(name="test")
        spec.interfaces.append(Interface(name="bare"))
        ok, issues = freeze_interfaces(spec)
        self.assertFalse(ok)


class TestLockPurchasedParts(unittest.TestCase):
    def test_identifies_locked(self) -> None:
        spec = MasterSpec(name="test")
        spec.subsystems.append(
            Subsystem(
                name="bearing",
                kind=SubsystemKind.CATALOG,
            )
        )
        spec.subsystems.append(
            Subsystem(
                name="gear",
                kind=SubsystemKind.GENERATED,
            )
        )
        spec.subsystems.append(
            Subsystem(
                name="bolt",
                kind=SubsystemKind.STANDARD,
            )
        )
        locked = lock_purchased_parts(spec)
        self.assertEqual(sorted(locked), ["bearing", "bolt"])


class TestGenerateIcdSummary(unittest.TestCase):
    def test_summary_structure(self) -> None:
        spec = MasterSpec(name="test")
        spec.interfaces.append(
            _complete_interface(
                mating=MatingSemantic(type="planar_contact"),
            )
        )
        spec.interfaces.append(Interface(name="bare"))
        summary = generate_icd_summary(spec)
        self.assertEqual(summary["total_interfaces"], 2)
        self.assertEqual(summary["complete_count"], 1)
        self.assertFalse(summary["all_complete"])
        self.assertEqual(len(summary["interfaces"]), 2)


if __name__ == "__main__":
    unittest.main()
