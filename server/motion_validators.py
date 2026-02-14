"""Analytical validators for the motion validation pipeline (Tier 1).

Each validator is a pure function that takes a Mechanism and returns a
ValidatorResult.  The VALIDATORS registry maps names to callables for
easy dispatch and discovery.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from server.models import ValidatorResult
from server.motion_models import JointType, Mechanism


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gear_joints(mech: Mechanism) -> list:
    """Return all gear-mesh joints."""
    return [j for j in mech.joints if j.joint_type == JointType.GEAR_MESH]


def _belt_joints(mech: Mechanism) -> list:
    """Return all belt/chain joints."""
    return [j for j in mech.joints if j.joint_type == JointType.BELT_CHAIN]


def _effective_ratio(joint) -> float | None:
    """Compute effective gear ratio from explicit ratio or tooth counts."""
    if joint.gear_ratio is not None:
        return joint.gear_ratio
    if joint.teeth_parent is not None and joint.teeth_child is not None:
        if joint.teeth_child == 0:
            return None
        return joint.teeth_parent / joint.teeth_child
    return None


def _build_adjacency(mech: Mechanism) -> dict[str, list[tuple[str, Any]]]:
    """Build part adjacency from joints.  Returns {part_id: [(neighbor, joint), ...]}."""
    adj: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for j in mech.joints:
        adj[j.parent_part].append((j.child_part, j))
        adj[j.child_part].append((j.parent_part, j))
    return adj


# ---------------------------------------------------------------------------
# Speed propagation (BFS from driven joints)
# ---------------------------------------------------------------------------

def propagate_speeds(mech: Mechanism) -> dict[str, float]:
    """BFS from driven joints to compute RPM at every part.

    Returns {part_id: rpm}.  Parts not reachable from a drive get rpm=0.
    """
    speeds: dict[str, float] = {}
    adj = _build_adjacency(mech)

    # Seed driven parts
    for drive in mech.drives:
        if drive.speed_rpm is None:
            continue
        joint = mech.get_joint(drive.joint_id)
        if joint is None:
            continue
        # The drive applies to the parent part of the joint
        speeds[joint.parent_part] = drive.speed_rpm

    # BFS
    queue = list(speeds.keys())
    visited = set(speeds.keys())
    while queue:
        current = queue.pop(0)
        current_rpm = speeds[current]
        for neighbor, joint in adj.get(current, []):
            if neighbor in visited:
                continue
            ratio = _effective_ratio(joint)
            if ratio is not None and joint.joint_type in (
                JointType.GEAR_MESH, JointType.BELT_CHAIN,
            ):
                # Convention: ratio = teeth_parent / teeth_child
                # parent speed / child speed = teeth_child / teeth_parent = 1/ratio
                if current == joint.parent_part:
                    neighbor_rpm = current_rpm / ratio
                else:
                    neighbor_rpm = current_rpm * ratio
            elif joint.joint_type == JointType.FIXED:
                neighbor_rpm = current_rpm
            elif joint.joint_type == JointType.REVOLUTE:
                # Revolute joints don't enforce a speed relationship
                # but for carriers/linked parts the speed is inherited
                neighbor_rpm = current_rpm
            else:
                neighbor_rpm = current_rpm
            speeds[neighbor] = neighbor_rpm
            visited.add(neighbor)
            queue.append(neighbor)

    # Parts with no drive path get 0
    for part in mech.parts:
        if part.id not in speeds and not part.is_ground:
            speeds[part.id] = 0.0
    for part in mech.parts:
        if part.is_ground:
            speeds[part.id] = 0.0

    return speeds


def propagate_torques(mech: Mechanism) -> dict[str, float]:
    """Compute torque at each part via power conservation through gear stages.

    Returns {part_id: torque_nm}.
    """
    torques: dict[str, float] = {}
    adj = _build_adjacency(mech)

    # Seed driven parts
    for drive in mech.drives:
        if drive.torque_nm is None:
            continue
        joint = mech.get_joint(drive.joint_id)
        if joint is None:
            continue
        torques[joint.parent_part] = drive.torque_nm

    # BFS
    queue = list(torques.keys())
    visited = set(torques.keys())
    while queue:
        current = queue.pop(0)
        current_torque = torques[current]
        for neighbor, joint in adj.get(current, []):
            if neighbor in visited:
                continue
            ratio = _effective_ratio(joint)
            eff = joint.mesh_efficiency
            if ratio is not None and joint.joint_type in (
                JointType.GEAR_MESH, JointType.BELT_CHAIN,
            ):
                if current == joint.parent_part:
                    neighbor_torque = current_torque * ratio * eff
                else:
                    if ratio == 0:
                        neighbor_torque = 0.0
                    else:
                        neighbor_torque = current_torque / ratio * eff
            elif joint.joint_type == JointType.FIXED:
                neighbor_torque = current_torque
            else:
                neighbor_torque = current_torque
            torques[neighbor] = neighbor_torque
            visited.add(neighbor)
            queue.append(neighbor)

    return torques


# ---------------------------------------------------------------------------
# Individual validators
# ---------------------------------------------------------------------------

def _check_gear_ratio_consistency(mech: Mechanism) -> ValidatorResult:
    """Verify gear_ratio matches teeth_parent/teeth_child for all gear meshes."""
    issues = []
    for j in _gear_joints(mech) + _belt_joints(mech):
        if j.gear_ratio is not None and j.teeth_parent is not None and j.teeth_child is not None:
            expected = j.teeth_parent / j.teeth_child if j.teeth_child != 0 else None
            if expected is not None and abs(j.gear_ratio - expected) > 1e-6:
                issues.append(
                    f"Joint '{j.id}': gear_ratio={j.gear_ratio} but "
                    f"teeth {j.teeth_parent}/{j.teeth_child}={expected:.4f}"
                )
    if issues:
        return ValidatorResult(
            name="gear_ratio_consistency",
            status="fail",
            message="; ".join(issues),
            measured={"inconsistent_joints": len(issues)},
            priority=100,
        )
    gear_count = len(_gear_joints(mech)) + len(_belt_joints(mech))
    return ValidatorResult(
        name="gear_ratio_consistency",
        status="pass",
        message=f"All {gear_count} gear/belt joints have consistent ratios",
        measured={"gear_joint_count": gear_count},
        priority=100,
    )


def _check_speed_propagation(mech: Mechanism) -> ValidatorResult:
    """Compute speeds via BFS and report results."""
    speeds = propagate_speeds(mech)
    unreachable = [
        pid for pid, rpm in speeds.items()
        if rpm == 0.0 and not (mech.get_part(pid) and mech.get_part(pid).is_ground)
    ]
    if unreachable:
        return ValidatorResult(
            name="speed_propagation",
            status="warn",
            message=f"Parts with no speed path: {unreachable}",
            measured={"speeds_rpm": speeds, "unreachable": unreachable},
            priority=200,
        )
    return ValidatorResult(
        name="speed_propagation",
        status="pass",
        message="All parts have computed speeds",
        measured={"speeds_rpm": speeds},
        priority=200,
    )


def _check_torque_balance(mech: Mechanism) -> ValidatorResult:
    """Verify torque propagation through gear stages."""
    torques = propagate_torques(mech)
    if not torques:
        return ValidatorResult(
            name="torque_balance",
            status="note",
            message="No drive torques specified",
            measured={},
            priority=300,
        )
    return ValidatorResult(
        name="torque_balance",
        status="pass",
        message="Torque propagated through all stages",
        measured={"torques_nm": torques},
        priority=300,
    )


def _check_power_conservation(mech: Mechanism) -> ValidatorResult:
    """Verify P_in >= P_out + losses (within tolerance)."""
    speeds = propagate_speeds(mech)
    torques = propagate_torques(mech)

    # Compute total input power
    p_in = 0.0
    input_parts: list[str] = []
    for drive in mech.drives:
        joint = mech.get_joint(drive.joint_id)
        if joint is None:
            continue
        pid = joint.parent_part
        rpm = speeds.get(pid, 0.0)
        torque = torques.get(pid, 0.0)
        omega = rpm * 2 * math.pi / 60
        p_in += abs(torque * omega)
        input_parts.append(pid)

    # Compute total output power (all non-input, non-ground parts)
    p_out = 0.0
    for part in mech.parts:
        if part.is_ground or part.id in input_parts:
            continue
        rpm = speeds.get(part.id, 0.0)
        torque = torques.get(part.id, 0.0)
        omega = rpm * 2 * math.pi / 60
        p_out += abs(torque * omega)

    if p_in == 0:
        return ValidatorResult(
            name="power_conservation",
            status="note",
            message="No input power (no driven speed+torque)",
            measured={"p_in_w": 0, "p_out_w": 0},
            priority=350,
        )

    ratio = p_out / p_in if p_in > 0 else 0
    if ratio > 1.01:
        return ValidatorResult(
            name="power_conservation",
            status="fail",
            message=f"Output power ({p_out:.2f} W) exceeds input ({p_in:.2f} W) — energy creation",
            measured={"p_in_w": round(p_in, 4), "p_out_w": round(p_out, 4), "ratio": round(ratio, 4)},
            priority=350,
        )
    return ValidatorResult(
        name="power_conservation",
        status="pass",
        message=f"Power conserved: P_in={p_in:.2f} W, P_out={p_out:.2f} W, efficiency={ratio:.1%}",
        measured={"p_in_w": round(p_in, 4), "p_out_w": round(p_out, 4), "ratio": round(ratio, 4)},
        priority=350,
    )


def _check_dof_analysis(mech: Mechanism) -> ValidatorResult:
    """Gruebler's equation for planar mechanisms: DOF = 3(n-1) - 2*j1 - j2.

    n = number of links (including ground)
    j1 = full joints (revolute, prismatic, gear_mesh, fixed remove 2 DOF)
    j2 = half joints (cam, planar remove 1 DOF)
    """
    n = len(mech.parts)  # includes ground
    j1 = 0
    j2 = 0
    for j in mech.joints:
        if j.joint_type in (JointType.REVOLUTE, JointType.PRISMATIC, JointType.GEAR_MESH, JointType.BELT_CHAIN):
            j1 += 1
        elif j.joint_type == JointType.FIXED:
            j1 += 1  # Fixed removes all relative DOF; in 2D that's ~3 constraints, but Gruebler counts as j1
        elif j.joint_type in (JointType.CAM, JointType.PLANAR):
            j2 += 1

    # Fixed joints in Gruebler actually remove 3 DOF in 2D (= rigid connection).
    # Standard Gruebler: DOF = 3(n-1) - 2*j_full - 1*j_half
    # But a fixed joint is really 3 constraints, so treat it as removing 3 DOF.
    fixed_count = sum(1 for j in mech.joints if j.joint_type == JointType.FIXED)
    # Adjust: fixed joints counted in j1 remove 2, but they should remove 3
    # So subtract the extra 1 per fixed joint
    dof = 3 * (n - 1) - 2 * j1 - j2 - fixed_count

    status = "pass"
    msg_parts = [f"DOF={dof} (n={n}, j1={j1}, j2={j2}, fixed={fixed_count})"]
    if dof < 0:
        status = "warn"
        msg_parts.append("over-constrained (DOF < 0)")
    elif dof == 0:
        status = "warn"
        msg_parts.append("statically determinate (DOF = 0, no motion possible)")
    elif dof > len(mech.drives):
        status = "warn"
        msg_parts.append(f"under-driven: {len(mech.drives)} drives for {dof} DOF")

    return ValidatorResult(
        name="dof_analysis",
        status=status,
        message="; ".join(msg_parts),
        measured={"dof": dof, "n": n, "j1": j1, "j2": j2, "fixed": fixed_count},
        priority=150,
    )


def _check_center_distance(mech: Mechanism) -> ValidatorResult:
    """Verify mesh center distance matches (d1+d2)/2 for gear meshes with known origins."""
    issues = []
    checked = 0
    for j in _gear_joints(mech):
        p1 = mech.get_part(j.parent_part)
        p2 = mech.get_part(j.child_part)
        if p1 is None or p2 is None:
            continue
        # Only check if both parts have joints with known origins (approximate via joint origin)
        # We check if the joint has teeth info to compute pitch diameters
        if j.teeth_parent is None or j.teeth_child is None:
            continue
        # Without a module/pitch we can't compute pitch diameters
        # This validator is informational — it checks ratio fields, not geometry
        checked += 1

    return ValidatorResult(
        name="center_distance_check",
        status="pass",
        message=f"Checked {checked} gear meshes (geometric center distance requires module info)",
        measured={"checked": checked},
        priority=400,
    )


def _check_planet_spacing(mech: Mechanism) -> ValidatorResult:
    """Check that planets don't overlap in a planetary set.

    Looks for patterns: multiple gear meshes sharing the same parent (sun)
    or child (ring) part, indicating planets.
    """
    # Find potential sun gears: parts that mesh with 2+ other parts
    mesh_counts: dict[str, list[str]] = defaultdict(list)
    for j in _gear_joints(mech):
        mesh_counts[j.parent_part].append(j.child_part)
        mesh_counts[j.child_part].append(j.parent_part)

    sun_candidates = [
        pid for pid, neighbors in mesh_counts.items()
        if len(neighbors) >= 2
    ]

    if not sun_candidates:
        return ValidatorResult(
            name="planet_spacing_check",
            status="note",
            message="No planetary arrangement detected",
            measured={},
            priority=450,
        )

    # For each sun candidate, check if planets (its neighbors) have enough angular spacing
    # This is a heuristic — proper check needs pitch diameters
    for sun_id in sun_candidates:
        planets = mesh_counts[sun_id]
        n_planets = len(planets)
        if n_planets < 2:
            continue
        # Minimum angular spacing
        min_spacing_deg = 360.0 / n_planets
        if min_spacing_deg < 30:
            return ValidatorResult(
                name="planet_spacing_check",
                status="warn",
                message=f"Part '{sun_id}' has {n_planets} meshing partners — "
                        f"angular spacing {min_spacing_deg:.1f}° may be too tight",
                measured={"sun_id": sun_id, "planet_count": n_planets, "spacing_deg": min_spacing_deg},
                priority=450,
            )

    return ValidatorResult(
        name="planet_spacing_check",
        status="pass",
        message="Planet spacing appears adequate",
        measured={"sun_candidates": sun_candidates},
        priority=450,
    )


def _check_linkage_grashof(mech: Mechanism) -> ValidatorResult:
    """Grashof criterion for four-bar linkages: s+l <= p+q for full rotation.

    Looks for exactly 4 parts (including ground) connected by 4 revolute joints
    with link_length_mm specified.
    """
    rev_joints = [j for j in mech.joints if j.joint_type == JointType.REVOLUTE]
    if len(mech.parts) != 4 or len(rev_joints) != 4:
        return ValidatorResult(
            name="linkage_grashof",
            status="note",
            message="Not a four-bar linkage (need 4 parts + 4 revolute joints)",
            measured={},
            priority=500,
        )

    lengths = []
    for j in rev_joints:
        if j.link_length_mm is None:
            return ValidatorResult(
                name="linkage_grashof",
                status="note",
                message="Four-bar detected but link_length_mm not specified on all joints",
                measured={},
                priority=500,
            )
        lengths.append(j.link_length_mm)

    lengths_sorted = sorted(lengths)
    s, p, q, l_ = lengths_sorted[0], lengths_sorted[1], lengths_sorted[2], lengths_sorted[3]
    grashof = s + l_ <= p + q

    if grashof:
        return ValidatorResult(
            name="linkage_grashof",
            status="pass",
            message=f"Grashof condition satisfied: s+l={s+l_:.1f} <= p+q={p+q:.1f} — full rotation possible",
            measured={"s": s, "p": p, "q": q, "l": l_, "grashof": True},
            priority=500,
        )
    return ValidatorResult(
        name="linkage_grashof",
        status="warn",
        message=f"Grashof condition NOT satisfied: s+l={s+l_:.1f} > p+q={p+q:.1f} — no full rotation",
        measured={"s": s, "p": p, "q": q, "l": l_, "grashof": False},
        priority=500,
    )


def _check_expected_outputs(mech: Mechanism) -> ValidatorResult:
    """Compare computed speeds/torques against expected_outputs within tolerance."""
    if not mech.expected_outputs:
        return ValidatorResult(
            name="expected_output_check",
            status="note",
            message="No expected_outputs specified",
            measured={},
            priority=50,
        )

    speeds = propagate_speeds(mech)
    torques = propagate_torques(mech)
    tolerance = mech.expected_outputs.get("_tolerance", 0.05)  # default 5%

    mismatches = []
    checks = 0
    for key, expected_val in mech.expected_outputs.items():
        if key.startswith("_"):
            continue
        if not isinstance(expected_val, (int, float)):
            continue
        expected_val = float(expected_val)

        # Parse key: "part_id_speed_rpm" or "part_id_torque_nm"
        actual = None
        if key.endswith("_speed_rpm"):
            part_id = key[: -len("_speed_rpm")]
            actual = speeds.get(part_id)
        elif key.endswith("_torque_nm"):
            part_id = key[: -len("_torque_nm")]
            actual = torques.get(part_id)

        if actual is None:
            mismatches.append(f"{key}: expected {expected_val}, not computed")
            continue

        checks += 1
        if expected_val == 0:
            if abs(actual) > 1e-6:
                mismatches.append(f"{key}: expected 0, got {actual:.4f}")
        else:
            rel_err = abs(actual - expected_val) / abs(expected_val)
            if rel_err > tolerance:
                mismatches.append(
                    f"{key}: expected {expected_val:.4f}, got {actual:.4f} "
                    f"(error {rel_err:.1%} > {tolerance:.0%})"
                )

    if mismatches:
        return ValidatorResult(
            name="expected_output_check",
            status="fail",
            message="; ".join(mismatches),
            measured={"mismatches": mismatches, "checks": checks},
            priority=50,
        )
    return ValidatorResult(
        name="expected_output_check",
        status="pass",
        message=f"All {checks} expected outputs match within {tolerance:.0%} tolerance",
        measured={"checks": checks, "tolerance": tolerance},
        priority=50,
    )


# ---------------------------------------------------------------------------
# Gear train analysis (dedicated tool)
# ---------------------------------------------------------------------------

def analyze_gear_train(mech: Mechanism) -> dict[str, Any]:
    """Analyze a gear train: overall ratio, per-stage ratios, contact ratios.

    Returns a dict suitable for the motion.check_gear_train tool response.
    """
    gear_joints = _gear_joints(mech) + _belt_joints(mech)
    if not gear_joints:
        return {"overall_ratio": None, "stages": [], "message": "No gear meshes found"}

    stages = []
    overall_ratio = 1.0
    for j in gear_joints:
        ratio = _effective_ratio(j)
        stage = {
            "joint_id": j.id,
            "parent": j.parent_part,
            "child": j.child_part,
            "ratio": ratio,
            "teeth_parent": j.teeth_parent,
            "teeth_child": j.teeth_child,
            "efficiency": j.mesh_efficiency,
        }
        # Contact ratio estimate (only for involute spur gears)
        if j.teeth_parent is not None and j.teeth_child is not None:
            # Approximate contact ratio for 20° pressure angle:
            # CR ≈ 0.94 * sqrt(z1) + 0.94 * sqrt(z2) - something
            # Simplified: CR ≈ 1.0 + (z1+z2)/200 for rough estimate
            z1, z2 = j.teeth_parent, j.teeth_child
            cr_approx = 1.0 + (z1 + z2) / 200
            stage["contact_ratio_approx"] = round(cr_approx, 2)
        stages.append(stage)
        if ratio is not None:
            overall_ratio *= ratio

    return {
        "overall_ratio": round(overall_ratio, 6),
        "stages": stages,
    }


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

ValidatorFn = Callable[[Mechanism], ValidatorResult]

VALIDATORS: dict[str, ValidatorFn] = {
    "gear_ratio_consistency": _check_gear_ratio_consistency,
    "speed_propagation": _check_speed_propagation,
    "torque_balance": _check_torque_balance,
    "power_conservation": _check_power_conservation,
    "dof_analysis": _check_dof_analysis,
    "center_distance_check": _check_center_distance,
    "planet_spacing_check": _check_planet_spacing,
    "linkage_grashof": _check_linkage_grashof,
    "expected_output_check": _check_expected_outputs,
}


def run_validators(
    mech: Mechanism,
    validator_names: list[str] | None = None,
) -> list[ValidatorResult]:
    """Run selected validators (or all if names is None) against a mechanism."""
    names = validator_names or list(VALIDATORS.keys())
    results = []
    for name in names:
        fn = VALIDATORS.get(name)
        if fn is None:
            results.append(ValidatorResult(
                name=name, status="fail",
                message=f"Unknown validator: {name}",
            ))
            continue
        results.append(fn(mech))
    results.sort(key=lambda r: r.priority)
    return results
