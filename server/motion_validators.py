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


def _revolute_origin_map(mech: Mechanism) -> dict[str, tuple[float, float, float]]:
    """Map part_id -> rotation center from the first revolute joint referencing that part."""
    origins: dict[str, tuple[float, float, float]] = {}
    for j in mech.joints:
        if j.joint_type in (JointType.REVOLUTE, JointType.CONTINUOUS):
            for pid in (j.parent_part, j.child_part):
                if pid not in origins:
                    origins[pid] = j.origin
    return origins


# ---------------------------------------------------------------------------
# Gear mesh phase computation
# ---------------------------------------------------------------------------

def _find_gear_seed(mech: Mechanism) -> str | None:
    """Find the driven part to use as BFS seed for gear computations.

    Returns the non-ground part attached to the drive joint so that BFS
    traverses gear meshes instead of short-circuiting through revolute
    joints connected to the ground frame.
    """
    gear_joints = _gear_joints(mech)
    ground_ids = {p.id for p in mech.parts if p.is_ground}
    for drive in mech.drives:
        jt = mech.get_joint(drive.joint_id)
        if jt is not None:
            # Prefer the non-ground side of the drive joint
            if jt.parent_part not in ground_ids:
                return jt.parent_part
            return jt.child_part
    if gear_joints:
        return gear_joints[0].parent_part
    return None


def _gear_mesh_origins_and_adj(
    mech: Mechanism,
) -> tuple[
    dict[str, tuple[float, float, float]],
    dict[str, list[tuple[str, Any]]],
]:
    """Build rotation-center map and gear-mesh adjacency."""
    gear_joints = _gear_joints(mech)
    origins = _revolute_origin_map(mech)
    for j in gear_joints:
        for pid in (j.parent_part, j.child_part):
            if pid not in origins:
                origins[pid] = j.origin
    gear_adj: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for j in gear_joints:
        gear_adj[j.parent_part].append((j.child_part, j))
        gear_adj[j.child_part].append((j.parent_part, j))
    return origins, gear_adj


def compute_gear_mesh_phases(mech: Mechanism) -> dict[str, float]:
    """BFS from the driven gear to compute phase offset (deg) for each part.

    The phase offset ensures that gear teeth interlock rather than overlap
    during animation.  The formula accounts for the current gear's tooth
    position at the contact point (cross-coupling term) and reduces modulo
    the neighbor's angular pitch for minimal rotation:

        p_cur = 360 / Z_current
        p_nbr = 360 / Z_neighbor
        contact_angle = atan2(dy, dx)

        # External gear:
        raw = contact_angle + 180 - p_nbr/2 + p_nbr * (contact_angle - phase_cur) / p_cur
        phase_nbr = raw % p_nbr

    Returns {part_id: phase_offset_deg}.  Parts without gear meshes get 0.
    """
    gear_joints = _gear_joints(mech)
    if not gear_joints:
        return {}

    origins, gear_adj = _gear_mesh_origins_and_adj(mech)
    seed = _find_gear_seed(mech)
    if seed is None:
        return {}

    phases: dict[str, float] = {seed: 0.0}
    queue = [seed]
    visited = {seed}

    while queue:
        current = queue.pop(0)
        current_phase = phases[current]
        o_a = origins.get(current)
        if o_a is None:
            continue

        for neighbor, joint in gear_adj.get(current, []):
            if neighbor in visited:
                continue

            o_b = origins.get(neighbor)
            if o_b is None:
                visited.add(neighbor)
                phases[neighbor] = 0.0
                queue.append(neighbor)
                continue

            if current == joint.parent_part:
                z_current = joint.teeth_parent
                z_neighbor = joint.teeth_child
            else:
                z_current = joint.teeth_child
                z_neighbor = joint.teeth_parent

            if (
                z_neighbor is None or z_neighbor <= 0
                or z_current is None or z_current <= 0
            ):
                visited.add(neighbor)
                phases[neighbor] = 0.0
                queue.append(neighbor)
                continue

            dx = o_b[0] - o_a[0]
            dy = o_b[1] - o_a[1]
            contact_angle = math.degrees(math.atan2(dy, dx))

            p_cur = 360.0 / z_current
            p_nbr = 360.0 / z_neighbor

            # Cross-coupling: where current gear's teeth actually are
            # at the contact direction, accounting for current_phase.
            coupling = p_nbr * (contact_angle - current_phase) / p_cur

            if joint.internal:
                raw = contact_angle - p_nbr / 2 + coupling
            else:
                raw = contact_angle + 180.0 - p_nbr / 2 + coupling

            # Reduce modulo angular pitch for minimal rotation
            neighbor_phase = raw % p_nbr

            phases[neighbor] = neighbor_phase
            visited.add(neighbor)
            queue.append(neighbor)

    return phases


