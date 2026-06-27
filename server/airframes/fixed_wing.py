"""FixedWingAirframe — STUB.

This module locks in the dataclass shape for fixed-wing drones so the
multi-frame-type architecture doesn't have to be re-litigated when
the time comes to actually implement plane SITL.

The stubs here construct successfully (so type-checking and tests for
the shared :class:`~server.airframes.AirframeSpec` Protocol pass) but
``to_sim_model`` and ``to_px4_airframe_params`` raise
``NotImplementedError`` — they're explicitly out-of-scope for the
current PR.

When fixed-wing comes online, fill in:

- :meth:`FixedWingAirframe.to_sim_model` — single ground link with
  control-surface revolute joints (ailerons, elevator, rudder), one
  motor link with a continuous joint along ``+X``.
- :meth:`FixedWingAirframe.to_px4_airframe_params` — uses
  ``CA_AIRFRAME = 3`` and a different PID gain set (``FW_*RATE_P``
  rather than ``MC_*RATE_P``).
- :meth:`FixedWingAirframe.trim_throttle` — fixed-wing analogue of
  multicopter's ``hover_throttle``; depends on wing area, drag
  coefficient, and target cruise speed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from server.airframes import SensorPack, StructuralBody

if TYPE_CHECKING:
    from server.px4_airframe_generator import AirframeParams
    from server.sim_export import SimModel


@dataclass(frozen=True, slots=True)
class Motor:
    """The single thrust motor on a fixed-wing aircraft.

    Position is in chassis frame.  Direction is the propeller axis,
    typically ``+X`` (pulling forward).
    """
    name: str
    position_m: tuple[float, float, float]
    direction_axis: tuple[float, float, float] = (1.0, 0.0, 0.0)
    motor_constant: float = 8.54858e-06
    max_rot_velocity_rad_s: float = 1500.0


@dataclass(frozen=True, slots=True)
class ControlSurface:
    """A movable aerodynamic surface (aileron, elevator, rudder, flap)."""
    name: str
    surface_type: Literal["aileron", "elevator", "rudder", "flap"]
    hinge_position_m: tuple[float, float, float]
    hinge_axis: tuple[float, float, float]
    deflection_limits_rad: tuple[float, float] = (-0.524, 0.524)   # ±30°


@dataclass(frozen=True, slots=True)
class Wing:
    """Aerodynamic surface used for trim-throttle and lift calculations."""
    span_m: float
    chord_m: float
    area_m2: float
    cl_max: float = 1.2
    cd_zero: float = 0.03


@dataclass(frozen=True, slots=True)
class FixedWingAirframe:
    """Stub spec for a fixed-wing drone.  Not implemented in this PR."""

    name: str
    chassis: StructuralBody
    motor: Motor
    wing: Wing
    control_surfaces: tuple[ControlSurface, ...] = ()
    structural_bodies: tuple[StructuralBody, ...] = ()
    sensors: SensorPack = SensorPack.PX4_DEFAULT
    ground_clearance_m: float = 0.10

    def total_mass_kg(self) -> float:
        return self.chassis.mass_kg + sum(b.mass_kg for b in self.structural_bodies)

    def to_sim_model(self) -> SimModel:
        raise NotImplementedError(
            "FixedWingAirframe.to_sim_model is a stub. Implement when "
            "fixed-wing SITL is on the roadmap."
        )

    def to_px4_airframe_params(self) -> AirframeParams:
        raise NotImplementedError(
            "FixedWingAirframe.to_px4_airframe_params is a stub. Will "
            "use CA_AIRFRAME=3 (Plane) and FW_*RATE_P gains."
        )

    def ca_airframe_id(self) -> int:
        return 3   # PX4: standard plane


__all__ = ["FixedWingAirframe", "Motor", "ControlSurface", "Wing"]
