from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ConstraintNode:
    id: str
    entity_type: str
    entity_id: str
    key: str
    value: Any
    unit: str | None = None
    tolerance: dict[str, Any] | None = None
    source: str = "spec"
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConstraintRelation:
    from_node: str
    to_node: str
    relation_type: str
    strength: float = 1.0


@dataclass(frozen=True, slots=True)
class UnresolvedConstraint:
    node_id: str
    reason: str
    suggestions: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ConstraintGraph:
    nodes: list[ConstraintNode]
    relations: list[ConstraintRelation]
    unresolved: list[UnresolvedConstraint]
    metadata: dict[str, Any] = field(default_factory=dict)


class ConstraintGraphBuilder:
    def __init__(self) -> None:
        self._nodes: list[ConstraintNode] = []
        self._relations: list[ConstraintRelation] = []
        self._unresolved: list[UnresolvedConstraint] = []
        self._metadata: dict[str, Any] = {}
        self._node_counter: int = 0

    def add_constraint(
        self,
        entity_type: str,
        entity_id: str,
        key: str,
        value: Any,
        unit: str | None = None,
        tolerance: dict[str, Any] | None = None,
        source: str = "spec",
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> ConstraintNode:
        node_id = f"C{self._node_counter}"
        self._node_counter += 1
        node = ConstraintNode(
            id=node_id,
            entity_type=entity_type,
            entity_id=entity_id,
            key=key,
            value=value,
            unit=unit,
            tolerance=tolerance,
            source=source,
            confidence=confidence,
            metadata=metadata or {},
        )
        self._nodes.append(node)
        return node

    def add_relation(
        self,
        from_node: str,
        to_node: str,
        relation_type: str,
        strength: float = 1.0,
    ) -> ConstraintRelation:
        relation = ConstraintRelation(
            from_node=from_node,
            to_node=to_node,
            relation_type=relation_type,
            strength=strength,
        )
        self._relations.append(relation)
        return relation

    def add_unresolved(
        self,
        node_id: str,
        reason: str,
        suggestions: list[str] | None = None,
    ) -> UnresolvedConstraint:
        unresolved = UnresolvedConstraint(
            node_id=node_id,
            reason=reason,
            suggestions=suggestions or [],
        )
        self._unresolved.append(unresolved)
        return unresolved

    def set_metadata(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def build(self) -> ConstraintGraph:
        return ConstraintGraph(
            nodes=list(self._nodes),
            relations=list(self._relations),
            unresolved=list(self._unresolved),
            metadata=dict(self._metadata),
        )

    def extract_dimensions_from_spec(self, spec: dict[str, Any]) -> None:
        envelope = spec.get("envelope")
        if not isinstance(envelope, dict):
            return

        for dim_name, dim_value in envelope.items():
            if dim_name in ["length", "width", "height", "diameter", "radius"]:
                if isinstance(dim_value, dict):
                    value = dim_value.get("value")
                    unit = dim_value.get("unit")
                    tolerance = dim_value.get("tolerance")
                    if value is not None:
                        self.add_constraint(
                            entity_type="envelope",
                            entity_id="overall",
                            key=dim_name,
                            value=value,
                            unit=unit,
                            tolerance=tolerance,
                            confidence=1.0,
                        )

    def extract_feature_constraints(self, spec: dict[str, Any]) -> None:
        geometry = spec.get("geometry", {})
        features = geometry.get("features", [])
        hole_features = geometry.get("hole_features", [])

        for feature in features:
            if not isinstance(feature, dict):
                continue
            feature_type = str(feature.get("type", "unknown"))
            feature_name = str(feature.get("id")) if feature.get("id") else feature_type

            for key, value in feature.items():
                if key in ["type", "id", "name"]:
                    continue
                if isinstance(value, dict) and "value" in value:
                    self.add_constraint(
                        entity_type="feature",
                        entity_id=feature_name,
                        key=key,
                        value=value.get("value"),
                        unit=value.get("unit"),
                        tolerance=value.get("tolerance"),
                        confidence=0.9,
                    )

        for hole in hole_features:
            if not isinstance(hole, dict):
                continue
            hole_id = str(hole.get("id")) if hole.get("id") else "hole"

            diameter = hole.get("diameter")
            if diameter and isinstance(diameter, dict):
                self.add_constraint(
                    entity_type="hole",
                    entity_id=hole_id,
                    key="diameter",
                    value=diameter.get("value"),
                    unit=diameter.get("unit"),
                    confidence=0.95,
                )

            depth = hole.get("depth")
            if depth and isinstance(depth, dict):
                self.add_constraint(
                    entity_type="hole",
                    entity_id=hole_id,
                    key="depth",
                    value=depth.get("value"),
                    unit=depth.get("unit"),
                    confidence=0.95,
                )

            location = hole.get("location")
            if location and isinstance(location, dict):
                for axis, coord in location.items():
                    if isinstance(coord, dict):
                        self.add_constraint(
                            entity_type="hole",
                            entity_id=hole_id,
                            key=f"location_{axis}",
                            value=coord.get("value"),
                            unit=coord.get("unit"),
                            confidence=0.95,
                        )

    def normalize_units(self, target_unit: str = "mm") -> None:
        conversion_factors = {
            ("mm", "mm"): 1.0,
            ("cm", "mm"): 10.0,
            ("m", "mm"): 1000.0,
            ("in", "mm"): 25.4,
            ("ft", "mm"): 304.8,
            ("deg", "deg"): 1.0,
            ("rad", "deg"): 57.2958,
        }

        normalized_nodes: list[ConstraintNode] = []
        for node in self._nodes:
            if node.unit and node.unit != target_unit and node.unit != "deg":
                factor = conversion_factors.get((node.unit, target_unit), 1.0)
                if factor != 1.0 and isinstance(node.value, (int, float)):
                    from dataclasses import replace

                    normalized = replace(
                        node,
                        value=node.value * factor
                        if isinstance(node.value, (float))
                        else node.value,
                        unit=target_unit,
                    )
                    normalized_nodes.append(normalized)
                else:
                    normalized_nodes.append(node)
            else:
                normalized_nodes.append(node)

        self._nodes = normalized_nodes

    def build_from_spec(self, spec: dict[str, Any]) -> ConstraintGraph:
        self.extract_dimensions_from_spec(spec)
        self.extract_feature_constraints(spec)
        self.normalize_units(target_unit="mm")
        return self.build()
