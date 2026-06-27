"""MulticopterAirframe — concrete drone spec for X-quad / hex / oct rotors.

This is the only airframe type with a full implementation in the
current PR.  Fixed-wing and VTOL stubs sit alongside this file so the
architecture is locked in for future expansion.

The concrete spec is responsible for:

1. Assembling its own ``SimModel`` (no detour through the legacy
   ``mechanism + manifest`` path) — chassis link + one rotor link per
   :class:`Rotor`, joined by continuous joints along Z.
2. Computing the chassis link's inertia by aggregating itself plus
   every :class:`~server.airframes.StructuralBody` via the
   parallel-axis theorem (``server.inertia.aggregate``).
3. Producing PX4 ``AirframeParams`` with a quadratic-correct hover
   throttle.

Any test that constructs a :class:`MulticopterAirframe` literal can
assert the exact ``SimModel`` and ``AirframeParams`` it produces — no
intermediate FreeCAD documents required.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from server.airframes import (
    Cylinder,
    Disk,
    SensorPack,
    StructuralBody,
)
from server.inertia import (
    Inertia6,
    InertiaContribution,
    aggregate,
    box_inertia,
    cylinder_inertia,
    thin_disk_inertia,
)

if TYPE_CHECKING:
    from server.px4_airframe_generator import AirframeParams
    from server.sim_export import SimModel

_GRAVITY_MS2 = 9.81

# Defaults that match Gazebo's MulticopterMotorModel canonical example
# and the x500 reference SDF.  Override per-rotor if your motor has
# different specs.
_DEFAULT_MOTOR_CONSTANT = 8.54858e-06     # N·s² (T = K·ω²)
_DEFAULT_MOMENT_CONSTANT = 0.016          # τ_yaw / T  (matches x500 SDF)
_DEFAULT_MAX_ROT_VELOCITY = 1000.0        # rad/s
_DEFAULT_MIN_ROT_VELOCITY = 150.0         # SIM_GZ_EC_MIN; idle/spin-up


@dataclass(frozen=True, slots=True)
class Rotor:
    """One rotor on a multicopter.

    Position is in chassis-frame metres.  Direction selects the spin
    sense seen from above (``ccw`` makes the body yaw clockwise via
    Newton's third law).  Radius + thickness are used for both visual
    sizing and the thin-disk inertia of the rotor link.

    The ``body_already_placed`` flag tells the SDF generator that this
    rotor's mesh (if any) is already in body-local coordinates; the
    legacy auto-yaw heuristic in ``sim_export`` should NOT re-rotate
    it.  Default ``True`` because every drone built through this spec
    explicitly places its rotor bodies.
    """

    name: str
    position_m: tuple[float, float, float]
    direction: Literal["ccw", "cw"]
    motor_constant: float = _DEFAULT_MOTOR_CONSTANT
    moment_constant: float = _DEFAULT_MOMENT_CONSTANT
    max_rot_velocity_rad_s: float = _DEFAULT_MAX_ROT_VELOCITY
    radius_m: float = 0.10
    thickness_m: float = 0.005
    mass_kg: float = 0.016
    body_already_placed: bool = True
    mesh_path: str | None = None

    def __post_init__(self) -> None:  # type: ignore[misc]
        if self.direction not in ("ccw", "cw"):
            raise ValueError(
                f"Rotor '{self.name}': direction must be 'ccw' or 'cw', "
                f"got {self.direction!r}"
            )
        if self.radius_m <= 0:
            raise ValueError(
                f"Rotor '{self.name}': radius_m must be > 0, "
                f"got {self.radius_m}"
            )
        if self.mass_kg <= 0:
            raise ValueError(
                f"Rotor '{self.name}': mass_kg must be > 0, "
                f"got {self.mass_kg}"
            )

    @property
    def direction_sign(self) -> int:
        """+1 for CCW, −1 for CW.  Used for ``CA_ROTORn_KM`` sign."""
        return 1 if self.direction == "ccw" else -1

    def disk_inertia(self) -> Inertia6:
        """Thin-disk inertia about the rotor's spin axis (link Z)."""
        return thin_disk_inertia(
            mass_kg=self.mass_kg,
            radius_m=self.radius_m,
            thickness_m=self.thickness_m,
            axis="z",
        )


@dataclass(frozen=True, slots=True)
class MulticopterAirframe:
    """A multicopter (X-quad, hex, oct) airframe.

    Two physical concepts that older code conflated:

    - **Chassis link**: the rigid body that all rotors are joined to.
      Holds the dominant mass.  Its inertia is the parallel-axis
      aggregate of itself + every :attr:`structural_bodies` member.
    - **Structural bodies**: battery, payload, arm tubes, motor mounts,
      wiring — anything mounted to the chassis but not actuated.  They
      contribute mass and inertia to the chassis link, NOT their own
      kinematic link.

    Why bother distinguishing?  Because the chassis link's inertia
    drives PX4's attitude controller bandwidth.  A 1.7 kg drone whose
    chassis link reports the inertia of a 0.3 kg plate (because we
    forgot the battery + payload + arms) flies divergently.
    """

    name: str
    chassis: StructuralBody
    rotors: tuple[Rotor, ...]
    structural_bodies: tuple[StructuralBody, ...] = ()
    sensors: SensorPack = SensorPack.PX4_DEFAULT
    ground_clearance_m: float = 0.10
    rotor_min_velocity_rad_s: float = _DEFAULT_MIN_ROT_VELOCITY

    def __post_init__(self) -> None:  # type: ignore[misc]
        if not self.rotors:
            raise ValueError(f"MulticopterAirframe '{self.name}': rotors must be non-empty")
        if len(self.rotors) % 2 != 0 and len(self.rotors) < 3:
            raise ValueError(
                f"MulticopterAirframe '{self.name}': need ≥ 3 rotors, "
                f"got {len(self.rotors)}"
            )
        # Yaw-balance check: signed moments must sum to zero for a
        # symmetric multirotor.  Asymmetric configs (e.g. tricopters
        # with a tail servo) need explicit operator override.
        signed_moments = sum(r.direction_sign * r.moment_constant for r in self.rotors)
        if abs(signed_moments) > 1e-6:
            # Only warn; some valid configurations (3-rotor) are asymmetric.
            # Loud enough to surface during inspection but not fatal.
            import warnings
            warnings.warn(
                f"MulticopterAirframe '{self.name}': rotor turning directions "
                f"don't balance yaw (Σ KM = {signed_moments:.3f}). Verify "
                "this is intentional (e.g. tricopter).",
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Mass + inertia
    # ------------------------------------------------------------------

    def total_mass_kg(self) -> float:
        """Sum of chassis + structural bodies + rotors."""
        return (
            self.chassis.mass_kg
            + sum(b.mass_kg for b in self.structural_bodies)
            + sum(r.mass_kg for r in self.rotors)
        )

    def chassis_inertia(self) -> tuple[float, tuple[float, float, float], Inertia6]:
        """Aggregate chassis + structural bodies via parallel-axis theorem.

        Returns ``(combined_mass_kg, com_offset_m, inertia)`` about the
        combined COM.  Rotors are NOT included — they're separate
        kinematic links with their own inertia.
        """
        contribs = [
            InertiaContribution(
                mass_kg=self.chassis.mass_kg,
                com_offset_m=self.chassis.com_offset_m,
                body_local=_inertia_for_shape(self.chassis),
            ),
        ]
        for body in self.structural_bodies:
            contribs.append(InertiaContribution(
                mass_kg=body.mass_kg,
                com_offset_m=body.com_offset_m,
                body_local=_inertia_for_shape(body),
            ))
        return aggregate(contribs)

    # ------------------------------------------------------------------
    # PX4 hover throttle — quadratic-correct
    # ------------------------------------------------------------------

    def hover_throttle(self) -> float:
        """Throttle command (0..1) at which total thrust balances gravity.

        Gazebo's ``MulticopterMotorModel`` plugin uses ``T = K · ω²``
        where ``K`` is ``motorConstant`` and ``ω`` is rotor angular
        velocity.  PX4 maps throttle linearly to ω via the
        ``SIM_GZ_EC_MIN`` / ``SIM_GZ_EC_MAX`` deadband:

            ω(throttle) = ω_min + throttle × (ω_max − ω_min)

        Solving for the throttle that hovers a drone of total mass ``m``
        with ``n`` rotors of identical ``K`` and ``ω_max``:

            ω_hover = sqrt(m · g / (n · K))
            throttle_hover = (ω_hover − ω_min) / (ω_max − ω_min)

        The legacy formula in ``px4_airframe_generator.compute_hover_throttle``
        used ``m·g / (n·K·ω_max²)`` — that's the *square* of the
        correct value when ω_min = 0, and worse when ω_min is non-zero.
        For a 1.7 kg drone with stock motor constants it gave 0.488
        when the actual hover is 0.61, leaving PX4 perpetually under-
        throttled during AUTO_TAKEOFF.
        """
        if not self.rotors:
            raise ValueError(f"{self.name}: no rotors, cannot compute hover throttle")
        # Use first rotor's spec (multicopters have identical rotors)
        K = self.rotors[0].motor_constant
        omega_max = self.rotors[0].max_rot_velocity_rad_s
        omega_min = self.rotor_min_velocity_rad_s
        n = len(self.rotors)
        omega_hover_sq = self.total_mass_kg() * _GRAVITY_MS2 / (n * K)
        if omega_hover_sq < 0:
            raise ValueError(f"{self.name}: negative hover ω² (mass or K negative?)")
        omega_hover = math.sqrt(omega_hover_sq)
        if omega_hover > omega_max:
            raise ValueError(
                f"{self.name}: drone too heavy — needs ω={omega_hover:.0f} rad/s "
                f"to hover, but rotor max is {omega_max:.0f}. Reduce mass or "
                "increase motor_constant / max_rot_velocity."
            )
        if omega_hover < omega_min:
            raise ValueError(
                f"{self.name}: drone too light — needs ω={omega_hover:.0f} rad/s "
                f"to hover, but rotor min is {omega_min:.0f}. PX4 cannot command "
                "below the minimum spin rate."
            )
        return (omega_hover - omega_min) / (omega_max - omega_min)

    def arm_length_m(self) -> float:
        """Mean radial distance from chassis origin to each rotor centre."""
        if not self.rotors:
            return 0.0
        radii = [
            math.hypot(r.position_m[0], r.position_m[1])
            for r in self.rotors
        ]
        return sum(radii) / len(radii)

    # ------------------------------------------------------------------
    # SimModel construction (drives URDF + SDF)
    # ------------------------------------------------------------------

    def to_sim_model(self) -> SimModel:
        """Build the format-agnostic kinematic + inertial description.

        Imported lazily to avoid the import cycle between this module
        and ``server.sim_export``.
        """
        from server.sim_export import CollisionShape, SimJoint, SimLink, SimModel

        # Chassis link: aggregated mass + inertia, primitive box collision.
        chassis_mass, chassis_com, chassis_inertia = self.chassis_inertia()
        chassis_collision: CollisionShape | None = None
        if hasattr(self.chassis.shape, "size_m"):
            chassis_collision = CollisionShape(
                kind="box",
                size_m=self.chassis.shape.size_m,
            )
        elif hasattr(self.chassis.shape, "radius_m"):
            chassis_collision = CollisionShape(
                kind="cylinder",
                radius_m=self.chassis.shape.radius_m,
                length_m=getattr(self.chassis.shape, "length_m",
                                 getattr(self.chassis.shape, "thickness_m", 0.01)),
            )

        # SimLink positions are in mm (FreeCAD convention).  The chassis
        # is placed at the world origin + the COM offset that fell out
        # of aggregation.
        # Link must be named "base_link" — PX4's gz_bridge subscribes to
        # /world/.../model/<model>/link/base_link/sensor/... topics with
        # that name hard-coded (see GZBridge.cpp:224). Renaming the
        # chassis link to anything else means the IMU/baro/mag/GPS topics
        # are silently never bridged into PX4 and pre-flight checks fail
        # with "Accel/Gyro/barometer/compass missing".
        chassis_link = SimLink(
            name="base_link",
            position=(chassis_com[0] * 1000.0, chassis_com[1] * 1000.0, chassis_com[2] * 1000.0),
            mass_kg=chassis_mass,
            inertia=chassis_inertia.as_tuple(),
            is_root=True,
            collision_shape=chassis_collision,
        )

        # Rotor links: thin-disk inertia + cylinder collision.
        # ``Rotor.position_m`` is in Gazebo's FLU model frame (X forward,
        # Y left, Z up) — the intuitive right-handed convention.  SDF
        # uses this frame directly, so we emit positions as-is.  The
        # CA_ROTOR params (PX4 FRD body frame) are negated in Y/Z by
        # ``to_px4_airframe_params``.
        rotor_links = []
        rotor_joints = []
        for rotor in self.rotors:
            rotor_link = SimLink(
                name=rotor.name,
                mesh_path=rotor.mesh_path,
                position=(
                    rotor.position_m[0] * 1000.0,
                    rotor.position_m[1] * 1000.0,
                    rotor.position_m[2] * 1000.0,
                ),
                mass_kg=rotor.mass_kg,
                inertia=rotor.disk_inertia().as_tuple(),
                # Rotor collision = thin cylinder, smaller than the visual
                # so adjacent rotors / chassis can't accidentally inter-
                # penetrate (which crashes Gazebo's DART/ODE physics).
                collision_shape=CollisionShape(
                    kind="cylinder",
                    radius_m=rotor.radius_m * 0.5,
                    length_m=rotor.thickness_m,
                ),
            )
            rotor_links.append(rotor_link)
            rotor_joints.append(SimJoint(
                name=f"{rotor.name}_joint",
                joint_type="continuous",
                parent=chassis_link.name,
                child=rotor.name,
                axis=(0.0, 0.0, 1.0),
                origin_xyz=rotor.position_m,
            ))

        return SimModel(
            name=self.name,
            links=(chassis_link, *rotor_links),
            joints=tuple(rotor_joints),
        )

    # ------------------------------------------------------------------
    # PX4 airframe params
    # ------------------------------------------------------------------

    def to_px4_airframe_params(self) -> AirframeParams:
        """Build the params bundle for ``format_airframe_init_script``."""
        from server.px4_airframe_generator import (
            AirframeParams,
            RotorParams,
            compute_sys_autostart,
            seed_pid_gains,
        )

        # Use sanitized name with the "gz_" prefix that PX4's CMakeLists
        # glob requires (init.d-posix/airframes/*_gz_*).  Without the
        # prefix the gz_<model> make target won't exist.
        ctor_name = self.name if self.name.startswith("gz_") else f"gz_{self.name}"

        # Convert rotor positions from Gazebo FLU (the SDF frame, where Y
        # is left and Z is up) to PX4's FRD body frame (Y right, Z down)
        # by negating Y and Z. The physical SDF position and the
        # CA_ROTOR moment-arm both refer to the same rotor; only the
        # axis convention differs.  The legacy drone_config path
        # (server.px4_airframe_generator.extract_rotors) applies the
        # same conversion — keep them in sync if you change one.
        rotors = tuple(
            RotorParams(
                px_m=r.position_m[0],
                py_m=-r.position_m[1],
                pz_m=-r.position_m[2],
                direction=r.direction_sign,
                moment_constant=r.moment_constant,
                motor_constant=r.motor_constant,
                max_rot_velocity=r.max_rot_velocity_rad_s,
            )
            for r in self.rotors
        )

        mass_kg = self.total_mass_kg()
        arm_m = self.arm_length_m()
        hover = self.hover_throttle()
        pid = seed_pid_gains(mass_kg=mass_kg, arm_length_m=arm_m, rotor_count=len(rotors))

        return AirframeParams(
            name=ctor_name,
            sys_autostart=compute_sys_autostart(ctor_name),
            rotors=rotors,
            mass_kg=mass_kg,
            arm_length_m=arm_m,
            hover_throttle=hover,
            motor_min=int(self.rotor_min_velocity_rad_s),
            motor_max=int(self.rotors[0].max_rot_velocity_rad_s),
            mc_rollrate_p=pid["mc_rollrate_p"],
            mc_pitchrate_p=pid["mc_pitchrate_p"],
            mc_yawrate_p=pid["mc_yawrate_p"],
        )

    # ------------------------------------------------------------------
    # CA_AIRFRAME selector
    # ------------------------------------------------------------------

    def ca_airframe_id(self) -> int:
        """``CA_AIRFRAME`` enum: 0 = generic multirotor."""
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inertia_for_shape(body: StructuralBody) -> Inertia6:
    """Pick the right inertia formula for a structural body's shape."""
    shape = body.shape
    if hasattr(shape, "size_m"):              # Box
        dx, dy, dz = shape.size_m
        return box_inertia(body.mass_kg, dx, dy, dz)
    if isinstance(shape, Disk):
        return thin_disk_inertia(
            body.mass_kg, shape.radius_m, shape.thickness_m, axis=shape.axis,
        )
    if isinstance(shape, Cylinder):
        return cylinder_inertia(
            body.mass_kg, shape.radius_m, shape.length_m, axis=shape.axis,
        )
    # CustomMesh — no closed-form inertia; the user must provide it
    # via a separate hook (TODO: add a ``mass_inertia`` field on
    # StructuralBody to take an explicit Inertia6).  For now, fall
    # back to a unit sphere approximation so SDF emission doesn't
    # crash.
    if hasattr(shape, "mesh_path"):
        # 2/5·m·r² for r ≈ 1 cm.  Rough but never zero.
        m = body.mass_kg
        r = 0.01
        i = 0.4 * m * r * r
        return Inertia6(i, 0.0, 0.0, i, 0.0, i)
    raise TypeError(f"Unknown shape: {type(shape).__name__}")


__all__ = ["Rotor", "MulticopterAirframe"]
