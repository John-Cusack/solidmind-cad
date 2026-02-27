from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Stderr handler — always present, keeps MCP host (Claude Code) informed
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    stream=sys.stderr,
)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / ".solidmind" / "logs"

# Current document-specific file handler (swapped on cad.new_document)
_doc_handler: logging.FileHandler | None = None


def switch_document_log(doc_name: str) -> Path:
    """Switch the file log handler to .solidmind/logs/<doc_name>/server.log.

    Called from cad_new_document when a new part is created.
    Returns the new log file path.
    """
    global _doc_handler  # noqa: PLW0603

    log_dir = _LOG_DIR / doc_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "server.log"

    root_logger = logging.getLogger()

    # Remove previous document handler if any
    if _doc_handler is not None:
        root_logger.removeHandler(_doc_handler)
        _doc_handler.close()

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    ))
    root_logger.addHandler(handler)
    _doc_handler = handler

    logging.getLogger("mcp.serve").info("Document log: %s", log_file)
    return log_file

from server.jsonutil import dumps as json_dumps
from server.jsonutil import loads as json_loads
from server.prompts import get_prompt, list_prompts
from server.tools import (
    spec_apply_answer,
    spec_assess_design_path,
    spec_export_brief,
    spec_export_rfq_summary,
    spec_finalize,
    spec_generate_cad,
    spec_next_question,
    spec_plan_geometry,
    spec_select_schema,
    spec_validate,
)
from server.tools_cad import (
    cad_animate,
    cad_animate_stop,
    cad_assembly_audit,
    cad_chamfer,
    cad_create_primitive,
    cad_create_primitives,
    cad_define_selection,
    cad_delete_objects,
    cad_delete_selection,
    cad_draft,
    cad_export,
    cad_export_body,
    cad_export_sim_package,
    cad_fillet,
    cad_find_edges,
    cad_register_placement_plan,
    cad_clear_placement_plan,
    cad_freecad_info,
    cad_get_body_topology,
    cad_get_camera,
    cad_get_dimensions,
    cad_get_model_tree,
    cad_get_selection,
    cad_helix,
    cad_hole,
    cad_linear_pattern,
    cad_list_selections,
    cad_loft,
    cad_check_clearance,
    cad_check_swept_clearance,
    cad_measure_between,
    cad_mirror,
    cad_new_body,
    cad_new_document,
    cad_pad,
    cad_pocket,
    cad_polar_pattern,
    cad_resolve_selection,
    cad_revolution,
    cad_screenshot,
    cad_set_camera,
    cad_set_placement,
    cad_set_visibility,
    cad_sketch,
    cad_sweep,
    cad_thickness,
    cad_undo,
)
from server.tools_mfg import (
    mfg_export_rfq,
    mfg_readiness_check,
    mfg_set_property,
)
from server.tools_knowledge import (
    knowledge_extract,
    knowledge_ingest,
    knowledge_ingest_status,
    knowledge_search,
    knowledge_status,
)
from server.tools_geometry import (
    geometry_gear_params,
    geometry_involute_points,
    geometry_planetary_layout,
    geometry_propeller_blade,
    geometry_spur_gear,
    geometry_tooth_slot,
)
from server.tools_me import (
    me_apply_risk_gates,
    me_build_traceability,
    me_design_loop,
    me_list_validators,
    me_validate_constraints,
)
from server.tools_study import (
    study_cancel,
    study_create,
    study_get_variant,
    study_list,
    study_results,
    study_run,
    study_status,
)
from server.tools_motion import (
    motion_check_gear_train,
    motion_check_interference,
    motion_check_joint_connectivity,
    motion_create_assembly,
    motion_define_mechanism,
    motion_drive_joint,
    motion_isaac_screenshot,
    motion_list_mechanisms,
    motion_propagate_motion,
    motion_simulate,
    motion_teleop_command,
    motion_teleop_start,
    motion_teleop_state,
    motion_teleop_stop,
    motion_validate,
    motion_verify_sim_package,
)
from server.tools_rl import (
    rl_configure_environment,
    rl_deploy_policy,
    rl_evaluate_policy,
    rl_monitor_training,
    rl_start_training,
    rl_stop_training,
)
from server.tools_design import (
    design_add_interface,
    design_add_part,
    design_generate_mechanism,
    design_get_brief,
    design_get_part,
    design_list_briefs,
    design_save_brief,
    design_update_brief,
    design_update_part,
    design_verify_build,
)
from server.tools_fastener import cad_fastener_spec
from server.tools_fastener_build import cad_bolt, cad_find_holes, cad_nut


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