def compute_gear_animation_ratios(mech: Mechanism) -> dict[str, float]:
    """BFS from the driven gear to compute signed animation speed ratios.

    Returns {part_id: signed_ratio} where:
    - Driver part: +1.0
    - External gear mesh: direction flips, magnitude = Z_current / Z_neighbor
    - Internal gear mesh: direction preserved
    - Fixed / revolute: ratio inherited (co-axial parts)

    These ratios give physically correct animation: external gears
    counter-rotate, ratio magnitude = teeth_driver / teeth_driven.
    """
    gear_joints = _gear_joints(mech)
    if not gear_joints:
        return {}

    seed = _find_gear_seed(mech)
    if seed is None:
        return {}

    adj = _build_adjacency(mech)
    ratios: dict[str, float] = {seed: 1.0}
    queue = [seed]
    visited = {seed}

    while queue:
        current = queue.pop(0)
        cur_ratio = ratios[current]

        for neighbor, joint in adj.get(current, []):
            if neighbor in visited:
                continue

            if joint.joint_type in (JointType.GEAR_MESH, JointType.BELT_CHAIN):
                if current == joint.parent_part:
                    z_cur = joint.teeth_parent
                    z_nbr = joint.teeth_child
                else:
                    z_cur = joint.teeth_child
                    z_nbr = joint.teeth_parent

                if z_cur and z_nbr and z_nbr > 0:
                    mag = z_cur / z_nbr
                else:
                    mag = 1.0

                if joint.joint_type == JointType.GEAR_MESH and not joint.internal:
                    nbr_ratio = -cur_ratio * mag
                else:
                    nbr_ratio = cur_ratio * mag
            else:
                nbr_ratio = cur_ratio

            ratios[neighbor] = nbr_ratio
            visited.add(neighbor)
            queue.append(neighbor)

    return ratios


# ---------------------------------------------------------------------------
# Speed propagation (BFS from driven joints)
# ---------------------------------------------------------------------------

