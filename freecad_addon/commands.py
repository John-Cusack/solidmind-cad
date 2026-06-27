"""FreeCAD API command handlers for the SolidMind CAD addon.

Each public function accepts keyword arguments (from the protocol ``args`` dict)
and returns a result value that will be wrapped in a ``Response``.  If the
function raises, the socket server catches it and returns an error response.

FreeCAD modules (``FreeCAD``, ``FreeCADGui``, ``Part``, ``Sketcher``) are
imported at module level — this file is only loaded inside FreeCAD.
"""
from __future__ import annotations

import base64
import logging
import math
import tempfile
from pathlib import Path
from typing import Any

import FreeCAD  # type: ignore[import-untyped]
import Part  # type: ignore[import-untyped]

logger = logging.getLogger("solidmind.commands")


def _set_sketch_support(sketch: Any, support: Any, map_mode: str = "FlatFace") -> None:
    """Set sketch attachment support — delegates to compat layer."""
    from freecad_addon.compat import set_sketch_support
    set_sketch_support(sketch, support, map_mode)


# FreeCADGui may not be available in headless mode (FreeCADCmd).
try:
    import FreeCADGui  # type: ignore[import-untyped]
except ImportError:
    FreeCADGui = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Document & Structure
# ---------------------------------------------------------------------------

def new_document(name: str = "Unnamed") -> dict[str, Any]:
    """Create a new FreeCAD document."""
    logger.info("new_document: name=%s", name)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(doc.Name)
    logger.info("new_document: created %s", doc.Name)
    return {"name": doc.Name, "label": doc.Label}


def save_document(doc: str | None = None, path: str | None = None) -> dict[str, Any]:
    """Save the FreeCAD document. If *path* is given, saves to that location
    (saveAs). Otherwise saves in place if the document already has a file path,
    or saves to /tmp/<DocName>.FCStd as a fallback."""
    d = _get_doc(doc)
    if path is not None:
        d.saveAs(path)
        return {"saved": True, "path": path}
    # Try save in place
    if d.FileName:
        d.save()
        return {"saved": True, "path": d.FileName}
    # First save — use a default path
    default_path = str(Path(tempfile.gettempdir()) / f"{d.Name}.FCStd")
    d.saveAs(default_path)
    return {"saved": True, "path": default_path}


def new_body(doc: str | None = None, name: str = "Body") -> dict[str, Any]:
    """Create a PartDesign Body in the given document."""
    logger.info("new_body: name=%s", name)
    d = _get_doc(doc)
    body = d.addObject("PartDesign::Body", name)
    d.recompute()
    logger.info("new_body: created %s", body.Name)
    return {"name": body.Name, "label": body.Label}


def get_model_tree(doc: str | None = None, detail: str = "bodies") -> dict[str, Any]:
    """Return the feature tree of the document.

    ``detail`` controls verbosity:
    - ``"bodies"`` (default): compact body-level overview with sizes and
      feature counts.  Non-body top-level objects listed in ``other_objects``.
    - ``"full"``: flat list of every object with bounding boxes and topology
      counts (legacy behaviour).
    """
    d = _get_doc(doc)

    if detail == "full":
        tree: list[dict[str, Any]] = []
        for obj in d.Objects:
            node: dict[str, Any] = {
                "name": obj.Name,
                "label": obj.Label,
                "type": obj.TypeId,
            }
            if hasattr(obj, "Shape") and obj.Shape is not None:
                try:
                    bb = obj.Shape.BoundBox
                    node["bounding_box"] = {
                        "x_min": bb.XMin, "y_min": bb.YMin, "z_min": bb.ZMin,
                        "x_max": bb.XMax, "y_max": bb.YMax, "z_max": bb.ZMax,
                        "x_len": bb.XLength, "y_len": bb.YLength, "z_len": bb.ZLength,
                    }
                except Exception:
                    pass
                node["is_valid"] = getattr(obj, "isValid", lambda: True)()
                node["num_faces"] = len(obj.Shape.Faces)
                node["num_edges"] = len(obj.Shape.Edges)
                node["num_vertices"] = len(obj.Shape.Vertexes)
            tree.append(node)
        return {"doc": d.Name, "objects": tree}

    # detail == "bodies" — compact body-centric output
    bodies: list[dict[str, Any]] = []
    other_objects: list[str] = []
    body_names: set[str] = set()

    for obj in d.Objects:
        if obj.TypeId == "PartDesign::Body":
            body_names.add(obj.Name)
            entry: dict[str, Any] = {
                "name": obj.Name,
                "label": obj.Label,
            }
            try:
                tip = _get_tip(obj)
                entry["tip"] = tip.Name
                if hasattr(tip, "Shape") and tip.Shape is not None:
                    bb = tip.Shape.BoundBox
                    entry["size"] = [
                        round(bb.XLength, 2),
                        round(bb.YLength, 2),
                        round(bb.ZLength, 2),
                    ]
            except Exception:
                entry["tip"] = None
            entry["feature_count"] = len(obj.Group)
            # Placement position & rotation
            plc = obj.Placement
            entry["position"] = [
                round(plc.Base.x, 3),
                round(plc.Base.y, 3),
                round(plc.Base.z, 3),
            ]
            rot = plc.Rotation
            entry["rotation_angle_deg"] = round(math.degrees(rot.Angle), 3)
            axis = rot.Axis
            entry["rotation_axis"] = [round(axis.x, 6), round(axis.y, 6), round(axis.z, 6)]
            # World-space bounding box from tip shape
            try:
                tip = _get_tip(obj)
                if hasattr(tip, "Shape") and tip.Shape is not None:
                    bb = tip.Shape.BoundBox
                    entry["world_bbox"] = {
                        "min": [round(bb.XMin, 2), round(bb.YMin, 2), round(bb.ZMin, 2)],
                        "max": [round(bb.XMax, 2), round(bb.YMax, 2), round(bb.ZMax, 2)],
                    }
            except Exception:
                pass
            bodies.append(entry)

    for obj in d.Objects:
        if obj.TypeId == "App::Origin":
            continue
        if obj.TypeId == "PartDesign::Body":
            continue
        # Skip objects that belong to a body (features, sketches, etc.)
        parents = getattr(obj, "InList", [])
        if any(p.Name in body_names for p in parents):
            continue
        other_objects.append(obj.Name)

    return {
        "doc": d.Name,
        "body_count": len(bodies),
        "bodies": bodies,
        "other_objects": other_objects,
    }


def undo(doc: str | None = None) -> dict[str, Any]:
    """Undo the last operation."""
    d = _get_doc(doc)
    d.undo()
    d.recompute()
    return {"undone": True}


def redo(doc: str | None = None) -> dict[str, Any]:
    """Redo the last undone operation."""
    d = _get_doc(doc)
    d.redo()
    d.recompute()
    return {"redone": True}


# ---------------------------------------------------------------------------
# Sketcher
# ---------------------------------------------------------------------------

