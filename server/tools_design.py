"""MCP tool implementations for the design brief pipeline.

Tools: save_brief, get_brief, update_brief, add_part, update_part,
get_part, add_interface, list_briefs, verify_build, generate_mechanism.
The phased design pipeline uses these to decompose assemblies into parts
with tracked interfaces and verify that all planned parts are built.
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
from server.motion_models import JointEdge, JointType, Mechanism, PartNode

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


def _extract_placement_plan(
    brief: Any,
) -> dict[str, dict[str, Any]]:
    """Extract a placement plan dict from a design brief.

    Reads ``brief.parameters["layout"]["positions"]`` and per-part specs
    for position, rotation, and size information.

    Returns a dict mapping body labels (or part names) to plan entries.
    """
    from server.design_models import DesignBrief  # noqa: C0415

    plan: dict[str, dict[str, Any]] = {}
    layout = brief.parameters.get("layout", {})
    positions = layout.get("positions", {})

    for part in brief.parts:
        if part.kind == "purchased":
            continue

        # Determine the label to use (body_label if set, else part name)
        label = part.body_label or part.name

        # Find position: positions dict, then part specs
        pos = positions.get(part.name)
        if pos is None:
            pos = part.specs.get("position_mm") or part.specs.get("position")
        if pos is None:
            continue
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            continue

        entry: dict[str, Any] = {
            "position": [float(pos[0]), float(pos[1]), float(pos[2])],
        }

        # Rotation from part specs
        rot_axis = part.specs.get("rotation_axis")
        if isinstance(rot_axis, (list, tuple)) and len(rot_axis) >= 3:
            entry["rotation_axis"] = [float(v) for v in rot_axis[:3]]
        rot_angle = part.specs.get("rotation_angle_deg")
        if isinstance(rot_angle, (int, float)):
            entry["rotation_angle_deg"] = float(rot_angle)

        # Size from dimensional specs
        size_keys = ("length_mm", "width_mm", "height_mm")
        dims = [part.specs.get(k) for k in size_keys]
        if all(isinstance(d, (int, float)) for d in dims):
            entry["expected_size"] = [float(d) for d in dims]

        plan[label] = entry

    return plan


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

    # Read old brief before update to detect transitions
    old_brief = get_brief(brief_id)

    updated = update_brief(
        brief_id,
        parameters=parameters,
        status=status,
        research_notes=research_notes,
        name=name,
    )
    if updated is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    result: dict[str, Any] = {"ok": True, "brief": updated.to_dict()}

    # Auto-register placement plan on transition to "building"
    if (
        status == "building"
        and old_brief is not None
        and old_brief.status != "building"
    ):
        try:
            plan = _extract_placement_plan(updated)
            if plan:
                from server.tools_cad import cad_register_placement_plan  # noqa: C0415

                plan_result = cad_register_placement_plan(plan=plan)
                if plan_result.get("ok"):
                    result["placement_plan_registered"] = plan_result.get(
                        "registered", 0
                    )
        except Exception:
            log.debug("Failed to auto-register placement plan", exc_info=True)

    return result


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


# ── Mechanism generation ───────────────────────────────────────────

# Mapping from interface spec keywords to joint types.
_FIXED_KEYWORDS = frozenset({
    "bolt", "bolt_pair", "screw", "rivet", "weld", "glue", "adhesive",
    "clamp", "press_fit", "press-fit", "pressfit", "snap", "snap_fit",
    "fixed", "bonded",
})
_REVOLUTE_KEYWORDS = frozenset({
    "bearing", "shaft", "hinge", "pivot", "bushing", "journal",
    "revolute", "rotation", "axle",
})
_PRISMATIC_KEYWORDS = frozenset({
    "slider", "rail", "linear", "slide", "prismatic", "guide",
})


def _classify_joint_type(spec: dict[str, Any]) -> JointType:
    """Infer joint type from an interface spec dict.

    Checks the ``type`` key and ``pattern`` key for keywords.  Falls back
    to ``fixed`` when nothing matches — bolted/clamped connections are the
    most common assembly interface.
    """
    tokens: list[str] = []
    for key in ("type", "pattern"):
        val = spec.get(key, "")
        if isinstance(val, str):
            tokens.extend(val.lower().replace("-", "_").split("_"))

    token_set = set(tokens)
    if token_set & _REVOLUTE_KEYWORDS:
        return JointType.REVOLUTE
    if token_set & _PRISMATIC_KEYWORDS:
        return JointType.PRISMATIC
    if token_set & _FIXED_KEYWORDS:
        return JointType.FIXED

    # Default: if spec has a bolt size or pattern, treat as fixed
    if spec.get("bolt_size") or spec.get("pattern"):
        return JointType.FIXED

    return JointType.FIXED


def _resolve_origin(
    part_name: str,
    layout: dict[str, Any],
    part_specs: dict[str, Any],
) -> tuple[float, float, float]:
    """Derive a joint origin from brief layout or part specs.

    Checks layout.motor_positions (by part name pattern), layout.<part>_position,
    and falls back to part specs position_mm / origin_mm.
    """
    # Check motor_positions if part name contains "motor"
    motor_positions = layout.get("motor_positions", [])
    if "motor" in part_name.lower() and motor_positions:
        # Return first motor position as representative
        pos = motor_positions[0]
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            return (float(pos[0]), float(pos[1]), float(pos[2]))

    # Check layout.<part_name>_position
    pos_key = f"{part_name}_position"
    pos = layout.get(pos_key)
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        return (float(pos[0]), float(pos[1]), float(pos[2]))

    # Check part specs
    for key in ("position_mm", "origin_mm", "position"):
        pos = part_specs.get(key)
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            return (float(pos[0]), float(pos[1]), float(pos[2]))

    return (0.0, 0.0, 0.0)


def _resolve_mass(part: PartEntry, default_kg: float = 0.1) -> float:
    """Get part mass from specs.  Checks mass_g and mass_kg keys."""
    mass_g = part.specs.get("mass_g")
    if isinstance(mass_g, (int, float)) and mass_g > 0:
        return float(mass_g) / 1000.0
    mass_kg = part.specs.get("mass_kg")
    if isinstance(mass_kg, (int, float)) and mass_kg > 0:
        return float(mass_kg)
    return default_kg


def design_generate_mechanism(
    brief_id: str,
    ground_part: str | None = None,
) -> dict[str, Any]:
    """Auto-generate a Mechanism definition from a design brief.

    Reads all parts and interfaces from the brief.  Maps interface spec
    types to joint types (bolt/clamp → fixed, bearing/shaft → revolute,
    slider/rail → prismatic).  Derives joint origins from the brief's
    layout parameters or part specs.

    Returns a mechanism dict ready to pass to ``motion.define_mechanism()``.
    The user can review/edit before committing.

    Parameters
    ----------
    brief_id : str
        The design brief to generate from.
    ground_part : str | None
        Name of the part to treat as ground (is_ground=True).  If None,
        uses the first part in the brief (typically the frame/base).
    """
    brief = get_brief(brief_id)
    if brief is None:
        return _error_result("BRIEF_NOT_FOUND", f"No brief with id '{brief_id}'")

    if not brief.parts:
        return _error_result("NO_PARTS", "Brief has no parts defined")

    if not brief.interfaces:
        return _error_result("NO_INTERFACES", "Brief has no interfaces defined — add interfaces first")

    # Determine ground part
    ground_name = ground_part or brief.parts[0].name

    # Build layout dict from brief parameters
    layout = brief.parameters.get("layout", {})

    # Build PartNodes
    part_nodes: list[PartNode] = []
    part_names: set[str] = set()
    for part in brief.parts:
        part_names.add(part.name)
        part_nodes.append(PartNode(
            id=part.name,
            body_name=part.body_label or part.name,
            mass_kg=_resolve_mass(part),
            is_ground=(part.name == ground_name),
        ))

    # Build JointEdges from interfaces
    joint_edges: list[JointEdge] = []
    for idx, iface in enumerate(brief.interfaces):
        joint_type = _classify_joint_type(iface.spec)

        # Derive origin from child part's layout position
        child_specs = {}
        child_part = brief.get_part(iface.part_b)
        if child_part:
            child_specs = child_part.specs
        origin = _resolve_origin(iface.part_b, layout, child_specs)

        joint_id = f"joint_{iface.part_a}_{iface.part_b}_{idx}"

        joint_edges.append(JointEdge(
            id=joint_id,
            joint_type=joint_type,
            parent_part=iface.part_a,
            child_part=iface.part_b,
            origin=origin,
        ))

    # Build mechanism dict (not stored yet — user reviews first)
    mechanism = Mechanism(
        name=brief.name,
        parts=tuple(part_nodes),
        joints=tuple(joint_edges),
        drives=(),
    )

    return {
        "ok": True,
        "mechanism": mechanism.to_dict(),
        "summary": {
            "part_count": len(part_nodes),
            "joint_count": len(joint_edges),
            "ground_part": ground_name,
            "joint_types": {jt.value: sum(1 for j in joint_edges if j.joint_type == jt)
                           for jt in JointType if any(j.joint_type == jt for j in joint_edges)},
        },
        "hint": (
            "Review the mechanism dict above.  When ready, pass it to "
            "motion.define_mechanism() to register it for simulation."
        ),
    }


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
    mechanism_id: str | None = None,
    check_clearance: bool = False,
    clearance_threshold_mm: float = 0.5,
) -> dict[str, Any]:
    """Verify that all planned parts from a design brief exist in FreeCAD.

    Compares the brief's parts list against the model tree from FreeCAD.
    For each custom part, classifies as OK / MISSING / PARTIAL / STALE.
    Also checks bounding box dimensions against part specs.

    If ``mechanism_id`` is provided, also checks that every interface in the
    brief has a corresponding joint in the mechanism connecting the same
    two parts.  Unconnected interfaces are reported as warnings.
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

    # Interface-joint coverage check
    interface_warnings: list[str] = []
    if mechanism_id is not None:
        from server import motion_store  # noqa: C0415

        mech = motion_store.get(mechanism_id)
        if mech is None:
            interface_warnings.append(
                f"Mechanism '{mechanism_id}' not found — skipping interface-joint check"
            )
        else:
            # Build a set of connected part pairs from mechanism joints
            connected_pairs: set[frozenset[str]] = set()
            for joint in mech.joints:
                connected_pairs.add(frozenset({joint.parent_part, joint.child_part}))

            for iface in brief.interfaces:
                pair = frozenset({iface.part_a, iface.part_b})
                if pair not in connected_pairs:
                    interface_warnings.append(
                        f"Interface {iface.part_a}:{iface.port_a} ↔ "
                        f"{iface.part_b}:{iface.port_b} has no corresponding "
                        f"joint in mechanism"
                    )

    # Optional batch clearance check
    clearance_violations: list[dict[str, Any]] = []
    if check_clearance:
        from server.tools_cad import cad_check_clearance  # noqa: C0415

        cl_result = cad_check_clearance(
            threshold_mm=clearance_threshold_mm, doc=doc,
        )
        if cl_result.get("ok", False):
            clearance_violations = cl_result.get("violations", [])
            for v in clearance_violations:
                if v.get("intersecting"):
                    action_items.append(
                        f"Clearance: {v['body_a']} intersects {v['body_b']}"
                    )
                else:
                    action_items.append(
                        f"Clearance: {v['body_a']} ↔ {v['body_b']} = "
                        f"{v['distance_mm']}mm (< {clearance_threshold_mm}mm)"
                    )

    result: dict[str, Any] = {
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
    if interface_warnings:
        result["interface_warnings"] = interface_warnings
    if clearance_violations:
        result["clearance_violations"] = clearance_violations
    return result
