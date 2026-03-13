"""Tests for orchestrator.spec — dataclass round-trip with all v3 fields."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.spec import (
    ArtifactEntry,
    AssemblySkeleton,
    ComplexityClass,
    CostPolicy,
    FailureCode,
    Interface,
    KnowledgeConfig,
    MasterSpec,
    Objective,
    ProvenanceManifest,
    SpecStatus,
    Subsystem,
    SubsystemKind,
    WorkerMode,
    WorkerResult,
)


class TestSpecStatus(unittest.TestCase):
    def test_new_states_exist(self) -> None:
        self.assertEqual(SpecStatus.NORMALIZING.value, "normalizing")
        self.assertEqual(SpecStatus.LAYOUT_FROZEN.value, "layout_frozen")
        self.assertEqual(SpecStatus.RELEASE_PACKAGING.value, "release_packaging")

    def test_all_v2_states_preserved(self) -> None:
        for name in ("DRAFT", "COUNCIL_REVIEW", "INTERFACES_FROZEN", "BUILDING",
                      "GEOMETRY_VALIDATING", "SCORING", "AWAITING_HUMAN", "DONE", "FAILED"):
            self.assertIn(name, SpecStatus.__members__)


class TestSubsystemKind(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(SubsystemKind.GENERATED.value, "generated")
        self.assertEqual(SubsystemKind.CATALOG.value, "catalog")
        self.assertEqual(SubsystemKind.STANDARD.value, "standard")


class TestFailureCode(unittest.TestCase):
    def test_new_codes(self) -> None:
        self.assertEqual(FailureCode.SKELETON_CONFLICT.value, "SKELETON_CONFLICT")
        self.assertEqual(FailureCode.ICD_INCOMPLETE.value, "ICD_INCOMPLETE")
        self.assertEqual(FailureCode.ASSEMBLY_ACCESS_FAIL.value, "ASSEMBLY_ACCESS_FAIL")


class TestAssemblySkeleton(unittest.TestCase):
    def test_defaults(self) -> None:
        sk = AssemblySkeleton()
        self.assertEqual(sk.datums, {})
        self.assertEqual(sk.shaft_axes, {})
        self.assertEqual(sk.keepout_zones, [])


class TestProvenanceManifest(unittest.TestCase):
    def test_fields(self) -> None:
        p = ProvenanceManifest(run_id="r1", worker_id="w1", spec_hash="abc")
        self.assertEqual(p.run_id, "r1")
        self.assertEqual(p.tool_versions, {})


class TestArtifactEntry(unittest.TestCase):
    def test_fields(self) -> None:
        a = ArtifactEntry(path="/out/part.step", sha256="deadbeef", size_bytes=1024)
        self.assertEqual(a.path, "/out/part.step")
        self.assertEqual(a.size_bytes, 1024)


class TestSubsystemExtensions(unittest.TestCase):
    def test_kind_default(self) -> None:
        s = Subsystem(name="gear")
        self.assertEqual(s.kind, SubsystemKind.GENERATED)

    def test_catalog_part(self) -> None:
        s = Subsystem(
            name="bearing",
            kind=SubsystemKind.CATALOG,
            supplier_part="SKF 6201-2Z",
        )
        self.assertEqual(s.kind, SubsystemKind.CATALOG)
        self.assertEqual(s.supplier_part, "SKF 6201-2Z")

    def test_standard_part(self) -> None:
        s = Subsystem(
            name="bolt",
            kind=SubsystemKind.STANDARD,
            standard="ISO 4762 M5x20",
        )
        self.assertEqual(s.standard, "ISO 4762 M5x20")


class TestInterfaceExtensions(unittest.TestCase):
    def test_icd_fields(self) -> None:
        ifc = Interface(
            name="shaft_bore",
            datum_scheme="A-B-C",
            ctqs=["bore_diameter", "concentricity"],
            inspection={"method": "CMM", "frequency": "100%"},
        )
        self.assertEqual(ifc.datum_scheme, "A-B-C")
        self.assertEqual(len(ifc.ctqs), 2)
        self.assertEqual(ifc.inspection["method"], "CMM")


class TestWorkerResultExtensions(unittest.TestCase):
    def test_provenance_and_artifacts(self) -> None:
        wr = WorkerResult(
            subsystem_name="sun",
            provenance=ProvenanceManifest(run_id="r1", worker_id="w1"),
            artifact_manifest=[
                ArtifactEntry(path="sun.step", sha256="abc", size_bytes=500),
            ],
        )
        self.assertEqual(wr.provenance.run_id, "r1")
        self.assertEqual(len(wr.artifact_manifest), 1)


class TestMasterSpecSkeleton(unittest.TestCase):
    def test_skeleton_field(self) -> None:
        spec = MasterSpec(name="test")
        self.assertIsInstance(spec.skeleton, AssemblySkeleton)

    def test_skeleton_with_data(self) -> None:
        sk = AssemblySkeleton(
            datums={"A": [0, 0, 0], "B": [10, 0, 0]},
            shaft_axes={"main": {"origin": [0, 0, 0], "direction": [0, 0, 1]}},
        )
        spec = MasterSpec(name="test", skeleton=sk)
        self.assertEqual(spec.skeleton.datums["A"], [0, 0, 0])


class TestMasterSpecYamlRoundTrip(unittest.TestCase):
    """Full round-trip: build spec → YAML → load → compare."""

    def _make_spec(self) -> MasterSpec:
        spec = MasterSpec(
            id="test123",
            name="Test Assembly",
            description="Round-trip test",
            status=SpecStatus.LAYOUT_FROZEN,
            worker_mode=WorkerMode.CLAUDE_CODE,
            global_constraints={"max_mass_kg": 1.0},
            objectives=[
                Objective(name="mass", direction="minimize", unit="kg", weight=1.0),
            ],
            skeleton=AssemblySkeleton(
                datums={"A": [0, 0, 0]},
                shaft_axes={"main": {"dir": [0, 0, 1]}},
                reserved_volumes={"motor": {"bbox": [20, 20, 30]}},
                keepout_zones=[{"name": "cable_run", "bbox": [5, 5, 50]}],
            ),
            cost_policy=CostPolicy(max_run_cost_usd=25.0),
        )
        spec.subsystems.append(Subsystem(
            id="s1",
            name="sun_gear",
            kind=SubsystemKind.GENERATED,
            complexity_class=ComplexityClass.S,
            envelope_mm=[16, 16, 20],
            mass_budget_kg=0.02,
            material="steel",
            assembly_constraints={"coaxial_with": "main_shaft"},
        ))
        spec.subsystems.append(Subsystem(
            id="s2",
            name="bearing",
            kind=SubsystemKind.CATALOG,
            supplier_part="SKF 6201-2Z",
            complexity_class=ComplexityClass.S,
        ))
        spec.interfaces.append(Interface(
            id="ifc1",
            name="shaft_bore",
            subsystem_a="sun_gear",
            port_a="bore",
            subsystem_b="input_shaft",
            port_b="spline",
            datum_scheme="A-B",
            ctqs=["bore_dia"],
            inspection={"method": "CMM"},
        ))
        return spec

    def test_yaml_round_trip(self) -> None:
        original = self._make_spec()
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)
        try:
            original.save(path)
            loaded = MasterSpec.load(path)

            # Core fields
            self.assertEqual(loaded.id, original.id)
            self.assertEqual(loaded.name, original.name)
            self.assertEqual(loaded.status, SpecStatus.LAYOUT_FROZEN)

            # Skeleton
            self.assertEqual(loaded.skeleton.datums, {"A": [0, 0, 0]})
            self.assertEqual(len(loaded.skeleton.keepout_zones), 1)

            # Subsystem kind
            sun = loaded.get_subsystem("sun_gear")
            self.assertIsNotNone(sun)
            self.assertEqual(sun.kind, SubsystemKind.GENERATED)
            self.assertEqual(sun.assembly_constraints, {"coaxial_with": "main_shaft"})

            bearing = loaded.get_subsystem("bearing")
            self.assertIsNotNone(bearing)
            self.assertEqual(bearing.kind, SubsystemKind.CATALOG)
            self.assertEqual(bearing.supplier_part, "SKF 6201-2Z")

            # Interface ICD extensions
            ifc = loaded.get_interface("ifc1")
            self.assertIsNotNone(ifc)
            self.assertEqual(ifc.datum_scheme, "A-B")
            self.assertEqual(ifc.ctqs, ["bore_dia"])
            self.assertEqual(ifc.inspection, {"method": "CMM"})
        finally:
            path.unlink(missing_ok=True)

    def test_backward_compat_load(self) -> None:
        """Loading a v2-era YAML (no new fields) should use defaults."""
        v2_yaml = """\
