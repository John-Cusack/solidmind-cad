from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from jsonschema import Draft202012Validator

from server.constants import COVERAGE_THRESHOLDS, MATURITY_LEVELS, SUPPORTED_PROCESS, SUPPORTED_SPEC_MAJOR
from server.jsonutil import loads as json_loads
from server.models import Finding, Severity, ToolError
from server.paths import data_path
from server.question_bank import compute_coverage, load_question_bank
from server.rules_cnc import run as run_rules_cnc


def _parse_semver_major(version: str) -> int | None:
    if not isinstance(version, str):
        return None
    parts = version.split(".")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0], 10)
    except ValueError:
        return None


def _path_to_pointer(path: Any) -> str:
    # jsonschema yields deque/list of path items.
    tokens: list[str] = []
    for p in list(path):
        if isinstance(p, int):
            tokens.append(str(p))
        else:
            tokens.append(str(p))
    return "/" + "/".join(tokens) if tokens else ""


@lru_cache(maxsize=8)
def _load_schema(process: str) -> dict:
    schema_path = data_path("schemas", f"{process}.schema.json")
    return json_loads(schema_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _validator(process: str) -> Draft202012Validator:
    schema = _load_schema(process)
    return Draft202012Validator(schema)


def validate_shape(spec_draft: dict) -> tuple[bool, list[ToolError]]:
    meta = spec_draft.get("meta") if isinstance(spec_draft, dict) else None
    version = meta.get("spec_version") if isinstance(meta, dict) else None
    major = _parse_semver_major(version) if isinstance(version, str) else None
    if major is None:
        return False, [
            ToolError(
                code="SCHEMA_VIOLATION",
                message="meta.spec_version must be a semver string (e.g., 1.0.0).",
                field="/meta/spec_version",
                details={"expected_major": SUPPORTED_SPEC_MAJOR},
            )
        ]
    if major != SUPPORTED_SPEC_MAJOR:
        return False, [
            ToolError(
                code="UNSUPPORTED_SPEC_VERSION",
                message=f"Unsupported spec major version: {major}",
                field="/meta/spec_version",
                details={"supported_major": SUPPORTED_SPEC_MAJOR},
            )
        ]

    process = meta.get("process") if isinstance(meta, dict) else None
    if process != SUPPORTED_PROCESS:
        return False, [
            ToolError(
                code="UNSUPPORTED_PROCESS",
                message=f"Unsupported process: {process!r}",
                field="/meta/process",
                details={"supported_process": SUPPORTED_PROCESS},
            )
        ]

    errors: list[ToolError] = []
    v = _validator(process)
    for err in v.iter_errors(spec_draft):
        field = _path_to_pointer(err.absolute_path) or None
        errors.append(
            ToolError(
                code="SCHEMA_VIOLATION",
                message=err.message,
                field=field,
                details={"validator": err.validator, "validator_value": err.validator_value},
            )
        )

    errors.sort(key=lambda e: ((e.field or ""), e.message, e.code))
    return len(errors) == 0, errors


def coverage_threshold(maturity_level: str) -> float:
    return float(COVERAGE_THRESHOLDS.get(maturity_level, 1.0))


def run_rules(spec_draft: dict) -> tuple[list[Finding], list[Finding]]:
    # MVP: cnc only.
    findings = run_rules_cnc(spec_draft)
    blockers = [f for f in findings if f.severity == Severity.BLOCK]
    warnings = [f for f in findings if f.severity != Severity.BLOCK]

    blockers.sort(key=lambda f: (-int(f.priority), f.rule_id))
    warnings.sort(key=lambda f: (-int(f.priority), f.rule_id))
    return blockers, warnings


@dataclass(frozen=True, slots=True)
class ValidateResult:
    shape_valid: bool
    errors: list[ToolError]
    coverage_score: float
    coverage_threshold: float
    blockers: list[Finding]
    warnings: list[Finding]


def validate_all(spec_draft: dict) -> ValidateResult:
    shape_valid, errors = validate_shape(spec_draft)

    meta = spec_draft.get("meta") if isinstance(spec_draft, dict) else None
    maturity = meta.get("maturity_level") if isinstance(meta, dict) else None
    maturity_level = maturity if maturity in MATURITY_LEVELS else "L1"

    qb = load_question_bank(SUPPORTED_PROCESS)
    coverage = compute_coverage(spec_draft, qb, maturity_level)
    threshold = coverage_threshold(maturity_level)

    blockers, warnings = run_rules(spec_draft)

    return ValidateResult(
        shape_valid=shape_valid,
        errors=errors,
        coverage_score=coverage,
        coverage_threshold=threshold,
        blockers=blockers,
        warnings=warnings,
    )
