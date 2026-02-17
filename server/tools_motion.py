"""MCP tool implementations for the motion validation pipeline.

Tier 1 tools are purely analytical — no FreeCAD or external dependencies needed.
Tier 3 tools use the Chrono daemon for dynamic simulation (graceful degradation
if the daemon is not running).
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from server.motion_models import (
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.motion_store import get as store_get
from server.motion_store import list_all as store_list_all
from server.motion_store import store as store_put
from server.motion_planetary import PlanetarySet, detect_planetary_sets
from server.motion_validators import (
    analyze_gear_train,
    propagate_speeds,
    propagate_torques,
    run_validators,
)

log = logging.getLogger("solidmind.tools_motion")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))

# Maps mechanism_id -> {joint_id -> FreeCAD object name}
# Populated by motion_create_assembly, consumed by motion_drive_joint
_assembly_joint_maps: dict[str, dict[str, str]] = {}

# Maps mechanism_id -> {part_id -> FreeCAD link name}
# Populated by motion_create_assembly, consumed by motion_drive_joint
_assembly_link_maps: dict[str, dict[str, str]] = {}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Tier 0: Mechanism definition
# ---------------------------------------------------------------------------

def motion_define_mechanism(mechanism: dict[str, Any]) -> dict[str, Any]:
    """Parse and store a mechanism definition.  Returns mechanism_id."""
    if not isinstance(mechanism, dict):
        return _error_result("INVALID_INPUT", "mechanism must be an object")
    if _TOOL_LOG:
        log.info("CALL motion_define_mechanism name=%s", mechanism.get("name"))
    t0 = time.monotonic()

    try:
        mech = Mechanism.from_dict(mechanism)
    except (KeyError, ValueError, TypeError) as exc:
        return _error_result("INVALID_MECHANISM", str(exc))

    # Basic validation
    warnings: list[str] = []
    part_ids = {p.id for p in mech.parts}
    for j in mech.joints:
        if j.parent_part not in part_ids:
            warnings.append(f"Joint '{j.id}' references unknown parent_part '{j.parent_part}'")
        if j.child_part not in part_ids:
            warnings.append(f"Joint '{j.id}' references unknown child_part '{j.child_part}'")
    for d in mech.drives:
        if mech.get_joint(d.joint_id) is None:
            warnings.append(f"Drive references unknown joint_id '{d.joint_id}'")
    if not mech.ground_parts():
        warnings.append("No ground part defined (is_ground=true)")

    handle = store_put(mech)

    if _TOOL_LOG:
        log.info(
            "OK   motion_define_mechanism %.3fs id=%s parts=%d joints=%d",
            time.monotonic() - t0, handle, len(mech.parts), len(mech.joints),
        )

    return {
        "ok": True,
        "mechanism_id": handle,
        "summary": {
            "name": mech.name,
            "part_count": len(mech.parts),
            "joint_count": len(mech.joints),
            "drive_count": len(mech.drives),
            "ground_parts": [p.id for p in mech.ground_parts()],
        },
        "warnings": warnings,
    }


def motion_list_mechanisms() -> dict[str, Any]:
    """List all stored mechanisms."""
    return {"ok": True, "mechanisms": store_list_all()}


# ---------------------------------------------------------------------------
# Tier 1: Analytical validation
# ---------------------------------------------------------------------------

def motion_validate(
    mechanism_id: str,
    validators: list[str] | None = None,
) -> dict[str, Any]:
    """Run analytical validators against a stored mechanism."""
    if _TOOL_LOG:
        log.info("CALL motion_validate id=%s validators=%s", mechanism_id, validators)
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    results = run_validators(mech, validators)

    blockers = [r for r in results if r.status == "fail"]
    warnings = [r for r in results if r.status == "warn"]
    notes = [r for r in results if r.status == "note"]
    passes = [r for r in results if r.status == "pass"]

    def _fmt(r):
        return {"name": r.name, "status": r.status, "message": r.message, "measured": r.measured}

    if _TOOL_LOG:
        log.info(
            "OK   motion_validate %.3fs pass=%d warn=%d fail=%d note=%d",
            time.monotonic() - t0, len(passes), len(warnings), len(blockers), len(notes),
        )

    return {
        "ok": True,
        "results": [_fmt(r) for r in results],
        "blockers": [_fmt(r) for r in blockers],
        "warnings": [_fmt(r) for r in warnings],
        "notes": [_fmt(r) for r in notes],
    }


def motion_propagate_motion(mechanism_id: str) -> dict[str, Any]:
    """Compute speeds and torques at every part via BFS propagation."""
    if _TOOL_LOG:
        log.info("CALL motion_propagate_motion id=%s", mechanism_id)
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    speeds = propagate_speeds(mech)
    torques = propagate_torques(mech)

    # Compute power at each part
    states: dict[str, dict[str, float]] = {}
    total_p_in = 0.0
    total_p_out = 0.0
    input_parts = set()
    for drive in mech.drives:
        joint = mech.get_joint(drive.joint_id)
        if joint:
            input_parts.add(joint.parent_part)

    for part in mech.parts:
        rpm = speeds.get(part.id, 0.0)
        torque = torques.get(part.id, 0.0)
        omega = rpm * 2 * math.pi / 60
        power = abs(torque * omega)
        states[part.id] = {
            "rpm": round(rpm, 4),
            "torque_nm": round(torque, 4),
            "power_w": round(power, 4),
        }
        if part.id in input_parts:
            total_p_in += power
        elif not part.is_ground:
            total_p_out += power

    efficiency = total_p_out / total_p_in if total_p_in > 0 else 0.0

    if _TOOL_LOG:
        log.info("OK   motion_propagate_motion %.3fs parts=%d", time.monotonic() - t0, len(states))

    return {
        "ok": True,
        "states": states,
        "efficiency": round(efficiency, 4),
        "total_input_power_w": round(total_p_in, 4),
        "total_output_power_w": round(total_p_out, 4),
    }


def motion_check_gear_train(mechanism_id: str) -> dict[str, Any]:
    """Analyze the gear train: overall ratio, per-stage details."""
    if _TOOL_LOG:
        log.info("CALL motion_check_gear_train id=%s", mechanism_id)

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    result = analyze_gear_train(mech)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Tier 2: Kinematic validation (FreeCAD Assembly)
# ---------------------------------------------------------------------------

def motion_create_assembly(
    mechanism_id: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Translate a mechanism definition into a FreeCAD Assembly.

    Creates an Assembly container, links each part's body into it,
    and adds joint constraints.  Requires FreeCAD with Assembly workbench.
    """
    if _TOOL_LOG:
        log.info("CALL motion_create_assembly id=%s", mechanism_id)
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

    try:
        client = get_client()

        # Create assembly
        asm_kwargs: dict[str, Any] = {"name": f"Asm_{mech.name}"}
        if doc is not None:
            asm_kwargs["doc"] = doc
        asm_result = client.send_command("assembly_create", **asm_kwargs)
        asm_name = asm_result["name"]

        # Add each part that has a body_name
        link_map: dict[str, str] = {}  # part_id -> link_name
        for part in mech.parts:
            if part.body_name is None:
                continue
            add_kwargs: dict[str, Any] = {
                "assembly": asm_name,
                "body": part.body_name,
            }
            if doc is not None:
                add_kwargs["doc"] = doc
            add_result = client.send_command("assembly_add_part", **add_kwargs)
            link_map[part.id] = add_result["link_name"]

        # Place planets at their orbital positions before adding joints
        p_sets = detect_planetary_sets(mech)
        if p_sets and link_map:
            planet_placements: dict[str, dict[str, Any]] = {}
            for ps in p_sets:
                for planet_id in ps.planets:
                    link_name = link_map.get(planet_id)
                    if link_name is None:
                        continue
                    # Find the carrier-planet revolute joint to get orbital position
                    for j in mech.joints:
                        if j.joint_type == JointType.REVOLUTE and (
                            (j.parent_part == ps.carrier and j.child_part == planet_id)
                            or (j.child_part == ps.carrier and j.parent_part == planet_id)
                        ):
                            if j.origin != (0.0, 0.0, 0.0):
                                planet_placements[link_name] = {
                                    "position": list(j.origin),
                                    "rotation_axis": [0.0, 0.0, 1.0],
                                    "rotation_angle_deg": 0.0,
                                }
                            break

            if planet_placements:
                try:
                    set_kwargs_init: dict[str, Any] = {
                        "assembly": asm_name,
                        "placements": planet_placements,
                    }
                    if doc is not None:
                        set_kwargs_init["doc"] = doc
                    client.send_command("assembly_set_placements", **set_kwargs_init)
                except FreeCADCommandError:
                    pass  # Non-fatal — planets will just be at origin

        # Add joints and build joint name mapping
        joint_names: list[str] = []
        joint_map: dict[str, str] = {}  # mechanism joint_id -> FreeCAD name
        warnings: list[str] = []
        for joint in mech.joints:
            link_a = link_map.get(joint.parent_part)
            link_b = link_map.get(joint.child_part)
            if link_a is None or link_b is None:
                warnings.append(
                    f"Joint '{joint.id}': skipped — part(s) not linked "
                    f"(parent={joint.parent_part}, child={joint.child_part})"
                )
                continue

            joint_kwargs: dict[str, Any] = {
                "assembly": asm_name,
                "joint_type": joint.joint_type.value,
                "part_a": link_a,
                "element_a": "auto",
                "part_b": link_b,
                "element_b": "auto",
                "name": joint.id,
                "joint_origin": list(joint.origin),
                "joint_axis": list(joint.axis),
            }
            if joint.gear_ratio is not None:
                joint_kwargs["ratio"] = joint.gear_ratio
            if doc is not None:
                joint_kwargs["doc"] = doc

            try:
                j_result = client.send_command("assembly_add_joint", **joint_kwargs)
                fc_name = j_result["joint_name"]
                joint_names.append(fc_name)
                joint_map[joint.id] = fc_name
            except FreeCADCommandError as exc:
                warnings.append(f"Joint '{joint.id}': failed — {exc}")

        # Store mappings for later use by motion_drive_joint
        _assembly_joint_maps[mechanism_id] = joint_map
        _assembly_link_maps[mechanism_id] = dict(link_map)

        # Solve
        try:
            solve_kwargs: dict[str, Any] = {"assembly": asm_name}
            if doc is not None:
                solve_kwargs["doc"] = doc
            client.send_command("assembly_solve", **solve_kwargs)
        except FreeCADCommandError as exc:
            warnings.append(f"Assembly solve failed: {exc}")

        if _TOOL_LOG:
            log.info(
                "OK   motion_create_assembly %.3fs asm=%s parts=%d joints=%d",
                time.monotonic() - t0, asm_name, len(link_map), len(joint_names),
            )

        return {
            "ok": True,
            "assembly_name": asm_name,
            "link_map": link_map,
            "joint_names": joint_names,
            "warnings": warnings,
        }

    except FreeCADConnectionError as exc:
        return _error_result("CONNECTION_ERROR", str(exc))
    except FreeCADCommandError as exc:
        return _error_result("COMMAND_ERROR", str(exc))


