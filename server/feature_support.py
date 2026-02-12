from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from server.paths import repo_root


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

        required = ("id", "platform", "feature", "common_usage", "baseline_status", "checks")
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
        from server.main import _CAD_DISPATCH, _ME_DISPATCH, _MFG_DISPATCH, _SPEC_DISPATCH

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
        return CheckResult(ctype, name, passed, "tool registered" if passed else "tool not registered")

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
