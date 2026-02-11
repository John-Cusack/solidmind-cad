from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

from server.jsonutil import dumps as json_dumps
from server.jsonutil import loads as json_loads
from server.prompts import get_prompt, list_prompts
from server.resources import list_resources, read_resource
from server.tools import (
    spec_apply_answer,
    spec_export_brief,
    spec_export_rfq_summary,
    spec_finalize,
    spec_generate_cad,
    spec_next_question,
    spec_select_schema,
    spec_validate,
)
from server.tools_cad import (
    cad_chamfer,
    cad_define_selection,
    cad_delete_selection,
    cad_export,
    cad_fillet,
    cad_find_edges,
    cad_get_body_topology,
    cad_get_dimensions,
    cad_get_model_tree,
    cad_get_selection,
    cad_hole,
    cad_list_selections,
    cad_new_body,
    cad_new_document,
    cad_pad,
    cad_pocket,
    cad_polar_pattern,
    cad_resolve_selection,
    cad_revolution,
    cad_sketch,
    cad_undo,
)
from server.tools_mfg import (
    mfg_export_rfq,
    mfg_readiness_check,
    mfg_set_property,
)


def _json_dumps(obj: Any) -> bytes:
    return json_dumps(obj)


def _send(msg: dict[str, Any]) -> None:
    log = logging.getLogger("mcp.io")
    payload = _json_dumps(msg)
    log.debug("send id=%s len=%d", msg.get("id"), len(payload))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _read_message() -> dict[str, Any] | None:
    log = logging.getLogger("mcp.io")
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            log.debug("stdin EOF")
            return None
        if line in (b"\r\n", b"\n"):
            continue

        # Check if this looks like an HTTP-style header (contains ':').
        # LSP/MCP framing can send Content-Type before Content-Length.
        stripped = line.strip()
        if b":" in stripped and not stripped.startswith(b"{"):
            # LSP-style framing: read headers until blank line, then body.
            headers = {}
            while line not in (b"\r\n", b"\n"):
                k, _, v = line.partition(b":")
                headers[k.strip().lower()] = v.strip()
                line = stdin.readline()
                if not line:
                    return None
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                log.error("Invalid Content-Length header")
                return None
            if length == 0:
                continue
            body = stdin.read(length)
            if not body:
                return None
            msg = json_loads(body)
            log.debug("recv method=%s id=%s", msg.get("method"), msg.get("id"))
            return msg

        # Fallback: assume newline-delimited JSON for manual debugging.
        log.debug("fallback newline-JSON parse")
        return json_loads(line.decode("utf-8").strip())


