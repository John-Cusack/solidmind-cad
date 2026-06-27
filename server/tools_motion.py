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
from typing import Any, Literal

from server.motion_models import (
    JointEdge,
    JointType,
    Mechanism,
)
from server.motion_planetary import PlanetarySet, detect_planetary_sets
from server.motion_store import get as store_get
from server.motion_store import list_all as store_list_all
from server.motion_store import store as store_put
from server.motion_validators import (
    analyze_gear_train,
    compute_gear_animation_ratios,
    compute_gear_mesh_phases,
    propagate_speeds,
    propagate_torques,
    run_validators,
    validate_mechanism_structure,
)

log = logging.getLogger("solidmind.tools_motion")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))

# Maps mechanism_id -> {joint_id -> FreeCAD object name}
# Populated by motion_create_assembly, consumed by motion_drive_joint
_assembly_joint_maps: dict[str, dict[str, str]] = {}

# Maps mechanism_id -> {part_id -> FreeCAD link name}
# Populated by motion_create_assembly, consumed by motion_drive_joint
_assembly_link_maps: dict[str, dict[str, str]] = {}

# Active Isaac sessions keyed by session_id (teleop and interactive sim).
_active_sessions: dict[str, dict[str, Any]] = {}

_SIM_BACKENDS = {"chrono", "isaac", "gazebo"}
_TELEOP_BACKENDS = {"isaac", "gazebo"}
_SIM_MODES = {"batch", "teleop"}
_DEFAULT_SIM_BACKEND: Literal["isaac"] = "isaac"
_GAZEBO_CONTROLLER_TYPES = {"multirotor_direct", "px4_offboard"}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# Capture-signal aliases recognised by motion.simulate's `capture` parameter.
# Each maps a high-level name to a derivation rule applied to the simulate
# response (after the daemon round-trip). Signals not in this map are listed
# back to the caller as `unrecognized` so they can compute them analytically.
_CHRONO_CAPTURE_SIGNALS: set[str] = {
    "thrust_mean_N",
    "thrust_std_N",
    "hub_bearing_load_N",
    "peak_hub_bearing_load_N",
    "applied_force_count",
}


def _derive_captures(
    response: dict[str, Any],
    capture: list[str],
) -> dict[str, Any]:
    """Derive high-level capture signals from a Chrono simulate response.

    Only daemon-derivable signals are computed here. Analytical signals like
    blade_root_moment_Nm or tip_deflection_mm are left to the caller — they
    appear in the returned ``unrecognized`` list so Phase-5 callers know they
    must compute them from BEMT per-station data + beam theory.
    """
    summary = response.get("summary", {}) or {}
    out: dict[str, Any] = {}
    unrecognized: list[str] = []

    for name in capture:
        if name == "thrust_mean_N":
            out[name] = summary.get("applied_force_world_z_mean_N")
        elif name == "thrust_std_N":
            out[name] = summary.get("applied_force_world_z_std_N")
        elif name == "applied_force_count":
            out[name] = summary.get("applied_force_count")
        elif name == "hub_bearing_load_N":
            mean_forces = summary.get("mean_joint_forces") or {}
            out[name] = dict(mean_forces) if mean_forces else {}
        elif name == "peak_hub_bearing_load_N":
            peak_forces = summary.get("peak_joint_forces") or {}
            out[name] = dict(peak_forces) if peak_forces else {}
        else:
            unrecognized.append(name)

    return {"signals": out, "unrecognized": unrecognized}


def _rotate_point_around_center(
    point: tuple[float, float, float],
    center: tuple[float, float, float],
    axis: tuple[float, float, float],
    angle_deg: float,
) -> tuple[float, float, float]:
    """Rodrigues rotation of *point* around *center* by *angle_deg* about *axis*."""
    angle_rad = math.radians(angle_deg)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    dx, dy, dz = point[0] - center[0], point[1] - center[1], point[2] - center[2]
    dot = axis[0] * dx + axis[1] * dy + axis[2] * dz
    cx = axis[1] * dz - axis[2] * dy
    cy = axis[2] * dx - axis[0] * dz
    cz = axis[0] * dy - axis[1] * dx
    rx = dx * cos_a + cx * sin_a + axis[0] * dot * (1 - cos_a)
    ry = dy * cos_a + cy * sin_a + axis[1] * dot * (1 - cos_a)
    rz = dz * cos_a + cz * sin_a + axis[2] * dot * (1 - cos_a)
    return (center[0] + rx, center[1] + ry, center[2] + rz)


def _extract_peak_joint_forces(
    samples: list[dict[str, Any]],
) -> dict[str, float]:
    """Extract peak absolute joint efforts from a time series.

    Returns a dict mapping joint index (as string) to peak absolute effort.
    If the time series contains ``joint_efforts`` arrays, we track the maximum
    absolute value per joint across all timesteps.  Returns empty dict if no
    effort data is present.
    """
    peak: dict[int, float] = {}
    for sample in samples:
        efforts = sample.get("joint_efforts")
        if not efforts:
            continue
        for i, e in enumerate(efforts):
            val = abs(float(e))
            if i not in peak or val > peak[i]:
                peak[i] = val
    if not peak:
        return {}
    return {f"joint_{i}": round(v, 4) for i, v in sorted(peak.items())}


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
    alternates = sorted(_SIM_BACKENDS - {requested_backend})
    choices: list[dict[str, str]] = [
        {
            "action": "retry_with_backend",
            "backend": requested_backend,
            "description": f"Retry with '{requested_backend}' after setup/fix.",
        },
    ]
    for alt in alternates:
        choices.append({
            "action": "retry_with_backend",
            "backend": alt,
            "description": f"Retry now with '{alt}'.",
        })
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
        "choices": choices,
        "unavailable": {
            "backend": requested_backend,
            "code": unavailable_code,
            "message": message,
        },
    }


def _validate_simulation_params(
    *,
    duration_s: float,
    dt_s: float,
    output_interval: float,
) -> str | None:
    numeric_fields = {
        "duration_s": duration_s,
        "dt_s": dt_s,
        "output_interval": output_interval,
    }
    for name, value in numeric_fields.items():
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return f"{name} must be a finite number"
        if float(value) <= 0.0:
            return f"{name} must be > 0"
    if output_interval < dt_s:
        return "output_interval must be >= dt_s"
    if output_interval > duration_s:
        return "output_interval must be <= duration_s"
    return None


def _is_unknown_session_error(result: dict[str, Any]) -> bool:
    err = result.get("error")
    if not isinstance(err, dict):
        return False
    code = str(err.get("code", "")).strip().upper()
    if code in {
        "ISAAC_UNKNOWN_SESSION", "ISAAC_SESSION_NOT_FOUND",
        "GAZEBO_UNKNOWN_SESSION", "GAZEBO_SESSION_NOT_FOUND",
    }:
        return True
    msg = str(err.get("message", "")).strip().lower()
    return (
        "unknown session" in msg
        or "session not found" in msg
        or "no such session" in msg
    )