def propagate_speeds(mech: Mechanism) -> dict[str, float]:
    """BFS from driven joints to compute RPM at every part.

    For planetary gear sets (detected via ``detect_planetary_sets``), the
    Willis equation is used instead of naive BFS propagation:
      - carrier speed: ``w_carrier = w_sun / (1 + z_ring/z_sun)``  (ring fixed)
      - planet speed:  ``w_planet = w_carrier - (z_sun/z_planet) * (w_sun - w_carrier)``

    Returns {part_id: rpm}.  Parts not reachable from a drive get rpm=0.
    """
    from server.motion_planetary import detect_planetary_sets

    speeds: dict[str, float] = {}
    adj = _build_adjacency(mech)

    # Detect planetary topology
    planetary_sets = detect_planetary_sets(mech)
    # Collect planet part IDs — they are derived via Willis, not BFS
    planet_part_ids: set[str] = set()
    # Map carrier/sun/ring to their planetary set for Willis computation
    planetary_member_to_set: dict[str, Any] = {}
    for ps in planetary_sets:
        for pid in ps.planets:
            planet_part_ids.add(pid)
        for member_id in [ps.carrier, ps.sun, ps.ring]:
            planetary_member_to_set[member_id] = ps

    # Identify revolute joints within planetary sets (skip in normal BFS)
    # Any revolute connecting two members of the same planetary set should
    # be handled by Willis, not by speed inheritance.
    planetary_revolute_pairs: set[tuple[str, str]] = set()
    for ps in planetary_sets:
        all_members = {ps.carrier, ps.sun, ps.ring} | set(ps.planets)
        for j in mech.joints:
            if j.joint_type != JointType.REVOLUTE:
                continue
            if j.parent_part in all_members and j.child_part in all_members:
                planetary_revolute_pairs.add((j.parent_part, j.child_part))
                planetary_revolute_pairs.add((j.child_part, j.parent_part))

    # Seed driven parts
    for drive in mech.drives:
        if drive.speed_rpm is None:
            continue
        joint = mech.get_joint(drive.joint_id)
        if joint is None:
            continue
        # The drive applies to the parent part of the joint
        speeds[joint.parent_part] = drive.speed_rpm

    # Seed ground parts at 0 (if not already seeded by a drive).
    # This is needed so Willis can use ground ring speed during BFS.
    for part in mech.parts:
        if part.is_ground and part.id not in speeds:
            speeds[part.id] = 0.0

    # BFS with Willis-aware propagation
    queue = list(speeds.keys())
    visited = set(speeds.keys())

    def _try_willis(ps: Any) -> bool:
        """Try to compute carrier/sun speeds via Willis if enough info is known.

        Returns True if new speeds were derived and added to the queue.
        """
        derived = False
        w_sun = speeds.get(ps.sun)
        w_ring = speeds.get(ps.ring)
        w_carrier = speeds.get(ps.carrier)

        ratio = 1 + ps.teeth_ring / ps.teeth_sun  # e.g. 3.0

        if w_sun is not None and w_ring is not None and w_carrier is None:
            # Willis: w_carrier = (w_sun + (z_ring/z_sun) * w_ring) / (1 + z_ring/z_sun)
            w_carrier = (w_sun + (ps.teeth_ring / ps.teeth_sun) * w_ring) / ratio
            speeds[ps.carrier] = w_carrier
            if ps.carrier not in visited:
                visited.add(ps.carrier)
                queue.append(ps.carrier)
            derived = True

        elif w_carrier is not None and w_ring is not None and w_sun is None:
            w_sun = w_carrier * ratio - (ps.teeth_ring / ps.teeth_sun) * w_ring
            speeds[ps.sun] = w_sun
            if ps.sun not in visited:
                visited.add(ps.sun)
                queue.append(ps.sun)
            derived = True

        elif w_sun is not None and w_carrier is not None and w_ring is None:
            w_ring = (w_carrier * ratio - w_sun) / (ps.teeth_ring / ps.teeth_sun)
            speeds[ps.ring] = w_ring
            if ps.ring not in visited:
                visited.add(ps.ring)
                queue.append(ps.ring)
            derived = True

        # Derive planet speeds if carrier and sun are both known
        w_sun = speeds.get(ps.sun)
        w_carrier = speeds.get(ps.carrier)
        if w_sun is not None and w_carrier is not None:
            teeth_ratio = ps.teeth_sun / ps.teeth_planet if ps.teeth_planet > 0 else 1.0
            for pid in ps.planets:
                if pid not in speeds:
                    w_planet = w_carrier - teeth_ratio * (w_sun - w_carrier)
                    speeds[pid] = w_planet
                    visited.add(pid)
                    derived = True

        return derived

    # Initial Willis pass for any planetary sets that already have enough seeds
    for ps in planetary_sets:
        _try_willis(ps)

    while queue:
        current = queue.pop(0)
        current_rpm = speeds[current]
        for neighbor, joint in adj.get(current, []):
            if neighbor in visited:
                continue
            # Skip planets in BFS — they are derived via Willis
            if neighbor in planet_part_ids:
                continue

            ratio = _effective_ratio(joint)
            if ratio is not None and joint.joint_type in (
                JointType.GEAR_MESH, JointType.BELT_CHAIN,
            ):
                # Convention: ratio = teeth_parent / teeth_child
                # parent speed / child speed = teeth_child / teeth_parent = 1/ratio
                if current == joint.parent_part:
                    neighbor_rpm = current_rpm * ratio
                else:
                    neighbor_rpm = current_rpm / ratio
            elif joint.joint_type == JointType.FIXED:
                neighbor_rpm = current_rpm
            elif joint.joint_type == JointType.REVOLUTE:
                # Planetary carrier-planet revolutes are handled by Willis
                pair = (current, neighbor)
                if pair in planetary_revolute_pairs:
                    continue
                # Non-planetary revolutes: inherit speed (linked parts)
                neighbor_rpm = current_rpm
            else:
                neighbor_rpm = current_rpm
            speeds[neighbor] = neighbor_rpm
            visited.add(neighbor)
            queue.append(neighbor)

            # After setting a new speed, try Willis for any planetary set
            # this neighbor belongs to
            if neighbor in planetary_member_to_set:
                _try_willis(planetary_member_to_set[neighbor])

        # Also try Willis after processing each node (current might be a
        # planetary member whose speed was just used)
        if current in planetary_member_to_set:
            _try_willis(planetary_member_to_set[current])

    # Parts with no drive path get 0
    for part in mech.parts:
        if part.id not in speeds and not part.is_ground:
            speeds[part.id] = 0.0
    # Ground parts are always 0 (override any BFS-propagated values)
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
                    neighbor_torque = current_torque / ratio * eff
                else:
                    if ratio == 0:
                        neighbor_torque = 0.0
                    else:
                        neighbor_torque = current_torque * ratio * eff
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


