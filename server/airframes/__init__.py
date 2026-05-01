"""Drone airframe specifications.

Public surface: a small typed vocabulary for describing a drone's
physical airframe in one place, so the SDF, URDF, and PX4 airframe
params are all derived from the same source of truth.

Why this exists
---------------

Before this package, building a custom drone for PX4 SITL meant juggling
three loosely-coupled inputs:

1. A FreeCAD ``Mechanism`` (for kinematics)
2. A ``drone_config`` dict (for rotor positions + spin direction)
3. The mass on each ``SimPart`` (for SDF inertia)

Forgetting to register a structural body (battery, payload, arms) into
the mechanism silently dropped its mass from the chassis inertia;
mismatched rotor positions between SDF and ``CA_ROTORn_PX/PY`` produced
divergent flight; the hover throttle was computed with a linear formula
that contradicted the simulator's quadratic motor model.

``AirframeSpec`` collapses those three inputs into one typed dataclass,
makes physically-incorrect inputs unrepresentable (a rotor without a
``radius_m`` fails type-checking; a structural body without a shape
can't be aggregated), and routes every downstream artifact through a
single ``to_sim_model`` / ``to_px4_airframe_params`` pair.

Design
------

``AirframeSpec`` is a ``Protocol`` so concrete frame types
(:class:`MulticopterAirframe`, :class:`FixedWingAirframe`,
:class:`VTOLAirframe`) can have their own physics methods (e.g.
multicopter ``hover_throttle``, fixed-wing ``trim_throttle``) without
forcing one super-dataclass to know about all frame types.

Only :class:`MulticopterAirframe` has a real implementation today.
Fixed-wing and VTOL ship as dataclass shells so the architecture is
ready for them; their ``to_sim_model`` raises ``NotImplementedError``.

Examples
--------

The legacy ``mechanism + drone_config`` API still works for the
hexapod, planetary gearbox, and any non-drone use of
``cad.export_sim_package``; ``AirframeSpec`` is purely additive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, Union, runtime_checkable

if TYPE_CHECKING:
    # Avoid import cycle: sim_export imports from airframes/multicopter.py
    # only at runtime, not at module load.
    from server.sim_export import SimModel
    from server.px4_airframe_generator import AirframeParams


# ---------------------------------------------------------------------------
# Shape primitives — describe the geometric form of a body for inertia +
# collision generation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    """Rectangular cuboid."""
    size_m: tuple[float, float, float]   # (dx, dy, dz)


@dataclass(frozen=True, slots=True)
class Cylinder:
    """Solid cylinder along ``axis``."""
    radius_m: float
    length_m: float
    axis: Literal["x", "y", "z"] = "z"


@dataclass(frozen=True, slots=True)
class Disk:
    """Thin disk (a special case of cylinder for which we use disk inertia)."""
    radius_m: float
    thickness_m: float = 0.005
    axis: Literal["x", "y", "z"] = "z"


@dataclass(frozen=True, slots=True)
class CustomMesh:
    """Externally-supplied mesh.  Inertia must be provided explicitly."""
    mesh_path: str


ShapeSpec = Union[Box, Cylinder, Disk, CustomMesh]


# ---------------------------------------------------------------------------
# Structural body — anything that has mass + a position in the airframe
# but is not its own kinematic link (battery, payload, arm tube, motor mount,
# wiring harness, ...).  These get aggregated INTO the chassis link's
# inertia via the parallel-axis theorem.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructuralBody:
    """A non-actuated mass element on the chassis.

    Used to compute a physically-correct inertia tensor for the chassis
    link.  ``com_offset_m`` is measured from the chassis frame origin
    (typically the geometric centre of the FreeCAD ``FrameCenter`` body).
    """
    name: str
    mass_kg: float
    shape: ShapeSpec
    com_offset_m: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:  # type: ignore[misc]
        if self.mass_kg <= 0.0:
            raise ValueError(
                f"StructuralBody '{self.name}': mass_kg must be > 0, "
                f"got {self.mass_kg}"
            )


# ---------------------------------------------------------------------------
# Sensor pack — which Gazebo sensors to attach to the chassis link.
# ---------------------------------------------------------------------------


class SensorPack(Enum):
    """Pre-configured sensor bundles for common PX4 SITL setups."""

    PX4_DEFAULT = "px4_default"
    """IMU + GPS (NAVSAT) + barometer + magnetometer.  Matches x500."""

    NONE = "none"
    """No sensors attached.  Useful for unit tests."""


# ---------------------------------------------------------------------------
# Top-level Protocol — every airframe type must satisfy this.
# ---------------------------------------------------------------------------


@runtime_checkable
class AirframeSpec(Protocol):
    """Common interface for all airframe types.

    Concrete implementations: :class:`MulticopterAirframe` (full),
    :class:`FixedWingAirframe` (stub), :class:`VTOLAirframe` (stub).
    """

    name: str
    chassis: StructuralBody
    structural_bodies: tuple[StructuralBody, ...]
    sensors: SensorPack
    ground_clearance_m: float

    def total_mass_kg(self) -> float:
        """Sum of chassis + every structural body + every actuated link."""
        ...

    def to_sim_model(self) -> "SimModel":
        """Build the format-agnostic kinematic + inertial description."""
        ...

    def to_px4_airframe_params(self) -> "AirframeParams":
        """Build the PX4 airframe init script parameters."""
        ...

    def ca_airframe_id(self) -> int:
        """PX4's ``CA_AIRFRAME`` enum (0=multi, 3=plane, 11=VTOL)."""
        ...


# Re-exports for convenience
__all__ = [
    "AirframeSpec",
    "StructuralBody",
    "ShapeSpec",
    "Box",
    "Cylinder",
    "Disk",
    "CustomMesh",
    "SensorPack",
]