def _has_sim_path(path: str | None) -> bool:
    return bool(path and str(path).strip())


def _validate_gazebo_sim_paths(urdf_path: str | None, sdf_path: str | None) -> str | None:
    if _has_sim_path(urdf_path) or _has_sim_path(sdf_path):
        return None
    return (
        "Gazebo simulation requires at least one model path: "
        "provide urdf_path or sdf_path."
    )


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

    # Structural validation — catches bad data early
    errors, warnings = validate_mechanism_structure(mech, mode="strict")
    if errors:
        return _error_result(
            "INVALID_MECHANISM",
            f"{len(errors)} structural error(s): {'; '.join(errors)}",
        )

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

    # Build per-joint force summary for FEA coupling
    joint_forces: dict[str, dict[str, float]] = {}
    for joint in mech.joints:
        parent_torque = torques.get(joint.parent_part, 0.0)
        child_torque = torques.get(joint.child_part, 0.0)
        # Reaction torque at the joint is the torque transmitted
        reaction_torque = max(abs(parent_torque), abs(child_torque))
        joint_forces[joint.id] = {
            "reaction_torque_nm": round(reaction_torque, 4),
            "parent_part": joint.parent_part,
            "child_part": joint.child_part,
        }

    if _TOOL_LOG:
        log.info("OK   motion_propagate_motion %.3fs parts=%d", time.monotonic() - t0, len(states))

    return {
        "ok": True,
        "states": states,
        "joint_forces": joint_forces,
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

        # Pre-flight: when the doc reports SOME bodies but not the ones the
        # mechanism references, surface that here with all missing names at
        # once instead of raising mid-loop on the first one. The "no bodies
        # at all" case falls through to the addon's assembly_add_part, which
        # has its own informative error pointing at cad.new_body etc.
        referenced_bodies = {
            part.body_name for part in mech.parts if part.body_name is not None
        }
        if referenced_bodies:
            try:
                tree_kwargs: dict[str, Any] = {"detail": "bodies"}
                if doc is not None:
                    tree_kwargs["doc"] = doc
                tree = client.send_command("get_model_tree", **tree_kwargs)
                if isinstance(tree, dict):
                    available = set(tree.get("bodies") or [])
                elif isinstance(tree, list):
                    available = {b if isinstance(b, str) else b.get("name", "") for b in tree}
                else:
                    available = set()
                missing = sorted(referenced_bodies - available)
                if missing and available:
                    return _error_result(
                        "MISSING_BODIES",
                        f"Mechanism '{mech.name}' references bodies that don't "
                        f"exist in the target doc: {missing}. Available bodies: "
                        f"{sorted(available)}. Build the geometry first via "
                        "cad.new_body / cad.create_primitive / cad.import_step, "
                        "then retry motion.create_assembly.",
                    )
            except FreeCADCommandError:
                # get_model_tree might fail for unrelated reasons; fall
                # through and let assembly_add_part raise the per-body error.
                pass

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

        # Warn if no bodies were linked despite parts having body_name
        expected_parts = sum(
            1 for p in mech.parts if p.body_name and not p.is_ground
        )
        if expected_parts > 0 and not link_map:
            warnings.append(
                f"Assembly is empty: {expected_parts} part(s) have body_name "
                f"but 0 were linked. motion_drive_joint will use body_placement fallback."
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
    check_collisions: bool = False,
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

    # Compute gear mesh phase offsets for tooth interlocking
    mesh_phases = compute_gear_mesh_phases(mech)

    # Run geometric interference check on the computed phases
    from server.motion_validators import check_tooth_interference

    phase_interference = check_tooth_interference(mech, mesh_phases)
    phase_warnings = [r for r in phase_interference if r.get("ok") is False]

    # Compute physically correct signed animation ratios for gears.
    # These give correct direction (external gears counter-rotate) and
    # correct magnitude (Z_driver / Z_driven), unlike propagate_speeds
    # which is used for validation but has inverted gear ratios.
    anim_ratios = compute_gear_animation_ratios(mech)

    # Build part_id -> (signed_ratio, rotation_origin, rotation_axis, phase_offset)
    # angle = signed_ratio * driven_angle + phase
    _ROTATION_JOINT_TYPES = {JointType.REVOLUTE, JointType.CONTINUOUS}
    part_kinematics: dict[str, tuple[float, tuple[float, ...], tuple[float, ...], float]] = {}
    for part in mech.parts:
        if part.is_ground:
            continue
        # Skip planets — they use compound placement
        if part.id in planet_part_ids:
            continue

        # Use animation ratio if available (gear-connected parts),
        # otherwise fall back to propagate_speeds ratio
        if part.id in anim_ratios:
            ratio = anim_ratios[part.id]
        else:
            part_rpm = speeds.get(part.id, 0.0)
            ratio = part_rpm / ref_rpm if ref_rpm != 0.0 else 0.0

        # Find the REVOLUTE/CONTINUOUS joint for this part's rotation center.
        # Gear-mesh or belt-chain origins are at the contact point, not the
        # rotation axis — prefer revolute joints.
        rot_origin = (0.0, 0.0, 0.0)
        rot_axis = (0.0, 0.0, 1.0)
        for j in mech.joints:
            if j.joint_type in _ROTATION_JOINT_TYPES and (
                j.child_part == part.id or j.parent_part == part.id
            ):
                rot_origin = j.origin
                rot_axis = j.axis
                break
        else:
            # Fallback: any joint if no revolute found
            for j in mech.joints:
                if j.child_part == part.id or j.parent_part == part.id:
                    rot_origin = j.origin
                    rot_axis = j.axis
                    break

        phase = mesh_phases.get(part.id, 0.0)
        part_kinematics[part.id] = (ratio, rot_origin, rot_axis, phase)

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

        # Capture initial body positions for the body_placement fallback.
        # When P0 == origin this is a no-op (standard workflow).
        initial_positions: dict[str, tuple[float, float, float]] = {}
        if not link_map:
            try:
                from server.tools_cad import cad_get_model_tree
                tree_kw: dict[str, Any] = {"detail": "bodies"}
                if doc is not None:
                    tree_kw["doc"] = doc
                tree_result = cad_get_model_tree(**tree_kw)
                if tree_result.get("ok"):
                    for body in tree_result.get("bodies", []):
                        pos = body.get("position")
                        label = body.get("label")
                        if label and pos and len(pos) >= 3:
                            initial_positions[label] = (pos[0], pos[1], pos[2])
            except Exception as exc:
                log.debug("Could not capture initial body positions: %s", exc)

        step_collisions: list[dict[str, Any]] = []

        # Collect body names for collision checking
        collision_body_names: list[str] = []
        if check_collisions:
            collision_body_names = [
                p.body_name for p in mech.parts
                if p.body_name and not p.is_ground
            ]

        for i in range(steps + 1):
            fraction = i / steps
            driven_angle = value * fraction

            # Build placements for all non-ground, non-planet parts
            placements: dict[str, dict[str, Any]] = {}
            for part_id, (ratio, origin, axis, phase) in part_kinematics.items():
                link_name = link_map.get(part_id)
                if link_name is None:
                    continue
                angle = ratio * driven_angle + phase
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

            # --- Fallback: set body placements directly when assembly has no links ---
            # Rotate each body's initial position around the joint origin using
            # Rodrigues.  When P0 == origin (standard workflow where body is
            # placed at the joint origin), this reduces to the previous behaviour.
            if not link_map:
                for part_id, (ratio, origin, axis, phase) in part_kinematics.items():
                    part = mech.get_part(part_id)
                    body_label = part.body_name if part else None
                    if not body_label:
                        continue
                    angle = ratio * driven_angle + phase
                    p0 = initial_positions.get(body_label, origin)
                    new_base = _rotate_point_around_center(p0, origin, axis, angle)
                    sp_kwargs: dict[str, Any] = {
                        "object_name": body_label,
                        "position": list(new_base),
                        "rotation_axis": list(axis),
                        "rotation_angle_deg": angle,
                    }
                    if doc is not None:
                        sp_kwargs["doc"] = doc
                    client.send_command("set_placement", **sp_kwargs)

                # Screenshot on last step
                if i == steps:
                    sc_kwargs: dict[str, Any] = {}
                    if doc is not None:
                        sc_kwargs["doc"] = doc
                    try:
                        sc_result = client.send_command("screenshot", **sc_kwargs)
                        if sc_result.get("screenshot"):
                            screenshots.append(sc_result["screenshot"])
                    except Exception:
                        pass
            else:
                set_kwargs: dict[str, Any] = {
                    "assembly": asm_name,
                    "placements": placements,
                    "screenshot": (i == steps),  # screenshot on last step
                }
                if doc is not None:
                    set_kwargs["doc"] = doc

                result = client.send_command("assembly_set_placements", **set_kwargs)

                if result.get("screenshot"):
                    screenshots.append(result["screenshot"])

            # Collision check at this step
            if check_collisions and len(collision_body_names) >= 2:
                try:
                    clr_kwargs: dict[str, Any] = {
                        "bodies": collision_body_names,
                        "threshold_mm": 0.0,
                    }
                    if doc is not None:
                        clr_kwargs["doc"] = doc
                    clr_result = client.send_command(
                        "check_clearance", **clr_kwargs,
                    )
                    for v in clr_result.get("violations", []):
                        if v.get("intersecting"):
                            step_collisions.append({
                                "step": i,
                                "driven_angle": driven_angle,
                                "body_a": v["body_a"],
                                "body_b": v["body_b"],
                                "distance_mm": v.get("distance_mm", 0.0),
                            })
                except Exception:
                    pass  # clearance check failure shouldn't abort animation

            # Collect all part angles for step_positions
            all_part_angles: dict[str, float] = {
                pid: round(ratio * driven_angle + phase, 4)
                for pid, (ratio, _, _, phase) in part_kinematics.items()
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

        if _TOOL_LOG:
            log.info(
                "OK   motion_drive_joint %.3fs steps=%d screenshots=%d",
                time.monotonic() - t0, steps, len(screenshots),
            )

        resp: dict[str, Any] = {
            "ok": True,
            "assembly": asm_name,
            "joint": joint_id,
            "total_value": value,
            "steps": steps,
            "step_positions": step_positions,
            "screenshots": screenshots,
            "method": "analytical",
        }
        if not link_map:
            resp["fallback"] = "body_placement"
        if phase_warnings:
            resp["tooth_interference"] = phase_warnings
            resp["tooth_interference_warning"] = (
                f"Tooth interference detected at {len(phase_warnings)} "
                f"mesh(es) — animation may show colliding teeth"
            )
        if check_collisions:
            resp["collisions"] = step_collisions
            resp["collision_free"] = len(step_collisions) == 0
            if step_collisions:
                resp["collision_summary"] = (
                    f"{len(step_collisions)} collision(s) detected across "
                    f"{len(set((c['body_a'], c['body_b']) for c in step_collisions))} pair(s)"
                )
        return resp

    except FreeCADConnectionError as exc:
        return _error_result("CONNECTION_ERROR", str(exc))
    except FreeCADCommandError as exc:
        return _error_result("COMMAND_ERROR", str(exc))


def motion_check_joint_connectivity(
    mechanism_id: str,
    tolerance_mm: float = 2.0,
    doc: str | None = None,
) -> dict[str, Any]:
    """Check that each joint origin touches both parent and child body geometry.

    Uses ``distToShape`` in FreeCAD to measure the distance from each joint
    origin point to both the parent and child body shapes.  Flags joints
    where either body is farther than ``tolerance_mm`` from the origin.

    Run this after building bodies but before URDF export to catch
    connectivity issues early.
    """
    if _TOOL_LOG:
        log.info("CALL motion_check_joint_connectivity id=%s tol=%.1f", mechanism_id, tolerance_mm)
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

    # Build the joints list for the FreeCAD command: map part ids to body names.
    part_body_map: dict[str, str] = {}
    for part in mech.parts:
        part_body_map[part.id] = part.body_name or part.id

    joints_arg: list[dict[str, Any]] = []
    for jedge in mech.joints:
        parent_body = part_body_map.get(jedge.parent_part, jedge.parent_part)
        child_body = part_body_map.get(jedge.child_part, jedge.child_part)
        joints_arg.append({
            "id": jedge.id,
            "parent_body": parent_body,
            "child_body": child_body,
            "origin": list(jedge.origin),
        })

    try:
        client = get_client()
        cmd_kwargs: dict[str, Any] = {
            "joints": joints_arg,
            "tolerance_mm": tolerance_mm,
        }
        if doc is not None:
            cmd_kwargs["doc"] = doc

        result = client.send_command("check_joint_connectivity", **cmd_kwargs)

        if _TOOL_LOG:
            log.info(
                "OK   motion_check_joint_connectivity %.3fs all_connected=%s",
                time.monotonic() - t0, result.get("all_connected"),
            )

        return {"ok": True, **result}

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

    # Extract peak joint forces from Chrono time series if present
    ts = result.get("time_series", [])
    peak_forces = _extract_peak_joint_forces(ts)
    if peak_forces:
        response.setdefault("summary", {})["peak_joint_forces"] = peak_forces
    # Also check if Chrono daemon returned peak_joint_forces directly
    chrono_peaks = result.get("summary", {}).get("peak_joint_forces")
    if chrono_peaks and isinstance(chrono_peaks, dict):
        response.setdefault("summary", {})["peak_joint_forces"] = chrono_peaks

    return response


def _simulate_with_gazebo(
    mech: Mechanism,
    *,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any],
    urdf_path: str | None = None,
    sdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run batch simulation via the optional Gazebo sidecar (single-call)."""
    model_path_error = _validate_gazebo_sim_paths(urdf_path, sdf_path)
    if model_path_error is not None:
        return _error_result("INVALID_INPUT", model_path_error)

    preflight = _run_sdf_preflight(sdf_path) if _has_sim_path(sdf_path) else _run_urdf_preflight(urdf_path)
    if preflight and preflight["blockers"]:
        return _error_result(
            "URDF_VALIDATION_FAILED" if not _has_sim_path(sdf_path) else "SDF_VALIDATION_FAILED",
            f"Model pre-flight found {len(preflight['blockers'])} blocker(s): "
            + "; ".join(b["message"] for b in preflight["blockers"]),
        )

    from server import gazebo_adapter

    result = gazebo_adapter.simulate(
        mechanism=mech,
        duration_s=duration_s,
        dt_s=dt_s,
        output_interval=output_interval,
        profile=profile,
        urdf_path=urdf_path,
        sdf_path=sdf_path,
        import_config=import_config,
    )
    if not result.get("ok", False):
        err = result.get("error", {})
        code = err.get("code", "GAZEBO_COMMAND_ERROR")
        if code == "GAZEBO_NOT_CONNECTED":
            return _backend_unavailable_result(
                "gazebo",
                err.get("message", "Gazebo bridge is not available."),
                unavailable_code=code,
            )
        return result
    result["backend_used"] = "gazebo"
    result["mode_used"] = "batch"
    if preflight:
        result["urdf_validation" if not _has_sim_path(sdf_path) else "sdf_validation"] = preflight

    # Extract peak joint forces from Gazebo time series if present
    ts = result.get("time_series", [])
    peak_forces = _extract_peak_joint_forces(ts)
    if peak_forces:
        result.setdefault("summary", {})["peak_joint_forces"] = peak_forces

    return result


def _simulate_with_isaac_legacy(
    mech: Mechanism,
    *,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any],
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fallback: use the old monolithic simulate command for old bridges."""
    from server import isaac_adapter

    result = isaac_adapter.simulate(
        mechanism=mech,
        duration_s=duration_s,
        dt_s=dt_s,
        output_interval=output_interval,
        profile=profile,
        urdf_path=urdf_path,
        import_config=import_config,
    )
    if not result.get("ok", False):
        err = result.get("error", {})
        code = err.get("code", "ISAAC_ERROR")
        if code == "ISAAC_NOT_CONNECTED":
            return _backend_unavailable_result(
                "isaac",
                err.get("message", "Isaac bridge is not available."),
                unavailable_code=code,
            )
        return result
    result["backend_used"] = "isaac"
    result["mode_used"] = "batch"
    return result


def _run_urdf_preflight(urdf_path: str | None) -> dict[str, Any] | None:
    """Run URDF structural validation if a path is provided.

    Returns ``None`` if there's no URDF or no blockers/warnings.
    Otherwise returns a dict with ``blockers`` and ``warnings`` lists.
    If blockers exist the caller should short-circuit.
    """
    if urdf_path is None:
        return None
    if not os.path.isfile(urdf_path):
        return None

    try:
        from server.models import Severity
        from server.sim_export import validate_urdf
    except ImportError:
        log.debug("sim_export.validate_urdf not available, skipping preflight")
        return None

    try:
        findings = validate_urdf(urdf_path)
    except Exception as exc:
        log.warning("URDF preflight validation failed: %s", exc)
        return None

    if not findings:
        return None

    blockers = [f.to_dict() for f in findings if f.severity == Severity.BLOCK]
    warnings = [f.to_dict() for f in findings if f.severity == Severity.WARN]
    notes = [f.to_dict() for f in findings if f.severity == Severity.NOTE]

    if not blockers and not warnings and not notes:
        return None

    return {
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
    }


def _run_sdf_preflight(sdf_path: str | None) -> dict[str, Any] | None:
    """Run SDF structural validation if a path is provided."""
    if sdf_path is None:
        return None
    if not os.path.isfile(sdf_path):
        return None

    try:
        from server.models import Severity
        from server.sim_export import validate_sdf
    except ImportError:
        log.debug("sim_export.validate_sdf not available, skipping preflight")
        return None

    try:
        findings = validate_sdf(sdf_path)
    except Exception as exc:
        log.warning("SDF preflight validation failed: %s", exc)
        return None

    if not findings:
        return None

    blockers = [f.to_dict() for f in findings if f.severity == Severity.BLOCK]
    warnings = [f.to_dict() for f in findings if f.severity == Severity.WARN]
    notes = [f.to_dict() for f in findings if f.severity == Severity.NOTE]

    if not blockers and not warnings and not notes:
        return None
    return {"blockers": blockers, "warnings": warnings, "notes": notes}


def _simulate_with_isaac(
    mech: Mechanism,
    *,
    duration_s: float,
    dt_s: float,
    output_interval: float,
    profile: dict[str, Any],
    urdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Run simulation via the optional Isaac sidecar using session lifecycle."""
    # URDF pre-flight validation
    preflight = _run_urdf_preflight(urdf_path)
    if preflight and preflight["blockers"]:
        return _error_result(
            "URDF_VALIDATION_FAILED",
            f"URDF pre-flight found {len(preflight['blockers'])} blocker(s): "
            + "; ".join(b["message"] for b in preflight["blockers"]),
        )

    from server import isaac_adapter

    # Start session
    start_result = isaac_adapter.simulate_start(
        mechanism=mech,
        duration_s=duration_s,
        dt_s=dt_s,
        output_interval=output_interval,
        profile=profile,
        urdf_path=urdf_path,
        import_config=import_config,
        verify=verify,
    )
    if not start_result.get("ok", False):
        err = start_result.get("error", {})
        code = err.get("code", "ISAAC_ERROR")
        if code == "ISAAC_NOT_CONNECTED":
            return _backend_unavailable_result(
                "isaac",
                err.get("message", "Isaac bridge is not available."),
                unavailable_code=code,
            )
        # Fallback: old bridge without simulate_start support
        if code == "UNKNOWN_COMMAND":
            return _simulate_with_isaac_legacy(
                mech,
                duration_s=duration_s,
                dt_s=dt_s,
                output_interval=output_interval,
                profile=profile,
                urdf_path=urdf_path,
                import_config=import_config,
            )
        return start_result

    session_id = str(start_result.get("session_id", "")).strip()
    if not session_id:
        return _error_result("ISAAC_PROTOCOL_ERROR", "simulate_start missing session_id")

    # Interactive mode: return immediately, store session for later stop
    if start_result.get("interactive"):
        _active_sessions[session_id] = {
            "mechanism_id": None,
            "backend": "isaac",
            "session_type": "simulate",
            "created_at": time.time(),
        }
        return {
            "ok": True,
            "backend_used": "isaac",
            "mode_used": "interactive",
            **start_result,
        }

    # Batch mode: poll until complete
    while True:
        status_result = isaac_adapter.simulate_status(session_id=session_id)
        if not status_result.get("ok", False):
            return status_result
        if status_result.get("status") == "complete":
            break
        time.sleep(0.01)

    # Stop and collect results
    stop_result = isaac_adapter.simulate_stop(session_id=session_id)
    if not stop_result.get("ok", False):
        return stop_result

    # Build backward-compatible response
    samples = stop_result.get("samples", [])
    speeds = start_result.get("steady_state_speeds", {})

    # Extract peak joint efforts from time series
    peak_joint_forces = _extract_peak_joint_forces(samples)

    result: dict[str, Any] = {
        "ok": True,
        "time_series": samples,
        "summary": {
            "simulation_time_s": duration_s,
            "time_steps": stop_result.get("target_steps", 0),
            "output_samples": len(samples),
            "steady_state_speeds": speeds,
            "engine_mode": "isaac_urdf" if start_result.get("prim_path") else "reference",
        },
        "profile_used": start_result.get("profile_used", {}),
        "backend_used": "isaac",
        "mode_used": "batch",
    }
    if peak_joint_forces:
        result["summary"]["peak_joint_forces"] = peak_joint_forces
    if start_result.get("prim_path"):
        result["summary"]["prim_path"] = start_result["prim_path"]
        result["summary"]["joint_count"] = start_result.get("joint_count", 0)
        result["summary"]["link_count"] = start_result.get("link_count", 0)
    warnings = start_result.get("warnings") or stop_result.get("warnings")
    if warnings:
        result["warnings"] = warnings
    # Include URDF pre-flight findings (non-blocker warnings/notes)
    if preflight:
        result["urdf_validation"] = preflight
    # Forward verification images from the bridge
    if start_result.get("verification_images"):
        result["verification_images"] = start_result["verification_images"]
    return result


def motion_simulate(
    mechanism_id: str,
    duration_s: float = 1.0,
    dt_s: float = 0.001,
    output_interval: float = 0.01,
    backend: str | None = None,
    mode: str = "batch",
    profile: dict[str, Any] | None = None,
    urdf_path: str | None = None,
    sdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
    verify: bool = True,
    capture: list[str] | None = None,
) -> dict[str, Any]:
    """Run dynamic simulation via selected backend (`isaac`, `chrono`, or `gazebo`).

    ``capture`` is an optional list of high-level signal names. Recognized
    daemon-derivable signals (thrust_mean_N, thrust_std_N, hub_bearing_load_N,
    peak_hub_bearing_load_N, applied_force_count) are computed from summary
    fields and surfaced under ``response['captures']['signals']``. Unrecognized
    signals are listed under ``response['captures']['unrecognized']`` so the
    caller can compute them analytically (blade_root_moment_Nm, tip_deflection_mm,
    etc.).
    """
    if _TOOL_LOG:
        log.info(
            "CALL motion_simulate id=%s backend=%s mode=%s duration=%.3f dt=%.4f",
            mechanism_id, backend, mode, duration_s, dt_s,
        )
    t0 = time.monotonic()

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")
    if profile is not None and not isinstance(profile, dict):
        return _error_result("INVALID_INPUT", "profile must be an object")

    selected_backend = _normalize_backend(backend)
    selected_mode = _normalize_mode(mode)
    param_error = _validate_simulation_params(
        duration_s=duration_s,
        dt_s=dt_s,
        output_interval=output_interval,
    )
    if param_error is not None:
        return _error_result("INVALID_INPUT", param_error)

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

    if selected_mode == "teleop" and selected_backend not in _TELEOP_BACKENDS:
        return _error_result(
            "INVALID_INPUT",
            f"mode='teleop' is only supported with backends {sorted(_TELEOP_BACKENDS)}",
        )
    if selected_backend == "gazebo":
        path_error = _validate_gazebo_sim_paths(urdf_path, sdf_path)
        if path_error is not None:
            return _error_result("INVALID_INPUT", path_error)

    if selected_mode == "teleop":
        response = motion_teleop_start(
            mechanism_id=mechanism_id,
            backend=selected_backend,
            profile=profile or {},
            urdf_path=urdf_path,
            sdf_path=sdf_path,
            import_config=import_config,
        )
    elif selected_backend == "chrono":
        response = _simulate_with_chrono(
            mech,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
    elif selected_backend == "gazebo":
        response = _simulate_with_gazebo(
            mech,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
            profile=profile or {},
            urdf_path=urdf_path,
            sdf_path=sdf_path,
            import_config=import_config,
        )
    else:
        response = _simulate_with_isaac(
            mech,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
            profile=profile or {},
            urdf_path=urdf_path,
            import_config=import_config,
            verify=verify,
        )

    if capture and response.get("ok"):
        if not isinstance(capture, list) or not all(isinstance(c, str) for c in capture):
            return _error_result(
                "INVALID_INPUT",
                "capture must be a list of strings",
            )
        response["captures"] = _derive_captures(response, capture)

    if _TOOL_LOG:
        log.info(
            "DONE motion_simulate %.3fs ok=%s backend=%s mode=%s",
            time.monotonic() - t0,
            response.get("ok"),
            response.get("backend_used", selected_backend),
            response.get("mode_used", selected_mode),
        )
    return response


def motion_verify_sim_package(
    mechanism_id: str,
    urdf_path: str | None = None,
    doc: str | None = None,
    check_isaac: bool = False,
    prim_path: str | None = None,
) -> dict[str, Any]:
    """Verify that a mechanism exported correctly through the sim pipeline.

    Runs up to 3 verification stages:
    1. Mechanism vs FreeCAD model tree (always, if FreeCAD connected)
    2. Mechanism vs URDF file (if urdf_path provided)
    3. URDF vs Isaac USD scene (if check_isaac=True and Isaac bridge available)

    Returns a report with findings classified as block/warn/note.
    """
    from server.sim_verify import verify_sim_package

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    # Stage 1: Get FreeCAD model tree
    model_tree_bodies: list[dict[str, Any]] | None = None
    try:
        from server.tools_cad import cad_get_model_tree
        tree_result = cad_get_model_tree(doc=doc, detail="bodies")
        if tree_result.get("ok"):
            model_tree_bodies = tree_result.get("bodies", [])
    except Exception as exc:
        log.warning("Could not get model tree for verify: %s", exc)

    # Stage 3: Get Isaac scene state (if requested)
    isaac_diagnose: dict[str, Any] | None = None
    if check_isaac:
        try:
            from server.isaac_client import get_client as get_isaac_client

            client = get_isaac_client()
            if client is not None:
                diag_result = client.send_command(
                    "diagnose",
                    prim_path=prim_path or "/",
                )
                if isinstance(diag_result, dict):
                    isaac_diagnose = diag_result
        except Exception as exc:
            log.warning("Could not get Isaac diagnose for verify: %s", exc)

    result = verify_sim_package(
        mechanism=mech,
        model_tree_bodies=model_tree_bodies,
        urdf_path=urdf_path,
        isaac_diagnose=isaac_diagnose,
    )

    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Auto-profile generation from mechanism
# ---------------------------------------------------------------------------

def _build_profile_from_mechanism(
    mechanism: Mechanism,
    manifest: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Auto-extract a teleop controller profile from a mechanism definition.

    Walks the kinematic tree from the chassis (ground part) through revolute
    joints to identify leg chains, then populates:
    - joint_names / leg_joint_names
    - leg geometry (l_coxa, l_femur, l_tibia)
    - hip mounts
    - body dimensions
    - controller_type
    - tripod phase offsets
    - left_legs / right_legs / tripod_a / tripod_b (for 1-DOF)

    Returns a profile dict suitable for merging with user-provided overrides.
    """
    profile: dict[str, Any] = {}

    # Find ground/chassis part(s)
    ground_ids = {p.id for p in mechanism.parts if p.is_ground}
    if not ground_ids:
        log.debug("_build_profile_from_mechanism: no ground part found")
        return profile

    # Build adjacency: parent_part → [(joint, child_part)]
    children_of: dict[str, list[tuple[JointEdge, str]]] = {}
    for jnt in mechanism.joints:
        children_of.setdefault(jnt.parent_part, []).append((jnt, jnt.child_part))

    # Walk from ground through fixed joints to reach the real chassis
    _MOVABLE_TYPES = {JointType.REVOLUTE, JointType.CONTINUOUS}

    def _walk_chain(part_id: str) -> list[list[JointEdge]]:
        """DFS collecting chains of movable joints."""
        kids = children_of.get(part_id, [])
        movable = [(j, c) for j, c in kids if j.joint_type in _MOVABLE_TYPES]
        fixed = [(j, c) for j, c in kids if j.joint_type not in _MOVABLE_TYPES]

        chains: list[list[JointEdge]] = []
        for _fj, fchild in fixed:
            chains.extend(_walk_chain(fchild))
        for mj, mchild in movable:
            sub_chains = _walk_chain(mchild)
            if sub_chains:
                for sc in sub_chains:
                    chains.append([mj] + sc)
            else:
                chains.append([mj])
        return chains

    all_chains: list[list[JointEdge]] = []
    for gid in ground_ids:
        all_chains.extend(_walk_chain(gid))

    if not all_chains:
        return profile

    # Find the dominant chain length (= dofs_per_leg)
    from collections import Counter
    length_counts = Counter(len(c) for c in all_chains)
    dofs_per_leg = length_counts.most_common(1)[0][0]
    chains = [c for c in all_chains if len(c) == dofs_per_leg]

    # Sort chains into canonical order: left side (y >= 0) before right
    # side (y < 0), front-to-back (descending X) within each side.
    # This produces [LF, LM, LR, RF, RM, RR] which matches the hardcoded
    # leg_phase_offsets and controller expectations regardless of DFS
    # discovery order.
    def _chain_sort_key(chain: list[JointEdge]) -> tuple[int, float]:
        oy = chain[0].origin[1]
        ox = chain[0].origin[0]
        side = 0 if oy >= 0 else 1  # left first
        return (side, -ox)  # front (high X) first within each side

    chains.sort(key=_chain_sort_key)

    n_legs = len(chains)

    if n_legs > 0:
        log.info(
            "_build_profile_from_mechanism: sorted %d chains — order: %s",
            n_legs,
            ", ".join(
                f"{c[0].id}(x={c[0].origin[0]:.1f},y={c[0].origin[1]:.1f})"
                for c in chains
            ),
        )

    profile["dofs_per_leg"] = dofs_per_leg

    # Build leg_joint_names (flat list, groups of dofs_per_leg)
    leg_joint_names: list[str] = []
    hip_mounts: list[list[float]] = []

    for chain in chains:
        for jnt in chain:
            leg_joint_names.append(jnt.id)

        # Compute hip mount from first joint origin (mm → m)
        coxa_origin = chain[0].origin
        hx = coxa_origin[0] / 1000.0
        hy = coxa_origin[1] / 1000.0
        hip_angle = math.atan2(hy, hx)
        hip_mounts.append([hx, hy, hip_angle])

    profile["leg_joint_names"] = leg_joint_names
    profile["hip_mounts"] = hip_mounts

    # Compute segment lengths from joint-to-joint distances (mm → m)
    if dofs_per_leg >= 2 and chains:
        # Use the first leg chain as reference
        ref = chains[0]
        seg_names = ["l_coxa", "l_femur", "l_tibia"]
        for i in range(min(dofs_per_leg - 1, 3)):
            o1 = ref[i].origin
            o2 = ref[i + 1].origin
            dist_m = math.sqrt(
                (o2[0] - o1[0]) ** 2
                + (o2[1] - o1[1]) ** 2
                + (o2[2] - o1[2]) ** 2
            ) / 1000.0
            if i < len(seg_names):
                profile[seg_names[i]] = dist_m

    # Body dimensions from hip positions
    if hip_mounts:
        xs = [abs(m[0]) for m in hip_mounts]
        ys = [abs(m[1]) for m in hip_mounts]
        profile["body_length"] = max(xs) * 2.0 if xs else 0.14
        profile["body_width"] = max(ys) * 2.0 if ys else 0.15

    # Controller type selection
    if dofs_per_leg == 1:
        profile["controller_type"] = "hexapod_1dof_tripod"
    elif dofs_per_leg == 2:
        profile["controller_type"] = "hexapod_2dof_tripod"
    elif dofs_per_leg == 3:
        profile["controller_type"] = "hexapod_3dof_tripod"

    # Tripod phase offsets (alternating pattern for 6 legs)
    if n_legs == 6:
        profile["leg_phase_offsets"] = [0.0, 0.5, 0.0, 0.5, 0.0, 0.5]
    else:
        # Evenly distribute phases
        profile["leg_phase_offsets"] = [i / n_legs for i in range(n_legs)]

    # Left/right classification based on hip Y position
    left_legs: list[str] = []
    right_legs: list[str] = []
    for leg_idx, mount in enumerate(hip_mounts):
        base = leg_idx * dofs_per_leg
        # Use first joint of each leg for left/right classification
        joint_name = leg_joint_names[base]
        if mount[1] >= 0:
            left_legs.append(joint_name)
        else:
            right_legs.append(joint_name)
    profile["left_legs"] = left_legs
    profile["right_legs"] = right_legs

    # Set joint_names for all DOF counts so the DOF mapping in the
    # Isaac runtime can always find the right names to resolve.
    profile["joint_names"] = leg_joint_names

    # For 1-DOF: also set tripod_a, tripod_b
    if dofs_per_leg == 1:
        # Alternate tripod groups
        tripod_a: list[str] = []
        tripod_b: list[str] = []
        for i, jname in enumerate(leg_joint_names):
            if i % 2 == 0:
                tripod_a.append(jname)
            else:
                tripod_b.append(jname)
        profile["tripod_a"] = tripod_a
        profile["tripod_b"] = tripod_b

    log.info(
        "_build_profile_from_mechanism: n_legs=%d dofs_per_leg=%d controller=%s",
        n_legs, dofs_per_leg, profile.get("controller_type", "unknown"),
    )
    return profile


def motion_teleop_start(
    mechanism_id: str,
    backend: str = "isaac",
    profile: dict[str, Any] | None = None,
    urdf_path: str | None = None,
    sdf_path: str | None = None,
    import_config: dict[str, Any] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Start a teleop session for a mechanism (Isaac or Gazebo).

    The ``profile`` dict configures the teleop controller.  All keys are
    optional and default to a 1-DOF hexapod tripod gait.  Profile keys:

    - ``controller_type`` (str): Controller selection (default ``"hexapod_1dof_tripod"``).
    - ``joint_names`` (list[str]): Ordered joint names for the controller.
    - ``tripod_a`` / ``tripod_b`` (list[str]): Tripod leg groups (must partition ``joint_names``).
    - ``left_legs`` / ``right_legs`` (list[str]): Left/right leg groups for yaw differential.
    - ``neutral_deg`` (float): Neutral joint angle in degrees.
    - ``amplitude_deg`` (float, >0): Oscillation amplitude in degrees.
    - ``stride_hz`` (float, >0): Gait cycle frequency in Hz.
    - ``yaw_mix_deg`` (float, >=0): Yaw differential gain in degrees.
    - ``height_mix_deg`` (float, >=0): Height offset gain in degrees.
    - ``vx_max_mps`` (float, >0): Maximum forward velocity in m/s.
    - ``yaw_max_rps`` (float, >0): Maximum yaw rate in rad/s.
    - ``height_max_m`` (float, >0): Maximum body height in m.
    - ``slew_vx_mps2`` (float, >0): Forward velocity slew rate in m/s².
    - ``slew_yaw_rps2`` (float, >0): Yaw slew rate in rad/s².
    - ``slew_height_mps2`` (float, >0): Height slew rate in m/s².

    Response includes ``session_id`` and backend-specific telemetry.
    Gazebo profile contract:
    - ``controller_type`` must be ``multirotor_direct`` or ``px4_offboard``.
    """
    selected_backend = _normalize_backend(backend)
    if selected_backend not in _TELEOP_BACKENDS:
        return _error_result(
            "INVALID_INPUT",
            f"motion.teleop_start only supports backends {sorted(_TELEOP_BACKENDS)}",
        )
    if profile is not None and not isinstance(profile, dict):
        return _error_result("INVALID_INPUT", "profile must be an object")
    profile_obj = profile or {}

    mech = store_get(mechanism_id)
    if mech is None:
        return _error_result("NOT_FOUND", f"No mechanism with id '{mechanism_id}'")

    # Auto-populate profile from mechanism when fields are missing.
    # Only for Isaac backend — Gazebo uses a different controller model
    # (multirotor_direct, px4_offboard) that doesn't match hexapod profiles.
    if selected_backend == "isaac":
        auto_profile = _build_profile_from_mechanism(mech)
        if auto_profile:
            merged = {**auto_profile, **profile_obj}
            profile_obj = merged

        # Auto-compute spawn_height from stance geometry so the robot
        # doesn't clip through the ground on the first physics step.
        ic = import_config or {}
        if "spawn_height" not in ic:
            stance_h = profile_obj.get("stance_height", -0.09)
            margin = 0.02  # 2 cm clearance above ground
            computed_spawn = abs(stance_h) + margin
            ic = {**ic, "spawn_height": computed_spawn}
            log.info(
                "motion_teleop_start: auto spawn_height=%.4f "
                "(|stance_height|=%.4f + margin=%.4f)",
                computed_spawn, abs(stance_h), margin,
            )
            import_config = ic

        # Default to mobile robot type so Isaac uses free-base defaults
        if "robot_type" not in ic:
            import_config = {**(import_config or {}), "robot_type": "mobile"}

    if selected_backend == "gazebo":
        path_error = _validate_gazebo_sim_paths(urdf_path, sdf_path)
        if path_error is not None:
            return _error_result("INVALID_INPUT", path_error)

        controller_type = str(profile_obj.get("controller_type", "multirotor_direct")).strip().lower()
        if controller_type not in _GAZEBO_CONTROLLER_TYPES:
            return _error_result(
                "INVALID_INPUT",
                (
                    "For Gazebo teleop, profile.controller_type must be "
                    "'multirotor_direct' or 'px4_offboard'."
                ),
            )

    preflight = _run_sdf_preflight(sdf_path) if _has_sim_path(sdf_path) else _run_urdf_preflight(urdf_path)
    if preflight and preflight["blockers"]:
        return _error_result(
            "URDF_VALIDATION_FAILED" if not _has_sim_path(sdf_path) else "SDF_VALIDATION_FAILED",
            f"Model pre-flight found {len(preflight['blockers'])} blocker(s): "
            + "; ".join(b["message"] for b in preflight["blockers"]),
        )

    if selected_backend == "gazebo":
        from server import gazebo_adapter

        result = gazebo_adapter.teleop_start(
            mechanism=mech,
            profile=profile_obj,
            urdf_path=urdf_path,
            sdf_path=sdf_path,
            import_config=import_config,
            verify=verify,
        )
        not_connected_code = "GAZEBO_NOT_CONNECTED"
        protocol_error_code = "GAZEBO_PROTOCOL_ERROR"
    else:
        from server import isaac_adapter

        result = isaac_adapter.teleop_start(
            mechanism=mech,
            profile=profile_obj,
            urdf_path=urdf_path,
            import_config=import_config,
            verify=verify,
        )
        not_connected_code = "ISAAC_NOT_CONNECTED"
        protocol_error_code = "ISAAC_PROTOCOL_ERROR"

    if not result.get("ok", False):
        err = result.get("error", {})
        if err.get("code") == not_connected_code:
            return _backend_unavailable_result(
                selected_backend,
                err.get("message", f"{selected_backend} bridge is not available."),
                unavailable_code=not_connected_code,
            )
        return result

    session_id_val = str(result.get("session_id", "")).strip()
    if not session_id_val:
        return _error_result(protocol_error_code, f"{selected_backend} teleop response missing session_id")

    _active_sessions[session_id_val] = {
        "mechanism_id": mechanism_id,
        "backend": selected_backend,
        "created_at": time.time(),
    }
    response: dict[str, Any] = {
        "ok": True,
        "backend_used": selected_backend,
        "mode_used": "teleop",
        **result,
    }
    # Include URDF pre-flight findings (non-blocker warnings/notes)
    if preflight:
        response["urdf_validation" if not _has_sim_path(sdf_path) else "sdf_validation"] = preflight
    return response


def motion_teleop_command(
    session_id: str,
    vx_mps: float = 0.0,
    yaw_rate_rps: float = 0.0,
    body_height_m: float = 0.0,
    vy_mps: float = 0.0,
    vz_mps: float = 0.0,
) -> dict[str, Any]:
    """Send a teleop command to an active teleop session.

    Commands are rate-limited by the controller's slew filters and
    clamped to the profile's maxima (``vx_max_mps``, ``yaw_max_rps``,
    ``height_max_m``).  The controller applies them on the next tick.
    """
    session = _active_sessions.get(session_id)
    if session is None:
        return _error_result("NOT_FOUND", f"No active teleop session '{session_id}'")

    session_backend = session.get("backend", "isaac")

    # Isaac does not support vy_mps / vz_mps — reject non-zero values
    if session_backend == "isaac" and (vy_mps != 0.0 or vz_mps != 0.0):
        return _error_result(
            "INVALID_INPUT",
            "Isaac backend does not support vy_mps/vz_mps; only Gazebo accepts lateral/vertical velocities.",
        )

    if session_backend == "gazebo":
        from server import gazebo_adapter

        result = gazebo_adapter.teleop_command(
            session_id=session_id,
            vx_mps=vx_mps,
            yaw_rate_rps=yaw_rate_rps,
            body_height_m=body_height_m,
            vy_mps=vy_mps,
            vz_mps=vz_mps,
        )
    elif session_backend == "isaac":
        from server import isaac_adapter

        result = isaac_adapter.teleop_command(
            session_id=session_id,
            vx_mps=vx_mps,
            yaw_rate_rps=yaw_rate_rps,
            body_height_m=body_height_m,
        )
    else:
        return _error_result("NOT_FOUND", f"Unknown session backend '{session_backend}'")

    if not result.get("ok", False):
        if _is_unknown_session_error(result):
            _active_sessions.pop(session_id, None)
        return result
    return {"ok": True, "backend_used": session_backend, "mode_used": "teleop", **result}


def motion_teleop_state(session_id: str) -> dict[str, Any]:
    """Read current state from an active teleop session.

    Returns commanded state (``vx_mps``, ``yaw_rate_rps``, ``body_height_m``),
    ``uptime_s``, and teleop telemetry: ``controller_type``, ``joint_names``,
    ``tick_count``, ``limit_clamp_count``, ``last_joint_targets_rad`` (dict
    of joint name → target in radians), and ``last_apply_ok`` (bool).
    """
    session = _active_sessions.get(session_id)
    if session is None:
        return _error_result("NOT_FOUND", f"No active teleop session '{session_id}'")

    session_backend = session.get("backend", "isaac")

    if session_backend == "gazebo":
        from server import gazebo_adapter
        result = gazebo_adapter.teleop_state(session_id=session_id)
    elif session_backend == "isaac":
        from server import isaac_adapter
        result = isaac_adapter.teleop_state(session_id=session_id)
    else:
        return _error_result("NOT_FOUND", f"Unknown session backend '{session_backend}'")

    if not result.get("ok", False):
        if _is_unknown_session_error(result):
            _active_sessions.pop(session_id, None)
        return result
    return {"ok": True, "backend_used": session_backend, "mode_used": "teleop", **result}


def motion_teleop_stop(session_id: str) -> dict[str, Any]:
    """Stop and remove an active teleop session.

    Returns final telemetry: ``stopped``, ``controller_type``,
    ``tick_count``, ``limit_clamp_count``, ``last_joint_targets_rad``.
    Cleans up engine resources so a subsequent ``teleop_start`` can
    create a fresh session.
    """
    session = _active_sessions.get(session_id)
    if session is None:
        return _error_result("NOT_FOUND", f"No active teleop session '{session_id}'")

    session_backend = session.get("backend", "isaac")

    if session_backend == "gazebo":
        from server import gazebo_adapter
        result = gazebo_adapter.teleop_stop(session_id=session_id)
    elif session_backend == "isaac":
        from server import isaac_adapter
        result = isaac_adapter.teleop_stop(session_id=session_id)
    else:
        return _error_result("NOT_FOUND", f"Unknown session backend '{session_backend}'")

    if not result.get("ok", False):
        if _is_unknown_session_error(result):
            _active_sessions.pop(session_id, None)
        return result
    _active_sessions.pop(session_id, None)
    return {"ok": True, "backend_used": session_backend, "mode_used": "teleop", **result}


# ---------------------------------------------------------------------------
# Isaac viewport screenshot
# ---------------------------------------------------------------------------

def motion_isaac_screenshot(
    width: int = 1280,
    height: int = 720,
    camera_position: list[float] | None = None,
    camera_target: list[float] | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Capture the Isaac Sim viewport and return as base64 PNG.

    *target* is a preset name (``"iso"``, ``"front"``, ``"top"``,
    ``"right"``, ``"back"``, ``"bottom"``, ``"left"``).  When set and
    no explicit ``camera_position`` is given, the camera auto-frames
    from that direction.

    The returned dict includes ``image_base64`` which the MCP response
    handler in ``server/main.py`` automatically extracts into an MCP
    image content block.
    """
    from server import isaac_adapter

    return isaac_adapter.isaac_screenshot(
        width=width,
        height=height,
        camera_position=camera_position,
        camera_target=camera_target,
        preset=target,
    )


def motion_isaac_launch(
    headless: bool = False,
    port: int = 9878,
    environment: str = "full_warehouse.usd",
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Launch the Isaac bridge as a managed subprocess.

    Spawns the bridge process and waits for it to accept TCP connections.
    If the bridge is already running, returns immediately.
    """
    from server import isaac_adapter

    return isaac_adapter.launch_bridge(
        headless=headless,
        port=port,
        environment=environment,
        timeout_s=timeout_s,
    )


def motion_isaac_stop() -> dict[str, Any]:
    """Stop the managed Isaac bridge subprocess."""
    from server import isaac_adapter

    return isaac_adapter.stop_bridge()
