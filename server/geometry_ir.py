from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from server.jcs import canonicalize as jcs_canonicalize


@dataclass(frozen=True, slots=True)
class Tolerance:
    model: str
    lower: float | None = None
    upper: float | None = None
    value: float | None = None


@dataclass(frozen=True, slots=True)
class Quantity:
    value: float
    unit: str
    tol: Tolerance | None = None


@dataclass(frozen=True, slots=True)
class Point3D:
    x: Quantity
    y: Quantity
    z: Quantity


@dataclass(frozen=True, slots=True)
class Vector3D:
    x: float
    y: float
    z: float


@dataclass(frozen=True, slots=True)
class Transform:
    rotation: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    translation: Vector3D


@dataclass(frozen=True, slots=True)
class Frame:
    id: str
    type: str
    parent: str | None = None
    transform: Transform | None = None


@dataclass(frozen=True, slots=True)
class ReferenceToken:
    token: str
    origin_op_id: str
    selector: dict[str, Any] | None = None
    invariants: dict[str, Any] | None = None
    tie_break_key: float | None = None


@dataclass(frozen=True, slots=True)
class Invariant:
    type: str
    threshold: float | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class Notice:
    code: str
    severity: str
    message: str
    context: dict[str, Any] | None = None
    recommended_actions: list[str] | None = None


@dataclass(frozen=True, slots=True)
class PrimitiveIntent:
    id: str
    primitive_type: str
    dimensions: dict[str, Quantity]
    type: str = "primitive"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class SketchProfileIntent:
    id: str
    plane: str
    elements: list[dict[str, Any]]
    constraints: list[dict[str, Any]] = field(default_factory=list)
    type: str = "sketch_profile"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class ExtrudeIntent:
    id: str
    base_profile_id: str
    distance: Quantity
    operation_type: str
    direction: Vector3D | None = None
    type: str = "extrude_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class RevolveIntent:
    id: str
    base_profile_id: str
    axis: Vector3D
    axis_point: Point3D
    angle: Quantity
    operation_type: str
    type: str = "revolve_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class HoleIntent:
    id: str
    diameter: Quantity
    depth: Quantity
    hole_type: str
    location: Point3D
    face_reference: str | None = None
    axis: Vector3D | None = None
    type: str = "hole_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class PatternIntent:
    id: str
    pattern_type: str
    feature_ids: list[str]
    count: int
    axis: Vector3D | None = None
    center_point: Point3D | None = None
    spacing: Quantity | None = None
    total_angle: Quantity | None = None
    type: str = "pattern_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class SweepIntent:
    id: str
    profile_id: str
    spine_id: str
    operation_type: str
    type: str = "sweep_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class LoftIntent:
    id: str
    section_ids: list[str]
    operation_type: str
    ruled: bool = False
    closed: bool = False
    type: str = "loft_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class BlendIntent:
    id: str
    blend_type: str
    edge_references: list[str]
    radius: Quantity | None = None
    distance: Quantity | None = None
    type: str = "blend_intent"
    frame_id: str | None = None
    traces_to: list[str] = field(default_factory=list)
    x_freecad: dict[str, Any] | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


FeatureIntent = (
    PrimitiveIntent
    | SketchProfileIntent
    | ExtrudeIntent
    | RevolveIntent
    | SweepIntent
    | LoftIntent
    | HoleIntent
    | PatternIntent
    | BlendIntent
)


