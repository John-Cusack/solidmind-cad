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
from typing import Literal
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

# Active Isaac teleop sessions keyed by session_id.
_teleop_sessions: dict[str, dict[str, Any]] = {}

_SIM_BACKENDS = {"chrono", "isaac"}
_SIM_MODES = {"batch", "teleop"}
_DEFAULT_SIM_BACKEND: Literal["isaac"] = "isaac"


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _normalize_backend(backend: str | None) -> str:
    if backend is None:
        return _DEFAULT_SIM_BACKEND
    return str(backend).strip().lower()


def _normalize_mode(mode: str | None) -> str:
    if mode is None:
        return "batch"
    return str(mode).strip().lower()


def _backend_unavailable_result(
    requested_backend: str,
    message: str,
    *,
    unavailable_code: str,
) -> dict[str, Any]:
    alternate = "chrono" if requested_backend == "isaac" else "isaac"
    return {
        "ok": False,
        "error": {
            "code": "BACKEND_UNAVAILABLE_CHOOSE",
            "message": (
                f"Requested simulation backend '{requested_backend}' is unavailable. "
                f"{message}"
            ),
        },
        "backend_requested": requested_backend,
        "choices": [
            {
                "action": "retry_with_backend",
                "backend": requested_backend,
                "description": f"Retry with '{requested_backend}' after setup/fix.",
            },
            {
                "action": "retry_with_backend",
                "backend": alternate,
                "description": f"Retry now with '{alternate}'.",
            },
        ],
        "unavailable": {
            "backend": requested_backend,
            "code": unavailable_code,
            "message": message,
        },
    }


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
# Tier 3: Dynamic validation (Chrono or Isaac)
# ---------------------------------------------------------------------------

def _simulate_with_chrono(
    mech: Mechanism,
    *,
    duration_s: float,
    dt_s: float,
    output_interval: float,
) -> dict[str, Any]:
    """Run dynamic simulation via the Chrono daemon for one mechanism."""
    # Lazy import to avoid import-time dependency on chrono_client
    from server.chrono_client import ChronoCommandError, ChronoConnectionError, get_client

    client = get_client()
    if client is None:
        return _backend_unavailable_result(
            "chrono",
            (
                "Chrono daemon not running on localhost:9877. "
                "Start it with: chrono_daemon/run.sh "
                "(or systemctl --user start chrono-daemon)."
            ),
            unavailable_code="CHRONO_NOT_CONNECTED",
        )

    # Build simulation spec (Python planner -> C++ executor)
    from server.simulation_spec_builder import (
        add_derived_speeds,
        build_simulation_spec,
        validate_simulation_spec,
    )

    spec = build_simulation_spec(mech)

    # Pre-flight: catch referential integrity issues before the C++ round-trip.
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

    # Post-process: compute derived planet speeds from sun/carrier.
    add_derived_speeds(result, spec)

    # Surface C++ build warnings.
    build_warnings = result.pop("warnings", [])

    # Post-flight: detect zero-motion (all steady-state speeds are zero).
    diagnostics: list[str] = []
    ss_speeds = result.get("summary", {}).get("steady_state_speeds", {})
    if ss_speeds and all(abs(v) < 1e-6 for v in ss_speeds.values()):
        diagnostics.append(
            "All steady-state speeds are zero — the simulation produced no motion. "
            "This usually means a motor failed to attach to its target shaft/body. "
            "Check build_warnings for details."
        )

    response: dict[str, Any] = {"ok": True, **result, "backend_used": "chrono", "mode_used": "batch"}
    if build_warnings:
        response["build_warnings"] = build_warnings
    if diagnostics:
        response["diagnostics"] = diagnostics
    return response


def _simulate_with_isaac(
    mech: Mechanism,
    *,
    duration_s: float,
    dt_s: float,
    output_interval: float,
) -> dict[str, Any]:
    """Run batch simulation via the optional Isaac sidecar."""
    from server import isaac_adapter

    result = isaac_adapter.simulate(
        mechanism=mech,
        duration_s=duration_s,
        dt_s=dt_s,
        output_interval=output_interval,
    )
    if not result.get("ok", False):
        err = result.get("error", {})
        code = err.get("code", "ISAAC_ERROR")
        if code == "ISAAC_NOT_CONNECTED":
            return _backend_unavailable_result(
                "isaac",
                err.get(
                    "message",
                    "Isaac bridge is not available.",
                ),
                unavailable_code=code,
            )
        return result
    result["backend_used"] = "isaac"
    result["mode_used"] = "batch"
    return result