_VERIFY_PROP: dict[str, Any] = {
    "type": "boolean",
    "default": True,
    "description": "Capture verification screenshots from 2 angles after the operation. Set false to skip for speed.",
}


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
                "Elements: rect, circle, line, arc, spline, external_ref, sketch_fillet, sketch_chamfer. "
                "ALL element types are fully supported including splines (B-spline curves from control points "
                "with degree/weights/periodic options). Use splines for smooth contours, airfoils, blade "
                "profiles, and organic shapes — never approximate with line segments. "
                "Any element can have \"construction\": true to make it a reference line/circle. "
                "external_ref projects edges from existing features (needs 'feature' and 'edge' fields). "
                "sketch_fillet/sketch_chamfer round or chamfer sketch vertices (needs 'vertex' and 'radius'/'size'). "
                "Constraints: Coincident, Horizontal, Vertical, Distance, Radius, Angle, etc. "
                "Constraints use partial recovery — a single failed constraint won't abort the sketch."
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
                    "geometry_ref": {
                        "type": "string",
                        "description": (
                            "Handle returned by a geometry.* tool (e.g. geometry.spur_gear). "
                            "Elements are resolved server-side — the LLM never sees the raw data. "
                            "Can be combined with 'elements' (ref elements added first, then inline)."
                        ),
                    },
                    "elements": {
                        "type": "array",
                        "description": "Geometry elements to add to the sketch",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["rect", "circle", "line", "arc", "spline", "external_ref", "sketch_fillet", "sketch_chamfer"]},
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
                    "verify": _VERIFY_PROP,
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
                "Creates a PartDesign::Revolution feature, or a PartDesign::Groove (subtractive cut) when subtractive=true."
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
                    "subtractive": {"type": "boolean", "description": "If true, create a subtractive revolution (Groove) that cuts material", "default": False},
                    "verify": _VERIFY_PROP,
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
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["features"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.mirror",
            "description": (
                "Mirror features across a symmetry plane. "
                "Creates a PartDesign::Mirrored feature."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "features": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Feature names to mirror (e.g., ['Pad'])",
                    },
                    "plane": {
                        "type": "string",
                        "enum": ["Base_X", "Base_Y", "Base_Z", "V", "H"],
                        "description": "Mirror plane: Base_X/Y/Z (document origin planes) or V/H (sketch axes)",
                        "default": "Base_X",
                    },
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["features"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.linear_pattern",
            "description": (
                "Create a linear pattern of features along an axis. "
                "Creates a PartDesign::LinearPattern feature."
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
                        "description": "Pattern direction: Base_X/Y/Z (document origin axes) or V/H (sketch axes)",
                        "default": "Base_X",
                    },
                    "length": {
                        "type": "number",
                        "description": "Total span of the pattern in mm",
                        "default": 100.0,
                    },
                    "occurrences": {
                        "type": "integer",
                        "description": "Total number of copies including the original",
                        "default": 3,
                    },
                    "reversed": {"type": "boolean", "description": "Reverse pattern direction", "default": False},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["features"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.thickness",
            "description": (
                "Shell/hollow out a solid by removing faces and adding wall thickness. "
                "Creates a PartDesign::Thickness feature. Use to create hollow enclosures, "
                "boxes, containers, and cups."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "faces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Face references to remove/open (e.g., ['Face6'])",
                    },
                    "thickness": {
                        "type": "number",
                        "description": "Wall thickness in mm",
                    },
                    "join_type": {
                        "type": "string",
                        "enum": ["Arc", "Tangent", "Intersection"],
                        "description": "Corner join type",
                        "default": "Arc",
                    },
                    "reversed": {"type": "boolean", "description": "Reverse thickness direction (inward vs outward)", "default": False},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["faces", "thickness"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.draft",
            "description": (
                "Add draft/taper to faces for injection molding or casting. "
                "Creates a PartDesign::Draft feature."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "faces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Face references to draft (e.g., ['Face2', 'Face4'])",
                    },
                    "angle": {
                        "type": "number",
                        "description": "Draft angle in degrees",
                    },
                    "neutral_plane": {
                        "type": "string",
                        "description": "Face reference for the neutral/stationary plane (e.g., 'Face1')",
                        "default": "Face1",
                    },
                    "reversed": {"type": "boolean", "description": "Reverse pull direction", "default": False},
                    "body": {"type": "string", "description": "Body name (optional)"},
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["faces", "angle"],
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
                        "default": "auto",
                        "description": (
                            "Pocket direction. 'auto' (default) resolves deterministically "
                            "from sketch plane and body geometry — no guessing needed. "
                            "Set true/false to override."
                        ),
                    },
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["sketch"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.sweep",
            "description": (
                "Sweep a profile sketch along a spine sketch to create a solid. "
                "Creates a PartDesign::AdditivePipe (or SubtractivePipe) feature."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "profile_sketch": {"type": "string", "description": "Sketch name for the sweep profile"},
                    "spine_sketch": {"type": "string", "description": "Sketch name for the sweep path/spine"},
                    "subtractive": {
                        "type": "boolean",
                        "description": "If true, create a subtractive (cut) sweep",
                        "default": False,
                    },
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["profile_sketch", "spine_sketch"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.helix",
            "description": (
                "Create a helical sweep of a sketch profile around an axis. "
                "Creates a PartDesign::AdditiveHelix feature. "
                "Use for threads, springs, spiral features, and helical cuts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sketch": {"type": "string", "description": "Sketch name for the helix profile"},
                    "pitch": {"type": "number", "description": "Distance between turns in mm (used in pitch-height and pitch-turns modes)"},
                    "height": {"type": "number", "description": "Total helix height in mm (used in pitch-height and height-turns modes)"},
                    "turns": {"type": "number", "description": "Number of turns (used in pitch-turns and height-turns modes)"},
                    "axis": {
                        "type": "string",
                        "enum": ["V", "H", "Base_X", "Base_Y", "Base_Z"],
                        "description": "Helix axis: V (sketch vertical), H (sketch horizontal), or Base_X/Y/Z (document origin axes)",
                        "default": "V",
                    },
                    "angle": {"type": "number", "description": "Taper angle in degrees (0 = straight helix)", "default": 0.0},
                    "growth": {"type": "number", "description": "Radial growth per revolution in mm (0 = constant radius)", "default": 0.0},
                    "left_handed": {"type": "boolean", "description": "Create a left-handed helix", "default": False},
                    "reversed": {"type": "boolean", "description": "Reverse helix direction", "default": False},
                    "mode": {
                        "type": "string",
                        "enum": ["pitch-height", "pitch-turns", "height-turns"],
                        "description": "How to define the helix: pitch-height (default), pitch-turns, or height-turns",
                        "default": "pitch-height",
                    },
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["sketch"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.loft",
            "description": (
                "Loft between two or more sketch profiles to create a solid. "
                "Creates a PartDesign::AdditiveLoft (or SubtractiveLoft) feature."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sketches": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Sketch names to loft between (at least 2)",
                        "minItems": 2,
                    },
                    "ruled": {
                        "type": "boolean",
                        "description": "Use ruled surfaces between sections",
                        "default": False,
                    },
                    "closed": {
                        "type": "boolean",
                        "description": "Close the loft (connect last section back to first)",
                        "default": False,
                    },
                    "subtractive": {
                        "type": "boolean",
                        "description": "If true, create a subtractive (cut) loft",
                        "default": False,
                    },
                    "verify": _VERIFY_PROP,
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["sketches"],
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
                    "verify": _VERIFY_PROP,
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
                    "verify": _VERIFY_PROP,
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
                    "verify": _VERIFY_PROP,
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
            "description": "Get the feature tree of the current FreeCAD document. Bodies include position, rotation, world bounding box, and sizes — use as a one-call spatial overview.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "Document name (optional)"},
                    "detail": {"type": "string", "enum": ["bodies", "full"], "default": "bodies", "description": "Detail level. 'bodies' (default): compact body-level overview with sizes and feature counts. 'full': flat list of every object with bounding boxes and topology."},
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
                    "body": {"type": "string", "description": "Alias for object_name (either works)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
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
            "name": "cad.screenshot",
            "description": (
                "Take a screenshot of the model with smart camera targeting. "
                "Supports presets (iso, front, top, right, back, bottom, left), "
                "face references (Face3), feature names (Pocket001), or explicit [x,y,z] points."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {
                        "description": (
                            "What to look at. Preset name (iso, front, top, right, back, bottom, left), "
                            "face ref (Face3), feature name (Pocket001), or [x,y,z] point."
                        ),
                        "default": "iso",
                    },
                    "distance": {
                        "type": "number",
                        "description": "Distance multiplier on bounding-box diagonal. 1.0=tight, 3.0=far",
                        "default": 2.0,
                    },
                    "direction": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Override look-from direction [x,y,z]. If null, computed from target.",
                    },
                    "up": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Camera up vector [x,y,z]",
                        "default": [0, 0, 1],
                    },
                    "near_clip": {
                        "type": "number",
                        "description": "Near clipping plane in mm. Set to slice into model for cavity views.",
                    },
                    "width": {"type": "integer", "description": "Image width in pixels", "default": 512},
                    "height": {"type": "integer", "description": "Image height in pixels", "default": 512},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                    "hide_bodies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Body names to temporarily hide during capture (e.g. ['Body_Ring'] to see occluded gears).",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.set_camera",
            "description": "Set camera position and orientation for precise control (cavity inspection, clipping planes).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Camera position [x,y,z] in mm",
                    },
                    "target": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Look-at point [x,y,z] in mm",
                    },
                    "up": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Up vector [x,y,z]",
                        "default": [0, 0, 1],
                    },
                    "near_clip": {
                        "type": "number",
                        "description": "Near clipping plane in mm",
                    },
                    "fit_all": {
                        "type": "boolean",
                        "description": "Fit all objects in view first",
                        "default": False,
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.get_camera",
            "description": "Get current camera position, orientation, and clipping planes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
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
        {
            "name": "cad.export_body",
            "description": "Export a single PartDesign body to STL, STEP, or OBJ.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Body name to export"},
                    "format": {"type": "string", "enum": ["stl", "step", "obj"], "default": "stl"},
                    "path": {"type": "string", "description": "Output file path (optional, auto-generated if omitted)"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                    "strip_placement": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Zero the Body Placement before export so mesh vertices are in "
                            "body-local coordinates. Use for sim-package-style exports. "
                            "Default false preserves world coordinates."
                        ),
                    },
                },
                "required": ["body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.export_sim_package",
            "description": (
                "Export all (or specified) bodies as individual meshes + optionally generate URDF from mechanism. "
                "One MCP call that exports each body as a separate mesh file with its placement, "
                "and generates a URDF file if a mechanism_id is provided. Ready for Isaac Sim import."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "bodies": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Body names to export (default: all PartDesign::Body objects)",
                    },
                    "format": {"type": "string", "enum": ["stl", "step"], "default": "stl"},
                    "output_dir": {"type": "string", "description": "Output directory (auto tempdir if omitted)"},
                    "mechanism_id": {"type": "string", "description": "Mechanism handle from motion.define_mechanism — triggers URDF generation"},
                    "emit_sdf": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When mechanism_id is provided, also emit an SDF artifact beside URDF. "
                            "Recommended for Gazebo-native drone simulation."
                        ),
                    },
                    "ground_clearance_m": {
                        "type": "number",
                        "description": (
                            "Height in meters to raise the robot above the ground plane. "
                            "Adds a base_link with a fixed joint at this Z offset. "
                            "Use for ground-standing robots (hexapods, wheeled bots) "
                            "where mesh geometry extends below the kinematic origin."
                        ),
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.delete_objects",
            "description": "Delete objects from the FreeCAD document by name. Use to clean up stale assemblies, unused bodies, or failed features.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}, "description": "Object names to delete"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["names"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.set_placement",
            "description": (
                "Set the Placement of any FreeCAD object (body, link, etc.). "
                "When a placement plan is registered (via "
                "cad.register_placement_plan), the response includes a "
                "'plan_check' dict with position/rotation/size validation "
                "against the plan — no separate call needed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Name of the FreeCAD object"},
                    "position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Translation [x, y, z] in mm (optional, keeps current if omitted)",
                    },
                    "rotation_axis": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Rotation axis [ax, ay, az] (default: [0,0,1])",
                    },
                    "rotation_angle_deg": {
                        "type": "number",
                        "default": 0.0,
                        "description": "Rotation angle in degrees",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["object_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.set_visibility",
            "description": "Show or hide objects in the FreeCAD viewport by name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "objects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Object names to show/hide",
                    },
                    "visible": {
                        "type": "boolean",
                        "default": True,
                        "description": "True to show, False to hide",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["objects"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.animate",
            "description": (
                "Play a looping animation of placement frames in FreeCAD viewport. "
                "Pass frames from motion.drive_joint step_positions or build custom frames. "
                "Animation loops for duration_s seconds then auto-stops."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "frames": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "List of placement dicts, one per frame. Each is "
                            "{link_name: {position, rotation_axis, rotation_angle_deg}} "
                            "or {link_name: {angle_deg, axis, center}}."
                        ),
                    },
                    "duration_s": {
                        "type": "number",
                        "default": 10.0,
                        "description": "Total animation duration in seconds",
                    },
                    "fps": {
                        "type": "integer",
                        "default": 30,
                        "description": "Target frames per second",
                    },
                    "assembly": {
                        "type": "string",
                        "description": "Assembly name (optional, auto-detected if one exists)",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["frames"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.animate_stop",
            "description": "Stop any running animation in FreeCAD viewport.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.create_primitive",
            "description": (
                "Create a simple solid body (box or cylinder) with position and orientation in one call. "
                "Combines body creation, sketch, pad, and placement. Use for fasteners, motor housings, "
                "standoffs, and any case where you need a positioned primitive without complex features."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Body name (e.g. 'Servo_hip_yaw_L1')"},
                    "shape": {"type": "string", "enum": ["box", "cylinder"], "description": "Primitive shape type"},
                    "dimensions": {
                        "type": "object",
                        "description": "Shape dimensions in mm. Box: {length, width, height}. Cylinder: {radius, height}.",
                        "properties": {
                            "length": {"type": "number", "description": "Box length along local X (mm)"},
                            "width": {"type": "number", "description": "Box width along local Y (mm)"},
                            "height": {"type": "number", "description": "Extrusion height along local Z (mm)"},
                            "radius": {"type": "number", "description": "Cylinder radius (mm)"},
                        },
                    },
                    "position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "World position [x, y, z] in mm. The primitive is centered at this point.",
                    },
                    "rotation_axis": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Rotation axis [ax, ay, az] (default: [0, 0, 1])",
                    },
                    "rotation_angle_deg": {
                        "type": "number",
                        "description": "Rotation angle in degrees (default: 0)",
                        "default": 0,
                    },
                    "verify": {
                        "type": "boolean",
                        "description": "Capture verification screenshots (default: false for primitives)",
                        "default": False,
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["name", "shape", "dimensions"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.create_primitives",
            "description": (
                "Create multiple simple solid bodies in one call. Each item has the same schema as "
                "cad.create_primitive. Use for layout visualization and static assemblies (fasteners, "
                "standoffs, enclosure parts). For articulated mechanisms (hexapods, robot arms), do NOT "
                "use this for final builds — each kinematic segment should be ONE composite body built "
                "with sketch + pad + pocket instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "List of primitive specs",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "shape": {"type": "string", "enum": ["box", "cylinder"]},
                                "dimensions": {"type": "object"},
                                "position": {"type": "array", "items": {"type": "number"}},
                                "rotation_axis": {"type": "array", "items": {"type": "number"}},
                                "rotation_angle_deg": {"type": "number", "default": 0},
                            },
                            "required": ["name", "shape", "dimensions"],
                        },
                    },
                    "verify": {
                        "type": "boolean",
                        "description": "Capture one verification screenshot after all primitives are created (default: true)",
                        "default": True,
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.freecad_info",
            "description": (
                "Get FreeCAD runtime environment information: version, available workbenches, "
                "module availability (Sketcher, Part, PartDesign, JointObject, UtilsAssembly), "
                "and Qt backend. Use for diagnostics when operations fail."
            ),
            "inputSchema": {
                "type": "object",
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
    """Deterministic specification interview + finalization tools."""
    return [
        {
            "name": "spec.select_schema",
            "description": "Select schema/question-bank/coverage threshold by process + maturity level.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "process": {"type": "string", "enum": ["cnc", "print_3d"]},
                    "maturity_level": {"type": "string", "enum": ["L1", "L2", "L3"]},
                    "spec_version": {"type": "string"},
                },
                "required": ["process", "maturity_level", "spec_version"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.apply_answer",
            "description": "Apply a deterministic mutation to spec_draft using JSON-pointer addressing.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec_draft": {"type": "object"},
                    "op": {"type": "string", "enum": ["set", "append", "remove"]},
                    "path": {"type": "string"},
                    "value": {},
                    "question_id": {"type": "string"},
                    "source": {"type": "string", "enum": ["user", "llm_proposal", "default", "import", "user_skip"]},
                },
                "required": ["spec_draft", "op", "path", "source"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.validate",
            "description": "Validate draft shape + coverage + deterministic blocker/warning rules.",
            "inputSchema": {
                "type": "object",
                "properties": {"spec_draft": {"type": "object"}},
                "required": ["spec_draft"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.next_question",
            "description": "Pick the next best interview question from blockers, required fields, and weighted gaps.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec_draft": {"type": "object"},
                    "conversation_signals": {
                        "type": "object",
                        "properties": {
                            "user_expertise": {"type": "string", "enum": ["novice", "intermediate", "expert", "unknown"]},
                            "language_preference": {"type": "string", "enum": ["plain", "technical", "auto"]},
                            "previous_question_id": {"type": "string"},
                            "allow_revisit_skipped": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["spec_draft"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.finalize",
            "description": "Finalize spec_draft into canonical spec plus deterministic hash/provenance sidecar.",
            "inputSchema": {
                "type": "object",
                "properties": {"spec_draft": {"type": "object"}},
                "required": ["spec_draft"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.export_brief",
            "description": "Export human-readable design brief markdown from finalized spec.",
            "inputSchema": {
                "type": "object",
                "properties": {"spec": {"type": "object"}},
                "required": ["spec"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.export_rfq_summary",
            "description": "Export process-specific RFQ summary markdown from finalized spec.",
            "inputSchema": {
                "type": "object",
                "properties": {"spec": {"type": "object"}},
                "required": ["spec"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.assess_design_path",
            "description": "Classify a draft as basic_box vs spec_driven for coverage-gating behavior.",
            "inputSchema": {
                "type": "object",
                "properties": {"spec_draft": {"type": "object"}},
                "required": ["spec_draft"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.generate_cad",
            "description": "Generate CAD geometry from finalized spec with deterministic precondition checks.",
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
        {
            "name": "spec.plan_geometry",
            "description": (
                "Read-only observability tool: plan geometry from a finalized spec, "
                "returning GIR (Geometry Intent Representation) and EIR (Execution Intent "
                "Representation) with notices and hashes, without executing any CAD operations."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {"type": "object"},
                    "options": {
                        "type": "object",
                        "properties": {
                            "planning_mode": {"type": "string", "enum": ["legacy", "policy_v1"]},
                            "strict_mode": {"type": "boolean"},
                            "question_budget_override": {"type": "integer", "minimum": 0},
                        },
                    },
                },
                "required": ["spec"],
                "additionalProperties": False,
            },
        },
    ]


def _me_tool_list() -> list[dict[str, Any]]:
    """ME-grade constraint validation and risk gate tools."""
    return [
        {
            "name": "me.validate_constraints",
            "description": "Run deterministic Tier 0/1 proxy validators over a constraint dict.",
            "inputSchema": {
                "type": "object",
                "properties": {"constraint_sheet": {"type": "object"}},
                "required": ["constraint_sheet"],
                "additionalProperties": False,
            },
        },
        {
            "name": "me.build_traceability",
            "description": "Build requirement-to-evidence traceability matrix from constraints + validation report.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "constraint_sheet": {"type": "object"},
                    "validation_report": {"type": "object"},
                },
                "required": ["constraint_sheet", "validation_report"],
                "additionalProperties": False,
            },
        },
        {
            "name": "me.apply_risk_gates",
            "description": "Assign risk class and signoff gates from constraints + validation findings.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "constraint_sheet": {"type": "object"},
                    "validation_report": {"type": "object"},
                },
                "required": ["constraint_sheet", "validation_report"],
                "additionalProperties": False,
            },
        },
        {
            "name": "me.design_loop",
            "description": (
                "Optional ME preflight for complex/high-risk parts. "
                "Takes a constraint dict constructed by the LLM, runs validate -> trace -> risk gates. "
                "Call when needed, then continue geometry with cad.*."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "constraints": {"type": "object", "description": "Constraint dict with geometry_interfaces, operating_envelope, material, manufacturing, etc."},
                },
                "required": ["constraints"],
                "additionalProperties": False,
            },
        },
        {
            "name": "me.list_validators",
            "description": (
                "List available validators with metadata: what fields they read, "
                "what thresholds they accept, and their priority. Use this to discover "
                "what to put in a constraint dict before calling me.validate_constraints."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
            },
        },
    ]


def _knowledge_tool_list() -> list[dict[str, Any]]:
    """Knowledge management tools — semantic search, extraction, ingestion."""
    return [
        {
            "name": "knowledge.extract",
            "description": (
                "Send a file to Docling for processing, return extracted text/markdown "
                "content directly to the LLM. No indexing — just parsing. "
                "LLM can then decide what to do with the content."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to extract (PDF, DOCX, etc.)"},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge.ingest",
            "description": (
                "Submit a file OR directory for ingestion into the knowledge base. "
                "Ingestion is synchronous and in-process (no polling needed). "
                "Directories are walked recursively for PDF/DOCX/MD files."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path to ingest"},
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File extensions to include for directory ingestion (default: .pdf, .docx, .md)",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge.ingest_status",
            "description": (
                "Poll ingestion status for one or more task IDs. "
                "Ingestion is now synchronous, so this always returns 'complete'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Single task ID to check"},
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple task IDs to check",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge.search",
            "description": (
                "Semantic search across all ingested docs. Returns ranked results "
                "with source, score, and relevant chunks. Falls back to listing "
                "local me_knowledge/notes/ files when the knowledge store is unavailable."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Number of results to return", "default": 5},
                    "filters": {"type": "object", "description": "Optional filters"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge.status",
            "description": "Check knowledge store health, document count, index info.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
            },
        },
    ]


def _geometry_tool_list() -> list[dict[str, Any]]:
    """Parametric geometry generators — involute gears, planetary layouts."""
    return [
        {
            "name": "geometry.spur_gear",
            "description": (
                "Generate a spur gear profile (external or internal). "
                "Returns a geometry_ref handle + element_count + params. "
                "Pass geometry_ref to cad.sketch(geometry_ref=...) to use the profile."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "module": {"type": "number", "description": "Gear module (tooth size) in mm"},
                    "teeth": {"type": "integer", "description": "Number of teeth"},
                    "pressure_angle_deg": {"type": "number", "default": 20.0, "description": "Pressure angle in degrees"},
                    "profile_shift": {"type": "number", "default": 0.0, "description": "Profile shift coefficient"},
                    "backlash": {"type": "number", "default": 0.0, "description": "Backlash in mm"},
                    "center_x": {"type": "number", "default": 0.0, "description": "Center X coordinate"},
                    "center_y": {"type": "number", "default": 0.0, "description": "Center Y coordinate"},
                    "internal": {"type": "boolean", "default": False, "description": "Generate internal (ring) gear profile"},
                    "num_involute_pts": {"type": "integer", "default": 20, "description": "Points per involute curve"},
                    "clearance_coeff": {"type": "number", "default": 0.25, "description": "Clearance coefficient"},
                },
                "required": ["module", "teeth"],
                "additionalProperties": False,
            },
        },
        {
            "name": "geometry.tooth_slot",
            "description": (
                "Generate a single tooth slot profile for pocket + polar_pattern workflow. "
                "Returns a geometry_ref handle. Create a blank cylinder, "
                "pocket this slot via cad.sketch(geometry_ref=...), then polar_pattern for all teeth."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "module": {"type": "number", "description": "Gear module in mm"},
                    "teeth": {"type": "integer", "description": "Number of teeth"},
                    "pressure_angle_deg": {"type": "number", "default": 20.0},
                    "profile_shift": {"type": "number", "default": 0.0},
                    "backlash": {"type": "number", "default": 0.0},
                    "center_x": {"type": "number", "default": 0.0},
                    "center_y": {"type": "number", "default": 0.0},
                    "num_involute_pts": {"type": "integer", "default": 20},
                    "clearance_coeff": {"type": "number", "default": 0.25},
                },
                "required": ["module", "teeth"],
                "additionalProperties": False,
            },
        },
        {
            "name": "geometry.gear_params",
            "description": "Compute gear parameters (diameters, etc.) without generating geometry.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "module": {"type": "number", "description": "Gear module in mm"},
                    "teeth": {"type": "integer", "description": "Number of teeth"},
                    "pressure_angle_deg": {"type": "number", "default": 20.0},
                    "profile_shift": {"type": "number", "default": 0.0},
                    "backlash": {"type": "number", "default": 0.0},
                    "internal": {"type": "boolean", "default": False},
                    "clearance_coeff": {"type": "number", "default": 0.25},
                },
                "required": ["module", "teeth"],
                "additionalProperties": False,
            },
        },
        {
            "name": "geometry.planetary_layout",
            "description": (
                "Generate a planetary gear layout with sun, planet, and ring profiles. "
                "Validates ring=sun+2*planet and assembly condition. "
                "Returns geometry_ref handles for sun, planet, and ring + planet positions. "
                "Pass each geometry_ref to cad.sketch(geometry_ref=...) to use."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "module": {"type": "number", "description": "Gear module in mm"},
                    "sun_teeth": {"type": "integer", "description": "Sun gear teeth"},
                    "planet_teeth": {"type": "integer", "description": "Planet gear teeth"},
                    "num_planets": {"type": "integer", "default": 3, "description": "Number of planets"},
                    "pressure_angle_deg": {"type": "number", "default": 20.0},
                    "profile_shift": {"type": "number", "default": 0.0},
                    "backlash": {"type": "number", "default": 0.0},
                    "center_x": {"type": "number", "default": 0.0},
                    "center_y": {"type": "number", "default": 0.0},
                    "num_involute_pts": {"type": "integer", "default": 20},
                    "clearance_coeff": {"type": "number", "default": 0.25},
                },
                "required": ["module", "sun_teeth", "planet_teeth"],
                "additionalProperties": False,
            },
        },
        {
            "name": "geometry.involute_points",
            "description": "Generate points along an involute curve for a given base circle.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base_radius": {"type": "number", "description": "Base circle radius in mm"},
                    "start_radius": {"type": "number", "description": "Start radius in mm"},
                    "end_radius": {"type": "number", "description": "End radius in mm"},
                    "num_points": {"type": "integer", "default": 20, "description": "Number of points"},
                },
                "required": ["base_radius", "start_radius", "end_radius"],
                "additionalProperties": False,
            },
        },
        {
            "name": "geometry.propeller_blade",
            "description": (
                "Generate a propeller blade definition with NACA 4-digit airfoil sections. "
                "Computes chord taper, twist from pitch, and airfoil profiles at radial stations. "
                "Returns geometry_ref handles for each section + hub, a blade_table for BEMT analysis, "
                "and a Selig-format airfoil_dat for XFOIL. "
                "Use sections with cad.sketch(geometry_ref=...) → cad.loft → cad.polar_pattern."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "diameter": {"type": "number", "description": "Total propeller diameter in mm"},
                    "pitch": {"type": "number", "description": "Propeller pitch in mm (distance per revolution)"},
                    "hub_diameter": {"type": "number", "description": "Hub/root diameter in mm"},
                    "num_blades": {"type": "integer", "default": 2, "description": "Number of blades"},
                    "airfoil": {"type": "string", "default": "NACA4412", "description": "NACA 4-digit airfoil code (e.g. 'NACA4412', 'NACA0012')"},
                    "chord_root": {"type": "number", "description": "Root chord in mm (auto-sized ~12% diameter if omitted)"},
                    "chord_tip": {"type": "number", "description": "Tip chord in mm (auto-sized ~40% of root if omitted)"},
                    "num_sections": {"type": "integer", "default": 6, "description": "Number of radial cross-sections for loft"},
                    "num_points": {"type": "integer", "default": 40, "description": "Points per airfoil surface"},
                },
                "required": ["diameter", "pitch", "hub_diameter"],
                "additionalProperties": False,
            },
        },
    ]


def _study_tool_list() -> list[dict[str, Any]]:
    """Parametric design optimization study tools."""
    return [
        {
            "name": "study.create",
            "description": (
                "Define a parametric study with design variables, solver, and optimization objective. "
                "Returns study_id and execution plan with time estimates. "
                "For OpenFOAM solver, provide geometry_script — a FreeCAD Python script that "
                "reads params JSON from sys.argv[1] and exports STL to sys.argv[2]."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Study name"},
                    "variables": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "var_type": {"type": "string", "enum": ["continuous", "discrete", "categorical"]},
                                "min_val": {"type": "number"},
                                "max_val": {"type": "number"},
                                "coarse_step": {"type": "number"},
                                "fine_step": {"type": "number"},
                                "categories": {"type": "array", "items": {"type": "string"}},
                                "pinned_values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["name", "var_type"],
                        },
                        "description": "Design variables to sweep",
                    },
                    "solver": {
                        "type": "object",
                        "properties": {
                            "solver_type": {"type": "string", "enum": ["mock", "bemt_xfoil", "openfoam", "chrono"]},
                            "params": {"type": "object"},
                            "timeout_s": {"type": "number"},
                        },
                        "required": ["solver_type"],
                        "description": "Solver configuration",
                    },
                    "objective": {
                        "type": "object",
                        "properties": {
                            "primary_metric": {"type": "string"},
                            "direction": {"type": "string", "enum": ["maximize", "minimize"]},
                            "constraint_bounds": {"type": "object"},
                            "weights": {"type": "object"},
                        },
                        "required": ["primary_metric"],
                        "description": "Optimization objective",
                    },
                    "fixed_params": {"type": "object", "description": "Fixed parameters passed to solver"},
                    "geometry_script": {
                        "type": "string",
                        "description": (
                            "FreeCAD Python script for geometry generation (required for OpenFOAM). "
                            "Script reads params JSON from sys.argv[1], exports STL to sys.argv[2]. "
                            "Runs in FreeCAD headless mode (FreeCADCmd)."
                        ),
                    },
                },
                "required": ["name", "variables", "solver", "objective"],
                "additionalProperties": False,
            },
        },
        {
            "name": "study.run",
            "description": "Spawn background runner subprocess for a study. Returns PID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID to run"},
                },
                "required": ["study_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "study.status",
            "description": "Poll study progress (coarse N/M, refined N/M, status).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID to check"},
                },
                "required": ["study_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "study.results",
            "description": "Get ranked study results (top_n, filter by phase, sort by metric).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID"},
                    "top_n": {"type": "integer", "default": 10, "description": "Number of top results"},
                    "phase": {"type": "string", "enum": ["coarse", "refined"], "description": "Filter by phase"},
                    "sort_by": {"type": "string", "description": "Metric to sort by (default: primary metric)"},
                },
                "required": ["study_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "study.cancel",
            "description": "Send SIGTERM to cancel a running study.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID to cancel"},
                },
                "required": ["study_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "study.list",
            "description": "List all studies with summary status.",
            "inputSchema": {"type": "object", "additionalProperties": False},
        },
        {
            "name": "study.get_variant",
            "description": "Get full params + metrics for one variant.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID"},
                    "variant_id": {"type": "string", "description": "Variant ID"},
                },
                "required": ["study_id", "variant_id"],
                "additionalProperties": False,
            },
        },
    ]


