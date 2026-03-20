"""Interface freeze — extended ICD validation and purchased-part lock."""
from __future__ import annotations

from typing import Any

from orchestrator.spec import Interface, MasterSpec, SubsystemKind


def is_interface_complete_extended(ifc: Interface) -> tuple[bool, list[str]]:
    """Baseline is_complete() plus context-specific checks.

    - gear_mesh: requires backlash spec
    - cylindrical_fit: requires runout_or_concentricity
    - bolt_pattern: requires preload spec
    """
    issues: list[str] = []

    if not ifc.is_complete():
        issues.append(f"Interface '{ifc.name}' (id={ifc.id}) fails baseline completeness")

    mating_type = ifc.mating.type

    if mating_type == "gear_mesh" and not ifc.backlash:
        issues.append(
            f"Interface '{ifc.name}': gear_mesh requires backlash specification"
        )

    if mating_type == "cylindrical_fit" and ifc.runout_or_concentricity is None:
        issues.append(
            f"Interface '{ifc.name}': cylindrical_fit requires runout_or_concentricity"
        )

    if mating_type == "bolt_pattern" and not ifc.preload:
        issues.append(
            f"Interface '{ifc.name}': bolt_pattern requires preload specification"
        )

    return len(issues) == 0, issues


def validate_purchased_lock(spec: MasterSpec) -> tuple[bool, list[str]]:
    """Validate purchased/standard parts are fully specified.

    - CATALOG must have supplier_part
    - STANDARD must have standard
    - All must have quantity >= 1
    """
    issues: list[str] = []

    for sub in spec.subsystems:
        if sub.kind == SubsystemKind.CATALOG:
            if not sub.supplier_part:
                issues.append(
                    f"CATALOG subsystem '{sub.name}' missing supplier_part"
                )
        elif sub.kind == SubsystemKind.STANDARD:
            if not sub.standard:
                issues.append(
                    f"STANDARD subsystem '{sub.name}' missing standard"
                )

        if sub.quantity < 1:
            issues.append(
                f"Subsystem '{sub.name}' has quantity={sub.quantity} (must be >= 1)"
            )

    return len(issues) == 0, issues


def freeze_interfaces(spec: MasterSpec) -> tuple[bool, list[str]]:
    """Aggregate check: ICD completeness + purchased lock + dangling refs."""
    all_issues: list[str] = []

    for ifc in spec.interfaces:
        _, ifc_issues = is_interface_complete_extended(ifc)
        all_issues.extend(ifc_issues)

    _, purchased_issues = validate_purchased_lock(spec)
    all_issues.extend(purchased_issues)

    ok_refs, dangling = spec.check_dangling_refs()
    if not ok_refs:
        all_issues.append(f"Dangling interface refs: {dangling}")

    return len(all_issues) == 0, all_issues


def lock_purchased_parts(spec: MasterSpec) -> list[str]:
    """Report which subsystems are locked (CATALOG or STANDARD)."""
    return [
        sub.name
        for sub in spec.subsystems
        if sub.kind in (SubsystemKind.CATALOG, SubsystemKind.STANDARD)
    ]


def generate_icd_summary(spec: MasterSpec) -> dict[str, Any]:
    """Per-interface completeness summary for A3 gate presentation."""
    summary: dict[str, Any] = {
        "total_interfaces": len(spec.interfaces),
        "interfaces": [],
    }

    complete_count = 0
    for ifc in spec.interfaces:
        ok, issues = is_interface_complete_extended(ifc)
        entry = {
            "id": ifc.id,
            "name": ifc.name,
            "subsystem_a": ifc.subsystem_a,
            "subsystem_b": ifc.subsystem_b,
            "mating_type": ifc.mating.type,
            "complete": ok,
            "issues": issues,
        }
        summary["interfaces"].append(entry)
        if ok:
            complete_count += 1

    summary["complete_count"] = complete_count
    summary["all_complete"] = complete_count == len(spec.interfaces)
    return summary
