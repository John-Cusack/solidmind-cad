"""Build Chrono simulation specs from Mechanism definitions.

This module is the Python "planner" in the Python Planner + C++ Executor
architecture.  It converts a high-level Mechanism (parts, joints, drives)
into a flat list of Chrono objects that the C++ daemon instantiates directly
— no domain logic in C++.

Key design decisions:
- Gears → 1D shaft elements (ChShaft + ChShaftsGear / ChShaftsPlanetary)
  because ChLinkLockGear requires UpdateFlags::VISUAL_ASSETS which is
  stripped during DoStepDynamics() in headless mode.
- Planets are *derived outputs* — their speeds are computed post-simulation
  from sun + carrier speeds using Willis kinematics.
- Bodies + revolute joints are used for non-gear parts (linkages, props).
- ChShaftBodyRotation bridges 1D shafts to 3D bodies when needed.
"""
from __future__ import annotations

import math
from typing import Any

from server.motion_models import JointType, Mechanism
from server.motion_planetary import PlanetarySet, detect_planetary_sets


def build_simulation_spec(mechanism: Mechanism) -> dict[str, Any]:
    """Convert a Mechanism into a simulation spec for the Chrono daemon.

    Returns a dict with:
      - "objects": list of Chrono element dicts to instantiate
      - "derived_outputs": dict of outputs computed post-simulation
    """
    planetary_sets = detect_planetary_sets(mechanism)

    # Collect all part IDs that are handled by planetary/gear shaft elements
    shaft_part_ids: set[str] = set()
    consumed_joints: set[str] = set()

    for ps in planetary_sets:
        shaft_part_ids.update([ps.carrier, ps.sun, ps.ring])
        shaft_part_ids.update(ps.planets)

    objects: list[dict[str, Any]] = []

    # Build gear (shaft-based) objects
    gear_objs, gear_consumed = _build_gear_objects(mechanism, planetary_sets)
    objects.extend(gear_objs)
    consumed_joints.update(gear_consumed)

    # Collect shaft part IDs from simple gear pairs too
    for obj in gear_objs:
        if obj["type"] == "shaft":
            shaft_part_ids.add(obj["id"])

    # Build body objects for non-gear parts
    body_objs, body_consumed = _build_body_objects(
        mechanism, shaft_part_ids, consumed_joints,
    )
    objects.extend(body_objs)
    consumed_joints.update(body_consumed)

    # Build motors
    motor_objs = _build_motor_objects(mechanism, shaft_part_ids)
    objects.extend(motor_objs)

    # Build applied forces (e.g. BEMT distributed loads on a blade body)
    force_objs = _build_applied_force_objects(mechanism, shaft_part_ids)
    objects.extend(force_objs)

    # Build derived outputs for planet speeds
    derived = _build_derived_outputs(planetary_sets)

    return {"objects": objects, "derived_outputs": derived}


