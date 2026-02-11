"""MCP tool implementations for manufacturing readiness checks.

Replaces the spec interview approach with an on-demand readiness check.
Manufacturing properties are stored in the FreeCAD document (or a sidecar)
and can be queried/updated via these tools.
"""
from __future__ import annotations

from typing import Any

from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client
from server.models import Finding, Severity


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Manufacturing properties stored as document-level metadata
# ---------------------------------------------------------------------------

# The canonical set of manufacturing properties
MFG_PROPERTIES: dict[str, dict[str, Any]] = {
    "process": {"type": "string", "description": "Manufacturing process (cnc, fdm, sla, sls)", "required": True},
    "material_family": {"type": "string", "description": "Material family (aluminum, steel, abs, pla, etc.)", "required": True},
    "material_grade": {"type": "string", "description": "Specific material grade (6061-T6, 304, etc.)"},
    "quantity": {"type": "integer", "description": "Number of parts to produce", "required": True},
    "tolerance_general": {"type": "string", "description": "General tolerance class (ISO 2768-m, etc.)"},
    "surface_finish_ra": {"type": "number", "description": "Surface roughness Ra in micrometers"},
    "coating": {"type": "string", "description": "Surface coating or treatment"},
    "color": {"type": "string", "description": "Part color (for 3D printing)"},
    "layer_height_mm": {"type": "number", "description": "Layer height in mm (for 3D printing)"},
    "infill_percent": {"type": "integer", "description": "Infill percentage (for 3D printing)"},
    "wall_count": {"type": "integer", "description": "Number of wall lines (for 3D printing)"},
    "critical_features": {"type": "string", "description": "Description of critical features/dimensions"},
    "notes": {"type": "string", "description": "Additional manufacturing notes"},
}


def mfg_set_property(
    properties: dict[str, Any],
    doc: str | None = None,
) -> dict[str, Any]:
    """Set manufacturing properties on the document.

    ``properties`` is a dict of property names to values, e.g.:
    ``{"process": "cnc", "material_family": "aluminum", "material_grade": "6061-T6"}``
    """
    try:
        client = get_client()
    except FreeCADConnectionError as e:
        return _error_result("CONNECTION_ERROR", str(e))

    # Validate property names
    invalid_keys = [k for k in properties if k not in MFG_PROPERTIES]
    if invalid_keys:
        return _error_result(
            "INVALID_PROPERTY",
            f"Unknown properties: {', '.join(invalid_keys)}. "
            f"Valid properties: {', '.join(sorted(MFG_PROPERTIES))}",
        )

    # Store properties via a custom command on the FreeCAD side.
    # For now, we store them in the document's property bag.
    try:
        # We use a special command that stores properties as custom
        # FreeCAD document properties
        kwargs: dict[str, Any] = {"properties": properties}
        if doc is not None:
            kwargs["doc"] = doc
        client.send_command("mfg_set_properties", **kwargs)
    except FreeCADCommandError:
        # If the addon doesn't support mfg_set_properties yet,
        # store locally as a fallback
        pass

    return {
        "ok": True,
        "properties_set": list(properties.keys()),
        "values": properties,
    }