def motion_simulate(
    mechanism_id: str,
    duration_s: float = 1.0,
    dt_s: float = 0.001,
    output_interval: float = 0.01,
    backend: str | None = None,
    mode: str = "batch",
) -> dict[str, Any]:
    """Run dynamic simulation via selected backend (`isaac` or `chrono`)."""
    if _TOOL_LOG:
        log.info(
            "CALL motion_simulate id=%s backend=%s mode=%s duration=%.3f dt=%.4f",
            mechanism_id, backend, mode, duration_s, dt_s,
        )
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    selected_backend = _normalize_backend(backend)
    selected_mode = _normalize_mode(mode)

    if selected_backend not in _SIM_BACKENDS:
        return _error_result(
            "INVALID_INPUT",
            f"backend must be one of {sorted(_SIM_BACKENDS)}",
        )
    if selected_mode not in _SIM_MODES:
        return _error_result(
            "INVALID_INPUT",
            f"mode must be one of {sorted(_SIM_MODES)}",
        )

    if selected_backend == "chrono" and selected_mode != "batch":
        return _error_result(
            "INVALID_INPUT",
            "mode='teleop' is only supported with backend='isaac'",
        )

    if selected_backend == "chrono":
        response = _simulate_with_chrono(
            mech,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
    elif selected_mode == "teleop":
        response = motion_teleop_start(mechanism_id=mechanism_id, backend="isaac")
    else:
        response = _simulate_with_isaac(
            mech,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )

    if _TOOL_LOG:
        log.info(
            "DONE motion_simulate %.3fs ok=%s backend=%s mode=%s",
            time.monotonic() - t0,
            response.get("ok"),
            response.get("backend_used", selected_backend),
            response.get("mode_used", selected_mode),
        )
    return response


def motion_teleop_start(
    mechanism_id: str,
    backend: str = "isaac",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start an Isaac teleop session for a mechanism."""
    selected_backend = _normalize_backend(backend)
    if selected_backend != "isaac":
        return _error_result(
            "INVALID_INPUT",
            "motion.teleop_start only supports backend='isaac'",
        )

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    from server import isaac_adapter

    result = isaac_adapter.teleop_start(mechanism=mech, profile=profile or {})
    if not result.get("ok", False):
        err = result.get("error", {})
        if err.get("code") == "ISAAC_NOT_CONNECTED":
            return _backend_unavailable_result(
                "isaac",
                err.get("message", "Isaac bridge is not available."),
                unavailable_code="ISAAC_NOT_CONNECTED",
            )
        return result

    session_id = str(result.get("session_id", "")).strip()
    if not session_id:
        return _error_result("ISAAC_ERROR", "Isaac teleop response missing session_id")

    _teleop_sessions[session_id] = {
        "mechanism_id": mechanism_id,
        "backend": "isaac",
        "created_at": time.time(),
    }
    return {
        "ok": True,
        "backend_used": "isaac",
        "mode_used": "teleop",
        **result,
    }


def motion_teleop_command(
    session_id: str,
    vx_mps: float = 0.0,
    yaw_rate_rps: float = 0.0,
    body_height_m: float = 0.0,
) -> dict[str, Any]:
    """Send a teleop command to an active Isaac session."""
    session = _teleop_sessions.get(session_id)
    if session is None:
        return _error_result("NOT_FOUND", f"No active teleop session '{session_id}'")

    from server import isaac_adapter

    result = isaac_adapter.teleop_command(
        session_id=session_id,
        vx_mps=vx_mps,
        yaw_rate_rps=yaw_rate_rps,
        body_height_m=body_height_m,
    )
    if not result.get("ok", False):
        return result
    return {"ok": True, "backend_used": "isaac", "mode_used": "teleop", **result}


def motion_teleop_state(session_id: str) -> dict[str, Any]:
    """Read current state from an active Isaac teleop session."""
    session = _teleop_sessions.get(session_id)
    if session is None:
        return _error_result("NOT_FOUND", f"No active teleop session '{session_id}'")

    from server import isaac_adapter

    result = isaac_adapter.teleop_state(session_id=session_id)
    if not result.get("ok", False):
        return result
    return {"ok": True, "backend_used": "isaac", "mode_used": "teleop", **result}


def motion_teleop_stop(session_id: str) -> dict[str, Any]:
    """Stop and remove an active Isaac teleop session."""
    session = _teleop_sessions.get(session_id)
    if session is None:
        return _error_result("NOT_FOUND", f"No active teleop session '{session_id}'")

    from server import isaac_adapter

    result = isaac_adapter.teleop_stop(session_id=session_id)
    if not result.get("ok", False):
        return result
    _teleop_sessions.pop(session_id, None)
    return {"ok": True, "backend_used": "isaac", "mode_used": "teleop", **result}
