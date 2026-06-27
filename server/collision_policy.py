"""Collision policy filtering for interference detection.

Derives collision policies from ``design.add_interface`` specs and filters
raw clearance/interference violations against those policies.  Interface
type + thresholds determine what is intentional contact vs. a defect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Policy model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollisionPolicy:
    """A single collision-filter policy derived from an interface."""

    interface_id: str  # "{part_a}:{port_a}->{part_b}:{port_b}"
    part_a: str
    part_b: str
    contact_type: str  # "gear_mesh", "press_fit", "bolt_pattern", …
    max_penetration_mm: float  # 0.0 = contact only, >0 = interference fit
    min_clearance_mm: float  # minimum acceptable gap for non-contact zones


# Default thresholds per contact type.
# (max_penetration_mm, min_clearance_mm)
CONTACT_DEFAULTS: dict[str, tuple[float, float]] = {
    "gear_mesh": (0.0, 0.1),
    "press_fit": (0.05, 0.0),
    "snap_fit": (0.1, 0.0),
    "thread": (0.0, 0.0),
    "bolt_pattern": (0.0, 0.5),
    "bearing_bore": (0.02, 0.0),
    "slider": (0.0, 0.1),
    "clamp": (0.0, 0.0),
}


# ---------------------------------------------------------------------------
# Derive policies from design brief interfaces
# ---------------------------------------------------------------------------


def derive_policies(interfaces: list[dict[str, Any]]) -> list[CollisionPolicy]:
    """Build collision policies from interface dicts (as returned by ``InterfaceEntry.to_dict()``)."""
    policies: list[CollisionPolicy] = []
    for iface in interfaces:
        spec = iface.get("spec", {})
        contact_type = spec.get("type") or spec.get("pattern") or ""
        if not contact_type:
            continue  # no type declared — no policy can be inferred

        defaults = CONTACT_DEFAULTS.get(contact_type, (0.0, 0.0))
        max_pen = spec.get("max_penetration_mm", defaults[0])
        min_clr = spec.get("min_clearance_mm", defaults[1])

        iface_id = (
            f"{iface.get('part_a', '?')}:{iface.get('port_a', '')}"
            f"->{iface.get('part_b', '?')}:{iface.get('port_b', '')}"
        )
        policies.append(
            CollisionPolicy(
                interface_id=iface_id,
                part_a=iface.get("part_a", ""),
                part_b=iface.get("part_b", ""),
                contact_type=contact_type,
                max_penetration_mm=float(max_pen),
                min_clearance_mm=float(min_clr),
            )
        )
    return policies


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def build_name_map(
    brief_parts: list[dict[str, Any]],
    mechanism_parts: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Build ``{part_name: body_label}`` from brief parts and mechanism parts.

    Brief parts use ``body_label``, mechanism parts use ``body_name``.
    Mechanism parts take precedence when both exist for the same name.
    """
    name_to_body: dict[str, str] = {}

    for p in brief_parts:
        name = p.get("name", "")
        body = p.get("body_label", "")
        if name and body:
            name_to_body[name] = body

    if mechanism_parts:
        for p in mechanism_parts:
            pid = p.get("id", "")
            body = p.get("body_name", "")
            if pid and body:
                name_to_body[pid] = body

    return name_to_body


def _normalize_pair(a: str, b: str) -> frozenset[str]:
    """Canonical unordered pair key."""
    return frozenset((a, b))


# ---------------------------------------------------------------------------
# Filter violations
# ---------------------------------------------------------------------------


def filter_violations(
    violations: list[dict[str, Any]],
    policies: list[CollisionPolicy],
    name_map: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter raw collision violations against interface policies.

    Parameters
    ----------
    violations
        Raw violation dicts from ``cad_check_clearance`` or
        ``motion_check_interference``.  Must have ``body_a``, ``body_b``,
        and ``distance_mm`` (or ``intersecting``).
    policies
        Collision policies derived from interfaces.
    name_map
        Optional ``{part_name: body_label}`` for resolving policy part names
        to body names used in violations.

    Returns
    -------
    (filtered, suppressed)
        ``filtered`` = violations that remain after policy filtering.
        ``suppressed`` = violations removed by policy (intentional contact).
    """
    if not policies:
        return list(violations), []

    # Build policy lookup: frozenset(body_a, body_b) -> policy
    # Resolve part names to body names via name_map
    body_map = name_map or {}
    policy_by_pair: dict[frozenset[str], CollisionPolicy] = {}
    for p in policies:
        body_a = body_map.get(p.part_a, p.part_a)
        body_b = body_map.get(p.part_b, p.part_b)
        key = _normalize_pair(body_a, body_b)
        policy_by_pair[key] = p

    filtered: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

    for v in violations:
        va = v.get("body_a", "")
        vb = v.get("body_b", "")
        pair_key = _normalize_pair(va, vb)

        policy = policy_by_pair.get(pair_key)
        if policy is None:
            # No interface declares this contact — always a finding
            filtered.append(v)
            continue

        # Determine penetration: if intersecting, use distance as 0 (contact).
        # distToShape returns 0 for intersecting; penetration depth requires
        # further analysis.  For now, intersecting = 0mm penetration.
        distance = v.get("distance_mm", 0.0)
        is_intersecting = v.get("intersecting", False)

        if is_intersecting and policy.max_penetration_mm >= 0.0:
            # Contact/intersection: suppressed if policy allows contact
            entry = {**v, "_suppressed_by": policy.interface_id}
            suppressed.append(entry)
        elif not is_intersecting and distance < policy.min_clearance_mm:
            # Too close but not intersecting — still a violation
            v_annotated = {
                **v,
                "_policy_note": (
                    f"Within {policy.contact_type} zone but clearance "
                    f"{distance:.3f}mm < min {policy.min_clearance_mm}mm"
                ),
            }
            filtered.append(v_annotated)
        elif is_intersecting and policy.max_penetration_mm < 0.0:
            # Policy doesn't allow any interference
            filtered.append(v)
        else:
            # Outside policy concern (far enough away)
            filtered.append(v)

    return filtered, suppressed
