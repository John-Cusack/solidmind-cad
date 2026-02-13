from __future__ import annotations

from typing import Any

from server.geometry_constraints import ConstraintGraphBuilder, ConstraintGraph
from server.geometry_ir import (
    EIR,
    EIRBuilder,
    GIR,
    GIRBuilder,
    Invariant,
    Quantity,
    Point3D,
    Vector3D,
    compute_eir_hash,
    compute_gir_hash,
)
from server.geometry_planner import StrategyPlanner
from server.feature_support import load_geometry_capabilities


def plan_geometry(spec: dict[str, Any]) -> dict[str, Any]:
    """Plan geometry from finalized spec using generic geometry engine.

    Orchestrates: constraints -> GIR -> strategy -> EIR -> hashes.

    Args:
        spec: Finalized specification document

    Returns:
        Dictionary containing gir, eir, notices, metadata.
    """
    notices: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "engine_mode": "generic_v1",
        "engine_version": "1.0",
    }

    # Step 1: Build constraint graph from spec
    cg_builder = ConstraintGraphBuilder()
    constraint_graph = cg_builder.build_from_spec(spec)

    # Step 2: Extract GIR from spec + constraints
    gir = _extract_gir(spec, constraint_graph, notices)
    gir_hash = compute_gir_hash(gir)
    metadata["gir_hash"] = gir_hash

    # Step 3: Select strategy
    gir_dict = _gir_to_dict(gir)
    capabilities = load_geometry_capabilities()
    planner = StrategyPlanner(capabilities)
    strategies = planner.select_strategy(gir_dict, backend="freecad")
    metadata["strategy"] = strategies.primary.strategy_name
    metadata["strategy_confidence"] = strategies.primary.confidence

    # Step 4: Generate EIR from GIR + strategy
    eir = _generate_eir(gir, strategies.primary.strategy_name, notices)
    eir_hash = compute_eir_hash(eir)
    metadata["eir_hash"] = eir_hash

    eir_dict = _eir_to_dict(eir)

    return {
        "gir": gir_dict,
        "eir": eir_dict,
        "notices": notices,
        "metadata": metadata,
    }


def _extract_gir(
    spec: dict[str, Any],
    constraint_graph: ConstraintGraph,
    notices: list[dict[str, Any]],
) -> GIR:
    """Extract GIR from spec geometry fields and constraint graph."""
    gir_builder = GIRBuilder()
    frame_id = gir_builder.add_global_frame()

    envelope = spec.get("envelope", {})
    geometry = spec.get("geometry", {})

    # Extract envelope dimensions
    dims = _extract_envelope_dims(envelope)

    has_envelope = bool(dims)

    if has_envelope:
        # Create sketch profile for rectangular base
        sketch = _add_box_sketch(gir_builder, dims, frame_id)

        # Create extrude intent from sketch + height
        height = dims.get("height")
        if height is None:
            height = Quantity(value=10.0, unit="mm")
            notices.append({
                "code": "DEFAULT_HEIGHT",
                "severity": "info",
                "message": "No height specified, using default 10mm",
            })

        extrude = gir_builder.add_extrude_intent(
            base_profile_id=sketch.id,
            distance=height,
            operation_type="add",
            frame_id=frame_id,
            traces_to=["envelope"],
        )

        # Extract hole features
        hole_features = geometry.get("hole_features", [])
        hole_intents = _extract_holes(
            gir_builder, hole_features, extrude.id, frame_id, notices,
        )

        # Extract blend features (fillets/chamfers)
        blend_specs = geometry.get("fillets", [])
        chamfer_specs = geometry.get("chamfers", [])
        _extract_blends(
            gir_builder, blend_specs, chamfer_specs, extrude.id, frame_id, notices,
        )
    else:
        notices.append({
            "code": "NO_ENVELOPE",
            "severity": "warning",
            "message": "No envelope dimensions found in spec",
        })

    gir_builder.set_metadata("source", "spec")
    return gir_builder.build()


