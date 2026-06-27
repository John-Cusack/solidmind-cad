"""Unified preflight check for collision and interference detection.

Single entry point that runs all applicable validation stages and returns
a structured report with explicit gate mode semantics.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("solidmind.preflight")


def preflight_check(
    brief_id: str,
    mechanism_id: str | None = None,
    gate_mode: str = "advisory",
    doc: str | None = None,
) -> dict[str, Any]:
    """Run all applicable collision/interference checks.

    Parameters
    ----------
    brief_id
        Design brief to verify.
    mechanism_id
        Optional mechanism for motion-level checks (tooth interference,
        joint connectivity, swept clearance).
    gate_mode
        ``"advisory"`` (default) — findings are warnings, never blocks.
        ``"strict"`` — any unsuppressed fail yields overall fail.
    doc
        Optional FreeCAD document name.

    Returns
    -------
    Structured report with per-category status and policy summary.
    """
    if gate_mode not in ("advisory", "strict"):
        return {
            "ok": False,
            "error": {
                "code": "INVALID_INPUT",
                "message": f"gate_mode must be 'advisory' or 'strict', got '{gate_mode}'",
            },
        }

    t_total_start = time.monotonic()
    categories: list[dict[str, Any]] = []
    pairs_checked = 0
    sweep_samples = 0

    # ------------------------------------------------------------------
    # Stage 1: Design completeness
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    build_cat = _run_design_completeness(brief_id, doc)
    build_cat["timing_ms"] = round((time.monotonic() - t0) * 1000, 1)
    categories.append(build_cat)

    # ------------------------------------------------------------------
    # Stage 2: Name resolution
    # ------------------------------------------------------------------
    name_map, policies, name_cat = _run_name_resolution(brief_id, mechanism_id)
    categories.append(name_cat)

    # ------------------------------------------------------------------
    # Stage 3: Static clearance (with policy filtering)
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    clearance_cat = _run_static_clearance(brief_id, policies, name_map, doc)
    clearance_cat["timing_ms"] = round((time.monotonic() - t0) * 1000, 1)
    pairs_checked += clearance_cat.get("pairs_checked", 0)
    categories.append(clearance_cat)

    # ------------------------------------------------------------------
    # Stage 4: Assembly interference (with policy filtering)
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    interf_cat = _run_assembly_interference(mechanism_id, policies, name_map, doc)
    interf_cat["timing_ms"] = round((time.monotonic() - t0) * 1000, 1)
    categories.append(interf_cat)

    # ------------------------------------------------------------------
    # Stage 5: Swept clearance (for driven joints)
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    swept_cat = _run_swept_clearance(mechanism_id, doc)
    swept_cat["timing_ms"] = round((time.monotonic() - t0) * 1000, 1)
    sweep_samples += swept_cat.get("sweep_samples", 0)
    categories.append(swept_cat)

    # ------------------------------------------------------------------
    # Stage 6: Joint connectivity
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    conn_cat = _run_joint_connectivity(mechanism_id, doc)
    conn_cat["timing_ms"] = round((time.monotonic() - t0) * 1000, 1)
    categories.append(conn_cat)

    # ------------------------------------------------------------------
    # Stage 7: Motion validators (includes tooth interference)
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    motion_cat = _run_motion_validators(mechanism_id)
    motion_cat["timing_ms"] = round((time.monotonic() - t0) * 1000, 1)
    categories.append(motion_cat)

    # ------------------------------------------------------------------
    # Compute overall status
    # ------------------------------------------------------------------
    total_suppressed = sum(c.get("suppressed_count", 0) for c in categories)
    total_policy_violations = sum(
        1 for c in categories for f in c.get("findings", []) if f.get("_policy_note")
    )

    statuses = [c["status"] for c in categories]
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "pass"

    # In strict mode, skipped stages with no data are a concern
    if gate_mode == "strict":
        skipped_critical = [
            c["name"]
            for c in categories
            if c["status"] == "skipped"
            and c["name"]
            in (
                "static_clearance",
                "assembly_interference",
                "motion_validators",
            )
        ]
        if skipped_critical and overall != "fail":
            overall = "fail"
            categories.append(
                {
                    "name": "gate_coverage",
                    "status": "fail",
                    "findings": [
                        {
                            "reason_code": "INSUFFICIENT_COVERAGE",
                            "message": f"Critical stages skipped in strict mode: {skipped_critical}",
                        }
                    ],
                    "suppressed_count": 0,
                }
            )

    total_ms = round((time.monotonic() - t_total_start) * 1000, 1)

    return {
        "ok": True,
        "gate_mode": gate_mode,
        "overall_status": overall,
        "categories": categories,
        "coverage": {
            "pairs_checked": pairs_checked,
            "sweep_samples": sweep_samples,
        },
        "timing_ms": {
            "total": total_ms,
        },
        "policy_summary": {
            "entries_loaded": len(policies),
            "findings_suppressed": total_suppressed,
            "suppression_violations": total_policy_violations,
        },
    }


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def _run_design_completeness(
    brief_id: str,
    doc: str | None,
) -> dict[str, Any]:
    """Stage 1: design_verify_build."""
    try:
        from server.tools_design import design_verify_build

        result = design_verify_build(
            brief_id,
            doc=doc,
            check_clearance=False,  # clearance handled in dedicated stage
        )
        if not result.get("ok"):
            code = result.get("error", {}).get("code", "UNKNOWN")
            msg = result.get("error", {}).get("message", "")
            if code == "MODEL_TREE_ERROR":
                return {
                    "name": "design_completeness",
                    "status": "skipped",
                    "findings": [{"reason_code": "TOOL_ERROR", "message": msg}],
                    "suppressed_count": 0,
                }
            return {
                "name": "design_completeness",
                "status": "fail",
                "findings": [{"reason_code": code, "message": msg}],
                "suppressed_count": 0,
            }

        summary = result.get("summary", {})
        completeness = summary.get("completeness_pct", 0)
        action_items = result.get("action_items", [])

        if completeness < 100:
            status = "warn"
        else:
            status = "pass"

        return {
            "name": "design_completeness",
            "status": status,
            "findings": [{"action": item} for item in action_items] if action_items else [],
            "suppressed_count": 0,
            "completeness_pct": completeness,
        }
    except Exception as exc:
        return {
            "name": "design_completeness",
            "status": "fail",
            "findings": [{"reason_code": "TOOL_ERROR", "message": str(exc)}],
            "suppressed_count": 0,
        }


def _run_name_resolution(
    brief_id: str,
    mechanism_id: str | None,
) -> tuple[dict[str, str], list[Any], dict[str, Any]]:
    """Stage 2: build name map and derive policies."""
    from server.collision_policy import CollisionPolicy, build_name_map, derive_policies
    from server.design_store import get_brief

    brief = get_brief(brief_id)
    name_map: dict[str, str] = {}
    policies: list[CollisionPolicy] = []
    findings: list[dict[str, Any]] = []

    if brief is None:
        return (
            name_map,
            policies,
            {
                "name": "name_resolution",
                "status": "fail",
                "findings": [
                    {"reason_code": "BRIEF_NOT_FOUND", "message": f"No brief '{brief_id}'"}
                ],
                "suppressed_count": 0,
            },
        )

    # Build name map from brief parts
    brief_parts = [p.to_dict() for p in brief.parts]

    # Add mechanism parts if available
    mech_parts = None
    if mechanism_id:
        from server.motion_store import get as mech_get

        mech = mech_get(mechanism_id)
        if mech is not None:
            mech_parts = [p.to_dict() for p in mech.parts]

    name_map = build_name_map(brief_parts, mech_parts)

    # Check for parts without body mappings
    for p in brief.parts:
        if p.kind == "custom" and p.status == "built" and not name_map.get(p.name):
            findings.append(
                {
                    "reason_code": "POLICY_MISMATCH",
                    "message": f"Part '{p.name}' marked built but no body mapping found",
                }
            )

    # Derive collision policies from interfaces
    iface_dicts = [i.to_dict() for i in brief.interfaces]
    policies = derive_policies(iface_dicts)

    status = "warn" if findings else "pass"
    return (
        name_map,
        policies,
        {
            "name": "name_resolution",
            "status": status,
            "findings": findings,
            "suppressed_count": 0,
            "name_map_size": len(name_map),
            "policies_derived": len(policies),
        },
    )


def _run_static_clearance(
    brief_id: str,
    policies: list[Any],
    name_map: dict[str, str],
    doc: str | None,
) -> dict[str, Any]:
    """Stage 3: cad_check_clearance with policy filtering."""
    try:
        from server.collision_policy import filter_violations
        from server.tools_cad import cad_check_clearance

        result = cad_check_clearance(doc=doc)
        if not result.get("ok"):
            return {
                "name": "static_clearance",
                "status": "skipped",
                "findings": [
                    {
                        "reason_code": "TOOL_ERROR",
                        "message": result.get("error", {}).get("message", "FreeCAD unavailable"),
                    }
                ],
                "suppressed_count": 0,
                "pairs_checked": 0,
            }

        raw_violations = result.get("violations", [])
        pairs_checked = result.get("pairs_checked", 0)

        filtered, suppressed = filter_violations(raw_violations, policies, name_map)

        if any(v.get("intersecting") for v in filtered):
            status = "fail"
        elif filtered:
            status = "warn"
        else:
            status = "pass"

        return {
            "name": "static_clearance",
            "status": status,
            "findings": filtered,
            "suppressed_count": len(suppressed),
            "pairs_checked": pairs_checked,
        }
    except Exception as exc:
        return {
            "name": "static_clearance",
            "status": "skipped",
            "findings": [{"reason_code": "TOOL_ERROR", "message": str(exc)}],
            "suppressed_count": 0,
            "pairs_checked": 0,
        }


def _run_assembly_interference(
    mechanism_id: str | None,
    policies: list[Any],
    name_map: dict[str, str],
    doc: str | None,
) -> dict[str, Any]:
    """Stage 4: motion_check_interference with policy filtering."""
    if mechanism_id is None:
        return {
            "name": "assembly_interference",
            "status": "skipped",
            "findings": [],
            "suppressed_count": 0,
        }
    try:
        from server.collision_policy import filter_violations
        from server.tools_motion import motion_check_interference

        result = motion_check_interference(mechanism_id, doc=doc)
        if not result.get("ok"):
            return {
                "name": "assembly_interference",
                "status": "skipped",
                "findings": [
                    {
                        "reason_code": "TOOL_ERROR",
                        "message": result.get("error", {}).get("message", ""),
                    }
                ],
                "suppressed_count": 0,
            }

        collisions = result.get("collisions", [])
        # Normalize collision format to match filter_violations expectations
        violations = []
        for c in collisions:
            violations.append(
                {
                    "body_a": c.get("part_a", ""),
                    "body_b": c.get("part_b", ""),
                    "intersecting": True,
                    "distance_mm": 0.0,
                    "overlap_volume_mm3": c.get("overlap_volume_mm3", 0.0),
                }
            )

        filtered, suppressed = filter_violations(violations, policies, name_map)

        if filtered:
            status = "fail"
        else:
            status = "pass"

        return {
            "name": "assembly_interference",
            "status": status,
            "findings": filtered,
            "suppressed_count": len(suppressed),
        }
    except Exception as exc:
        return {
            "name": "assembly_interference",
            "status": "skipped",
            "findings": [{"reason_code": "TOOL_ERROR", "message": str(exc)}],
            "suppressed_count": 0,
        }


def _run_swept_clearance(
    mechanism_id: str | None,
    doc: str | None,
) -> dict[str, Any]:
    """Stage 5: swept clearance for driven joints."""
    if mechanism_id is None:
        return {
            "name": "swept_clearance",
            "status": "skipped",
            "findings": [],
            "suppressed_count": 0,
            "sweep_samples": 0,
        }

    try:
        from server.motion_store import get as mech_get

        mech = mech_get(mechanism_id)
        if mech is None:
            return {
                "name": "swept_clearance",
                "status": "skipped",
                "findings": [
                    {"reason_code": "NOT_FOUND", "message": f"No mechanism '{mechanism_id}'"}
                ],
                "suppressed_count": 0,
                "sweep_samples": 0,
            }

        # Find driven parts that have body names
        driven_bodies: list[tuple[str, tuple[float, ...], tuple[float, ...]]] = []
        for drive in mech.drives:
            joint = mech.get_joint(drive.joint_id)
            if joint is None:
                continue
            # Check parent part for body name
            parent = mech.get_part(joint.parent_part)
            if parent and parent.body_name:
                driven_bodies.append(
                    (
                        parent.body_name,
                        joint.axis,
                        joint.origin,
                    )
                )

        if not driven_bodies:
            return {
                "name": "swept_clearance",
                "status": "skipped",
                "findings": [],
                "suppressed_count": 0,
                "sweep_samples": 0,
            }

        from server.tools_cad import cad_check_swept_clearance

        all_findings: list[dict[str, Any]] = []
        total_samples = 0

        for body_name, axis, center in driven_bodies:
            result = cad_check_swept_clearance(
                body=body_name,
                axis=list(axis),
                center=list(center),
                steps=36,
                doc=doc,
            )
            if not result.get("ok"):
                all_findings.append(
                    {
                        "reason_code": "TOOL_ERROR",
                        "message": f"Swept check failed for {body_name}: {result.get('error', {}).get('message', '')}",
                    }
                )
                continue

            total_samples += result.get("steps", 0)
            for v in result.get("violations", []):
                all_findings.append(
                    {
                        "reason_code": "DYNAMIC_SWEEP_COLLISION",
                        "body": body_name,
                        **v,
                    }
                )

        sweep_collisions = [
            f for f in all_findings if f.get("reason_code") == "DYNAMIC_SWEEP_COLLISION"
        ]
        tool_errors = [f for f in all_findings if f.get("reason_code") == "TOOL_ERROR"]

        if sweep_collisions:
            status = "fail"
        elif tool_errors:
            status = "warn"
        else:
            status = "pass"

        return {
            "name": "swept_clearance",
            "status": status,
            "findings": all_findings,
            "suppressed_count": 0,
            "sweep_samples": total_samples,
        }
    except Exception as exc:
        return {
            "name": "swept_clearance",
            "status": "skipped",
            "findings": [{"reason_code": "TOOL_ERROR", "message": str(exc)}],
            "suppressed_count": 0,
            "sweep_samples": 0,
        }


def _run_joint_connectivity(
    mechanism_id: str | None,
    doc: str | None,
) -> dict[str, Any]:
    """Stage 6: joint connectivity check."""
    if mechanism_id is None:
        return {
            "name": "joint_connectivity",
            "status": "skipped",
            "findings": [],
            "suppressed_count": 0,
        }
    try:
        from server.tools_motion import motion_check_joint_connectivity

        result = motion_check_joint_connectivity(mechanism_id, doc=doc)
        if not result.get("ok"):
            return {
                "name": "joint_connectivity",
                "status": "skipped",
                "findings": [
                    {
                        "reason_code": "TOOL_ERROR",
                        "message": result.get("error", {}).get("message", ""),
                    }
                ],
                "suppressed_count": 0,
            }

        disconnected = result.get("disconnected_joints", [])
        if disconnected:
            findings = [
                {
                    "reason_code": "CONNECTIVITY_FAIL",
                    **j,
                }
                for j in disconnected
            ]
            return {
                "name": "joint_connectivity",
                "status": "fail",
                "findings": findings,
                "suppressed_count": 0,
            }

        return {
            "name": "joint_connectivity",
            "status": "pass",
            "findings": [],
            "suppressed_count": 0,
        }
    except Exception as exc:
        return {
            "name": "joint_connectivity",
            "status": "skipped",
            "findings": [{"reason_code": "TOOL_ERROR", "message": str(exc)}],
            "suppressed_count": 0,
        }


def _run_motion_validators(
    mechanism_id: str | None,
) -> dict[str, Any]:
    """Stage 7: analytical motion validators (includes tooth interference)."""
    if mechanism_id is None:
        return {
            "name": "motion_validators",
            "status": "skipped",
            "findings": [],
            "suppressed_count": 0,
        }
    try:
        from server.tools_motion import motion_validate

        result = motion_validate(mechanism_id)
        if not result.get("ok"):
            return {
                "name": "motion_validators",
                "status": "fail",
                "findings": [
                    {
                        "reason_code": "TOOL_ERROR",
                        "message": result.get("error", {}).get("message", ""),
                    }
                ],
                "suppressed_count": 0,
            }

        validator_results = result.get("results", [])
        findings: list[dict[str, Any]] = []
        worst_status = "pass"

        for vr in validator_results:
            vr_status = vr.get("status", "pass")
            if vr_status == "fail":
                worst_status = "fail"
                findings.append(
                    {
                        "reason_code": "TOOTH_INTERFERENCE"
                        if vr["name"] == "mesh_phasing"
                        else vr["name"],
                        "validator": vr["name"],
                        "message": vr.get("message", ""),
                        "measured": vr.get("measured", {}),
                    }
                )
            elif vr_status == "warn" and worst_status != "fail":
                worst_status = "warn"
                findings.append(
                    {
                        "reason_code": vr["name"],
                        "validator": vr["name"],
                        "message": vr.get("message", ""),
                    }
                )

        return {
            "name": "motion_validators",
            "status": worst_status,
            "findings": findings,
            "suppressed_count": 0,
        }
    except Exception as exc:
        return {
            "name": "motion_validators",
            "status": "skipped",
            "findings": [{"reason_code": "TOOL_ERROR", "message": str(exc)}],
            "suppressed_count": 0,
        }
