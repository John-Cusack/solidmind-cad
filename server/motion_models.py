"""Data models for the motion validation pipeline.

Defines the mechanism graph: parts (nodes), joints (edges), drive conditions,
and the top-level Mechanism container.  All dataclasses are frozen with
__slots__ for consistency with the rest of the codebase.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any


class JointType(str, Enum):
    REVOLUTE = "revolute"
    CONTINUOUS = "continuous"
    PRISMATIC = "prismatic"
    GEAR_MESH = "gear_mesh"
    BELT_CHAIN = "belt_chain"
    CAM = "cam"
    FIXED = "fixed"
    PLANAR = "planar"


@dataclass(frozen=True, slots=True)
class PartNode:
    """A rigid body in the mechanism graph."""
    id: str
    body_name: str | None = None
    mesh_path: str | None = None
    mass_kg: float | None = None
    inertia_kg_m2: float | None = None
    is_ground: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "is_ground": self.is_ground}
        if self.body_name is not None:
            d["body_name"] = self.body_name
        if self.mesh_path is not None:
            d["mesh_path"] = self.mesh_path
        if self.mass_kg is not None:
            d["mass_kg"] = self.mass_kg
        if self.inertia_kg_m2 is not None:
            d["inertia_kg_m2"] = self.inertia_kg_m2
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PartNode:
        return cls(
            id=d["id"],
            body_name=d.get("body_name"),
            mesh_path=d.get("mesh_path"),
            mass_kg=d.get("mass_kg"),
            inertia_kg_m2=d.get("inertia_kg_m2"),
            is_ground=d.get("is_ground", False),
        )


@dataclass(frozen=True, slots=True)
class JointEdge:
    """A kinematic constraint between two parts."""
    id: str
    joint_type: JointType
    parent_part: str
    child_part: str
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # Gear/belt parameters
    gear_ratio: float | None = None
    teeth_parent: int | None = None
    teeth_child: int | None = None
    mesh_efficiency: float = 1.0
    internal: bool = False
    # Linkage parameters
    link_length_mm: float | None = None
    # Joint limits
    min_angle_deg: float | None = None
    max_angle_deg: float | None = None
    min_travel_mm: float | None = None
    max_travel_mm: float | None = None
    # Actuator parameters (for URDF export)
    damping: float | None = None
    friction: float | None = None
    effort_nm: float | None = None       # max torque (Nm) or force (N)
    velocity_rad_s: float | None = None  # max velocity (rad/s or m/s)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "joint_type": self.joint_type.value,
            "parent_part": self.parent_part,
            "child_part": self.child_part,
            "axis": list(self.axis),
            "origin": list(self.origin),
            "mesh_efficiency": self.mesh_efficiency,
        }
        if self.gear_ratio is not None:
            d["gear_ratio"] = self.gear_ratio
        if self.teeth_parent is not None:
            d["teeth_parent"] = self.teeth_parent
        if self.teeth_child is not None:
            d["teeth_child"] = self.teeth_child
        if self.internal:
            d["internal"] = True
        if self.link_length_mm is not None:
            d["link_length_mm"] = self.link_length_mm
        if self.min_angle_deg is not None:
            d["min_angle_deg"] = self.min_angle_deg
        if self.max_angle_deg is not None:
            d["max_angle_deg"] = self.max_angle_deg
        if self.min_travel_mm is not None:
            d["min_travel_mm"] = self.min_travel_mm
        if self.max_travel_mm is not None:
            d["max_travel_mm"] = self.max_travel_mm
        if self.damping is not None:
            d["damping"] = self.damping
        if self.friction is not None:
            d["friction"] = self.friction
        if self.effort_nm is not None:
            d["effort_nm"] = self.effort_nm
        if self.velocity_rad_s is not None:
            d["velocity_rad_s"] = self.velocity_rad_s
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JointEdge:
        axis = d.get("axis", [0.0, 0.0, 1.0])
        origin = d.get("origin", [0.0, 0.0, 0.0])
        return cls(
            id=d["id"],
            joint_type=JointType(d["joint_type"]),
            parent_part=d["parent_part"],
            child_part=d["child_part"],
            axis=tuple(axis),
            origin=tuple(origin),
            gear_ratio=d.get("gear_ratio"),
            teeth_parent=d.get("teeth_parent"),
            teeth_child=d.get("teeth_child"),
            mesh_efficiency=d.get("mesh_efficiency", 1.0),
            internal=d.get("internal", False),
            link_length_mm=d.get("link_length_mm"),
            min_angle_deg=d.get("min_angle_deg"),
            max_angle_deg=d.get("max_angle_deg"),
            min_travel_mm=d.get("min_travel_mm"),
            max_travel_mm=d.get("max_travel_mm"),
            damping=d.get("damping"),
            friction=d.get("friction"),
            effort_nm=d.get("effort_nm"),
            velocity_rad_s=d.get("velocity_rad_s"),
        )


@dataclass(frozen=True, slots=True)
class DriveCondition:
    """Input motion/load applied to a joint."""
    joint_id: str
    speed_rpm: float | None = None
    torque_nm: float | None = None
    force_n: float | None = None
    driven_part: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"joint_id": self.joint_id}
        if self.speed_rpm is not None:
            d["speed_rpm"] = self.speed_rpm
        if self.torque_nm is not None:
            d["torque_nm"] = self.torque_nm
        if self.force_n is not None:
            d["force_n"] = self.force_n
        if self.driven_part is not None:
            d["driven_part"] = self.driven_part
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DriveCondition:
        return cls(
            joint_id=d["joint_id"],
            speed_rpm=d.get("speed_rpm"),
            torque_nm=d.get("torque_nm"),
            force_n=d.get("force_n"),
            driven_part=d.get("driven_part"),
        )


@dataclass(frozen=True, slots=True)
class Mechanism:
    """Top-level mechanism definition: a graph of parts + joints + drives."""
    name: str
    parts: tuple[PartNode, ...]
    joints: tuple[JointEdge, ...]
    drives: tuple[DriveCondition, ...]
    expected_outputs: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "parts": [p.to_dict() for p in self.parts],
            "joints": [j.to_dict() for j in self.joints],
            "drives": [d.to_dict() for d in self.drives],
            "expected_outputs": dict(self.expected_outputs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Mechanism:
        return cls(
            name=d["name"],
            parts=tuple(PartNode.from_dict(p) for p in d.get("parts", [])),
            joints=tuple(JointEdge.from_dict(j) for j in d.get("joints", [])),
            drives=tuple(DriveCondition.from_dict(dc) for dc in d.get("drives", [])),
            expected_outputs=d.get("expected_outputs", {}),
        )

    def get_part(self, part_id: str) -> PartNode | None:
        for p in self.parts:
            if p.id == part_id:
                return p
        return None

    def get_joint(self, joint_id: str) -> JointEdge | None:
        for j in self.joints:
            if j.id == joint_id:
                return j
        return None

    def ground_parts(self) -> list[PartNode]:
        return [p for p in self.parts if p.is_ground]

    def moving_parts(self) -> list[PartNode]:
        return [p for p in self.parts if not p.is_ground]

    def joints_for_part(self, part_id: str) -> list[JointEdge]:
        return [
            j for j in self.joints
            if j.parent_part == part_id or j.child_part == part_id
        ]
