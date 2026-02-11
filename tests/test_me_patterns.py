from __future__ import annotations

import unittest

import yaml

from server.paths import data_path
from server.resources import list_resources, read_resource


ME_PATTERN_URIS = [
    "resource://me_patterns/index.yml",
    "resource://me_patterns/brackets/mounting_bracket.yml",
    "resource://me_patterns/brackets/l_bracket.yml",
    "resource://me_patterns/enclosures/rectangular_box.yml",
    "resource://me_patterns/fastening/simple_gear.yml",
    "resource://me_patterns/guides/design_for_cnc.yml",
    "resource://me_patterns/guides/design_for_fdm.yml",
]


class TestMEPatternFiles(unittest.TestCase):
    """Validate all ME pattern YAML files parse correctly."""

    def test_all_pattern_files_exist(self) -> None:
        for uri in ME_PATTERN_URIS:
            rel_path = uri.removeprefix("resource://")
            path = data_path(rel_path)
            self.assertTrue(path.exists(), f"Pattern file missing: {path}")

    def test_all_pattern_files_parse_as_yaml(self) -> None:
        for uri in ME_PATTERN_URIS:
            rel_path = uri.removeprefix("resource://")
            path = data_path(rel_path)
            with self.subTest(uri=uri):
                text = path.read_text(encoding="utf-8")
                data = yaml.safe_load(text)
                self.assertIsInstance(data, dict, f"{uri} should parse to a dict")

    def test_index_references_valid_files(self) -> None:
        index_path = data_path("me_patterns", "index.yml")
        index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        self.assertIn("patterns", index)
        for entry in index["patterns"]:
            self.assertIn("id", entry)
            self.assertIn("path", entry)
            full_path = data_path("me_patterns", entry["path"])
            self.assertTrue(full_path.exists(), f"Index references missing file: {entry['path']}")

    def test_part_patterns_have_required_keys(self) -> None:
        required_keys = {"name", "description", "feature_sequence", "typical_dimensions", "manufacturing_notes"}
        part_uris = [u for u in ME_PATTERN_URIS if "guides/" not in u and "index" not in u]
        for uri in part_uris:
            rel_path = uri.removeprefix("resource://")
            path = data_path(rel_path)
            with self.subTest(uri=uri):
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                missing = required_keys - set(data.keys())
                self.assertFalse(missing, f"{uri} missing keys: {missing}")

    def test_guide_patterns_have_required_keys(self) -> None:
        required_keys = {"name", "description"}
        guide_uris = [u for u in ME_PATTERN_URIS if "guides/" in u]
        for uri in guide_uris:
            rel_path = uri.removeprefix("resource://")
            path = data_path(rel_path)
            with self.subTest(uri=uri):
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                missing = required_keys - set(data.keys())
                self.assertFalse(missing, f"{uri} missing keys: {missing}")


class TestMEPatternResources(unittest.TestCase):
    """Validate ME patterns are registered and readable as MCP resources."""

    def test_all_patterns_in_list_resources(self) -> None:
        all_uris = {r["uri"] for r in list_resources()}
        for uri in ME_PATTERN_URIS:
            self.assertIn(uri, all_uris, f"{uri} not in list_resources()")

    def test_all_patterns_readable(self) -> None:
        for uri in ME_PATTERN_URIS:
            with self.subTest(uri=uri):
                result = read_resource(uri)
                self.assertEqual(result["uri"], uri)
                self.assertEqual(result["mimeType"], "text/yaml")
                self.assertIsInstance(result["text"], str)
                self.assertGreater(len(result["text"]), 0)

    def test_read_unknown_resource_raises(self) -> None:
        with self.assertRaises(KeyError):
            read_resource("resource://me_patterns/nonexistent.yml")


if __name__ == "__main__":
    unittest.main()