def add_derived_speeds(result: dict[str, Any], spec: dict[str, Any]) -> None:
    """Post-process simulation results to compute derived planet speeds.

    Modifies *result* in-place, adding planet entries to summary and
    time_series.
    """
    derived = spec.get("derived_outputs", {})
    if not derived:
        return

    summary = result.get("summary", {})
    ss_speeds = summary.get("steady_state_speeds", {})
    time_series = result.get("time_series", [])

    for planet_id, info in derived.items():
        carrier_id = info["carrier"]
        sun_id = info["sun"]
        teeth_ratio = info["teeth_ratio"]  # z_sun / z_planet

        # Derive steady-state speed
        w_carrier = ss_speeds.get(carrier_id, 0.0)
        w_sun = ss_speeds.get(sun_id, 0.0)
        w_planet = w_carrier - teeth_ratio * (w_sun - w_carrier)
        ss_speeds[planet_id] = round(w_planet, 2)

        # Derive time-series entries
        for step in time_series:
            parts = step.get("parts", {})
            carrier_rpm = parts.get(carrier_id, {}).get("omega_rpm", 0.0)
            sun_rpm = parts.get(sun_id, {}).get("omega_rpm", 0.0)
            planet_rpm = carrier_rpm - teeth_ratio * (sun_rpm - carrier_rpm)

            pos = info.get("position", [0, 0, 0])
            parts[planet_id] = {
                "pos": pos,
                "rot": [1, 0, 0, 0],
                "omega_rpm": round(planet_rpm, 2),
            }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_gear_objects(
    mechanism: Mechanism,
    planetary_sets: list[PlanetarySet],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build shaft-based objects for gears. Returns (objects, consumed_joint_ids)."""
    objects: list[dict[str, Any]] = []
    consumed: set[str] = set()
    created_shafts: set[str] = set()

    part_map = {p.id: p for p in mechanism.parts}

    def _ensure_shaft(part_id: str) -> None:
        if part_id in created_shafts:
            return
        part = part_map.get(part_id)
        if part is None:
            return
        inertia = part.inertia_kg_m2 if part.inertia_kg_m2 else 0.01
        objects.append({
            "type": "shaft",
            "id": part_id,
            "inertia": inertia,
            "fixed": part.is_ground,
        })
        created_shafts.add(part_id)

    # Planetary sets → shafts_planetary
    planetary_parts: set[str] = set()
    for ps in planetary_sets:
        _ensure_shaft(ps.sun)
        _ensure_shaft(ps.carrier)
        _ensure_shaft(ps.ring)
        planetary_parts.update([ps.sun, ps.carrier, ps.ring])
        planetary_parts.update(ps.planets)

        objects.append({
            "type": "shafts_planetary",
            "id": f"planetary_{ps.sun}_{ps.ring}",
            "shaft_sun": ps.sun,
            "shaft_carrier": ps.carrier,
            "shaft_ring": ps.ring,
            "t0": ps.t0,
        })

        # Mark all gear_mesh joints involving planets as consumed
        for j in mechanism.joints:
            if j.joint_type != JointType.GEAR_MESH:
                continue
            if j.parent_part in ps.planets or j.child_part in ps.planets:
                consumed.add(j.id)
        # Mark revolute joints between carrier and planets as consumed
        for j in mechanism.joints:
            if j.joint_type != JointType.REVOLUTE:
                continue
            if j.parent_part == ps.carrier and j.child_part in ps.planets:
                consumed.add(j.id)
            elif j.child_part == ps.carrier and j.parent_part in ps.planets:
                consumed.add(j.id)

    # Remaining gear_mesh joints → shafts_gear (fixed-axis pairs)
    for j in mechanism.joints:
        if j.id in consumed:
            continue
        if j.joint_type != JointType.GEAR_MESH:
            continue

        _ensure_shaft(j.parent_part)
        _ensure_shaft(j.child_part)

        # Compute ratio
        if j.teeth_parent and j.teeth_child:
            if j.internal:
                ratio = j.teeth_parent / j.teeth_child
            else:
                ratio = -(j.teeth_parent / j.teeth_child)
        elif j.gear_ratio:
            ratio = -j.gear_ratio if not j.internal else j.gear_ratio
        else:
            ratio = -1.0

        objects.append({
            "type": "shafts_gear",
            "id": f"gear_{j.id}",
            "shaft_1": j.parent_part,
            "shaft_2": j.child_part,
            "ratio": ratio,
        })
        consumed.add(j.id)

    return objects, consumed


def _build_body_objects(
    mechanism: Mechanism,
    shaft_part_ids: set[str],
    consumed_joints: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build 3D body objects for parts not handled by shaft elements."""
    objects: list[dict[str, Any]] = []
    consumed: set[str] = set()
    part_map = {p.id: p for p in mechanism.parts}
    created_bodies: set[str] = set()

    def _ensure_body(part_id: str) -> None:
        if part_id in created_bodies or part_id in shaft_part_ids:
            return
        part = part_map.get(part_id)
        if part is None:
            return
        mass = part.mass_kg if part.mass_kg else 1.0
        inertia = part.inertia_kg_m2 if part.inertia_kg_m2 else 0.01
        objects.append({
            "type": "body",
            "id": part_id,
            "mass": mass,
            "inertia": inertia,
            "fixed": part.is_ground,
            "pos": [0, 0, 0],
        })
        created_bodies.add(part_id)

    for j in mechanism.joints:
        if j.id in consumed_joints:
            continue
        if j.joint_type in (JointType.GEAR_MESH, JointType.BELT_CHAIN):
            continue  # Already handled

        # Skip joints where either part is a shaft — shafts don't need
        # 3D revolute/prismatic joints; their constraints are 1D.
        if j.parent_part in shaft_part_ids or j.child_part in shaft_part_ids:
            consumed.add(j.id)
            continue

        if j.joint_type == JointType.REVOLUTE:
            _ensure_body(j.parent_part)
            _ensure_body(j.child_part)

            origin = list(j.origin) if j.origin else [0, 0, 0]
            # Convert mm to m for Chrono
            origin_m = [v / 1000.0 for v in origin]

            objects.append({
                "type": "revolute",
                "id": j.id,
                "body_1": j.parent_part,
                "body_2": j.child_part,
                "pos": origin_m,
            })
            consumed.add(j.id)

        elif j.joint_type == JointType.PRISMATIC:
            _ensure_body(j.parent_part)
            _ensure_body(j.child_part)

            origin = list(j.origin) if j.origin else [0, 0, 0]
            origin_m = [v / 1000.0 for v in origin]

            objects.append({
                "type": "prismatic",
                "id": j.id,
                "body_1": j.parent_part,
                "body_2": j.child_part,
                "pos": origin_m,
            })
            # Optional linear spring acting along the prismatic axis. Emitted as
            # a sibling object so the prismatic constraint stays untouched when
            # no spring is present (regression-safe).
            if j.spring_k_n_per_m is not None:
                spring_obj: dict[str, Any] = {
                    "type": "spring",
                    "id": f"{j.id}_spring",
                    "body_1": j.parent_part,
                    "body_2": j.child_part,
                    "k_n_per_m": j.spring_k_n_per_m,
                    "preload_n": j.spring_preload_n,
                    "pos": origin_m,
                    "axis": list(j.axis),
                }
                if j.spring_rest_length_m is not None:
                    spring_obj["rest_length_m"] = j.spring_rest_length_m
                objects.append(spring_obj)
            consumed.add(j.id)

        elif j.joint_type == JointType.FIXED:
            _ensure_body(j.parent_part)
            _ensure_body(j.child_part)

            origin = list(j.origin) if j.origin else [0, 0, 0]
            origin_m = [v / 1000.0 for v in origin]

            objects.append({
                "type": "fixed",
                "id": j.id,
                "body_1": j.parent_part,
                "body_2": j.child_part,
                "pos": origin_m,
            })
            consumed.add(j.id)

    return objects, consumed


def _build_motor_objects(
    mechanism: Mechanism,
    shaft_part_ids: set[str],
) -> list[dict[str, Any]]:
    """Build motor objects from drive conditions.

    Motor target resolution order:
    1. Use ``drive.driven_part`` if explicitly set
    2. Try ``joint.parent_part``
    3. Fall back to ``joint.child_part`` if parent isn't a known shaft/body
    """
    objects: list[dict[str, Any]] = []

    for drive in mechanism.drives:
        joint = mechanism.get_joint(drive.joint_id)
        if joint is None:
            continue

        speed_rpm = drive.speed_rpm
        if speed_rpm is None:
            continue

        # Resolve which part the motor drives
        driven_part = _resolve_driven_part(drive, joint, shaft_part_ids)
        if driven_part is None:
            continue

        if driven_part in shaft_part_ids:
            objects.append({
                "type": "motor_shaft_speed",
                "id": f"{drive.joint_id}_motor",
                "shaft": driven_part,
                "speed_rpm": speed_rpm,
            })
        else:
            objects.append({
                "type": "motor_body_speed",
                "id": f"{drive.joint_id}_motor",
                "body": driven_part,
                "speed_rpm": speed_rpm,
            })

    return objects


def _resolve_driven_part(
    drive: Any,
    joint: Any,
    shaft_part_ids: set[str],
) -> str | None:
    """Resolve the part a motor should drive.

    Priority:
    1. Explicit ``drive.driven_part`` (caller knows best)
    2. ``joint.parent_part`` if it's a known shaft
    3. ``joint.child_part`` as fallback
    4. ``joint.parent_part`` unconditionally (body motor)
    """
    if drive.driven_part is not None:
        return drive.driven_part

    if joint.parent_part in shaft_part_ids:
        return joint.parent_part
    if joint.child_part in shaft_part_ids:
        return joint.child_part

    # Neither side is a shaft — default to parent for body motors
    return joint.parent_part


def _build_applied_force_objects(
    mechanism: Mechanism,
    shaft_part_ids: set[str],
) -> list[dict[str, Any]]:
    """Translate Mechanism.applied_forces into Chrono daemon spec objects.

    Each AppliedForce becomes one ``applied_force`` spec entry that the C++
    daemon attaches to the named body. Forces targeting shaft-only parts are
    skipped with a warning-equivalent: the Chrono daemon's 1D shafts cannot
    carry 3D point loads.
    """
    objects: list[dict[str, Any]] = []
    for i, f in enumerate(mechanism.applied_forces):
        if f.target_body in shaft_part_ids:
            # Shaft-based parts can't take 3D point loads. Skip.
            continue
        objects.append({
            "type": "applied_force",
            "id": f.label or f"applied_force_{i}",
            "body": f.target_body,
            "position_local": list(f.position_local),
            "force_vector": list(f.force_vector),
            "frame": f.frame,
        })
    return objects


def _build_derived_outputs(
    planetary_sets: list[PlanetarySet],
) -> dict[str, Any]:
    """Build derived output definitions for planet speeds."""
    derived: dict[str, Any] = {}

    for ps in planetary_sets:
        teeth_ratio = ps.teeth_sun / ps.teeth_planet if ps.teeth_planet > 0 else 1.0

        for i, planet_id in enumerate(ps.planets):
            derived[planet_id] = {
                "formula": "carrier_planet",
                "carrier": ps.carrier,
                "sun": ps.sun,
                "teeth_ratio": teeth_ratio,
            }

    return derived


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

def validate_simulation_spec(spec: dict[str, Any]) -> list[str]:
    """Validate a simulation spec before sending to the C++ daemon.

    Returns a list of issue strings.  Empty list = spec is valid.
    Checks:
    1. At least one motor object exists
    2. At least one non-fixed element exists
    3. Motor shaft/body references point to existing objects
    """
    objects = spec.get("objects", [])
    issues: list[str] = []

    # Index object IDs by type
    shaft_ids: set[str] = set()
    body_ids: set[str] = set()
    non_fixed_exists = False

    for obj in objects:
        otype = obj.get("type", "")
        oid = obj.get("id", "")
        if otype == "shaft":
            shaft_ids.add(oid)
            if not obj.get("fixed", False):
                non_fixed_exists = True
        elif otype == "body":
            body_ids.add(oid)
            if not obj.get("fixed", False):
                non_fixed_exists = True

    # Collect motors and check references
    motors: list[dict[str, Any]] = []
    for obj in objects:
        otype = obj.get("type", "")
        if otype == "motor_shaft_speed":
            motors.append(obj)
            target = obj.get("shaft", "")
            if target not in shaft_ids:
                issues.append(
                    f"Motor '{obj.get('id')}' targets shaft '{target}' "
                    f"which does not exist in spec (available shafts: {sorted(shaft_ids)})"
                )
        elif otype == "motor_body_speed":
            motors.append(obj)
            target = obj.get("body", "")
            if target not in body_ids:
                issues.append(
                    f"Motor '{obj.get('id')}' targets body '{target}' "
                    f"which does not exist in spec (available bodies: {sorted(body_ids)})"
                )

    if not motors:
        issues.append("No motor objects in spec — simulation will have no driving force")

    if not non_fixed_exists:
        issues.append("All elements are fixed — nothing can move")

    return issues
