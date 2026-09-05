"""Tests for the content-addressed artifact store."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import artifact_store as cas
from server.artifact_store import ArtifactError


class ArtifactStoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"


class TestObjects(ArtifactStoreBase):
    def test_put_get_json_round_trip(self) -> None:
        obj = {"b": 2, "a": [1.5, "x", None, True]}
        h = cas.put_json(obj, root=self.root)
        self.assertEqual(cas.get_json(h, root=self.root), obj)

    def test_hash_is_sha256_of_stored_bytes(self) -> None:
        h = cas.put_json({"k": 1}, root=self.root)
        path = self.root / "objects" / h[:2] / h
        self.assertTrue(path.exists())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), h)

    def test_dict_order_invariance(self) -> None:
        h1 = cas.put_json({"a": 1, "b": 2}, root=self.root)
        h2 = cas.put_json({"b": 2, "a": 1}, root=self.root)
        self.assertEqual(h1, h2)

    def test_value_change_changes_hash(self) -> None:
        h1 = cas.put_json({"a": 1.0}, root=self.root)
        h2 = cas.put_json({"a": 1.0000001}, root=self.root)
        self.assertNotEqual(h1, h2)

    def test_put_twice_is_dedupe_noop(self) -> None:
        h1 = cas.put_json({"a": 1}, root=self.root)
        h2 = cas.put_json({"a": 1}, root=self.root)
        self.assertEqual(h1, h2)
        objects = list((self.root / "objects").rglob(h1))
        self.assertEqual(len(objects), 1)

    def test_get_missing_raises(self) -> None:
        with self.assertRaises(ArtifactError):
            cas.get_json("0" * 64, root=self.root)

    def test_corrupt_object_detected_on_read(self) -> None:
        h = cas.put_json({"a": 1}, root=self.root)
        path = self.root / "objects" / h[:2] / h
        path.write_bytes(b'{"a":2}')
        with self.assertRaises(ArtifactError):
            cas.get_json(h, root=self.root)

    def test_put_bytes_round_trip(self) -> None:
        data = b"\x00\x01binary"
        h = cas.put_bytes(data, root=self.root)
        self.assertEqual(cas.get_bytes(h, root=self.root), data)

    def test_bad_hash_rejected(self) -> None:
        with self.assertRaises(ArtifactError):
            cas.exists("not-a-hash", root=self.root)

    def test_atomic_write_failure_leaves_no_object(self) -> None:
        with patch("server.artifact_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cas.put_json({"a": 1}, root=self.root)
        h = hashlib.sha256(b'{"a":1}').hexdigest()
        self.assertFalse((self.root / "objects" / h[:2] / h).exists())


class TestRefsAndLineage(ArtifactStoreBase):
    def test_set_get_ref(self) -> None:
        h = cas.put_json({"a": 1}, root=self.root)
        cas.set_ref("dg_revisions", "rev0", h, meta={"note": "x"}, root=self.root)
        record = cas.get_ref("dg_revisions", "rev0", root=self.root)
        assert record is not None
        self.assertEqual(record["hash"], h)
        self.assertEqual(record["meta"], {"note": "x"})
        self.assertIn("created_at", record)

    def test_get_missing_ref_returns_none(self) -> None:
        self.assertIsNone(cas.get_ref("dg_revisions", "nope", root=self.root))

    def test_list_refs(self) -> None:
        h1 = cas.put_json({"a": 1}, root=self.root)
        h2 = cas.put_json({"a": 2}, root=self.root)
        cas.set_ref("ns", "beta", h2, root=self.root)
        cas.set_ref("ns", "alpha", h1, root=self.root)
        records = cas.list_refs("ns", root=self.root)
        self.assertEqual([r["name"] for r in records], ["alpha", "beta"])
        self.assertEqual(cas.list_refs("empty_ns", root=self.root), [])

    def test_unsafe_ref_name_rejected(self) -> None:
        h = cas.put_json({"a": 1}, root=self.root)
        with self.assertRaises(ArtifactError):
            cas.set_ref("ns", "../escape", h, root=self.root)
        with self.assertRaises(ArtifactError):
            cas.set_ref("ns/sub", "name", h, root=self.root)

    def test_lineage_round_trip(self) -> None:
        h_in = cas.put_json({"in": 1}, root=self.root)
        h_out = cas.put_json({"out": 2}, root=self.root)
        cas.record_lineage(
            h_out,
            op="evaluate",
            inputs={"revision": h_in},
            env_identity_hash="e" * 64,
            root=self.root,
        )
        record = cas.get_lineage(h_out, root=self.root)
        assert record is not None
        self.assertEqual(record["op"], "evaluate")
        self.assertEqual(record["inputs"], {"revision": h_in})

    def test_lineage_rejects_bad_input_hash(self) -> None:
        h_out = cas.put_json({"out": 2}, root=self.root)
        with self.assertRaises(ArtifactError):
            cas.record_lineage(
                h_out, op="x", inputs={"bad": "zz"}, env_identity_hash="e" * 64, root=self.root
            )


class TestResolve(ArtifactStoreBase):
    def test_hex_passthrough(self) -> None:
        h = "a" * 64
        self.assertEqual(cas.resolve(h, root=self.root), h)

    def test_named_ref_resolution(self) -> None:
        h = cas.put_json({"a": 1}, root=self.root)
        cas.set_ref("dg_revisions", "latest", h, root=self.root)
        self.assertEqual(cas.resolve("latest", root=self.root), h)

    def test_unknown_ref_raises(self) -> None:
        with self.assertRaises(ArtifactError):
            cas.resolve("missing", root=self.root)


class TestRootResolution(unittest.TestCase):
    def test_env_override(self) -> None:
        with patch.dict("os.environ", {"SOLIDMIND_ARTIFACTS_ROOT": "/tmp/custom-root"}):
            self.assertEqual(cas.store_root(None), Path("/tmp/custom-root"))

    def test_explicit_root_wins(self) -> None:
        with patch.dict("os.environ", {"SOLIDMIND_ARTIFACTS_ROOT": "/tmp/custom-root"}):
            self.assertEqual(cas.store_root(Path("/explicit")), Path("/explicit"))


if __name__ == "__main__":
    unittest.main()