def _motion_tool_list() -> list[dict[str, Any]]:
    """Motion validation pipeline tools (Tier 1: analytical)."""
    return [
        {
            "name": "motion.define_mechanism",
            "description": (
                "Define a mechanism for motion validation. Takes parts (nodes), "
                "joints (edges), drives (input conditions), and expected_outputs. "
                "Returns a mechanism_id handle for subsequent validation calls."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism": {
                        "type": "object",
                        "description": (
                            "Mechanism definition with name, parts[], joints[], drives[], "
                            "expected_outputs{}. Parts need id and is_ground. Joints need "
                            "id, joint_type (revolute/gear_mesh/belt_chain/prismatic/cam/fixed/planar), "
                            "parent_part, child_part. Gear meshes need gear_ratio or teeth_parent+teeth_child. "
                            "Joints should include: axis (e.g. [0,0,1] for yaw, [1,0,0] for pitch), "
                            "min_angle_deg/max_angle_deg (joint limits in degrees, default ±60°), "
                            "damping (joint damping coefficient, default 0.1), "
                            "friction (joint friction, default 0.0), "
                            "effort_nm (max torque in Nm, default 1.5), "
                            "velocity_rad_s (max velocity in rad/s, default 6.28)."
                        ),
                    },
                },
                "required": ["mechanism"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.list_mechanisms",
            "description": "List all stored mechanism definitions with summary info.",
            "inputSchema": {"type": "object", "additionalProperties": False},
        },
        {
            "name": "motion.validate",
            "description": (
                "Run analytical validators on a mechanism: gear ratio consistency, "
                "speed propagation, torque balance, power conservation, DOF analysis, "
                "Grashof criterion, expected output checks. Returns blockers, warnings, notes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle from motion.define_mechanism"},
                    "validators": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of validator names to run. If omitted, runs all. "
                            "Available: gear_ratio_consistency, speed_propagation, torque_balance, "
                            "power_conservation, dof_analysis, center_distance_check, "
                            "planet_spacing_check, linkage_grashof, expected_output_check"
                        ),
                    },
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.propagate_motion",
            "description": (
                "Compute speeds (RPM), torques (Nm), and power (W) at every part "
                "via BFS propagation from driven joints. Returns per-part states and "
                "overall efficiency."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle"},
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.check_gear_train",
            "description": (
                "Analyze the gear train: overall ratio, per-stage ratios, "
                "approximate contact ratios. Works on gear_mesh and belt_chain joints."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle"},
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.create_assembly",
            "description": (
                "Create a FreeCAD Assembly from a mechanism definition (Tier 2). "
                "Links each part's body into an assembly, adds joint constraints, "
                "and solves. Requires FreeCAD with Assembly workbench."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle from motion.define_mechanism"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.drive_joint",
            "description": (
                "Drive a joint through a range of values for visual verification (Tier 2). "
                "Captures screenshots at each step. For revolute joints, value is total "
                "rotation in degrees. For prismatic, total translation in mm."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle"},
                    "joint_id": {"type": "string", "description": "Joint ID within the mechanism"},
                    "value": {"type": "number", "description": "Total range to drive (degrees for revolute, mm for prismatic)"},
                    "steps": {"type": "integer", "default": 10, "description": "Number of steps to divide the motion into"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["mechanism_id", "joint_id", "value"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.check_joint_connectivity",
            "description": (
                "Check that each joint origin touches both parent and child body geometry. "
                "Uses distToShape in FreeCAD to measure distance from each joint origin point "
                "to both parent and child body shapes. Run after building bodies but before "
                "URDF export to catch connectivity issues early."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle from motion.define_mechanism"},
                    "tolerance_mm": {"type": "number", "default": 2.0, "description": "Max distance in mm for a joint to be considered connected"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.check_interference",
            "description": (
                "Check for collision between parts in the mechanism's assembly (Tier 2). "
                "Uses BRepAlgoAPI_Common to detect overlapping volumes between all part pairs. "
                "Returns clear=true if no collisions, or a list of colliding part pairs with overlap volume."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.simulate",
            "description": (
                "Run Tier 3 dynamic simulation using selected backend. "
                "Backends: isaac (default, GPU physics, best for legged robots and articulated mechanisms), "
                "chrono (C++ multibody, batch only, best for gear trains and linkages), "
                "gazebo (CPU physics + ROS/PX4 ecosystem, best for drones and wheeled vehicles). "
                "Set mode=teleop (Isaac or Gazebo) to start a live drive session."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle from motion.define_mechanism"},
                    "duration_s": {
                        "type": "number",
                        "default": 1.0,
                        "exclusiveMinimum": 0,
                        "description": "Simulation duration in seconds (> 0).",
                    },
                    "dt_s": {
                        "type": "number",
                        "default": 0.001,
                        "exclusiveMinimum": 0,
                        "description": "Time step in seconds (> 0).",
                    },
                    "output_interval": {
                        "type": "number",
                        "default": 0.01,
                        "exclusiveMinimum": 0,
                        "description": "Output sampling interval in seconds (> 0, >= dt_s, <= duration_s).",
                    },
                    "backend": {
                        "type": "string",
                        "enum": ["isaac", "chrono", "gazebo"],
                        "default": "isaac",
                        "description": "Simulation backend. Defaults to isaac.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["batch", "teleop"],
                        "default": "batch",
                        "description": "Isaac and Gazebo support batch and teleop. Chrono supports batch only.",
                    },
                    "profile": {
                        "type": "object",
                        "description": "Optional Isaac runtime profile/config overrides.",
                    },
                    "urdf_path": {
                        "type": "string",
                        "description": (
                            "Path to URDF file from cad.export_sim_package. "
                            "Enables physics-based articulation simulation with Isaac."
                        ),
                    },
                    "sdf_path": {
                        "type": "string",
                        "description": (
                            "Path to SDF file from cad.export_sim_package(emit_sdf=true). "
                            "For Gazebo backend, provide urdf_path or sdf_path (sdf preferred)."
                        ),
                    },
                    "import_config": {
                        "type": "object",
                        "description": (
                            "URDF import config overrides: merge_fixed_joints, convex_decomp, "
                            "import_inertia_tensor, fix_base, distance_scale. "
                            "Set robot_type='mobile' for mobile robots (auto-applies "
                            "fix_base=False, merge_fixed_joints=True, lower stiffness/damping)."
                        ),
                    },
                    "verify": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Capture verification screenshots from 4 angles after URDF import. "
                            "Set false to skip for speed."
                        ),
                    },
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.teleop_start",
            "description": (
                "Start a teleop session for a mechanism. "
                "Isaac backend: legged robots, articulated mechanisms. "
                "Gazebo backend: drones (PX4 SITL), wheeled vehicles, supports vy_mps/vz_mps."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {"type": "string", "description": "Mechanism handle"},
                    "backend": {
                        "type": "string",
                        "enum": ["isaac", "gazebo"],
                        "default": "isaac",
                        "description": "Teleop backend (isaac or gazebo).",
                    },
                    "profile": {
                        "type": "object",
                        "description": (
                            "Optional teleop profile/config overrides. "
                            "All fields have sensible defaults for a 1-DOF hexapod tripod gait. "
                            "Keys: controller_type (Isaac default 'hexapod_1dof_tripod'; "
                            "Gazebo requires 'multirotor_direct' or 'px4_offboard'), "
                            "joint_names (list[str]), tripod_a/tripod_b (list[str], must partition joint_names), "
                            "left_legs/right_legs (list[str]), neutral_deg (float), "
                            "amplitude_deg (float, >0, oscillation amplitude), "
                            "stride_hz (float, >0, gait frequency), "
                            "yaw_mix_deg (float, >=0, yaw differential), "
                            "height_mix_deg (float, >=0, height offset gain), "
                            "vx_max_mps (float, >0, max forward velocity), "
                            "yaw_max_rps (float, >0, max yaw rate), "
                            "height_max_m (float, >0, max body height), "
                            "slew_vx_mps2/slew_yaw_rps2/slew_height_mps2 (float, >0, rate limiters)."
                        ),
                    },
                    "urdf_path": {
                        "type": "string",
                        "description": (
                            "Path to URDF file from cad.export_sim_package. "
                            "Enables physics-based articulation teleop with Isaac."
                        ),
                    },
                    "sdf_path": {
                        "type": "string",
                        "description": (
                            "Path to SDF file from cad.export_sim_package(emit_sdf=true). "
                            "For Gazebo backend, provide urdf_path or sdf_path."
                        ),
                    },
                    "import_config": {
                        "type": "object",
                        "description": (
                            "URDF import config overrides: merge_fixed_joints, convex_decomp, "
                            "import_inertia_tensor, fix_base, distance_scale. "
                            "Set robot_type='mobile' for mobile robots (auto-applies "
                            "fix_base=False, merge_fixed_joints=True, lower stiffness/damping)."
                        ),
                    },
                    "verify": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Capture verification screenshots from 4 angles after URDF import. "
                            "Set false to skip for speed."
                        ),
                    },
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.teleop_command",
            "description": "Send one drive command sample to an active Isaac teleop session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Teleop session ID"},
                    "vx_mps": {"type": "number", "default": 0.0, "description": "Forward velocity command (m/s)"},
                    "yaw_rate_rps": {"type": "number", "default": 0.0, "description": "Yaw-rate command (rad/s)"},
                    "body_height_m": {"type": "number", "default": 0.0, "description": "Body height command (m)"},
                    "vy_mps": {"type": "number", "default": 0.0, "description": "Lateral velocity command (m/s, Gazebo only)"},
                    "vz_mps": {"type": "number", "default": 0.0, "description": "Vertical velocity command (m/s, Gazebo only)"},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.teleop_state",
            "description": (
                "Read current state of an active Isaac teleop session. "
                "Returns state (vx_mps, yaw_rate_rps, body_height_m), uptime_s, "
                "and teleop telemetry: controller_type, joint_names, tick_count, "
                "limit_clamp_count, last_joint_targets_rad, last_apply_ok."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Teleop session ID"},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.teleop_stop",
            "description": (
                "Stop and close an active Isaac teleop session. "
                "Returns final telemetry: stopped, controller_type, tick_count, "
                "limit_clamp_count, last_joint_targets_rad. "
                "Cleans up engine resources so a new session can start fresh."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Teleop session ID"},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.isaac_screenshot",
            "description": (
                "Capture the Isaac Sim viewport as a PNG image. "
                "Use after importing a URDF or running a simulation to visually inspect the scene. "
                "Optionally reposition the camera before capture."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "width": {
                        "type": "integer",
                        "default": 1280,
                        "description": "Image width in pixels",
                    },
                    "height": {
                        "type": "integer",
                        "default": 720,
                        "description": "Image height in pixels",
                    },
                    "camera_position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Camera position [x, y, z] in meters",
                    },
                    "camera_target": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Camera look-at target [x, y, z] in meters",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Preset camera direction. One of: iso, front, back, top, "
                            "bottom, right, left. Auto-frames the scene from this angle. "
                            "Ignored when camera_position is provided."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "motion.verify_sim_package",
            "description": (
                "Verify that a mechanism exported correctly through the FreeCAD → URDF → Isaac pipeline. "
                "Runs up to 3 stages: (1) mechanism parts vs FreeCAD model tree, "
                "(2) mechanism vs URDF file (mesh existence, joint types/counts, mass/inertia, limits), "
                "(3) URDF vs Isaac USD scene (joint counts, DOF counts, drive configuration). "
                "Returns findings classified as block/warn/note. Use after export_sim_package "
                "and before running simulation to catch silent data loss."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mechanism_id": {
                        "type": "string",
                        "description": "Mechanism ID from motion.define_mechanism",
                    },
                    "urdf_path": {
                        "type": "string",
                        "description": "Path to the generated URDF file (from cad.export_sim_package)",
                    },
                    "doc": {
                        "type": "string",
                        "description": "FreeCAD document name (optional, for stage 1 model tree check)",
                    },
                    "check_isaac": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, also query Isaac Sim scene and compare against URDF (stage 3)",
                    },
                    "prim_path": {
                        "type": "string",
                        "description": "USD prim path to diagnose in Isaac (default '/'). Only used if check_isaac=true.",
                    },
                },
                "required": ["mechanism_id"],
                "additionalProperties": False,
            },
        },
    ]


def _rl_tool_list() -> list[dict[str, Any]]:
    """RL training pipeline tools."""
    return [
        {
            "name": "rl.configure_environment",
            "description": (
                "Parse URDF → URDFAnalysis → generate Isaac Lab env config. "
                "Classifies morphology, extracts joint topology, and writes "
                "a Python config file for training."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urdf_path": {"type": "string", "description": "Path to the URDF file"},
                    "output_path": {"type": "string", "description": "Output path for env config .py file (auto-generated if omitted)"},
                    "num_envs": {"type": "integer", "default": 4096, "description": "Number of parallel environments"},
                },
                "required": ["urdf_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "rl.start_training",
            "description": "Spawn rl_training/train.py subprocess, return training_id. Uses ISAAC_PYTHON env var if set.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "env_config": {"type": "string", "description": "Path to generated env config .py file"},
                    "output_dir": {"type": "string", "description": "Output directory for checkpoints (auto-generated if omitted)"},
                    "max_iterations": {"type": "integer", "description": "Override max training iterations"},
                    "num_envs": {"type": "integer", "description": "Override number of parallel environments"},
                },
                "required": ["env_config"],
                "additionalProperties": False,
            },
        },
        {
            "name": "rl.monitor_training",
            "description": "Read training progress → iteration, mean_reward, status, elapsed time.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "training_id": {"type": "string", "description": "Training ID from rl.start_training"},
                },
                "required": ["training_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "rl.stop_training",
            "description": "SIGTERM the training subprocess.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "training_id": {"type": "string", "description": "Training ID to stop"},
                },
                "required": ["training_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "rl.deploy_policy",
            "description": (
                "JIT export best checkpoint → return policy_path. "
                "Produces policy.pt + normalization_params.json + deployment_config.json."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "training_id": {"type": "string", "description": "Training ID to export from"},
                    "checkpoint_dir": {"type": "string", "description": "Direct path to checkpoint directory"},
                    "output_dir": {"type": "string", "description": "Output directory for deployed policy"},
                    "alpha": {"type": "number", "default": 0.3, "description": "Residual blending factor for deployment"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "rl.evaluate_policy",
            "description": "Validate a deployed policy: load check, output shape, basic inference test.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "policy_path": {"type": "string", "description": "Path to policy.pt file"},
                    "urdf_path": {"type": "string", "description": "Path to URDF for full evaluation (optional)"},
                    "num_episodes": {"type": "integer", "default": 10, "description": "Number of evaluation episodes"},
                },
                "required": ["policy_path"],
                "additionalProperties": False,
            },
        },
    ]


def _design_tool_list() -> list[dict[str, Any]]:
    """Design brief pipeline tools."""
    _STATUS_ENUM = ["intent", "sizing", "layout", "approved", "building", "done"]
    return [
        {
            "name": "design.save_brief",
            "description": (
                "Create a new design brief. Start of the phased design pipeline. "
                "Parameters dict is open — store whatever the design needs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Brief name"},
                    "parameters": {"type": "object", "description": "Design parameters (any key-value pairs)"},
                    "status": {
                        "type": "string",
                        "default": "intent",
                        "enum": _STATUS_ENUM,
                        "description": "Initial phase (typically 'intent')",
                    },
                    "research_notes": {
                        "type": "string",
                        "default": "",
                        "description": "Research notes and sources",
                    },
                },
                "required": ["name", "parameters"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.get_brief",
            "description": "Retrieve a saved brief by ID, including all parts and interfaces.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID from design.save_brief"},
                },
                "required": ["brief_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.update_brief",
            "description": (
                "Patch top-level brief fields: parameters, status, notes, name. "
                "Use status to advance through phases: intent → sizing → layout → approved → building → done."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID to update"},
                    "parameters": {"type": "object", "description": "New parameters (replaces all top-level params)"},
                    "status": {
                        "type": "string",
                        "enum": _STATUS_ENUM,
                        "description": "New phase/status",
                    },
                    "research_notes": {"type": "string", "description": "Updated research notes"},
                    "name": {"type": "string", "description": "Updated name"},
                },
                "required": ["brief_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.add_part",
            "description": (
                "Add a part to the brief. kind='custom' for parts to design in CAD, "
                "'purchased' for off-the-shelf components whose specs constrain "
                "the custom parts. specs is an open dict for any dimensions, "
                "materials, model numbers, mounting patterns, etc."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID"},
                    "name": {"type": "string", "description": "Part name (unique within brief)"},
                    "kind": {
                        "type": "string",
                        "enum": ["custom", "purchased"],
                        "default": "custom",
                        "description": "custom = design in CAD, purchased = off-the-shelf",
                    },
                    "quantity": {"type": "integer", "default": 1, "description": "How many of this part"},
                    "specs": {"type": "object", "description": "Part specs (any key-value pairs)"},
                },
                "required": ["brief_id", "name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.update_part",
            "description": (
                "Update fields on a named part. Use to mark as built, "
                "attach body_label after cad.new_body, or refine specs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID"},
                    "name": {"type": "string", "description": "Part name to update"},
                    "specs": {"type": "object", "description": "Updated specs (replaces all)"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "building", "built"],
                        "description": "Part build status",
                    },
                    "body_label": {"type": "string", "description": "FreeCAD body label after creation"},
                    "kind": {
                        "type": "string",
                        "enum": ["custom", "purchased"],
                        "description": "Updated kind",
                    },
                    "quantity": {"type": "integer", "description": "Updated quantity"},
                },
                "required": ["brief_id", "name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.get_part",
            "description": (
                "Get a single part and all its interfaces. Returns just what's "
                "needed to build that part — specs plus connection constraints "
                "from neighboring parts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID"},
                    "name": {"type": "string", "description": "Part name to retrieve"},
                },
                "required": ["brief_id", "name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.add_interface",
            "description": (
                "Define a connection between two parts. port_a/port_b name the "
                "connection points (e.g. 'top', 'base', 'arm_slot'). spec "
                "describes the physical connection (bolt pattern, press fit, etc.)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID"},
                    "part_a": {"type": "string", "description": "First part name"},
                    "port_a": {"type": "string", "description": "Connection point on part_a"},
                    "part_b": {"type": "string", "description": "Second part name"},
                    "port_b": {"type": "string", "description": "Connection point on part_b"},
                    "spec": {"type": "object", "description": "Connection spec (pattern, bolt size, fit type, etc.)"},
                },
                "required": ["brief_id", "part_a", "port_a", "part_b", "port_b"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.list_briefs",
            "description": "List all stored design briefs with summary info (id, name, status, counts).",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "design.verify_build",
            "description": (
                "Verify that all planned parts from a design brief exist in FreeCAD. "
                "Compares brief parts list against model tree. Reports MISSING, PARTIAL, "
                "STALE, or OK for each custom part. Checks bounding box dimensions "
                "against part specs. If mechanism_id is provided, also checks that "
                "every interface has a corresponding joint. Use before marking a brief as 'done'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID to verify"},
                    "doc": {"type": "string", "description": "Document name (optional)"},
                    "mechanism_id": {
                        "type": "string",
                        "description": (
                            "Optional mechanism handle from motion.define_mechanism. "
                            "If provided, checks that every brief interface has a "
                            "corresponding joint in the mechanism."
                        ),
                    },
                    "check_clearance": {
                        "type": "boolean",
                        "description": (
                            "If true, run a batch clearance check on all bodies "
                            "and report violations. Default false."
                        ),
                    },
                    "clearance_threshold_mm": {
                        "type": "number",
                        "description": (
                            "Minimum clearance in mm for the clearance check. "
                            "Default 0.5. Only used when check_clearance is true."
                        ),
                    },
                },
                "required": ["brief_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "design.generate_mechanism",
            "description": (
                "Auto-generate a Mechanism definition from a design brief's parts "
                "and interfaces. Maps interface types to joint types: bolt/clamp → fixed, "
                "bearing/shaft → revolute, slider/rail → prismatic. Returns a mechanism "
                "dict ready for motion.define_mechanism(). Review before committing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief_id": {"type": "string", "description": "Brief ID to generate mechanism from"},
                    "ground_part": {
                        "type": "string",
                        "description": (
                            "Part name to use as ground (is_ground=True). "
                            "Defaults to the first part in the brief."
                        ),
                    },
                },
                "required": ["brief_id"],
                "additionalProperties": False,
            },
        },
    ]


def _cad_measure_tool_list() -> list[dict[str, Any]]:
    """Measurement tools."""
    return [
        {
            "name": "cad.measure_between",
            "description": (
                "Measure the minimum distance between two references. "
                "Each ref can be a body name (uses tip shape), "
                "'Body.Face3' format (uses that sub-shape), "
                "or [x,y,z] point coordinates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref_a": {
                        "description": "First reference: body name, 'Body.Face3', or [x,y,z]",
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        ],
                    },
                    "ref_b": {
                        "description": "Second reference: body name, 'Body.Face3', or [x,y,z]",
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        ],
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["ref_a", "ref_b"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.check_clearance",
            "description": (
                "Batch clearance check between all PartDesign body pairs (or a "
                "specified subset). Reports pairs closer than threshold_mm, "
                "including intersecting bodies. Use after placing fasteners or "
                "near moving parts to verify nothing collides."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "bodies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of body names to check. "
                            "If omitted, checks all PartDesign::Body objects."
                        ),
                    },
                    "threshold_mm": {
                        "type": "number",
                        "description": (
                            "Minimum clearance threshold in mm (default 0.5). "
                            "Pairs closer than this are reported as violations."
                        ),
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.check_swept_clearance",
            "description": (
                "Swept clearance check — rotates a copy of a body's shape "
                "through angular steps around an axis/center and checks "
                "distToShape against other bodies at each angle. Use for "
                "rotating parts (propellers, gears, arms) to verify they "
                "clear nearby bodies across the full sweep. No document "
                "modifications — purely read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "body": {
                        "type": "string",
                        "description": "The rotating body name.",
                    },
                    "axis": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                        "description": "Rotation axis [x,y,z] (default [0,0,1]).",
                    },
                    "center": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                        "description": "Rotation center [x,y,z] in mm (default [0,0,0]).",
                    },
                    "angle_deg": {
                        "type": "number",
                        "description": "Total sweep angle in degrees (default 360).",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Number of angular steps (default 36 = every 10°).",
                    },
                    "others": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Bodies to check against. If omitted, checks all "
                            "other PartDesign::Body objects."
                        ),
                    },
                    "threshold_mm": {
                        "type": "number",
                        "description": (
                            "Violation threshold in mm (default 0.5). "
                            "Pairs closer than this at any angle are reported."
                        ),
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.assembly_audit",
            "description": (
                "Spatial coherence audit for multi-body assemblies. Returns all "
                "bodies with positions and bounding boxes, plus anomaly warnings: "
                "CLUSTER (bodies piled at same position), ISOLATED (body far from "
                "all neighbors), OVERLAP (excessive bounding box overlap), DRIFT "
                "(position differs from expected). Automatically uses the "
                "registered placement plan for DRIFT detection when no "
                "expected_positions are passed explicitly."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cluster_radius_mm": {
                        "type": "number",
                        "description": (
                            "Bodies within this distance are flagged as a cluster "
                            "(default 1.0mm). Catches 'forgot to call set_placement'."
                        ),
                    },
                    "isolation_radius_mm": {
                        "type": "number",
                        "description": (
                            "Body farther than this from all neighbors is flagged "
                            "as isolated (default 500.0mm). Catches wrong coordinates."
                        ),
                    },
                    "overlap_fraction": {
                        "type": "number",
                        "description": (
                            "Bounding box overlap threshold (0-1, default 0.8). "
                            "Pairs exceeding this are flagged as overlapping."
                        ),
                    },
                    "expected_positions": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 3,
                            "maxItems": 3,
                        },
                        "description": (
                            "Optional dict mapping body label/name to expected "
                            "[x,y,z] position. Bodies drifting >5mm from expected "
                            "position are flagged as DRIFT."
                        ),
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.register_placement_plan",
            "description": (
                "Register expected positions/rotations/sizes for bodies. "
                "After registration, every cad.set_placement returns a "
                "'plan_check' dict with position/rotation/size validation. "
                "Also auto-used by cad.assembly_audit for DRIFT detection. "
                "Auto-registered when design.update_brief transitions to "
                "'building' status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "position": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 3,
                                    "maxItems": 3,
                                    "description": "Expected [x,y,z] position in mm (required)",
                                },
                                "rotation_axis": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "description": "Expected rotation axis",
                                },
                                "rotation_angle_deg": {
                                    "type": "number",
                                    "description": "Expected rotation angle in degrees",
                                },
                                "expected_size": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 3,
                                    "maxItems": 3,
                                    "description": "Expected bounding box [sx,sy,sz] in mm",
                                },
                                "tolerance_mm": {
                                    "type": "number",
                                    "description": "Per-body position tolerance (default 5.0mm)",
                                },
                            },
                            "required": ["position"],
                        },
                        "description": "Dict mapping body label to plan entry.",
                    },
                    "default_tolerance_mm": {
                        "type": "number",
                        "description": "Default position tolerance in mm (default 5.0).",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.clear_placement_plan",
            "description": "Clear the registered placement plan.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "additionalProperties": False,
            },
        },
    ]


