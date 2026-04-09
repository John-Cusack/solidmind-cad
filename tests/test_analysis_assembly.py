"""Unit tests for direct tet4 assembly utilities."""
from __future__ import annotations

import unittest

import numpy as np

from server.analysis_assembly import (
    _apply_total_force_bc,
    assemble_tet4_system,
)
from server.analysis_models import BoundaryCondition, Material


try:
    import scipy  # noqa: F401
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False


def _material() -> Material:
    return Material(
        name="test_steel",
        youngs_modulus_mpa=210_000.0,
        poissons_ratio=0.30,
        density_kg_m3=7800.0,
        yield_strength_mpa=250.0,
    )


@unittest.skipUnless(HAS_SCIPY, "scipy not installed")
class TestAssemblyCore(unittest.TestCase):
    def setUp(self) -> None:
        self.node_coords = np.array(
            [
                [0.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        self.connectivity = np.array([[0, 1, 2, 3]], dtype=np.int64)

    def test_matrix_symmetry_and_spd_after_bcs(self) -> None:
        nodal_forces = np.zeros(12, dtype=np.float64)
        system = assemble_tet4_system(
            self.node_coords,
            self.connectivity,
            _material(),
            fixed_nodes={0, 1, 2},
            nodal_forces=nodal_forces,
            bc_hash="bc_a",
        )
        K = system.K.toarray()
        self.assertTrue(np.allclose(K, K.T, atol=1e-8))

        eigvals = np.linalg.eigvalsh(K)
        self.assertTrue(np.all(eigvals > 0.0), msg=f"Eigenvalues not SPD: {eigvals}")

    def test_force_contract_distributes_total_force(self) -> None:
        nodal_forces = np.zeros(12, dtype=np.float64)
        bc = BoundaryCondition(
            bc_type="force",
            faces=("Face1",),
            value={"fx": 10.0, "fy": -6.0, "fz": 4.0},
        )
        _apply_total_force_bc(nodal_forces, np.array([0, 3], dtype=np.int64), bc)

        self.assertAlmostEqual(float(nodal_forces[0]), 5.0)
        self.assertAlmostEqual(float(nodal_forces[1]), -3.0)
        self.assertAlmostEqual(float(nodal_forces[2]), 2.0)
        self.assertAlmostEqual(float(nodal_forces[9]), 5.0)
        self.assertAlmostEqual(float(nodal_forces[10]), -3.0)
        self.assertAlmostEqual(float(nodal_forces[11]), 2.0)

    def test_cache_key_changes_with_bc_hash(self) -> None:
        nodal_forces = np.zeros(12, dtype=np.float64)

        a = assemble_tet4_system(
            self.node_coords,
            self.connectivity,
            _material(),
            fixed_nodes={0, 1, 2},
            nodal_forces=nodal_forces,
            topology_hash="topo",
            material_hash="mat",
            bc_hash="bc_a",
            element_order="tet4",
            precision="float64",
            options_signature="opt",
        )
        b = assemble_tet4_system(
            self.node_coords,
            self.connectivity,
            _material(),
            fixed_nodes={0, 1, 2},
            nodal_forces=nodal_forces,
            topology_hash="topo",
            material_hash="mat",
            bc_hash="bc_b",
            element_order="tet4",
            precision="float64",
            options_signature="opt",
        )

        self.assertNotEqual(a.factor_cache_key("cholmod"), b.factor_cache_key("cholmod"))


if __name__ == "__main__":
    unittest.main()