def _extract_envelope_dims(envelope: dict[str, Any]) -> dict[str, Quantity]:
    """Extract envelope dimensions as Quantity objects."""
    dims: dict[str, Quantity] = {}
    for dim_name in ["length", "width", "height"]:
        dim_value = envelope.get(dim_name)
        if isinstance(dim_value, dict):
            value = dim_value.get("value")
            unit = dim_value.get("unit", "mm")
            if value is not None:
                dims[dim_name] = Quantity(value=float(value), unit=unit)
    return dims


def _add_box_sketch(
    gir_builder: GIRBuilder,
    dims: dict[str, Quantity],
    frame_id: str,
) -> Any:
    """Add a rectangular sketch profile from envelope dimensions."""
    length = dims.get("length", Quantity(value=100.0, unit="mm"))
    width = dims.get("width", length)

    # Centered rectangle elements
    lv = length.value
    wv = width.value
    elements: list[dict[str, Any]] = [
        {
            "type": "rect",
            "x": -lv / 2,
            "y": -wv / 2,
            "width": lv,
            "height": wv,
        }
    ]

    constraints: list[dict[str, Any]] = [
        {"type": "Symmetric", "axis": "X"},
        {"type": "Symmetric", "axis": "Y"},
    ]

    return gir_builder.add_sketch_profile(
        plane="XY",
        elements=elements,
        constraints=constraints,
        frame_id=frame_id,
        traces_to=["envelope"],
    )


def _extract_holes(
    gir_builder: GIRBuilder,
    hole_features: list[dict[str, Any]],
    parent_feature_id: str,
    frame_id: str,
    notices: list[dict[str, Any]],
) -> list[Any]:
    """Extract hole intents from spec hole_features."""
    hole_intents = []
    for hole_spec in hole_features:
        if not isinstance(hole_spec, dict):
            continue

        hole_id = str(hole_spec.get("id", "hole"))

        # Diameter
        diam = hole_spec.get("diameter", {})
        if not isinstance(diam, dict) or diam.get("value") is None:
            notices.append({
                "code": "MISSING_HOLE_DIAMETER",
                "severity": "warning",
                "message": f"Hole '{hole_id}' missing diameter, skipping",
            })
            continue
        diameter = Quantity(value=float(diam["value"]), unit=diam.get("unit", "mm"))

        # Depth
        depth_spec = hole_spec.get("depth", {})
        if isinstance(depth_spec, dict) and depth_spec.get("value") is not None:
            depth = Quantity(
                value=float(depth_spec["value"]),
                unit=depth_spec.get("unit", "mm"),
            )
        else:
            depth = Quantity(value=0.0, unit="mm")  # through-all sentinel

        # Hole type
        hole_type = str(hole_spec.get("type", "simple"))
        if depth.value == 0.0:
            hole_type = "through"

        # Location
        loc = hole_spec.get("location", {})
        location = _parse_location(loc)

        # Face reference token
        face_ref = f"ref:{parent_feature_id}:top_face"

        intent = gir_builder.add_hole_intent(
            diameter=diameter,
            depth=depth,
            hole_type=hole_type,
            location=location,
            face_reference=face_ref,
            axis=Vector3D(x=0.0, y=0.0, z=-1.0),
            frame_id=frame_id,
            traces_to=[f"hole:{hole_id}"],
        )
        hole_intents.append(intent)

    return hole_intents


def _parse_location(loc: dict[str, Any]) -> Point3D:
    """Parse a location dict into a Point3D."""
    def _coord(key: str) -> Quantity:
        val = loc.get(key, {})
        if isinstance(val, dict) and val.get("value") is not None:
            return Quantity(value=float(val["value"]), unit=val.get("unit", "mm"))
        if isinstance(val, (int, float)):
            return Quantity(value=float(val), unit="mm")
        return Quantity(value=0.0, unit="mm")

    return Point3D(x=_coord("x"), y=_coord("y"), z=_coord("z"))


