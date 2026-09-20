"""Conservation, affine-field consistency and restart contracts of linear heat FEM."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

try:
    import meshio

    from server.analysis_linear_thermal import LinearThermalBody
except ModuleNotFoundError as exc:
    if exc.name not in {"meshio", "scipy"}:
        raise
    HAS_THERMAL_DEPENDENCIES = False
else:
    HAS_THERMAL_DEPENDENCIES = True


@unittest.skipUnless(HAS_THERMAL_DEPENDENCIES, "requires the fea and fea-cpu extras")
class TestLinearThermal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cube.msh"
        points = np.array(
            [
                [0.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [1.0, 1.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
                [1.0, 0.0, 1.0],
                [1.0, 1.0, 1.0],
                [0.0, 1.0, 1.0],
            ]
        )
        cells = np.array(
            [[0, 1, 2, 6], [0, 2, 3, 6], [0, 3, 7, 6], [0, 7, 4, 6], [0, 4, 5, 6], [0, 5, 1, 6]]
        )
        faces = {}
        for cell in cells:
            for missing in range(4):
                key = tuple(sorted(np.delete(cell, missing)))
                faces[key] = faces.get(key, 0) + 1
        boundary = np.array([face for face, count in faces.items() if count == 1])
        tags = np.array(
            [
                1 if np.all(points[f, 0] == 0) else 2 if np.all(points[f, 0] == 1) else 3
                for f in boundary
            ]
        )
        meshio.write(
            self.path,
            meshio.Mesh(
                points,
                [("tetra", cells), ("triangle", boundary)],
                cell_data={
                    "gmsh:physical": [np.full(len(cells), 4), tags],
                    "gmsh:geometrical": [np.full(len(cells), 4), tags],
                },
            ),
            file_format="gmsh22",
            binary=False,
        )
        self.body = LinearThermalBody.from_mesh(
            self.path,
            {"left": 1, "right": 2, "other": 3},
            density_kg_m3=2.0,
            heat_capacity_j_kg_k=3.0,
            conductivity_w_m_k=4.0,
        )

    def test_insulated_uniform_field_remains_uniform(self):
        initial = self.body.uniform_temperature(300.0)
        result = self.body.step(initial, 10.0, {"left": 0.0, "right": 0.0, "other": 0.0})
        np.testing.assert_allclose(result.temperatures_k, initial, rtol=0, atol=1e-11)
        self.assertAlmostEqual(result.energy_change_j, 0.0, places=10)

    def test_affine_temperature_and_balanced_flux_are_stationary(self):
        initial = 300.0 + 2.0 * self.body.points[:, 0]
        result = self.body.step(initial, 1.0, {"left": -8.0, "right": 8.0, "other": 0.0})
        np.testing.assert_allclose(result.temperatures_k, initial, rtol=0, atol=1e-10)
        self.assertAlmostEqual(result.balance_j, 0.0, places=10)

    def test_flux_changes_integrated_energy_without_mutating_accepted_state(self):
        initial = self.body.uniform_temperature(300.0)
        result = self.body.step(initial, 0.1, {"left": 1.0, "right": 1.0, "other": 1.0})
        self.assertAlmostEqual(result.energy_change_j, 0.6, places=10)
        self.assertAlmostEqual(result.balance_j, 0.0, places=10)
        self.assertGreater(result.temperatures_k.max(), 300.0)
        np.testing.assert_array_equal(initial, np.full(8, 300.0))

    def test_substeps_match_restarted_temperature_and_energy(self):
        flux = {"left": -2.0, "right": 1.0, "other": 0.0}
        initial = self.body.uniform_temperature(300.0)
        combined = self.body.step(initial, 0.2, flux, nsub=2)
        first = self.body.step(initial, 0.1, flux)
        second = self.body.step(first.temperatures_k, 0.1, flux)
        np.testing.assert_allclose(combined.temperatures_k, second.temperatures_k, atol=1e-12)
        self.assertAlmostEqual(
            combined.energy_change_j, first.energy_change_j + second.energy_change_j
        )

    def test_boundary_response_predicts_full_spatial_trial(self):
        initial = 300.0 + 2.0 * self.body.points[:, 0]
        zero = dict.fromkeys(self.body.patch_areas_m2, 0.0)
        flux = {"left": -2.0, "right": 1.0, "other": 0.5}
        free = self.body.step(initial, 0.2, zero, nsub=2)
        loaded = self.body.step(initial, 0.2, flux, nsub=2)
        response = self.body.boundary_response(0.2, nsub=2)
        names = tuple(self.body.patch_areas_m2)
        power = np.array([flux[name] * self.body.patch_areas_m2[name] for name in names])
        predicted = np.array([free.wall_temps_k[name] for name in names]) + response @ power
        np.testing.assert_allclose(
            predicted, [loaded.wall_temps_k[name] for name in names], rtol=0, atol=1e-10
        )

    def test_near_perfect_contact_conserves_heat_without_relaxation(self):
        response = self.body.boundary_response(0.1)
        names = tuple(self.body.patch_areas_m2)
        right, left = names.index("right"), names.index("left")
        resistance = 1e-8
        power = 200.0 / (resistance + response[right, right] + response[left, left])
        hot = self.body.step(
            self.body.uniform_temperature(500.0), 0.1, {"left": 0.0, "right": -power, "other": 0.0}
        )
        cold = self.body.step(
            self.body.uniform_temperature(300.0), 0.1, {"left": power, "right": 0.0, "other": 0.0}
        )
        self.assertAlmostEqual(hot.energy_change_j + cold.energy_change_j, 0.0, places=9)
        self.assertAlmostEqual(
            hot.wall_temps_k["right"] - cold.wall_temps_k["left"],
            power * resistance,
            places=10,
        )

    def test_unknown_or_missing_flux_patch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "every named"):
            self.body.step(self.body.uniform_temperature(300.0), 1.0, {"left": 1.0})

    def test_missing_boundary_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no triangles"):
            LinearThermalBody.from_mesh(
                self.path,
                {"missing": 999},
                density_kg_m3=2.0,
                heat_capacity_j_kg_k=3.0,
                conductivity_w_m_k=4.0,
            )


if __name__ == "__main__":
    unittest.main()
