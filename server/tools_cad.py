"""MCP tool implementations for CAD operations.

Each function corresponds to a ``cad.*`` MCP tool.  They translate MCP tool
arguments into FreeCAD addon socket commands via ``freecad_client``.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

log = logging.getLogger("solidmind.tools_cad")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))

# Keys whose values are too bulky for INFO-level entry logging
_BULK_KEYS = frozenset({"elements", "constraints", "geometry_ref"})


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _wrap(fn: Any) -> Any:
    """Decorator to catch connection/command errors and return MCP error format.

    When ``SOLIDMIND_TOOL_LOG=1``, also logs CALL/OK/FAIL with timing.
    Errors are always logged regardless of the toggle.
    """
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if not _TOOL_LOG:
            try:
                return fn(*args, **kwargs)
            except FreeCADConnectionError as e:
                log.error("FAIL %s CONNECTION_ERROR: %s", fn.__name__, e)
                return _error_result("CONNECTION_ERROR", str(e))
            except FreeCADCommandError as e:
                log.error("FAIL %s COMMAND_ERROR: %s", fn.__name__, e)
                return _error_result("COMMAND_ERROR", str(e))

        # Verbose mode: log entry, timing, and exit
        compact = {k: v for k, v in kwargs.items() if k not in _BULK_KEYS}
        log.info("CALL %s %s", fn.__name__, compact)
        for k in _BULK_KEYS:
            if k in kwargs and kwargs[k] is not None:
                val = kwargs[k]
                if isinstance(val, list):
                    log.debug("  %s: %d items", k, len(val))
                else:
                    log.debug("  %s: %s", k, type(val).__name__)

        t0 = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except FreeCADConnectionError as e:
            elapsed = time.monotonic() - t0
            log.error("FAIL %s %.3fs CONNECTION_ERROR: %s", fn.__name__, elapsed, e)
            return _error_result("CONNECTION_ERROR", str(e))
        except FreeCADCommandError as e:
            elapsed = time.monotonic() - t0
            log.error("FAIL %s %.3fs COMMAND_ERROR: %s", fn.__name__, elapsed, e)
            return _error_result("COMMAND_ERROR", str(e))

        elapsed = time.monotonic() - t0
        if isinstance(result, dict) and not result.get("ok", True):
            err = result.get("error", {})
            log.warning(
                "FAIL %s %.3fs code=%s msg=%s",
                fn.__name__, elapsed,
                err.get("code", "?"), err.get("message", "?"),
            )
        else:
            log.info("OK   %s %.3fs", fn.__name__, elapsed)
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_wrap
def cad_new_document(name: str = "Unnamed") -> dict[str, Any]:
    """Create a new FreeCAD document."""
    client = get_client()
    result = client.send_command("new_document", name=name)
    return {"ok": True, **result}


@_wrap
def cad_new_body(name: str = "Body", doc: str | None = None) -> dict[str, Any]:
    """Create a PartDesign Body in the document."""
    client = get_client()
    kwargs: dict[str, Any] = {"name": name}
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("new_body", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_sketch(
    body: str,
    plane: str = "XY",
    elements: list[dict[str, Any]] | None = None,
    constraints: list[dict[str, Any]] | None = None,
    geometry_ref: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a sketch, populate it with geometry and constraints, and close it.

    This is a compound tool that combines new_sketch + geometry + constraints +
    close_sketch into a single MCP tool call for convenience.

    ``geometry_ref`` is a handle returned by a ``geometry.*`` tool.  When
    provided, the stored elements are resolved server-side and added to the
    sketch *before* any inline ``elements``.  This avoids sending bulk geometry
    data through the LLM.

    ``elements`` is a list of geometry dicts, each with a ``type`` field:
    - ``{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50}``
    - ``{"type": "circle", "cx": 0, "cy": 0, "r": 10}``
    - ``{"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}``
    - ``{"type": "arc", "cx": 0, "cy": 0, "r": 10, "start_angle": 0, "end_angle": 90}``

    ``constraints`` is a list of constraint dicts with a ``type`` field and
    parameters specific to the constraint type.
    """
    from server.geometry_store import retrieve as _retrieve_geometry

    client = get_client()

    # Resolve geometry_ref into elements
    ref_elements: list[dict[str, Any]] = []
    if geometry_ref is not None:
        stored = _retrieve_geometry(geometry_ref)
        if stored is None:
            return _error_result(
                "INVALID_GEOMETRY_REF",
                f"Geometry reference '{geometry_ref}' not found in store. "
                "It may have expired or never existed.",
            )
        ref_elements = stored

    # Combine: ref elements first, then inline elements
    all_elements = ref_elements + (elements or [])

    # 1. Create sketch
    sk_kwargs: dict[str, Any] = {"body": body, "plane": plane}
    if doc is not None:
        sk_kwargs["doc"] = doc
    sk_result = client.send_command("new_sketch", **sk_kwargs)
    sketch_name = sk_result["sketch"]

    cmd_kwargs: dict[str, Any] = {"sketch": sketch_name}
    if doc is not None:
        cmd_kwargs["doc"] = doc

    geometry_indices: list[dict[str, Any]] = []

    # 2. Add geometry elements
    if all_elements:
        elements = all_elements
    if elements:
        for elem in elements:
            elem_type = elem.get("type", "")
            if elem_type == "rect":
                r = client.send_command(
                    "sketch_rect",
                    x=elem.get("x", 0), y=elem.get("y", 0),
                    w=elem["w"], h=elem["h"],
                    **cmd_kwargs,
                )
                geometry_indices.append({"type": "rect", "indices": r.get("geometry_indices", [])})
            elif elem_type == "circle":
                r = client.send_command(
                    "sketch_circle",
                    cx=elem.get("cx", 0), cy=elem.get("cy", 0),
                    r=elem["r"],
                    **cmd_kwargs,
                )
                geometry_indices.append({"type": "circle", "index": r.get("geometry_index")})
            elif elem_type == "line":
                r = client.send_command(
                    "sketch_line",
                    x1=elem["x1"], y1=elem["y1"],
                    x2=elem["x2"], y2=elem["y2"],
                    **cmd_kwargs,
                )
                geometry_indices.append({"type": "line", "index": r.get("geometry_index")})
            elif elem_type == "arc":
                r = client.send_command(
                    "sketch_arc",
                    cx=elem.get("cx", 0), cy=elem.get("cy", 0),
                    r=elem["r"],
                    start_angle=elem["start_angle"], end_angle=elem["end_angle"],
                    **cmd_kwargs,
                )
                geometry_indices.append({"type": "arc", "index": r.get("geometry_index")})
            elif elem_type == "spline":
                sp_kwargs: dict[str, Any] = {"points": elem["points"]}
                if "degree" in elem:
                    sp_kwargs["degree"] = elem["degree"]
                if "periodic" in elem:
                    sp_kwargs["periodic"] = elem["periodic"]
                if "weights" in elem:
                    sp_kwargs["weights"] = elem["weights"]
                r = client.send_command(
                    "sketch_bspline",
                    **sp_kwargs,
                    **cmd_kwargs,
                )
                geometry_indices.append({"type": "spline", "index": r.get("geometry_index")})
            else:
                return _error_result("INVALID_ELEMENT", f"Unknown element type: {elem_type}")

    # 3. Add constraints
    if constraints:
        for con in constraints:
            con_type = con.pop("type", None)
            if con_type is None:
                continue
            client.send_command(
                "sketch_constrain",
                constraint_type=con_type,
                **{k: v for k, v in con.items() if k != "type"},
                **cmd_kwargs,
            )

    # 4. Close sketch
    close_result = client.send_command("close_sketch", **cmd_kwargs)

    return {
        "ok": True,
        "sketch": sketch_name,
        "geometry": geometry_indices,
        "fully_constrained": close_result.get("fully_constrained"),
    }


