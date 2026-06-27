from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.geometry_ir import Notice, ReferenceToken


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    """Result of resolving a reference token against actual geometry."""

    status: str  # "resolved" | "unresolved"
    matches: list[str] = field(default_factory=list)
    selected: str | None = None
    drift_class: str = "none"  # "none" | "minor" | "major" | "unresolved"
    notices: list[Notice] = field(default_factory=list)


class ReferenceResolver:
    """Minimal V1 reference resolver using positional heuristics.

    Phase 1 scope: positional resolution only (top face after pad,
    vertical edges after pad). Drift class is 'none' for trivial cases.
    """

    def __init__(self) -> None:
        self._reference_map: dict[str, str] = {}

    @property
    def reference_map(self) -> dict[str, str]:
        return dict(self._reference_map)

    def resolve(
        self,
        token: ReferenceToken,
        context: dict[str, Any] | None = None,
    ) -> ResolvedReference:
        """Resolve a reference token to a concrete geometry name.

        Args:
            token: The reference token to resolve.
            context: Optional context with topology info from FreeCAD.

        Returns:
            ResolvedReference with resolution status and drift class.
        """
        ctx = context or {}

        # Check cached resolution first
        cached = self._reference_map.get(token.token)
        if cached:
            return ResolvedReference(
                status="resolved",
                matches=[cached],
                selected=cached,
                drift_class="none",
            )

        # Parse the reference token format: "ref:<op_id>:<selector>"
        resolved = self._resolve_by_selector(token, ctx)

        if resolved.status == "resolved" and resolved.selected:
            self._reference_map[token.token] = resolved.selected

        return resolved

    def resolve_face_ref(
        self,
        ref_string: str,
        context: dict[str, Any] | None = None,
    ) -> ResolvedReference:
        """Convenience method: resolve a face reference string."""
        token = _parse_ref_string(ref_string)
        if token is None:
            return ResolvedReference(
                status="unresolved",
                drift_class="unresolved",
                notices=[
                    Notice(
                        code="INVALID_REFERENCE",
                        severity="warning",
                        message=f"Cannot parse reference: {ref_string}",
                    )
                ],
            )
        return self.resolve(token, context)

    def resolve_edge_refs(
        self,
        ref_strings: list[str],
        context: dict[str, Any] | None = None,
    ) -> list[ResolvedReference]:
        """Resolve a list of edge reference strings."""
        return [self._resolve_edge_ref(r, context) for r in ref_strings]

    def register_result(
        self,
        op_id: str,
        result: dict[str, Any],
    ) -> None:
        """Register operation result for future reference resolution.

        After a pad operation, the top face is typically the last face
        created. This stores predictable face/edge mappings.
        """
        result_name = result.get("result_name", "")
        if result_name:
            self._reference_map[f"ref:{op_id}:top_face"] = "Face6"
            self._reference_map[f"ref:{op_id}:vertical_edges"] = "vertical"

    def _resolve_by_selector(
        self,
        token: ReferenceToken,
        context: dict[str, Any],
    ) -> ResolvedReference:
        """Resolve using the selector field of the token."""
        selector = token.selector or {}
        selector.get("type", "")

        # Positional resolution for common cases
        if "top_face" in token.token:
            face = context.get("top_face", "Face6")
            return ResolvedReference(
                status="resolved",
                matches=[face],
                selected=face,
                drift_class="none",
            )

        if "vertical_edges" in token.token:
            edges = context.get("vertical_edges", [])
            if edges:
                return ResolvedReference(
                    status="resolved",
                    matches=edges,
                    selected=edges[0],
                    drift_class="none",
                )
            return ResolvedReference(
                status="resolved",
                matches=[],
                selected=None,
                drift_class="none",
            )

        if "bottom_face" in token.token:
            face = context.get("bottom_face", "Face1")
            return ResolvedReference(
                status="resolved",
                matches=[face],
                selected=face,
                drift_class="none",
            )

        # If topology context is available, try invariant-based matching
        if token.invariants and context.get("topology"):
            return self._resolve_by_invariants(token, context)

        # Fallback: use default naming convention
        return ResolvedReference(
            status="unresolved",
            drift_class="unresolved",
            notices=[
                Notice(
                    code="REFERENCE_UNRESOLVED",
                    severity="info",
                    message=f"Could not resolve reference: {token.token}",
                    context={"token": token.token},
                )
            ],
        )

    def _resolve_by_invariants(
        self,
        token: ReferenceToken,
        context: dict[str, Any],
    ) -> ResolvedReference:
        """Resolve using geometric invariants against topology data."""
        invariants = token.invariants or {}
        topology = context.get("topology", {})
        notices: list[Notice] = []

        expected_normal = invariants.get("normal")
        expected_area = invariants.get("area")

        candidates: list[str] = []
        for face_name, face_data in topology.get("faces", {}).items():
            if expected_normal:
                actual_normal = face_data.get("normal", [])
                if actual_normal == expected_normal:
                    candidates.append(face_name)
            elif expected_area:
                actual_area = face_data.get("area", 0.0)
                if abs(actual_area - expected_area) / max(expected_area, 1e-9) < 0.01:
                    candidates.append(face_name)

        if len(candidates) == 1:
            return ResolvedReference(
                status="resolved",
                matches=candidates,
                selected=candidates[0],
                drift_class="none",
            )
        if len(candidates) > 1:
            # Use tie-break key if available
            selected = candidates[0]
            if token.tie_break_key is not None:
                idx = int(token.tie_break_key) % len(candidates)
                selected = candidates[idx]
            notices.append(
                Notice(
                    code="REFERENCE_AMBIGUOUS",
                    severity="info",
                    message=f"Multiple matches for {token.token}, using tie-break",
                    context={"matches": candidates, "selected": selected},
                )
            )
            return ResolvedReference(
                status="resolved",
                matches=candidates,
                selected=selected,
                drift_class="minor",
                notices=notices,
            )

        return ResolvedReference(
            status="unresolved",
            drift_class="major",
            notices=[
                Notice(
                    code="REFERENCE_DRIFT",
                    severity="warning",
                    message=f"No geometry matches invariants for {token.token}",
                    context={"token": token.token, "invariants": invariants},
                )
            ],
        )

    def _resolve_edge_ref(
        self,
        ref_string: str,
        context: dict[str, Any] | None,
    ) -> ResolvedReference:
        """Resolve a single edge reference string."""
        # Direct edge names pass through
        if ref_string.startswith("Edge"):
            return ResolvedReference(
                status="resolved",
                matches=[ref_string],
                selected=ref_string,
                drift_class="none",
            )

        token = _parse_ref_string(ref_string)
        if token is None:
            return ResolvedReference(
                status="unresolved",
                drift_class="unresolved",
                notices=[
                    Notice(
                        code="INVALID_REFERENCE",
                        severity="warning",
                        message=f"Cannot parse edge reference: {ref_string}",
                    )
                ],
            )
        return self.resolve(token, context)


def _parse_ref_string(ref_string: str) -> ReferenceToken | None:
    """Parse a reference string like 'ref:F1:top_face' into a ReferenceToken."""
    if not ref_string.startswith("ref:"):
        return None

    parts = ref_string.split(":", maxsplit=2)
    if len(parts) < 3:
        return None

    return ReferenceToken(
        token=ref_string,
        origin_op_id=parts[1],
        selector={"type": parts[2]},
    )
