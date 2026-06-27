"""Stage 7: Release package generation.

Produces BOM, ICD set, inspection notes, provenance bundle, and decision report.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.scorer import ScoringReport, _extract_variant_index
from orchestrator.spec import (
    MasterSpec,
    SubsystemKind,
)
from orchestrator.validator import ValidationReport

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BOMLine:
    """A single line in the Bill of Materials."""

    item_number: int = 0
    name: str = ""
    kind: str = ""  # generated | catalog | standard
    material: str = ""
    quantity: int = 1
    supplier_part: str = ""
    standard: str = ""
    mass_kg: float | None = None
    notes: str = ""


def generate_bom(
    spec: MasterSpec,
    *,
    validation_reports: list[ValidationReport] | None = None,
    scoring_report: ScoringReport | None = None,
) -> list[BOMLine]:
    """Generate BOM from the master spec subsystem list.

    Uses measured mass from validation reports when available,
    falling back to mass_budget_kg.
    """
    # Build lookup of measured masses from winner variant's validation
    measured_masses: dict[str, float] = {}
    if validation_reports:
        # Determine winner variants
        winner_variants: dict[str, int] = {}
        if scoring_report and scoring_report.candidates and scoring_report.winner_index is not None:
            winner = scoring_report.candidates[scoring_report.winner_index]
            for name, v in winner.variants.items():
                winner_variants[name] = v.variant_index

        for report in validation_reports:
            if report.mass_kg is not None:
                # If we have a winner, only use that variant's mass
                if winner_variants:
                    variant_idx = _extract_variant_index(report.worker_id)
                    if report.subsystem_name in winner_variants:
                        if variant_idx != winner_variants[report.subsystem_name]:
                            continue
                measured_masses[report.subsystem_name] = report.mass_kg

    lines: list[BOMLine] = []
    for i, sub in enumerate(spec.subsystems, start=1):
        mass = measured_masses.get(sub.name)
        notes = sub.description
        if mass is not None:
            notes_suffix = " [measured]"
        else:
            mass = sub.mass_budget_kg
            notes_suffix = " [budget]" if mass is not None else ""
        line = BOMLine(
            item_number=i,
            name=sub.name,
            kind=sub.kind.value,
            material=sub.material,
            quantity=sub.quantity,
            supplier_part=sub.supplier_part,
            standard=sub.standard,
            mass_kg=mass,
            notes=(notes or "") + notes_suffix,
        )
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Purchased parts list
# ---------------------------------------------------------------------------


def generate_purchased_parts_list(spec: MasterSpec) -> list[dict[str, Any]]:
    """Extract purchased and standard parts from the spec."""
    parts: list[dict[str, Any]] = []
    for sub in spec.subsystems:
        if sub.kind not in (SubsystemKind.CATALOG, SubsystemKind.STANDARD):
            continue
        parts.append(
            {
                "name": sub.name,
                "kind": sub.kind.value,
                "supplier_part": sub.supplier_part,
                "standard": sub.standard,
                "quantity": sub.quantity,
                "description": sub.description,
            }
        )
    return parts


# ---------------------------------------------------------------------------
# ICD summary
# ---------------------------------------------------------------------------


def generate_icd_set(spec: MasterSpec) -> list[dict[str, Any]]:
    """Generate the frozen ICD set for release."""
    icds: list[dict[str, Any]] = []
    for ifc in spec.interfaces:
        icd: dict[str, Any] = {
            "id": ifc.id,
            "name": ifc.name,
            "subsystem_a": ifc.subsystem_a,
            "subsystem_b": ifc.subsystem_b,
            "mating_type": ifc.mating.type,
            "geometry": ifc.geometry,
            "tolerances": {
                "fit_class": ifc.tolerances.fit_class,
                "dimensional": ifc.tolerances.dimensional,
                "geometric": ifc.tolerances.geometric,
            },
            "datum_scheme": ifc.datum_scheme,
            "ctqs": ifc.ctqs,
            "inspection": ifc.inspection,
        }
        if ifc.backlash:
            icd["backlash"] = ifc.backlash
        if ifc.runout_or_concentricity is not None:
            icd["runout_or_concentricity"] = ifc.runout_or_concentricity
        if ifc.preload:
            icd["preload"] = ifc.preload
        if ifc.lubrication:
            icd["lubrication"] = ifc.lubrication
        if ifc.retention:
            icd["retention"] = ifc.retention
        icds.append(icd)
    return icds


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def generate_provenance_manifest(
    spec: MasterSpec,
    run_dir: Path,
) -> dict[str, Any]:
    """Build a provenance manifest for the release."""
    import platform
    import subprocess as _sp

    spec_yaml = spec.to_yaml()
    spec_hash = hashlib.sha256(spec_yaml.encode()).hexdigest()

    artifacts: list[dict[str, Any]] = []
    for artifact_path in run_dir.rglob("*.step"):
        artifacts.append(_hash_file(artifact_path))
    for artifact_path in run_dir.rglob("*.stl"):
        artifacts.append(_hash_file(artifact_path))

    # Git hash
    git_hash = ""
    try:
        result = _sp.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_hash = result.stdout.strip()
    except Exception:
        pass

    return {
        "run_id": run_dir.name,
        "spec_hash": f"sha256:{spec_hash}",
        "timestamp": datetime.now(UTC).isoformat(),
        "git_hash": git_hash,
        "python_version": platform.python_version(),
        "artifacts": artifacts,
    }


def _hash_file(path: Path) -> dict[str, Any]:
    """Compute SHA256 and size for a file."""
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


# ---------------------------------------------------------------------------
# Decision report
# ---------------------------------------------------------------------------


def generate_decision_report(
    spec: MasterSpec,
    scoring_report: ScoringReport | None = None,
    validation_reports: list[ValidationReport] | None = None,
) -> str:
    """Generate a markdown decision report."""
    lines: list[str] = [
        f"# Decision Report: {spec.name}",
        "",
        f"**Status:** {spec.status.value}",
        f"**Date:** {datetime.now(UTC).strftime('%Y-%m-%d')}",
        "",
        "## Objectives",
        "",
    ]

    for obj in spec.objectives:
        threshold = f" (threshold: {obj.threshold} {obj.unit})" if obj.threshold else ""
        lines.append(f"- **{obj.name}**: {obj.direction} [{obj.unit}]{threshold}")

    lines.extend(["", "## Subsystems", ""])
    for sub in spec.subsystems:
        lines.append(
            f"- **{sub.name}** ({sub.kind.value}): {sub.material or 'n/a'}, qty={sub.quantity}"
        )

    lines.extend(["", "## Interfaces", ""])
    for ifc in spec.interfaces:
        lines.append(
            f"- **{ifc.name}**: {ifc.subsystem_a} ↔ {ifc.subsystem_b} "
            f"({ifc.mating.type or 'unspecified'})"
        )

    if scoring_report and scoring_report.candidates:
        lines.extend(["", "## Ranking", ""])
        for i, c in enumerate(scoring_report.candidates[:5]):
            marker = " ← WINNER" if i == scoring_report.winner_index else ""
            variant_names = ", ".join(
                f"{name}_v{v.variant_index}" for name, v in c.variants.items()
            )
            lines.append(f"{i + 1}. Score={c.assembly_score:.4f}: {variant_names}{marker}")

    if scoring_report and scoring_report.frontier:
        lines.extend(["", "## Pareto Frontier", ""])
        lines.append(f"{len(scoring_report.frontier)} candidate(s) on the frontier.")

    lines.extend(["", "## Residual Risks", ""])
    lines.append("- (To be assessed by human reviewer)")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Release package assembly
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReleasePackage:
    """The complete release package."""

    bom: list[BOMLine] = field(default_factory=list)
    icd_set: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    decision_report: str = ""
    package_dir: Path | None = None
    purchased_parts: list[dict[str, Any]] = field(default_factory=list)


def build_release_package(
    spec: MasterSpec,
    run_dir: Path,
    *,
    scoring_report: ScoringReport | None = None,
    validation_reports: list[ValidationReport] | None = None,
) -> ReleasePackage:
    """Assemble the full release package."""
    pkg_dir = run_dir / "release_package"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    bom = generate_bom(
        spec,
        validation_reports=validation_reports,
        scoring_report=scoring_report,
    )
    icd_set = generate_icd_set(spec)
    provenance = generate_provenance_manifest(spec, run_dir)
    decision = generate_decision_report(spec, scoring_report, validation_reports)
    purchased_parts = generate_purchased_parts_list(spec)

    package = ReleasePackage(
        bom=bom,
        icd_set=icd_set,
        provenance=provenance,
        decision_report=decision,
        package_dir=pkg_dir,
        purchased_parts=purchased_parts,
    )

    # Write files
    _save_bom(bom, pkg_dir / "bom.json")
    (pkg_dir / "icd_set.json").write_text(json.dumps(icd_set, indent=2))
    (pkg_dir / "provenance.json").write_text(json.dumps(provenance, indent=2))
    (pkg_dir / "decision_report.md").write_text(decision)
    if purchased_parts:
        (pkg_dir / "purchased_parts.json").write_text(json.dumps(purchased_parts, indent=2))

    # Copy spec
    spec.save(pkg_dir / "spec.yaml")

    # Copy winner geometry artifacts (STEP, STL, screenshots) into release
    _collect_winner_artifacts(run_dir, pkg_dir, scoring_report)

    log.info("Release package written to %s", pkg_dir)
    return package


def _collect_winner_artifacts(
    run_dir: Path,
    pkg_dir: Path,
    scoring_report: ScoringReport | None,
) -> None:
    """Copy STEP/STL/PNG files for the winning candidate into the release package."""
    import shutil

    geometry_dir = pkg_dir / "geometry"
    geometry_dir.mkdir(exist_ok=True)

    # Determine which variant index to use per subsystem
    winner_variants: dict[str, int] = {}
    if scoring_report and scoring_report.candidates and scoring_report.winner_index is not None:
        winner = scoring_report.candidates[scoring_report.winner_index]
        for name, v in winner.variants.items():
            winner_variants[name] = v.variant_index
    else:
        # No scoring — take variant 0 for each subsystem
        for d in run_dir.iterdir():
            if d.is_dir() and (d / "output").is_dir():
                parts = d.name.rsplit("_", 1)
                if len(parts) == 2:
                    try:
                        idx = int(parts[1])
                        sub_name = parts[0]
                        if sub_name not in winner_variants:
                            winner_variants[sub_name] = idx
                    except ValueError:
                        pass

    copied = 0
    for sub_name, variant_idx in winner_variants.items():
        output_dir = run_dir / f"{sub_name}_{variant_idx}" / "output"
        if not output_dir.exists():
            continue
        for ext in ("*.step", "*.stl", "*.png"):
            for src in output_dir.glob(ext):
                dst = geometry_dir / src.name
                shutil.copy2(src, dst)
                copied += 1

    if copied:
        log.info("Copied %d geometry artifacts to %s", copied, geometry_dir)


def _save_bom(bom: list[BOMLine], path: Path) -> None:
    """Save BOM to JSON."""
    data = [
        {
            "item_number": line.item_number,
            "name": line.name,
            "kind": line.kind,
            "material": line.material,
            "quantity": line.quantity,
            "supplier_part": line.supplier_part,
            "standard": line.standard,
            "mass_kg": line.mass_kg,
            "notes": line.notes,
        }
        for line in bom
    ]
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Gate G7
# ---------------------------------------------------------------------------


def check_gate_g7(
    package: ReleasePackage,
    spec: MasterSpec | None = None,
) -> tuple[bool, list[str]]:
    """G7: Complete release artifacts exist."""
    issues: list[str] = []

    if not package.bom:
        issues.append("BOM is empty")
    if not package.icd_set:
        issues.append("ICD set is empty")
    if not package.provenance:
        issues.append("Provenance manifest missing")
    if not package.decision_report:
        issues.append("Decision report missing")
    if package.package_dir and not package.package_dir.exists():
        issues.append(f"Package directory does not exist: {package.package_dir}")

    # Check purchased_parts.json present if spec has CATALOG/STANDARD subsystems
    if spec is not None:
        has_purchased = any(
            s.kind in (SubsystemKind.CATALOG, SubsystemKind.STANDARD) for s in spec.subsystems
        )
        if has_purchased and not package.purchased_parts:
            issues.append("Purchased parts list missing but spec has CATALOG/STANDARD subsystems")

    return len(issues) == 0, issues