id: old123
name: Old Spec
status: draft
worker_mode: claude_code
global_constraints: {}
objectives: []
subsystems:
  - id: s1
    name: gear
    description: test
    envelope_mm: [10, 10, 10]
    interfaces: []
    specs: {}
    worker_count: 1
    complexity_class: S
    manufacturing:
      process: CNC_turning
      min_feature_size_mm: 0.5
      min_wall_mm: 1.0
      notes: ""
interfaces: []
knowledge:
  global_paths: []
  project_path: ""
  share_mode: project_slice
cost_policy:
  max_run_cost_usd: 50.0
  max_stage_cost_usd: 20.0
  warn_at_pct: 80
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False,
        ) as f:
            f.write(v2_yaml)
            path = Path(f.name)
        try:
            loaded = MasterSpec.load(path)
            self.assertEqual(loaded.status, SpecStatus.DRAFT)
            # New fields should have defaults
            gear = loaded.get_subsystem("gear")
            self.assertIsNotNone(gear)
            self.assertEqual(gear.kind, SubsystemKind.GENERATED)
            self.assertEqual(gear.standard, "")
            self.assertEqual(gear.supplier_part, "")
            self.assertIsInstance(loaded.skeleton, AssemblySkeleton)
            self.assertEqual(loaded.skeleton.datums, {})
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