@_wrap
def cad_pad(
    sketch: str,
    length: float,
    symmetric: bool = False,
    reversed: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Extrude (pad) a sketch to create a solid."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketch": sketch,
        "length": length,
        "symmetric": symmetric,
        "reversed": reversed,
        "verify": verify,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("pad", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_revolution(
    sketch: str,
    axis: str = "V",
    angle: float = 360.0,
    symmetric: bool = False,
    reversed: bool = False,
    subtractive: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Revolve a sketch around an axis to create a solid of revolution."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketch": sketch,
        "axis": axis,
        "angle": angle,
        "symmetric": symmetric,
        "reversed": reversed,
        "subtractive": subtractive,
        "verify": verify,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("revolution", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_polar_pattern(
    features: list[str],
    axis: str = "Base_Z",
    occurrences: int = 6,
    angle: float = 360.0,
    reversed: bool = False,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a polar (circular) pattern of features around an axis."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "features": features,
        "axis": axis,
        "occurrences": occurrences,
        "angle": angle,
        "reversed": reversed,
        "verify": verify,
    }
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("polar_pattern", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_pocket(
    sketch: str,
    length: float = 0.0,
    pocket_type: str = "Dimension",
    reversed: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Cut a pocket from a sketch."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketch": sketch,
        "length": length,
        "pocket_type": pocket_type,
        "reversed": reversed,
        "verify": verify,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("pocket", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_sweep(
    profile_sketch: str,
    spine_sketch: str,
    subtractive: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Sweep a profile sketch along a spine sketch."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "profile_sketch": profile_sketch,
        "spine_sketch": spine_sketch,
        "subtractive": subtractive,
        "verify": verify,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("sweep", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_helix(
    sketch: str,
    pitch: float = 0.0,
    height: float = 0.0,
    turns: float = 0.0,
    axis: str = "V",
    angle: float = 0.0,
    growth: float = 0.0,
    left_handed: bool = False,
    reversed: bool = False,
    mode: str = "pitch-height",
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a helical sweep of a sketch profile."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketch": sketch,
        "pitch": pitch,
        "height": height,
        "turns": turns,
        "axis": axis,
        "angle": angle,
        "growth": growth,
        "left_handed": left_handed,
        "reversed": reversed,
        "mode": mode,
        "verify": verify,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("helix", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_loft(
    sketches: list[str],
    ruled: bool = False,
    closed: bool = False,
    subtractive: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Loft between two or more sketch profiles."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketches": sketches,
        "ruled": ruled,
        "closed": closed,
        "subtractive": subtractive,
        "verify": verify,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("loft", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_hole(
    face: str,
    diameter: float,
    depth: float,
    body: str | None = None,
    hole_type: str = "Dimension",
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a hole on a face."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "face": face,
        "diameter": diameter,
        "depth": depth,
        "hole_type": hole_type,
        "verify": verify,
    }
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("hole", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_fillet(
    edges: list[str] | None = None,
    radius: float = 1.0,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Fillet edges. Accepts edges list or a named selection."""
    client = get_client()
    kwargs: dict[str, Any] = {"radius": radius, "verify": verify}
    if edges is not None:
        kwargs["edges"] = edges
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    if selection is not None:
        kwargs["selection"] = selection
    result = client.send_command("fillet", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_chamfer(
    edges: list[str] | None = None,
    size: float = 1.0,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Chamfer edges. Accepts edges list or a named selection."""
    client = get_client()
    kwargs: dict[str, Any] = {"size": size, "verify": verify}
    if edges is not None:
        kwargs["edges"] = edges
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    if selection is not None:
        kwargs["selection"] = selection
    result = client.send_command("chamfer", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_get_dimensions(object_name: str, doc: str | None = None) -> dict[str, Any]:
    """Get bounding box, volume, surface area, and topology counts of an object."""
    client = get_client()
    kwargs: dict[str, Any] = {"object_name": object_name}
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("get_dimensions", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_get_body_topology(body: str | None = None, doc: str | None = None) -> dict[str, Any]:
    """Get all faces and edges on the body with geometric properties."""
    client = get_client()
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("get_body_topology", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_find_edges(
    body: str | None = None,
    doc: str | None = None,
    axis: str | None = None,
    curve_type: str | None = None,
    min_length: float | None = None,
    max_length: float | None = None,
    on_face: str | None = None,
    near_point: list[float] | None = None,
    near_distance: float | None = None,
    convexity: str | None = None,
) -> dict[str, Any]:
    """Find edges matching geometric criteria. Returns edge names for use with fillet/chamfer."""
    client = get_client()
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    if axis is not None:
        kwargs["axis"] = axis
    if curve_type is not None:
        kwargs["curve_type"] = curve_type
    if min_length is not None:
        kwargs["min_length"] = min_length
    if max_length is not None:
        kwargs["max_length"] = max_length
    if on_face is not None:
        kwargs["on_face"] = on_face
    if near_point is not None:
        kwargs["near_point"] = near_point
    if near_distance is not None:
        kwargs["near_distance"] = near_distance
    if convexity is not None:
        kwargs["convexity"] = convexity
    result = client.send_command("find_edges", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_define_selection(
    name: str,
    query: dict[str, Any],
    invariants: dict[str, Any] | None = None,
    body: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Define a named edge selection query with optional invariants."""
    client = get_client()
    kwargs: dict[str, Any] = {"name": name, "query": query}
    if invariants is not None:
        kwargs["invariants"] = invariants
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("define_selection", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_resolve_selection(
    name: str,
    body: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Re-resolve a named selection against current geometry."""
    client = get_client()
    kwargs: dict[str, Any] = {"name": name}
    if body is not None:
        kwargs["body"] = body
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("resolve_selection", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_list_selections() -> dict[str, Any]:
    """List all defined selection sets."""
    client = get_client()
    result = client.send_command("list_selections")
    return {"ok": True, **result}


@_wrap
def cad_delete_selection(name: str) -> dict[str, Any]:
    """Remove a named selection set."""
    client = get_client()
    result = client.send_command("delete_selection", name=name)
    return {"ok": True, **result}


@_wrap
def cad_get_selection() -> dict[str, Any]:
    """Get the current selection in FreeCAD (what the user clicked on)."""
    client = get_client()
    result = client.send_command("get_selection")
    return {"ok": True, **result}


@_wrap
def cad_get_model_tree(doc: str | None = None) -> dict[str, Any]:
    """Get the feature tree of the current document."""
    client = get_client()
    kwargs: dict[str, Any] = {}
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("get_model_tree", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_undo(doc: str | None = None) -> dict[str, Any]:
    """Undo the last operation in FreeCAD."""
    client = get_client()
    kwargs: dict[str, Any] = {}
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("undo", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_screenshot(
    target: str | list[float] = "iso",
    distance: float = 2.0,
    direction: list[float] | None = None,
    up: list[float] | None = None,
    near_clip: float | None = None,
    width: int = 512,
    height: int = 512,
    doc: str | None = None,
) -> dict[str, Any]:
    """Take a screenshot with smart camera targeting."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "target": target,
        "distance": distance,
        "width": width,
        "height": height,
    }
    if direction is not None:
        kwargs["direction"] = direction
    if up is not None:
        kwargs["up"] = up
    if near_clip is not None:
        kwargs["near_clip"] = near_clip
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("screenshot", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_set_camera(
    position: list[float] | None = None,
    target: list[float] | None = None,
    up: list[float] | None = None,
    near_clip: float | None = None,
    fit_all: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
    """Set camera position and orientation."""
    client = get_client()
    kwargs: dict[str, Any] = {"fit_all": fit_all}
    if position is not None:
        kwargs["position"] = position
    if target is not None:
        kwargs["target"] = target
    if up is not None:
        kwargs["up"] = up
    if near_clip is not None:
        kwargs["near_clip"] = near_clip
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("set_camera", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_get_camera(doc: str | None = None) -> dict[str, Any]:
    """Get current camera state."""
    client = get_client()
    kwargs: dict[str, Any] = {}
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("get_camera", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_export(
    format: str = "step",
    path: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Export the document to STEP, STL, or FCStd."""
    client = get_client()
    kwargs: dict[str, Any] = {"format": format}
    if path is not None:
        kwargs["path"] = path
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("export", **kwargs)
    return {"ok": True, **result}
