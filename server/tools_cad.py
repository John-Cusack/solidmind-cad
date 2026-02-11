"""MCP tool implementations for CAD operations.

Each function corresponds to a ``cad.*`` MCP tool.  They translate MCP tool
arguments into FreeCAD addon socket commands via ``freecad_client``.
"""
from __future__ import annotations

from typing import Any

from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _wrap(fn: Any) -> Any:
    """Decorator to catch connection/command errors and return MCP error format."""
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except FreeCADConnectionError as e:
            return _error_result("CONNECTION_ERROR", str(e))
        except FreeCADCommandError as e:
            return _error_result("COMMAND_ERROR", str(e))
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
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a sketch, populate it with geometry and constraints, and close it.

    This is a compound tool that combines new_sketch + geometry + constraints +
    close_sketch into a single MCP tool call for convenience.

    ``elements`` is a list of geometry dicts, each with a ``type`` field:
    - ``{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50}``
    - ``{"type": "circle", "cx": 0, "cy": 0, "r": 10}``
    - ``{"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}``
    - ``{"type": "arc", "cx": 0, "cy": 0, "r": 10, "start_angle": 0, "end_angle": 90}``

    ``constraints`` is a list of constraint dicts with a ``type`` field and
    parameters specific to the constraint type.
    """
    client = get_client()

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
    doc: str | None = None,
) -> dict[str, Any]:
    """Extrude (pad) a sketch to create a solid."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketch": sketch,
        "length": length,
        "symmetric": symmetric,
        "reversed": reversed,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("pad", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_pocket(
    sketch: str,
    length: float = 0.0,
    pocket_type: str = "Dimension",
    reversed: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
    """Cut a pocket from a sketch."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "sketch": sketch,
        "length": length,
        "pocket_type": pocket_type,
        "reversed": reversed,
    }
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("pocket", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_hole(
    face: str,
    diameter: float,
    depth: float,
    body: str | None = None,
    hole_type: str = "Dimension",
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a hole on a face."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "face": face,
        "diameter": diameter,
        "depth": depth,
        "hole_type": hole_type,
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
    doc: str | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Fillet edges. Accepts edges list or a named selection."""
    client = get_client()
    kwargs: dict[str, Any] = {"radius": radius}
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
    doc: str | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Chamfer edges. Accepts edges list or a named selection."""
    client = get_client()
    kwargs: dict[str, Any] = {"size": size}
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