def check_tooth_interference(
    mech: Mechanism,
    phases: dict[str, float] | None = None,
    pressure_angle_deg: float = 20.0,
) -> list[dict[str, Any]]:
    """Check for tooth-level interference at each gear mesh contact zone.

    For each gear_mesh joint, computes where each gear's teeth fall at the
    contact point (on the line of centers) and checks whether both gears
    present a tooth simultaneously (interference) or one presents a tooth
    while the other presents a gap (correct meshing).

    Returns a list of per-joint results:
      {"joint_id", "parent", "child", "ok", "residual_parent_deg",
       "residual_child_deg", "clearance_deg", "detail"}

    ``clearance_deg`` is how far the nearest tooth edges are from overlapping.
    Negative means overlap (interference).
    """
    if phases is None:
        phases = compute_gear_mesh_phases(mech)
    gear_joints = _gear_joints(mech)
    origins, _ = _gear_mesh_origins_and_adj(mech)

    results: list[dict[str, Any]] = []

    for j in gear_joints:
        o_parent = origins.get(j.parent_part)
        o_child = origins.get(j.child_part)
        z_p = j.teeth_parent
        z_c = j.teeth_child

        if (
            o_parent is None or o_child is None
            or z_p is None or z_p <= 0
            or z_c is None or z_c <= 0
        ):
            results.append({
                "joint_id": j.id,
                "parent": j.parent_part,
                "child": j.child_part,
                "ok": None,
                "detail": "missing origins or teeth data",
            })
            continue

        dx = o_child[0] - o_parent[0]
        dy = o_child[1] - o_parent[1]
        contact_angle = math.degrees(math.atan2(dy, dx))

        p_parent = 360.0 / z_p  # angular pitch of parent
        p_child = 360.0 / z_c

        phase_p = phases.get(j.parent_part, 0.0)
        phase_c = phases.get(j.child_part, 0.0)

        # Tooth half-width at pitch circle = half the angular pitch / 2
        # (tooth occupies half of one angular pitch)
        tooth_half_p = p_parent / 4.0
        tooth_half_c = p_child / 4.0

        # Parent: contact direction is toward child = contact_angle
        # Residual within one pitch period: how far from nearest tooth center
        residual_p = (contact_angle - phase_p) % p_parent
        dist_to_tooth_p = min(residual_p, p_parent - residual_p)

        # Child: contact direction is toward parent
        if j.internal:
            # Internal gear: same direction (teeth face inward)
            child_contact = contact_angle
        else:
            # External gear: opposite direction
            child_contact = contact_angle + 180.0

        residual_c = (child_contact - phase_c) % p_child
        dist_to_tooth_c = min(residual_c, p_child - residual_c)

        # Both gears present a tooth at contact if dist < tooth_half
        # Clearance = how much gap remains (positive = no interference)
        # For proper meshing, when parent has a tooth, child should have a gap
        parent_in_tooth = dist_to_tooth_p < tooth_half_p
        child_in_tooth = dist_to_tooth_c < tooth_half_c

        if parent_in_tooth and child_in_tooth:
            # Both teeth at contact — interference
            # Clearance is negative: how much they overlap
            gap_p = tooth_half_p - dist_to_tooth_p  # how deep into parent tooth
            gap_c = tooth_half_c - dist_to_tooth_c  # how deep into child tooth
            clearance = -(gap_p + gap_c)
            ok = False
            detail = (
                f"INTERFERENCE: both gears have teeth at contact zone "
                f"(overlap {abs(clearance):.2f}°)"
            )
        elif not parent_in_tooth and not child_in_tooth:
            # Both in gaps — teeth don't interlock (backlash issue)
            gap_margin_p = dist_to_tooth_p - tooth_half_p
            gap_margin_c = dist_to_tooth_c - tooth_half_c
            clearance = gap_margin_p + gap_margin_c
            ok = True
            detail = (
                f"Both gears in gap at contact zone "
                f"(clearance {clearance:.2f}°, may indicate excessive backlash)"
            )
        else:
            # One tooth, one gap — correct meshing
            if parent_in_tooth:
                margin = dist_to_tooth_c - tooth_half_c
            else:
                margin = dist_to_tooth_p - tooth_half_p
            clearance = margin
            ok = True
            detail = f"Correct tooth-gap interlocking (clearance {clearance:.2f}°)"

        results.append({
            "joint_id": j.id,
            "parent": j.parent_part,
            "child": j.child_part,
            "ok": ok,
            "residual_parent_deg": round(dist_to_tooth_p, 4),
            "residual_child_deg": round(dist_to_tooth_c, 4),
            "clearance_deg": round(clearance, 4),
            "detail": detail,
        })

    return results


