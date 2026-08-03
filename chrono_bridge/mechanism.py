"""Contract-side view of a mechanism, for the Chrono bridge.

Core sends a mechanism as plain JSON (``Mechanism.to_dict()``); this module
turns that back into the attribute-shaped objects the spec builder reads, so
the translation code stays readable without importing anything from core.

The duplication of ``JointType`` and the planetary-set detection is deliberate:
the contract is data, not a shared library, and an engine repository has to
stand on its own (architecture doc, Principle 2).  These types describe what
arrives on the wire — they are not core's model classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "AppliedForceView",
    "DriveView",
    "JointType",
    "JointView",
    "MechanismView",
    "PartView",
    "PlanetarySet",
    "as_mechanism",
    "detect_planetary_sets",
]


class JointType(str, Enum):
    """Joint kinds the contract carries.  Values match core's vocabulary."""

    REVOLUTE = "revolute"
    PRISMATIC = "prismatic"
    FIXED = "fixed"
    GEAR_MESH = "gear_mesh"
    BELT_CHAIN = "belt_chain"
    PLANAR = "planar"
    CAM = "cam"
    CONTINUOUS = "continuous"

    @classmethod
    def parse(cls, value: Any) -> JointType:
        """Accept our own members, a wire string, or any enum with a value.

        ``str()`` on a ``(str, Enum)`` member yields ``"JointType.GEAR_MESH"``,
        not the value — so read ``.value`` when it is there.
        """
        if isinstance(value, cls):
            return value
        raw = getattr(value, "value", value)
        try:
            return cls(str(raw))
        except ValueError:
            return cls.FIXED


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass(slots=True)
class PartView:
    id: str
    is_ground: bool = False
    mass_kg: float | None = None
    inertia_kg_m2: float | None = None
    body_name: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PartView:
        return cls(
            id=str(data.get("id", "")),
            is_ground=bool(data.get("is_ground", False)),
            mass_kg=_f(data.get("mass_kg")),
            inertia_kg_m2=_f(data.get("inertia_kg_m2")),
            body_name=data.get("body_name"),
        )


@dataclass(slots=True)
class JointView:
    id: str
    joint_type: JointType
    parent_part: str
    child_part: str
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gear_ratio: float | None = None
    teeth_parent: int | None = None
    teeth_child: int | None = None
    internal: bool = False
    min_travel_mm: float | None = None
    max_travel_mm: float | None = None
    spring_k_n_per_m: float | None = None
    spring_rest_length_m: float | None = None
    spring_damping_n_s_per_m: float | None = None
    spring_preload_n: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JointView:
        axis = data.get("axis") or (0.0, 0.0, 1.0)
        origin = data.get("origin") or (0.0, 0.0, 0.0)
        return cls(
            id=str(data.get("id", "")),
            joint_type=JointType.parse(data.get("joint_type")),
            parent_part=str(data.get("parent_part", "")),
            child_part=str(data.get("child_part", "")),
            axis=(float(axis[0]), float(axis[1]), float(axis[2])),
            origin=(float(origin[0]), float(origin[1]), float(origin[2])),
            gear_ratio=_f(data.get("gear_ratio")),
            teeth_parent=data.get("teeth_parent"),
            teeth_child=data.get("teeth_child"),
            internal=bool(data.get("internal", False)),
            min_travel_mm=_f(data.get("min_travel_mm")),
            max_travel_mm=_f(data.get("max_travel_mm")),
            spring_k_n_per_m=_f(data.get("spring_k_n_per_m")),
            spring_rest_length_m=_f(data.get("spring_rest_length_m")),
            spring_damping_n_s_per_m=_f(data.get("spring_damping_n_s_per_m")),
            spring_preload_n=_f(data.get("spring_preload_n")),
        )


@dataclass(slots=True)
class DriveView:
    joint_id: str
    speed_rpm: float | None = None
    torque_nm: float | None = None
    driven_part: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriveView:
        return cls(
            joint_id=str(data.get("joint_id", "")),
            speed_rpm=_f(data.get("speed_rpm")),
            torque_nm=_f(data.get("torque_nm")),
            driven_part=data.get("driven_part"),
        )


@dataclass(slots=True)
class AppliedForceView:
    target_body: str
    force_vector: tuple[float, float, float] = (0.0, 0.0, 0.0)
    position_local: tuple[float, float, float] = (0.0, 0.0, 0.0)
    frame: str = "world"
    label: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppliedForceView:
        force = data.get("force_vector") or (0.0, 0.0, 0.0)
        position = data.get("position_local") or (0.0, 0.0, 0.0)
        return cls(
            target_body=str(data.get("target_body", "")),
            force_vector=(float(force[0]), float(force[1]), float(force[2])),
            position_local=(float(position[0]), float(position[1]), float(position[2])),
            frame=str(data.get("frame", "world")),
            label=data.get("label"),
        )


