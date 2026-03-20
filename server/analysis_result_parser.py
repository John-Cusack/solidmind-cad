"""Parse CalculiX .frd output files.

The .frd format is CalculiX's native results format.  This parser extracts:
- Nodal displacements (U)
- Element stresses (S) → von Mises

Reference: CalculiX documentation, section on .frd file format.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from server.analysis_models import (
    AnalysisCheck,
    AnalysisSpec,
    CheckStatus,
    FieldResult,
    ScalarFieldSummary,
)

log = logging.getLogger("solidmind.analysis_result_parser")


def parse_frd(frd_path: Path, spec: AnalysisSpec) -> FieldResult:
    """Parse a CalculiX .frd file and produce a FieldResult.

    The .frd file uses fixed-width fields:
    - Lines starting with ``-1`` in col 1-3: node data
    - Lines starting with ``-2`` in col 1-3: element data
    - ``100CL`` blocks define datasets (DISP, STRESS, etc.)
    """
    text = frd_path.read_text()
    lines = text.splitlines()

    nodes = _parse_nodes(lines)
    displacements = _parse_displacement_block(lines, nodes)
    stresses = _parse_stress_block(lines, nodes)

    # Compute summaries
    max_disp = 0.0
    max_disp_loc = (0.0, 0.0, 0.0)
    disp_sum = 0.0

    for node_id, (ux, uy, uz) in displacements.items():
        mag = math.sqrt(ux**2 + uy**2 + uz**2)
        disp_sum += mag
        if mag > max_disp:
            max_disp = mag
            loc = nodes.get(node_id, (0.0, 0.0, 0.0))
            max_disp_loc = loc

    mean_disp = disp_sum / len(displacements) if displacements else 0.0

    max_vm = 0.0
    max_vm_loc = (0.0, 0.0, 0.0)
    min_vm = float("inf")
    vm_sum = 0.0

    for node_id, vm in stresses.items():
        vm_sum += vm
        if vm > max_vm:
            max_vm = vm
            loc = nodes.get(node_id, (0.0, 0.0, 0.0))
            max_vm_loc = loc
        if vm < min_vm:
            min_vm = vm

    if not stresses:
        min_vm = 0.0
    mean_vm = vm_sum / len(stresses) if stresses else 0.0

    # Build checks
    yield_mpa = spec.material.yield_strength_mpa
    safety_factor = yield_mpa / max_vm if max_vm > 0 else 99.0

    checks: list[AnalysisCheck] = []

    if max_vm > yield_mpa:
        checks.append(AnalysisCheck(
            name="yield_check",
            status=CheckStatus.FAIL,
            message=f"Max von Mises {max_vm:.1f} MPa EXCEEDS yield {yield_mpa:.1f} MPa",
            measured=max_vm,
            limit=yield_mpa,
            suggestion="Increase cross-section, add fillets at stress concentration, or use stronger material",
        ))
    elif max_vm > yield_mpa * 0.8:
        checks.append(AnalysisCheck(
            name="yield_check",
            status=CheckStatus.WARN,
            message=f"Max von Mises {max_vm:.1f} MPa is within 20% of yield {yield_mpa:.1f} MPa (SF={safety_factor:.2f})",
            measured=max_vm,
            limit=yield_mpa,
            suggestion="Consider increasing thickness or adding reinforcement",
        ))
    else:
        checks.append(AnalysisCheck(
            name="yield_check",
            status=CheckStatus.PASS,
            message=f"Max von Mises {max_vm:.1f} MPa < yield {yield_mpa:.1f} MPa (SF={safety_factor:.2f})",
            measured=max_vm,
            limit=yield_mpa,
        ))

    # Displacement check (warn if > 1% of typical part dimension)
    if max_disp > 1.0:
        checks.append(AnalysisCheck(
            name="displacement_check",
            status=CheckStatus.WARN,
            message=f"Max displacement {max_disp:.3f} mm may be excessive",
            measured=max_disp,
            suggestion="Increase stiffness or add supports",
        ))
    else:
        checks.append(AnalysisCheck(
            name="displacement_check",
            status=CheckStatus.PASS,
            message=f"Max displacement {max_disp:.3f} mm",
            measured=max_disp,
        ))

    overall = CheckStatus.PASS
    for c in checks:
        if c.status == CheckStatus.FAIL:
            overall = CheckStatus.FAIL
            break
        if c.status == CheckStatus.WARN:
            overall = CheckStatus.WARN

    fields = [
        ScalarFieldSummary(
            field_name="von_mises_stress",
            min_val=round(min_vm, 2),
            max_val=round(max_vm, 2),
            mean_val=round(mean_vm, 2),
            unit="MPa",
            max_location_xyz=max_vm_loc,
        ),
        ScalarFieldSummary(
            field_name="displacement",
            min_val=0.0,
            max_val=round(max_disp, 4),
            mean_val=round(mean_disp, 4),
            unit="mm",
            max_location_xyz=max_disp_loc,
        ),
    ]

    return FieldResult(
        analysis_id="",  # filled by caller
        status=overall,
        safety_factor=round(safety_factor, 2),
        max_von_mises_mpa=round(max_vm, 2),
        max_displacement_mm=round(max_disp, 4),
        checks=tuple(checks),
        scalar_fields=tuple(fields),
        solver_name="calculix",
    )


# ---------------------------------------------------------------------------
# .frd parsing helpers
# ---------------------------------------------------------------------------


def _parse_nodes(lines: list[str]) -> dict[int, tuple[float, float, float]]:
    """Extract node coordinates from .frd.

    Node block starts with ``    2C`` and ends with ``    3C``.
    Node lines start with `` -1`` (space + ``-1``) and have format:
    `` -1<node_id:10><x:12><y:12><z:12>``
    """
    nodes: dict[int, tuple[float, float, float]] = {}
    in_node_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("2C"):
            in_node_block = True
            continue
        if stripped.startswith("3C") and in_node_block:
            break
        if in_node_block and stripped.startswith("-1"):
            try:
                node_id = int(line[3:13])
                x = float(line[13:25])
                y = float(line[25:37])
                z = float(line[37:49])
                nodes[node_id] = (x, y, z)
            except (ValueError, IndexError):
                continue

    return nodes


def _parse_displacement_block(
    lines: list[str],
    nodes: dict[int, tuple[float, float, float]],
) -> dict[int, tuple[float, float, float]]:
    """Extract displacement (U) data from .frd."""
    displacements: dict[int, tuple[float, float, float]] = {}
    in_disp_block = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_disp_block and stripped.startswith("-4") and "DISP" in line:
            in_disp_block = True
            continue
        if in_disp_block and stripped.startswith("-3"):
            break
        if in_disp_block and stripped.startswith("-1"):
            try:
                node_id = int(line[3:13])
                ux = float(line[13:25])
                uy = float(line[25:37])
                uz = float(line[37:49])
                displacements[node_id] = (ux, uy, uz)
            except (ValueError, IndexError):
                continue

    return displacements


def _parse_stress_block(
    lines: list[str],
    nodes: dict[int, tuple[float, float, float]],
) -> dict[int, float]:
    """Extract von Mises stress from .frd STRESS block.

    CalculiX outputs 6 stress components (Sxx, Syy, Szz, Sxy, Syz, Szx).
    We compute von Mises from these.
    """
    stresses: dict[int, float] = {}
    in_stress_block = False

    for line in lines:
        stripped = line.strip()
        # The STRESS header is on a `-4  STRESS` line (separate from 100CL)
        if not in_stress_block and stripped.startswith("-4") and "STRESS" in line:
            in_stress_block = True
            continue
        if in_stress_block and stripped.startswith("-3"):
            break
        if in_stress_block and stripped.startswith("-1"):
            try:
                node_id = int(line[3:13])
                sxx = float(line[13:25])
                syy = float(line[25:37])
                szz = float(line[37:49])
                sxy = float(line[49:61])
                syz = float(line[61:73])
                szx = float(line[73:85])
                vm = _von_mises(sxx, syy, szz, sxy, syz, szx)
                stresses[node_id] = vm
            except (ValueError, IndexError):
                continue

    return stresses


def _von_mises(
    sxx: float, syy: float, szz: float,
    sxy: float, syz: float, szx: float,
) -> float:
    """Compute von Mises equivalent stress from 6 stress components."""
    term1 = (sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2
    term2 = 6 * (sxy ** 2 + syz ** 2 + szx ** 2)
    return math.sqrt(0.5 * (term1 + term2))