def _fastener_tool_list() -> list[dict[str, Any]]:
    """Fastener dimension lookup and geometry building tools."""
    return [
        {
            "name": "cad.fastener_spec",
            "description": (
                "Look up all dimensions for a metric fastener: head size, "
                "through-hole diameters (close/normal/loose fit), counterbore "
                "or countersink dimensions, tap drill size, socket/wrench size, "
                "washer + nut dimensions, and thread pitch. Accepts aliases: "
                "'socket' for socket_head, 'button' for button_head, "
                "'csk'/'flat' for countersunk. Avoids recalling ISO tables from memory."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "size": {
                        "type": "string",
                        "description": "Metric size, e.g. 'M4', 'M8'",
                    },
                    "length": {
                        "type": "number",
                        "default": 0,
                        "description": "Bolt shaft length in mm (optional, 0 if only head/hole dims needed)",
                    },
                    "head_type": {
                        "type": "string",
                        "default": "socket_head",
                        "description": "Head type: socket_head, hex, button_head, countersunk, set_screw (aliases: socket, button, csk, flat, grub)",
                    },
                },
                "required": ["size"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.bolt",
            "description": (
                "Build a complete bolt in FreeCAD. Creates a body with head + shaft "
                "in one call. Hex heads are proper hexagons; other types are cylindrical. "
                "Head sits at z=0..head_height, shaft extends z=0..-length. "
                "Use rotation_axis + rotation_angle_deg for non-vertical holes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "size": {
                        "type": "string",
                        "description": "Metric size, e.g. 'M4', 'M8'",
                    },
                    "length": {
                        "type": "number",
                        "description": "Shaft length in mm",
                        "exclusiveMinimum": 0,
                    },
                    "head_type": {
                        "type": "string",
                        "enum": ["socket_head", "hex", "button_head", "countersunk", "set_screw"],
                        "default": "socket_head",
                        "description": "Head type",
                    },
                    "position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                        "description": "Optional [x, y, z] placement in mm",
                    },
                    "rotation_axis": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                        "description": "Rotation axis, e.g. [1,0,0] to tilt bolt",
                    },
                    "rotation_angle_deg": {
                        "type": "number",
                        "default": 0.0,
                        "description": "Rotation angle in degrees",
                    },
                    "name": {
                        "type": "string",
                        "description": "Body label (default: Bolt_M4_socket_head etc.)",
                    },
                    "verify": {
                        "type": "boolean",
                        "default": True,
                        "description": "Capture verification screenshots",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["size", "length"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.nut",
            "description": (
                "Build a complete nut in FreeCAD. Creates a hexagonal prism body "
                "with a center through-hole in one call. Sits from z=0 to z=height. "
                "Nut types: hex (ISO 4032), thin (ISO 4035), nyloc (ISO 7040). "
                "Use rotation_axis + rotation_angle_deg for non-horizontal orientation."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "size": {
                        "type": "string",
                        "description": "Metric size, e.g. 'M4', 'M8'",
                    },
                    "nut_type": {
                        "type": "string",
                        "enum": ["hex", "thin", "nyloc"],
                        "default": "hex",
                        "description": "Nut type",
                    },
                    "position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                        "description": "Optional [x, y, z] placement in mm",
                    },
                    "rotation_axis": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                        "description": "Rotation axis, e.g. [1,0,0] to tilt nut",
                    },
                    "rotation_angle_deg": {
                        "type": "number",
                        "default": 0.0,
                        "description": "Rotation angle in degrees",
                    },
                    "name": {
                        "type": "string",
                        "description": "Body label (default: Nut_M4_hex etc.)",
                    },
                    "verify": {
                        "type": "boolean",
                        "default": True,
                        "description": "Capture verification screenshots",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                },
                "required": ["size"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cad.find_holes",
            "description": (
                "Find cylindrical holes in a body. Returns each hole's diameter, "
                "axis direction, center position, and depth. Suggests matching "
                "bolt sizes from ISO 273 clearance hole tables. Use this to "
                "identify where to place bolts and nuts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "body": {
                        "type": "string",
                        "description": "Body name to inspect (uses active body if omitted)",
                    },
                    "doc": {"type": "string", "description": "Document name (optional)"},
                    "min_diameter": {
                        "type": "number",
                        "default": 0.0,
                        "description": "Minimum hole diameter to report (mm)",
                    },
                    "max_diameter": {
                        "type": "number",
                        "default": 200.0,
                        "description": "Maximum hole diameter to report (mm)",
                    },
                },
                "additionalProperties": False,
            },
        },
    ]