class CompilerStatus:
    COMPILED = "compiled"
    LOWERED = "lowered"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class GIR:
    gir_version: str = "1.0"
    frames: list[Frame] = field(default_factory=list)
    features: list[FeatureIntent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompiledOp:
    id: str
    op_type: str
    inputs: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    invariants: list[Invariant] = field(default_factory=list)
    retry_policy: dict[str, Any] | None = None
    local_frame: str | None = None
    feature_provenance_id: str | None = None
    phase_id: str | None = None
    reference_support_type: str | None = None
    topology_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class CompilerResult:
    status: str
    ops: list[CompiledOp] | None
    notices: list[Notice]
    fallback_ops: list[CompiledOp] | None = None


@dataclass(frozen=True, slots=True)
class EIR:
    eir_version: str = "1.0"
    operations: list[CompiledOp] = field(default_factory=list)
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def _normalize_quantity(q: Quantity, precision: int = 10) -> dict[str, Any]:
    normalized = math.isclose(q.value, round(q.value, precision), rel_tol=1e-12)
    value = round(q.value, precision) if normalized else q.value
    result = {"value": float(value) if normalized else q.value, "unit": q.unit}
    if q.tol:
        result["tol"] = _normalize_tolerance(q.tol, precision)
    return result


def _normalize_tolerance(tol: Tolerance, precision: int = 10) -> dict[str, Any]:
    result: dict[str, Any] = {"model": tol.model}
    if tol.lower is not None:
        result["lower"] = round(tol.lower, precision)
    if tol.upper is not None:
        result["upper"] = round(tol.upper, precision)
    if tol.value is not None:
        result["value"] = round(tol.value, precision)
    return result


def _normalize_vector3d(v: Vector3D | None, precision: int = 10) -> dict[str, float] | None:
    if v is None:
        return None
    return {
        "x": round(v.x, precision),
        "y": round(v.y, precision),
        "z": round(v.z, precision),
    }


def _normalize_point3d(p: Point3D | None, precision: int = 10) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "x": _normalize_quantity(p.x, precision),
        "y": _normalize_quantity(p.y, precision),
        "z": _normalize_quantity(p.z, precision),
    }


def _normalize_feature_intent(feature: FeatureIntent, precision: int = 10) -> dict[str, Any]:
    base_data: dict[str, Any] = {
        "id": feature.id,
        "type": feature.type,
        "frame_id": feature.frame_id,
        "traces_to": sorted(feature.traces_to),
        "phase_id": feature.phase_id,
        "reference_support_type": feature.reference_support_type,
        "topology_sensitive": feature.topology_sensitive,
    }
    if feature.x_freecad:
        base_data["x_freecad"] = feature.x_freecad

    if isinstance(feature, PrimitiveIntent):
        dims = {k: _normalize_quantity(v, precision) for k, v in feature.dimensions.items()}
        base_data["primitive_type"] = feature.primitive_type
        base_data["dimensions"] = dict(sorted(dims.items()))
    elif isinstance(feature, SketchProfileIntent):
        base_data["plane"] = feature.plane
        base_data["elements"] = feature.elements
        base_data["constraints"] = feature.constraints
    elif isinstance(feature, ExtrudeIntent):
        base_data["base_profile_id"] = feature.base_profile_id
        base_data["distance"] = _normalize_quantity(feature.distance, precision)
        base_data["operation_type"] = feature.operation_type
        base_data["direction"] = _normalize_vector3d(feature.direction, precision)
    elif isinstance(feature, RevolveIntent):
        base_data["base_profile_id"] = feature.base_profile_id
        base_data["axis"] = _normalize_vector3d(feature.axis, precision)
        base_data["axis_point"] = _normalize_point3d(feature.axis_point, precision)
        base_data["angle"] = _normalize_quantity(feature.angle, precision)
        base_data["operation_type"] = feature.operation_type
    elif isinstance(feature, SweepIntent):
        base_data["profile_id"] = feature.profile_id
        base_data["spine_id"] = feature.spine_id
        base_data["operation_type"] = feature.operation_type
    elif isinstance(feature, LoftIntent):
        base_data["section_ids"] = list(feature.section_ids)
        base_data["operation_type"] = feature.operation_type
        base_data["ruled"] = feature.ruled
        base_data["closed"] = feature.closed
    elif isinstance(feature, HoleIntent):
        base_data["diameter"] = _normalize_quantity(feature.diameter, precision)
        base_data["depth"] = _normalize_quantity(feature.depth, precision)
        base_data["hole_type"] = feature.hole_type
        base_data["location"] = _normalize_point3d(feature.location, precision)
        base_data["face_reference"] = feature.face_reference
        base_data["axis"] = _normalize_vector3d(feature.axis, precision)
    elif isinstance(feature, PatternIntent):
        base_data["pattern_type"] = feature.pattern_type
        base_data["feature_ids"] = sorted(feature.feature_ids)
        base_data["count"] = feature.count
        base_data["axis"] = _normalize_vector3d(feature.axis, precision)
        base_data["center_point"] = _normalize_point3d(feature.center_point, precision)
        base_data["spacing"] = (
            _normalize_quantity(feature.spacing, precision) if feature.spacing else None
        )
        base_data["total_angle"] = (
            _normalize_quantity(feature.total_angle, precision) if feature.total_angle else None
        )
    elif isinstance(feature, BlendIntent):
        base_data["blend_type"] = feature.blend_type
        base_data["edge_references"] = sorted(feature.edge_references)
        base_data["radius"] = (
            _normalize_quantity(feature.radius, precision) if feature.radius else None
        )
        base_data["distance"] = (
            _normalize_quantity(feature.distance, precision) if feature.distance else None
        )

    return base_data