def _extract_blends(
    gir_builder: GIRBuilder,
    fillet_specs: list[dict[str, Any]],
    chamfer_specs: list[dict[str, Any]],
    parent_feature_id: str,
    frame_id: str,
    notices: list[dict[str, Any]],
) -> None:
    """Extract blend intents (fillets and chamfers) from spec."""
    for fillet in fillet_specs:
        if not isinstance(fillet, dict):
            continue
        radius = fillet.get("radius", {})
        if isinstance(radius, dict) and radius.get("value") is not None:
            r = Quantity(value=float(radius["value"]), unit=radius.get("unit", "mm"))
        elif isinstance(radius, (int, float)):
            r = Quantity(value=float(radius), unit="mm")
        else:
            notices.append({
                "code": "MISSING_FILLET_RADIUS",
                "severity": "warning",
                "message": "Fillet missing radius, skipping",
            })
            continue

        edge_refs = _get_edge_refs(fillet, parent_feature_id)
        gir_builder.add_blend_intent(
            blend_type="fillet",
            edge_references=edge_refs,
            radius=r,
            frame_id=frame_id,
            traces_to=["blend:fillet"],
        )

    for chamfer in chamfer_specs:
        if not isinstance(chamfer, dict):
            continue
        dist = chamfer.get("distance", chamfer.get("size", {}))
        if isinstance(dist, dict) and dist.get("value") is not None:
            d = Quantity(value=float(dist["value"]), unit=dist.get("unit", "mm"))
        elif isinstance(dist, (int, float)):
            d = Quantity(value=float(dist), unit="mm")
        else:
            notices.append({
                "code": "MISSING_CHAMFER_SIZE",
                "severity": "warning",
                "message": "Chamfer missing size, skipping",
            })
            continue

        edge_refs = _get_edge_refs(chamfer, parent_feature_id)
        gir_builder.add_blend_intent(
            blend_type="chamfer",
            edge_references=edge_refs,
            distance=d,
            frame_id=frame_id,
            traces_to=["blend:chamfer"],
        )


def _get_edge_refs(blend_spec: dict[str, Any], parent_feature_id: str) -> list[str]:
    """Extract edge references from a blend spec."""
    edges = blend_spec.get("edges", [])
    if edges:
        return [str(e) for e in edges]
    # Default: reference all vertical edges of parent
    edge_selector = blend_spec.get("edge_selector", "vertical")
    return [f"ref:{parent_feature_id}:{edge_selector}_edges"]


# ---------------------------------------------------------------------------
# EIR generation
# ---------------------------------------------------------------------------