def _rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _rpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _cad_tool_list() -> list[dict[str, Any]]:
    """CAD geometry tools — drive FreeCAD PartDesign directly."""
    return [
        {
            "name": "cad.new_document",
            "description": "Create a new FreeCAD document.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Document name", "default": "Unnamed"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.new_body",
            "description": "Create a PartDesign Body in the document.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Body name", "default": "Body"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.sketch",
            "description": (
                "Create a sketch with geometry and constraints. Combines sketch creation, "
                "geometry addition, constraint application, and sketch closing into one call. "
                "Elements: rect, circle, line, arc. Constraints: Coincident, Horizontal, "
                "Vertical, Distance, Radius, Angle, etc."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Body name to attach sketch to"},
                    "plane": {
                        "type": "string",
                        "description": "Sketch plane: XY, XZ, YZ, or a face reference like 'Face1'",
                        "default": "XY",
                    },
                    "elements": {
                        "type": "array",
                        "description": "Geometry elements to add to the sketch",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["rect", "circle", "line", "arc"]},
                            },
                            "required": ["type"],
                        },
                    },
                    "constraints": {
                        "type": "array",
                        "description": "Constraints to apply to the sketch geometry",
                        "items": {"type": "object"},
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.pad",
            "description": "Extrude (pad) a sketch to create a solid. Creates a PartDesign::Pad feature.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sketch": {"type": "string", "description": "Sketch name to pad"},
                    "length": {"type": "number", "description": "Extrusion length in mm"},
                    "symmetric": {"type": "boolean", "description": "Pad symmetrically from sketch plane", "default": False},
                    "reversed": {"type": "boolean", "description": "Reverse pad direction", "default": False},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["sketch", "length"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.revolution",
            "description": (
                "Revolve a sketch around an axis to create a solid of revolution. "
                "Creates a PartDesign::Revolution feature."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sketch": {"type": "string", "description": "Sketch name to revolve"},
                    "axis": {
                        "type": "string",
                        "enum": ["V", "H", "Base_X", "Base_Y", "Base_Z"],
                        "description": "Revolution axis: V (sketch vertical), H (sketch horizontal), or Base_X/Y/Z (document origin axes)",
                        "default": "V",
                    },
                    "angle": {"type": "number", "description": "Revolution angle in degrees", "default": 360.0},
                    "symmetric": {"type": "boolean", "description": "Revolve symmetrically from sketch plane", "default": False},
                    "reversed": {"type": "boolean", "description": "Reverse revolution direction", "default": False},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["sketch"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.polar_pattern",
            "description": (
                "Create a polar (circular) pattern of features around an axis. "
                "Creates a PartDesign::PolarPattern feature."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "features": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Feature names to pattern (e.g., ['Pocket'])",
                    },
                    "axis": {
                        "type": "string",
                        "enum": ["Base_X", "Base_Y", "Base_Z", "V", "H"],
                        "description": "Pattern axis: Base_X/Y/Z (document origin) or V/H (sketch axes)",
                        "default": "Base_Z",
                    },
                    "occurrences": {
                        "type": "integer",
                        "description": "Total number of copies including the original",
                        "default": 6,
                    },
                    "angle": {"type": "number", "description": "Total angle span in degrees", "default": 360.0},
                    "reversed": {"type": "boolean", "description": "Reverse pattern direction", "default": False},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["features"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.pocket",
            "description": "Cut a pocket from a sketch. Creates a PartDesign::Pocket feature.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sketch": {"type": "string", "description": "Sketch name for pocket profile"},
                    "length": {"type": "number", "description": "Pocket depth in mm"},
                    "pocket_type": {
                        "type": "string",
                        "enum": ["Dimension", "ThroughAll", "ToFirst", "ToLast"],
                        "default": "Dimension",
                    },
                    "reversed": {
                        "type": "boolean",
                        "default": False,
                        "description": "Reverse pocket direction",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["sketch"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.hole",
            "description": "Add a hole on a face. Creates a sketch point on the face and a PartDesign::Hole feature.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "face": {"type": "string", "description": "Face reference (e.g., 'Face6')"},
                    "diameter": {"type": "number", "description": "Hole diameter in mm"},
                    "depth": {"type": "number", "description": "Hole depth in mm"},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "hole_type": {"type": "string", "enum": ["Dimension", "ThroughAll"], "default": "Dimension"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["face", "diameter", "depth"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.fillet",
            "description": (
                "Fillet (round) edges. Creates a PartDesign::Fillet feature. "
                "Provide either 'edges' list or 'selection' name (from cad.define_selection)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "edges": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Edge references to fillet (e.g., ['Edge1', 'Edge3'])",
                    },
                    "radius": {"type": "number", "description": "Fillet radius in mm"},
                    "selection": {"type": "string", "description": "Named selection set to use instead of edges"},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["radius"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.chamfer",
            "description": (
                "Chamfer edges. Creates a PartDesign::Chamfer feature. "
                "Provide either 'edges' list or 'selection' name (from cad.define_selection)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "edges": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Edge references to chamfer (e.g., ['Edge1', 'Edge3'])",
                    },
                    "size": {"type": "number", "description": "Chamfer size in mm"},
                    "selection": {"type": "string", "description": "Named selection set to use instead of edges"},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["size"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.get_selection",
            "description": (
                "Get what the user has selected (clicked on) in FreeCAD. "
                "Returns object name, sub-element type (Face/Edge/Vertex), "
                "and geometric data (normals, positions, etc.)."
            ),
            "inputSchema": {"type": "object", "additionalProperties": False},
        },
        {
            "name": "cad.get_model_tree",
            "description": "Get the feature tree of the current FreeCAD document with object types and bounding boxes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.get_dimensions",
            "description": (
                "Get bounding box, volume, surface area, and topology counts (faces/edges/vertices) "
                "of an object. Use to verify operations changed the model as expected."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Name of the object to measure"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["object_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.get_body_topology",
            "description": (
                "Get ALL faces and edges on the body with geometric properties (normals, centers, "
                "lengths, positions). Use BEFORE fillet/chamfer to identify correct edge references "
                "by position and length — never guess edge names."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Body name (optional, uses first body if omitted)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.find_edges",
            "description": (
                "Find edges matching geometric criteria. Returns edge names for use with "
                "cad.fillet/cad.chamfer. Use axis='Z' for vertical edges, convexity='convex' "
                "for outer corners, on_face='Face3' for edges of a specific face."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Body name (optional, uses first body if omitted)"},
                    "axis": {
                        "type": "string",
                        "enum": ["X", "Y", "Z"],
                        "description": "Filter to straight edges parallel to this axis",
                    },
                    "curve_type": {
                        "type": "string",
                        "enum": ["Line", "Circle", "BSplineCurve", "Ellipse"],
                        "description": "Filter by curve type",
                    },
                    "min_length": {"type": "number", "description": "Minimum edge length in mm"},
                    "max_length": {"type": "number", "description": "Maximum edge length in mm"},
                    "on_face": {"type": "string", "description": "Only edges bounding this face (e.g. 'Face3')"},
                    "near_point": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Filter edges near this [x, y, z] point",
                    },
                    "near_distance": {"type": "number", "description": "Max distance from near_point in mm (default 1.0)"},
                    "convexity": {
                        "type": "string",
                        "enum": ["convex", "concave"],
                        "description": "Filter by edge convexity: 'convex' for outer corners, 'concave' for inner corners",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.define_selection",
            "description": (
                "Define a named edge selection query with optional invariants. The query is "
                "re-resolved against current geometry on every use, so names survive topology "
                "changes. Use before fillet/chamfer to create reusable edge references."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for this selection set"},
                    "query": {
                        "type": "object",
                        "description": (
                            "Edge query filters (same as cad.find_edges): axis, curve_type, "
                            "min_length, max_length, on_face, near_point, near_distance, convexity"
                        ),
                    },
                    "invariants": {
                        "type": "object",
                        "description": (
                            "Optional invariants to check: expected_count (int), "
                            "min_length (float), max_length (float)"
                        ),
                    },
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["name", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.resolve_selection",
            "description": (
                "Re-resolve a named selection against current geometry. Use before "
                "fillet/chamfer to get current edge names after topology changes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the selection set to resolve"},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.list_selections",
            "description": "List all defined selection sets.",
            "inputSchema": {"type": "object", "additionalProperties": False},
        },
        {
            "name": "cad.delete_selection",
            "description": "Remove a named selection set.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the selection set to delete"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.undo",
            "description": "Undo the last operation in FreeCAD.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.export",
            "description": "Export the FreeCAD document to STEP, STL, or FCStd format.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["step", "stl", "fcstd"], "default": "step"},
                    "path": {"type": "string", "description": "Output file path (optional, auto-generated if omitted)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
    ]


def _mfg_tool_list() -> list[dict[str, Any]]:
    """Manufacturing readiness tools."""
    return [
        {
            "name": "mfg.set_property",
            "description": (
                "Set manufacturing properties on the model (material, tolerance, finish, etc.). "
                "Properties: process, material_family, material_grade, quantity, tolerance_general, "
                "surface_finish_ra, coating, color, layer_height_mm, infill_percent, wall_count, "
                "critical_features, notes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "properties": {
                        "type": "object",
                        "description": "Dict of property names to values",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["properties"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mfg.readiness_check",
            "description": (
                "Run manufacturing readiness checks. Returns a checklist of missing info, "
                "geometric warnings, and process-specific issues with severity levels."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "process": {
                        "type": "string",
                        "enum": ["cnc", "fdm", "sla", "sls", "print_3d"],
                        "default": "cnc",
                    },
                    "properties": {
                        "type": "object",
                        "description": "Manufacturing properties to check against",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "mfg.export_rfq",
            "description": "Generate an RFQ (Request for Quote) summary from the model and manufacturing properties.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "properties": {
                        "type": "object",
                        "description": "Manufacturing properties for the RFQ",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
    ]


def _spec_tool_list() -> list[dict[str, Any]]:
    """Legacy spec interview tools (kept for backward compatibility)."""
    return [
        {
            "name": "spec.select_schema",
            "description": "Select schema/question bank and coverage threshold for a process+maturity.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "process": {"type": "string"},
                    "maturity_level": {"type": "string"},
                    "spec_version": {"type": "string"},
                },
                "required": ["process", "maturity_level", "spec_version"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.apply_answer",
            "description": "Atomically mutate spec_draft via JSON Pointer + op (set|append|remove).",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.validate",
            "description": "Validate shape + compute coverage + run deterministic rules.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.next_question",
            "description": "Deterministically select the next best question (skip-aware).",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.finalize",
            "description": "Freeze spec (strip internals), compute deterministic hash, changelog, provenance.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.export_brief",
            "description": "Export a CAD/design brief as Markdown from a finalized spec.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.export_rfq_summary",
            "description": "Export an RFQ-ready summary as Markdown from a finalized spec.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.generate_cad",
            "description": "Generate CAD geometry (STEP/STL/FCStd) for FreeCAD from a finalized spec.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {"type": "object"},
                    "output_format": {"type": "string", "enum": ["step", "stl", "freecad"]},
                    "output_path": {"type": "string"},
                    "options": {"type": "object"},
                },
                "required": ["spec", "output_format"],
                "additionalProperties": False,
            },
        },
    ]


def _tool_list() -> list[dict[str, Any]]:
    return _cad_tool_list() + _mfg_tool_list() + _spec_tool_list()


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

_CAD_DISPATCH: dict[str, Any] = {
    "cad.new_document": cad_new_document,
    "cad.new_body": cad_new_body,
    "cad.sketch": cad_sketch,
    "cad.pad": cad_pad,
    "cad.revolution": cad_revolution,
    "cad.polar_pattern": cad_polar_pattern,
    "cad.pocket": cad_pocket,
    "cad.hole": cad_hole,
    "cad.fillet": cad_fillet,
    "cad.chamfer": cad_chamfer,
    "cad.get_selection": cad_get_selection,
    "cad.get_model_tree": cad_get_model_tree,
    "cad.get_dimensions": cad_get_dimensions,
    "cad.get_body_topology": cad_get_body_topology,
    "cad.find_edges": cad_find_edges,
    "cad.define_selection": cad_define_selection,
    "cad.resolve_selection": cad_resolve_selection,
    "cad.list_selections": cad_list_selections,
    "cad.delete_selection": cad_delete_selection,
    "cad.undo": cad_undo,
    "cad.export": cad_export,
}

_MFG_DISPATCH: dict[str, Any] = {
    "mfg.set_property": mfg_set_property,
    "mfg.readiness_check": mfg_readiness_check,
    "mfg.export_rfq": mfg_export_rfq,
}

_SPEC_DISPATCH: dict[str, Any] = {
    "spec.select_schema": spec_select_schema,
    "spec.apply_answer": spec_apply_answer,
    "spec.validate": spec_validate,
    "spec.next_question": spec_next_question,
    "spec.finalize": spec_finalize,
    "spec.export_brief": spec_export_brief,
    "spec.export_rfq_summary": spec_export_rfq_summary,
    "spec.generate_cad": spec_generate_cad,
}


def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    handler = _CAD_DISPATCH.get(name) or _MFG_DISPATCH.get(name) or _SPEC_DISPATCH.get(name)
    if handler is None:
        raise KeyError(f"Unknown tool: {name}")
    return handler(**arguments)


def serve() -> int:
    log = logging.getLogger("mcp.serve")
    log.info("SolidMind CAD MCP server starting (stdio)")
    while True:
        msg = _read_message()
        if msg is None:
            log.info("No more messages, exiting")
            return 0

        rpc_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        # Notifications: ignore (no id).
        if rpc_id is None:
            continue

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "solidmind-cad", "version": "0.2.0"},
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                }
                _send(_rpc_result(rpc_id, result))
                continue

            if method == "tools/list":
                _send(_rpc_result(rpc_id, {"tools": _tool_list()}))
                continue

            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    _send(_rpc_result(rpc_id, {"isError": True, "content": [{"type": "text", "text": "Invalid params"}]}))
                    continue
                out = _call_tool(name, arguments)
                _send(_rpc_result(rpc_id, {"isError": False, "content": [{"type": "text", "text": json.dumps(out)}]}))
                continue

            if method == "resources/list":
                _send(_rpc_result(rpc_id, {"resources": list_resources()}))
                continue

            if method == "resources/read":
                uri = params.get("uri")
                if not isinstance(uri, str):
                    _send(_rpc_error(rpc_id, -32602, "Invalid params"))
                    continue
                content = read_resource(uri)
                _send(_rpc_result(rpc_id, {"contents": [content]}))
                continue

            if method == "prompts/list":
                _send(_rpc_result(rpc_id, {"prompts": list_prompts()}))
                continue

            if method == "prompts/get":
                name = params.get("name")
                if not isinstance(name, str):
                    _send(_rpc_error(rpc_id, -32602, "Invalid params"))
                    continue
                _send(_rpc_result(rpc_id, {"prompt": get_prompt(name)}))
                continue

            _send(_rpc_error(rpc_id, -32601, f"Method not found: {method}"))
        except Exception as e:
            _send(_rpc_error(rpc_id, -32603, f"Internal error: {e}"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SolidMind CAD MCP server over stdio.")
    parser.add_argument("--serve", action="store_true", help="Run the stdio server (default).")
    args = parser.parse_args(argv)

    raise SystemExit(serve())


if __name__ == "__main__":
    main()
