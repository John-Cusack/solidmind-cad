"""Tests for orchestrator.release."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.release import (
    ReleasePackage,
    build_release_package,
    check_gate_g7,
    generate_bom,
    generate_decision_report,
    generate_icd_set,
    generate_provenance_manifest,
    generate_purchased_parts_list,
)
from orchestrator.spec import (
    Interface,
    MasterSpec,
    MatingSemantic,
    Objective,
    Subsystem,
    SubsystemKind,
)
from orchestrator.validator import ValidationReport


def _make_spec() -> MasterSpec:
    spec = MasterSpec(
        name="Test Assembly",
        objectives=[Objective(name="mass", direction="minimize", unit="kg")],
    )
    spec.subsystems.append(Subsystem(
        name="gear", kind=SubsystemKind.GENERATED,
        material="steel", quantity=1, mass_budget_kg=0.05,
    ))
    spec.subsystems.append(Subsystem(
        name="bearing", kind=SubsystemKind.CATALOG,
        supplier_part="SKF 6201", quantity=2,
    ))
    spec.subsystems.append(Subsystem(
        name="bolt", kind=SubsystemKind.STANDARD,
        standard="ISO 4762 M5x20", quantity=4,
    ))
    spec.interfaces.append(Interface(
        id="ifc1", name="shaft_bore",
        subsystem_a="gear", subsystem_b="shaft",
        mating=MatingSemantic(type="cylindrical_fit"),
        geometry={"diameter_mm": 8.0},
    ))
    return spec


class TestGenerateBom(unittest.TestCase):
    def test_bom_lines(self) -> None:
        spec = _make_spec()
        bom = generate_bom(spec)
        self.assertEqual(len(bom), 3)
        gear_line = bom[0]
        self.assertEqual(gear_line.name, "gear")
        self.assertEqual(gear_line.kind, "generated")
        bearing_line = bom[1]
        self.assertEqual(bearing_line.supplier_part, "SKF 6201")
        self.assertEqual(bearing_line.quantity, 2)
        bolt_line = bom[2]
        self.assertEqual(bolt_line.standard, "ISO 4762 M5x20")
        self.assertEqual(bolt_line.quantity, 4)


class TestGenerateIcdSet(unittest.TestCase):
    def test_icd_set(self) -> None:
        spec = _make_spec()
        icds = generate_icd_set(spec)
        self.assertEqual(len(icds), 1)
        self.assertEqual(icds[0]["name"], "shaft_bore")
        self.assertEqual(icds[0]["mating_type"], "cylindrical_fit")


class TestGenerateDecisionReport(unittest.TestCase):
    def test_contains_sections(self) -> None:
        spec = _make_spec()
        report = generate_decision_report(spec)
        self.assertIn("# Decision Report", report)
        self.assertIn("## Objectives", report)
        self.assertIn("## Subsystems", report)
        self.assertIn("gear", report)
        self.assertIn("bearing", report)


class TestBuildReleasePackage(unittest.TestCase):
    def test_writes_files(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            run_dir.mkdir()
            package = build_release_package(spec, run_dir)
            self.assertTrue(package.package_dir.exists())
            self.assertTrue((package.package_dir / "bom.json").exists())
            self.assertTrue((package.package_dir / "icd_set.json").exists())
            self.assertTrue((package.package_dir / "provenance.json").exists())
            self.assertTrue((package.package_dir / "decision_report.md").exists())
            self.assertTrue((package.package_dir / "spec.yaml").exists())


class TestCheckGateG7(unittest.TestCase):
    def test_complete_passes(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            run_dir.mkdir()
            package = build_release_package(spec, run_dir)
            ok, issues = check_gate_g7(package)
            self.assertTrue(ok, issues)

    def test_empty_fails(self) -> None:
        package = ReleasePackage()
        ok, issues = check_gate_g7(package)
        self.assertFalse(ok)
        self.assertGreater(len(issues), 0)


class TestBomUsesMeasuredMass(unittest.TestCase):
    """Phase 5a: BOM prefers measured mass over budget."""

    def test_measured_mass_in_bom(self) -> None:
        spec = _make_spec()
        reports = [
            ValidationReport(
                subsystem_name="gear", worker_id="gear_0",
                mass_kg=0.042, overall_pass=True,
            ),
        ]
        bom = generate_bom(spec, validation_reports=reports)
        gear_line = next(line for line in bom if line.name == "gear")
        self.assertAlmostEqual(gear_line.mass_kg, 0.042)
        self.assertIn("measured", gear_line.notes)

    def test_budget_fallback(self) -> None:
        spec = _make_spec()
        bom = generate_bom(spec)  # no validation_reports
        gear_line = next(line for line in bom if line.name == "gear")
        self.assertAlmostEqual(gear_line.mass_kg, 0.05)
        self.assertIn("budget", gear_line.notes)


class TestPurchasedPartsList(unittest.TestCase):
    """Phase 5b: purchased parts extraction."""

    def test_extracts_catalog_and_standard(self) -> None:
        spec = _make_spec()
        parts = generate_purchased_parts_list(spec)
        self.assertEqual(len(parts), 2)  # bearing + bolt
        names = {p["name"] for p in parts}
        self.assertIn("bearing", names)
        self.assertIn("bolt", names)

    def test_excludes_generated(self) -> None:
        spec = _make_spec()
        parts = generate_purchased_parts_list(spec)
        names = {p["name"] for p in parts}
        self.assertNotIn("gear", names)

    def test_includes_supplier_part(self) -> None:
        spec = _make_spec()
        parts = generate_purchased_parts_list(spec)
        bearing = next(p for p in parts if p["name"] == "bearing")
        self.assertEqual(bearing["supplier_part"], "SKF 6201")


class TestProvenanceHasGitHash(unittest.TestCase):
    """Phase 5c: provenance includes git_hash and python_version."""

    def test_git_hash_present(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            run_dir.mkdir()
            prov = generate_provenance_manifest(spec, run_dir)
            self.assertIn("git_hash", prov)
            self.assertIn("python_version", prov)
            # git_hash may be empty if not in a git repo, but the key must exist
            self.assertIsInstance(prov["git_hash"], str)


class TestGateG7PurchasedParts(unittest.TestCase):
    """Phase 5d: G7 checks for purchased_parts when spec has CATALOG/STANDARD."""

    def test_missing_purchased_parts_fails(self) -> None:
        spec = _make_spec()
        package = ReleasePackage(
            bom=[object()],  # type: ignore
            icd_set=[{}],
            provenance={"x": 1},
            decision_report="report",
        )
        ok, issues = check_gate_g7(package, spec=spec)
        self.assertFalse(ok)
        self.assertTrue(any("Purchased parts" in i for i in issues))

    def test_with_purchased_parts_passes(self) -> None:
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "test_run"
            run_dir.mkdir()
            package = build_release_package(spec, run_dir)
            ok, issues = check_gate_g7(package, spec=spec)
            self.assertTrue(ok, issues)


if __name__ == "__main__":
    unittest.main()