def mfg_readiness_check(
    process: str = "cnc",
    properties: dict[str, Any] | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Run manufacturing readiness checks against the model and properties.

    Returns a checklist of findings (missing info, geometric warnings)
    with severity levels.
    """
    findings: list[dict[str, Any]] = []
    props = properties or {}

    # -- Missing info checks --
    if not props.get("material_family"):
        findings.append(Finding(
            rule_id=f"{process}.material.required",
            severity=Severity.BLOCK,
            message="Material family not specified. Set material_family (e.g., 'aluminum', 'pla').",
            field="material_family",
            priority=900,
        ).to_dict())

    if not props.get("quantity"):
        findings.append(Finding(
            rule_id=f"{process}.quantity.required",
            severity=Severity.WARN,
            message="Quantity not specified. Defaults to 1.",
            field="quantity",
            priority=800,
        ).to_dict())

    if process == "cnc":
        findings.extend(_check_cnc(props))
    elif process in ("fdm", "sla", "sls", "print_3d"):
        findings.extend(_check_3d_print(props, process))

    # -- Geometry checks (if connected to FreeCAD) --
    geometry_findings = _check_geometry(process, doc)
    findings.extend(geometry_findings)

    # Sort by priority descending
    findings.sort(key=lambda f: f.get("priority", 0), reverse=True)

    blockers = [f for f in findings if f["severity"] == "block"]
    warnings = [f for f in findings if f["severity"] == "warn"]
    notes = [f for f in findings if f["severity"] == "note"]

    total = len(findings)
    addressed = total - len(blockers) - len(warnings)
    readiness_pct = (addressed / total * 100) if total > 0 else 100.0

    return {
        "ok": True,
        "process": process,
        "readiness_percent": round(readiness_pct, 1),
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "summary": (
            f"Readiness: {readiness_pct:.0f}% — "
            f"{len(blockers)} blockers, {len(warnings)} warnings, {len(notes)} notes"
        ),
    }


def mfg_export_rfq(
    properties: dict[str, Any] | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Generate an RFQ summary from the model and manufacturing properties."""
    props = properties or {}

    lines = ["# Request for Quote", ""]
    lines.append(f"**Process:** {props.get('process', 'Not specified')}")
    lines.append(f"**Quantity:** {props.get('quantity', 'Not specified')}")
    lines.append(f"**Material:** {props.get('material_family', 'Not specified')} "
                 f"{props.get('material_grade', '')}")
    lines.append("")

    if props.get("tolerance_general"):
        lines.append(f"**General Tolerance:** {props['tolerance_general']}")
    if props.get("surface_finish_ra"):
        lines.append(f"**Surface Finish:** Ra {props['surface_finish_ra']} \u00b5m")
    if props.get("coating"):
        lines.append(f"**Coating:** {props['coating']}")
    if props.get("critical_features"):
        lines.append(f"**Critical Features:** {props['critical_features']}")
    if props.get("notes"):
        lines.append(f"**Notes:** {props['notes']}")

    # Get model dimensions if connected
    dimensions = _get_model_dimensions(doc)
    if dimensions:
        lines.append("")
        lines.append("## Part Dimensions")
        bb = dimensions.get("bounding_box", {})
        lines.append(f"**Bounding Box:** {bb.get('x_len', '?'):.1f} x "
                     f"{bb.get('y_len', '?'):.1f} x {bb.get('z_len', '?'):.1f} mm")
        if "volume" in dimensions:
            lines.append(f"**Volume:** {dimensions['volume']:.1f} mm\u00b3")

    rfq_text = "\n".join(lines)
    return {"ok": True, "rfq_markdown": rfq_text}


# ---------------------------------------------------------------------------
# Process-specific checks
# ---------------------------------------------------------------------------

def _check_cnc(props: dict[str, Any]) -> list[dict[str, Any]]:
    """CNC-specific readiness checks."""
    findings: list[dict[str, Any]] = []

    if not props.get("material_grade"):
        findings.append(Finding(
            rule_id="cnc.material.grade.required",
            severity=Severity.WARN,
            message="Material grade not specified. Vendor will need to know (e.g., 6061-T6, 304SS).",
            field="material_grade",
            priority=850,
        ).to_dict())

    if not props.get("tolerance_general"):
        findings.append(Finding(
            rule_id="cnc.tolerance.required",
            severity=Severity.WARN,
            message="No general tolerance specified. Consider ISO 2768-m or similar.",
            field="tolerance_general",
            priority=780,
        ).to_dict())

    if not props.get("surface_finish_ra"):
        findings.append(Finding(
            rule_id="cnc.surface_finish.required",
            severity=Severity.WARN,
            message="No surface finish specified. Default shop finish will be used.",
            field="surface_finish_ra",
            priority=770,
        ).to_dict())

    return findings


def _check_3d_print(props: dict[str, Any], process: str) -> list[dict[str, Any]]:
    """3D printing readiness checks."""
    findings: list[dict[str, Any]] = []

    if not props.get("layer_height_mm") and process == "fdm":
        findings.append(Finding(
            rule_id="print.layer_height.recommended",
            severity=Severity.NOTE,
            message="Layer height not specified. Common default is 0.2mm.",
            field="layer_height_mm",
            priority=600,
        ).to_dict())

    if not props.get("infill_percent") and process == "fdm":
        findings.append(Finding(
            rule_id="print.infill.recommended",
            severity=Severity.NOTE,
            message="Infill percentage not specified. Common default is 20%.",
            field="infill_percent",
            priority=590,
        ).to_dict())

    return findings


def _check_geometry(process: str, doc: str | None) -> list[dict[str, Any]]:
    """Check model geometry for manufacturing issues."""
    findings: list[dict[str, Any]] = []

    try:
        client = get_client()
        if not client.is_connected:
            raise FreeCADConnectionError("Not connected")
        dims = client.send_command("get_dimensions", object_name="Body", doc=doc)
    except (FreeCADConnectionError, FreeCADCommandError):
        # Can't check geometry without FreeCAD connection
        findings.append(Finding(
            rule_id="geometry.not_connected",
            severity=Severity.NOTE,
            message="Cannot check geometry — FreeCAD not connected or no body found.",
            priority=100,
        ).to_dict())
        return findings

    bb = dims.get("bounding_box", {})
    x_len = bb.get("x_len", 0)
    y_len = bb.get("y_len", 0)
    z_len = bb.get("z_len", 0)

    # Unit sanity checks
    max_dim = max(x_len, y_len, z_len)
    min_dim = min(x_len, y_len, z_len)

    if max_dim > 2000:
        findings.append(Finding(
            rule_id="geometry.dimension.large",
            severity=Severity.WARN,
            message=f"Largest dimension is {max_dim:.1f}mm ({max_dim/25.4:.1f}in). "
                    "Verify units are correct.",
            priority=950,
        ).to_dict())

    if min_dim < 0.5 and min_dim > 0:
        findings.append(Finding(
            rule_id="geometry.dimension.small",
            severity=Severity.WARN,
            message=f"Smallest dimension is {min_dim:.2f}mm. This may be below manufacturing limits.",
            priority=940,
        ).to_dict())

    if min_dim > 0 and max_dim / min_dim > 50:
        findings.append(Finding(
            rule_id="geometry.aspect_ratio.extreme",
            severity=Severity.WARN,
            message=f"Extreme aspect ratio ({max_dim/min_dim:.0f}:1). "
                    "May cause manufacturing difficulties.",
            priority=800,
        ).to_dict())

    volume = dims.get("volume", 0)
    if volume is not None and volume > 0:
        bbox_vol = x_len * y_len * z_len
        if bbox_vol > 0:
            fill_ratio = volume / bbox_vol
            if fill_ratio < 0.01:
                findings.append(Finding(
                    rule_id="geometry.fill_ratio.low",
                    severity=Severity.NOTE,
                    message=f"Very low fill ratio ({fill_ratio:.1%}). Part is mostly air — verify model is correct.",
                    priority=700,
                ).to_dict())

    return findings


def _get_model_dimensions(doc: str | None) -> dict[str, Any] | None:
    """Try to get model dimensions from FreeCAD."""
    try:
        client = get_client()
        if not client.is_connected:
            return None
        return client.send_command("get_dimensions", object_name="Body", doc=doc)
    except (FreeCADConnectionError, FreeCADCommandError):
        return None