def _generate_eir(
    gir: GIR,
    strategy: str,
    notices: list[dict[str, Any]],
) -> EIR:
    """Translate GIR feature graph to ordered EIR operation DAG."""
    eir_builder = EIRBuilder()
    eir_builder.set_metadata("strategy", strategy)

    sketch_op_ids: dict[str, str] = {}  # feature_id -> op_id
    pad_op_ids: dict[str, str] = {}     # feature_id -> op_id

    for feature in gir.features:
        if feature.type == "sketch_profile":
            op = eir_builder.add_operation(
                op_type="create_sketch",
                inputs={
                    "body": "Body",
                    "plane": feature.plane,
                    "elements": feature.elements,
                    "constraints": feature.constraints,
                },
                invariants=[
                    Invariant(type="sketch_valid", scope="local"),
                ],
                feature_provenance_id=feature.id,
            )
            sketch_op_ids[feature.id] = op.id

        elif feature.type == "extrude_intent":
            depends = []
            sketch_op = sketch_op_ids.get(feature.base_profile_id)
            if sketch_op:
                depends.append(sketch_op)

            # Resolve sketch name from dependency
            sketch_name = f"Sketch"

            op = eir_builder.add_operation(
                op_type="pad",
                inputs={
                    "sketch": sketch_name,
                    "length": feature.distance.value,
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="solid_created", scope="global"),
                    Invariant(
                        type="dimension_check",
                        threshold=feature.distance.value,
                        scope="z_extent",
                    ),
                ],
                feature_provenance_id=feature.id,
            )
            pad_op_ids[feature.id] = op.id

        elif feature.type == "hole_intent":
            depends = list(pad_op_ids.values())

            op = eir_builder.add_operation(
                op_type="hole",
                inputs={
                    "face": feature.face_reference or "Face6",
                    "diameter": feature.diameter.value,
                    "depth": feature.depth.value,
                    "hole_type": "ThroughAll" if feature.hole_type == "through" else "Dimension",
                },
                depends_on=depends,
                invariants=[
                    Invariant(
                        type="hole_diameter",
                        threshold=feature.diameter.value,
                        scope="local",
                    ),
                ],
                feature_provenance_id=feature.id,
            )

        elif feature.type == "blend_intent":
            # Blends depend on all prior geometry ops
            depends = list(pad_op_ids.values())

            if feature.blend_type == "fillet":
                op = eir_builder.add_operation(
                    op_type="fillet",
                    inputs={
                        "radius": feature.radius.value if feature.radius else 1.0,
                        "edges": feature.edge_references,
                    },
                    depends_on=depends,
                    invariants=[
                        Invariant(type="blend_applied", scope="local"),
                    ],
                    feature_provenance_id=feature.id,
                )
            elif feature.blend_type == "chamfer":
                op = eir_builder.add_operation(
                    op_type="chamfer",
                    inputs={
                        "size": feature.distance.value if feature.distance else 1.0,
                        "edges": feature.edge_references,
                    },
                    depends_on=depends,
                    invariants=[
                        Invariant(type="blend_applied", scope="local"),
                    ],
                    feature_provenance_id=feature.id,
                )

        elif feature.type == "revolve_intent":
            depends = []
            sketch_op = sketch_op_ids.get(feature.base_profile_id)
            if sketch_op:
                depends.append(sketch_op)

            axis_map = {
                (0, 1, 0): "V",
                (1, 0, 0): "H",
                (0, 0, 1): "Base_Z",
            }
            axis_key = (
                int(feature.axis.x),
                int(feature.axis.y),
                int(feature.axis.z),
            )
            axis_name = axis_map.get(axis_key, "V")

            op = eir_builder.add_operation(
                op_type="revolve",
                inputs={
                    "sketch": "Sketch",
                    "axis": axis_name,
                    "angle": feature.angle.value,
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="solid_created", scope="global"),
                ],
                feature_provenance_id=feature.id,
            )
            pad_op_ids[feature.id] = op.id

        elif feature.type == "sweep_intent":
            depends = []
            profile_op = sketch_op_ids.get(feature.profile_id)
            if profile_op:
                depends.append(profile_op)
            spine_op = sketch_op_ids.get(feature.spine_id)
            if spine_op:
                depends.append(spine_op)

            subtractive = feature.operation_type == "cut"

            op = eir_builder.add_operation(
                op_type="sweep",
                inputs={
                    "profile_sketch": "Sketch",
                    "spine_sketch": "Sketch",
                    "subtractive": subtractive,
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="solid_created", scope="global"),
                ],
                feature_provenance_id=feature.id,
            )
            pad_op_ids[feature.id] = op.id

        elif feature.type == "loft_intent":
            depends = []
            for sid in feature.section_ids:
                sec_op = sketch_op_ids.get(sid)
                if sec_op:
                    depends.append(sec_op)

            subtractive = feature.operation_type == "cut"

            op = eir_builder.add_operation(
                op_type="loft",
                inputs={
                    "sketches": [f"Sketch" for _ in feature.section_ids],
                    "ruled": feature.ruled,
                    "closed": feature.closed,
                    "subtractive": subtractive,
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="solid_created", scope="global"),
                ],
                feature_provenance_id=feature.id,
            )
            pad_op_ids[feature.id] = op.id

        elif feature.type == "pattern_intent":
            depends = []
            for fid in feature.feature_ids:
                if fid in pad_op_ids:
                    depends.append(pad_op_ids[fid])

            op = eir_builder.add_operation(
                op_type="polar_pattern",
                inputs={
                    "features": feature.feature_ids,
                    "occurrences": feature.count,
                    "axis": "Base_Z",
                    "angle": feature.total_angle.value if feature.total_angle else 360.0,
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="pattern_count", threshold=float(feature.count), scope="global"),
                ],
                feature_provenance_id=feature.id,
            )

        elif feature.type == "primitive":
            # Legacy primitive support - emit as sketch + pad
            dims = feature.dimensions
            if feature.primitive_type == "box" and dims:
                length = dims.get("length", Quantity(100.0, "mm"))
                width = dims.get("width", length)
                height = dims.get("height", Quantity(10.0, "mm"))

                sketch_op = eir_builder.add_operation(
                    op_type="create_sketch",
                    inputs={
                        "body": "Body",
                        "plane": "XY",
                        "elements": [{"type": "rect", "x": -length.value / 2, "y": -width.value / 2, "width": length.value, "height": width.value}],
                    },
                    feature_provenance_id=feature.id,
                )

                pad_op = eir_builder.add_operation(
                    op_type="pad",
                    inputs={"sketch": "Sketch", "length": height.value},
                    depends_on=[sketch_op.id],
                    feature_provenance_id=feature.id,
                )
                pad_op_ids[feature.id] = pad_op.id

    return eir_builder.build()


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _gir_to_dict(gir: GIR) -> dict[str, Any]:
    """Serialize GIR to dict for JSON response."""
    frames = []
    for frame in gir.frames:
        frames.append({"id": frame.id, "type": frame.type, "parent": frame.parent})

    features = []
    for f in gir.features:
        feat: dict[str, Any] = {"id": f.id, "type": f.type}
        if hasattr(f, "traces_to") and f.traces_to:
            feat["traces_to"] = list(f.traces_to)
        if f.type == "sketch_profile":
            feat["plane"] = f.plane
            feat["elements"] = f.elements
        elif f.type == "extrude_intent":
            feat["base_profile_id"] = f.base_profile_id
            feat["distance"] = {"value": f.distance.value, "unit": f.distance.unit}
            feat["operation_type"] = f.operation_type
        elif f.type == "hole_intent":
            feat["diameter"] = {"value": f.diameter.value, "unit": f.diameter.unit}
            feat["depth"] = {"value": f.depth.value, "unit": f.depth.unit}
            feat["hole_type"] = f.hole_type
            feat["face_reference"] = f.face_reference
        elif f.type == "blend_intent":
            feat["blend_type"] = f.blend_type
            feat["edge_references"] = f.edge_references
            if f.radius:
                feat["radius"] = {"value": f.radius.value, "unit": f.radius.unit}
            if f.distance:
                feat["distance"] = {"value": f.distance.value, "unit": f.distance.unit}
        elif f.type == "revolve_intent":
            feat["base_profile_id"] = f.base_profile_id
            feat["angle"] = {"value": f.angle.value, "unit": f.angle.unit}
        elif f.type == "sweep_intent":
            feat["profile_id"] = f.profile_id
            feat["spine_id"] = f.spine_id
            feat["operation_type"] = f.operation_type
        elif f.type == "loft_intent":
            feat["section_ids"] = list(f.section_ids)
            feat["operation_type"] = f.operation_type
            feat["ruled"] = f.ruled
            feat["closed"] = f.closed
        elif f.type == "pattern_intent":
            feat["pattern_type"] = f.pattern_type
            feat["count"] = f.count
            feat["feature_ids"] = f.feature_ids
        elif f.type == "primitive":
            feat["primitive_type"] = f.primitive_type
            feat["dimensions"] = {
                k: {"value": v.value, "unit": v.unit}
                for k, v in f.dimensions.items()
            }
        features.append(feat)

    return {
        "gir_version": gir.gir_version,
        "frames": frames,
        "features": features,
        "metadata": dict(gir.metadata),
    }


def _eir_to_dict(eir: EIR) -> dict[str, Any]:
    """Serialize EIR to dict for JSON response."""
    operations = []
    for op in eir.operations:
        op_dict: dict[str, Any] = {
            "id": op.id,
            "op_type": op.op_type,
            "inputs": op.inputs,
            "depends_on": op.depends_on,
        }
        if op.invariants:
            op_dict["invariants"] = [
                {"type": inv.type, "threshold": inv.threshold, "scope": inv.scope}
                for inv in op.invariants
            ]
        if op.feature_provenance_id:
            op_dict["feature_provenance_id"] = op.feature_provenance_id
        operations.append(op_dict)

    return {
        "eir_version": eir.eir_version,
        "operations": operations,
        "dependency_graph": dict(eir.dependency_graph),
        "metadata": dict(eir.metadata),
    }
