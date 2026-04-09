"""Tests for the built-in material library."""
from __future__ import annotations

import unittest

from server.analysis_materials import get_material, list_materials


class TestGetMaterial(unittest.TestCase):
    def test_canonical_name(self) -> None:
        m = get_material("aluminum_6061_t6")
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "aluminum_6061_t6")
        self.assertAlmostEqual(m.youngs_modulus_mpa, 68_900)

    def test_alias_lookup(self) -> None:
        for alias in ("aluminum", "aluminium", "al6061", "6061"):
            m = get_material(alias)
            self.assertIsNotNone(m, f"Alias {alias!r} should resolve")
            self.assertEqual(m.name, "aluminum_6061_t6")

    def test_steel_aliases(self) -> None:
        m = get_material("steel")
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "steel_1018")

        m2 = get_material("4140")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.name, "steel_4140")

    def test_case_insensitive(self) -> None:
        m = get_material("Aluminum_6061_T6")
        self.assertIsNotNone(m)

    def test_unknown_returns_none(self) -> None:
        m = get_material("unobtanium")
        self.assertIsNone(m)

    def test_plastics(self) -> None:
        for name in ("pla", "abs", "nylon"):
            m = get_material(name)
            self.assertIsNotNone(m, f"{name} should be in library")

    def test_titanium(self) -> None:
        m = get_material("titanium")
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "titanium_6al4v")
        self.assertGreater(m.yield_strength_mpa, 800)


class TestListMaterials(unittest.TestCase):
    def test_list_all(self) -> None:
        mats = list_materials()
        self.assertGreaterEqual(len(mats), 10)
        names = {m["name"] for m in mats}
        self.assertIn("aluminum_6061_t6", names)
        self.assertIn("pla", names)

    def test_filter_by_category(self) -> None:
        metals = list_materials("metal")
        self.assertGreaterEqual(len(metals), 5)
        for m in metals:
            self.assertNotIn(m["name"], ("pla", "abs", "nylon_6"))

        plastics = list_materials("plastic")
        self.assertEqual(len(plastics), 3)

    def test_unknown_category_empty(self) -> None:
        result = list_materials("ceramic")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