def _tool_list() -> list[dict[str, Any]]:
    return (
        _cad_tool_list()
        + _cad_measure_tool_list()
        + _mfg_tool_list()
        + _spec_tool_list()
        + _me_tool_list()
        + _knowledge_tool_list()
        + _geometry_tool_list()
        + _study_tool_list()
        + _motion_tool_list()
        + _rl_tool_list()
        + _design_tool_list()
        + _fastener_tool_list()
    )


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
    "cad.mirror": cad_mirror,
    "cad.linear_pattern": cad_linear_pattern,
    "cad.thickness": cad_thickness,
    "cad.draft": cad_draft,
    "cad.pocket": cad_pocket,
    "cad.sweep": cad_sweep,
    "cad.helix": cad_helix,
    "cad.loft": cad_loft,
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
    "cad.screenshot": cad_screenshot,
    "cad.set_camera": cad_set_camera,
    "cad.get_camera": cad_get_camera,
    "cad.undo": cad_undo,
    "cad.delete_objects": cad_delete_objects,
    "cad.export": cad_export,
    "cad.export_body": cad_export_body,
    "cad.export_sim_package": cad_export_sim_package,
    "cad.set_placement": cad_set_placement,
    "cad.set_visibility": cad_set_visibility,
    "cad.animate": cad_animate,
    "cad.animate_stop": cad_animate_stop,
    "cad.freecad_info": cad_freecad_info,
    "cad.create_primitive": cad_create_primitive,
    "cad.create_primitives": cad_create_primitives,
    "cad.measure_between": cad_measure_between,
    "cad.check_clearance": cad_check_clearance,
    "cad.check_swept_clearance": cad_check_swept_clearance,
    "cad.assembly_audit": cad_assembly_audit,
    "cad.register_placement_plan": cad_register_placement_plan,
    "cad.clear_placement_plan": cad_clear_placement_plan,
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
    "spec.assess_design_path": spec_assess_design_path,
    "spec.generate_cad": spec_generate_cad,
    "spec.plan_geometry": spec_plan_geometry,
}

