"""MCP tool implementations for the design brief pipeline.

Tools: save_brief, get_brief, update_brief, add_part, update_part,
get_part, add_interface, list_briefs, verify_build.  The phased design
pipeline uses these to decompose assemblies into parts with tracked
interfaces and verify that all planned parts are built.
"""
from __future__ import annotations

import logging
from typing import Any

from server.design_models import InterfaceEntry, PartEntry
from server.design_store import (
    add_interface as store_add_interface,
    add_part as store_add_part,
    get_brief,
    get_part as store_get_part,
    list_briefs,
    store_brief,
    update_brief,
    update_part as store_update_part,
)

log = logging.getLogger("solidmind.tools_design")

_VALID_STATUSES = {"intent", "sizing", "layout", "approved", "building", "done"}
_VALID_PART_KINDS = {"custom", "purchased"}
_VALID_PART_STATUSES = {"pending", "building", "built"}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# ── Brief CRUD ──────────────────────────────────────────────────────

def design_save_brief(
    name: str,
    parameters: dict[str, Any],
    status: str = "intent",
    research_notes: str = "",
) -> dict[str, Any]:
    """Save a design brief.  Accepts any parameters dict.

    The LLM extracts parameters from user specs, research, or conversation
    and stores them here.  The user reviews and approves before building.
    """
    if not name:
        return _error_result("INVALID_INPUT", "Brief name is required")
    if not isinstance(parameters, dict):
        return _error_result("INVALID_INPUT", "Parameters must be a dict")
    if status not in _VALID_STATUSES:
        return _error_result("INVALID_INPUT", f"Invalid status '{status}'. Must be one of: {sorted(_VALID_STATUSES)}")

    brief = store_brief(
        name=name,
        parameters=parameters,
        status=status,
        research_notes=research_notes,
    )
    return {"ok": True, "brief": brief.to_dict()}


def design_get_brief(brief_id: str) -> dict[str, Any]:
    """Retrieve a saved brief by ID."""
    brief = get_brief(brief_id)
    if brief is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")
    return {"ok": True, "brief": brief.to_dict()}


