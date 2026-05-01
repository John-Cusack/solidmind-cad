"""Preset airframe factories for tests and example flights.

Each preset returns a fully-populated :class:`MulticopterAirframe`
that can be passed straight to ``to_sim_model`` /
``to_px4_airframe_params``.  Useful for:

- Regression tests that need a known-good drone literal.
- Example scripts that don't want to copy 50 lines of dataclass
  construction.
- Quickly bootstrapping a new drone before tweaking individual
  parameters.

Naming convention:
- ``x500_like()`` — best-effort port of PX4's stock x500 (2 kg, 0.247 m
  arm).  Useful as the default sanity-check spec.
- ``small_quad_3in()`` — a tiny FPV-class quad (~250 g, 3″ props).
- ``cinema_drone()`` — a 6 kg X-quad with camera payload + battery,
  matching the example we just built in the camera_drone walkthrough.
"""
from __future__ import annotations

from server.airframes import Box, Disk, SensorPack, StructuralBody
from server.airframes.multicopter import MulticopterAirframe, Rotor


def x500_like(name: str = "x500_like") -> MulticopterAirframe:
    """Approximate PX4's stock x500.

    Mass and rotor positions match the x500 SDF; inertia falls out of
    aggregating the chassis box.  Useful as the canonical "should
    fly" reference.
    """
    chassis = StructuralBody(
        name="chassis",
        mass_kg=2.0,
        shape=Box(size_m=(0.30, 0.30, 0.10)),
    )
    return MulticopterAirframe(
        name=name,
        chassis=chassis,
        # X-quad in FLU: +Y is left.  Stock PX4 x500 puts rotor 0 at
        # front-right (FRD +X +Y), which is FLU +X -Y.  Diagonal pair
        # rotors 0+1 spin CCW; pair 2+3 spin CW.
        rotors=(
            Rotor(name="rotor_0", position_m=(+0.13, -0.22, 0.06), direction="ccw"),
            Rotor(name="rotor_1", position_m=(-0.13, +0.20, 0.06), direction="ccw"),
            Rotor(name="rotor_2", position_m=(+0.13, +0.22, 0.06), direction="cw"),
            Rotor(name="rotor_3", position_m=(-0.13, -0.20, 0.06), direction="cw"),
        ),
        sensors=SensorPack.PX4_DEFAULT,
    )


def small_quad_3in(name: str = "small_quad") -> MulticopterAirframe:
    """A 3-inch FPV-class quadrotor (~250 g, 0.12 m arm)."""
    chassis = StructuralBody(
        name="chassis",
        mass_kg=0.18,
        shape=Box(size_m=(0.10, 0.10, 0.03)),
    )
    battery = StructuralBody(
        name="battery",
        mass_kg=0.05,
        shape=Box(size_m=(0.05, 0.03, 0.015)),
        com_offset_m=(0.0, 0.0, 0.02),
    )
    return MulticopterAirframe(
        name=name,
        chassis=chassis,
        structural_bodies=(battery,),
        rotors=(
            Rotor(name="rotor_FL", position_m=(+0.085, +0.085, 0.02),
                  direction="ccw", radius_m=0.038, mass_kg=0.005,
                  motor_constant=2.0e-06, max_rot_velocity_rad_s=2200.0),
            Rotor(name="rotor_FR", position_m=(+0.085, -0.085, 0.02),
                  direction="cw", radius_m=0.038, mass_kg=0.005,
                  motor_constant=2.0e-06, max_rot_velocity_rad_s=2200.0),
            Rotor(name="rotor_RR", position_m=(-0.085, -0.085, 0.02),
                  direction="ccw", radius_m=0.038, mass_kg=0.005,
                  motor_constant=2.0e-06, max_rot_velocity_rad_s=2200.0),
            Rotor(name="rotor_RL", position_m=(-0.085, +0.085, 0.02),
                  direction="cw", radius_m=0.038, mass_kg=0.005,
                  motor_constant=2.0e-06, max_rot_velocity_rad_s=2200.0),
        ),
    )


def cinema_drone(name: str = "cinema_drone") -> MulticopterAirframe:
    """A 6 kg cinema-class X-quad with camera payload + battery.

    Matches the airframe we built end-to-end in
    ``examples/quadrotor_camera_drone/run.py``.  Rotor positions are
    at ±0.2475 m corners (700 mm wheelbase X-pattern); chassis aggregates
    the FrameCenter plate plus battery + payload + arms + motor mounts.
    """
    chassis = StructuralBody(
        name="frame_center",
        mass_kg=0.30,
        shape=Box(size_m=(0.20, 0.20, 0.03)),
    )
    battery = StructuralBody(
        name="battery_pack",
        mass_kg=1.50,
        shape=Box(size_m=(0.10, 0.08, 0.04)),
        com_offset_m=(0.0, 0.0, 0.05),
    )
    payload = StructuralBody(
        name="payload_block",
        mass_kg=2.50,
        shape=Box(size_m=(0.10, 0.08, 0.06)),
        com_offset_m=(0.0, 0.0, -0.04),
    )
    arms = StructuralBody(
        name="arms",
        mass_kg=0.40,
        shape=Box(size_m=(0.50, 0.50, 0.018)),
        com_offset_m=(0.0, 0.0, 0.009),
    )
    motor_mounts = StructuralBody(
        name="motor_mounts",
        mass_kg=0.20,
        shape=Box(size_m=(0.495, 0.495, 0.020)),
        com_offset_m=(0.0, 0.0, 0.028),
    )
    wiring = StructuralBody(
        name="wiring_electronics",
        mass_kg=0.60,
        shape=Box(size_m=(0.15, 0.15, 0.02)),
        com_offset_m=(0.0, 0.0, 0.01),
    )

    # Cinema-class motors: T-Motor MN605S 320 Kv-class.  Peak thrust per
    # rotor ~28 N at ω_max = 1800 rad/s with stock motorConstant; gives
    # T/W ≈ 2.0 on 5.7 kg AUW.
    rotor_kwargs = dict(
        radius_m=0.20,
        mass_kg=0.05,
        max_rot_velocity_rad_s=1800.0,
    )
    return MulticopterAirframe(
        name=name,
        chassis=chassis,
        structural_bodies=(battery, payload, arms, motor_mounts, wiring),
        rotors=(
            Rotor(name="rotor_FL", position_m=(+0.2475, +0.2475, 0.038),
                  direction="ccw", **rotor_kwargs),
            Rotor(name="rotor_FR", position_m=(+0.2475, -0.2475, 0.038),
                  direction="cw", **rotor_kwargs),
            Rotor(name="rotor_RR", position_m=(-0.2475, -0.2475, 0.038),
                  direction="ccw", **rotor_kwargs),
            Rotor(name="rotor_RL", position_m=(-0.2475, +0.2475, 0.038),
                  direction="cw", **rotor_kwargs),
        ),
    )


__all__ = ["x500_like", "small_quad_3in", "cinema_drone"]