_ME_DISPATCH: dict[str, Any] = {
    "me.validate_constraints": me_validate_constraints,
    "me.build_traceability": me_build_traceability,
    "me.apply_risk_gates": me_apply_risk_gates,
    "me.design_loop": me_design_loop,
    "me.list_validators": me_list_validators,
}

_KNOWLEDGE_DISPATCH: dict[str, Any] = {
    "knowledge.extract": knowledge_extract,
    "knowledge.ingest": knowledge_ingest,
    "knowledge.ingest_status": knowledge_ingest_status,
    "knowledge.search": knowledge_search,
    "knowledge.status": knowledge_status,
}

_GEOMETRY_DISPATCH: dict[str, Any] = {
    "geometry.spur_gear": geometry_spur_gear,
    "geometry.tooth_slot": geometry_tooth_slot,
    "geometry.gear_params": geometry_gear_params,
    "geometry.planetary_layout": geometry_planetary_layout,
    "geometry.involute_points": geometry_involute_points,
    "geometry.propeller_blade": geometry_propeller_blade,
}

_STUDY_DISPATCH: dict[str, Any] = {
    "study.create": study_create,
    "study.run": study_run,
    "study.status": study_status,
    "study.results": study_results,
    "study.cancel": study_cancel,
    "study.list": study_list,
    "study.get_variant": study_get_variant,
}