@dataclass(slots=True)
class MechanismView:
    name: str = "mechanism"
    parts: tuple[PartView, ...] = ()
    joints: tuple[JointView, ...] = ()
    drives: tuple[DriveView, ...] = ()
    applied_forces: tuple[AppliedForceView, ...] = field(default=())

    def get_joint(self, joint_id: str) -> JointView | None:
        for joint in self.joints:
            if joint.id == joint_id:
                return joint
        return None


def as_mechanism(source: Any) -> Any:
    """Return an attribute-shaped mechanism.

    Dicts (what arrives over the socket) are parsed into views; anything that
    already exposes ``joints`` is passed through untouched, so in-process
    callers can hand over their own model objects.
    """
    if not isinstance(source, dict):
        return source
    return MechanismView(
        name=str(source.get("name", "mechanism")),
        parts=tuple(
            PartView.from_dict(p) for p in source.get("parts") or () if isinstance(p, dict)
        ),
        joints=tuple(
            JointView.from_dict(j) for j in source.get("joints") or () if isinstance(j, dict)
        ),
        drives=tuple(
            DriveView.from_dict(d) for d in source.get("drives") or () if isinstance(d, dict)
        ),
        applied_forces=tuple(
            AppliedForceView.from_dict(f)
            for f in source.get("applied_forces") or ()
            if isinstance(f, dict)
        ),
    )


# ---------------------------------------------------------------------------
# Planetary topology detection
# ---------------------------------------------------------------------------


@dataclass
class PlanetarySet:
    """A detected planetary gear set.

    Ported verbatim from core's topology detection: the contract carries the
    same mechanism data, so the two must agree on what a planetary set is.
    """

    carrier: str
    sun: str
    ring: str
    planets: list[str]
    teeth_sun: int
    teeth_ring: int
    teeth_planet: int
    t0: float  # Willis ratio: -z_sun / z_ring


def detect_planetary_sets(mechanism: Any) -> list[PlanetarySet]:
    """Detect planetary gear sets from the mechanism topology.

    Strategy:
    1. Find revolute joints between two non-ground parts → carrier-planet pairs
    2. For each planet, find its gear_mesh neighbors (sun, ring)
    3. Group into PlanetarySet
    """
    part_map = {p.id: p for p in mechanism.parts}
    gear_meshes = [
        j for j in mechanism.joints if JointType.parse(j.joint_type) == JointType.GEAR_MESH
    ]
    revolute_joints = [
        j for j in mechanism.joints if JointType.parse(j.joint_type) == JointType.REVOLUTE
    ]

    # carrier → [planet_ids] from revolute joints between non-ground parts
    carrier_planets: dict[str, list[str]] = {}
    for rj in revolute_joints:
        parent = part_map.get(rj.parent_part)
        child = part_map.get(rj.child_part)
        if parent is None or child is None:
            continue
        if parent.is_ground or child.is_ground:
            continue
        # Convention: parent is carrier, child is planet
        carrier_planets.setdefault(rj.parent_part, []).append(rj.child_part)

    # For each carrier's planets, find sun and ring via gear meshes
    sets: list[PlanetarySet] = []
    used_carriers: set[str] = set()

    for carrier_id, planet_ids in carrier_planets.items():
        if carrier_id in used_carriers:
            continue

        sun_id: str | None = None
        ring_id: str | None = None
        teeth_sun = 0
        teeth_ring = 0
        teeth_planet = 0

        for planet_id in planet_ids:
            for gm in gear_meshes:
                other: str | None = None
                is_parent_planet = gm.parent_part == planet_id
                is_child_planet = gm.child_part == planet_id

                if is_parent_planet:
                    other = gm.child_part
                elif is_child_planet:
                    other = gm.parent_part
                else:
                    continue

                if other in planet_ids or other == carrier_id:
                    continue

                if gm.internal:
                    ring_id = other
                    if is_parent_planet:
                        teeth_planet = gm.teeth_parent or 0
                        teeth_ring = gm.teeth_child or 0
                    else:
                        teeth_planet = gm.teeth_child or 0
                        teeth_ring = gm.teeth_parent or 0
                else:
                    sun_id = other
                    if is_parent_planet:
                        teeth_planet = gm.teeth_parent or 0
                        teeth_sun = gm.teeth_child or 0
                    else:
                        teeth_planet = gm.teeth_child or 0
                        teeth_sun = gm.teeth_parent or 0

        if sun_id is not None and ring_id is not None and teeth_ring > 0:
            t0 = -teeth_sun / teeth_ring
            sets.append(
                PlanetarySet(
                    carrier=carrier_id,
                    sun=sun_id,
                    ring=ring_id,
                    planets=planet_ids,
                    teeth_sun=teeth_sun,
                    teeth_ring=teeth_ring,
                    teeth_planet=teeth_planet,
                    t0=t0,
                )
            )
            used_carriers.add(carrier_id)

    return sets