def _normalize_compiled_op(op: CompiledOp, precision: int = 10) -> dict[str, Any]:
    return {
        "id": op.id,
        "op_type": op.op_type,
        "inputs": op.inputs,
        "depends_on": sorted(op.depends_on),
        "invariants": [
            {"type": inv.type, "threshold": inv.threshold, "scope": inv.scope}
            for inv in op.invariants
        ],
        "retry_policy": op.retry_policy,
        "local_frame": op.local_frame,
        "feature_provenance_id": op.feature_provenance_id,
        "phase_id": op.phase_id,
        "reference_support_type": op.reference_support_type,
        "topology_sensitive": op.topology_sensitive,
    }


def compute_gir_hash(gir: GIR, precision: int = 10) -> str:
    from server.jcs import canonicalize as jcs_c

    normalized_features = [_normalize_feature_intent(f, precision) for f in gir.features]
    features_sorted_by_content = sorted(normalized_features, key=lambda x: str(x))

    canonical = {
        "gir_version": gir.gir_version,
        "frames": sorted(
            [{"id": f.id, "type": f.type, "parent": f.parent} for f in gir.frames],
            key=lambda x: str(x["id"]),
        ),
        "features": features_sorted_by_content,
        "metadata": gir.metadata,
    }
    canonical_str = jcs_c(canonical)
    return hashlib.sha256(canonical_str.encode()).hexdigest()


def compute_eir_hash(eir: EIR, precision: int = 10) -> str:
    canonical = {
        "eir_version": eir.eir_version,
        "operations": sorted(
            [_normalize_compiled_op(op, precision) for op in eir.operations],
            key=lambda x: str(x["id"]),
        ),
        "dependency_graph": {k: sorted(v) for k, v in eir.dependency_graph.items()},
        "metadata": eir.metadata,
    }
    canonical_str = jcs_canonicalize(canonical)
    return hashlib.sha256(canonical_str.encode()).hexdigest()