_MOTION_DISPATCH: dict[str, Any] = {
    "motion.define_mechanism": motion_define_mechanism,
    "motion.list_mechanisms": motion_list_mechanisms,
    "motion.validate": motion_validate,
    "motion.propagate_motion": motion_propagate_motion,
    "motion.check_gear_train": motion_check_gear_train,
    "motion.create_assembly": motion_create_assembly,
    "motion.drive_joint": motion_drive_joint,
    "motion.check_joint_connectivity": motion_check_joint_connectivity,
    "motion.check_interference": motion_check_interference,
    "motion.simulate": motion_simulate,
    "motion.teleop_start": motion_teleop_start,
    "motion.teleop_command": motion_teleop_command,
    "motion.teleop_state": motion_teleop_state,
    "motion.teleop_stop": motion_teleop_stop,
    "motion.isaac_screenshot": motion_isaac_screenshot,
    "motion.verify_sim_package": motion_verify_sim_package,
}

_RL_DISPATCH: dict[str, Any] = {
    "rl.configure_environment": rl_configure_environment,
    "rl.start_training": rl_start_training,
    "rl.monitor_training": rl_monitor_training,
    "rl.stop_training": rl_stop_training,
    "rl.deploy_policy": rl_deploy_policy,
    "rl.evaluate_policy": rl_evaluate_policy,
}