def _resolve_link_map(
    client: Any,
    mech: Mechanism,
    asm_name: str,
    doc: str | None,
) -> dict[str, str]:
    """Return ``{part_id: link_name}`` from cache or by querying FreeCAD.

    First checks ``_assembly_link_maps`` for a cached mapping.  On cache miss,
    calls the ``assembly_get_links`` command to discover links in the assembly,
    then matches body names back to mechanism parts.
    """
    # Try the in-memory cache first (populated by motion_create_assembly)
    for mid, lmap in _assembly_link_maps.items():
        stored = store_get(mid)
        if stored is not None and stored.name == mech.name and lmap:
            return lmap

    # Cache miss — query FreeCAD for the assembly's links
    try:
        kwargs: dict[str, Any] = {"assembly": asm_name}
        if doc is not None:
            kwargs["doc"] = doc
        result = client.send_command("assembly_get_links", **kwargs)
    except Exception:
        return {}

    fc_links: dict[str, str] = result.get("links", {})
    # fc_links is {link_name: body_name} — invert to match parts
    body_to_link: dict[str, str] = {v: k for k, v in fc_links.items()}

    link_map: dict[str, str] = {}
    for part in mech.parts:
        if part.body_name and part.body_name in body_to_link:
            link_map[part.id] = body_to_link[part.body_name]

    return link_map