class GIRBuilder:
    def __init__(self) -> None:
        self._features: list[FeatureIntent] = []
        self._frames: list[Frame] = []
        self._metadata: dict[str, Any] = {}
        self._feature_counter: int = 0
        self._frame_counter: int = 0

    def add_global_frame(self) -> str:
        frame_id = f"D{self._frame_counter}"
        self._frame_counter += 1
        frame = Frame(id=frame_id, type="global")
        self._frames.append(frame)
        return frame_id

    def add_local_frame(self, parent_id: str, transform: Transform | None = None) -> str:
        frame_id = f"D{self._frame_counter}"
        self._frame_counter += 1
        frame = Frame(id=frame_id, type="local", parent=parent_id, transform=transform)
        self._frames.append(frame)
        return frame_id

    def add_primitive(
        self,
        primitive_type: str,
        dimensions: dict[str, Quantity],
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> PrimitiveIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        primitive = PrimitiveIntent(
            id=feature_id,
            primitive_type=primitive_type,
            dimensions=dimensions,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(primitive)
        return primitive

    def add_sketch_profile(
        self,
        plane: str,
        elements: list[dict[str, Any]],
        constraints: list[dict[str, Any]] | None = None,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> SketchProfileIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        sketch = SketchProfileIntent(
            id=feature_id,
            plane=plane,
            elements=elements,
            constraints=constraints or [],
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(sketch)
        return sketch

    def add_extrude_intent(
        self,
        base_profile_id: str,
        distance: Quantity,
        operation_type: str,
        direction: Vector3D | None = None,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> ExtrudeIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        extrude = ExtrudeIntent(
            id=feature_id,
            base_profile_id=base_profile_id,
            distance=distance,
            operation_type=operation_type,
            direction=direction,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(extrude)
        return extrude

    def add_hole_intent(
        self,
        diameter: Quantity,
        depth: Quantity,
        hole_type: str,
        location: Point3D,
        face_reference: str | None = None,
        axis: Vector3D | None = None,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> HoleIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        hole = HoleIntent(
            id=feature_id,
            diameter=diameter,
            depth=depth,
            hole_type=hole_type,
            location=location,
            face_reference=face_reference,
            axis=axis,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(hole)
        return hole

    def add_revolve_intent(
        self,
        base_profile_id: str,
        axis: Vector3D,
        axis_point: Point3D,
        angle: Quantity,
        operation_type: str,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> RevolveIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        revolve = RevolveIntent(
            id=feature_id,
            base_profile_id=base_profile_id,
            axis=axis,
            axis_point=axis_point,
            angle=angle,
            operation_type=operation_type,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(revolve)
        return revolve

    def add_sweep_intent(
        self,
        profile_id: str,
        spine_id: str,
        operation_type: str,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> SweepIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        sweep = SweepIntent(
            id=feature_id,
            profile_id=profile_id,
            spine_id=spine_id,
            operation_type=operation_type,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(sweep)
        return sweep

    def add_loft_intent(
        self,
        section_ids: list[str],
        operation_type: str,
        ruled: bool = False,
        closed: bool = False,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> LoftIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        loft = LoftIntent(
            id=feature_id,
            section_ids=section_ids,
            operation_type=operation_type,
            ruled=ruled,
            closed=closed,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(loft)
        return loft

    def add_pattern_intent(
        self,
        pattern_type: str,
        feature_ids: list[str],
        count: int,
        axis: Vector3D | None = None,
        center_point: Point3D | None = None,
        spacing: Quantity | None = None,
        total_angle: Quantity | None = None,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> PatternIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        pattern = PatternIntent(
            id=feature_id,
            pattern_type=pattern_type,
            feature_ids=feature_ids,
            count=count,
            axis=axis,
            center_point=center_point,
            spacing=spacing,
            total_angle=total_angle,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(pattern)
        return pattern

    def add_blend_intent(
        self,
        blend_type: str,
        edge_references: list[str],
        radius: Quantity | None = None,
        distance: Quantity | None = None,
        frame_id: str | None = None,
        traces_to: list[str] | None = None,
        x_freecad: dict[str, Any] | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> BlendIntent:
        feature_id = f"F{self._feature_counter}"
        self._feature_counter += 1
        blend = BlendIntent(
            id=feature_id,
            blend_type=blend_type,
            edge_references=edge_references,
            radius=radius,
            distance=distance,
            frame_id=frame_id,
            traces_to=traces_to or [],
            x_freecad=x_freecad,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._features.append(blend)
        return blend

    def set_metadata(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def build(self) -> GIR:
        return GIR(
            gir_version="1.0",
            frames=list(self._frames),
            features=list(self._features),
            metadata=dict(self._metadata),
        )


class EIRBuilder:
    def __init__(self) -> None:
        self._operations: list[CompiledOp] = []
        self._dependency_graph: dict[str, list[str]] = {}
        self._metadata: dict[str, Any] = {}
        self._op_counter: int = 0

    def add_operation(
        self,
        op_type: str,
        inputs: dict[str, Any],
        depends_on: list[str] | None = None,
        invariants: list[Invariant] | None = None,
        retry_policy: dict[str, Any] | None = None,
        local_frame: str | None = None,
        feature_provenance_id: str | None = None,
        phase_id: str | None = None,
        reference_support_type: str | None = None,
        topology_sensitive: bool | None = None,
    ) -> CompiledOp:
        op_id = f"OP{self._op_counter}"
        self._op_counter += 1
        op = CompiledOp(
            id=op_id,
            op_type=op_type,
            inputs=inputs,
            depends_on=depends_on or [],
            invariants=invariants or [],
            retry_policy=retry_policy,
            local_frame=local_frame,
            feature_provenance_id=feature_provenance_id,
            phase_id=phase_id,
            reference_support_type=reference_support_type,
            topology_sensitive=topology_sensitive,
        )
        self._operations.append(op)
        if op.depends_on:
            self._dependency_graph[op_id] = op.depends_on
        return op

    def set_metadata(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def build(self) -> EIR:
        return EIR(
            eir_version="1.0",
            operations=list(self._operations),
            dependency_graph=dict(self._dependency_graph),
            metadata=dict(self._metadata),
        )
