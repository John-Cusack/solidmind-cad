"""Tests for design graph models, hashing, and the revision store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server import artifact_store as cas
from server.dg_models import (
    PARAMS_SCHEMA,
    STRUCTURE_SCHEMA,
    Component,
    InterfaceDecl,
    ParamSpec,
    Revision,
    StructureDoc,
    ValidityDomain,
    compute_revision_id,
    hash_doc,
)
from server.dg_store import DesignGraphError, commit_revision, list_revisions, load_revision


def _structure(name: str = "test") -> StructureDoc:
    return StructureDoc(
        name=name,
        components=(Component(id="latch_sear", ports=("tooth",)),),
        interfaces=(
            InterfaceDecl(
                id="latch_sear.tooth__plunger_rod.notch",
                a=("latch_sear", "tooth"),
                b=("plunger_rod", "notch"),
                kind="latch",
            ),
        ),
        materials={"default": "pla"},
        param_specs=(
            ParamSpec(
                path="/parts/latch_sear/specs/fillet_mm",
                unit="mm",
                domain=ValidityDomain(min_value=0.0, max_value=1.0),
            ),
        ),
    )


def _params(fillet: float = 0.0) -> dict:
    return {
        "schema": PARAMS_SCHEMA,
        "parts": {"latch_sear": {"specs": {"fillet_mm": {"value": fillet, "unit": "mm"}}}},
    }


class TestSerde(unittest.TestCase):
    def test_structure_round_trip(self) -> None:
        doc = _structure()
        self.assertEqual(StructureDoc.from_dict(doc.to_dict()), doc)

    def test_revision_round_trip(self) -> None:
        rev = Revision(structure_hash="a" * 64, params_hash="b" * 64, parent=None)
        self.assertEqual(Revision.from_dict(rev.to_dict()), rev)

    def test_spec_for(self) -> None:
        doc = _structure()
        self.assertIsNotNone(doc.spec_for("/parts/latch_sear/specs/fillet_mm"))
        self.assertIsNone(doc.spec_for("/nope"))


class TestHashing(unittest.TestCase):
    def test_value_change_changes_hash(self) -> None:
        self.assertNotEqual(hash_doc(_params(0.0)), hash_doc(_params(0.5)))

    def test_dict_order_invariance(self) -> None:
        a = {"schema": PARAMS_SCHEMA, "x": 1, "y": 2}
        b = {"y": 2, "x": 1, "schema": PARAMS_SCHEMA}
        self.assertEqual(hash_doc(a), hash_doc(b))

    def test_structure_and_params_hashes_independent(self) -> None:
        s = _structure().to_dict()
        r1 = Revision(structure_hash=hash_doc(s), params_hash=hash_doc(_params(0.0)))
        r2 = Revision(structure_hash=hash_doc(s), params_hash=hash_doc(_params(0.5)))
        self.assertEqual(r1.structure_hash, r2.structure_hash)
        self.assertNotEqual(compute_revision_id(r1), compute_revision_id(r2))


class TestValidityDomain(unittest.TestCase):
    def test_numeric_bounds(self) -> None:
        d = ValidityDomain(min_value=0.0, max_value=1.0)
        self.assertIsNone(d.check(0.5))
        self.assertEqual(d.check(-0.1), "below_min")
        self.assertEqual(d.check(1.1), "above_max")

    def test_choices(self) -> None:
        d = ValidityDomain(choices=("pla", "petg"))
        self.assertIsNone(d.check("pla"))
        self.assertEqual(d.check("abs"), "not_in_choices")

    def test_non_numeric_passes_numeric_domain(self) -> None:
        d = ValidityDomain(min_value=0.0)
        self.assertIsNone(d.check("text"))


class TestRevisionStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"

    def test_commit_load_round_trip(self) -> None:
        structure = _structure().to_dict()
        params = _params(0.0)
        rev_id = commit_revision(structure, params, root=self.root)
        loaded_structure, loaded_params, manifest = load_revision(rev_id, root=self.root)
        self.assertEqual(loaded_structure, structure)
        self.assertEqual(loaded_params, params)
        self.assertIsNone(manifest.parent)

    def test_commit_is_deterministic(self) -> None:
        structure = _structure().to_dict()
        rev1 = commit_revision(structure, _params(0.0), root=self.root)
        rev2 = commit_revision(structure, _params(0.0), root=self.root)
        self.assertEqual(rev1, rev2)

    def test_params_change_changes_revision_id(self) -> None:
        structure = _structure().to_dict()
        rev1 = commit_revision(structure, _params(0.0), root=self.root)
        rev2 = commit_revision(structure, _params(0.5), root=self.root, parent=rev1)
        self.assertNotEqual(rev1, rev2)
        _, _, manifest = load_revision(rev2, root=self.root)
        self.assertEqual(manifest.parent, rev1)

    def test_unknown_parent_rejected(self) -> None:
        with self.assertRaises(DesignGraphError):
            commit_revision(_structure().to_dict(), _params(), parent="f" * 64, root=self.root)

    def test_wrong_schema_rejected(self) -> None:
        with self.assertRaises(DesignGraphError):
            commit_revision({"schema": "bogus"}, _params(), root=self.root)
        with self.assertRaises(DesignGraphError):
            commit_revision(_structure().to_dict(), {"schema": "bogus"}, root=self.root)

    def test_load_non_manifest_object_rejected(self) -> None:
        h = cas.put_json({"schema": STRUCTURE_SCHEMA, "name": "x"}, root=self.root)
        with self.assertRaises(DesignGraphError):
            load_revision(h, root=self.root)

    def test_list_revisions(self) -> None:
        structure = _structure().to_dict()
        rev1 = commit_revision(structure, _params(0.0), root=self.root, meta={"label": "v0"})
        rev2 = commit_revision(structure, _params(0.5), root=self.root, parent=rev1)
        listed = {r["revision_id"]: r for r in list_revisions(root=self.root)}
        self.assertEqual(set(listed), {rev1, rev2})
        self.assertEqual(listed[rev1]["meta"], {"label": "v0"})
        self.assertEqual(listed[rev2]["parent"], rev1)


if __name__ == "__main__":
    unittest.main()