def motion_drive_joint(
    mechanism_id: str,
    joint_id: str,
    value: float,
    steps: int = 10,
    doc: str | None = None,
) -> dict[str, Any]:
    """Drive a mechanism analytically and animate in FreeCAD.

    Uses Tier 1 speed propagation to compute how each part moves relative
    to the driven joint, then directly sets link placements in FreeCAD
    (bypassing the Assembly constraint solver which can't handle involute
    gear bodies without cylindrical faces).

    For revolute joints, *value* is the total rotation in degrees applied
    to the driven joint.  All other parts rotate proportionally based on
    their speed ratios from ``propagate_speeds``.
    """
    if _TOOL_LOG:
        log.info(
            "CALL motion_drive_joint id=%s joint=%s value=%.2f steps=%d",
            mechanism_id, joint_id, value, steps,
        )
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    joint = mech.get_joint(joint_id)
    if joint is None:
        return _error_result("NOT_FOUND", f"No joint '{joint_id}' in mechanism")

    # --- Compute speed ratios from Tier 1 propagation ---
    speeds = propagate_speeds(mech)

    # The driven joint's parent part defines the reference speed.
    # All angles are relative to this part's rotation.
    ref_part = joint.parent_part
    ref_rpm = speeds.get(ref_part, 0.0)
    if ref_rpm == 0.0:
        # If parent is ground, use child as reference
        ref_part = joint.child_part
        ref_rpm = speeds.get(ref_part, 0.0)

    # Detect planetary sets for compound planet kinematics
    p_sets = detect_planetary_sets(mech)
    planet_part_ids: set[str] = set()
    planet_to_set: dict[str, PlanetarySet] = {}
    # Map planet_id -> initial orbital position from carrier-planet revolute joint
    planet_initial_pos: dict[str, tuple[float, float, float]] = {}
    for ps in p_sets:
        for pid in ps.planets:
            planet_part_ids.add(pid)
            planet_to_set[pid] = ps
            # Find orbital position from carrier-planet revolute joint
            for j in mech.joints:
                if j.joint_type == JointType.REVOLUTE and (
                    (j.parent_part == ps.carrier and j.child_part == pid)
                    or (j.child_part == ps.carrier and j.parent_part == pid)
                ):
                    planet_initial_pos[pid] = j.origin
                    break

    # Build part_id -> (speed_ratio, rotation_origin, rotation_axis)
    # Speed ratio = part_rpm / ref_rpm  →  angle = ratio * value
    part_kinematics: dict[str, tuple[float, tuple[float, ...], tuple[float, ...]]] = {}
    for part in mech.parts:
        if part.is_ground:
            continue
        # Skip planets — they use compound placement
        if part.id in planet_part_ids:
            continue
        part_rpm = speeds.get(part.id, 0.0)
        ratio = part_rpm / ref_rpm if ref_rpm != 0.0 else 0.0

        # Find the joint that connects this part to determine rotation center
        rot_origin = (0.0, 0.0, 0.0)
        rot_axis = (0.0, 0.0, 1.0)
        for j in mech.joints:
            if j.child_part == part.id or j.parent_part == part.id:
                rot_origin = j.origin
                rot_axis = j.axis
                break

        part_kinematics[part.id] = (ratio, rot_origin, rot_axis)

    from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

    try:
        client = get_client()
        asm_name = f"Asm_{mech.name}"

        # --- Link name mapping: cache or re-derive from FreeCAD ---
        link_map = _resolve_link_map(client, mech, asm_name, doc)

        step_positions: list[dict[str, Any]] = []
        screenshots: list[str] = []

        # Precompute planetary speed ratios for the driven joint
        carrier_ratios: dict[str, float] = {}  # ps.carrier -> ratio
        for ps in p_sets:
            carrier_rpm = speeds.get(ps.carrier, 0.0)
            carrier_ratios[ps.carrier] = carrier_rpm / ref_rpm if ref_rpm != 0.0 else 0.0

        for i in range(steps + 1):
            fraction = i / steps
            driven_angle = value * fraction

            # Build placements for all non-ground, non-planet parts
            placements: dict[str, dict[str, Any]] = {}
            for part_id, (ratio, origin, axis) in part_kinematics.items():
                link_name = link_map.get(part_id)
                if link_name is None:
                    continue
                angle = ratio * driven_angle
                placements[link_name] = {
                    "angle_deg": angle,
                    "axis": list(axis),
                    "center": list(origin),
                }

            # Compound placement for planets
            for ps in p_sets:
                carrier_ratio = carrier_ratios.get(ps.carrier, 0.0)
                carrier_angle_deg = carrier_ratio * driven_angle
                carrier_angle_rad = math.radians(carrier_angle_deg)

                sun_ratio = speeds.get(ps.sun, 0.0) / ref_rpm if ref_rpm != 0.0 else 0.0
                sun_angle_deg = sun_ratio * driven_angle

                for pid in ps.planets:
                    link_name = link_map.get(pid)
                    if link_name is None:
                        continue
                    init_pos = planet_initial_pos.get(pid, (0.0, 0.0, 0.0))

                    # Orbit: rotate initial position by carrier angle about origin
                    cos_c = math.cos(carrier_angle_rad)
                    sin_c = math.sin(carrier_angle_rad)
                    px = init_pos[0] * cos_c - init_pos[1] * sin_c
                    py = init_pos[0] * sin_c + init_pos[1] * cos_c
                    pz = init_pos[2]

                    # Planet spin relative to carrier frame
                    teeth_ratio = ps.teeth_sun / ps.teeth_planet if ps.teeth_planet > 0 else 1.0
                    planet_spin_deg = -(sun_angle_deg - carrier_angle_deg) * teeth_ratio

                    # World rotation = carrier orbit + planet spin
                    planet_world_deg = carrier_angle_deg + planet_spin_deg

                    placements[link_name] = {
                        "position": [px, py, pz],
                        "rotation_axis": [0.0, 0.0, 1.0],
                        "rotation_angle_deg": planet_world_deg,
                    }

            set_kwargs: dict[str, Any] = {
                "assembly": asm_name,
                "placements": placements,
                "screenshot": (i == steps),  # screenshot on last step
            }
            if doc is not None:
                set_kwargs["doc"] = doc

            result = client.send_command("assembly_set_placements", **set_kwargs)

            # Collect all part angles for step_positions
            all_part_angles: dict[str, float] = {
                pid: round(ratio * driven_angle, 4)
                for pid, (ratio, _, _) in part_kinematics.items()
            }
            for ps in p_sets:
                c_ratio = carrier_ratios.get(ps.carrier, 0.0)
                s_ratio = speeds.get(ps.sun, 0.0) / ref_rpm if ref_rpm != 0.0 else 0.0
                c_angle = c_ratio * driven_angle
                s_angle = s_ratio * driven_angle
                for pid in ps.planets:
                    tr = ps.teeth_sun / ps.teeth_planet if ps.teeth_planet > 0 else 1.0
                    spin = -(s_angle - c_angle) * tr
                    all_part_angles[pid] = round(c_angle + spin, 4)

            step_positions.append({
                "step": i,
                "driven_angle": driven_angle,
                "part_angles": all_part_angles,
            })

            if result.get("screenshot"):
                screenshots.append(result["screenshot"])

        if _TOOL_LOG:
            log.info(
                "OK   motion_drive_joint %.3fs steps=%d screenshots=%d",
                time.monotonic() - t0, steps, len(screenshots),
            )

        return {
            "ok": True,
            "assembly": asm_name,
            "joint": joint_id,
            "total_value": value,
            "steps": steps,
            "step_positions": step_positions,
            "screenshots": screenshots,
            "method": "analytical",
        }

    except FreeCADConnectionError as exc:
        return _error_result("CONNECTION_ERROR", str(exc))
    except FreeCADCommandError as exc:
        return _error_result("COMMAND_ERROR", str(exc))


