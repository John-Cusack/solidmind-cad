"""Council validation — decomposition, sizing, and feasibility checks."""
from __future__ import annotations

from orchestrator.spec import (
    MasterSpec,
    RuntimePolicy,
    Subsystem,
    SubsystemKind,
)


def validate_decomposition(spec: MasterSpec) -> tuple[bool, list[str]]:
    """Validate that subsystem decomposition is well-formed.

    Checks:
    - All subsystems have non-empty names
    - GENERATED subsystems have envelopes and materials
    - No duplicate subsystem names
    - All interface refs in subsystems resolve to existing interfaces
    """
    issues: list[str] = []

    if not spec.subsystems:
        issues.append("No subsystems defined")
        return False, issues

    names: set[str] = set()
    ifc_ids = {i.id for i in spec.interfaces}

    for sub in spec.subsystems:
        if not sub.name:
            issues.append(f"Subsystem id={sub.id} has empty name")
            continue

        if sub.name in names:
            issues.append(f"Duplicate subsystem name: '{sub.name}'")
        names.add(sub.name)

        if sub.kind == SubsystemKind.GENERATED:
            if not sub.envelope_mm:
                issues.append(f"Subsystem '{sub.name}' (GENERATED) missing envelope_mm")
            if not sub.material:
                issues.append(f"Subsystem '{sub.name}' (GENERATED) missing material")

        for ref in sub.interfaces:
            if ref not in ifc_ids:
                issues.append(f"Subsystem '{sub.name}' references unknown interface '{ref}'")

    return len(issues) == 0, issues


def validate_sizing(spec: MasterSpec) -> tuple[bool, list[str]]:
    """Validate sizing: mass budgets sum correctly, GENERATED parts have mass.

    Checks:
    - If global max_mass_kg is set, subsystem mass budgets must sum ≤ it
    - GENERATED subsystems should have mass_budget_kg set
    """
    issues: list[str] = []

    for sub in spec.subsystems:
        if sub.kind == SubsystemKind.GENERATED and sub.mass_budget_kg is None:
            issues.append(f"Subsystem '{sub.name}' (GENERATED) missing mass_budget_kg")

    ok_mass, msg = spec.check_mass_budget()
    if not ok_mass:
        issues.append(f"Mass budget exceeded: {msg}")

    return len(issues) == 0, issues


def check_feasibility(spec: MasterSpec) -> tuple[bool, list[str]]:
    """G1-backbone: combines decomposition + sizing + dangling refs."""
    all_issues: list[str] = []

    _, decomp_issues = validate_decomposition(spec)
    all_issues.extend(decomp_issues)

    _, sizing_issues = validate_sizing(spec)
    all_issues.extend(sizing_issues)

    ok_refs, dangling = spec.check_dangling_refs()
    if not ok_refs:
        all_issues.append(f"Dangling interface refs: {dangling}")

    return len(all_issues) == 0, all_issues


def classify_subsystem(sub: Subsystem) -> SubsystemKind:
    """Heuristic classification from supplier_part/standard fields."""
    if sub.supplier_part:
        return SubsystemKind.CATALOG
    if sub.standard:
        return SubsystemKind.STANDARD
    return SubsystemKind.GENERATED


def apply_complexity_defaults(sub: Subsystem) -> None:
    """Set runtime_policy from complexity_class if not already set."""
    if sub.runtime_policy is None:
        sub.runtime_policy = RuntimePolicy.from_complexity(sub.complexity_class)
