"""Tests for the design.brief/v1 → design graph importer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server import json_pointer
from server.dg_import import BriefImportError, import_brief, import_brief_file
from server.dg_models import StructureDoc
from server.dg_store import load_revision
from server.paths import repo_root

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"


class TestImportBrief(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.brief = json.loads(FIXTURE.read_text())
        cls.structure_dict, cls.params = import_brief(cls.brief)
        cls.structure = StructureDoc.from_dict(cls.structure_dict)

    def test_components_and_interfaces_counted(self) -> None:
        self.assertEqual(len(self.structure.components), 11)
        self.assertEqual(len(self.structure.interfaces), 6)

    def test_latch_fillet_leaf(self) -> None:
        leaf = json_pointer.get(self.params, "/parts/latch_sear/specs/fillet_mm")
        self.assertEqual(leaf, {"value": 0.0, "unit": "mm"})

    def test_synthesized_root_mm(self) -> None:
        leaf = json_pointer.get(self.params, "/parts/latch_sear/specs/root_mm")
        self.assertEqual(leaf, {"value": 1.0, "unit": "mm"})
        spec = self.structure.spec_for("/parts/latch_sear/specs/root_mm")
        assert spec is not None
        self.assertEqual(spec.provenance, "synthesized:run.py:LATCH_V1")

    def test_synthesized_impact_factor(self) -> None:
        leaf = json_pointer.get(self.params, "/scenario_defaults/latch_impact_factor")
        self.assertEqual(leaf, {"value": 2.0, "unit": "1"})

    def test_fillet_domain_bounded_by_root(self) -> None:
        spec = self.structure.spec_for("/parts/latch_sear/specs/fillet_mm")
        assert spec is not None and spec.domain is not None
        self.assertEqual(spec.domain.min_value, 0.0)
        self.assertEqual(spec.domain.max_value, 1.0)

    def test_units_inferred(self) -> None:
        spec = self.structure.spec_for("/physical_defaults/spring_k_n_per_m")
        assert spec is not None
        self.assertEqual(spec.unit, "N/m")
        self.assertEqual(
            json_pointer.get(self.params, "/constraints/launch_angle_deg")["unit"], "deg"
        )

    def test_nulls_skipped(self) -> None:
        with self.assertRaises(json_pointer.JsonPointerError):
            json_pointer.get(self.params, "/physical_defaults/dart_diameter_mm")
        self.assertIsNone(self.structure.spec_for("/physical_defaults/dart_diameter_mm"))

    def test_interface_decl_and_specs(self) -> None:
        iface_id = "latch_sear.tooth__plunger_rod.notch"
        decl = next(i for i in self.structure.interfaces if i.id == iface_id)
        self.assertEqual(decl.kind, "latch")
        leaf = json_pointer.get(self.params, f"/interfaces/{iface_id}/hold_force_basis")
        self.assertEqual(leaf["value"], "max_spring_load")

    def test_ports_collected_from_interfaces(self) -> None:
        latch = next(c for c in self.structure.components if c.id == "latch_sear")
        self.assertEqual(latch.ports, ("tooth",))

    def test_materials_and_frames(self) -> None:
        self.assertEqual(self.structure.materials["default"], "pla")
        self.assertIn("z_layers", self.structure.frames)

    def test_every_declared_path_resolves(self) -> None:
        for spec in self.structure.param_specs:
            leaf = json_pointer.get(self.params, spec.path)
            self.assertIn("value", leaf, spec.path)

    def test_wrong_schema_rejected(self) -> None:
        with self.assertRaises(BriefImportError):
            import_brief({"schema": "bogus"})


class TestImportFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"

    def test_import_is_deterministic(self) -> None:
        rev1 = import_brief_file(FIXTURE, root=self.root)
        rev2 = import_brief_file(FIXTURE, root=self.root)
        self.assertEqual(rev1, rev2)

    def test_revision_zero_loads(self) -> None:
        rev = import_brief_file(FIXTURE, root=self.root)
        structure, params, manifest = load_revision(rev, root=self.root)
        self.assertIsNone(manifest.parent)
        self.assertEqual(structure["name"], "Foam-Dart Spring Launcher")
        self.assertEqual(
            json_pointer.get(params, "/parts/latch_sear/specs/fillet_mm")["value"], 0.0
        )

    def test_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "server.dg_import", str(FIXTURE), "--root", str(self.root)],
            capture_output=True,
            text=True,
            cwd=repo_root(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rev_id = proc.stdout.strip()
        self.assertEqual(len(rev_id), 64)
        structure, _, _ = load_revision(rev_id, root=self.root)
        self.assertEqual(structure["name"], "Foam-Dart Spring Launcher")

    def test_cli_bad_file(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "server.dg_import", "/nonexistent.json"],
            capture_output=True,
            text=True,
            cwd=repo_root(),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
