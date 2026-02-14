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
from server.motion_validators import (
    analyze_gear_train,
    propagate_speeds,
    propagate_torques,
    run_validators,
)

log = logging.getLogger("solidmind.tools_motion")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))


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

        # Add joints
        joint_names: list[str] = []
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
                "element_a": "Face1",  # Default face reference
                "part_b": link_b,
                "element_b": "Face1",
                "name": joint.id,
            }
            if joint.gear_ratio is not None:
                joint_kwargs["ratio"] = joint.gear_ratio
            if doc is not None:
                joint_kwargs["doc"] = doc

            try:
                j_result = client.send_command("assembly_add_joint", **joint_kwargs)
                joint_names.append(j_result["joint_name"])
            except FreeCADCommandError as exc:
                warnings.append(f"Joint '{joint.id}': failed — {exc}")

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


def motion_drive_joint(
    mechanism_id: str,
    joint_id: str,
    value: float,
    steps: int = 10,
    doc: str | None = None,
) -> dict[str, Any]:
    """Drive a joint through a range of values with visual verification.

    For revolute joints, value is the total rotation in degrees.
    For prismatic joints, value is total translation in mm.
    Captures screenshots at each step for visual inspection.
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

    from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

    try:
        client = get_client()

        # We need the assembly name — look for Asm_ prefixed object
        # The caller should know the assembly name, but we can try to find it
        drive_kwargs: dict[str, Any] = {
            "assembly": f"Asm_{mech.name}",
            "joint": joint_id,
            "value": value,
            "steps": steps,
        }
        if doc is not None:
            drive_kwargs["doc"] = doc

        result = client.send_command("assembly_drive_joint", **drive_kwargs)

        if _TOOL_LOG:
            log.info(
                "OK   motion_drive_joint %.3fs steps=%d screenshots=%d",
                time.monotonic() - t0, steps, len(result.get("screenshots", [])),
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
    from server.chrono_client import ChronoConnectionError, get_client

    client = get_client()
    if client is None:
        return _error_result(
            "CHRONO_NOT_CONNECTED",
            "Chrono daemon not running on localhost:9877. "
            "Start the chrono_daemon binary to enable Tier 3 dynamic validation. "
            "Tier 1 (analytical) and Tier 2 (kinematic) validation are still available.",
        )

    try:
        result = client.simulate(
            mechanism=mech.to_dict(),
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
    except ChronoConnectionError as exc:
        return _error_result("CHRONO_CONNECTION_LOST", str(exc))
    except Exception as exc:
        return _error_result("CHRONO_ERROR", str(exc))

    if _TOOL_LOG:
        samples = len(result.get("time_series", []))
        log.info(
            "OK   motion_simulate %.3fs samples=%d",
            time.monotonic() - t0, samples,
        )

    return {"ok": True, **result}