def new_sketch(
    body: str,
    plane: str = "XY",
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a new sketch on a plane (XY, XZ, YZ) or a face reference.

    Returns the sketch object name so subsequent sketch_* commands can
    reference it.
    """
    logger.info("new_sketch: body=%s plane=%s", body, plane)
    d = _get_doc(doc)
    body_obj = d.getObject(body)
    if body_obj is None:
        raise ValueError(f"Body '{body}' not found in document '{d.Name}'")

    sketch = d.addObject("Sketcher::SketchObject", "Sketch")
    body_obj.addObject(sketch)

    plane_map = {
        "XY": FreeCAD.Placement(
            FreeCAD.Vector(0, 0, 0),
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), 0),
        ),
        "XZ": FreeCAD.Placement(
            FreeCAD.Vector(0, 0, 0),
            FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), -90),
        ),
        "YZ": FreeCAD.Placement(
            FreeCAD.Vector(0, 0, 0),
            FreeCAD.Rotation(FreeCAD.Vector(0, 1, 0), 90),
        ),
    }

    if plane.upper() in plane_map:
        sketch.Placement = plane_map[plane.upper()]
    else:
        # Face reference like "Face3" — must reference the tip feature, not
        # the Body, otherwise FreeCAD complains about DAG scope violations.
        tip = _get_tip(body_obj)
        _set_sketch_support(sketch, [(tip, plane)])

    d.recompute()
    logger.info("new_sketch: created %s", sketch.Name)
    return {"sketch": sketch.Name, "body": body_obj.Name, "plane": plane}


def sketch_rect(
    sketch: str,
    x: float,
    y: float,
    w: float,
    h: float,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a rectangle to the sketch."""
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    import Sketcher  # type: ignore[import-untyped]

    p1 = FreeCAD.Vector(x, y, 0)
    p2 = FreeCAD.Vector(x + w, y, 0)
    p3 = FreeCAD.Vector(x + w, y + h, 0)
    p4 = FreeCAD.Vector(x, y + h, 0)

    i0 = sk.addGeometry(Part.LineSegment(p1, p2))
    i1 = sk.addGeometry(Part.LineSegment(p2, p3))
    i2 = sk.addGeometry(Part.LineSegment(p3, p4))
    i3 = sk.addGeometry(Part.LineSegment(p4, p1))

    # Constrain corners to be coincident
    sk.addConstraint(Sketcher.Constraint("Coincident", i0, 2, i1, 1))
    sk.addConstraint(Sketcher.Constraint("Coincident", i1, 2, i2, 1))
    sk.addConstraint(Sketcher.Constraint("Coincident", i2, 2, i3, 1))
    sk.addConstraint(Sketcher.Constraint("Coincident", i3, 2, i0, 1))

    # Horizontal/vertical constraints
    sk.addConstraint(Sketcher.Constraint("Horizontal", i0))
    sk.addConstraint(Sketcher.Constraint("Horizontal", i2))
    sk.addConstraint(Sketcher.Constraint("Vertical", i1))
    sk.addConstraint(Sketcher.Constraint("Vertical", i3))

    d.recompute()
    return {"sketch": sk.Name, "geometry_indices": [i0, i1, i2, i3]}


def sketch_circle(
    sketch: str,
    cx: float,
    cy: float,
    r: float,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a circle to the sketch."""
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    idx = sk.addGeometry(Part.Circle(FreeCAD.Vector(cx, cy, 0), FreeCAD.Vector(0, 0, 1), r))
    d.recompute()
    return {"sketch": sk.Name, "geometry_index": idx}


def sketch_line(
    sketch: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a line segment to the sketch."""
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    idx = sk.addGeometry(
        Part.LineSegment(FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x2, y2, 0))
    )
    d.recompute()
    return {"sketch": sk.Name, "geometry_index": idx}


def sketch_arc(
    sketch: str,
    cx: float,
    cy: float,
    r: float,
    start_angle: float,
    end_angle: float,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add an arc to the sketch (angles in degrees)."""
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    idx = sk.addGeometry(
        Part.ArcOfCircle(
            Part.Circle(FreeCAD.Vector(cx, cy, 0), FreeCAD.Vector(0, 0, 1), r),
            math.radians(start_angle),
            math.radians(end_angle),
        )
    )
    d.recompute()
    return {"sketch": sk.Name, "geometry_index": idx}


def sketch_constrain(
    sketch: str,
    constraint_type: str,
    doc: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Add a constraint to the sketch.

    ``constraint_type`` is a Sketcher constraint name: Coincident, Horizontal,
    Vertical, Parallel, Perpendicular, Distance, DistanceX, DistanceY, Radius,
    Angle, Equal, Symmetric, Tangent, etc.

    Keyword args map to constraint constructor parameters, e.g.:
    - ``first``, ``first_pos``, ``second``, ``second_pos``: geometry indices
    - ``value``: dimension value
    """
    import Sketcher  # type: ignore[import-untyped]

    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    # Build constraint args in the order Sketcher expects
    cargs: list[Any] = [constraint_type]

    first = kwargs.get("first")
    first_pos = kwargs.get("first_pos")
    second = kwargs.get("second")
    second_pos = kwargs.get("second_pos")
    value = kwargs.get("value")

    if first is not None:
        cargs.append(int(first))
    if first_pos is not None:
        cargs.append(int(first_pos))
    if second is not None:
        cargs.append(int(second))
    if second_pos is not None:
        cargs.append(int(second_pos))
    if value is not None:
        cargs.append(float(value))

    idx = sk.addConstraint(Sketcher.Constraint(*cargs))
    d.recompute()
    return {"sketch": sk.Name, "constraint_index": idx}


def sketch_bspline(
    sketch: str,
    points: list[list[float]],
    degree: int = 3,
    periodic: bool = False,
    weights: list[float] | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a B-spline curve to the sketch from control points.

    ``points``: list of [x, y] control points.
    ``degree``: spline degree (default 3, cubic).
    ``periodic``: if True, create a periodic (closed) B-spline.
    ``weights``: optional list of weights (same length as points).
    """
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    if len(points) < degree + 1:
        raise ValueError(
            f"Need at least {degree + 1} control points for degree-{degree} B-spline, "
            f"got {len(points)}"
        )

    poles = [FreeCAD.Vector(p[0], p[1], 0) for p in points]
    n_poles = len(poles)

    if weights is None:
        w = [1.0] * n_poles
    else:
        if len(weights) != n_poles:
            raise ValueError(
                f"weights length ({len(weights)}) must match points length ({n_poles})"
            )
        w = list(weights)

    # Clamped uniform knot vector
    n_knots = n_poles + degree + 1
    if periodic:
        # Uniform knots for periodic
        knots_flat = [float(i) for i in range(n_knots)]
    else:
        # Clamped: degree+1 repeats at each end
        knots_flat = (
            [0.0] * (degree + 1)
            + [float(i) for i in range(1, n_poles - degree)]
            + [float(n_poles - degree)] * (degree + 1)
        )

    # Convert flat knot vector to (unique_knots, multiplicities)
    unique_knots: list[float] = []
    mults: list[int] = []
    for k in knots_flat:
        if unique_knots and abs(unique_knots[-1] - k) < 1e-10:
            mults[-1] += 1
        else:
            unique_knots.append(k)
            mults.append(1)

    bspline = Part.BSplineCurve()
    bspline.buildFromPolesMultsKnots(
        poles, mults, unique_knots, periodic, degree, w,
    )

    idx = sk.addGeometry(bspline)
    d.recompute()
    return {"sketch": sk.Name, "geometry_index": idx}


def sketch_populate(
    sketch: str,
    elements: list[dict[str, Any]] | None = None,
    constraints: list[dict[str, Any]] | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add geometry elements and constraints to a sketch in a single batch.

    This replaces individual ``sketch_line`` / ``sketch_circle`` /
    ``sketch_constrain`` calls with a single command that processes ALL
    elements and ALL constraints with **one** ``recompute()`` at the end.

    ``elements``: list of geometry dicts with a ``type`` field:
      - ``{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50}``
      - ``{"type": "circle", "cx": 0, "cy": 0, "r": 10}``
      - ``{"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}``
      - ``{"type": "arc", "cx": 0, "cy": 0, "r": 10, "start_angle": 0, "end_angle": 90}``
      - ``{"type": "spline", "points": [[x,y], ...], "degree": 3, ...}``

    ``constraints``: list of constraint dicts with a ``type`` field and
    parameters (first, first_pos, second, second_pos, value).

    Returns element count, constraint count, and geometry index mapping.
    """
    import Sketcher  # type: ignore[import-untyped]

    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    geometry_indices: list[dict[str, Any]] = []

    # --- Add all geometry elements without recompute ---
    for elem in (elements or []):
        elem_type = elem.get("type", "")
        is_construction = elem.get("construction", False)

        if elem_type == "rect":
            x = elem.get("x", 0)
            y = elem.get("y", 0)
            w = elem["w"]
            h = elem["h"]
            p1 = FreeCAD.Vector(x, y, 0)
            p2 = FreeCAD.Vector(x + w, y, 0)
            p3 = FreeCAD.Vector(x + w, y + h, 0)
            p4 = FreeCAD.Vector(x, y + h, 0)
            i0 = sk.addGeometry(Part.LineSegment(p1, p2))
            i1 = sk.addGeometry(Part.LineSegment(p2, p3))
            i2 = sk.addGeometry(Part.LineSegment(p3, p4))
            i3 = sk.addGeometry(Part.LineSegment(p4, p1))
            # Add rect constraints inline (no recompute yet)
            sk.addConstraint(Sketcher.Constraint("Coincident", i0, 2, i1, 1))
            sk.addConstraint(Sketcher.Constraint("Coincident", i1, 2, i2, 1))
            sk.addConstraint(Sketcher.Constraint("Coincident", i2, 2, i3, 1))
            sk.addConstraint(Sketcher.Constraint("Coincident", i3, 2, i0, 1))
            sk.addConstraint(Sketcher.Constraint("Horizontal", i0))
            sk.addConstraint(Sketcher.Constraint("Horizontal", i2))
            sk.addConstraint(Sketcher.Constraint("Vertical", i1))
            sk.addConstraint(Sketcher.Constraint("Vertical", i3))
            if is_construction:
                for ci in (i0, i1, i2, i3):
                    sk.toggleConstruction(ci)
            geometry_indices.append({"type": "rect", "indices": [i0, i1, i2, i3]})

        elif elem_type == "circle":
            center = elem.get("center")
            if center:
                cx, cy = center[0], center[1]
            else:
                cx = elem.get("cx", 0)
                cy = elem.get("cy", 0)
            r = elem["r"]
            idx = sk.addGeometry(
                Part.Circle(FreeCAD.Vector(cx, cy, 0), FreeCAD.Vector(0, 0, 1), r)
            )
            if is_construction:
                sk.toggleConstruction(idx)
            geometry_indices.append({"type": "circle", "index": idx})

        elif elem_type == "line":
            idx = sk.addGeometry(
                Part.LineSegment(
                    FreeCAD.Vector(elem["x1"], elem["y1"], 0),
                    FreeCAD.Vector(elem["x2"], elem["y2"], 0),
                )
            )
            if is_construction:
                sk.toggleConstruction(idx)
            geometry_indices.append({"type": "line", "index": idx})

        elif elem_type == "arc":
            center = elem.get("center")
            if center:
                cx, cy = center[0], center[1]
            else:
                cx = elem.get("cx", 0)
                cy = elem.get("cy", 0)
            r = elem["r"]
            sa = elem["start_angle"]
            ea = elem["end_angle"]
            if ea < sa:
                ea += 360.0
            idx = sk.addGeometry(
                Part.ArcOfCircle(
                    Part.Circle(FreeCAD.Vector(cx, cy, 0), FreeCAD.Vector(0, 0, 1), r),
                    math.radians(sa),
                    math.radians(ea),
                )
            )
            if is_construction:
                sk.toggleConstruction(idx)
            geometry_indices.append({"type": "arc", "index": idx})

        elif elem_type == "spline":
            points = elem["points"]
            degree = elem.get("degree", 3)
            periodic = elem.get("periodic", False)
            weights = elem.get("weights")

            if len(points) < degree + 1:
                raise ValueError(
                    f"Need at least {degree + 1} control points for degree-{degree} "
                    f"B-spline, got {len(points)}"
                )

            poles = [FreeCAD.Vector(p[0], p[1], 0) for p in points]
            n_poles = len(poles)
            w = weights if weights is not None else [1.0] * n_poles
            if len(w) != n_poles:
                raise ValueError(
                    f"weights length ({len(w)}) must match points length ({n_poles})"
                )

            n_knots = n_poles + degree + 1
            if periodic:
                knots_flat = [float(i) for i in range(n_knots)]
            else:
                knots_flat = (
                    [0.0] * (degree + 1)
                    + [float(i) for i in range(1, n_poles - degree)]
                    + [float(n_poles - degree)] * (degree + 1)
                )
            unique_knots: list[float] = []
            mults: list[int] = []
            for k in knots_flat:
                if unique_knots and abs(unique_knots[-1] - k) < 1e-10:
                    mults[-1] += 1
                else:
                    unique_knots.append(k)
                    mults.append(1)

            bspline = Part.BSplineCurve()
            bspline.buildFromPolesMultsKnots(poles, mults, unique_knots, periodic, degree, w)
            idx = sk.addGeometry(bspline)
            if is_construction:
                sk.toggleConstruction(idx)
            geometry_indices.append({"type": "spline", "index": idx})

        elif elem_type == "external_ref":
            feature = elem.get("feature")
            edge = elem.get("edge", "Edge1")
            if feature is None:
                raise ValueError("external_ref requires a 'feature' field")
            ref_str = f"{feature}.{edge}"
            idx = sk.addExternal(feature, edge)
            geometry_indices.append({"type": "external_ref", "index": idx, "ref": ref_str})

        elif elem_type == "sketch_fillet":
            vertex = elem.get("vertex")
            radius = elem.get("radius")
            if vertex is None or radius is None:
                raise ValueError("sketch_fillet requires 'vertex' (geometry index) and 'radius' fields")
            result_indices = sk.fillet(int(vertex), float(radius))
            idx = result_indices if isinstance(result_indices, int) else result_indices[0] if result_indices else -1
            geometry_indices.append({"type": "sketch_fillet", "index": idx, "vertex": vertex, "radius": radius})

        elif elem_type == "sketch_chamfer":
            vertex = elem.get("vertex")
            size = elem.get("size")
            if vertex is None or size is None:
                raise ValueError("sketch_chamfer requires 'vertex' (geometry index) and 'size' fields")
            result_indices = sk.chamfer(int(vertex), float(size))
            idx = result_indices if isinstance(result_indices, int) else result_indices[0] if result_indices else -1
            geometry_indices.append({"type": "sketch_chamfer", "index": idx, "vertex": vertex, "size": size})

        else:
            raise ValueError(f"Unknown element type: {elem_type}")

    # --- Add all constraints without recompute ---
    # Partial recovery: each constraint is attempted independently so a single
    # failure doesn't abort the whole sketch.  Failures are collected and
    # returned alongside the successful constraints.
    constraint_count = 0
    constraint_errors: list[dict[str, Any]] = []
    for ci, con in enumerate(constraints or []):
        con_type = con.get("type")
        if con_type is None:
            continue

        cargs: list[Any] = [con_type]
        first = con.get("first")
        first_pos = con.get("first_pos")
        second = con.get("second")
        second_pos = con.get("second_pos")
        value = con.get("value")

        if first is not None:
            cargs.append(int(first))
        if first_pos is not None:
            cargs.append(int(first_pos))
        if second is not None:
            cargs.append(int(second))
        if second_pos is not None:
            cargs.append(int(second_pos))
        if value is not None:
            cargs.append(float(value))

        try:
            sk.addConstraint(Sketcher.Constraint(*cargs))
            constraint_count += 1
        except Exception as exc:
            constraint_errors.append({
                "index": ci,
                "type": con_type,
                "error": str(exc),
            })
            logger.warning("Constraint %d (%s) failed: %s", ci, con_type, exc)

    # --- Single recompute for everything ---
    d.recompute()

    result: dict[str, Any] = {
        "sketch": sk.Name,
        "element_count": len(geometry_indices),
        "constraint_count": constraint_count,
        "geometry": geometry_indices,
    }
    if constraint_errors:
        result["constraint_errors"] = constraint_errors
    return result


def close_sketch(sketch: str, doc: str | None = None) -> dict[str, Any]:
    """Close/validate a sketch."""
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    d.recompute()

    fully_constrained = sk.FullyConstrained if hasattr(sk, "FullyConstrained") else None
    open_vertices = sk.OpenVertices if hasattr(sk, "OpenVertices") else None

    return {
        "sketch": sk.Name,
        "fully_constrained": fully_constrained,
        "open_vertices": len(open_vertices) if open_vertices else 0,
    }


# ---------------------------------------------------------------------------
# PartDesign Features
# ---------------------------------------------------------------------------

def _validate_profile_closure(sk: Any, operation: str) -> None:
    """Check that the sketch profile has closed wires before pad/pocket.

    Raises ``ValueError`` with actionable diagnostics if the profile is open.
    """
    shape = getattr(sk, "Shape", None)
    if shape is None:
        return

    wires = shape.Wires
    if not wires:
        raise ValueError(
            f"{operation} failed: sketch '{sk.Name}' has no wires. "
            "Ensure the sketch contains a closed profile."
        )

    open_vertices = getattr(sk, "OpenVertices", None)
    if open_vertices and len(open_vertices) > 0:
        positions = []
        for v in open_vertices[:5]:  # limit to first 5
            if hasattr(v, "Point"):
                p = v.Point
                positions.append(f"({p.x:.2f}, {p.y:.2f})")
            elif hasattr(v, "x"):
                positions.append(f"({v.x:.2f}, {v.y:.2f})")
        msg = (
            f"{operation} failed: sketch '{sk.Name}' has {len(open_vertices)} open "
            f"vertex(es) — the profile is not closed."
        )
        if positions:
            msg += f" Open vertices near: {', '.join(positions)}."
        msg += " Close all gaps in the profile before extruding."
        raise ValueError(msg)

    for i, wire in enumerate(wires):
        if not wire.isClosed():
            raise ValueError(
                f"{operation} failed: wire {i} in sketch '{sk.Name}' is not closed. "
                "Ensure all profile edges form a closed loop."
            )


def pad(
    sketch: str,
    length: float,
    symmetric: bool = False,
    reversed: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Extrude (pad) a sketch."""
    logger.info("pad: sketch=%s length=%s symmetric=%s reversed=%s", sketch, length, symmetric, reversed)
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)
    _validate_profile_closure(sk, "Pad")

    body = _find_parent_body(d, sk)
    pad_obj = d.addObject("PartDesign::Pad", "Pad")
    body.addObject(pad_obj)
    pad_obj.Profile = sk
    pad_obj.Length = length
    pad_obj.Midplane = symmetric
    pad_obj.Reversed = reversed

    op_context = {"op": "pad", "sketch": sketch, "length": length}
    result = _recompute_and_check(d, pad_obj, body=body, op_context=op_context)
    logger.info("pad: created %s", pad_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)
    return result


def _resolve_pocket_direction(
    doc: Any,
    sketch: Any,
    body: Any,
) -> dict[str, Any]:
    """Deterministically compute the ``reversed`` flag for a pocket.

    Algorithm:
    1. Extract the sketch plane normal ``N`` and origin ``O`` from the
       sketch's ``Placement``.
    2. Get the body's current tip shape centroid ``C``.
    3. Compute ``dot = (C - O) · N``.
    4. If ``dot > 0``: the solid is in the ``+N`` direction.  FreeCAD's
       default pocket direction is ``-N`` (``reversed=False``), so we
       need ``reversed=True`` to cut into the solid.
    5. If ``dot ≤ 0``: the solid is in the ``-N`` direction, which is the
       default pocket direction, so ``reversed=False``.

    Returns a dict with ``reversed``, ``confidence``, and diagnostic info.
    """
    # Sketch normal = local Z axis transformed by placement rotation
    normal = sketch.Placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    origin = sketch.Placement.Base

    # Get existing solid centroid
    try:
        tip = _get_tip(body)
        shape = tip.Shape
        if shape is None or shape.isNull():
            return {
                "reversed": False,
                "confidence": "low",
                "reason": "body has no solid shape yet",
            }
        centroid = shape.CenterOfMass
    except (ValueError, AttributeError):
        return {
            "reversed": False,
            "confidence": "low",
            "reason": "could not determine body centroid",
        }

    # Project body centroid onto sketch normal
    to_centroid = centroid.sub(origin)
    dot = to_centroid.dot(normal)

    # The solid is in the +N direction → need reversed=True to pocket into it
    reversed_flag = dot > 0

    return {
        "reversed": reversed_flag,
        "confidence": "high",
        "reason": (
            f"solid centroid is {'above' if dot > 0 else 'below'} sketch plane "
            f"(dot={dot:.2f}), pocket should go "
            f"{'into +normal (reversed=True)' if reversed_flag else 'into -normal (reversed=False)'}"
        ),
        "dot_product": round(dot, 4),
        "sketch_origin": _vec_to_list(origin),
        "sketch_normal": _vec_to_list(normal),
        "body_centroid": _vec_to_list(centroid),
    }


def resolve_pocket_direction(
    sketch: str,
    body: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Query command: resolve the correct pocket direction for a sketch.

    Returns ``{"reversed": true/false, "confidence": "high"/"low", "reason": "..."}``
    so the LLM (or planner) can use the right direction without guessing.
    """
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)
    body_obj = _find_parent_body(d, sk) if body is None else _resolve_body(d, body)
    return _resolve_pocket_direction(d, sk, body_obj)


def pocket(
    sketch: str,
    length: float = 0.0,
    pocket_type: str = "Dimension",
    reversed: bool | str = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Cut a pocket from a sketch.

    ``pocket_type``: "Dimension", "ThroughAll", "ToFirst", "ToLast".

    ``reversed``: ``True``/``False`` for explicit direction, or ``"auto"``
    to resolve deterministically from the sketch plane and body geometry.
    """
    logger.info("pocket: sketch=%s length=%s type=%s reversed=%s", sketch, length, pocket_type, reversed)
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)
    _validate_profile_closure(sk, "Pocket")

    body = _find_parent_body(d, sk)

    # Auto-resolve pocket direction
    auto_resolved = None
    if reversed == "auto":
        auto_resolved = _resolve_pocket_direction(d, sk, body)
        reversed = auto_resolved["reversed"]
        logger.info(
            "pocket: auto-resolved reversed=%s (%s)",
            reversed, auto_resolved["reason"],
        )

    pocket_obj = d.addObject("PartDesign::Pocket", "Pocket")
    body.addObject(pocket_obj)
    pocket_obj.Profile = sk
    pocket_obj.Type = _pocket_type_enum(pocket_type)
    if pocket_type == "Dimension":
        pocket_obj.Length = length
    pocket_obj.Reversed = bool(reversed)

    op_context = {"op": "pocket", "sketch": sketch, "length": length, "pocket_type": pocket_type}
    result = _recompute_and_check(d, pocket_obj, body=body, op_context=op_context)
    logger.info("pocket: created %s", pocket_obj.Name)
    if auto_resolved is not None:
        result["auto_reversed"] = auto_resolved
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)
    return result


def hole(
    face: str,
    diameter: float,
    depth: float,
    body: str | None = None,
    hole_type: str = "Dimension",
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a PartDesign Hole on a face.

    This creates a sketch with a point on the face, then a PartDesign::Hole
    feature.  ``hole_type``: "Dimension", "ThroughAll".
    """
    logger.info("hole: face=%s diameter=%s depth=%s type=%s", face, diameter, depth, hole_type)
    d = _get_doc(doc)

    body_obj = _resolve_body(d, body)

    # Create a sketch on the face
    sk = d.addObject("Sketcher::SketchObject", "HoleSketch")
    body_obj.addObject(sk)
    _set_sketch_support(sk, [(body_obj, face)])

    # Add a point at origin (center of face)
    sk.addGeometry(Part.Point(FreeCAD.Vector(0, 0, 0)))
    d.recompute()

    hole_obj = d.addObject("PartDesign::Hole", "Hole")
    body_obj.addObject(hole_obj)
    hole_obj.Profile = sk
    hole_obj.Diameter = diameter
    hole_obj.Depth = depth
    hole_obj.HoleType = 0  # Simple

    if hole_type == "ThroughAll":
        hole_obj.DepthType = 1  # Through all
    else:
        hole_obj.DepthType = 0  # Dimension

    op_context = {"op": "hole", "face": face, "diameter": diameter, "depth": depth}
    result = _recompute_and_check(d, hole_obj, body=body_obj, op_context=op_context)
    logger.info("hole: created %s", hole_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def fillet(
    edges: list[str] | None = None,
    radius: float = 1.0,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Fillet edges. ``edges`` are sub-element names like ["Edge1", "Edge3"].

    If ``selection`` is provided instead of ``edges``, the named selector is
    resolved first and its matched edge names are used.
    """
    logger.info("fillet: edges=%s radius=%s selection=%s", edges, radius, selection)
    if selection is not None:
        sel_result = resolve_selection(selection, body=body, doc=doc)
        if not sel_result["invariants_ok"]:
            raise ValueError(
                f"Selection '{selection}' invariant violations: {sel_result['violations']}"
            )
        edges = [e["name"] for e in sel_result["matched_edges"]]

    if not edges:
        raise ValueError("No edges specified — provide 'edges' list or 'selection' name")

    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    # Find the tip (last feature with a shape)
    tip = _get_tip(body_obj)

    fillet_obj = d.addObject("PartDesign::Fillet", "Fillet")
    body_obj.addObject(fillet_obj)
    fillet_obj.Base = (tip, edges)
    fillet_obj.Radius = radius

    op_context = {"op": "fillet", "radius": radius, "edge_count": len(edges)}
    result = _recompute_and_check(d, fillet_obj, body=body_obj, op_context=op_context)
    logger.info("fillet: created %s", fillet_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def revolution(
    sketch: str,
    axis: str = "V",
    angle: float = 360.0,
    symmetric: bool = False,
    reversed: bool = False,
    subtractive: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Revolve a sketch around an axis to create a solid of revolution.

    ``axis``: ``"V"`` (sketch vertical), ``"H"`` (sketch horizontal),
    ``"Base_X"``, ``"Base_Y"``, ``"Base_Z"`` (document origin axes).

    If ``subtractive`` is True, creates a PartDesign::Groove (cut) instead.
    """
    logger.info("revolution: sketch=%s axis=%s angle=%s symmetric=%s reversed=%s subtractive=%s", sketch, axis, angle, symmetric, reversed, subtractive)
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    body = _find_parent_body(d, sk)
    type_id = "PartDesign::Groove" if subtractive else "PartDesign::Revolution"
    feat_name = "Groove" if subtractive else "Revolution"
    rev_obj = d.addObject(type_id, feat_name)
    body.addObject(rev_obj)
    rev_obj.Profile = sk
    rev_obj.Angle = angle
    rev_obj.Midplane = symmetric
    rev_obj.Reversed = reversed

    # Map axis string to FreeCAD reference
    if axis in ("V", "H"):
        rev_obj.ReferenceAxis = (sk, [f"{axis}_Axis"])
    else:
        axis_map = {
            "Base_X": "X_Axis",
            "Base_Y": "Y_Axis",
            "Base_Z": "Z_Axis",
        }
        fc_axis = axis_map.get(axis)
        if fc_axis is None:
            raise ValueError(f"Invalid axis '{axis}', must be V, H, Base_X, Base_Y, or Base_Z")
        axis_obj = d.getObject(fc_axis)
        if axis_obj is None:
            raise ValueError(f"Document has no '{fc_axis}' object")
        rev_obj.ReferenceAxis = (axis_obj, [""])

    op_context = {"op": "revolution", "sketch": sketch, "axis": axis, "angle": angle, "subtractive": subtractive}
    result = _recompute_and_check(d, rev_obj, body=body, op_context=op_context)
    logger.info("revolution: created %s", rev_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)
    return result


def polar_pattern(
    features: list[str],
    axis: str = "Base_Z",
    occurrences: int = 6,
    angle: float = 360.0,
    reversed: bool = False,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a polar (circular) pattern of features around an axis.

    ``features``: list of feature names to pattern (e.g. ``["Pocket"]``).
    ``axis``: ``"Base_X"``, ``"Base_Y"``, ``"Base_Z"`` (document origin axes),
    or ``"V"``, ``"H"`` (sketch axes of the first feature's sketch).
    ``occurrences``: total number of copies including the original.
    """
    logger.info("polar_pattern: features=%s axis=%s occurrences=%s angle=%s", features, axis, occurrences, angle)
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    # Resolve feature objects
    originals = []
    for feat_name in features:
        feat = d.getObject(feat_name)
        if feat is None:
            raise ValueError(f"Feature '{feat_name}' not found")
        originals.append(feat)

    pattern_obj = d.addObject("PartDesign::PolarPattern", "PolarPattern")
    body_obj.addObject(pattern_obj)
    pattern_obj.Originals = originals
    pattern_obj.Occurrences = occurrences
    pattern_obj.Angle = angle
    pattern_obj.Reversed = reversed

    # Map axis string to FreeCAD reference
    if axis in ("V", "H"):
        # Use the sketch of the first original feature
        first_feat = originals[0]
        sk = getattr(first_feat, "Profile", None)
        if sk is None:
            raise ValueError("First feature has no Profile sketch for V/H axis reference")
        if isinstance(sk, (list, tuple)):
            sk = sk[0]
        pattern_obj.Axis = (sk, [f"{axis}_Axis"])
    else:
        axis_map = {
            "Base_X": ("X_Axis", "X_Axis"),
            "Base_Y": ("Y_Axis", "Y_Axis"),
            "Base_Z": ("Z_Axis", "Z_Axis"),
        }
        if axis not in axis_map:
            raise ValueError(f"Invalid axis '{axis}', must be V, H, Base_X, Base_Y, or Base_Z")
        # Reference the BODY's own Origin axis, not the document-scope
        # ``X_Axis``/``Y_Axis``/``Z_Axis`` (which is the *first* body's
        # axis).  Cross-body references trigger FreeCAD's "out of allowed
        # scope" warning AND silently leave ``body.Tip`` unchanged, so
        # the polar pattern's geometry never reaches the body's Shape —
        # only the source feature gets exported (one blade instead of
        # the patterned three).  Body-scoped axis references work
        # cleanly across any body, regardless of creation order.
        body_label, _doc_label = axis_map[axis]
        axis_obj = None
        origin = getattr(body_obj, "Origin", None)
        if origin is not None:
            for child in getattr(origin, "OutList", ()):
                if getattr(child, "Name", "").endswith(body_label):
                    axis_obj = child
                    break
        if axis_obj is None:
            # Fall back to document-scope lookup for documents without an
            # Origin (older FreeCAD versions that don't expose body.Origin).
            axis_obj = d.getObject(body_label)
        if axis_obj is None:
            raise ValueError(
                f"Body '{body_obj.Name}' has no '{body_label}' axis in its "
                "Origin and document has no fallback. Cannot apply polar pattern."
            )
        pattern_obj.Axis = (axis_obj, [""])

    op_context = {"op": "polar_pattern", "axis": axis, "occurrences": occurrences, "angle": angle}
    result = _recompute_and_check(d, pattern_obj, body=body_obj, op_context=op_context)
    # Promote the new pattern to the body's Tip so body.Shape (and downstream
    # STL exports) include the patterned copies. FreeCAD doesn't always
    # auto-promote when features are added via Python scripting, leaving
    # body.Tip pointing at the source feature — which silently exports a
    # single un-patterned instance.
    try:
        body_obj.Tip = pattern_obj
        d.recompute()
    except Exception as exc:
        logger.warning("polar_pattern: failed to promote Tip: %s", exc)
    logger.info("polar_pattern: created %s", pattern_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def mirror(
    features: list[str],
    plane: str = "Base_X",
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Mirror features across a symmetry plane.

    Creates a ``PartDesign::Mirrored`` feature.

    ``features``: list of feature names to mirror (e.g. ``["Pad"]``).
    ``plane``: ``"Base_X"``, ``"Base_Y"``, ``"Base_Z"`` (document origin planes),
    or ``"V"``, ``"H"`` (sketch axes of the first feature's sketch).
    """
    logger.info("mirror: features=%s plane=%s", features, plane)
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    originals = []
    for feat_name in features:
        feat = d.getObject(feat_name)
        if feat is None:
            raise ValueError(f"Feature '{feat_name}' not found")
        originals.append(feat)

    mirror_obj = d.addObject("PartDesign::Mirrored", "Mirrored")
    body_obj.addObject(mirror_obj)
    mirror_obj.Originals = originals

    if plane in ("V", "H"):
        first_feat = originals[0]
        sk = getattr(first_feat, "Profile", None)
        if sk is None:
            raise ValueError("First feature has no Profile sketch for V/H plane reference")
        if isinstance(sk, (list, tuple)):
            sk = sk[0]
        mirror_obj.MirrorPlane = (sk, [f"{plane}_Axis"])
    else:
        # FreeCAD PartDesign::Mirrored uses origin planes as mirror planes
        fc_plane_map = {
            "Base_X": "YZ_Plane",
            "Base_Y": "XZ_Plane",
            "Base_Z": "XY_Plane",
        }
        fc_plane = fc_plane_map.get(plane)
        if fc_plane is None:
            raise ValueError(f"Invalid plane '{plane}', must be V, H, Base_X, Base_Y, or Base_Z")
        plane_obj = d.getObject(fc_plane)
        if plane_obj is None:
            raise ValueError(f"Document has no '{fc_plane}' object")
        mirror_obj.MirrorPlane = (plane_obj, [""])

    op_context = {"op": "mirror", "plane": plane}
    result = _recompute_and_check(d, mirror_obj, body=body_obj, op_context=op_context)
    logger.info("mirror: created %s", mirror_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def linear_pattern(
    features: list[str],
    axis: str = "Base_X",
    length: float = 100.0,
    occurrences: int = 3,
    reversed: bool = False,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a linear pattern of features along an axis.

    Creates a ``PartDesign::LinearPattern`` feature.

    ``features``: list of feature names to pattern (e.g. ``["Pocket"]``).
    ``axis``: ``"Base_X"``, ``"Base_Y"``, ``"Base_Z"`` (document origin axes),
    or ``"V"``, ``"H"`` (sketch axes of the first feature's sketch).
    ``length``: total span of the pattern in mm.
    ``occurrences``: total number of copies including the original.
    """
    logger.info("linear_pattern: features=%s axis=%s length=%s occurrences=%s", features, axis, length, occurrences)
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    originals = []
    for feat_name in features:
        feat = d.getObject(feat_name)
        if feat is None:
            raise ValueError(f"Feature '{feat_name}' not found")
        originals.append(feat)

    pattern_obj = d.addObject("PartDesign::LinearPattern", "LinearPattern")
    body_obj.addObject(pattern_obj)
    pattern_obj.Originals = originals
    pattern_obj.Length = length
    pattern_obj.Occurrences = occurrences
    pattern_obj.Reversed = reversed

    if axis in ("V", "H"):
        first_feat = originals[0]
        sk = getattr(first_feat, "Profile", None)
        if sk is None:
            raise ValueError("First feature has no Profile sketch for V/H axis reference")
        if isinstance(sk, (list, tuple)):
            sk = sk[0]
        pattern_obj.Direction = (sk, [f"{axis}_Axis"])
    else:
        axis_map = {
            "Base_X": "X_Axis",
            "Base_Y": "Y_Axis",
            "Base_Z": "Z_Axis",
        }
        fc_axis = axis_map.get(axis)
        if fc_axis is None:
            raise ValueError(f"Invalid axis '{axis}', must be V, H, Base_X, Base_Y, or Base_Z")
        axis_obj = d.getObject(fc_axis)
        if axis_obj is None:
            raise ValueError(f"Document has no '{fc_axis}' object")
        pattern_obj.Direction = (axis_obj, [""])

    op_context = {"op": "linear_pattern", "axis": axis, "length": length, "occurrences": occurrences}
    result = _recompute_and_check(d, pattern_obj, body=body_obj, op_context=op_context)
    logger.info("linear_pattern: created %s", pattern_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def thickness(
    faces: list[str],
    thickness_value: float,
    join_type: str = "Arc",
    reversed: bool = False,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Shell/hollow out a solid by removing faces and adding wall thickness.

    Creates a ``PartDesign::Thickness`` feature.

    ``faces``: face references to remove/open (e.g. ``["Face6"]``).
    ``thickness_value``: wall thickness in mm.
    ``join_type``: ``"Arc"`` (default), ``"Tangent"``, or ``"Intersection"``.
    ``reversed``: reverse thickness direction (inward vs outward).
    """
    logger.info("thickness: faces=%s thickness=%s join=%s reversed=%s", faces, thickness_value, join_type, reversed)
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    tip = _get_tip(body_obj)

    # Base is (tip, [face_names]) — single tuple with list of face names
    thick_obj = d.addObject("PartDesign::Thickness", "Thickness")
    body_obj.addObject(thick_obj)
    thick_obj.Base = (tip, faces)
    thick_obj.Value = thickness_value
    thick_obj.Reversed = reversed

    join_map = {"Arc": 0, "Tangent": 1, "Intersection": 2}
    join_val = join_map.get(join_type, 0)
    thick_obj.Join = join_val

    op_context = {"op": "thickness", "faces": faces, "thickness": thickness_value}
    result = _recompute_and_check(d, thick_obj, body=body_obj, op_context=op_context)
    logger.info("thickness: created %s", thick_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def draft(
    faces: list[str],
    angle: float,
    neutral_plane: str = "Face1",
    reversed: bool = False,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add draft/taper to faces for injection molding or casting.

    Creates a ``PartDesign::Draft`` feature.

    ``faces``: face references to draft (e.g. ``["Face2", "Face4"]``).
    ``angle``: draft angle in degrees.
    ``neutral_plane``: the face that defines the stationary plane (e.g. ``"Face1"``).
    ``reversed``: reverse pull direction.
    """
    logger.info("draft: faces=%s angle=%s neutral_plane=%s reversed=%s", faces, angle, neutral_plane, reversed)
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    tip = _get_tip(body_obj)

    # Base is (tip, [face_names]) — single tuple with list of face names
    draft_obj = d.addObject("PartDesign::Draft", "Draft")
    body_obj.addObject(draft_obj)
    draft_obj.Base = (tip, faces)
    draft_obj.Angle = angle
    draft_obj.NeutralPlane = (tip, [neutral_plane])
    draft_obj.Reversed = reversed

    op_context = {"op": "draft", "faces": faces, "angle": angle, "neutral_plane": neutral_plane}
    result = _recompute_and_check(d, draft_obj, body=body_obj, op_context=op_context)
    logger.info("draft: created %s", draft_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


def sweep(
    profile_sketch: str,
    spine_sketch: str,
    subtractive: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Sweep a profile sketch along a spine sketch.

    Creates a ``PartDesign::AdditivePipe`` (or ``SubtractivePipe`` if
    ``subtractive`` is True).
    """
    logger.info("sweep: profile=%s spine=%s subtractive=%s", profile_sketch, spine_sketch, subtractive)
    d = _get_doc(doc)
    sk_profile = _get_sketch(d, profile_sketch)
    sk_spine = _get_sketch(d, spine_sketch)

    body = _find_parent_body(d, sk_profile)
    type_id = "PartDesign::SubtractivePipe" if subtractive else "PartDesign::AdditivePipe"
    pipe_obj = d.addObject(type_id, "Pipe")
    body.addObject(pipe_obj)
    pipe_obj.Profile = sk_profile
    # Use all edges in the spine sketch (not just Edge1) for multi-segment spines
    spine_edges = [f"Edge{i + 1}" for i in range(len(sk_spine.Shape.Edges))]
    pipe_obj.Spine = (sk_spine, spine_edges)

    op_context = {"op": "sweep", "subtractive": subtractive}
    result = _recompute_and_check(d, pipe_obj, body=body, op_context=op_context)
    logger.info("sweep: created %s", pipe_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)
    return result


def helix(
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
    """Create a helical sweep of a sketch profile.

    Creates a ``PartDesign::AdditiveHelix`` feature.

    ``mode``: ``"pitch-height"`` (default), ``"pitch-turns"``, or ``"height-turns"``.
    ``axis``: ``"V"`` (sketch vertical), ``"H"`` (sketch horizontal),
    ``"Base_X"``, ``"Base_Y"``, ``"Base_Z"`` (document origin axes).
    ``angle``: taper angle in degrees (0 = straight helix).
    ``growth``: radial growth per revolution in mm (0 = constant radius).
    """
    logger.info("helix: sketch=%s mode=%s pitch=%s height=%s turns=%s axis=%s", sketch, mode, pitch, height, turns, axis)
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    body = _find_parent_body(d, sk)
    helix_obj = d.addObject("PartDesign::AdditiveHelix", "Helix")
    body.addObject(helix_obj)
    helix_obj.Profile = sk

    # Mode mapping
    mode_map = {
        "pitch-height": 0,
        "pitch-turns": 1,
        "height-turns": 2,
    }
    mode_val = mode_map.get(mode)
    if mode_val is None:
        raise ValueError(f"Invalid mode '{mode}', must be pitch-height, pitch-turns, or height-turns")
    helix_obj.Mode = mode_val

    if mode == "pitch-height":
        helix_obj.Pitch = pitch
        helix_obj.Height = height
    elif mode == "pitch-turns":
        helix_obj.Pitch = pitch
        helix_obj.Turns = turns
    elif mode == "height-turns":
        helix_obj.Height = height
        helix_obj.Turns = turns

    helix_obj.Angle = angle
    helix_obj.Growth = growth
    helix_obj.LeftHanded = left_handed
    helix_obj.Reversed = reversed

    # Map axis string to FreeCAD reference
    if axis in ("V", "H"):
        helix_obj.ReferenceAxis = (sk, [f"{axis}_Axis"])
    else:
        axis_map = {
            "Base_X": "X_Axis",
            "Base_Y": "Y_Axis",
            "Base_Z": "Z_Axis",
        }
        fc_axis = axis_map.get(axis)
        if fc_axis is None:
            raise ValueError(f"Invalid axis '{axis}', must be V, H, Base_X, Base_Y, or Base_Z")
        axis_obj = d.getObject(fc_axis)
        if axis_obj is None:
            raise ValueError(f"Document has no '{fc_axis}' object")
        helix_obj.ReferenceAxis = (axis_obj, [""])

    op_context = {"op": "helix", "mode": mode}
    result = _recompute_and_check(d, helix_obj, body=body, op_context=op_context)
    logger.info("helix: created %s", helix_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)
    return result


def loft(
    sketches: list[str],
    ruled: bool = False,
    closed: bool = False,
    subtractive: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Loft between two or more sketch profiles.

    Creates a ``PartDesign::AdditiveLoft`` (or ``SubtractiveLoft`` if
    ``subtractive`` is True).

    ``sketches``: list of sketch names (at least 2). The first sketch is
    the profile; the rest are cross-sections.
    """
    logger.info("loft: sketches=%s ruled=%s closed=%s subtractive=%s", sketches, ruled, closed, subtractive)
    if len(sketches) < 2:
        raise ValueError("loft requires at least 2 sketches")

    d = _get_doc(doc)
    sk_objects = [_get_sketch(d, s) for s in sketches]

    body = _find_parent_body(d, sk_objects[0])
    type_id = "PartDesign::SubtractiveLoft" if subtractive else "PartDesign::AdditiveLoft"
    loft_obj = d.addObject(type_id, "Loft")
    body.addObject(loft_obj)
    loft_obj.Profile = sk_objects[0]
    loft_obj.Sections = sk_objects[1:]
    loft_obj.Ruled = ruled
    loft_obj.Closed = closed

    op_context = {"op": "loft", "sketch_count": len(sketches), "subtractive": subtractive}
    result = _recompute_and_check(d, loft_obj, body=body, op_context=op_context)
    logger.info("loft: created %s", loft_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)
    return result


def chamfer(
    edges: list[str] | None = None,
    size: float = 1.0,
    body: str | None = None,
    verify: bool = True,
    doc: str | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Chamfer edges. ``edges`` are sub-element names like ["Edge1", "Edge3"].

    If ``selection`` is provided instead of ``edges``, the named selector is
    resolved first and its matched edge names are used.
    """
    logger.info("chamfer: edges=%s size=%s selection=%s", edges, size, selection)
    if selection is not None:
        sel_result = resolve_selection(selection, body=body, doc=doc)
        if not sel_result["invariants_ok"]:
            raise ValueError(
                f"Selection '{selection}' invariant violations: {sel_result['violations']}"
            )
        edges = [e["name"] for e in sel_result["matched_edges"]]

    if not edges:
        raise ValueError("No edges specified — provide 'edges' list or 'selection' name")

    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)

    tip = _get_tip(body_obj)

    chamfer_obj = d.addObject("PartDesign::Chamfer", "Chamfer")
    body_obj.addObject(chamfer_obj)
    chamfer_obj.Base = (tip, edges)
    chamfer_obj.Size = size

    op_context = {"op": "chamfer", "size": size, "edge_count": len(edges)}
    result = _recompute_and_check(d, chamfer_obj, body=body_obj, op_context=op_context)
    logger.info("chamfer: created %s", chamfer_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body_obj)
    return result


# ---------------------------------------------------------------------------
# Query & Feedback
# ---------------------------------------------------------------------------

def get_selection() -> dict[str, Any]:
    """Return the current selection in the FreeCAD GUI."""
    if FreeCADGui is None:
        return {"selections": [], "error": "No GUI available (headless mode)"}

    selections: list[dict[str, Any]] = []
    for sel in FreeCADGui.Selection.getSelectionEx():
        obj_info: dict[str, Any] = {
            "object_name": sel.ObjectName,
            "object_label": sel.Object.Label if sel.Object else sel.ObjectName,
            "object_type": sel.Object.TypeId if sel.Object else "unknown",
            "sub_elements": [],
        }

        for i, sub_name in enumerate(sel.SubElementNames):
            sub_info: dict[str, Any] = {"name": sub_name}

            if sub_name.startswith("Face"):
                sub_info["type"] = "face"
                if i < len(sel.SubObjects):
                    face = sel.SubObjects[i]
                    if hasattr(face, "Surface") and hasattr(face.Surface, "Axis"):
                        sub_info["normal"] = _vec_to_list(face.Surface.Axis)
                    if hasattr(face, "CenterOfMass"):
                        sub_info["center"] = _vec_to_list(face.CenterOfMass)
                    if hasattr(face, "Area"):
                        sub_info["area"] = face.Area

            elif sub_name.startswith("Edge"):
                sub_info["type"] = "edge"
                if i < len(sel.SubObjects):
                    edge = sel.SubObjects[i]
                    if hasattr(edge, "Length"):
                        sub_info["length"] = edge.Length
                    verts = edge.Vertexes
                    if len(verts) >= 2:
                        sub_info["start"] = _vec_to_list(verts[0].Point)
                        sub_info["end"] = _vec_to_list(verts[1].Point)

            elif sub_name.startswith("Vertex"):
                sub_info["type"] = "vertex"
                if i < len(sel.SubObjects):
                    vertex = sel.SubObjects[i]
                    sub_info["position"] = _vec_to_list(vertex.Point)

            obj_info["sub_elements"].append(sub_info)

        selections.append(obj_info)

    return {"selections": selections}


def get_dimensions(object_name: str, doc: str | None = None) -> dict[str, Any]:
    """Get bounding box, volume, and surface area of an object."""
    d = _get_doc(doc)
    obj = d.getObject(object_name)
    if obj is None:
        raise ValueError(f"Object '{object_name}' not found")
    if not hasattr(obj, "Shape") or obj.Shape is None:
        raise ValueError(f"Object '{object_name}' has no shape")

    shape = obj.Shape
    bb = shape.BoundBox
    result: dict[str, Any] = {
        "object": object_name,
        "bounding_box": {
            "x_min": bb.XMin, "y_min": bb.YMin, "z_min": bb.ZMin,
            "x_max": bb.XMax, "y_max": bb.YMax, "z_max": bb.ZMax,
            "x_len": bb.XLength, "y_len": bb.YLength, "z_len": bb.ZLength,
        },
        "num_faces": len(shape.Faces),
        "num_edges": len(shape.Edges),
        "num_vertices": len(shape.Vertexes),
    }
    try:
        result["volume"] = shape.Volume
    except Exception:
        pass
    try:
        result["surface_area"] = shape.Area
    except Exception:
        pass
    return result


def get_body_topology(
    body: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Return all faces and edges on the body's tip shape with geometric properties.

    Accepts both ``PartDesign::Body`` objects (via ``_resolve_body`` +
    ``_get_tip``) and non-Body shape holders like ``Part::Feature``
    (used by the orchestrator's self-verifying validator after importing
    a worker's STEP file via ``import_step``). If body-resolution fails
    because the target has no ``Group`` attribute, fall through to
    direct shape access.
    """
    d = _get_doc(doc)
    try:
        body_obj = _resolve_body(d, body)
        tip = _get_tip(body_obj)
        shape = tip.Shape
    except (ValueError, AttributeError) as exc:
        if body is None:
            raise
        obj = d.getObject(body)
        if obj is None:
            raise ValueError(f"Object '{body}' not found in document '{d.Name}'") from exc
        if not hasattr(obj, "Shape") or obj.Shape is None:
            raise ValueError(f"Object '{body}' has no Shape attribute") from exc
        shape = obj.Shape

    faces: list[dict[str, Any]] = []
    for i, face in enumerate(shape.Faces):
        face_info: dict[str, Any] = {
            "name": f"Face{i + 1}",
            "area": face.Area,
        }
        surface = face.Surface
        face_info["surface_type"] = type(surface).__name__
        if hasattr(surface, "Axis"):
            face_info["normal"] = _vec_to_list(surface.Axis)
        if hasattr(face, "CenterOfMass"):
            face_info["center"] = _vec_to_list(face.CenterOfMass)
        bb = face.BoundBox
        face_info["bounds"] = {
            "x_len": bb.XLength, "y_len": bb.YLength, "z_len": bb.ZLength,
        }
        faces.append(face_info)

    edges: list[dict[str, Any]] = []
    for i, edge in enumerate(shape.Edges):
        edge_info: dict[str, Any] = {
            "name": f"Edge{i + 1}",
            "length": edge.Length,
        }
        curve = edge.Curve
        edge_info["curve_type"] = type(curve).__name__
        verts = edge.Vertexes
        if len(verts) >= 2:
            edge_info["start"] = _vec_to_list(verts[0].Point)
            edge_info["end"] = _vec_to_list(verts[1].Point)
        if len(verts) >= 1:
            mid_param = (edge.FirstParameter + edge.LastParameter) / 2
            try:
                edge_info["midpoint"] = _vec_to_list(edge.valueAt(mid_param))
            except Exception:
                pass
        if hasattr(curve, "Radius"):
            edge_info["radius"] = curve.Radius
        edges.append(edge_info)

    # Build face→edge adjacency
    for face_info, face in zip(faces, shape.Faces, strict=False):
        adjacent_edge_names: list[str] = []
        for face_edge in face.Edges:
            for j, body_edge in enumerate(shape.Edges):
                if face_edge.isSame(body_edge):
                    adjacent_edge_names.append(f"Edge{j + 1}")
                    break
        face_info["edge_names"] = adjacent_edge_names

    return {
        "body": body_obj.Name,
        "tip_feature": tip.Name,
        "num_faces": len(faces),
        "num_edges": len(edges),
        "faces": faces,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Named Selection Sets
# ---------------------------------------------------------------------------

_selection_sets: dict[str, dict[str, Any]] = {}


def define_selection(
    name: str,
    query: dict[str, Any],
    invariants: dict[str, Any] | None = None,
    body: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Define a named edge selection query with optional invariants.

    The query is stored and immediately resolved to validate it.
    On subsequent calls, the same name is updated (overwritten).
    """
    _selection_sets[name] = {
        "query": query,
        "invariants": invariants or {},
    }
    # Resolve immediately to validate and return results
    return resolve_selection(name, body=body, doc=doc)


def resolve_selection(
    name: str,
    body: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Re-resolve a named selector against current geometry and check invariants."""
    if name not in _selection_sets:
        raise ValueError(f"Selection set '{name}' not defined")

    entry = _selection_sets[name]
    query = entry["query"]
    invariants = entry["invariants"]

    # Build find_edges kwargs from query
    fe_kwargs: dict[str, Any] = {}
    if body is not None:
        fe_kwargs["body"] = body
    if doc is not None:
        fe_kwargs["doc"] = doc
    for key in ("axis", "curve_type", "min_length", "max_length", "on_face",
                "near_point", "near_distance", "convexity"):
        if key in query:
            fe_kwargs[key] = query[key]

    result = find_edges(**fe_kwargs)
    matched_edges = result["matched_edges"]

    # Check invariants
    violations: list[str] = []
    if "expected_count" in invariants:
        expected = invariants["expected_count"]
        actual = len(matched_edges)
        if actual != expected:
            violations.append(f"expected_count: expected {expected}, got {actual}")
    if "min_length" in invariants:
        for edge in matched_edges:
            if edge["length"] < invariants["min_length"]:
                violations.append(f"edge {edge['name']} length {edge['length']} < min_length {invariants['min_length']}")
    if "max_length" in invariants:
        for edge in matched_edges:
            if edge["length"] > invariants["max_length"]:
                violations.append(f"edge {edge['name']} length {edge['length']} > max_length {invariants['max_length']}")

    return {
        "name": name,
        "matched_edges": matched_edges,
        "num_matched": len(matched_edges),
        "invariants_ok": len(violations) == 0,
        "violations": violations,
    }


def list_selections() -> dict[str, Any]:
    """List all defined selection sets."""
    sets: list[dict[str, Any]] = []
    for name, entry in _selection_sets.items():
        sets.append({
            "name": name,
            "query": entry["query"],
            "invariants": entry["invariants"],
        })
    return {"selection_sets": sets, "count": len(sets)}


def delete_selection(name: str) -> dict[str, Any]:
    """Remove a named selection set."""
    if name not in _selection_sets:
        raise ValueError(f"Selection set '{name}' not defined")
    del _selection_sets[name]
    return {"deleted": name}


# ---------------------------------------------------------------------------
# Smart Edge Selection
# ---------------------------------------------------------------------------

_TOLERANCE = 1e-6


def _edge_convexity(shape: Any, edge: Any) -> str | None:
    """Determine if an edge is convex (outer corner) or concave (inner corner).

    Finds the two faces adjacent to the edge, computes the average outward
    normal at the edge midpoint, offsets a test point along that direction,
    and checks whether the point is inside the solid.  Inside → concave,
    outside → convex.

    Returns ``"convex"``, ``"concave"``, or ``None`` if undetermined.
    """
    # Find the two faces that share this edge
    adjacent_faces: list[Any] = []
    for face in shape.Faces:
        for fe in face.Edges:
            if fe.isSame(edge):
                adjacent_faces.append(face)
                break
        if len(adjacent_faces) == 2:
            break

    if len(adjacent_faces) != 2:
        return None

    # Get midpoint on the edge
    mid_param = (edge.FirstParameter + edge.LastParameter) / 2
    try:
        mid_point = edge.valueAt(mid_param)
    except Exception:
        return None

    # Get outward normals of both faces at the midpoint
    normals: list[Any] = []
    for face in adjacent_faces:
        try:
            u, v = face.Surface.parameter(mid_point)
            n = face.normalAt(u, v)
            normals.append(n)
        except Exception:
            return None

    # Average normal direction
    avg = normals[0].add(normals[1])
    if avg.Length < _TOLERANCE:
        return None
    avg.normalize()

    # Offset test point along average normal
    bb = shape.BoundBox
    offset = max(bb.XLength, bb.YLength, bb.ZLength) * 0.001
    test_point = mid_point.add(avg.multiply(offset))

    if shape.isInside(test_point, _TOLERANCE, False):
        return "concave"
    return "convex"


def find_edges(
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
    """Find edges matching geometric criteria on the body's tip shape.

    All filters are AND-composed: only edges satisfying every specified filter
    are returned.

    Filters:
    - ``axis``: ``"X"``, ``"Y"``, or ``"Z"`` — straight edges parallel to axis.
    - ``curve_type``: e.g. ``"Line"``, ``"Circle"`` — match ``type(edge.Curve).__name__``.
    - ``min_length`` / ``max_length``: edge length range.
    - ``on_face``: e.g. ``"Face3"`` — only edges bounding that face.
    - ``near_point`` / ``near_distance``: edges within distance of ``[x, y, z]``.
    - ``convexity``: ``"convex"`` (outer corner) or ``"concave"`` (inner corner).
    """
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)
    tip = _get_tip(body_obj)
    shape = tip.Shape

    # Pre-compute on_face edge set if needed
    face_edge_set: set[int] | None = None
    if on_face is not None:
        try:
            face_shape = shape.getElement(on_face)
        except Exception as exc:
            raise ValueError(f"Face '{on_face}' not found on shape") from exc
        face_edge_set = set()
        for fe in face_shape.Edges:
            for j, body_edge in enumerate(shape.Edges):
                if fe.isSame(body_edge):
                    face_edge_set.add(j)
                    break

    # Axis unit vectors
    axis_vectors = {
        "X": FreeCAD.Vector(1, 0, 0),
        "Y": FreeCAD.Vector(0, 1, 0),
        "Z": FreeCAD.Vector(0, 0, 1),
    }

    filters_applied: dict[str, Any] = {}
    if axis is not None:
        filters_applied["axis"] = axis
    if curve_type is not None:
        filters_applied["curve_type"] = curve_type
    if min_length is not None:
        filters_applied["min_length"] = min_length
    if max_length is not None:
        filters_applied["max_length"] = max_length
    if on_face is not None:
        filters_applied["on_face"] = on_face
    if near_point is not None:
        filters_applied["near_point"] = near_point
        filters_applied["near_distance"] = near_distance or 1.0
    if convexity is not None:
        filters_applied["convexity"] = convexity

    matched: list[dict[str, Any]] = []

    for i, edge in enumerate(shape.Edges):
        # --- curve_type filter ---
        if curve_type is not None:
            if type(edge.Curve).__name__ != curve_type:
                continue

        # --- axis filter (straight edges parallel to axis) ---
        if axis is not None:
            ax = axis.upper()
            if ax not in axis_vectors:
                raise ValueError(f"Invalid axis '{axis}', must be X, Y, or Z")
            if not hasattr(edge.Curve, "Direction"):
                continue  # Not a straight line
            direction = edge.Curve.Direction
            dot = abs(direction.dot(axis_vectors[ax]))
            if abs(dot - 1.0) > _TOLERANCE:
                continue

        # --- length filters ---
        if min_length is not None and edge.Length < min_length - _TOLERANCE:
            continue
        if max_length is not None and edge.Length > max_length + _TOLERANCE:
            continue

        # --- on_face filter ---
        if face_edge_set is not None and i not in face_edge_set:
            continue

        # --- near_point filter ---
        if near_point is not None:
            dist_threshold = near_distance if near_distance is not None else 1.0
            pt = FreeCAD.Vector(*near_point)
            mid_param = (edge.FirstParameter + edge.LastParameter) / 2
            try:
                mid = edge.valueAt(mid_param)
            except Exception:
                continue
            if pt.distanceToPoint(mid) > dist_threshold:
                continue

        # --- convexity filter ---
        if convexity is not None:
            ec = _edge_convexity(shape, edge)
            if ec != convexity:
                continue

        # Build result entry
        entry: dict[str, Any] = {
            "name": f"Edge{i + 1}",
            "length": round(edge.Length, 4),
        }
        verts = edge.Vertexes
        if len(verts) >= 2:
            entry["start"] = _vec_to_list(verts[0].Point)
            entry["end"] = _vec_to_list(verts[1].Point)
        matched.append(entry)

    return {
        "body": body_obj.Name,
        "filters_applied": filters_applied,
        "matched_edges": matched,
        "total_edges": len(shape.Edges),
        "num_matched": len(matched),
    }


# ---------------------------------------------------------------------------
# Screenshot & Camera
# ---------------------------------------------------------------------------

def _sb_vec3f(x: float, y: float, z: float) -> Any:
    """Create an SbVec3f, coercing all args to Python float.

    FreeCAD 1.0.2's pivy SWIG bindings can reject the 3-arg constructor
    even with Python floats.  We try three strategies:
    1. Array form  ``SbVec3f([fx, fy, fz])``  → ``float const [3]``
    2. Three-arg   ``SbVec3f(fx, fy, fz)``    → ``float, float, float``
    3. Default + setValue  (last resort)
    """
    from pivy.coin import SbVec3f  # type: ignore[import-untyped]
    fx, fy, fz = float(x), float(y), float(z)
    try:
        return SbVec3f([fx, fy, fz])
    except TypeError:
        pass
    try:
        return SbVec3f(fx, fy, fz)
    except TypeError:
        pass
    v = SbVec3f()
    v.setValue(fx, fy, fz)
    return v


_PRESET_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "iso": (1.0, 1.0, 1.0),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "right": (1.0, 0.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
}


def _capture_image(
    doc: Any,
    direction: tuple[float, float, float] = (1.0, 1.0, 1.0),
    up: tuple[float, float, float] = (0.0, 0.0, 1.0),
    distance_mult: float = 2.0,
    target_point: tuple[float, float, float] | None = None,
    near_clip: float | None = None,
    width: int = 512,
    height: int = 512,
) -> dict[str, Any]:
    """Position the camera and capture a screenshot, returning base64 PNG data.

    ``direction``: the direction FROM which the camera looks at the target.
    ``target_point``: where the camera looks (default: model center).
    ``distance_mult``: multiplier on bounding-box diagonal for camera distance.
    """
    if FreeCADGui is None:
        raise RuntimeError("No GUI available (headless mode) — cannot capture screenshots")

    _ensure_gui_doc(doc)
    view = FreeCADGui.ActiveDocument.ActiveView

    # Compute model center and bbox diagonal
    bbox = _model_bounding_box(doc)
    if target_point is None:
        cx = float((bbox.XMin + bbox.XMax) / 2)
        cy = float((bbox.YMin + bbox.YMax) / 2)
        cz = float((bbox.ZMin + bbox.ZMax) / 2)
        if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(cz)):
            cx, cy, cz = 0.0, 0.0, 0.0
        center = (cx, cy, cz)
    else:
        center = target_point

    diagonal = bbox.DiagonalLength if bbox.DiagonalLength > 0 else 100.0
    # Guard against Inf/NaN from degenerate bounding boxes
    if not math.isfinite(diagonal) or diagonal > 1e10:
        diagonal = 100.0
    cam_dist = diagonal * distance_mult

    # Normalize direction
    dx, dy, dz = direction
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length < 1e-10:
        dx, dy, dz = 1.0, 1.0, 1.0
        length = math.sqrt(3.0)
    dx, dy, dz = dx / length, dy / length, dz / length

    cam_pos = (
        center[0] + dx * cam_dist,
        center[1] + dy * cam_dist,
        center[2] + dz * cam_dist,
    )
    logger.debug("capture_image: direction=%s target=%s distance_mult=%s", direction, center, distance_mult)
    logger.debug("capture_image: cam_pos=%s", cam_pos)

    # Set camera via Coin3D
    try:
        cam = view.getCameraNode()
        cam.position.setValue(_sb_vec3f(cam_pos[0], cam_pos[1], cam_pos[2]))
        cam.pointAt(_sb_vec3f(center[0], center[1], center[2]), _sb_vec3f(up[0], up[1], up[2]))
        if near_clip is not None:
            cam.nearDistance.setValue(float(near_clip))
    except ImportError:
        # pivy not available — fall back to ViewFit
        FreeCADGui.SendMsgToActiveView("ViewFit")

    # Auto-fit: adjust zoom to frame the model while keeping the viewing direction
    FreeCADGui.SendMsgToActiveView("ViewFit")
    _process_qt_events()

    # Re-read actual camera position after ViewFit so returned values are accurate
    try:
        cam = view.getCameraNode()
        pos = cam.position.getValue()
        cam_pos = (pos[0], pos[1], pos[2])
    except Exception:
        pass  # keep original cam_pos if we can't read back

    # Capture to temp file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    view.saveImage(tmp_path, width, height)

    # Read and base64-encode
    with open(tmp_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("ascii")
    logger.debug("capture_image: saved %d bytes", len(image_data))

    # Clean up
    try:
        Path(tmp_path).unlink()
    except OSError:
        pass

    return {
        "image_base64": image_data,
        "mime_type": "image/png",
        "camera_position": list(cam_pos),
        "camera_target": list(center),
    }


def _capture_verification_views(
    doc: Any,
    width: int = 512,
    height: int = 512,
    op_context: dict[str, Any] | None = None,
    body: Any | None = None,
) -> list[dict[str, Any]]:
    """Capture 2 verification views: iso overview + targeted operation view."""
    views: list[dict[str, Any]] = []

    # View 1: Always iso for overall context
    try:
        img = _capture_image(doc, direction=_PRESET_DIRECTIONS["iso"], width=width, height=height)
        img["view"] = "iso"
        views.append(img)
    except Exception as e:
        logger.warning("Failed to capture iso view: %s", e)

    # View 2: Targeted at the operation area
    view_label, direction = _compute_targeted_view(op_context, doc, body=body)
    try:
        img = _capture_image(doc, direction=direction, width=width, height=height)
        img["view"] = view_label
        views.append(img)
    except Exception as e:
        logger.warning("Failed to capture %s view: %s", view_label, e)

    return views


def _model_bounding_box(doc: Any) -> Any:
    """Get the combined bounding box of all solid shapes in the document.

    Skips infinite-extent helper objects (Origin axes/planes) whose
    bounding-box diagonal exceeds a sanity threshold.
    """
    _MAX_DIAG = 1e10  # anything larger is an axis/plane/infinite helper
    combined = FreeCAD.BoundBox()
    for obj in doc.Objects:
        if not hasattr(obj, "Shape") or obj.Shape is None or obj.Shape.isNull():
            continue
        bb = obj.Shape.BoundBox
        if bb.DiagonalLength > _MAX_DIAG or bb.XMin > bb.XMax:
            continue
        combined.add(bb)
    return combined


def _resolve_target(
    target: str | list[float],
    doc: Any,
    body: Any | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float] | None]:
    """Resolve a screenshot target to (look_at_point, direction_or_None).

    Returns:
        (target_point, direction) where direction is None if not determined
        by the target itself (caller should use a default).
    """
    # Preset view name
    if isinstance(target, str) and target.lower() in _PRESET_DIRECTIONS:
        direction = _PRESET_DIRECTIONS[target.lower()]
        return (
            _bbox_center(_model_bounding_box(doc)),
            direction,
        )

    # Explicit [x, y, z] point
    if isinstance(target, list) and len(target) == 3:
        return (tuple(target), None)  # type: ignore[return-value]

    # Face reference like "Face3"
    if isinstance(target, str) and target.startswith("Face"):
        if body is None:
            body_obj = _resolve_body(doc, None)
        else:
            body_obj = body
        tip = _get_tip(body_obj)
        shape = tip.Shape
        try:
            face = shape.getElement(target)
        except Exception as exc:
            raise ValueError(f"Face '{target}' not found on shape") from exc
        center = _vec_to_list(face.CenterOfMass)
        normal = None
        if hasattr(face, "Surface") and hasattr(face.Surface, "Axis"):
            normal = _vec_to_list(face.Surface.Axis)
        return (
            tuple(center),  # type: ignore[arg-type]
            tuple(normal) if normal else None,  # type: ignore[arg-type]
        )

    # Feature name like "Pocket001"
    if isinstance(target, str):
        obj = doc.getObject(target)
        if obj is not None and hasattr(obj, "Shape") and obj.Shape is not None:
            bb = obj.Shape.BoundBox
            return (_bbox_center(bb), _PRESET_DIRECTIONS["iso"])
        raise ValueError(f"Feature '{target}' not found or has no shape")

    raise ValueError(f"Invalid target: {target}")


def _bbox_center(bb: Any) -> tuple[float, float, float]:
    return (
        float((bb.XMin + bb.XMax) / 2),
        float((bb.YMin + bb.YMax) / 2),
        float((bb.ZMin + bb.ZMax) / 2),
    )


def screenshot(
    target: str | list[float] = "iso",
    distance: float = 2.0,
    direction: list[float] | None = None,
    up: list[float] | None = None,
    near_clip: float | None = None,
    width: int = 512,
    height: int = 512,
    doc: str | None = None,
    hide_bodies: list[str] | None = None,
) -> dict[str, Any]:
    """Smart screenshot: compute view from target, capture image."""
    d = _get_doc(doc)
    _ensure_gui_doc(d)
    up_vec = tuple(up) if up else (0.0, 0.0, 1.0)

    target_point, resolved_dir = _resolve_target(target, d)

    if direction is not None:
        cam_dir = tuple(direction)
    elif resolved_dir is not None:
        cam_dir = resolved_dir
    else:
        cam_dir = _PRESET_DIRECTIONS["iso"]

    # Temporarily hide specified bodies for the capture
    restored: list[tuple[str, bool]] = []
    if hide_bodies and FreeCADGui is not None:
        for name in hide_bodies:
            gui_obj = FreeCADGui.ActiveDocument.getObject(name)
            if gui_obj is not None and gui_obj.Visibility:
                gui_obj.Visibility = False
                restored.append((name, True))
        _process_qt_events()

    try:
        img = _capture_image(
            d,
            direction=cam_dir,  # type: ignore[arg-type]
            up=up_vec,  # type: ignore[arg-type]
            distance_mult=distance,
            target_point=target_point,  # type: ignore[arg-type]
            near_clip=near_clip,
            width=width,
            height=height,
        )
    finally:
        for name, was_visible in restored:
            gui_obj = FreeCADGui.ActiveDocument.getObject(name)
            if gui_obj is not None:
                gui_obj.Visibility = was_visible
        if restored:
            _process_qt_events()

    result = {
        "ok": True,
        "width": width,
        "height": height,
        **img,
    }
    if restored:
        result["hidden_for_capture"] = [name for name, _ in restored]
    return result


def set_camera(
    position: list[float] | None = None,
    target: list[float] | None = None,
    up: list[float] | None = None,
    near_clip: float | None = None,
    fit_all: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
    """Low-level camera control via Coin3D."""
    if FreeCADGui is None:
        raise RuntimeError("No GUI available (headless mode)")

    d = _get_doc(doc)
    _ensure_gui_doc(d)
    view = FreeCADGui.ActiveDocument.ActiveView

    if fit_all:
        FreeCADGui.SendMsgToActiveView("ViewFit")
        _process_qt_events()

    try:
        cam = view.getCameraNode()

        if position is not None:
            cam.position.setValue(_sb_vec3f(position[0], position[1], position[2]))

        if target is not None:
            up_vec = up if up else [0.0, 0.0, 1.0]
            cam.pointAt(
                _sb_vec3f(target[0], target[1], target[2]),
                _sb_vec3f(up_vec[0], up_vec[1], up_vec[2]),
            )

        if near_clip is not None:
            cam.nearDistance.setValue(float(near_clip))

        # Read back camera state
        pos = cam.position.getValue()
        result_pos = [pos[0], pos[1], pos[2]]
    except ImportError:
        result_pos = position or [0, 0, 0]

    return {
        "camera_set": True,
        "position": result_pos,
    }


def get_camera(doc: str | None = None) -> dict[str, Any]:
    """Read current camera state."""
    if FreeCADGui is None:
        raise RuntimeError("No GUI available (headless mode)")

    d = _get_doc(doc)  # validate doc exists
    _ensure_gui_doc(d)
    view = FreeCADGui.ActiveDocument.ActiveView

    try:
        cam = view.getCameraNode()
        pos = cam.position.getValue()
        near_d = cam.nearDistance.getValue()
        far_d = cam.farDistance.getValue()

        return {
            "position": [pos[0], pos[1], pos[2]],
            "near_clip": near_d,
            "far_clip": far_d,
        }
    except Exception as e:
        raise RuntimeError(f"Failed to read camera: {e}") from e


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export(
    doc: str | None = None,
    format: str = "step",
    path: str | None = None,
) -> dict[str, Any]:
    """Export the document to STEP, STL, or FCStd."""
    d = _get_doc(doc)
    fmt = format.lower()

    if path is None:
        suffix = {"step": ".step", "stl": ".stl", "fcstd": ".FCStd"}.get(fmt, ".step")
        path = str(Path(tempfile.gettempdir()) / f"{d.Name}{suffix}")

    if fmt == "fcstd":
        d.saveAs(path)
    elif fmt in ("step", "stl"):
        # Collect all visible shapes
        shapes = []
        for obj in d.Objects:
            if hasattr(obj, "Shape") and obj.Shape is not None:
                if not obj.Shape.isNull():
                    shapes.append(obj.Shape)
        if not shapes:
            raise ValueError("No shapes to export")

        if fmt == "step":
            compound = Part.makeCompound(shapes) if len(shapes) > 1 else shapes[0]
            compound.exportStep(path)
        else:
            compound = Part.makeCompound(shapes) if len(shapes) > 1 else shapes[0]
            compound.exportStl(path)
    else:
        raise ValueError(f"Unsupported format: {format}")

    return {"path": path, "format": fmt}


def export_sim_package(
    bodies: list[str] | None = None,
    format: str = "stl",
    output_dir: str | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Export all (or specified) bodies as individual meshes with placements.

    Returns a manifest with per-body mesh_path + placement (position + quaternion),
    ready for sim description generation (URDF/SDF/USD).
    """
    d = _get_doc(doc)
    fmt = format.lower()
    suffix = {"step": ".step", "stl": ".stl", "obj": ".obj"}.get(fmt, ".stl")

    # Resolve output directory
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="sim_pkg_")
    else:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Find bodies to export
    if bodies is not None:
        body_objs = []
        for name in bodies:
            obj = d.getObject(name)
            if obj is None:
                raise ValueError(f"Body '{name}' not found in document '{d.Name}'")
            body_objs.append(obj)
    else:
        body_objs = [
            obj for obj in d.Objects
            if obj.TypeId == "PartDesign::Body"
        ]

    if not body_objs:
        raise ValueError("No PartDesign::Body objects found in document")

    manifest: list[dict[str, Any]] = []
    for body_obj in body_objs:
        shape = body_obj.Shape
        if shape is None or shape.isNull():
            logger.warning("Skipping body '%s' — no valid shape", body_obj.Name)
            continue

        # Export mesh in world coordinates (including Body Placement).
        # The URDF generator (build_sim_model) will transform meshes
        # to link-local coordinates after computing each link's world
        # position from the kinematic chain.  This approach is robust
        # regardless of whether the body uses Placement-based positioning
        # or has geometry at world coords in its internal features.
        mesh_path = str(Path(output_dir) / f"{body_obj.Name}{suffix}")
        if fmt == "step":
            shape.exportStep(mesh_path)
        elif fmt == "stl":
            shape.exportStl(mesh_path)
        elif fmt == "obj":
            import Mesh  # type: ignore[import-untyped]
            Mesh.export([body_obj], mesh_path)
        else:
            raise ValueError(f"Unsupported format: {format}")

        # Extract placement as position + quaternion
        plc = body_obj.Placement
        pos = plc.Base
        quat = plc.Rotation.Q  # (x, y, z, w) in FreeCAD

        # Bounding box and volume for auto-inertia computation
        bb = shape.BoundBox
        bbox_mm = [bb.XLength, bb.YLength, bb.ZLength]
        bbox_min_mm = [bb.XMin, bb.YMin, bb.ZMin]
        volume_mm3 = shape.Volume

        manifest.append({
            "name": body_obj.Name,
            "label": body_obj.Label,
            "mesh_path": mesh_path,
            "placement": {
                "position": [pos.x, pos.y, pos.z],
                "rotation_quat": [quat[3], quat[0], quat[1], quat[2]],  # w,x,y,z
            },
            "bbox_mm": bbox_mm,
            "bbox_min_mm": bbox_min_mm,
            "volume_mm3": volume_mm3,
        })

    return {
        "output_dir": output_dir,
        "format": fmt,
        "body_count": len(manifest),
        "bodies": manifest,
    }


def export_body(
    body: str,
    format: str = "stl",
    path: str | None = None,
    doc: str | None = None,
    strip_placement: bool = False,
) -> dict[str, Any]:
    """Export a single PartDesign body to STL, STEP, or OBJ.

    Required for per-body mesh export (Tier 2 assembly + Tier 3 Chrono).
    Each rigid body in a mechanism needs its own mesh file.

    When ``strip_placement`` is True the Body's Placement is temporarily
    zeroed before export so the mesh vertices are in body-local coordinates
    (matching ``export_sim_package`` behavior).  Default False preserves
    backward compat — standalone exports keep world coordinates.
    """
    d = _get_doc(doc)
    body_obj = d.getObject(body)
    if body_obj is None:
        raise ValueError(f"Body '{body}' not found in document '{d.Name}'")

    shape = body_obj.Shape
    if shape is None or shape.isNull():
        raise ValueError(f"Body '{body}' has no valid shape")

    fmt = format.lower()
    if path is None:
        suffix = {"step": ".step", "stl": ".stl", "obj": ".obj"}.get(fmt, ".stl")
        path = str(Path(tempfile.gettempdir()) / f"{body}{suffix}")

    saved_plc = None
    if strip_placement:
        saved_plc = body_obj.Placement
        body_obj.Placement = FreeCAD.Placement()  # type: ignore[name-defined]
        shape = body_obj.Shape  # re-evaluate with zeroed placement

    try:
        if fmt == "step":
            shape.exportStep(path)
        elif fmt == "stl":
            shape.exportStl(path)
        elif fmt == "obj":
            import Mesh  # type: ignore[import-untyped]
            Mesh.export([body_obj], path)
        else:
            raise ValueError(f"Unsupported format: {format}")
    finally:
        if saved_plc is not None:
            body_obj.Placement = saved_plc

    return {"path": path, "format": fmt, "body": body}


def import_step(
    path: str,
    doc: str | None = None,
    object_name: str = "ImportedStep",
) -> dict[str, Any]:
    """Import a STEP file as a Part::Feature in a fresh or named document.

    Used by the orchestrator's self-verifying validator
    (``orchestrator/measure.py``) to re-measure worker outputs
    independently of any metadata.json the worker wrote. The returned
    volume and bounding box come from the on-disk STEP file, not from
    any live session state.

    If ``doc`` is None, a fresh document named ``step_import`` is
    created. Otherwise the named document is reused. The imported
    feature is added with ``object_name`` (default ``ImportedStep``);
    the caller can look it up by name via ``d.getObject(object_name)``
    for subsequent measurement via ``measure_between`` or
    ``get_dimensions``.
    """
    import Part  # type: ignore[import-untyped]

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"STEP file not found: {path}")

    if doc is None:
        d = FreeCAD.newDocument("step_import")
    else:
        d = _get_doc(doc)

    shape = Part.Shape()
    shape.read(str(p))
    if shape.isNull():
        raise ValueError(f"STEP file '{path}' produced a null shape")

    obj = d.addObject("Part::Feature", object_name)
    obj.Shape = shape
    d.recompute()

    bb = shape.BoundBox
    return {
        "doc": d.Name,
        "object": obj.Name,
        "volume_mm3": float(shape.Volume),
        "bbox_mm": [
            float(bb.XLength),
            float(bb.YLength),
            float(bb.ZLength),
        ],
        "bbox_min_mm": [float(bb.XMin), float(bb.YMin), float(bb.ZMin)],
        "bbox_max_mm": [float(bb.XMax), float(bb.YMax), float(bb.ZMax)],
        "num_faces": len(shape.Faces),
        "num_edges": len(shape.Edges),
    }


# ---------------------------------------------------------------------------
# Assembly (Tier 2 kinematic validation)
# ---------------------------------------------------------------------------

# FreeCAD 1.0+ joint type indices (Assembly.JointObject.Joint constructor).
#
# Verified against FreeCAD 1.1.0 ``JointObject.JointTypes``:
#   ['Fixed', 'Revolute', 'Cylindrical', 'Slider', 'Ball', 'Distance',
#    'Parallel', 'Perpendicular', 'Angle', 'RackPinion', 'Screw',
#    'Gears', 'Belt']
# The order matches exactly — no version-specific branching needed.
# Internal names differ for clarity (prismatic↔Slider, gear_mesh↔Gears,
# belt_chain↔Belt, rack_pinion↔RackPinion).
_JOINT_TYPE_INDEX = {
    "fixed": 0,
    "revolute": 1,
    "cylindrical": 2,
    "prismatic": 3,
    "ball": 4,
    "distance": 5,
    "parallel": 6,
    "perpendicular": 7,
    "angle": 8,
    "rack_pinion": 9,
    "screw": 10,
    "gear_mesh": 11,
    "belt_chain": 12,
}


def assembly_create(
    name: str = "Assembly",
    doc: str | None = None,
) -> dict[str, Any]:
    """Create an Assembly container in the document.

    Uses the Assembly workbench (FreeCAD 1.0+).
    """
    from freecad_addon.compat import require_v1_plus
    require_v1_plus("Assembly creation")

    d = _get_doc(doc)

    # Create an Assembly4 or built-in Assembly container
    assembly = d.addObject("Assembly::AssemblyObject", name)
    d.recompute()

    return {
        "name": assembly.Name,
        "label": assembly.Label,
        "doc": d.Name,
    }


def assembly_add_part(
    assembly: str,
    body: str,
    placement: list[float] | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Add a body to an assembly with optional placement.

    placement: [x, y, z, roll_deg, pitch_deg, yaw_deg] — position + Euler angles.
    """
    from freecad_addon.compat import require_v1_plus
    require_v1_plus("Assembly add_part")

    d = _get_doc(doc)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")
    body_obj = d.getObject(body)
    if body_obj is None:
        available_bodies = [o.Name for o in d.Objects if o.TypeId == "PartDesign::Body"]
        if not available_bodies:
            raise ValueError(
                f"Body '{body}' not found in doc '{d.Name}', and no PartDesign "
                "bodies exist there at all. Bodies must be created (via "
                "cad.new_body / cad.create_primitive / cad.import_step) before "
                "they can be added to an assembly. If your mechanism was "
                "defined before any geometry was built, build the parts first "
                "or call motion_create_assembly with the doc that contains "
                "the bodies."
            )
        raise ValueError(
            f"Body '{body}' not found in doc '{d.Name}'. "
            f"Available bodies: {available_bodies}"
        )

    # Link the body into the assembly
    link = asm_obj.newObject("App::Link", f"{body}_link")
    link.setLink(body_obj)

    if placement is not None:
        if len(placement) >= 6:
            link.Placement = FreeCAD.Placement(
                FreeCAD.Vector(placement[0], placement[1], placement[2]),
                FreeCAD.Rotation(placement[3], placement[4], placement[5]),
            )
        elif len(placement) >= 3:
            link.Placement = FreeCAD.Placement(
                FreeCAD.Vector(placement[0], placement[1], placement[2]),
                FreeCAD.Rotation(),
            )

    d.recompute()

    return {
        "link_name": link.Name,
        "body": body,
        "assembly": assembly,
    }


def _resolve_joint_element(
    link_obj: Any,
    joint_type: str,
    *,
    origin: list[float] | None = None,
    axis: list[float] | None = None,
) -> str:
    """Auto-resolve the best sub-element reference for a joint type.

    For revolute/cylindrical/gear_mesh joints, finds a cylindrical face whose
    axis aligns with *axis* (defaults to Z).  When no cylindrical face exists
    (e.g. involute gear profiles with only BSpline surfaces), falls back to
    circular edges whose normal aligns with *axis*.

    *origin* is used to rank candidates by proximity — the closest match wins.
    Falls back to ``"Face1"`` when nothing suitable is found.
    """
    import math

    _CYLINDRICAL_TYPES = {"revolute", "cylindrical", "gear_mesh"}

    if joint_type not in _CYLINDRICAL_TYPES:
        return "Face1"

    try:
        # link_obj is an App::Link — follow to the real body shape
        shape = link_obj.LinkedObject.Shape
    except Exception:
        return "Face1"

    # Default axis/origin when not provided
    ax = axis or [0.0, 0.0, 1.0]
    org = origin or [0.0, 0.0, 0.0]

    def _dot(a: Any, b: list[float]) -> float:
        """Dot product between a FreeCAD Vector and a list."""
        return a.x * b[0] + a.y * b[1] + a.z * b[2]

    def _dist_to_origin(pt: Any) -> float:
        """Euclidean distance from pt to org."""
        return math.sqrt(
            (pt.x - org[0]) ** 2 + (pt.y - org[1]) ** 2 + (pt.z - org[2]) ** 2
        )

    # --- Pass 1: cylindrical faces aligned with axis ---
    cyl_candidates: list[tuple[float, str]] = []
    for i, face in enumerate(shape.Faces, start=1):
        surface = face.Surface
        if surface.__class__.__name__ == "Cylinder":
            try:
                face_axis = surface.Axis
                alignment = abs(_dot(face_axis, ax))
                if alignment > 0.9:
                    dist = _dist_to_origin(surface.Center)
                    cyl_candidates.append((dist, f"Face{i}"))
            except Exception:
                pass

    if cyl_candidates:
        cyl_candidates.sort()  # closest to origin first
        return cyl_candidates[0][1]

    # --- Pass 2: circular edge fallback (gear bodies with no cylinders) ---
    edge_candidates: list[tuple[float, str]] = []
    for i, edge in enumerate(shape.Edges, start=1):
        curve = edge.Curve
        if curve.__class__.__name__ == "Circle":
            try:
                edge_axis = curve.Axis
                alignment = abs(_dot(edge_axis, ax))
                if alignment > 0.9:
                    dist = _dist_to_origin(curve.Center)
                    edge_candidates.append((dist, f"Edge{i}"))
            except Exception:
                pass

    if edge_candidates:
        edge_candidates.sort()  # closest to origin first
        return edge_candidates[0][1]

    return "Face1"


def assembly_add_joint(
    assembly: str,
    joint_type: str,
    part_a: str,
    element_a: str,
    part_b: str,
    element_b: str,
    doc: str | None = None,
    **params: Any,
) -> dict[str, Any]:
    """Add a joint constraint between two parts in an assembly.

    Uses the FreeCAD 1.0+ JointObject API (bare imports — Assembly workbench
    adds src/Mod/Assembly/ to sys.path).

    joint_type: revolute, prismatic, fixed, gear_mesh, belt_chain, etc.
    part_a/part_b: link names in the assembly.
    element_a/element_b: sub-element references (e.g., 'Face1', 'Edge3').
    params: extra joint parameters (ratio for gear, etc.).
    """
    from freecad_addon.compat import get_assembly_modules, require_v1_plus
    require_v1_plus("Assembly joints")
    JointObject, UtilsAssembly = get_assembly_modules()

    d = _get_doc(doc)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")

    type_index = _JOINT_TYPE_INDEX.get(joint_type)
    if type_index is None:
        raise ValueError(
            f"Unknown joint type '{joint_type}'. "
            f"Valid types: {', '.join(_JOINT_TYPE_INDEX)}"
        )

    joint_name = params.pop("name", f"Joint_{joint_type}")

    from freecad_addon.compat import find_object
    link_a = find_object(d, part_a)
    link_b = find_object(d, part_b)
    if link_a is None:
        available_links = [o.Name for o in d.Objects if "Link" in o.TypeId or "link" in o.Name.lower()]
        raise ValueError(f"Part '{part_a}' not found in assembly. Available links: {available_links}")
    if link_b is None:
        available_links = [o.Name for o in d.Objects if "Link" in o.TypeId or "link" in o.Name.lower()]
        raise ValueError(f"Part '{part_b}' not found in assembly. Available links: {available_links}")

    # Auto-resolve element references when set to "auto"
    joint_origin = params.pop("joint_origin", None)
    joint_axis = params.pop("joint_axis", None)
    if element_a == "auto":
        element_a = _resolve_joint_element(link_a, joint_type, origin=joint_origin, axis=joint_axis)
    if element_b == "auto":
        element_b = _resolve_joint_element(link_b, joint_type, origin=joint_origin, axis=joint_axis)

    # Create joint via FreeCAD 1.0+ API
    joint_group = UtilsAssembly.getJointGroup(asm_obj)
    joint = joint_group.newObject("App::FeaturePython", joint_name)
    JointObject.Joint(joint, type_index)

    if FreeCADGui is not None:
        try:
            JointObject.ViewProviderJoint(joint.ViewObject)
        except Exception:
            pass  # headless or ViewProvider unavailable

    # Set references using FreeCAD 1.0 Reference properties
    joint.Reference1 = (link_a, [element_a])
    joint.Reference2 = (link_b, [element_b])

    # Apply joint-specific parameters
    if joint_type == "gear_mesh" and "ratio" in params:
        if hasattr(joint, "Ratio"):
            joint.Ratio = float(params["ratio"])
    if joint_type == "distance" and "distance" in params:
        if hasattr(joint, "Distance"):
            joint.Distance = float(params["distance"])

    joint_group.purgeTouched()
    asm_obj.purgeTouched()
    d.recompute()

    return {
        "joint_name": joint.Name,
        "joint_type": joint_type,
        "part_a": part_a,
        "element_a": element_a,
        "part_b": part_b,
        "element_b": element_b,
    }


def assembly_solve(
    assembly: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Solve the assembly constraints (run the Ondsel constraint solver)."""
    from freecad_addon.compat import require_v1_plus
    require_v1_plus("Assembly solve")

    d = _get_doc(doc)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")

    # Trigger solver
    if hasattr(asm_obj, "solve"):
        asm_obj.solve()
    d.recompute()

    # Collect placements after solving
    placements: dict[str, dict[str, Any]] = {}
    for obj in asm_obj.Group if hasattr(asm_obj, "Group") else []:
        if hasattr(obj, "Placement"):
            p = obj.Placement
            placements[obj.Name] = {
                "position": [p.Base.x, p.Base.y, p.Base.z],
                "rotation": [p.Rotation.Angle, *list(p.Rotation.Axis)],
            }

    return {
        "assembly": assembly,
        "solved": True,
        "placements": placements,
    }


def assembly_drive_joint(
    assembly: str,
    joint: str,
    value: float,
    steps: int = 10,
    doc: str | None = None,
) -> dict[str, Any]:
    """Drive a joint through a range of values, capturing screenshots at each step.

    For revolute joints, value is the total rotation in degrees.
    For prismatic joints, value is the total translation in mm.
    """
    from freecad_addon.compat import find_joint_in_assembly, require_v1_plus
    require_v1_plus("Assembly drive_joint")

    d = _get_doc(doc)
    _ensure_gui_doc(d)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")

    joint_obj = find_joint_in_assembly(d, asm_obj, joint)
    if joint_obj is None:
        # Build diagnostic list from the assembly's JointGroup children
        available_joints: list[str] = []
        if hasattr(asm_obj, "Group"):
            for child in asm_obj.Group:
                if hasattr(child, "Group"):
                    for gchild in child.Group:
                        available_joints.append(f"{gchild.Name} (Label={gchild.Label})")
        raise ValueError(
            f"Joint '{joint}' not found in assembly '{assembly}'. "
            f"Joints in JointGroup: {available_joints}"
        )

    step_positions: list[dict[str, Any]] = []
    screenshots: list[str] = []

    for i in range(steps + 1):
        fraction = i / steps
        current_value = value * fraction

        # Drive the joint by setting its offset/angle (FreeCAD 1.0+)
        if hasattr(joint_obj, "Angle"):
            joint_obj.Angle = current_value
        elif hasattr(joint_obj, "Distance"):
            joint_obj.Distance = current_value
        elif hasattr(joint_obj, "Offset"):
            joint_obj.Offset = current_value

        asm_obj.solve()
        d.recompute()

        # Collect placement of all parts
        placements: dict[str, list[float]] = {}
        for obj in asm_obj.Group if hasattr(asm_obj, "Group") else []:
            if hasattr(obj, "Placement"):
                p = obj.Placement
                placements[obj.Name] = [p.Base.x, p.Base.y, p.Base.z]

        step_positions.append({
            "step": i,
            "value": current_value,
            "placements": placements,
        })

        # Capture screenshot at each step
        if FreeCADGui is not None:
            _process_qt_events()
            try:
                view = FreeCADGui.ActiveDocument.ActiveView
                img_path = str(Path(tempfile.gettempdir()) / f"drive_{joint}_{i:03d}.png")
                view.saveImage(img_path, 512, 512)
                screenshots.append(img_path)
            except Exception:
                pass

    return {
        "assembly": assembly,
        "joint": joint,
        "total_value": value,
        "steps": steps,
        "step_positions": step_positions,
        "screenshots": screenshots,
    }


def assembly_check_interference(
    assembly: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Check for interference (collision) between parts in an assembly.

    Uses BRepAlgoAPI_Common (Part.Shape.common) to detect overlapping volumes.
    """
    from freecad_addon.compat import require_v1_plus
    require_v1_plus("Assembly check_interference")

    d = _get_doc(doc)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")

    # Collect all shapes from assembly links
    parts: list[tuple[str, Any]] = []
    for obj in asm_obj.Group if hasattr(asm_obj, "Group") else []:
        if hasattr(obj, "LinkedObject") and obj.LinkedObject is not None:
            linked = obj.LinkedObject
            if hasattr(linked, "Shape") and linked.Shape is not None and not linked.Shape.isNull():
                # Transform shape to assembly coordinates
                shape = linked.Shape.copy()
                shape.Placement = obj.Placement
                parts.append((obj.Name, shape))
        elif hasattr(obj, "Shape") and obj.Shape is not None and not obj.Shape.isNull():
            parts.append((obj.Name, obj.Shape))

    collisions: list[dict[str, Any]] = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            name_a, shape_a = parts[i]
            name_b, shape_b = parts[j]
            try:
                common = shape_a.common(shape_b)
                if common is not None and not common.isNull() and common.Volume > 1e-6:
                    collisions.append({
                        "part_a": name_a,
                        "part_b": name_b,
                        "overlap_mm3": round(common.Volume, 4),
                    })
            except Exception:
                # common() can fail for certain shape combinations
                pass

    return {
        "assembly": assembly,
        "clear": len(collisions) == 0,
        "part_count": len(parts),
        "checks_performed": len(parts) * (len(parts) - 1) // 2,
        "collisions": collisions,
    }


def assembly_get_links(
    assembly: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Return ``{link_name: body_name}`` for every link in an assembly."""
    d = _get_doc(doc)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        raise ValueError(f"Assembly '{assembly}' not found")

    links: dict[str, str] = {}
    for child in asm_obj.Group:
        linked = getattr(child, "LinkedObject", None)
        if linked is not None:
            links[child.Name] = linked.Name
    return {"assembly": assembly, "links": links}


def _apply_placements(
    assembly: str,
    placements: dict[str, dict[str, Any]],
    doc_name: str | None = None,
    skip_recompute: bool = False,
) -> list[str]:
    """Apply placement specs to assembly links. Returns list of applied link names.

    Each entry in *placements* maps a link name to one of two formats:

    **Legacy format** (rotation around a center)::

        {
            "angle_deg": float,      # rotation angle
            "axis": [ax, ay, az],    # rotation axis (unit vector)
            "center": [cx, cy, cz], # rotation center in mm
        }

    **Compound format** (explicit position + rotation)::

        {
            "position": [x, y, z],              # translation in mm
            "rotation_axis": [ax, ay, az],       # rotation axis (unit vector)
            "rotation_angle_deg": float,         # rotation angle
        }

    Detection: if ``"position"`` key is present, compound format is used.
    """
    d = _get_doc(doc_name)

    applied: list[str] = []
    for link_name, spec in placements.items():
        link = d.getObject(link_name)
        if link is None:
            continue

        if "position" in spec:
            # Compound format: explicit position + rotation
            pos = spec.get("position", [0.0, 0.0, 0.0])
            rot_axis = spec.get("rotation_axis", [0.0, 0.0, 1.0])
            rot_angle = spec.get("rotation_angle_deg", 0.0)
            rot = FreeCAD.Rotation(FreeCAD.Vector(*rot_axis), rot_angle)
            link.Placement = FreeCAD.Placement(FreeCAD.Vector(*pos), rot)
        else:
            # Legacy format: rotation around a center
            angle_deg = spec.get("angle_deg", 0.0)
            ax = spec.get("axis", [0.0, 0.0, 1.0])
            center = spec.get("center", [0.0, 0.0, 0.0])

            rot = FreeCAD.Rotation(FreeCAD.Vector(*ax), angle_deg)
            center_vec = FreeCAD.Vector(*center)
            # Rotation around an arbitrary center:
            # new_pos = center + rot * (original_pos - center)
            # For links starting at origin, original_pos = [0,0,0]:
            new_base = center_vec - rot.multVec(center_vec)
            link.Placement = FreeCAD.Placement(new_base, rot)

        applied.append(link_name)

    if not skip_recompute:
        d.recompute()
    return applied


def assembly_set_placements(
    assembly: str,
    placements: dict[str, dict[str, Any]],
    doc: str | None = None,
    screenshot: bool = False,
) -> dict[str, Any]:
    """Set link placements directly (analytical animation, bypasses solver)."""
    d = _get_doc(doc)
    _ensure_gui_doc(d)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")

    applied = _apply_placements(assembly, placements, doc)

    result: dict[str, Any] = {"assembly": assembly, "applied": applied}

    if screenshot and FreeCADGui is not None:
        _process_qt_events()
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            img_path = str(Path(tempfile.gettempdir()) / f"animate_{assembly}.png")
            view.saveImage(img_path, 512, 512)
            result["screenshot"] = img_path
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Animation engine (QTimer-based looping playback)
# ---------------------------------------------------------------------------

_active_animation: dict[str, Any] | None = None


def _animation_tick() -> None:
    """Called by QTimer on each frame. Apply the next frame's placements."""
    import time

    global _active_animation
    state = _active_animation
    if state is None:
        return

    elapsed = time.monotonic() - state["start_time"]
    if elapsed >= state["duration_s"]:
        _animation_stop_internal()
        return

    frames = state["frames"]
    idx = state["frame_index"] % len(frames)
    _apply_placements(state["assembly"], frames[idx], state["doc_name"], skip_recompute=True)
    _process_qt_events()
    state["frame_index"] += 1


def _animation_stop_internal() -> dict[str, Any]:
    """Stop the running animation and return stats."""
    import time

    global _active_animation
    state = _active_animation
    if state is None:
        return {"status": "no_animation_running"}

    timer = state["timer"]
    timer.stop()
    elapsed = time.monotonic() - state["start_time"]
    frames_played = state["frame_index"]
    _active_animation = None
    return {"status": "stopped", "frames_played": frames_played, "elapsed_s": round(elapsed, 2)}


def assembly_animate(
    assembly: str | None = None,
    frames: list[dict[str, Any]] | None = None,
    duration_s: float = 10.0,
    fps: int = 30,
    doc: str | None = None,
) -> dict[str, Any]:
    """Play a looping animation of placement frames in FreeCAD.

    *frames* is a list of placement dicts (one per frame).  Each dict maps
    object names (assembly links or standalone bodies) to placement specs
    (same format as ``assembly_set_placements``).
    The animation loops through the frames at *fps* for *duration_s* seconds,
    then auto-stops.

    When *assembly* is ``"__bodies__"`` or no assembly exists, objects are
    resolved directly by name (no assembly required).
    """
    import time

    from freecad_addon.qt_compat import QTimer

    global _active_animation

    if frames is None or len(frames) == 0:
        raise ValueError("frames must be a non-empty list of placement dicts")

    d = _get_doc(doc)

    # Auto-detect or skip assembly lookup
    if assembly == "__bodies__":
        # Explicit opt-in: animate standalone bodies, no assembly needed
        pass
    elif assembly is None:
        assemblies = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        if len(assemblies) == 0:
            # No assembly — animate bodies directly
            assembly = "__bodies__"
        else:
            assembly = assemblies[0]
    else:
        asm_obj = d.getObject(assembly)
        if asm_obj is None:
            available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
            raise ValueError(f"Assembly '{assembly}' not found. Available: {available}")

    # Stop any running animation first
    if _active_animation is not None:
        _animation_stop_internal()

    timer = QTimer()
    interval_ms = max(1, 1000 // fps)
    timer.setInterval(interval_ms)
    timer.timeout.connect(_animation_tick)

    _active_animation = {
        "assembly": assembly,
        "frames": frames,
        "doc_name": doc,
        "duration_s": duration_s,
        "fps": fps,
        "timer": timer,
        "frame_index": 0,
        "start_time": time.monotonic(),
    }

    timer.start()

    return {
        "status": "started",
        "assembly": assembly,
        "frame_count": len(frames),
        "duration_s": duration_s,
        "fps": fps,
    }


def assembly_animate_stop() -> dict[str, Any]:
    """Stop any running animation."""
    return _animation_stop_internal()


def assembly_get_placements(
    assembly: str,
    doc: str | None = None,
) -> dict[str, Any]:
    """Get current placements of all parts in an assembly."""
    from freecad_addon.compat import require_v1_plus
    require_v1_plus("Assembly get_placements")

    d = _get_doc(doc)
    asm_obj = d.getObject(assembly)
    if asm_obj is None:
        available = [o.Name for o in d.Objects if "Assembly" in o.TypeId]
        raise ValueError(f"Assembly '{assembly}' not found. Available assemblies: {available}")

    placements: dict[str, dict[str, Any]] = {}
    for obj in asm_obj.Group if hasattr(asm_obj, "Group") else []:
        if hasattr(obj, "Placement"):
            p = obj.Placement
            placements[obj.Name] = {
                "position": [p.Base.x, p.Base.y, p.Base.z],
                "rotation_angle_deg": math.degrees(p.Rotation.Angle),
                "rotation_axis": [p.Rotation.Axis.x, p.Rotation.Axis.y, p.Rotation.Axis.z],
            }

    return {
        "assembly": assembly,
        "placements": placements,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _process_qt_events() -> None:
    """Flush pending Qt events so viewport redraws complete."""
    from freecad_addon.qt_compat import QApplication
    QApplication.processEvents()


def _get_doc(doc: str | None) -> Any:
    """Resolve a FreeCAD document by name, or get the active one."""
    if doc is not None:
        d = FreeCAD.getDocument(doc)
        if d is None:
            raise ValueError(f"Document '{doc}' not found")
        return d
    d = FreeCAD.ActiveDocument
    if d is None:
        raise ValueError("No active document. Call new_document first.")
    return d


def _ensure_gui_doc(doc: Any) -> None:
    """Ensure FreeCADGui.ActiveDocument matches the given document."""
    if FreeCADGui is None:
        return
    gui_doc = FreeCADGui.ActiveDocument
    if gui_doc is None or gui_doc.Document.Name != doc.Name:
        FreeCAD.setActiveDocument(doc.Name)
        if hasattr(FreeCADGui, "setActiveDocument"):
            try:
                FreeCADGui.setActiveDocument(doc.Name)
            except Exception:
                pass


def _get_sketch(doc: Any, sketch_name: str) -> Any:
    """Get a sketch object by name."""
    sk = doc.getObject(sketch_name)
    if sk is None:
        raise ValueError(f"Sketch '{sketch_name}' not found")
    return sk


def _find_parent_body(doc: Any, obj: Any) -> Any:
    """Find the PartDesign::Body that contains ``obj``."""
    for candidate in doc.Objects:
        if candidate.TypeId == "PartDesign::Body":
            if obj in candidate.Group:
                return candidate
    raise ValueError(f"No PartDesign::Body found containing '{obj.Name}'")


def _resolve_body(doc: Any, body_name: str | None) -> Any:
    """Get a body by name, or the first body in the doc."""
    if body_name:
        body_obj = doc.getObject(body_name)
        if body_obj is None:
            raise ValueError(f"Body '{body_name}' not found")
        return body_obj
    # Find first body
    for obj in doc.Objects:
        if obj.TypeId == "PartDesign::Body":
            return obj
    raise ValueError("No PartDesign::Body in document. Call new_body first.")


def _get_tip(body: Any) -> Any:
    """Get the tip feature (last shape-producing feature) of a body."""
    if hasattr(body, "Tip") and body.Tip is not None:
        return body.Tip
    # Fallback: last object with a shape
    for obj in reversed(body.Group):
        if hasattr(obj, "Shape") and obj.Shape is not None:
            return obj
    raise ValueError(f"Body '{body.Name}' has no features")


# ---------------------------------------------------------------------------
# Shape Digest + Delta
# ---------------------------------------------------------------------------

_body_digests: dict[str, dict[str, Any]] = {}


def _compute_digest(shape: Any) -> dict[str, Any]:
    """Compute a shape digest: volume, surface area, bbox, topology counts."""
    bb = shape.BoundBox
    digest: dict[str, Any] = {
        "bbox": [round(bb.XLength, 4), round(bb.YLength, 4), round(bb.ZLength, 4)],
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
        "vertices": len(shape.Vertexes),
    }
    try:
        digest["volume"] = round(shape.Volume, 4)
    except Exception:
        digest["volume"] = None
    try:
        digest["surface_area"] = round(shape.Area, 4)
    except Exception:
        digest["surface_area"] = None
    return digest


def _compute_delta(
    prev: dict[str, Any] | None, curr: dict[str, Any],
) -> dict[str, Any] | None:
    """Compute numeric delta between two digests."""
    if prev is None:
        return None
    delta: dict[str, Any] = {}
    for key in ("volume", "surface_area", "faces", "edges", "vertices"):
        pv = prev.get(key)
        cv = curr.get(key)
        if pv is not None and cv is not None:
            diff = cv - pv
            delta[key] = round(diff, 4) if isinstance(diff, float) else diff
        else:
            delta[key] = None
    return delta


_FEATURE_HINTS: dict[str, str] = {
    "PartDesign::Pad": "sketch profile may be invalid or self-intersecting",
    "PartDesign::Pocket": "pocket depth may exceed solid thickness, or sketch profile may be invalid",
    "PartDesign::Hole": "hole diameter may be too large, or face reference may be invalid",
    "PartDesign::Fillet": "radius may be too large for the selected edges, or edge references may be invalid",
    "PartDesign::Chamfer": "size may be too large for the selected edges, or edge references may be invalid",
    "PartDesign::Revolution": "sketch profile may be invalid, not closed, or axis may intersect the profile",
    "PartDesign::Groove": "sketch profile may be invalid, not closed, or axis may intersect the profile",
    "PartDesign::AdditiveHelix": "sketch profile may be invalid, or helix parameters (pitch/height/turns) may be inconsistent",
    "PartDesign::PolarPattern": "pattern may produce overlapping or self-intersecting geometry",
    "PartDesign::AdditivePipe": "sweep failed: spine may be discontinuous, profile may be open, or profile may intersect the spine",
    "PartDesign::SubtractivePipe": "subtractive sweep failed: spine may be discontinuous, profile may be open, or cut may not intersect the solid",
    "PartDesign::AdditiveLoft": "loft failed: sections may have different vertex counts, or profiles may be too far apart",
    "PartDesign::SubtractiveLoft": "subtractive loft failed: sections may not form a valid cut, or profiles may be incompatible",
    "PartDesign::Mirrored": "mirror failed: pattern may produce overlapping or self-intersecting geometry",
    "PartDesign::LinearPattern": "linear pattern failed: pattern spacing may cause overlapping geometry",
    "PartDesign::Thickness": "shell/thickness failed: selected faces may not form a valid shell, or thickness may be too large",
    "PartDesign::Draft": "draft failed: faces may not be valid for tapering, or angle may be too large for the geometry",
}


def _gather_failure_diagnostics(obj: Any) -> str:
    """Inspect a failed feature's references to build a diagnostic string."""
    parts: list[str] = []
    try:
        base = getattr(obj, "Base", None)
        if base is not None:
            ref_obj, sub_names = base
            if hasattr(ref_obj, "Shape") and ref_obj.Shape is not None:
                shape = ref_obj.Shape
                parts.append(f"total_edges={len(shape.Edges)}; total_faces={len(shape.Faces)}")
                for sub_name in sub_names:
                    try:
                        sub_shape = shape.getElement(sub_name)
                        if hasattr(sub_shape, "Length"):
                            parts.append(f"{sub_name}: length={sub_shape.Length:.2f}mm")
                        elif hasattr(sub_shape, "Area"):
                            parts.append(f"{sub_name}: area={sub_shape.Area:.2f}mm²")
                    except Exception:
                        parts.append(f"{sub_name}: not found")
        # Add operation-specific values
        radius = getattr(obj, "Radius", None)
        if radius is not None:
            parts.append(f"requested_radius={radius:.2f}mm")
        size = getattr(obj, "Size", None)
        if size is not None:
            parts.append(f"requested_size={size:.2f}mm")
        length = getattr(obj, "Length", None)
        if length is not None and obj.TypeId in ("PartDesign::Pocket",):
            parts.append(f"requested_depth={length:.2f}mm")

        # Profile diagnostics for pad/pocket/revolution/pipe/loft
        profile = getattr(obj, "Profile", None)
        if profile is not None:
            sk = profile[0] if isinstance(profile, (list, tuple)) else profile
            if hasattr(sk, "Shape") and sk.Shape is not None:
                wires = sk.Shape.Wires
                parts.append(f"profile_wires={len(wires)}")
                for i, wire in enumerate(wires):
                    parts.append(f"wire{i}_closed={wire.isClosed()}")
                open_verts = getattr(sk, "OpenVertices", None)
                if open_verts:
                    parts.append(f"open_vertices={len(open_verts)}")

        # Spine diagnostics for pipe (sweep) features
        spine = getattr(obj, "Spine", None)
        if spine is not None:
            try:
                spine_obj = spine[0] if isinstance(spine, (list, tuple)) else spine
                if hasattr(spine_obj, "Shape") and spine_obj.Shape is not None:
                    spine_shape = spine_obj.Shape
                    parts.append(f"spine_edges={len(spine_shape.Edges)}")
                    if spine_shape.Wires:
                        for i, wire in enumerate(spine_shape.Wires):
                            parts.append(f"spine_wire{i}_closed={wire.isClosed()}")
            except Exception:
                pass

        # Originals diagnostics for pattern features
        originals = getattr(obj, "Originals", None)
        if originals is not None:
            valid_count = sum(1 for o in originals if getattr(o, "isValid", lambda: True)())
            parts.append(f"originals={len(originals)}; valid={valid_count}")

        # Thickness/Draft value
        value = getattr(obj, "Value", None)
        if value is not None and obj.TypeId == "PartDesign::Thickness":
            parts.append(f"requested_thickness={value:.2f}mm")
        angle = getattr(obj, "Angle", None)
        if angle is not None and obj.TypeId == "PartDesign::Draft":
            parts.append(f"requested_angle={angle:.2f}deg")
    except Exception:
        pass
    return "; ".join(parts)


def _recompute_and_check(
    doc: Any,
    obj: Any,
    body: Any | None = None,
    op_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the document and verify the feature is valid.

    If recompute fails (feature enters an invalid state), the broken feature
    is removed from the document and a ``ValueError`` is raised so the error
    propagates back to the MCP client.

    If ``body`` is provided, computes a shape digest, delta, face map, and
    operation summary after success.
    """
    doc.recompute()

    # Check if the feature is in a valid state
    is_valid = getattr(obj, "isValid", lambda: True)()
    state = getattr(obj, "State", [])
    has_invalid_state = "Invalid" in state if state else False
    shape = getattr(obj, "Shape", None)
    shape_is_null = shape.isNull() if shape is not None else False

    if not is_valid or has_invalid_state or shape_is_null:
        feature_type = obj.TypeId
        feature_label = feature_type.split("::")[-1]
        hint = _FEATURE_HINTS.get(feature_type, "geometry may be invalid")
        diagnostics = _gather_failure_diagnostics(obj)
        logger.error(
            "Feature %s (%s) failed recompute: valid=%s state=%s null_shape=%s. Diagnostics: %s",
            obj.Name, obj.TypeId, is_valid, state, shape_is_null, diagnostics,
        )
        obj_name = obj.Name
        doc.removeObject(obj_name)
        doc.recompute()
        msg = f"{feature_label} failed: recompute error ({hint}). The failed feature has been removed."
        if diagnostics:
            msg += f" Diagnostics: {diagnostics}"
        raise ValueError(msg)

    result = _feature_result(obj)

    # Compute digest + delta if body is available
    if body is not None:
        body_name = body.Name if hasattr(body, "Name") else str(body)
        try:
            tip = _get_tip(body)
            digest = _compute_digest(tip.Shape)
            prev_digest = _body_digests.get(body_name)
            delta = _compute_delta(prev_digest, digest)
            _body_digests[body_name] = digest
            result["digest"] = digest
            result["delta"] = delta
        except Exception:
            pass

    # Face map and operation summary for spatial reasoning
    if body is not None:
        try:
            result["face_map"] = _build_face_map(body)
        except Exception:
            pass
    result["operation_summary"] = _operation_summary(obj, op_context)

    # Drift detection: re-resolve all named selectors after topology changes
    if _selection_sets:
        drift_results: list[dict[str, Any]] = []
        for sel_name, entry in _selection_sets.items():
            try:
                sel_result = resolve_selection(sel_name, body=None, doc=None)
                drift_entry: dict[str, Any] = {
                    "name": sel_name,
                    "count": sel_result["num_matched"],
                }
                if sel_result["invariants_ok"]:
                    drift_entry["status"] = "ok"
                else:
                    drift_entry["status"] = "DRIFT"
                    drift_entry["violations"] = sel_result["violations"]
                    inv = entry.get("invariants", {})
                    if "expected_count" in inv:
                        drift_entry["expected_count"] = inv["expected_count"]
                        drift_entry["actual_count"] = sel_result["num_matched"]
                drift_results.append(drift_entry)
            except Exception:
                drift_results.append({
                    "name": sel_name,
                    "status": "ERROR",
                    "count": 0,
                })
        result["selection_drift"] = drift_results

    return result


def _feature_result(obj: Any) -> dict[str, Any]:
    """Build a standard result dict for a created feature."""
    result: dict[str, Any] = {"name": obj.Name, "label": obj.Label, "type": obj.TypeId}
    if hasattr(obj, "Shape") and obj.Shape is not None and not obj.Shape.isNull():
        bb = obj.Shape.BoundBox
        result["bounding_box"] = {
            "x_len": bb.XLength, "y_len": bb.YLength, "z_len": bb.ZLength,
        }
        try:
            result["volume"] = obj.Shape.Volume
        except Exception:
            pass
        try:
            result["surface_area"] = obj.Shape.Area
        except Exception:
            pass
        result["num_faces"] = len(obj.Shape.Faces)
        result["num_edges"] = len(obj.Shape.Edges)
        result["num_vertices"] = len(obj.Shape.Vertexes)
    return result


def _build_face_map(body: Any, max_faces: int = 30) -> dict[str, Any]:
    """Build a compact spatial index of body faces for LLM spatial reasoning.

    Returns face names, surface types, normals, centers, and areas sorted by
    area (largest first).  Caps at ``max_faces`` entries.
    """
    try:
        tip = _get_tip(body)
        shape = tip.Shape
    except Exception:
        return {"faces": [], "total_faces": 0}

    all_faces = shape.Faces
    face_list: list[dict[str, Any]] = []
    for i, face in enumerate(all_faces):
        info: dict[str, Any] = {"name": f"Face{i + 1}"}
        surface = face.Surface
        info["surface_type"] = type(surface).__name__
        if hasattr(surface, "Axis"):
            info["normal"] = [round(v, 4) for v in _vec_to_list(surface.Axis)]
        if hasattr(face, "CenterOfMass"):
            info["center"] = [round(v, 2) for v in _vec_to_list(face.CenterOfMass)]
        info["area"] = round(face.Area, 2)
        face_list.append(info)

    # Sort by area descending so the most important faces come first
    face_list.sort(key=lambda f: f["area"], reverse=True)
    total = len(face_list)
    truncated = total > max_faces
    face_list = face_list[:max_faces]

    result: dict[str, Any] = {"faces": face_list, "total_faces": total}
    if truncated:
        result["truncated"] = True
    return result


def _operation_summary(obj: Any, op_context: dict[str, Any] | None) -> str:
    """Generate a human-readable one-line summary of what the operation did."""
    if op_context is None:
        return f"Created {obj.Name}"

    op = op_context.get("op", "unknown")

    # Get bounding box dimensions for size description
    bbox_str = ""
    try:
        if hasattr(obj, "Shape") and obj.Shape is not None and not obj.Shape.isNull():
            bb = obj.Shape.BoundBox
            bbox_str = f" → {bb.XLength:.1f}×{bb.YLength:.1f}×{bb.ZLength:.1f}mm"
    except Exception:
        pass

    if op == "pad":
        length = op_context.get("length", "?")
        return f"Padded {op_context.get('sketch', 'sketch')} by {length}mm{bbox_str}"
    if op == "pocket":
        ptype = op_context.get("pocket_type", "Dimension")
        if ptype == "ThroughAll":
            return f"Pocketed through all{bbox_str}"
        length = op_context.get("length", "?")
        return f"Pocketed {length}mm deep{bbox_str}"
    if op == "hole":
        d = op_context.get("diameter", "?")
        depth = op_context.get("depth", "?")
        face = op_context.get("face", "?")
        return f"Hole ⌀{d}mm, {depth}mm deep on {face}"
    if op == "fillet":
        r = op_context.get("radius", "?")
        n = op_context.get("edge_count", "?")
        return f"Filleted {n} edge(s) with r={r}mm"
    if op == "chamfer":
        s = op_context.get("size", "?")
        n = op_context.get("edge_count", "?")
        return f"Chamfered {n} edge(s) with size={s}mm"
    if op == "revolution":
        angle = op_context.get("angle", 360)
        axis = op_context.get("axis", "V")
        sub = " (subtractive)" if op_context.get("subtractive") else ""
        return f"Revolved {angle}° around {axis}{sub}{bbox_str}"
    if op == "polar_pattern":
        n = op_context.get("occurrences", "?")
        angle = op_context.get("angle", 360)
        return f"Polar pattern: {n} copies over {angle}°"
    if op == "sweep":
        sub = "Subtractive sweep" if op_context.get("subtractive") else "Sweep"
        return f"{sub} along spine{bbox_str}"
    if op == "helix":
        mode = op_context.get("mode", "pitch-height")
        return f"Helix ({mode}){bbox_str}"
    if op == "loft":
        n = op_context.get("sketch_count", "?")
        sub = "Subtractive loft" if op_context.get("subtractive") else "Loft"
        return f"{sub} between {n} profiles{bbox_str}"

    return f"Created {obj.Name}{bbox_str}"


def _compute_targeted_view(
    op_context: dict[str, Any] | None,
    doc: Any,
    body: Any | None = None,
) -> tuple[str, tuple[float, float, float]]:
    """Compute a targeted camera direction based on the operation type.

    Returns (view_label, direction_tuple).  Falls back to front view.
    """
    if op_context is None:
        return ("front", _PRESET_DIRECTIONS["front"])

    op = op_context.get("op", "")

    try:
        if op in ("pad", "pocket") and body is not None:
            # Look from the sketch plane normal direction
            sketch_name = op_context.get("sketch")
            if sketch_name:
                tip = _get_tip(body)
                # For pad/pocket, look from the direction of the new top face
                # Use the last face's normal as an approximation
                shape = tip.Shape
                if shape.Faces:
                    # Find the largest planar face — likely the padded face
                    best_face = None
                    best_area = 0.0
                    for face in shape.Faces:
                        if type(face.Surface).__name__ == "Plane" and face.Area > best_area:
                            best_area = face.Area
                            best_face = face
                    if best_face and hasattr(best_face.Surface, "Axis"):
                        n = best_face.Surface.Axis
                        return ("sketch-normal", (n.x, n.y, n.z))

        if op == "hole":
            face_ref = op_context.get("face")
            if face_ref and body is not None:
                tip = _get_tip(body)
                try:
                    face = tip.Shape.getElement(face_ref)
                    if hasattr(face.Surface, "Axis"):
                        n = face.Surface.Axis
                        return ("hole-face", (n.x, n.y, n.z))
                except Exception:
                    pass

        if op in ("fillet", "chamfer"):
            # Look from iso-alt angle to show edge detail
            return ("detail", (1.0, -1.0, 0.5))

        if op == "revolution":
            # Side view perpendicular to revolution axis
            axis = op_context.get("axis", "V")
            if axis in ("V", "Base_Z"):
                return ("side", (1.0, 0.0, 0.0))
            if axis in ("H", "Base_X"):
                return ("side", (0.0, 1.0, 0.0))
            if axis == "Base_Y":
                return ("side", (1.0, 0.0, 0.0))

        if op == "polar_pattern":
            # Top-down if Z-axis, side view otherwise
            axis = op_context.get("axis", "Base_Z")
            if axis == "Base_Z":
                return ("top", _PRESET_DIRECTIONS["top"])
            return ("side", (1.0, 0.0, 0.0))

        if op in ("sweep", "helix", "loft"):
            # Alternate iso angle for better 3D perspective
            return ("iso-alt", (1.0, -1.0, 1.0))

    except Exception:
        pass

    return ("front", _PRESET_DIRECTIONS["front"])


def _pocket_type_enum(pocket_type: str) -> int:
    """Map pocket type string to FreeCAD enum value."""
    mapping = {
        "Dimension": 0,
        "ThroughAll": 1,
        "ToFirst": 2,
        "ToLast": 3,
    }
    return mapping.get(pocket_type, 0)


def _vec_to_list(vec: Any) -> list[float]:
    """Convert a FreeCAD Vector to a plain list."""
    return [vec.x, vec.y, vec.z]


def set_visibility(
    objects: list[str] | None = None,
    visible: bool = True,
    doc: str | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Show or hide objects by name."""
    d = _get_doc(doc)
    _ensure_gui_doc(d)
    if objects is None:
        objects = []
    if FreeCADGui is None:
        return {"ok": True, "changed": [], "visible": visible}
    changed: list[str] = []
    for name in objects:
        obj = d.getObject(name)
        if obj is None:
            continue
        gui_obj = FreeCADGui.ActiveDocument.getObject(name)
        if gui_obj is not None:
            gui_obj.Visibility = bool(visible)
            changed.append(name)
    return {"ok": True, "changed": changed, "visible": visible}


# ---------------------------------------------------------------------------
# Command registry — maps cmd strings to handler functions
# ---------------------------------------------------------------------------

def freecad_info() -> dict[str, Any]:
    """Return FreeCAD runtime environment information for diagnostics."""
    from freecad_addon.compat import freecad_info as _freecad_info
    return _freecad_info()


def delete_objects(
    names: list[str],
    doc: str | None = None,
) -> dict[str, Any]:
    """Delete objects from the document by name."""
    d = _get_doc(doc)
    deleted: list[str] = []
    not_found: list[str] = []
    for name in names:
        obj = d.getObject(name)
        if obj is not None:
            d.removeObject(name)
            deleted.append(name)
        else:
            not_found.append(name)
    d.recompute()
    return {"deleted": deleted, "not_found": not_found}


def cleanup_orphans(
    *,
    dry_run: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
    """Delete sketches/pads/patterns that aren't reachable from any body's Tip.

    When a feature operation fails (most commonly :func:`polar_pattern`
    falling back from a cross-body axis reference), the created sketch
    + pad + pattern objects end up un-parented in the document — they
    aren't in any body's ``InList`` chain but still appear in the FreeCAD
    viewport at the world origin.  After a few build/rebuild cycles a
    drone document accumulates dozens of these "ghost" objects, which
    show up in the GUI as a phantom prop / pad floating at the origin.

    This walks the document graph rooted at every ``PartDesign::Body``'s
    Tip, marks every reachable feature as live, and deletes the rest.
    Origin frames, axes, planes, and the sketches/pads referenced by
    live bodies are always preserved.

    Args:
        dry_run: When True, return the orphan list without deleting.
        doc: Document name (defaults to active doc).

    Returns:
        Dict with ``orphans`` (list of object names that were/would be
        deleted) and ``deleted`` (count actually removed).
    """
    d = _get_doc(doc)

    # Walk every body's Tip chain to discover live features.
    live: set[str] = set()
    bodies = [obj for obj in d.Objects if obj.TypeId == "PartDesign::Body"]
    for body in bodies:
        live.add(body.Name)
        # Body's Origin is part of the live set
        origin = getattr(body, "Origin", None)
        if origin is not None:
            live.add(origin.Name)
            for child in getattr(origin, "OutList", ()):
                live.add(child.Name)
        # Walk every feature in the body's Group (sketches, pads,
        # pockets, patterns, ...) — Group is the canonical list of
        # owned features in the body's tree.
        for feat in getattr(body, "Group", ()):
            live.add(feat.Name)
            # Sketches reference the plane they're on; that's already
            # in the Origin.  Features (pads/pockets/patterns) reference
            # the sketch via their Profile or Originals — those are
            # also in the body's Group, so they're caught here.

    # Anything in d.Objects that isn't live and isn't an origin/axis/plane
    # at document scope is an orphan.
    orphan_kinds = (
        "Sketcher::SketchObject",
        "PartDesign::Pad",
        "PartDesign::Pocket",
        "PartDesign::Hole",
        "PartDesign::Revolution",
        "PartDesign::Loft",
        "PartDesign::Sweep",
        "PartDesign::Helix",
        "PartDesign::PolarPattern",
        "PartDesign::LinearPattern",
        "PartDesign::Mirrored",
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
        "PartDesign::Draft",
        "PartDesign::Thickness",
    )
    orphans: list[str] = [
        obj.Name
        for obj in d.Objects
        if obj.Name not in live and obj.TypeId in orphan_kinds
    ]

    deleted = 0
    if not dry_run:
        # Delete in two passes: features first, then sketches.  Pads
        # depend on sketches via Profile, so removing the pad first
        # avoids dependency errors on the sketch removal.
        feature_orphans = [
            n for n in orphans
            if d.getObject(n) is not None
            and d.getObject(n).TypeId != "Sketcher::SketchObject"
        ]
        sketch_orphans = [n for n in orphans if n not in feature_orphans]
        for name in feature_orphans + sketch_orphans:
            obj = d.getObject(name)
            if obj is not None:
                d.removeObject(name)
                deleted += 1
        d.recompute()

    return {"orphans": orphans, "deleted": deleted, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# Placement Plan
# ---------------------------------------------------------------------------

_placement_plan: dict[str, dict[str, Any]] = {}
_placement_plan_default_tolerance_mm: float = 5.0


def register_placement_plan(
    plan: dict[str, dict[str, Any]],
    default_tolerance_mm: float = 5.0,
    doc: str | None = None,
) -> dict[str, Any]:
    """Register expected positions/rotations/sizes for bodies.

    Each entry in *plan* maps a body label to a dict with optional keys:
    - ``position``: [x, y, z] expected position in mm (required)
    - ``rotation_axis``: [ax, ay, az] expected rotation axis
    - ``rotation_angle_deg``: expected rotation angle in degrees
    - ``expected_size``: [sx, sy, sz] expected bounding box size in mm
    - ``tolerance_mm``: per-body position tolerance (overrides default)

    Only ``position`` is required per entry; the rest are optional.
    Returns the count and labels of registered entries.
    """
    global _placement_plan_default_tolerance_mm  # noqa: PLW0603
    _placement_plan.clear()
    _placement_plan_default_tolerance_mm = default_tolerance_mm

    labels: list[str] = []
    for label, entry in plan.items():
        if not isinstance(entry, dict):
            continue
        if "position" not in entry:
            continue
        _placement_plan[label] = entry
        labels.append(label)

    return {"registered": len(labels), "labels": labels}


def clear_placement_plan(doc: str | None = None) -> dict[str, Any]:
    """Clear the registered placement plan.

    Returns the count of entries that were cleared.
    """
    n = len(_placement_plan)
    _placement_plan.clear()
    return {"cleared": n}


def _check_placement_against_plan(
    obj: Any,
    plan_entry: dict[str, Any],
    doc: Any,
) -> dict[str, Any]:
    """Validate an object's placement against its plan entry.

    Returns a dict with position_status, rotation_status, and size_status.
    """
    _ROTATION_TOLERANCE_DEG = 2.0

    plan_check: dict[str, Any] = {}
    p = obj.Placement

    # --- Position check ---
    exp_pos = plan_entry.get("position")
    if exp_pos is not None and len(exp_pos) >= 3:
        actual_pos = [p.Base.x, p.Base.y, p.Base.z]
        drift = math.sqrt(sum(
            (a - e) ** 2 for a, e in zip(actual_pos, exp_pos, strict=False)
        ))
        tol = plan_entry.get("tolerance_mm", _placement_plan_default_tolerance_mm)
        plan_check["drift_mm"] = round(drift, 3)
        plan_check["position_status"] = "OK" if drift <= tol else "DRIFT"

    # --- Rotation check ---
    exp_angle = plan_entry.get("rotation_angle_deg")
    if exp_angle is not None:
        actual_angle = math.degrees(p.Rotation.Angle)
        # Normalize angles to 0-360 range for comparison
        actual_norm = actual_angle % 360.0
        exp_norm = float(exp_angle) % 360.0
        delta = abs(actual_norm - exp_norm)
        if delta > 180.0:
            delta = 360.0 - delta
        plan_check["angle_delta_deg"] = round(delta, 3)
        plan_check["rotation_status"] = (
            "OK" if delta <= _ROTATION_TOLERANCE_DEG else "ROTATION_MISMATCH"
        )

    # --- Size check ---
    exp_size = plan_entry.get("expected_size")
    if exp_size is not None and len(exp_size) >= 3:
        try:
            tip = _get_tip(obj)
            if hasattr(tip, "Shape") and tip.Shape is not None and not tip.Shape.isNull():
                bb = tip.Shape.BoundBox
                actual_size = sorted([bb.XLength, bb.YLength, bb.ZLength])
                expected_sorted = sorted(float(s) for s in exp_size)
                max_dim = max(max(actual_size), max(expected_sorted), 1e-9)
                size_tol = max(max_dim * 0.10, 1.0)
                mismatches = [
                    abs(a - e) for a, e in zip(actual_size, expected_sorted, strict=False)
                ]
                max_mismatch = max(mismatches)
                plan_check["actual_size"] = [round(s, 2) for s in actual_size]
                plan_check["expected_size"] = [round(s, 2) for s in expected_sorted]
                plan_check["size_status"] = (
                    "OK" if max_mismatch <= size_tol else "SIZE_MISMATCH"
                )
        except Exception:
            pass

    return plan_check


def set_placement(
    object_name: str,
    position: list[float] | None = None,
    rotation_axis: list[float] | None = None,
    rotation_angle_deg: float = 0.0,
    doc: str | None = None,
) -> dict[str, Any]:
    """Set the Placement of any FreeCAD object (body, link, etc.).

    When a placement plan is registered (via ``register_placement_plan``),
    the response includes a ``plan_check`` dict with position/rotation/size
    validation against the plan.

    Parameters:
        object_name: Name of the FreeCAD object.
        position: [x, y, z] translation in mm (default: unchanged).
        rotation_axis: [ax, ay, az] rotation axis (default: [0,0,1]).
        rotation_angle_deg: rotation angle in degrees.
        doc: Document name (optional).
    """
    d = _get_doc(doc)
    obj = d.getObject(object_name)
    if obj is None:
        available = [o.Name for o in d.Objects]
        raise ValueError(
            f"Object '{object_name}' not found. Available: {available[:20]}"
        )

    if rotation_axis is None:
        rotation_axis = [0.0, 0.0, 1.0]

    rot = FreeCAD.Rotation(FreeCAD.Vector(*rotation_axis), rotation_angle_deg)

    if position is not None:
        base = FreeCAD.Vector(*position)
    else:
        base = obj.Placement.Base

    obj.Placement = FreeCAD.Placement(base, rot)
    d.recompute()

    p = obj.Placement
    result: dict[str, Any] = {
        "object": object_name,
        "position": [p.Base.x, p.Base.y, p.Base.z],
        "rotation_angle_deg": round(math.degrees(p.Rotation.Angle), 6),
        "rotation_axis": list(p.Rotation.Axis),
    }

    # Validate against placement plan if registered
    plan_entry = _placement_plan.get(obj.Label) or _placement_plan.get(object_name)
    if plan_entry is not None:
        plan_check = _check_placement_against_plan(obj, plan_entry, d)
        result["plan_check"] = plan_check

    return result


# ---------------------------------------------------------------------------
# Compound Primitives
# ---------------------------------------------------------------------------

_BOX_KEYS = {"length", "width", "height"}
_CYLINDER_KEYS = {"radius", "height"}


def _build_primitive(
    d: Any,
    name: str,
    shape: str,
    dimensions: dict[str, float],
) -> tuple[Any, Any, Any]:
    """Create a body + sketch + pad for a primitive shape.

    Returns ``(body, sketch, pad_obj)`` so the caller can apply placement
    and verification independently.
    """
    # --- Validate shape ---
    if shape not in ("box", "cylinder"):
        raise ValueError(f"Unsupported shape '{shape}'. Must be 'box' or 'cylinder'.")

    # --- Validate dimensions ---
    if shape == "box":
        missing = _BOX_KEYS - dimensions.keys()
        if missing:
            raise ValueError(f"Box requires dimensions: {sorted(_BOX_KEYS)}. Missing: {sorted(missing)}")
    else:
        missing = _CYLINDER_KEYS - dimensions.keys()
        if missing:
            raise ValueError(f"Cylinder requires dimensions: {sorted(_CYLINDER_KEYS)}. Missing: {sorted(missing)}")

    for key, val in dimensions.items():
        if not isinstance(val, (int, float)) or val <= 0:
            raise ValueError(f"Dimension '{key}' must be a positive number, got {val!r}")

    # --- Create body ---
    body = d.addObject("PartDesign::Body", name)

    # --- Create sketch on XY ---
    sketch = d.addObject("Sketcher::SketchObject", "Sketch")
    body.addObject(sketch)
    sketch.Placement = FreeCAD.Placement(
        FreeCAD.Vector(0, 0, 0),
        FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), 0),
    )
    d.recompute()

    # --- Populate sketch using existing sketch_populate ---
    if shape == "box":
        length = dimensions["length"]
        width = dimensions["width"]
        elements = [{"type": "rect", "x": -length / 2, "y": -width / 2, "w": length, "h": width}]
    else:
        elements = [{"type": "circle", "cx": 0, "cy": 0, "r": dimensions["radius"]}]

    sketch_populate(sketch=sketch.Name, elements=elements, doc=d.Name)

    # --- Pad with Midplane (symmetric) ---
    height = dimensions["height"]
    pad_obj = d.addObject("PartDesign::Pad", "Pad")
    body.addObject(pad_obj)
    pad_obj.Profile = sketch
    pad_obj.Length = height
    pad_obj.Midplane = True

    op_context = {"op": "create_primitive", "shape": shape, "name": name}
    _recompute_and_check(d, pad_obj, body=body, op_context=op_context)

    return body, sketch, pad_obj


def create_primitive(
    name: str,
    shape: str,
    dimensions: dict[str, float],
    position: list[float] | None = None,
    rotation_axis: list[float] | None = None,
    rotation_angle_deg: float = 0.0,
    verify: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a simple positioned solid body (box or cylinder) in one call."""
    logger.info("create_primitive: name=%s shape=%s dims=%s", name, shape, dimensions)
    d = _get_doc(doc)

    body, sketch, pad_obj = _build_primitive(d, name, shape, dimensions)

    # --- Apply placement ---
    if position is not None or rotation_angle_deg != 0.0:
        if rotation_axis is None:
            rotation_axis = [0.0, 0.0, 1.0]
        rot = FreeCAD.Rotation(FreeCAD.Vector(*rotation_axis), rotation_angle_deg)
        base = FreeCAD.Vector(*(position or [0.0, 0.0, 0.0]))
        body.Placement = FreeCAD.Placement(base, rot)
        d.recompute()

    # --- Build result ---
    p = body.Placement
    bb = body.Shape.BoundBox
    result: dict[str, Any] = {
        "body": body.Name,
        "pad": pad_obj.Name,
        "sketch": sketch.Name,
        "position": [p.Base.x, p.Base.y, p.Base.z],
        "rotation_angle_deg": round(math.degrees(p.Rotation.Angle), 6),
        "rotation_axis": list(p.Rotation.Axis),
        "bbox_mm": [round(bb.XLength, 3), round(bb.YLength, 3), round(bb.ZLength, 3)],
    }

    if verify:
        op_context = {"op": "create_primitive", "shape": shape, "name": name}
        result["verification_images"] = _capture_verification_views(d, op_context=op_context, body=body)

    logger.info("create_primitive: created %s", body.Name)
    return result


def create_primitives(
    items: list[dict[str, Any]],
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create multiple simple positioned solid bodies in one call."""
    logger.info("create_primitives: %d items", len(items))
    d = _get_doc(doc)

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        item_name = item.get("name", f"Primitive_{idx}")
        try:
            body, sketch, pad_obj = _build_primitive(
                d,
                name=item_name,
                shape=item["shape"],
                dimensions=item["dimensions"],
            )

            # Apply placement
            position = item.get("position")
            rotation_axis = item.get("rotation_axis")
            rotation_angle_deg = item.get("rotation_angle_deg", 0.0)

            if position is not None or rotation_angle_deg != 0.0:
                if rotation_axis is None:
                    rotation_axis = [0.0, 0.0, 1.0]
                rot = FreeCAD.Rotation(FreeCAD.Vector(*rotation_axis), rotation_angle_deg)
                base = FreeCAD.Vector(*(position or [0.0, 0.0, 0.0]))
                body.Placement = FreeCAD.Placement(base, rot)
                d.recompute()

            p = body.Placement
            bb = body.Shape.BoundBox
            created.append({
                "body": body.Name,
                "pad": pad_obj.Name,
                "sketch": sketch.Name,
                "position": [p.Base.x, p.Base.y, p.Base.z],
                "rotation_angle_deg": round(math.degrees(p.Rotation.Angle), 6),
                "rotation_axis": list(p.Rotation.Axis),
                "bbox_mm": [round(bb.XLength, 3), round(bb.YLength, 3), round(bb.ZLength, 3)],
            })
        except Exception as exc:
            logger.warning("create_primitives: item %d (%s) failed: %s", idx, item_name, exc)
            failed.append({"index": idx, "name": item_name, "error": str(exc)})

    result: dict[str, Any] = {"created": created, "failed": failed}

    if verify and created:
        op_context = {"op": "create_primitives", "count": len(created)}
        result["verification_images"] = _capture_verification_views(d, op_context=op_context)

    logger.info("create_primitives: %d created, %d failed", len(created), len(failed))
    return result


def check_joint_connectivity(
    joints: list[dict[str, Any]],
    tolerance_mm: float = 2.0,
    doc: str | None = None,
) -> dict[str, Any]:
    """Check that each joint origin touches both parent and child body geometry.

    ``joints`` is a list of dicts, each with:
      - ``id``: joint name
      - ``parent_body``: FreeCAD body name for the parent part
      - ``child_body``: FreeCAD body name for the child part
      - ``origin``: [x, y, z] joint origin in mm (world frame)

    For each joint, computes the minimum distance from the origin point to
    both the parent and child body shapes.  If either distance exceeds
    ``tolerance_mm``, the joint is flagged as disconnected.
    """
    import Part as FreeCADPart  # type: ignore[import-untyped]

    d = _get_doc(doc)

    results: list[dict[str, Any]] = []
    all_connected = True

    for jt in joints:
        jt_id = jt["id"]
        parent_body_name = jt["parent_body"]
        child_body_name = jt["child_body"]
        origin = jt["origin"]

        import FreeCAD  # type: ignore[import-untyped]
        point_vec = FreeCAD.Vector(origin[0], origin[1], origin[2])
        # Part.Point creates a TopoDS_Vertex shape suitable for distToShape
        point_shape = FreeCADPart.Point(point_vec).toShape()

        entry: dict[str, Any] = {
            "joint": jt_id,
            "origin_mm": list(origin),
            "parent_body": parent_body_name,
            "child_body": child_body_name,
        }

        for role, body_name in [("parent", parent_body_name), ("child", child_body_name)]:
            obj = d.getObject(body_name)
            if obj is None:
                entry[f"{role}_error"] = f"Body '{body_name}' not found"
                entry[f"{role}_connected"] = False
                all_connected = False
                continue

            shape = getattr(obj, "Shape", None)
            if shape is None or shape.isNull():
                entry[f"{role}_error"] = f"Body '{body_name}' has no shape"
                entry[f"{role}_connected"] = False
                all_connected = False
                continue

            try:
                dist = shape.distToShape(point_shape)[0]
            except Exception as exc:
                entry[f"{role}_error"] = f"distToShape failed: {exc}"
                entry[f"{role}_connected"] = False
                all_connected = False
                continue

            entry[f"{role}_distance_mm"] = round(dist, 4)
            entry[f"{role}_connected"] = dist <= tolerance_mm

            if dist > tolerance_mm:
                all_connected = False

        results.append(entry)

    return {
        "all_connected": all_connected,
        "tolerance_mm": tolerance_mm,
        "joint_count": len(joints),
        "joints": results,
    }


def _resolve_measure_ref(doc: Any, ref: Any) -> Any:
    """Resolve a measure_between reference to a Part shape or vertex.

    ``ref`` can be:
    - A list/tuple [x, y, z] → ``Part.Vertex(x, y, z)``
    - A string ``"BodyName.Face3"`` → sub-shape of that body's tip
    - A plain body name string → tip shape of that body
    """
    if isinstance(ref, (list, tuple)):
        if len(ref) != 3:
            raise ValueError(f"Point reference must have 3 coordinates, got {len(ref)}")
        return Part.Vertex(float(ref[0]), float(ref[1]), float(ref[2]))

    if not isinstance(ref, str):
        raise ValueError(f"Reference must be a string or [x,y,z] list, got {type(ref).__name__}")

    if "." in ref:
        body_name, sub_name = ref.split(".", 1)
        body = doc.getObject(body_name)
        if body is None:
            raise ValueError(f"Body '{body_name}' not found")
        tip = _get_tip(body)
        shape = tip.Shape
        try:
            return shape.getElement(sub_name)
        except Exception as exc:
            raise ValueError(f"Sub-shape '{sub_name}' not found on body '{body_name}'") from exc

    # Plain body name
    body = doc.getObject(ref)
    if body is None:
        raise ValueError(f"Object '{ref}' not found")
    tip = _get_tip(body)
    return tip.Shape


def measure_between(
    ref_a: Any,
    ref_b: Any,
    doc: str | None = None,
) -> dict[str, Any]:
    """Measure the minimum distance between two references."""
    d = _get_doc(doc)
    shape_a = _resolve_measure_ref(d, ref_a)
    shape_b = _resolve_measure_ref(d, ref_b)

    dts_result = shape_a.distToShape(shape_b)
    dist = dts_result[0]
    solutions = dts_result[1] if len(dts_result) > 1 else []
    # solutions is a list of (point_a, point_b) tuples
    pt_a = list(solutions[0][0]) if solutions else []
    pt_b = list(solutions[0][1]) if solutions else []

    return {
        "distance_mm": round(dist, 4),
        "point_a": [round(v, 4) for v in pt_a],
        "point_b": [round(v, 4) for v in pt_b],
    }


# ---------------------------------------------------------------------------
# Hole detection
# ---------------------------------------------------------------------------

def find_holes(
    body: str | None = None,
    doc: str | None = None,
    min_diameter: float = 0.0,
    max_diameter: float = 200.0,
) -> dict[str, Any]:
    """Find cylindrical holes in a body or any shape-bearing object.

    Returns structured info for each cylindrical face: face name, diameter,
    axis direction, center position, and depth along the axis.  The caller
    can group co-axial cylinders and match diameters to bolt sizes.

    Accepts both ``PartDesign::Body`` objects (resolved via ``_resolve_body``
    and ``_get_tip``) and non-Body shape holders like ``Part::Feature``
    (used by the orchestrator's self-verifying validator after importing
    a worker's STEP file via ``import_step``).  When ``body`` is a
    non-Body object name, the object's ``Shape`` is used directly.
    """
    d = _get_doc(doc)

    # Try body-resolution first for backward compat. If body is an
    # imported Part::Feature or similar shape holder, fall through to
    # direct shape access.
    #
    # We catch AttributeError in addition to ValueError because
    # _resolve_body succeeds on any object found by name (including
    # Part::Feature imports) but _get_tip then walks `body.Group`,
    # which doesn't exist on Part::Feature and raises AttributeError.
    # The fallback path below handles non-Body shape holders directly.
    shape = None
    resolved_name = None
    try:
        body_obj = _resolve_body(d, body)
        tip = _get_tip(body_obj)
        shape = tip.Shape
        resolved_name = body_obj.Name
    except (ValueError, AttributeError) as exc:
        if body is None:
            raise
        obj = d.getObject(body)
        if obj is None:
            raise ValueError(f"Object '{body}' not found in document '{d.Name}'") from exc
        if not hasattr(obj, "Shape") or obj.Shape is None:
            raise ValueError(f"Object '{body}' has no Shape attribute") from exc
        shape = obj.Shape
        resolved_name = obj.Name

    holes: list[dict[str, Any]] = []
    for i, face in enumerate(shape.Faces):
        surface = face.Surface
        surface_type = type(surface).__name__
        if surface_type != "Cylinder":
            continue

        radius = surface.Radius
        diameter = radius * 2
        if diameter < min_diameter or diameter > max_diameter:
            continue

        axis = surface.Axis
        center = surface.Center

        # Compute depth from vertex projections along the cylinder axis
        projections: list[float] = []
        for edge in face.Edges:
            for v in edge.Vertexes:
                vec = v.Point - center
                proj = vec.x * axis.x + vec.y * axis.y + vec.z * axis.z
                projections.append(proj)

        depth = (max(projections) - min(projections)) if projections else 0.0

        # Compute the midpoint along the axis (useful for positioning)
        if projections:
            mid_proj = (max(projections) + min(projections)) / 2
            mid_point = [
                center.x + axis.x * mid_proj,
                center.y + axis.y * mid_proj,
                center.z + axis.z * mid_proj,
            ]
        else:
            mid_point = [center.x, center.y, center.z]

        holes.append({
            "face": f"Face{i + 1}",
            "diameter_mm": round(diameter, 4),
            "radius_mm": round(radius, 4),
            "depth_mm": round(depth, 4),
            "axis": [round(axis.x, 6), round(axis.y, 6), round(axis.z, 6)],
            "center": [round(center.x, 4), round(center.y, 4), round(center.z, 4)],
            "midpoint": [round(v, 4) for v in mid_point],
        })

    return {
        "body": resolved_name,
        "hole_count": len(holes),
        "holes": holes,
    }


# ---------------------------------------------------------------------------
# Batch clearance check
# ---------------------------------------------------------------------------

def check_clearance(
    bodies: list[str] | None = None,
    threshold_mm: float = 0.5,
    doc: str | None = None,
) -> dict[str, Any]:
    """Check minimum distance between all body pairs.

    Returns violations (pairs closer than *threshold_mm*) and an ``all_clear``
    flag.  Uses ``distToShape()`` on each pair's tip shape.
    """
    d = _get_doc(doc)

    # Collect target bodies
    if bodies:
        body_objs = []
        for name in bodies:
            obj = d.getObject(name)
            if obj is None:
                raise ValueError(f"Body '{name}' not found")
            body_objs.append(obj)
    else:
        body_objs = [
            obj for obj in d.Objects
            if obj.TypeId == "PartDesign::Body"
        ]

    # Filter to bodies that have a tip with a shape
    valid: list[tuple[str, Any]] = []
    for obj in body_objs:
        try:
            tip = _get_tip(obj)
            if tip.Shape and not tip.Shape.isNull():
                valid.append((obj.Name, tip.Shape))
        except Exception:
            continue

    violations: list[dict[str, Any]] = []
    pairs_checked = 0

    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            name_a, shape_a = valid[i]
            name_b, shape_b = valid[j]
            pairs_checked += 1

            try:
                dts_result = shape_a.distToShape(shape_b)
            except Exception:
                # distToShape can fail on complex/intersecting geometry
                violations.append({
                    "body_a": name_a,
                    "body_b": name_b,
                    "distance_mm": 0.0,
                    "intersecting": True,
                    "point_a": [],
                    "point_b": [],
                    "error": "distToShape failed (likely intersecting)",
                })
                continue
            dist = dts_result[0]
            solutions = dts_result[1] if len(dts_result) > 1 else []
            intersecting = dist < 1e-6

            if dist < threshold_mm:
                pt_a = list(solutions[0][0]) if solutions else []
                pt_b = list(solutions[0][1]) if solutions else []
                violations.append({
                    "body_a": name_a,
                    "body_b": name_b,
                    "distance_mm": round(dist, 4),
                    "intersecting": intersecting,
                    "point_a": [round(v, 4) for v in pt_a],
                    "point_b": [round(v, 4) for v in pt_b],
                })

    return {
        "pairs_checked": pairs_checked,
        "threshold_mm": threshold_mm,
        "violation_count": len(violations),
        "violations": violations,
        "all_clear": len(violations) == 0,
    }


def check_swept_clearance(
    body: str,
    axis: list[float] | None = None,
    center: list[float] | None = None,
    angle_deg: float = 360.0,
    steps: int = 36,
    others: list[str] | None = None,
    threshold_mm: float = 0.5,
    doc: str | None = None,
) -> dict[str, Any]:
    """Rotate a copy of *body*'s shape through *steps* increments and check
    ``distToShape`` against other bodies at each angle.

    Returns per-pair minimum distance and the angle where the closest
    approach occurs.  No document modifications are made.
    """
    d = _get_doc(doc)
    if axis is None:
        axis = [0.0, 0.0, 1.0]
    if center is None:
        center = [0.0, 0.0, 0.0]

    # Rotating body
    rot_obj = d.getObject(body)
    if rot_obj is None:
        raise ValueError(f"Body '{body}' not found")
    rot_tip = _get_tip(rot_obj)
    if rot_tip.Shape is None or rot_tip.Shape.isNull():
        raise ValueError(f"Body '{body}' has no valid shape")
    rot_shape = rot_tip.Shape

    # Other bodies
    if others:
        other_objs = []
        for name in others:
            obj = d.getObject(name)
            if obj is None:
                raise ValueError(f"Body '{name}' not found")
            other_objs.append(obj)
    else:
        other_objs = [
            obj for obj in d.Objects
            if obj.TypeId == "PartDesign::Body" and obj.Name != body
        ]

    other_shapes: list[tuple[str, Any]] = []
    for obj in other_objs:
        try:
            tip = _get_tip(obj)
            if tip.Shape and not tip.Shape.isNull():
                other_shapes.append((obj.Name, tip.Shape))
        except Exception:
            continue

    axis_vec = FreeCAD.Vector(*axis)
    center_vec = FreeCAD.Vector(*center)

    # Track per-pair minimum distance
    pair_best: dict[str, dict[str, Any]] = {}
    for other_name, _ in other_shapes:
        pair_best[other_name] = {
            "min_distance_mm": float("inf"),
            "worst_angle_deg": 0.0,
            "intersecting": False,
            "point_a": [],
            "point_b": [],
        }

    for i in range(steps):
        angle = angle_deg * i / steps
        rot = FreeCAD.Rotation(axis_vec, angle)
        new_base = center_vec - rot.multVec(center_vec)
        shape_copy = rot_shape.copy()
        shape_copy.Placement = FreeCAD.Placement(new_base, rot)

        for other_name, other_shape in other_shapes:
            try:
                dts_result = shape_copy.distToShape(other_shape)
            except Exception:
                pair_best[other_name] = {
                    "min_distance_mm": 0.0,
                    "worst_angle_deg": round(angle, 4),
                    "intersecting": True,
                    "point_a": [],
                    "point_b": [],
                }
                continue
            dist = dts_result[0]
            solutions = dts_result[1] if len(dts_result) > 1 else []
            if dist < pair_best[other_name]["min_distance_mm"]:
                pt_a = list(solutions[0][0]) if solutions else []
                pt_b = list(solutions[0][1]) if solutions else []
                pair_best[other_name] = {
                    "min_distance_mm": round(dist, 4),
                    "worst_angle_deg": round(angle, 4),
                    "intersecting": dist < 1e-6,
                    "point_a": [round(v, 4) for v in pt_a],
                    "point_b": [round(v, 4) for v in pt_b],
                }

    violations: list[dict[str, Any]] = []
    for other_name, best in pair_best.items():
        if best["min_distance_mm"] < threshold_mm:
            violations.append({"other_body": other_name, **best})

    return {
        "body": body,
        "steps": steps,
        "angle_deg": angle_deg,
        "pairs_checked": len(other_shapes),
        "violation_count": len(violations),
        "violations": violations,
        "all_clear": len(violations) == 0,
    }


def assembly_audit(
    cluster_radius_mm: float = 1.0,
    isolation_radius_mm: float = 500.0,
    overlap_fraction: float = 0.8,
    expected_positions: dict[str, list[float]] | None = None,
    doc: str | None = None,
) -> dict[str, Any]:
    """Spatial coherence audit for multi-body assemblies.

    Returns all bodies with positions + bounding boxes, plus automatic
    anomaly warnings:
    - CLUSTER: N bodies within *cluster_radius_mm* of same position
    - ISOLATED: body farther than *isolation_radius_mm* from all neighbors
    - OVERLAP: two bodies with >*overlap_fraction* bounding box overlap
    - DRIFT: position differs >5mm from *expected_positions* dict
    """
    d = _get_doc(doc)

    # Collect body spatial data
    body_data: list[dict[str, Any]] = []
    for obj in d.Objects:
        if obj.TypeId != "PartDesign::Body":
            continue
        plc = obj.Placement
        pos = [round(plc.Base.x, 3), round(plc.Base.y, 3), round(plc.Base.z, 3)]
        entry: dict[str, Any] = {
            "name": obj.Name,
            "label": obj.Label,
            "position": pos,
        }
        try:
            tip = _get_tip(obj)
            if hasattr(tip, "Shape") and tip.Shape is not None:
                bb = tip.Shape.BoundBox
                entry["world_bbox"] = {
                    "min": [round(bb.XMin, 2), round(bb.YMin, 2), round(bb.ZMin, 2)],
                    "max": [round(bb.XMax, 2), round(bb.YMax, 2), round(bb.ZMax, 2)],
                }
                entry["size"] = [
                    round(bb.XLength, 2),
                    round(bb.YLength, 2),
                    round(bb.ZLength, 2),
                ]
        except Exception:
            pass
        body_data.append(entry)

    n = len(body_data)
    anomalies: list[dict[str, Any]] = []

    # Helper: Euclidean distance between two 3D points
    def _dist(a: list[float], b: list[float]) -> float:
        return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b, strict=False)))

    # Helper: bounding box overlap fraction (intersection volume / smaller volume)
    def _bbox_overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
        a_min, a_max = a["min"], a["max"]
        b_min, b_max = b["min"], b["max"]
        ix = max(0, min(a_max[0], b_max[0]) - max(a_min[0], b_min[0]))
        iy = max(0, min(a_max[1], b_max[1]) - max(a_min[1], b_min[1]))
        iz = max(0, min(a_max[2], b_max[2]) - max(a_min[2], b_min[2]))
        intersection = ix * iy * iz
        if intersection == 0:
            return 0.0
        vol_a = max(1e-9, (a_max[0] - a_min[0]) * (a_max[1] - a_min[1]) * (a_max[2] - a_min[2]))
        vol_b = max(1e-9, (b_max[0] - b_min[0]) * (b_max[1] - b_min[1]) * (b_max[2] - b_min[2]))
        return intersection / min(vol_a, vol_b)

    # CLUSTER detection: bodies within cluster_radius_mm of each other
    clusters: dict[int, list[int]] = {}  # cluster_id → [body indices]
    visited: set[int] = set()
    for i in range(n):
        if i in visited:
            continue
        group = [i]
        visited.add(i)
        for j in range(i + 1, n):
            if j in visited:
                continue
            if _dist(body_data[i]["position"], body_data[j]["position"]) <= cluster_radius_mm:
                group.append(j)
                visited.add(j)
        if len(group) > 1:
            clusters[i] = group

    for _cid, indices in clusters.items():
        names = [body_data[idx]["label"] for idx in indices]
        pos = body_data[indices[0]]["position"]
        anomalies.append({
            "type": "CLUSTER",
            "message": f"{len(names)} bodies at ~{pos}: {', '.join(names)}",
            "bodies": names,
            "position": pos,
        })

    # ISOLATED detection: body farther than isolation_radius_mm from all others
    for i in range(n):
        if n <= 1:
            break
        min_d = min(
            _dist(body_data[i]["position"], body_data[j]["position"])
            for j in range(n) if j != i
        )
        if min_d > isolation_radius_mm:
            anomalies.append({
                "type": "ISOLATED",
                "message": (
                    f"{body_data[i]['label']} at {body_data[i]['position']} "
                    f"is {round(min_d, 1)}mm from nearest neighbor"
                ),
                "body": body_data[i]["label"],
                "nearest_distance_mm": round(min_d, 1),
            })

    # OVERLAP detection: bounding box overlap > overlap_fraction
    for i in range(n):
        if "world_bbox" not in body_data[i]:
            continue
        for j in range(i + 1, n):
            if "world_bbox" not in body_data[j]:
                continue
            frac = _bbox_overlap(body_data[i]["world_bbox"], body_data[j]["world_bbox"])
            if frac > overlap_fraction:
                anomalies.append({
                    "type": "OVERLAP",
                    "message": (
                        f"{body_data[i]['label']} and {body_data[j]['label']} "
                        f"overlap {round(frac * 100, 1)}%"
                    ),
                    "body_a": body_data[i]["label"],
                    "body_b": body_data[j]["label"],
                    "overlap_pct": round(frac * 100, 1),
                })

    # Auto-use registered placement plan for DRIFT detection
    if expected_positions is None and _placement_plan:
        expected_positions = {
            label: entry["position"]
            for label, entry in _placement_plan.items()
            if "position" in entry
        }

    # DRIFT detection: position vs expected_positions
    if expected_positions:
        for bd in body_data:
            exp = expected_positions.get(bd["label"]) or expected_positions.get(bd["name"])
            if exp is None:
                continue
            drift = _dist(bd["position"], exp)
            if drift > 5.0:
                anomalies.append({
                    "type": "DRIFT",
                    "message": (
                        f"{bd['label']} at {bd['position']} vs expected "
                        f"{exp} — drift {round(drift, 1)}mm"
                    ),
                    "body": bd["label"],
                    "actual": bd["position"],
                    "expected": exp,
                    "drift_mm": round(drift, 1),
                })

    return {
        "body_count": n,
        "bodies": body_data,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
    }


COMMAND_HANDLERS: dict[str, Any] = {
    "new_document": new_document,
    "new_body": new_body,
    "get_model_tree": get_model_tree,
    "undo": undo,
    "redo": redo,
    "new_sketch": new_sketch,
    "sketch_rect": sketch_rect,
    "sketch_circle": sketch_circle,
    "sketch_line": sketch_line,
    "sketch_arc": sketch_arc,
    "sketch_bspline": sketch_bspline,
    "sketch_constrain": sketch_constrain,
    "sketch_populate": sketch_populate,
    "close_sketch": close_sketch,
    "pad": pad,
    "resolve_pocket_direction": resolve_pocket_direction,
    "pocket": pocket,
    "revolution": revolution,
    "polar_pattern": polar_pattern,
    "mirror": mirror,
    "linear_pattern": linear_pattern,
    "thickness": thickness,
    "draft": draft,
    "hole": hole,
    "sweep": sweep,
    "helix": helix,
    "loft": loft,
    "fillet": fillet,
    "chamfer": chamfer,
    "get_selection": get_selection,
    "get_dimensions": get_dimensions,
    "get_body_topology": get_body_topology,
    "find_edges": find_edges,
    "define_selection": define_selection,
    "resolve_selection": resolve_selection,
    "list_selections": list_selections,
    "delete_selection": delete_selection,
    "screenshot": screenshot,
    "set_camera": set_camera,
    "get_camera": get_camera,
    "export": export,
    "export_body": export_body,
    "import_step": import_step,
    "export_sim_package": export_sim_package,
    "set_visibility": set_visibility,
    "assembly_create": assembly_create,
    "assembly_add_part": assembly_add_part,
    "assembly_add_joint": assembly_add_joint,
    "assembly_solve": assembly_solve,
    "assembly_drive_joint": assembly_drive_joint,
    "assembly_check_interference": assembly_check_interference,
    "assembly_get_links": assembly_get_links,
    "assembly_set_placements": assembly_set_placements,
    "assembly_get_placements": assembly_get_placements,
    "assembly_animate": assembly_animate,
    "assembly_animate_stop": assembly_animate_stop,
    "set_placement": set_placement,
    "delete_objects": delete_objects,
    "cleanup_orphans": cleanup_orphans,
    "freecad_info": freecad_info,
    "create_primitive": create_primitive,
    "create_primitives": create_primitives,
    "check_joint_connectivity": check_joint_connectivity,
    "measure_between": measure_between,
    "find_holes": find_holes,
    "check_clearance": check_clearance,
    "check_swept_clearance": check_swept_clearance,
    "register_placement_plan": register_placement_plan,
    "clear_placement_plan": clear_placement_plan,
    "assembly_audit": assembly_audit,
    "save_document": save_document,
}
