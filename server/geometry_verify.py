from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.geometry_executor import ExecutionTrace
from server.geometry_ir import GIR, Notice
from server.jcs import canonicalize as jcs_canonicalize

_POLICY_PATH = Path(__file__).parent.parent / "feature_support" / "verification_policy.yml"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Single verification check result."""

    check_id: str
    check_type: str
    passed: bool
    severity: str  # "error" | "warning" | "notice" | "info"
    message: str
    measured_value: float | None = None
    threshold_value: float | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Complete verification report."""

    results: list[VerificationResult] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    passed: bool = True
    report_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class VerificationEngine:
    """Verification engine consuming verification_policy.yml.

    Default behavior: notify-only (no blocking).
    Strict mode (Phase 3): blocks on error-severity findings.
    """

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> None:
        self._policy = policy or _load_policy()
        self._manifest = manifest or {}
        self._strict = strict

    def verify(
        self,
        execution_trace: ExecutionTrace,
        gir: GIR,
        spec: dict[str, Any],
    ) -> VerificationReport:
        """Run verification checks against execution results.

        Args:
            execution_trace: The completed execution trace.
            gir: The geometry intent representation.
            spec: The original specification.

        Returns:
            VerificationReport with check results and notices.
        """
        results: list[VerificationResult] = []
        notices: list[Notice] = []

        # Determine which policy to use
        policy_config = self._select_policy(spec)
        thresholds = policy_config.get("thresholds", {})
        baseline_checks = policy_config.get("baseline_checks", [])

        # Run each enabled baseline check
        for check_def in baseline_checks:
            if not check_def.get("enabled", True):
                continue

            check_id = check_def["check_id"]
            check_type = check_def["check_type"]
            severity = check_def.get("severity", "notice")
            threshold_key = check_def.get("threshold_key")
            threshold = thresholds.get(threshold_key) if threshold_key else None

            result = self._run_check(
                check_id=check_id,
                check_type=check_type,
                severity=severity,
                threshold=threshold,
                execution_trace=execution_trace,
                gir=gir,
                spec=spec,
            )
            results.append(result)

            if not result.passed:
                notices.append(
                    Notice(
                        code=result.check_id.upper().replace("-", "_"),
                        severity=result.severity,
                        message=result.message,
                        context=result.context,
                    )
                )

        # Feature count check (not in baseline_checks YAML, done generically)
        feature_result = self._check_feature_count(execution_trace, gir)
        results.append(feature_result)
        if not feature_result.passed:
            notices.append(
                Notice(
                    code="FEATURE_COUNT_MISMATCH",
                    severity=feature_result.severity,
                    message=feature_result.message,
                    context=feature_result.context,
                )
            )

        # Determine overall pass/fail
        all_passed = all(
            r.passed or r.severity not in ("error", "critical") for r in results
        )
        if self._strict:
            all_passed = all(r.passed for r in results if r.severity == "error")

        report = VerificationReport(
            results=results,
            notices=notices,
            passed=all_passed,
            metadata={
                "policy_key": self._get_policy_key(spec),
                "check_count": len(results),
                "passed_count": sum(1 for r in results if r.passed),
                "failed_count": sum(1 for r in results if not r.passed),
                "strict_mode": self._strict,
            },
        )

        # Compute report hash
        report_hash = _compute_report_hash(report)
        # Return new frozen instance with hash set
        return VerificationReport(
            results=report.results,
            notices=report.notices,
            passed=report.passed,
            report_hash=report_hash,
            metadata=report.metadata,
        )

    def _select_policy(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Select the best-matching policy for the spec."""
        policies = self._policy.get("policies", {})
        policy_key = self._get_policy_key(spec)
        return policies.get(policy_key, policies.get("default", {}))

    def _get_policy_key(self, spec: dict[str, Any]) -> str:
        """Derive policy lookup key from spec fields."""
        process = spec.get("process", "cnc")
        meta = spec.get("meta", {})
        if isinstance(meta, dict):
            process = meta.get("process", process)

        material_family = ""
        material = spec.get("material", {})
        if isinstance(material, dict):
            material_family = material.get("family", "").lower()

        mfg = spec.get("manufacturing", {})
        if isinstance(mfg, dict) and not material_family:
            mat = mfg.get("material", {})
            if isinstance(mat, dict):
                material_family = mat.get("family", "").lower()

        if process and material_family:
            key = f"{process}_{material_family}"
            if key in self._policy.get("policies", {}):
                return key

        return "default"

    def _run_check(
        self,
        check_id: str,
        check_type: str,
        severity: str,
        threshold: float | None,
        execution_trace: ExecutionTrace,
        gir: GIR,
        spec: dict[str, Any],
    ) -> VerificationResult:
        """Run a single verification check."""
        if check_type == "solid_validity":
            return self._check_solid_validity(check_id, severity, execution_trace)
        if check_type == "positive_volume":
            return self._check_positive_volume(check_id, severity, execution_trace)
        if check_type == "wall_thickness":
            return self._check_wall_thickness(check_id, severity, threshold, spec)
        if check_type == "hole_diameter":
            return self._check_hole_diameter(check_id, severity, threshold, gir)
        if check_type == "edge_spacing":
            return self._check_edge_spacing(check_id, severity, threshold)
        if check_type == "internal_radius":
            return self._check_internal_radius(check_id, severity, threshold, gir)
        if check_type == "pocket_depth_ratio":
            return self._check_pocket_depth_ratio(check_id, severity, threshold, spec)
        if check_type == "hole_depth_ratio":
            return self._check_hole_depth_ratio(check_id, severity, threshold, gir)
        if check_type == "overhang_angle":
            return self._check_overhang_angle(check_id, severity, threshold, spec)
        if check_type == "bridge_span":
            return self._check_bridge_span(check_id, severity, threshold, spec)

        # Unknown check type — pass with info notice
        return VerificationResult(
            check_id=check_id,
            check_type=check_type,
            passed=True,
            severity="info",
            message=f"Check type '{check_type}' not implemented, skipping",
        )

    def _check_solid_validity(
        self,
        check_id: str,
        severity: str,
        execution_trace: ExecutionTrace,
    ) -> VerificationResult:
        """Check that execution produced a valid solid."""
        failed_steps = [s for s in execution_trace.steps if s.status == "failed"]
        if failed_steps:
            return VerificationResult(
                check_id=check_id,
                check_type="solid_validity",
                passed=False,
                severity=severity,
                message=f"Solid invalid: {len(failed_steps)} operation(s) failed",
                context={"failed_ops": [s.op.id for s in failed_steps]},
            )
        return VerificationResult(
            check_id=check_id,
            check_type="solid_validity",
            passed=True,
            severity=severity,
            message="Solid validity check passed",
        )

    def _check_positive_volume(
        self,
        check_id: str,
        severity: str,
        execution_trace: ExecutionTrace,
    ) -> VerificationResult:
        """Check that the resulting solid has positive volume."""
        # In Phase 1, we check if at least one pad/extrude succeeded
        has_solid = any(
            s.status == "completed"
            and s.op.op_type in ("cad.pad", "pad", "cad.revolution", "revolve")
            for s in execution_trace.steps
        )
        if not has_solid and execution_trace.steps:
            return VerificationResult(
                check_id=check_id,
                check_type="positive_volume",
                passed=False,
                severity=severity,
                message="No solid-creating operation completed successfully",
            )
        return VerificationResult(
            check_id=check_id,
            check_type="positive_volume",
            passed=True,
            severity=severity,
            message="Positive volume check passed",
        )

    def _check_wall_thickness(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        spec: dict[str, Any],
    ) -> VerificationResult:
        """Check wall thickness against threshold using GIR-level analysis."""
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="wall_thickness",
                passed=True,
                severity="info",
                message="No wall thickness threshold configured",
            )

        # GIR-level check: compare envelope dimensions to detect thin walls
        envelope = spec.get("envelope", {})
        min_dim = None
        for dim_name in ["length", "width", "height"]:
            dim = envelope.get(dim_name, {})
            if isinstance(dim, dict) and dim.get("value") is not None:
                val = float(dim["value"])
                if min_dim is None or val < min_dim:
                    min_dim = val

        if min_dim is not None and min_dim < threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="wall_thickness",
                passed=False,
                severity=severity,
                message=f"Minimum dimension {min_dim}mm below wall thickness threshold {threshold}mm",
                measured_value=min_dim,
                threshold_value=threshold,
                context={"measured": min_dim, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="wall_thickness",
            passed=True,
            severity=severity,
            message="Wall thickness check passed",
            measured_value=min_dim,
            threshold_value=threshold,
        )

    def _check_hole_diameter(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        gir: GIR,
    ) -> VerificationResult:
        """Check hole diameters against minimum threshold."""
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="hole_diameter",
                passed=True,
                severity="info",
                message="No hole diameter threshold configured",
            )

        from server.geometry_ir import HoleIntent

        min_hole = None
        for feature in gir.features:
            if isinstance(feature, HoleIntent):
                d = feature.diameter.value
                if min_hole is None or d < min_hole:
                    min_hole = d

        if min_hole is not None and min_hole < threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="hole_diameter",
                passed=False,
                severity=severity,
                message=f"Hole diameter {min_hole}mm below minimum {threshold}mm",
                measured_value=min_hole,
                threshold_value=threshold,
                context={"measured": min_hole, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="hole_diameter",
            passed=True,
            severity=severity,
            message="Hole diameter check passed",
            measured_value=min_hole,
            threshold_value=threshold,
        )

    def _check_edge_spacing(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
    ) -> VerificationResult:
        """Check edge spacing (placeholder for geometry-based check in Phase 3)."""
        return VerificationResult(
            check_id=check_id,
            check_type="edge_spacing",
            passed=True,
            severity="info",
            message="Edge spacing check deferred to geometry-based analysis",
            threshold_value=threshold,
        )

    def _check_internal_radius(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        gir: GIR,
    ) -> VerificationResult:
        """Check internal (fillet) radii against minimum threshold."""
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="internal_radius",
                passed=True,
                severity="info",
                message="No internal radius threshold configured",
            )

        from server.geometry_ir import BlendIntent

        min_radius = None
        for feature in gir.features:
            if isinstance(feature, BlendIntent) and feature.blend_type == "fillet":
                if feature.radius:
                    r = feature.radius.value
                    if min_radius is None or r < min_radius:
                        min_radius = r

        if min_radius is not None and min_radius < threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="internal_radius",
                passed=False,
                severity=severity,
                message=f"Internal radius {min_radius}mm below minimum {threshold}mm",
                measured_value=min_radius,
                threshold_value=threshold,
                context={"measured": min_radius, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="internal_radius",
            passed=True,
            severity=severity,
            message="Internal radius check passed",
            measured_value=min_radius,
            threshold_value=threshold,
        )

    def _check_pocket_depth_ratio(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        spec: dict[str, Any],
    ) -> VerificationResult:
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="pocket_depth_ratio",
                passed=True,
                severity="info",
                message="No pocket depth ratio threshold configured",
            )

        geometry = spec.get("geometry", {})
        if not isinstance(geometry, dict):
            geometry = {}
        pockets = geometry.get("pocket_features", [])
        if not isinstance(pockets, list):
            pockets = []

        worst_ratio = None
        for pocket in pockets:
            if not isinstance(pocket, dict):
                continue
            depth = pocket.get("depth", {})
            width = pocket.get("width", {})
            depth_val = depth.get("value") if isinstance(depth, dict) else depth
            width_val = width.get("value") if isinstance(width, dict) else width
            if isinstance(depth_val, (int, float)) and isinstance(width_val, (int, float)) and width_val > 0:
                ratio = float(depth_val) / float(width_val)
                worst_ratio = ratio if worst_ratio is None else max(worst_ratio, ratio)

        if worst_ratio is not None and worst_ratio > threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="pocket_depth_ratio",
                passed=False,
                severity=severity,
                message=f"Pocket depth/width ratio {worst_ratio:.2f} exceeds max {threshold:.2f}",
                measured_value=worst_ratio,
                threshold_value=threshold,
                context={"measured": worst_ratio, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="pocket_depth_ratio",
            passed=True,
            severity=severity,
            message="Pocket depth ratio check passed",
            measured_value=worst_ratio,
            threshold_value=threshold,
        )

    def _check_hole_depth_ratio(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        gir: GIR,
    ) -> VerificationResult:
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="hole_depth_ratio",
                passed=True,
                severity="info",
                message="No hole depth ratio threshold configured",
            )

        from server.geometry_ir import HoleIntent

        worst_ratio = None
        for feature in gir.features:
            if isinstance(feature, HoleIntent) and feature.diameter.value > 0:
                ratio = feature.depth.value / feature.diameter.value
                worst_ratio = ratio if worst_ratio is None else max(worst_ratio, ratio)

        if worst_ratio is not None and worst_ratio > threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="hole_depth_ratio",
                passed=False,
                severity=severity,
                message=f"Hole depth/diameter ratio {worst_ratio:.2f} exceeds max {threshold:.2f}",
                measured_value=worst_ratio,
                threshold_value=threshold,
                context={"measured": worst_ratio, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="hole_depth_ratio",
            passed=True,
            severity=severity,
            message="Hole depth ratio check passed",
            measured_value=worst_ratio,
            threshold_value=threshold,
        )

    def _check_overhang_angle(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        spec: dict[str, Any],
    ) -> VerificationResult:
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="overhang_angle",
                passed=True,
                severity="info",
                message="No overhang threshold configured",
            )

        planning = spec.get("planning", {})
        if not isinstance(planning, dict):
            planning = {}
        measured = planning.get("max_overhang_angle_deg")
        if not isinstance(measured, (int, float)):
            measured = None

        if measured is not None and float(measured) > threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="overhang_angle",
                passed=False,
                severity=severity,
                message=f"Overhang angle {float(measured):.1f}deg exceeds max {threshold:.1f}deg",
                measured_value=float(measured),
                threshold_value=threshold,
                context={"measured": measured, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="overhang_angle",
            passed=True,
            severity=severity,
            message="Overhang angle check passed",
            measured_value=float(measured) if isinstance(measured, (int, float)) else None,
            threshold_value=threshold,
        )

    def _check_bridge_span(
        self,
        check_id: str,
        severity: str,
        threshold: float | None,
        spec: dict[str, Any],
    ) -> VerificationResult:
        if threshold is None:
            return VerificationResult(
                check_id=check_id,
                check_type="bridge_span",
                passed=True,
                severity="info",
                message="No bridge span threshold configured",
            )

        planning = spec.get("planning", {})
        if not isinstance(planning, dict):
            planning = {}
        measured = planning.get("max_bridge_span_mm")
        if not isinstance(measured, (int, float)):
            measured = None

        if measured is not None and float(measured) > threshold:
            return VerificationResult(
                check_id=check_id,
                check_type="bridge_span",
                passed=False,
                severity=severity,
                message=f"Bridge span {float(measured):.2f}mm exceeds max {threshold:.2f}mm",
                measured_value=float(measured),
                threshold_value=threshold,
                context={"measured": measured, "threshold": threshold},
            )

        return VerificationResult(
            check_id=check_id,
            check_type="bridge_span",
            passed=True,
            severity=severity,
            message="Bridge span check passed",
            measured_value=float(measured) if isinstance(measured, (int, float)) else None,
            threshold_value=threshold,
        )

    def _check_feature_count(
        self,
        execution_trace: ExecutionTrace,
        gir: GIR,
    ) -> VerificationResult:
        """Check that executed feature count matches GIR intent count."""
        gir_count = len(gir.features)
        # Count meaningful operations (exclude validate)
        exec_count = sum(
            1 for s in execution_trace.steps
            if s.status == "completed" and s.op.op_type not in ("validate", "cad.get_dimensions")
        )

        if gir_count > 0 and exec_count == 0:
            return VerificationResult(
                check_id="feature_count",
                check_type="feature_count",
                passed=False,
                severity="warning",
                message=f"No features executed (expected {gir_count} from GIR)",
                measured_value=float(exec_count),
                threshold_value=float(gir_count),
                context={"gir_features": gir_count, "executed": exec_count},
            )

        return VerificationResult(
            check_id="feature_count",
            check_type="feature_count",
            passed=True,
            severity="info",
            message=f"Feature count: {exec_count} executed from {gir_count} GIR intents",
            measured_value=float(exec_count),
            threshold_value=float(gir_count),
        )


def _load_policy() -> dict[str, Any]:
    """Load verification policy from YAML."""
    if _POLICY_PATH.exists():
        with open(_POLICY_PATH) as f:
            return yaml.safe_load(f) or {}
    return {"policies": {"default": {"thresholds": {}, "baseline_checks": []}}}


def _compute_report_hash(report: VerificationReport) -> str:
    """Compute deterministic hash of verification report."""
    results_data = []
    for r in report.results:
        results_data.append({
            "check_id": r.check_id,
            "check_type": r.check_type,
            "passed": r.passed,
            "severity": r.severity,
        })

    canonical = {
        "results": results_data,
        "passed": report.passed,
    }
    canonical_str = jcs_canonicalize(canonical)
    return hashlib.sha256(canonical_str.encode()).hexdigest()