def design_update_brief(
    brief_id: str,
    parameters: dict[str, Any] | None = None,
    status: str | None = None,
    research_notes: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Patch parameters, status, or notes on a brief."""
    if status is not None and status not in _VALID_STATUSES:
        return _error_result(
            "INVALID_INPUT",
            f"Invalid status '{status}'. Must be one of: {sorted(_VALID_STATUSES)}",
        )

    updated = update_brief(
        brief_id,
        parameters=parameters,
        status=status,
        research_notes=research_notes,
        name=name,
    )
    if updated is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    return {"ok": True, "brief": updated.to_dict()}


# ── Part management ─────────────────────────────────────────────────

def design_add_part(
    brief_id: str,
    name: str,
    kind: str = "custom",
    quantity: int = 1,
    specs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a part to the brief's parts list.

    kind: 'custom' (designed in CAD) or 'purchased' (off-the-shelf, specs
    constrain surrounding custom parts).
    specs: open dict — any key-value pairs relevant to this part
    (dimensions, material, model number, mounting pattern, etc.).
    """
    if not name:
        return _error_result("INVALID_INPUT", "Part name is required")
    if kind not in _VALID_PART_KINDS:
        return _error_result("INVALID_INPUT", f"Invalid kind '{kind}'. Must be one of: {sorted(_VALID_PART_KINDS)}")

    brief = get_brief(brief_id)
    if brief is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    # Check for duplicate name
    if brief.get_part(name) is not None:
        return _error_result("DUPLICATE_PART", f"Part '{name}' already exists in brief")

    part = PartEntry(
        name=name,
        kind=kind,
        quantity=quantity,
        specs=specs or {},
    )
    updated = store_add_part(brief_id, part)
    if updated is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    return {"ok": True, "part": part.to_dict(), "part_count": len(updated.parts)}


def design_update_part(
    brief_id: str,
    name: str,
    specs: dict[str, Any] | None = None,
    status: str | None = None,
    body_label: str | None = None,
    kind: str | None = None,
    quantity: int | None = None,
) -> dict[str, Any]:
    """Update fields on a named part.

    Use this to mark parts as built, attach body labels after cad.new_body,
    or update specs with refined dimensions.
    """
    if status is not None and status not in _VALID_PART_STATUSES:
        return _error_result(
            "INVALID_INPUT",
            f"Invalid part status '{status}'. Must be one of: {sorted(_VALID_PART_STATUSES)}",
        )
    if kind is not None and kind not in _VALID_PART_KINDS:
        return _error_result(
            "INVALID_INPUT",
            f"Invalid kind '{kind}'. Must be one of: {sorted(_VALID_PART_KINDS)}",
        )

    fields: dict[str, Any] = {}
    if specs is not None:
        fields["specs"] = specs
    if status is not None:
        fields["status"] = status
    if body_label is not None:
        fields["body_label"] = body_label
    if kind is not None:
        fields["kind"] = kind
    if quantity is not None:
        fields["quantity"] = quantity

    updated = store_update_part(brief_id, name, **fields)
    if updated is None:
        return _error_result("NOT_FOUND", f"Brief '{brief_id}' or part '{name}' not found")

    part = updated.get_part(name)
    return {"ok": True, "part": part.to_dict() if part else {}}


def design_get_part(brief_id: str, name: str) -> dict[str, Any]:
    """Retrieve a single part and its interfaces.

    Returns the part's specs plus all interfaces involving it — everything
    the LLM needs to build that part without fetching the whole brief.
    """
    part, interfaces = store_get_part(brief_id, name)
    if part is None:
        return _error_result("NOT_FOUND", f"Brief '{brief_id}' or part '{name}' not found")

    return {
        "ok": True,
        "part": part.to_dict(),
        "interfaces": [i.to_dict() for i in interfaces],
    }


# ── Interface management ────────────────────────────────────────────

def design_add_interface(
    brief_id: str,
    part_a: str,
    port_a: str,
    part_b: str,
    port_b: str,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Define a connection between two parts.

    port_a/port_b: named connection points on each part (e.g., 'top',
    'base', 'arm_slot').
    spec: open dict describing the physical connection (bolt pattern,
    press fit, clamp, etc.).
    """
    if not part_a or not part_b:
        return _error_result("INVALID_INPUT", "Both part_a and part_b are required")

    brief = get_brief(brief_id)
    if brief is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    # Validate that both parts exist
    if brief.get_part(part_a) is None:
        return _error_result("PART_NOT_FOUND", f"Part '{part_a}' not found in brief")
    if brief.get_part(part_b) is None:
        return _error_result("PART_NOT_FOUND", f"Part '{part_b}' not found in brief")

    iface = InterfaceEntry(
        part_a=part_a,
        port_a=port_a,
        part_b=part_b,
        port_b=port_b,
        spec=spec or {},
    )
    updated = store_add_interface(brief_id, iface)
    if updated is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    return {"ok": True, "interface": iface.to_dict(), "interface_count": len(updated.interfaces)}


# ── List briefs ────────────────────────────────────────────────────

def design_list_briefs() -> dict[str, Any]:
    """Return summary info for all stored briefs."""
    return {"ok": True, "briefs": list_briefs()}


# ── Build verification ─────────────────────────────────────────────

def _check_dimension(
    spec_key: str,
    spec_val: float,
    body_size: list[float],
) -> str | None:
    """Compare a single spec dimension against body bounding box size.

    Returns a warning string if the dimension is outside tolerance, else None.
    Tolerance: 10% or 1mm, whichever is larger.
    """
    if not isinstance(spec_val, (int, float)) or not body_size:
        return None

    sorted_size = sorted(body_size)

    if spec_key == "diameter_mm":
        actual = max(body_size[0], body_size[1]) if len(body_size) >= 2 else body_size[0]
    elif spec_key == "thickness_mm":
        actual = sorted_size[0]
    elif spec_key in ("length_mm", "width_mm", "height_mm"):
        # Map length→largest, width→middle, height→smallest for sorted comparison
        dim_rank = {"length_mm": -1, "width_mm": -2, "height_mm": -3}
        idx = dim_rank.get(spec_key, -1)
        if abs(idx) <= len(sorted_size):
            actual = sorted_size[idx]
        else:
            actual = sorted_size[-1]
    else:
        return None

    tol = max(abs(spec_val) * 0.10, 1.0)
    if abs(actual - spec_val) > tol:
        return (
            f"{spec_key}: expected {spec_val:.1f}mm, "
            f"got {actual:.1f}mm (delta {abs(actual - spec_val):.1f}mm)"
        )
    return None


def design_verify_build(
    brief_id: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Verify that all planned parts from a design brief exist in FreeCAD.

    Compares the brief's parts list against the model tree from FreeCAD.
    For each custom part, classifies as OK / MISSING / PARTIAL / STALE.
    Also checks bounding box dimensions against part specs.
    """
    from server.tools_cad import cad_get_model_tree  # noqa: C0415

    brief = get_brief(brief_id)
    if brief is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    # Get model tree from FreeCAD
    tree_result = cad_get_model_tree(doc=doc, detail="bodies")
    if not tree_result.get("ok", False):
        return _error_result(
            "MODEL_TREE_ERROR",
            f"Could not get model tree: {tree_result.get('error', {}).get('message', 'unknown')}",
        )

    bodies = tree_result.get("bodies", [])

    # Build lookups: by label (exact) and by label (case-insensitive)
    body_by_label: dict[str, dict[str, Any]] = {}
    body_by_label_lower: dict[str, list[dict[str, Any]]] = {}
    for b in bodies:
        label = b.get("label", b.get("name", ""))
        body_by_label[label] = b
        lower = label.lower()
        body_by_label_lower.setdefault(lower, []).append(b)

    matched_labels: set[str] = set()
    parts_report: list[dict[str, Any]] = []
    action_items: list[str] = []
    custom_planned = 0
    custom_found = 0
    purchased_count = 0

    for part in brief.parts:
        if part.kind == "purchased":
            purchased_count += 1
            parts_report.append({
                "name": part.name,
                "kind": "purchased",
                "quantity": part.quantity,
                "status_in_brief": part.status,
                "body_label": part.body_label,
                "found_bodies": [],
                "found_count": 0,
                "verdict": "PURCHASED_SKIPPED",
                "dimension_warnings": [],
            })
            continue

        custom_planned += 1
        found_bodies: list[str] = []

        # Strategy 1: exact body_label match
        if part.body_label and part.body_label in body_by_label:
            found_bodies.append(part.body_label)
            matched_labels.add(part.body_label)

        # Strategy 2: case-insensitive name match (for quantity > 1 or no body_label)
        if not found_bodies or part.quantity > 1:
            name_lower = part.name.lower()
            for label, b_list in body_by_label_lower.items():
                if name_lower in label or label in name_lower:
                    for b in b_list:
                        bl = b.get("label", b.get("name", ""))
                        if bl not in found_bodies:
                            found_bodies.append(bl)
                            matched_labels.add(bl)

            # Also check body_label pattern for multi-quantity
            if part.body_label:
                bl_lower = part.body_label.lower()
                for label, b_list in body_by_label_lower.items():
                    if bl_lower in label or label in bl_lower:
                        for b in b_list:
                            bl = b.get("label", b.get("name", ""))
                            if bl not in found_bodies:
                                found_bodies.append(bl)
                                matched_labels.add(bl)

        found_count = len(found_bodies)

        # Classify verdict
        if part.status == "built" and found_count == 0:
            verdict = "STALE"
        elif found_count == 0:
            verdict = "MISSING"
        elif part.quantity > 1 and found_count < part.quantity:
            verdict = "PARTIAL"
        else:
            verdict = "OK"
            custom_found += 1

        # Dimension checks for found bodies
        dim_warnings: list[str] = []
        dim_keys = {"diameter_mm", "thickness_mm", "length_mm", "width_mm", "height_mm"}
        for bl in found_bodies:
            body_data = body_by_label.get(bl, {})
            body_size = body_data.get("size", [])
            if not body_size:
                continue
            for sk, sv in part.specs.items():
                if sk in dim_keys:
                    warning = _check_dimension(sk, sv, body_size)
                    if warning:
                        dim_warnings.append(f"{bl}: {warning}")

        if verdict in ("MISSING", "STALE"):
            status_note = f"status: {part.status}"
            if part.quantity > 1:
                action_items.append(f"Build {part.name} (quantity {part.quantity}, {status_note})")
            else:
                action_items.append(f"Build {part.name} ({status_note})")
        elif verdict == "PARTIAL":
            action_items.append(
                f"Build remaining {part.name}: {found_count}/{part.quantity} found"
            )

        parts_report.append({
            "name": part.name,
            "kind": part.kind,
            "quantity": part.quantity,
            "status_in_brief": part.status,
            "body_label": part.body_label,
            "found_bodies": found_bodies,
            "found_count": found_count,
            "verdict": verdict,
            "dimension_warnings": dim_warnings,
        })

    # Unmatched bodies (in FreeCAD but not in any brief part)
    all_labels = {b.get("label", b.get("name", "")) for b in bodies}
    unmatched = sorted(all_labels - matched_labels)

    completeness = (custom_found / custom_planned * 100.0) if custom_planned > 0 else 100.0

    return {
        "ok": True,
        "summary": {
            "custom_parts_planned": custom_planned,
            "custom_parts_found": custom_found,
            "custom_parts_missing": custom_planned - custom_found,
            "purchased_parts": purchased_count,
            "completeness_pct": round(completeness, 1),
        },
        "parts": parts_report,
        "action_items": action_items,
        "unmatched_bodies": unmatched,
    }
