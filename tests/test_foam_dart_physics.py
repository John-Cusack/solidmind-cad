"""Physics tests for the foam-dart spring launcher example.

Covers the calibration-first model: monotonicity, the zero/limit cases, the
v∝x / range∝x² relationships, calibration round-trips, and input validation.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "foam_dart_spring_launcher"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

import physics_model as pm  # noqa: E402


def _spec(**kw) -> pm.LauncherSpec:
    base = dict(spring_k_n_per_m=300.0, dart_mass_kg=0.001, efficiency=0.45)
    base.update(kw)
    return pm.LauncherSpec(**base)


class TestEnergyChain(unittest.TestCase):
    def test_spring_energy(self) -> None:
        self.assertAlmostEqual(pm.spring_energy_j(300.0, 0.02), 0.5 * 300.0 * 0.02 ** 2)

    def test_velocity_proportional_to_pullback(self) -> None:
        # Fixed efficiency ⇒ v ∝ x.
        s = _spec()
        v1 = pm.muzzle_velocity_m_s(s, 0.010)
        v2 = pm.muzzle_velocity_m_s(s, 0.020)
        v3 = pm.muzzle_velocity_m_s(s, 0.030)
        self.assertAlmostEqual(v2 / v1, 2.0, places=6)
        self.assertAlmostEqual(v3 / v1, 3.0, places=6)

    def test_zero_compression_zero_range(self) -> None:
        s = _spec()
        self.assertEqual(pm.muzzle_velocity_m_s(s, 0.0), 0.0)
        self.assertEqual(pm.predicted_range_m(s, 0.0), 0.0)


class TestRange(unittest.TestCase):
    def test_range_monotonic_increasing_in_pullback(self) -> None:
        s = _spec()
        ranges = [pm.predicted_range_m(s, x / 1000.0) for x in (10, 20, 30)]
        self.assertTrue(all(b > a for a, b in zip(ranges, ranges[1:])))

    def test_range_ratio_tracks_x_squared_no_drag_no_height(self) -> None:
        # With launch_height 0 and no drag, range ∝ v² ∝ x².
        s = _spec(launch_height_m=0.0)
        r1 = pm.predicted_range_m(s, 0.010)
        r2 = pm.predicted_range_m(s, 0.020)
        r3 = pm.predicted_range_m(s, 0.030)
        self.assertAlmostEqual(r2 / r1, 4.0, places=5)
        self.assertAlmostEqual(r3 / r1, 9.0, places=5)

    def test_projectile_matches_closed_form_at_zero_height(self) -> None:
        # R = v² sin(2θ) / g  for launch height 0.
        v, ang = 10.0, 30.0
        r = pm.projectile_range_m(v, ang, 0.0)
        expected = v * v * math.sin(math.radians(2 * ang)) / pm.GRAVITY_M_S2
        self.assertAlmostEqual(r, expected, places=6)


class TestCalibration(unittest.TestCase):
    def test_calibration_round_trip(self) -> None:
        # Generate a synthetic "measured" shot from a known efficiency, then
        # recover that efficiency by calibrating against it.
        true_spec = _spec(efficiency=0.38)
        measured_range = pm.predicted_range_m(true_spec, 0.020)
        guess = _spec(efficiency=0.45)  # wrong starting efficiency
        cal = pm.calibrate_from_shot(guess, 20.0, measured_range)
        self.assertAlmostEqual(cal.efficiency, 0.38, places=4)

    def test_calibrated_spec_predicts_other_pullbacks(self) -> None:
        true_spec = _spec(efficiency=0.4)
        measured = pm.predicted_range_m(true_spec, 0.020)
        cal = pm.calibrate_from_shot(_spec(efficiency=0.6), 20.0, measured)
        # Predictions at the other pullbacks match the truth within tight tol.
        for x in (0.010, 0.030):
            self.assertAlmostEqual(
                pm.predicted_range_m(cal, x), pm.predicted_range_m(true_spec, x), places=3
            )

    def test_calibration_out_of_band_raises(self) -> None:
        # An absurdly long measured range forces efficiency > 1 → flagged.
        with self.assertRaises(ValueError):
            pm.calibrate_from_shot(_spec(), 20.0, 1000.0)


class TestValidation(unittest.TestCase):
    def test_invalid_spring_constant(self) -> None:
        with self.assertRaises(ValueError):
            _spec(spring_k_n_per_m=0.0).validated()

    def test_invalid_dart_mass(self) -> None:
        with self.assertRaises(ValueError):
            _spec(dart_mass_kg=-1.0).validated()

    def test_efficiency_bounds(self) -> None:
        with self.assertRaises(ValueError):
            _spec(efficiency=0.0).validated()
        with self.assertRaises(ValueError):
            _spec(efficiency=1.5).validated()
        # Boundary 1.0 is allowed.
        self.assertEqual(_spec(efficiency=1.0).validated().efficiency, 1.0)


if __name__ == "__main__":
    unittest.main()
