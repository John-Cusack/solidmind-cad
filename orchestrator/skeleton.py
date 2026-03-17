"""Skeleton validation — datums, reserved volumes, keepout zones, and G2 gate."""
from __future__ import annotations

from typing import Any

from orchestrator.spec import MasterSpec


def validate_datum_attachment(spec: MasterSpec) -> tuple[bool, list[str]]:
    """Every subsystem must reference at least one datum.

    Checks subsystem.assembly_constraints for a 'datum' or 'datums' key,
    or checks if a datum name matches the subsystem name.
    """
    issues: list[str] = []
    datum_names = set(spec.skeleton.datums.keys())

    for sub in spec.subsystems:
        # Check if subsystem has explicit datum reference
        ac = sub.assembly_constraints
        has_datum = (
            "datum" in ac
            or "datums" in ac
            or "coaxial_with" in ac
            or "mounted_on" in ac
            or sub.name in datum_names
        )
        if not has_datum:
            issues.append(f"Subsystem '{sub.name}' not attached to any datum")

    return len(issues) == 0, issues


def validate_reserved_volumes(spec: MasterSpec) -> tuple[bool, list[str]]:
    """Detect AABB overlap between reserved volumes."""
    issues: list[str] = []
    volumes = spec.skeleton.reserved_volumes
    names = list(volumes.keys())

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            vol_a = volumes[names[i]]
            vol_b = volumes[names[j]]
            if aabb_overlap(vol_a, vol_b):
                issues.append(
                    f"Reserved volumes '{names[i]}' and '{names[j]}' overlap"
                )

    return len(issues) == 0, issues


def validate_keepout_zones(spec: MasterSpec) -> tuple[bool, list[str]]:
    """No reserved volume may intersect a keepout zone."""
    issues: list[str] = []

    for vol_name, vol in spec.skeleton.reserved_volumes.items():
        for ki, keepout in enumerate(spec.skeleton.keepout_zones):
            kname = keepout.get("name", f"keepout_{ki}")
            if aabb_overlap(vol, keepout):
                issues.append(
                    f"Reserved volume '{vol_name}' intersects keepout zone '{kname}'"
                )

    return len(issues) == 0, issues


def check_gate_g2(spec: MasterSpec) -> tuple[bool, list[str]]:
    """G2: Skeleton completeness — datums exist, volumes don't clash."""
    all_issues: list[str] = []

    if not spec.skeleton.datums:
        all_issues.append("No datums defined in skeleton")

    ok_datum, datum_issues = validate_datum_attachment(spec)
    all_issues.extend(datum_issues)

    ok_vols, vol_issues = validate_reserved_volumes(spec)
    all_issues.extend(vol_issues)

    ok_keepout, keepout_issues = validate_keepout_zones(spec)
    all_issues.extend(keepout_issues)

    return len(all_issues) == 0, all_issues


def build_skeleton_summary(spec: MasterSpec) -> dict[str, Any]:
    """Human-readable summary for A2 gate presentation."""
    sk = spec.skeleton
    return {
        "datum_count": len(sk.datums),
        "datums": list(sk.datums.keys()),
        "shaft_axes": list(sk.shaft_axes.keys()),
        "bearing_spans": list(sk.bearing_spans.keys()),
        "reserved_volumes": list(sk.reserved_volumes.keys()),
        "keepout_zones": [
            kz.get("name", f"keepout_{i}")
            for i, kz in enumerate(sk.keepout_zones)
        ],
        "subsystem_count": len(spec.subsystems),
    }


# ---------------------------------------------------------------------------
# AABB helpers
# ---------------------------------------------------------------------------


def aabb_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Check if two axis-aligned bounding boxes overlap.

    Supports two formats:
    - {"origin": [x,y,z], "size": [w,h,d]} — origin is corner
    - {"bbox": [w,h,d], "position": [x,y,z]} — position is center or corner
    - {"min": [x,y,z], "max": [x,y,z]} — explicit min/max corners
    """
    a_min, a_max = aabb_bounds(a)
    b_min, b_max = aabb_bounds(b)

    if a_min is None or b_min is None:
        return False

    for i in range(3):
        if a_max[i] <= b_min[i] or b_max[i] <= a_min[i]:
            return False
    return True


def aabb_bounds(vol: dict[str, Any]) -> tuple[list[float] | None, list[float] | None]:
    """Extract [min_x,min_y,min_z], [max_x,max_y,max_z] from a volume dict."""
    if "min" in vol and "max" in vol:
        return vol["min"], vol["max"]

    if "origin" in vol and "size" in vol:
        o = vol["origin"]
        s = vol["size"]
        return o, [o[i] + s[i] for i in range(3)]

    if "bbox" in vol:
        pos = vol.get("position", [0.0, 0.0, 0.0])
        bbox = vol["bbox"]
        # Treat position as corner
        return pos, [pos[i] + bbox[i] for i in range(3)]

    return None, None


# Backward-compatible aliases
_aabb_overlap = aabb_overlap
_aabb_bounds = aabb_bounds
