"""Shared planetary gear set detection and kinematics.

Extracted from ``simulation_spec_builder`` so both the Chrono planner and
the motion validation / animation pipeline can reuse the same topology
detection logic.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.motion_models import JointType, Mechanism


@dataclass
class PlanetarySet:
    """A detected planetary gear set."""
    carrier: str
    sun: str
    ring: str
    planets: list[str]
    teeth_sun: int
    teeth_ring: int
    teeth_planet: int
    t0: float  # Willis ratio: -z_sun / z_ring


def detect_planetary_sets(mechanism: Mechanism) -> list[PlanetarySet]:
    """Detect planetary gear sets from the mechanism topology.

    Strategy:
    1. Find revolute joints between two non-ground parts → carrier-planet pairs
    2. For each planet, find its gear_mesh neighbors (sun, ring)
    3. Group into PlanetarySet
    """
    part_map = {p.id: p for p in mechanism.parts}
    gear_meshes = [
        j for j in mechanism.joints if j.joint_type == JointType.GEAR_MESH
    ]
    revolute_joints = [
        j for j in mechanism.joints if j.joint_type == JointType.REVOLUTE
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

        # For each planet, find its gear_mesh neighbors (excluding other planets)
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

                # Determine if this is sun or ring
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
            sets.append(PlanetarySet(
                carrier=carrier_id,
                sun=sun_id,
                ring=ring_id,
                planets=planet_ids,
                teeth_sun=teeth_sun,
                teeth_ring=teeth_ring,
                teeth_planet=teeth_planet,
                t0=t0,
            ))
            used_carriers.add(carrier_id)

    return sets