def motion_check_interference(
    mechanism_id: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Check for collision between parts in the mechanism's assembly.

    Uses BRepAlgoAPI_Common to detect overlapping volumes between all part pairs.
    """
    if _TOOL_LOG:
        log.info("CALL motion_check_interference id=%s", mechanism_id)
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

    try:
        client = get_client()

        check_kwargs: dict[str, Any] = {"assembly": f"Asm_{mech.name}"}
        if doc is not None:
            check_kwargs["doc"] = doc

        result = client.send_command("assembly_check_interference", **check_kwargs)

        if _TOOL_LOG:
            log.info(
                "OK   motion_check_interference %.3fs clear=%s collisions=%d",
                time.monotonic() - t0, result.get("clear"), len(result.get("collisions", [])),
            )

        return {"ok": True, **result}

    except FreeCADConnectionError as exc:
        return _error_result("CONNECTION_ERROR", str(exc))
    except FreeCADCommandError as exc:
        return _error_result("COMMAND_ERROR", str(exc))


# ---------------------------------------------------------------------------
# Tier 3: Dynamic validation (Project Chrono)
# ---------------------------------------------------------------------------

def motion_simulate(
    mechanism_id: str,
    duration_s: float = 1.0,
    dt_s: float = 0.001,
    output_interval: float = 0.01,
) -> dict[str, Any]:
    """Run dynamic simulation via the Chrono daemon.

    Requires the chrono_daemon binary to be running on localhost:9877.
    Returns graceful error if the daemon is not available.
    """
    if _TOOL_LOG:
        log.info(
            "CALL motion_simulate id=%s duration=%.3f dt=%.4f",
            mechanism_id, duration_s, dt_s,
        )
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    # Lazy import to avoid import-time dependency on chrono_client
    from server.chrono_client import ChronoCommandError, ChronoConnectionError, get_client

    client = get_client()
    if client is None:
        return _error_result(
            "CHRONO_NOT_CONNECTED",
            "Chrono daemon not running on localhost:9877. "
            "Start it with: chrono_daemon/run.sh (or systemctl --user start chrono-daemon). "
            "Tier 1 (analytical) and Tier 2 (kinematic) validation are still available.",
        )

    # Build simulation spec (Python planner → C++ executor)
    from server.simulation_spec_builder import (
        add_derived_speeds,
        build_simulation_spec,
        validate_simulation_spec,
    )

    spec = build_simulation_spec(mech)

    # Pre-flight: catch referential integrity issues before the C++ round-trip
    spec_issues = validate_simulation_spec(spec)
    if spec_issues:
        return _error_result(
            "SIMULATION_SPEC_INVALID",
            "Simulation spec failed pre-flight validation:\n"
            + "\n".join(f"  - {issue}" for issue in spec_issues),
        )

    try:
        result = client.simulate(
            simulation_spec=spec,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
    except ChronoConnectionError as exc:
        return _error_result("CHRONO_CONNECTION_LOST", str(exc))
    except ChronoCommandError as exc:
        return _error_result("CHRONO_COMMAND_ERROR", str(exc))
    except Exception as exc:
        return _error_result("CHRONO_ERROR", str(exc))

    # Post-process: compute derived planet speeds from sun/carrier
    add_derived_speeds(result, spec)

    # Surface C++ build warnings
    build_warnings = result.pop("warnings", [])

    # Post-flight: detect zero-motion (all steady-state speeds are zero)
    diagnostics: list[str] = []
    ss_speeds = result.get("summary", {}).get("steady_state_speeds", {})
    if ss_speeds and all(abs(v) < 1e-6 for v in ss_speeds.values()):
        diagnostics.append(
            "All steady-state speeds are zero — the simulation produced no motion. "
            "This usually means a motor failed to attach to its target shaft/body. "
            "Check build_warnings for details."
        )

    if _TOOL_LOG:
        samples = len(result.get("time_series", []))
        log.info(
            "OK   motion_simulate %.3fs samples=%d warnings=%d",
            time.monotonic() - t0, samples, len(build_warnings),
        )

    response: dict[str, Any] = {"ok": True, **result}
    if build_warnings:
        response["build_warnings"] = build_warnings
    if diagnostics:
        response["diagnostics"] = diagnostics
    return response
