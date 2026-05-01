"""Regression tests for ``server.inertia``.

Covers the bugs that motivated extracting this module:

- Box-formula-for-everything was producing izz < ixx for thin disks.
- Multi-body chassis inertia was just "first body's box" — never
  aggregated battery + payload + arms.
- Off-diagonal terms (parallel-axis cross-products) were absent.
"""
from __future__ import annotations

import math
import unittest

from server.inertia import (
    Inertia6,
    InertiaContribution,
    aggregate,
    box_inertia,
    cylinder_inertia,
    is_thin_disk_about_z,
    thin_disk_inertia,
)


class TestBoxInertia(unittest.TestCase):
    def test_unit_cube_unit_mass(self) -> None:
        # I = m/12 * 2*L^2 for each principal axis (cube).
        i = box_inertia(1.0, 1.0, 1.0, 1.0)
        self.assertAlmostEqual(i.ixx, 1.0 / 6.0, places=9)
        self.assertAlmostEqual(i.iyy, 1.0 / 6.0, places=9)
        self.assertAlmostEqual(i.izz, 1.0 / 6.0, places=9)
        # No off-diagonal for a primitive aligned with axes.
        self.assertEqual(i.ixy, 0.0)
        self.assertEqual(i.ixz, 0.0)
        self.assertEqual(i.iyz, 0.0)

    def test_zero_mass_returns_zero_tensor(self) -> None:
        i = box_inertia(0.0, 1.0, 1.0, 1.0)
        self.assertEqual(i.as_tuple(), (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

    def test_thin_plate_dominates_perpendicular_axis(self) -> None:
        # 200×200×30 mm flat plate, 0.3 kg — chassis frame baseline.
        i = box_inertia(0.3, 0.20, 0.20, 0.03)
        # ixx = m/12 * (dy^2 + dz^2) = 0.3/12 * (0.04 + 0.0009) = 0.001022
        self.assertAlmostEqual(i.ixx, 0.001022, places=5)
        # izz dominates: m/12 * (0.04 + 0.04) = 0.002
        self.assertAlmostEqual(i.izz, 0.002, places=5)
        self.assertGreater(i.izz, i.ixx)


class TestThinDiskInertia(unittest.TestCase):
    """Bug regression: rotor inertia must reflect thin-disk physics."""

    def test_izz_is_twice_equatorial_for_zero_thickness(self) -> None:
        # For a thin disk: izz = m·r²/2; ixx = iyy = m·r²/4.
        # → izz = 2·ixx exactly.
        i = thin_disk_inertia(0.016, 0.10, thickness_m=0.0)
        self.assertGreater(i.ixx, 0.0)
        self.assertAlmostEqual(i.izz / i.ixx, 2.0, places=9)
        self.assertAlmostEqual(i.iyy, i.ixx, places=9)
        self.assertEqual(i.ixy, 0.0)

    def test_thickness_adds_only_to_equatorial(self) -> None:
        thin = thin_disk_inertia(0.016, 0.10, thickness_m=0.0)
        thick = thin_disk_inertia(0.016, 0.10, thickness_m=0.005)
        self.assertEqual(thin.izz, thick.izz)            # izz unchanged
        self.assertGreater(thick.ixx, thin.ixx)          # ixx gains m·t²/12

    def test_axis_y_swaps_components(self) -> None:
        # Disk spinning about Y should put the m·r²/2 term on iyy.
        i = thin_disk_inertia(1.0, 0.5, thickness_m=0.0, axis="y")
        self.assertAlmostEqual(i.iyy, 0.5 * 1.0 * 0.25, places=9)
        self.assertAlmostEqual(i.ixx, 0.25 * 1.0 * 0.25, places=9)

    def test_helper_recognises_thin_disk_about_z(self) -> None:
        i = thin_disk_inertia(0.016, 0.10, thickness_m=0.005)
        self.assertTrue(is_thin_disk_about_z(i, tol=1e-3))
        # A box doesn't satisfy the thin-disk relation
        self.assertFalse(is_thin_disk_about_z(box_inertia(0.5, 0.1, 0.1, 0.1)))


class TestCylinderInertia(unittest.TestCase):
    def test_solid_cylinder_about_z(self) -> None:
        i = cylinder_inertia(2.0, 0.05, 0.30, axis="z")
        # izz = m·r²/2 = 2·0.0025/2 = 0.0025
        self.assertAlmostEqual(i.izz, 0.0025, places=9)
        # ixx = m/12 · (3r² + h²) = 2/12 · (0.0075 + 0.09) = 0.01625
        self.assertAlmostEqual(i.ixx, (2.0 / 12.0) * (3 * 0.0025 + 0.09), places=9)


class TestAggregateParallelAxis(unittest.TestCase):
    """Regression: chassis with 3 bodies must aggregate via parallel-axis."""

    def test_single_body_at_origin_returns_unchanged(self) -> None:
        body = box_inertia(1.0, 0.1, 0.1, 0.1)
        contrib = InertiaContribution(1.0, (0.0, 0.0, 0.0), body)
        mass, com, total = aggregate([contrib])
        self.assertAlmostEqual(mass, 1.0, places=9)
        self.assertEqual(com, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(total.ixx, body.ixx, places=9)
        self.assertAlmostEqual(total.iyy, body.iyy, places=9)
        self.assertAlmostEqual(total.izz, body.izz, places=9)

    def test_parallel_axis_offset_inflates_inertia(self) -> None:
        # Two unit-mass point-like bodies at (+L, 0) and (−L, 0).
        # Combined COM is origin; aggregate Ixx should be tiny (no Y/Z
        # offsets), but Iyy/Izz pick up 2·m·L² from the +/- offsets.
        L = 0.20
        small_box = box_inertia(1.0, 0.001, 0.001, 0.001)  # essentially point mass
        contribs = [
            InertiaContribution(1.0, (+L, 0.0, 0.0), small_box),
            InertiaContribution(1.0, (-L, 0.0, 0.0), small_box),
        ]
        mass, com, total = aggregate(contribs)
        self.assertAlmostEqual(mass, 2.0, places=9)
        # COM at origin (symmetric)
        self.assertAlmostEqual(com[0], 0.0, places=9)
        # Iyy and Izz get 2·m·L² = 2·1·0.04 = 0.08 each (point-mass limit)
        self.assertAlmostEqual(total.iyy, 0.08, places=4)
        self.assertAlmostEqual(total.izz, 0.08, places=4)
        # No off-diagonal because the offsets are along X only
        self.assertAlmostEqual(total.ixy, 0.0, places=9)

    def test_chassis_aggregation_not_just_chassis_box(self) -> None:
        """Bug F: 1.5 kg chassis + 1.5 kg battery must not look like just chassis.

        Before the fix the chassis link's inertia was computed from its
        single bounding box (a thin plate), giving ixx ≈ 0.005.  With
        the battery aggregated, ixx must be at least ~3× larger because
        the battery's mass is offset upward.
        """
        chassis_body = box_inertia(1.5, 0.20, 0.20, 0.03)   # ixx ≈ 0.0051
        battery_body = box_inertia(1.5, 0.10, 0.08, 0.04)
        contribs = [
            InertiaContribution(1.5, (0.0, 0.0, 0.0), chassis_body),
            InertiaContribution(1.5, (0.0, 0.0, 0.05), battery_body),  # 5 cm above
        ]
        mass, com, total = aggregate(contribs)
        # Combined COM picks up half the offset
        self.assertAlmostEqual(com[2], 0.025, places=4)
        # Aggregate ixx is meaningfully larger than chassis-alone:
        # body inertias sum (~0.0064) plus parallel-axis term
        # 2·m·rz² = 2·1.5·(0.025)² ≈ 0.0019 — adds another 30 %.
        self.assertGreater(total.ixx, 1.4 * chassis_body.ixx)

    def test_empty_input_returns_zero(self) -> None:
        mass, com, total = aggregate([])
        self.assertEqual(mass, 0.0)
        self.assertEqual(com, (0.0, 0.0, 0.0))
        self.assertEqual(total.ixx, 0.0)


if __name__ == "__main__":
    unittest.main()