_DESIGN_DISPATCH: dict[str, Any] = {
    "design.save_brief": design_save_brief,
    "design.get_brief": design_get_brief,
    "design.update_brief": design_update_brief,
    "design.add_part": design_add_part,
    "design.update_part": design_update_part,
    "design.get_part": design_get_part,
    "design.add_interface": design_add_interface,
    "design.list_briefs": design_list_briefs,
    "design.verify_build": design_verify_build,
    "design.generate_mechanism": design_generate_mechanism,
}

_FASTENER_DISPATCH: dict[str, Any] = {
    "cad.fastener_spec": cad_fastener_spec,
    "cad.bolt": cad_bolt,
    "cad.nut": cad_nut,
    "cad.find_holes": cad_find_holes,
}


def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    handler = (
        _CAD_DISPATCH.get(name)
        or _MFG_DISPATCH.get(name)
        or _SPEC_DISPATCH.get(name)
        or _ME_DISPATCH.get(name)
        or _KNOWLEDGE_DISPATCH.get(name)
        or _GEOMETRY_DISPATCH.get(name)
        or _STUDY_DISPATCH.get(name)
        or _MOTION_DISPATCH.get(name)
        or _RL_DISPATCH.get(name)
        or _DESIGN_DISPATCH.get(name)
        or _FASTENER_DISPATCH.get(name)
    )
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
                content: list[dict[str, Any]] = []

                # Handle verification images from modeling operations
                if isinstance(out, dict) and "verification_images" in out:
                    images = out.pop("verification_images")
                    # Preserve view labels so LLM knows which image is which
                    out["verification_views"] = [img.get("view", "unknown") for img in images]
                    for img in images:
                        content.append({
                            "type": "image",
                            "data": img["image_base64"],
                            "mimeType": img["mime_type"],
                        })

                # Handle single image from cad.screenshot
                if isinstance(out, dict) and "image_base64" in out:
                    content.append({
                        "type": "image",
                        "data": out.pop("image_base64"),
                        "mimeType": out.pop("mime_type", "image/png"),
                    })

                # Always include text result (remaining metadata)
                content.append({"type": "text", "text": json.dumps(out)})

                _send(_rpc_result(rpc_id, {"isError": False, "content": content}))
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
