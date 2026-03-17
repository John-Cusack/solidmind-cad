"""Tests for orchestrator.materials."""
from __future__ import annotations

import unittest

from orchestrator.materials import MATERIAL_DB, Material, resolve_material


class TestResolveMaterial(unittest.TestCase):
    def test_alias_steel(self):
        mat = resolve_material("steel")
        self.assertIsNotNone(mat)
        self.assertEqual(mat.name, "AISI 1045")

    def test_alias_aluminum(self):
        mat = resolve_material("aluminum")
        self.assertIsNotNone(mat)
        self.assertEqual(mat.name, "Al 6061-T6")

    def test_exact_key(self):
        mat = resolve_material("20MnCr5")
        self.assertIsNotNone(mat)
        self.assertEqual(mat.young_modulus_mpa, 210_000)
        self.assertEqual(mat.yield_strength_mpa, 590)

    def test_case_insensitive(self):
        mat = resolve_material("AISI_304")
        self.assertIsNotNone(mat)
        mat2 = resolve_material("aisi_304")
        self.assertIsNotNone(mat2)
        self.assertEqual(mat.name, mat2.name)

    def test_unknown_returns_none(self):
        self.assertIsNone(resolve_material("unobtainium"))

    def test_empty_returns_none(self):
        self.assertIsNone(resolve_material(""))

    def test_all_materials_valid(self):
        for key, mat in MATERIAL_DB.items():
            with self.subTest(key=key):
                self.assertGreater(mat.young_modulus_mpa, 0, f"{key} E <= 0")
                self.assertGreater(mat.poisson_ratio, 0, f"{key} v <= 0")
                self.assertLess(mat.poisson_ratio, 0.5, f"{key} v >= 0.5")
                self.assertGreater(mat.yield_strength_mpa, 0, f"{key} Sy <= 0")
                self.assertGreater(mat.density_kg_m3, 0, f"{key} rho <= 0")

    def test_display_name_lookup(self):
        mat = resolve_material("AISI 1045")
        self.assertIsNotNone(mat)
        self.assertEqual(mat.name, "AISI 1045")


if __name__ == "__main__":
    unittest.main()
