from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from server.paths import repo_root
from server.planning_types import (
    PlanningCheckpoint,
    PlanningPolicy,
    PlanningPolicyConstraint,
    PlanningPolicyManifest,
    PlanningPolicyPhase,
    PlanningPolicyRepairPlaybook,
)

VALID_STATUS = {"Yes", "Partial", "No"}
VALID_USAGE = {"High", "Medium", "Low"}
CHECK_TYPES = {
    "mcp_tool",
    "dispatch_tool",
    "file_exists",
    "text_contains",
    "py_def",
    "py_class_method",
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_type: str
    target: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class FeatureResult:
    feature_id: str
    platform: str
    feature: str
    common_usage: str
    baseline_status: str
    computed_status: str
    passed_checks: int
    total_checks: int
    checks: list[CheckResult]


_AST_CACHE: dict[Path, ast.Module] = {}
_MCP_TOOL_CACHE: set[str] | None = None
_DISPATCH_TOOL_CACHE: set[str] | None = None


def parse_matrix(path: Path) -> list[dict[str, str]]:
    """Parse feature rows from the matrix markdown table."""
    text = path.read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue

        cols = [part.strip() for part in stripped.split("|")[1:-1]]
        if len(cols) != 5:
            continue
        if cols[0] == "Platform":
            continue
        if cols[0].startswith("---"):
            continue

        rows.append(
            {
                "platform": cols[0],
                "feature": cols[1],
                "status": cols[2],
                "common_usage": cols[3],
                "notes": cols[4],
            }
        )
    return rows


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """Load and validate the feature support manifest."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be a mapping: {path}")

    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("Manifest is missing top-level 'features' list")

    out: list[dict[str, Any]] = []
    for idx, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise ValueError(f"features[{idx}] must be a mapping")

        required = (
            "id",
            "platform",
            "feature",
            "common_usage",
            "baseline_status",
            "checks",
        )
        for key in required:
            if key not in feature:
                raise ValueError(f"features[{idx}] missing required key: {key}")

        if feature["baseline_status"] not in VALID_STATUS:
            raise ValueError(
                f"features[{idx}] baseline_status must be one of {sorted(VALID_STATUS)}"
            )

        if feature["common_usage"] not in VALID_USAGE:
            raise ValueError(f"features[{idx}] common_usage must be one of {sorted(VALID_USAGE)}")

        checks = feature.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"features[{idx}] checks must be a non-empty list")

        for cidx, check in enumerate(checks):
            if not isinstance(check, dict):
                raise ValueError(f"features[{idx}].checks[{cidx}] must be a mapping")
            ctype = check.get("type")
            if ctype not in CHECK_TYPES:
                raise ValueError(
                    f"features[{idx}].checks[{cidx}] invalid check type {ctype!r}; "
                    f"valid types: {sorted(CHECK_TYPES)}"
                )

        out.append(feature)

    return out


def _status_from_counts(passed: int, total: int) -> str:
    if total <= 0:
        return "No"
    if passed == total:
        return "Yes"
    if passed == 0:
        return "No"
    return "Partial"


def _parse_python(path: Path) -> ast.Module:
    if path not in _AST_CACHE:
        source = path.read_text(encoding="utf-8")
        _AST_CACHE[path] = ast.parse(source, filename=str(path))
    return _AST_CACHE[path]


def _find_top_level_def(path: Path, name: str, kind: str) -> bool:
    tree = _parse_python(path)
    for node in tree.body:
        if kind == "function" and isinstance(node, ast.FunctionDef) and node.name == name:
            return True
        if kind == "class" and isinstance(node, ast.ClassDef) and node.name == name:
            return True
        if kind == "any":
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return True
            if isinstance(node, ast.ClassDef) and node.name == name:
                return True
    return False


def _find_class_method(path: Path, class_name: str, method_name: str) -> bool:
    tree = _parse_python(path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method_name:
                    return True
    return False


def _resolve_repo_path(path_str: str) -> Path:
    return repo_root() / path_str


def _mcp_tools() -> set[str]:
    global _MCP_TOOL_CACHE  # noqa: PLW0603
    if _MCP_TOOL_CACHE is None:
        from server.main import _tool_list

        _MCP_TOOL_CACHE = {str(t.get("name")) for t in _tool_list() if isinstance(t, dict)}
    return _MCP_TOOL_CACHE


def _dispatch_tools() -> set[str]:
    global _DISPATCH_TOOL_CACHE  # noqa: PLW0603
    if _DISPATCH_TOOL_CACHE is None:
        from server.main import (
            _CAD_DISPATCH,
            _ME_DISPATCH,
            _MFG_DISPATCH,
            _SPEC_DISPATCH,
        )

        _DISPATCH_TOOL_CACHE = (
            set(_CAD_DISPATCH.keys())
            | set(_MFG_DISPATCH.keys())
            | set(_SPEC_DISPATCH.keys())
            | set(_ME_DISPATCH.keys())
        )
    return _DISPATCH_TOOL_CACHE


def _eval_check(check: dict[str, Any]) -> CheckResult:
    ctype = str(check["type"])

    if ctype == "mcp_tool":
        name = str(check["name"])
        passed = name in _mcp_tools()
        return CheckResult(
            ctype, name, passed, "tool registered" if passed else "tool not registered"
        )

    if ctype == "dispatch_tool":
        name = str(check["name"])
        passed = name in _dispatch_tools()
        return CheckResult(
            ctype,
            name,
            passed,
            "dispatch handler found" if passed else "dispatch handler not found",
        )

    if ctype == "file_exists":
        path = _resolve_repo_path(str(check["path"]))
        passed = path.exists()
        return CheckResult(ctype, str(path), passed, "file exists" if passed else "file missing")

    if ctype == "text_contains":
        path = _resolve_repo_path(str(check["path"]))
        needle = str(check["text"])
        case_sensitive = bool(check.get("case_sensitive", True))
        haystack = path.read_text(encoding="utf-8")
        if case_sensitive:
            passed = needle in haystack
        else:
            passed = needle.lower() in haystack.lower()
        target = f"{path}:{needle}"
        return CheckResult(
            ctype,
            target,
            passed,
            "text found" if passed else "text not found",
        )

    if ctype == "py_def":
        path = _resolve_repo_path(str(check["path"]))
        name = str(check["name"])
        kind = str(check.get("kind", "any"))
        if kind not in {"function", "class", "any"}:
            raise ValueError(f"Invalid py_def kind: {kind!r}")
        passed = _find_top_level_def(path, name, kind)
        target = f"{path}:{name}"
        return CheckResult(ctype, target, passed, "symbol found" if passed else "symbol not found")

    if ctype == "py_class_method":
        path = _resolve_repo_path(str(check["path"]))
        class_name = str(check["class"])
        method = str(check["method"])
        passed = _find_class_method(path, class_name, method)
        target = f"{path}:{class_name}.{method}"
        return CheckResult(
            ctype,
            target,
            passed,
            "class method found" if passed else "class method not found",
        )

    raise ValueError(f"Unsupported check type: {ctype!r}")


def evaluate_manifest(manifest_path: Path) -> list[FeatureResult]:
    features = load_manifest(manifest_path)
    results: list[FeatureResult] = []

    for feature in features:
        checks = [_eval_check(check) for check in feature["checks"]]
        passed = sum(1 for c in checks if c.passed)
        total = len(checks)
        status = _status_from_counts(passed, total)
        results.append(
            FeatureResult(
                feature_id=str(feature["id"]),
                platform=str(feature["platform"]),
                feature=str(feature["feature"]),
                common_usage=str(feature["common_usage"]),
                baseline_status=str(feature["baseline_status"]),
                computed_status=status,
                passed_checks=passed,
                total_checks=total,
                checks=checks,
            )
        )

    return results


def summarize(results: list[FeatureResult]) -> dict[str, int]:
    counts = {"Yes": 0, "Partial": 0, "No": 0}
    for result in results:
        counts[result.computed_status] += 1
    return counts


@dataclass(frozen=True, slots=True)
class OpCapability:
    op_name: str
    status: str
    stability: str
    notes: str | None = None
    variants: list[dict[str, Any]] | None = None
    limits: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReferenceBehavior:
    rebinding_quality: str
    drift_detection: bool
    geometric_query: bool
    topology_aware: bool
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    backend_name: str
    backend_version: str
    operations: dict[str, OpCapability]
    reference_behavior: ReferenceBehavior


@dataclass(frozen=True, slots=True)
class GeometryCapabilities:
    version: str
    backends: dict[str, BackendCapabilities]


def load_geometry_capabilities(
    capabilities_path: Path | None = None,
) -> GeometryCapabilities:
    """Load geometry capabilities from YAML manifest.

    Args:
        capabilities_path: Path to geometry_capabilities.yml. Defaults to repo_root/feature_support/geometry_capabilities.yml

    Returns:
        GeometryCapabilities object containing backend capability matrix
    """
    if capabilities_path is None:
        capabilities_path = repo_root() / "feature_support" / "geometry_capabilities.yml"

    data = yaml.safe_load(capabilities_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Capabilities manifest must be a mapping: {capabilities_path}")

    version = data.get("version")
    if not isinstance(version, str):
        raise ValueError("Capabilities manifest missing 'version' field")

    backends_data = data.get("backends")
    if not isinstance(backends_data, dict):
        raise ValueError("Capabilities manifest missing 'backends' field")

    backends: dict[str, BackendCapabilities] = {}
    for backend_key, backend_value in backends_data.items():
        if not isinstance(backend_value, dict):
            raise ValueError(f"Backend {backend_key} must be a mapping")

        backend_name = backend_value.get("backend_name")
        backend_version = backend_value.get("backend_version")
        if not backend_name or not backend_version:
            raise ValueError(f"Backend {backend_key} missing name or version")

        ops_data = backend_value.get("operations")
        if not isinstance(ops_data, dict):
            raise ValueError(f"Backend {backend_key} missing operations field")

        operations: dict[str, OpCapability] = {}
        for op_key, op_value in ops_data.items():
            if not isinstance(op_value, dict):
                raise ValueError(f"Operation {op_key} must be a mapping")

            op_status = op_value.get("status")
            op_stability = op_value.get("stability")
            if op_status not in VALID_STATUS:
                raise ValueError(f"Operation {op_key} has invalid status: {op_status}")
            if not isinstance(op_stability, str):
                raise ValueError(f"Operation {op_key} missing stability field")

            operations[op_key] = OpCapability(
                op_name=op_key,
                status=op_status,
                stability=op_stability,
                notes=op_value.get("notes"),
                variants=op_value.get("variants"),
                limits=op_value.get("limits"),
            )

        ref_behavior_data = backend_value.get("reference_behavior")
        if not isinstance(ref_behavior_data, dict):
            raise ValueError(f"Backend {backend_key} missing reference_behavior field")

        reference_behavior = ReferenceBehavior(
            rebinding_quality=str(ref_behavior_data.get("rebinding_quality", "none")),
            drift_detection=bool(ref_behavior_data.get("drift_detection", False)),
            geometric_query=bool(ref_behavior_data.get("geometric_query", False)),
            topology_aware=bool(ref_behavior_data.get("topology_aware", False)),
            notes=ref_behavior_data.get("notes"),
        )

        backends[backend_key] = BackendCapabilities(
            backend_name=backend_name,
            backend_version=backend_version,
            operations=operations,
            reference_behavior=reference_behavior,
        )

    return GeometryCapabilities(version=version, backends=backends)


@dataclass(frozen=True, slots=True)
class Thresholds:
    wall_thickness_min_mm: float
    wall_thickness_nominal_mm: float
    hole_diameter_min_mm: float
    internal_radius_min_mm: float
    edge_spacing_min_mm: float
    feature_remove_angle_max_deg: float
    overhang_angle_max_deg: float | None = None
    bridge_length_max_mm: float | None = None
    pocket_depth_ratio_max: float | None = None
    hole_depth_ratio_max: float | None = None


@dataclass(frozen=True, slots=True)
class BaselineCheck:
    check_id: str
    check_type: str
    enabled: bool
    threshold_key: str | None = None
    severity: str = "notice"


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    process: str
    material_family: str
    thresholds: Thresholds
    baseline_checks: list[BaselineCheck]


@dataclass(frozen=True, slots=True)
class VerificationManifest:
    version: str
    policies: dict[str, VerificationPolicy]
    notice_severity_mapping: dict[str, str]


def load_verification_policy(policy_path: Path | None = None) -> VerificationManifest:
    """Load verification policy from YAML manifest.

    Args:
        policy_path: Path to verification_policy.yml. Defaults to repo_root/feature_support/verification_policy.yml

    Returns:
        VerificationManifest object containing verification policies
    """
    if policy_path is None:
        policy_path = repo_root() / "feature_support" / "verification_policy.yml"

    data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Verification policy must be a mapping: {policy_path}")

    version = data.get("version")
    if not isinstance(version, str):
        raise ValueError("Verification policy missing 'version' field")

    policies_data = data.get("policies")
    if not isinstance(policies_data, dict):
        raise ValueError("Verification policy missing 'policies' field")

    policies: dict[str, VerificationPolicy] = {}
    for policy_key, policy_value in policies_data.items():
        if not isinstance(policy_value, dict):
            raise ValueError(f"Policy {policy_key} must be a mapping")

        process = policy_value.get("process")
        material_family = policy_value.get("material_family")
        if not process or not material_family:
            raise ValueError(f"Policy {policy_key} missing process or material_family")

        thresholds_data = policy_value.get("thresholds")
        if not isinstance(thresholds_data, dict):
            raise ValueError(f"Policy {policy_key} missing thresholds field")

        try:
            thresholds = Thresholds(
                wall_thickness_min_mm=float(thresholds_data.get("wall_thickness_min_mm", 0)),
                wall_thickness_nominal_mm=float(
                    thresholds_data.get("wall_thickness_nominal_mm", 0)
                ),
                hole_diameter_min_mm=float(thresholds_data.get("hole_diameter_min_mm", 0)),
                internal_radius_min_mm=float(thresholds_data.get("internal_radius_min_mm", 0)),
                edge_spacing_min_mm=float(thresholds_data.get("edge_spacing_min_mm", 0)),
                feature_remove_angle_max_deg=float(
                    thresholds_data.get("feature_remove_angle_max_deg", 0)
                ),
                overhang_angle_max_deg=thresholds_data.get("overhang_angle_max_deg"),
                bridge_length_max_mm=thresholds_data.get("bridge_length_max_mm"),
                pocket_depth_ratio_max=thresholds_data.get("pocket_depth_ratio_max"),
                hole_depth_ratio_max=thresholds_data.get("hole_depth_ratio_max"),
            )
        except (ValueError, TypeError) as e:
            raise ValueError(f"Policy {policy_key} has invalid thresholds: {e}") from e

        checks_data = policy_value.get("baseline_checks")
        if not isinstance(checks_data, list):
            raise ValueError(f"Policy {policy_key} missing baseline_checks field")

        baseline_checks: list[BaselineCheck] = []
        for check_dict in checks_data:
            if not isinstance(check_dict, dict):
                raise ValueError(f"Policy {policy_key} contains invalid check entry")

            check_id = check_dict.get("check_id")
            check_type = check_dict.get("check_type")
            if not check_id or not check_type:
                raise ValueError(f"Policy {policy_key} check missing check_id or check_type")

            baseline_checks.append(
                BaselineCheck(
                    check_id=check_id,
                    check_type=check_type,
                    enabled=bool(check_dict.get("enabled", True)),
                    threshold_key=check_dict.get("threshold_key"),
                    severity=str(check_dict.get("severity", "notice")),
                )
            )

        policies[policy_key] = VerificationPolicy(
            process=process,
            material_family=material_family,
            thresholds=thresholds,
            baseline_checks=baseline_checks,
        )

    severity_mapping = data.get("notice_severity_mapping", {})
    if not isinstance(severity_mapping, dict):
        raise ValueError("Verification policy has invalid notice_severity_mapping")

    return VerificationManifest(
        version=version, policies=policies, notice_severity_mapping=severity_mapping
    )


def load_planning_policy(policy_path: Path | None = None) -> PlanningPolicyManifest:
    """Load planning policy manifest from YAML."""
    if policy_path is None:
        policy_path = repo_root() / "feature_support" / "planning_policy.yml"

    data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Planning policy must be a mapping: {policy_path}")

    version = data.get("version")
    if not isinstance(version, str):
        raise ValueError("Planning policy missing 'version' field")

    default_question_budget = data.get("default_question_budget", 2)
    if isinstance(default_question_budget, bool) or not isinstance(default_question_budget, int):
        raise ValueError("Planning policy default_question_budget must be an integer")

    policies_data = data.get("policies")
    if not isinstance(policies_data, dict):
        raise ValueError("Planning policy missing 'policies' field")

    policies: dict[str, PlanningPolicy] = {}
    for policy_key in sorted(policies_data.keys()):
        raw = policies_data[policy_key]
        if not isinstance(raw, dict):
            raise ValueError(f"Planning policy '{policy_key}' must be a mapping")

        process = str(raw.get("process", "")).strip()
        archetype = str(raw.get("archetype", "")).strip()
        required_parameters = raw.get("required_parameters", [])
        reference_strategy = raw.get("reference_strategy", {})
        phase_order = raw.get("phase_order", [])
        phase_policies_raw = raw.get("phase_policies", {})
        constraints_raw = raw.get("dfm_constraints", [])
        playbooks_raw = raw.get("repair_playbooks", [])

        if not process or not archetype:
            raise ValueError(f"Planning policy '{policy_key}' missing process/archetype")
        if not isinstance(required_parameters, list):
            raise ValueError(f"Planning policy '{policy_key}' required_parameters must be a list")
        if not isinstance(reference_strategy, dict):
            raise ValueError(f"Planning policy '{policy_key}' reference_strategy must be a mapping")
        if not isinstance(phase_order, list):
            raise ValueError(f"Planning policy '{policy_key}' phase_order must be a list")
        if not isinstance(phase_policies_raw, dict):
            raise ValueError(f"Planning policy '{policy_key}' phase_policies must be a mapping")
        if not isinstance(constraints_raw, list):
            raise ValueError(f"Planning policy '{policy_key}' dfm_constraints must be a list")
        if not isinstance(playbooks_raw, list):
            raise ValueError(f"Planning policy '{policy_key}' repair_playbooks must be a list")

        phase_policies: dict[str, PlanningPolicyPhase] = {}
        for phase_id in sorted(phase_policies_raw.keys()):
            phase_raw = phase_policies_raw[phase_id]
            if not isinstance(phase_raw, dict):
                raise ValueError(
                    f"Planning policy '{policy_key}' phase '{phase_id}' must be a mapping"
                )
            checkpoints_raw = phase_raw.get("checkpoints", [])
            if not isinstance(checkpoints_raw, list):
                raise ValueError(
                    f"Planning policy '{policy_key}' phase '{phase_id}' checkpoints must be a list"
                )
            checkpoints: list[PlanningCheckpoint] = []
            for cp in checkpoints_raw:
                if not isinstance(cp, dict):
                    raise ValueError(
                        f"Planning policy '{policy_key}' phase '{phase_id}' checkpoint must be an object"
                    )
                checkpoint_id = str(cp.get("checkpoint_id", "")).strip()
                validations = cp.get("validations", [])
                if not checkpoint_id:
                    raise ValueError(
                        f"Planning policy '{policy_key}' phase '{phase_id}' checkpoint missing checkpoint_id"
                    )
                if not isinstance(validations, list):
                    raise ValueError(
                        f"Planning policy '{policy_key}' phase '{phase_id}' validations must be a list"
                    )
                checkpoints.append(
                    PlanningCheckpoint(
                        checkpoint_id=checkpoint_id,
                        validations=[str(v) for v in validations],
                    )
                )

            phase_policies[phase_id] = PlanningPolicyPhase(
                phase_id=phase_id,
                checkpoints=checkpoints,
            )

        constraints: list[PlanningPolicyConstraint] = []
        for row in constraints_raw:
            if not isinstance(row, dict):
                raise ValueError(f"Planning policy '{policy_key}' constraint must be an object")
            constraints.append(
                PlanningPolicyConstraint(
                    constraint_id=str(row.get("id", "")),
                    severity=str(row.get("severity", "")),
                    metric=str(row.get("metric", "")),
                    operator=str(row.get("operator", "")),
                    value=float(row["value"])
                    if "value" in row and row.get("value") is not None
                    else None,
                    min_value=float(row["min"])
                    if "min" in row and row.get("min") is not None
                    else None,
                    max_value=float(row["max"])
                    if "max" in row and row.get("max") is not None
                    else None,
                    rationale=str(row.get("rationale", "")),
                    playbook_id=str(row.get("playbook_id"))
                    if row.get("playbook_id") is not None
                    else None,
                )
            )

        playbooks: list[PlanningPolicyRepairPlaybook] = []
        for row in playbooks_raw:
            if not isinstance(row, dict):
                raise ValueError(f"Planning policy '{policy_key}' playbook must be an object")
            pid = str(row.get("id", "")).strip()
            trigger = str(row.get("trigger", "")).strip()
            steps = row.get("steps", [])
            if not pid or not trigger or not isinstance(steps, list):
                raise ValueError(
                    f"Planning policy '{policy_key}' playbook must include id/trigger/steps"
                )
            playbooks.append(
                PlanningPolicyRepairPlaybook(
                    playbook_id=pid,
                    trigger=trigger,
                    steps=[str(s) for s in steps],
                )
            )

        policies[policy_key] = PlanningPolicy(
            key=policy_key,
            process=process,
            archetype=archetype,
            required_parameters=[dict(p) for p in required_parameters if isinstance(p, dict)],
            reference_strategy=dict(reference_strategy),
            phase_order=[str(p) for p in phase_order],
            phase_policies=phase_policies,
            dfm_constraints=constraints,
            repair_playbooks=playbooks,
        )

    return PlanningPolicyManifest(
        version=version,
        default_question_budget=default_question_budget,
        policies=policies,
    )
