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


def new_body(doc: str | None = None, name: str = "Body") -> dict[str, Any]:
    """Create a PartDesign Body in the given document."""
    logger.info("new_body: name=%s", name)
    d = _get_doc(doc)
    body = d.addObject("PartDesign::Body", name)
    d.recompute()
    logger.info("new_body: created %s", body.Name)
    return {"name": body.Name, "label": body.Label}


def get_model_tree(doc: str | None = None) -> dict[str, Any]:
    """Return the feature tree of the document."""
    d = _get_doc(doc)
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
        sketch.Support = [(tip, plane)]
        sketch.MapMode = "FlatFace"

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

    body = _find_parent_body(d, sk)
    pad_obj = d.addObject("PartDesign::Pad", "Pad")
    body.addObject(pad_obj)
    pad_obj.Profile = sk
    pad_obj.Length = length
    pad_obj.Midplane = symmetric
    pad_obj.Reversed = reversed

    result = _recompute_and_check(d, pad_obj, body=body)
    logger.info("pad: created %s", pad_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
    return result


def pocket(
    sketch: str,
    length: float = 0.0,
    pocket_type: str = "Dimension",
    reversed: bool = False,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Cut a pocket from a sketch.

    ``pocket_type``: "Dimension", "ThroughAll", "ToFirst", "ToLast".
    """
    logger.info("pocket: sketch=%s length=%s type=%s reversed=%s", sketch, length, pocket_type, reversed)
    d = _get_doc(doc)
    sk = _get_sketch(d, sketch)

    body = _find_parent_body(d, sk)
    pocket_obj = d.addObject("PartDesign::Pocket", "Pocket")
    body.addObject(pocket_obj)
    pocket_obj.Profile = sk
    pocket_obj.Type = _pocket_type_enum(pocket_type)
    if pocket_type == "Dimension":
        pocket_obj.Length = length
    pocket_obj.Reversed = reversed

    result = _recompute_and_check(d, pocket_obj, body=body)
    logger.info("pocket: created %s", pocket_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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
    sk.Support = [(body_obj, face)]
    sk.MapMode = "FlatFace"

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

    result = _recompute_and_check(d, hole_obj, body=body_obj)
    logger.info("hole: created %s", hole_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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

    result = _recompute_and_check(d, fillet_obj, body=body_obj)
    logger.info("fillet: created %s", fillet_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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

    result = _recompute_and_check(d, rev_obj, body=body)
    logger.info("revolution: created %s", rev_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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
        pattern_obj.Axis = (axis_obj, [""])

    result = _recompute_and_check(d, pattern_obj, body=body_obj)
    logger.info("polar_pattern: created %s", pattern_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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
    pipe_obj.Spine = (sk_spine, ["Edge1"])

    result = _recompute_and_check(d, pipe_obj, body=body)
    logger.info("sweep: created %s", pipe_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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

    result = _recompute_and_check(d, helix_obj, body=body)
    logger.info("helix: created %s", helix_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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

    result = _recompute_and_check(d, loft_obj, body=body)
    logger.info("loft: created %s", loft_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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

    result = _recompute_and_check(d, chamfer_obj, body=body_obj)
    logger.info("chamfer: created %s", chamfer_obj.Name)
    if verify:
        result["verification_images"] = _capture_verification_views(d)
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
    """Return all faces and edges on the body's tip shape with geometric properties."""
    d = _get_doc(doc)
    body_obj = _resolve_body(d, body)
    tip = _get_tip(body_obj)
    shape = tip.Shape

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
    for face_info, face in zip(faces, shape.Faces):
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
        except Exception:
            raise ValueError(f"Face '{on_face}' not found on shape")
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

    view = FreeCADGui.ActiveDocument.ActiveView

    # Compute model center and bbox diagonal
    bbox = _model_bounding_box(doc)
    if target_point is None:
        center = (
            (bbox.XMin + bbox.XMax) / 2,
            (bbox.YMin + bbox.YMax) / 2,
            (bbox.ZMin + bbox.ZMax) / 2,
        )
    else:
        center = target_point

    diagonal = bbox.DiagonalLength if bbox.DiagonalLength > 0 else 100.0
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
        from pivy.coin import SbVec3f  # type: ignore[import-untyped]
        cam = view.getCameraNode()
        cam.position.setValue(SbVec3f(cam_pos[0], cam_pos[1], cam_pos[2]))
        cam.pointAt(SbVec3f(center[0], center[1], center[2]), SbVec3f(up[0], up[1], up[2]))
        if near_clip is not None:
            cam.nearDistance.setValue(near_clip)
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
) -> list[dict[str, Any]]:
    """Capture 3 verification views: iso, front, top."""
    views: list[dict[str, Any]] = []
    for view_name in ("iso", "front", "top"):
        direction = _PRESET_DIRECTIONS[view_name]
        try:
            img = _capture_image(doc, direction=direction, width=width, height=height)
            img["view"] = view_name
            views.append(img)
        except Exception as e:
            logger.warning("Failed to capture %s view: %s", view_name, e)
    return views


def _model_bounding_box(doc: Any) -> Any:
    """Get the combined bounding box of all shapes in the document."""
    combined = FreeCAD.BoundBox()
    for obj in doc.Objects:
        if hasattr(obj, "Shape") and obj.Shape is not None and not obj.Shape.isNull():
            combined.add(obj.Shape.BoundBox)
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
        except Exception:
            raise ValueError(f"Face '{target}' not found on shape")
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
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
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
) -> dict[str, Any]:
    """Smart screenshot: compute view from target, capture image."""
    d = _get_doc(doc)
    up_vec = tuple(up) if up else (0.0, 0.0, 1.0)

    target_point, resolved_dir = _resolve_target(target, d)

    if direction is not None:
        cam_dir = tuple(direction)
    elif resolved_dir is not None:
        cam_dir = resolved_dir
    else:
        cam_dir = _PRESET_DIRECTIONS["iso"]

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
    return {
        "ok": True,
        "width": width,
        "height": height,
        **img,
    }


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
    view = FreeCADGui.ActiveDocument.ActiveView

    if fit_all:
        FreeCADGui.SendMsgToActiveView("ViewFit")
        _process_qt_events()

    try:
        from pivy.coin import SbVec3f  # type: ignore[import-untyped]
        cam = view.getCameraNode()

        if position is not None:
            cam.position.setValue(SbVec3f(position[0], position[1], position[2]))

        if target is not None:
            up_vec = up if up else [0.0, 0.0, 1.0]
            cam.pointAt(
                SbVec3f(target[0], target[1], target[2]),
                SbVec3f(up_vec[0], up_vec[1], up_vec[2]),
            )

        if near_clip is not None:
            cam.nearDistance.setValue(near_clip)

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

    _get_doc(doc)  # validate doc exists
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
        raise RuntimeError(f"Failed to read camera: {e}")


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _process_qt_events() -> None:
    """Flush pending Qt events so viewport redraws complete."""
    try:
        from PySide2.QtWidgets import QApplication  # type: ignore[import-untyped]
    except ImportError:
        from PySide6.QtWidgets import QApplication  # type: ignore[import-untyped]
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
    except Exception:
        pass
    return "; ".join(parts)


def _recompute_and_check(doc: Any, obj: Any, body: Any | None = None) -> dict[str, Any]:
    """Recompute the document and verify the feature is valid.

    If recompute fails (feature enters an invalid state), the broken feature
    is removed from the document and a ``ValueError`` is raised so the error
    propagates back to the MCP client.

    If ``body`` is provided, computes a shape digest and delta after success.
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


# ---------------------------------------------------------------------------
# Command registry — maps cmd strings to handler functions
# ---------------------------------------------------------------------------

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
    "close_sketch": close_sketch,
    "pad": pad,
    "pocket": pocket,
    "revolution": revolution,
    "polar_pattern": polar_pattern,
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
}
