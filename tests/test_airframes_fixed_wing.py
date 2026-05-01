"""Sanity tests for the FixedWingAirframe stub.

The implementation is intentionally absent in this PR — these tests
just lock in the dataclass shape (so the multi-frame-type architecture
holds) and confirm the explicit ``NotImplementedError`` keeps callers
honest.
"""
from __future__ import annotations

import unittest

from server.airframes import Box, StructuralBody
from server.airframes.fixed_wing import (
    ControlSurface,
    FixedWingAirframe,
    Motor,
    Wing,
)


class TestFixedWingStub(unittest.TestCase):
    def _make(self) -> FixedWingAirframe:
        chassis = StructuralBody("fuselage", 1.5, Box((0.6, 0.10, 0.10)))
        return FixedWingAirframe(
            name="cessna_lite",
            chassis=chassis,
            motor=Motor(
                name="prop",
                position_m=(0.30, 0.0, 0.0),
            ),
            wing=Wing(span_m=1.0, chord_m=0.20, area_m2=0.20),
            control_surfaces=(
                ControlSurface(
                    name="aileron_l",
                    surface_type="aileron",
                    hinge_position_m=(0.0, -0.40, 0.0),
                    hinge_axis=(1.0, 0.0, 0.0),
                ),
                ControlSurface(
                    name="elevator",
                    surface_type="elevator",
                    hinge_position_m=(-0.50, 0.0, 0.0),
                    hinge_axis=(0.0, 1.0, 0.0),
                ),
            ),
        )

    def test_construction_and_total_mass(self) -> None:
        af = self._make()
        # Just chassis + structural; no rotors on a fixed-wing.
        self.assertAlmostEqual(af.total_mass_kg(), 1.5, places=4)
        self.assertEqual(af.ca_airframe_id(), 3)   # PX4 plane

    def test_to_sim_model_raises_not_implemented(self) -> None:
        af = self._make()
        with self.assertRaises(NotImplementedError):
            af.to_sim_model()

    def test_to_px4_airframe_params_raises_not_implemented(self) -> None:
        af = self._make()
        with self.assertRaises(NotImplementedError):
            af.to_px4_airframe_params()


if __name__ == "__main__":
    unittest.main()
