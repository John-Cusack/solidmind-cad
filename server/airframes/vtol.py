"""VTOLAirframe — STUB.

VTOL is a hybrid: the airframe has both a multicopter rotor set
(for hover) and a fixed-wing forward-thrust motor + control surfaces
(for cruise).  PX4 handles the transition mode internally; the
airframe spec just has to declare both halves.

Not implemented in this PR — the dataclass exists so callers can
type-check against it.  When VTOL comes online, fill in:

- :meth:`VTOLAirframe.to_sim_model` — combined kinematics (rotors +
  motor + control-surface joints).
- :meth:`VTOLAirframe.to_px4_airframe_params` — ``CA_AIRFRAME``
  selects between standard VTOL (11), tailsitter (10), tilt-rotor
  (12), etc. depending on geometry.  Hover/trim throttles both come
  out of the spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.airframes import SensorPack, StructuralBody
from server.airframes.fixed_wing import ControlSurface, Motor, Wing
from server.airframes.multicopter import Rotor

if TYPE_CHECKING:
    from server.px4_airframe_generator import AirframeParams
    from server.sim_export import SimModel


@dataclass(frozen=True, slots=True)
class VTOLAirframe:
    """Stub spec for a VTOL drone.  Not implemented in this PR."""

    name: str
    chassis: StructuralBody
    rotors: tuple[Rotor, ...]                          # multicopter rotor set
    forward_motor: Motor                                # fixed-wing thrust
    wing: Wing
    control_surfaces: tuple[ControlSurface, ...] = ()
    structural_bodies: tuple[StructuralBody, ...] = ()
    sensors: SensorPack = SensorPack.PX4_DEFAULT
    ground_clearance_m: float = 0.10

    def total_mass_kg(self) -> float:
        return (
            self.chassis.mass_kg
            + sum(b.mass_kg for b in self.structural_bodies)
            + sum(r.mass_kg for r in self.rotors)
        )

    def to_sim_model(self) -> SimModel:
        raise NotImplementedError(
            "VTOLAirframe.to_sim_model is a stub. Will combine "
            "multicopter rotors with fixed-wing motor + control "
            "surfaces under one chassis."
        )

    def to_px4_airframe_params(self) -> AirframeParams:
        raise NotImplementedError(
            "VTOLAirframe.to_px4_airframe_params is a stub. CA_AIRFRAME "
            "depends on geometry: 10=tailsitter, 11=standard VTOL, "
            "12=tiltrotor."
        )

    def ca_airframe_id(self) -> int:
        return 11   # PX4: standard VTOL


__all__ = ["VTOLAirframe"]
