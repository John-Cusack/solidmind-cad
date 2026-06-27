"""Sanity tests for the VTOLAirframe stub.

Like the fixed-wing stub, this just locks in the dataclass shape so
the multi-frame-type architecture is ready when VTOL implementation
lands.
"""

from __future__ import annotations

import unittest

from server.airframes import Box, StructuralBody
from server.airframes.fixed_wing import Motor, Wing
from server.airframes.multicopter import Rotor
from server.airframes.vtol import VTOLAirframe


class TestVTOLStub(unittest.TestCase):
    def _make(self) -> VTOLAirframe:
        chassis = StructuralBody("frame", 2.0, Box((0.5, 0.5, 0.10)))
        return VTOLAirframe(
            name="standard_vtol",
            chassis=chassis,
            rotors=(
                Rotor(name="r0", position_m=(+0.20, +0.20, 0.05), direction="ccw"),
                Rotor(name="r1", position_m=(+0.20, -0.20, 0.05), direction="cw"),
                Rotor(name="r2", position_m=(-0.20, -0.20, 0.05), direction="ccw"),
                Rotor(name="r3", position_m=(-0.20, +0.20, 0.05), direction="cw"),
            ),
            forward_motor=Motor(
                name="forward_motor",
                position_m=(0.30, 0.0, 0.0),
            ),
            wing=Wing(span_m=1.5, chord_m=0.25, area_m2=0.375),
        )

    def test_construction_and_total_mass(self) -> None:
        af = self._make()
        # 2.0 chassis + 4 × 0.016 rotors = 2.064 kg
        self.assertAlmostEqual(af.total_mass_kg(), 2.064, places=4)
        self.assertEqual(af.ca_airframe_id(), 11)  # PX4 standard VTOL

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