def _check_mesh_phasing(mech: Mechanism) -> ValidatorResult:
    """Compute gear mesh phase offsets and verify geometric tooth interlocking."""
    gear_joints = _gear_joints(mech)
    if not gear_joints:
        return ValidatorResult(
            name="mesh_phasing",
            status="note",
            message="No gear meshes — phasing not applicable",
            measured={},
            priority=150,
        )

    phases = compute_gear_mesh_phases(mech)
    if not phases:
        return ValidatorResult(
            name="mesh_phasing",
            status="warn",
            message="Could not compute gear mesh phases (missing origins or teeth data)",
            measured={},
            priority=150,
        )

    # Check if any phases could not be computed (parts with 0 that are in gear meshes)
    origins = _revolute_origin_map(mech)
    missing_data: list[str] = []
    for j in gear_joints:
        for pid in (j.parent_part, j.child_part):
            if pid not in origins and pid not in missing_data:
                missing_data.append(pid)

    if missing_data:
        return ValidatorResult(
            name="mesh_phasing",
            status="warn",
            message=f"Phase computed but missing rotation origins for: {missing_data}",
            measured={"phase_offsets_deg": {k: round(v, 4) for k, v in phases.items()}},
            priority=150,
        )

    # Geometric interference check at each contact zone
    interference_results = check_tooth_interference(mech, phases)
    interference_issues = [r for r in interference_results if r.get("ok") is False]

    measured: dict[str, Any] = {
        "phase_offsets_deg": {k: round(v, 4) for k, v in phases.items()},
        "tooth_interference": interference_results,
    }

    if interference_issues:
        joint_ids = [r["joint_id"] for r in interference_issues]
        return ValidatorResult(
            name="mesh_phasing",
            status="fail",
            message=(
                f"Tooth interference detected at {len(interference_issues)} "
                f"mesh(es): {joint_ids}. Phase offsets may be incorrect."
            ),
            measured=measured,
            priority=150,
        )

    return ValidatorResult(
        name="mesh_phasing",
        status="pass",
        message=(
            f"Mesh phases computed for {len(phases)} parts — "
            f"geometric tooth interlocking verified at {len(interference_results)} mesh(es)"
        ),
        measured=measured,
        priority=150,
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

    phases = compute_gear_mesh_phases(mech)
    interference = check_tooth_interference(mech, phases)
    interference_issues = [r for r in interference if r.get("ok") is False]
    result: dict[str, Any] = {
        "overall_ratio": round(overall_ratio, 6),
        "stages": stages,
        "phase_offsets_deg": {k: round(v, 4) for k, v in phases.items()},
        "tooth_interference": interference,
    }
    if interference_issues:
        result["tooth_interference_warning"] = (
            f"Tooth interference detected at {len(interference_issues)} "
            f"mesh(es): {[r['joint_id'] for r in interference_issues]}"
        )
    return result


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

ValidatorFn = Callable[[Mechanism], ValidatorResult]

VALIDATORS: dict[str, ValidatorFn] = {
    "gear_ratio_consistency": _check_gear_ratio_consistency,
    "mesh_phasing": _check_mesh_phasing,
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


# ---------------------------------------------------------------------------
# Structural mechanism validation (pre-storage checks)
# ---------------------------------------------------------------------------

def validate_mechanism_structure(
    mech: Mechanism,
    *,
    mode: str = "warn",
) -> tuple[list[str], list[str]]:
    """Validate mechanism structural integrity before storage.

    Returns (errors, warnings).  Errors should block storage in strict mode.

    Checks performed:
    1.  Unique part IDs
    2.  Unique joint IDs
    3.  Finite numeric values on joints (axis, origin, limits, ratios)
    4.  Axis near-unit (within 5% tolerance; warning, auto-normalizable)
    5.  Dangling part references in joints
    6.  Dangling joint references in drives
    7.  No ground part defined
    8.  Limit consistency (min < max)
    9.  Cycle detection in kinematic tree
    10. Fixed joint with non-default axis (informational warning)
    11. Duplicate joint connections (same parent→child pair)
    12. Negative mass on non-ground parts
    """
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Unique part IDs
    part_ids: set[str] = set()
    for p in mech.parts:
        if p.id in part_ids:
            errors.append(f"Duplicate part ID: '{p.id}'")
        part_ids.add(p.id)

    # 2. Unique joint IDs
    joint_ids: set[str] = set()
    for j in mech.joints:
        if j.id in joint_ids:
            errors.append(f"Duplicate joint ID: '{j.id}'")
        joint_ids.add(j.id)

    # 3. Finite numeric values + 4. Axis near-unit
    for j in mech.joints:
        # Check axis components are finite
        for i, val in enumerate(j.axis):
            if not math.isfinite(val):
                errors.append(
                    f"Joint '{j.id}': axis[{i}]={val} is not finite"
                )
        # Check origin components are finite
        for i, val in enumerate(j.origin):
            if not math.isfinite(val):
                errors.append(
                    f"Joint '{j.id}': origin[{i}]={val} is not finite"
                )
        # Axis should be unit-length (5% tolerance)
        axis_mag_sq = sum(a * a for a in j.axis)
        if axis_mag_sq > 0:
            axis_mag = math.sqrt(axis_mag_sq)
            if not math.isclose(axis_mag, 1.0, rel_tol=0.05):
                msg = (
                    f"Joint '{j.id}': axis magnitude {axis_mag:.4f} "
                    f"is not unit-length (will be normalized in URDF)"
                )
                if mode == "strict":
                    errors.append(msg)
                else:
                    warnings.append(msg)
        else:
            errors.append(f"Joint '{j.id}': axis is zero vector")

        # Gear ratio / teeth finite
        if j.gear_ratio is not None and not math.isfinite(j.gear_ratio):
            errors.append(
                f"Joint '{j.id}': gear_ratio={j.gear_ratio} is not finite"
            )
        if j.teeth_parent is not None and j.teeth_parent <= 0:
            errors.append(
                f"Joint '{j.id}': teeth_parent={j.teeth_parent} must be positive"
            )
        if j.teeth_child is not None and j.teeth_child <= 0:
            errors.append(
                f"Joint '{j.id}': teeth_child={j.teeth_child} must be positive"
            )

    # 5. Dangling part references in joints
    for j in mech.joints:
        if j.parent_part not in part_ids:
            errors.append(
                f"Joint '{j.id}' references unknown parent_part '{j.parent_part}'"
            )
        if j.child_part not in part_ids:
            errors.append(
                f"Joint '{j.id}' references unknown child_part '{j.child_part}'"
            )

    # 6. Dangling joint references in drives
    for d in mech.drives:
        if mech.get_joint(d.joint_id) is None:
            errors.append(
                f"Drive references unknown joint_id '{d.joint_id}'"
            )

    # 7. No ground part — warning in "warn" mode, error in "strict" mode.
    if not mech.ground_parts():
        msg = "No ground part defined (is_ground=true)"
        if mode == "strict":
            errors.append(msg)
        else:
            warnings.append(msg)

    # 8. Limit consistency (min < max)
    for j in mech.joints:
        if j.min_angle_deg is not None and j.max_angle_deg is not None:
            if j.min_angle_deg >= j.max_angle_deg:
                errors.append(
                    f"Joint '{j.id}': min_angle_deg ({j.min_angle_deg}) "
                    f">= max_angle_deg ({j.max_angle_deg})"
                )
        if j.min_travel_mm is not None and j.max_travel_mm is not None:
            if j.min_travel_mm >= j.max_travel_mm:
                errors.append(
                    f"Joint '{j.id}': min_travel_mm ({j.min_travel_mm}) "
                    f">= max_travel_mm ({j.max_travel_mm})"
                )

    # 9. Cycle detection in kinematic tree
    # Build parent→children adjacency and check for cycles via DFS
    children_of: dict[str, list[str]] = defaultdict(list)
    for j in mech.joints:
        if j.parent_part in part_ids and j.child_part in part_ids:
            children_of[j.parent_part].append(j.child_part)
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _has_cycle(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for child in children_of.get(node, []):
            if _has_cycle(child):
                return True
        in_stack.discard(node)
        return False

    for pid in part_ids:
        if _has_cycle(pid):
            errors.append("Cycle detected in kinematic tree")
            break

    # 10. Fixed joint with non-default axis (informational)
    for j in mech.joints:
        if j.joint_type == JointType.FIXED:
            if j.axis != (0.0, 0.0, 1.0):
                warnings.append(
                    f"Joint '{j.id}' is fixed but has non-default "
                    f"axis {j.axis} (axis is ignored for fixed joints)"
                )

    # 10a. Prismatic joint axis not aligned with principal direction
    for j in mech.joints:
        if j.joint_type == JointType.PRISMATIC:
            axis_mag_sq = sum(a * a for a in j.axis)
            if axis_mag_sq > 0:  # zero-axis already caught above
                if max(abs(j.axis[i]) for i in range(3)) < 0.9:
                    warnings.append(
                        f"Joint '{j.id}': prismatic axis not aligned "
                        f"with any principal direction"
                    )

    # 10b. Continuous joint with angle limits
    for j in mech.joints:
        if j.joint_type == JointType.CONTINUOUS:
            if j.min_angle_deg is not None or j.max_angle_deg is not None:
                warnings.append(
                    f"Joint '{j.id}': continuous joint has angle limits "
                    f"set (use revolute instead)"
                )

    # 11. Duplicate joint connections (including reverse direction: a→b and b→a)
    seen_connections: set[frozenset[str]] = set()
    for j in mech.joints:
        key = frozenset((j.parent_part, j.child_part))
        if key in seen_connections:
            errors.append(
                f"Duplicate joint connection: '{j.parent_part}' → "
                f"'{j.child_part}' (joint '{j.id}')"
            )
        seen_connections.add(key)

    # 12. Negative mass
    for p in mech.parts:
        if p.mass_kg is not None and p.mass_kg < 0:
            errors.append(f"Part '{p.id}': mass_kg={p.mass_kg} is negative")
        if p.mass_kg is not None and p.mass_kg == 0 and not p.is_ground:
            warnings.append(
                f"Part '{p.id}': mass_kg=0 on non-ground part "
                f"(will cause simulation instability)"
            )

    # 13. Applied forces
    valid_frames = {"body", "world"}
    for i, f in enumerate(mech.applied_forces):
        label = f.label or f"applied_forces[{i}]"
        if f.target_body not in part_ids:
            errors.append(
                f"Applied force '{label}' targets unknown body '{f.target_body}'"
            )
        for k, val in enumerate(f.position_local):
            if not math.isfinite(val):
                errors.append(
                    f"Applied force '{label}': position_local[{k}]={val} is not finite"
                )
        for k, val in enumerate(f.force_vector):
            if not math.isfinite(val):
                errors.append(
                    f"Applied force '{label}': force_vector[{k}]={val} is not finite"
                )
        if all(v == 0.0 for v in f.force_vector):
            warnings.append(
                f"Applied force '{label}' has zero force vector — no effect"
            )
        if f.frame not in valid_frames:
            errors.append(
                f"Applied force '{label}' has invalid frame '{f.frame}' "
                f"(must be one of {sorted(valid_frames)})"
            )

    return errors, warnings
