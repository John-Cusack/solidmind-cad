"""L2 coarse FEA pipeline: Gmsh meshing + CalculiX linear elastic analysis."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.fea_bc_mapper import (
    FEABoundaryCondition,
    extract_mesh_nodes,
    find_nodes_near_frame,
    map_interface_bcs,
)
from orchestrator.materials import Material
from orchestrator.spec import Interface, Subsystem

log = logging.getLogger(__name__)


class FEAError(Exception):
    """Raised on FEA pipeline failures."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MeshInfo:
    """Mesh generation result."""

    element_count: int
    node_count: int
    element_size_mm: float
    path: Path


@dataclass(slots=True)
class SingularityFlag:
    """A flagged stress singularity."""

    element_id: int
    stress_mpa: float
    reason: str
    location_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(slots=True)
class FEAResult:
    """Result from a single FEA run at one mesh density."""

    mesh_density: str  # "coarse" | "fine"
    max_von_mises_mpa: float = 0.0
    max_displacement_mm: float = 0.0
    element_count: int = 0
    singularity_flags: list[SingularityFlag] = field(default_factory=list)
    yield_safety_factor: float = 0.0
    stress_per_element: list[float] = field(default_factory=list)
    filtered_max_stress_mpa: float = 0.0


@dataclass(slots=True)
class FEAReport:
    """Combined L2 FEA report with convergence check."""

    subsystem_name: str = ""
    coarse: FEAResult | None = None
    fine: FEAResult | None = None
    convergence_pct: float = 0.0
    converged: bool = False
    filtered_max_stress_mpa: float = 0.0
    safety_factor: float = 0.0
    passed: bool = False


# ---------------------------------------------------------------------------
# Meshing (Gmsh)
# ---------------------------------------------------------------------------


def mesh_step(
    step_path: Path,
    element_size_mm: float,
    output_dir: Path,
) -> MeshInfo:
    """Mesh a STEP file with Gmsh using C3D10 (quadratic tet) elements.

    Returns a MeshInfo pointing to the mesh-only .inp file.
    """
    try:
        import gmsh
    except ImportError as exc:
        raise FEAError("gmsh Python package not installed: pip install gmsh") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    inp_path = output_dir / f"{step_path.stem}_mesh.inp"

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Verbosity", 1)
        gmsh.merge(str(step_path))
        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", element_size_mm * 0.5)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", element_size_mm)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)  # quadratic
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)  # Delaunay
        gmsh.model.mesh.generate(3)

        # Get mesh stats
        node_tags, _, _ = gmsh.model.mesh.getNodes()
        elem_types, elem_tags, _ = gmsh.model.mesh.getElements(dim=3)
        n_elements = sum(len(t) for t in elem_tags)
        n_nodes = len(node_tags)

        # Write mesh-only .inp (Abaqus format, which CalculiX reads)
        gmsh.write(str(inp_path))
    finally:
        gmsh.finalize()

    return MeshInfo(
        element_count=n_elements,
        node_count=n_nodes,
        element_size_mm=element_size_mm,
        path=inp_path,
    )


# ---------------------------------------------------------------------------
# CalculiX .inp generation
# ---------------------------------------------------------------------------


def _extract_volume_elements(mesh_text: str) -> tuple[str, list[str]]:
    """Filter a Gmsh-emitted Abaqus mesh down to its solid (C3D10) elements.

    Gmsh writes lower-dimensional element blocks (``T3D3`` line elements,
    ``CPS6`` plane-stress triangles) alongside the ``C3D10`` tetrahedra. If those
    survive into the deck, CalculiX assigns the ``*SOLID SECTION`` to the plane
    elements and dies in ``gen3delem`` ("first thickness ... is zero"), and the
    bogus element ids also feed an over-broad ``EALL`` that segfaults the solver.

    Returns the mesh text with only ``*NODE`` and ``C3D10`` element blocks kept,
    plus the ordered list of real ``C3D10`` element ids (for an explicit EALL).
    """
    kept: list[str] = []
    elem_ids: list[str] = []
    # Three contexts for a data line: inside a kept C3D10 element block (keep +
    # collect id), inside a dropped non-C3D10 element block (skip), or anything
    # else — e.g. *NODE coordinates (keep). Only element data lines are ever
    # dropped; node and other-keyword data must survive.
    in_c3d10_block = False
    in_dropped_block = False
    for line in mesh_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("*ELEMENT"):
            in_c3d10_block = "C3D10" in stripped.upper()
            in_dropped_block = not in_c3d10_block
            if in_c3d10_block:
                kept.append(line)
            continue
        if stripped.startswith("*"):
            # Any other keyword (nodes, sets, etc.) — keep and leave element mode.
            in_c3d10_block = False
            in_dropped_block = False
            kept.append(line)
            continue
        # Data line.
        if in_dropped_block:
            continue
        kept.append(line)
        if in_c3d10_block and stripped and stripped[0].isdigit():
            elem_ids.append(stripped.split(",")[0].strip())
    return "\n".join(kept), elem_ids


def _ccx_num(value: float) -> str:
    """Format a float for a CalculiX card.

    CalculiX 2.21 reads material/load card data in fixed-width fields, so Python's
    full-precision repr (e.g. ``2.6999999999999998e-09``, 22 chars) overflows the
    field and corrupts the parse. A bounded 6-significant-figure form is always
    short enough and loses no engineering precision.
    """
    return f"{value:.6g}"


def build_inp(
    mesh_inp: Path,
    material: Material,
    bcs: list[FEABoundaryCondition],
    output_inp: Path,
    node_sets: dict[str, list[int]] | None = None,
) -> Path:
    """Read Gmsh mesh .inp, append material/BC/step cards for CalculiX.

    Args:
        mesh_inp: Gmsh-generated mesh-only .inp file.
        material: Material properties.
        bcs: Boundary conditions to apply.
        output_inp: Path to write complete CalculiX .inp.
        node_sets: {set_name: [node_ids]} for BC application.

    Returns path to the complete .inp file.
    """
    mesh_text = mesh_inp.read_text()
    volume_text, elem_ids = _extract_volume_elements(mesh_text)
    lines = [volume_text.rstrip()]

    # Node sets
    if node_sets:
        for set_name, node_ids in node_sets.items():
            lines.append(f"*NSET, NSET={set_name}")
            # Write node IDs in rows of 10
            for i in range(0, len(node_ids), 10):
                chunk = node_ids[i : i + 10]
                lines.append(", ".join(str(n) for n in chunk))

    # Element set listing only the real solid (C3D10) elements — an explicit list,
    # not GENERATE-to-huge, so EALL never references non-existent ids (which
    # segfaults ccx and assigns the solid section to phantom elements).
    lines.append("*ELSET, ELSET=EALL")
    for i in range(0, len(elem_ids), 10):
        lines.append(", ".join(elem_ids[i : i + 10]))

    # Material
    mat_name = "MAT1"
    lines.append(f"*MATERIAL, NAME={mat_name}")
    lines.append("*ELASTIC")
    lines.append(f"{_ccx_num(material.young_modulus_mpa)}, {_ccx_num(material.poisson_ratio)}")
    lines.append("*DENSITY")
    lines.append(_ccx_num(material.density_kg_m3 * 1e-12))  # kg/m3 -> tonne/mm3

    # Assign material to solid section
    lines.append(f"*SOLID SECTION, ELSET=EALL, MATERIAL={mat_name}")
    lines.append("")

    # Boundary conditions — solid (C3D10) nodes have only 3 translational DOF;
    # constraining DOF 4-6 segfaults ccx, so fix 1-3 only.
    for bc in bcs:
        if bc.bc_type == "fixed":
            lines.append("*BOUNDARY")
            lines.append(f"{bc.node_set_name}, 1, 3")

    # Step
    lines.append("*STEP")
    lines.append("*STATIC")

    # Applied loads
    for bc in bcs:
        if bc.bc_type == "force":
            for dof, val in enumerate(bc.values, start=1):
                if abs(val) > 1e-12:
                    lines.append("*CLOAD")
                    lines.append(f"{bc.node_set_name}, {dof}, {_ccx_num(val)}")

    # Output requests
    lines.append("*NODE FILE")
    lines.append("U")
    lines.append("*EL FILE")
    lines.append("S")
    lines.append("*END STEP")

    output_inp.write_text("\n".join(lines) + "\n")
    return output_inp


# ---------------------------------------------------------------------------
# CalculiX execution
# ---------------------------------------------------------------------------


def run_ccx(inp_path: Path, timeout_sec: int = 300) -> Path:
    """Run CalculiX on an .inp file.

    Returns path to the .frd result file.
    Raises FEAError on non-zero exit or timeout.
    """
    ccx_bin = shutil.which("ccx")
    if not ccx_bin:
        raise FEAError("ccx not found on PATH")

    stem = inp_path.stem
    work_dir = inp_path.parent

    try:
        result = subprocess.run(
            [ccx_bin, "-i", stem],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise FEAError(f"ccx timed out after {timeout_sec}s") from exc

    frd_path = work_dir / f"{stem}.frd"

    if result.returncode != 0:
        # ccx writes errors to stderr or stdout
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise FEAError(f"ccx exited with code {result.returncode}: {msg}")

    if not frd_path.exists():
        raise FEAError(f"ccx did not produce {frd_path}")

    return frd_path


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------


def parse_frd(frd_path: Path, yield_strength_mpa: float = 0.0) -> FEAResult:
    """Parse a CalculiX .frd result file to extract stress and displacement.

    Prefers meshio when it can read the .frd, falls back to the dependency-free
    text parser. meshio can't deduce the format from the ``.frd`` extension, so we
    pass it explicitly; any failure (unsupported format, parse error) degrades to
    the text parser rather than crashing the pipeline.
    """
    try:
        import meshio  # noqa: F401  availability probe; ImportError falls back to the text parser
    except ImportError:
        return _parse_frd_simple(frd_path, yield_strength_mpa)

    try:
        return _parse_frd_meshio(frd_path, yield_strength_mpa)
    except Exception:  # noqa: BLE001 — meshio frd support varies; text parser is authoritative
        log.debug("meshio could not read %s; using text parser", frd_path, exc_info=True)
        return _parse_frd_simple(frd_path, yield_strength_mpa)


def _parse_frd_meshio(frd_path: Path, yield_strength_mpa: float) -> FEAResult:
    """Parse .frd with meshio."""
    import meshio
    import numpy as np

    # meshio can't deduce "frd" from the extension — name the format explicitly.
    mesh = meshio.read(str(frd_path), file_format="frd")

    # Extract displacement
    max_disp = 0.0
    for key in ("displacement", "U", "DISP"):
        if key in mesh.point_data:
            disp = mesh.point_data[key]
            magnitudes = np.linalg.norm(disp, axis=1)
            max_disp = float(np.max(magnitudes))
            break

    # Extract von Mises stress
    max_vm = 0.0
    stress_per_element: list[float] = []
    for key in ("stress", "S", "STRESS"):
        if key in mesh.cell_data:
            for block_stress in mesh.cell_data[key]:
                if block_stress.ndim == 2 and block_stress.shape[1] >= 6:
                    # Full stress tensor: Sxx, Syy, Szz, Sxy, Sxz, Syz
                    sxx = block_stress[:, 0]
                    syy = block_stress[:, 1]
                    szz = block_stress[:, 2]
                    sxy = block_stress[:, 3]
                    sxz = block_stress[:, 4]
                    syz = block_stress[:, 5]
                    vm = np.sqrt(
                        0.5
                        * (
                            (sxx - syy) ** 2
                            + (syy - szz) ** 2
                            + (szz - sxx) ** 2
                            + 6 * (sxy**2 + sxz**2 + syz**2)
                        )
                    )
                    stress_per_element.extend(float(v) for v in vm)
                    max_vm = max(max_vm, float(np.max(vm)))
                elif block_stress.ndim == 1:
                    # Scalar von Mises directly
                    stress_per_element.extend(float(v) for v in block_stress)
                    max_vm = max(max_vm, float(np.max(block_stress)))
            break
        if key in mesh.point_data:
            pdata = mesh.point_data[key]
            if pdata.ndim == 1:
                max_vm = float(np.max(pdata))
                stress_per_element = [float(v) for v in pdata]
            break

    sf = yield_strength_mpa / max_vm if max_vm > 0 else float("inf")

    return FEAResult(
        mesh_density="",
        max_von_mises_mpa=max_vm,
        max_displacement_mm=max_disp,
        element_count=sum(len(c.data) for c in mesh.cells),
        stress_per_element=stress_per_element,
        yield_safety_factor=sf,
        filtered_max_stress_mpa=max_vm,
    )


def _parse_frd_simple(frd_path: Path, yield_strength_mpa: float) -> FEAResult:
    """Simple .frd text parser fallback when meshio is unavailable."""
    text = frd_path.read_text(errors="replace")
    lines = text.splitlines()

    max_disp = 0.0
    max_stress = 0.0
    stress_values: list[float] = []
    in_disp = False
    in_stress = False

    for line in lines:
        # FRD format: blocks start with " -4  DISP" or " -4  STRESS"
        if " -4  DISP" in line:
            in_disp = True
            in_stress = False
            continue
        if " -4  STRESS" in line:
            in_stress = True
            in_disp = False
            continue
        if line.startswith(" -3"):
            in_disp = False
            in_stress = False
            continue

        if in_disp and line.startswith(" -1"):
            # Node data line: " -1      nodeID  ux  uy  uz"
            parts = line.split()
            if len(parts) >= 5:
                try:
                    ux = float(parts[2])
                    uy = float(parts[3])
                    uz = float(parts[4])
                    mag = (ux**2 + uy**2 + uz**2) ** 0.5
                    max_disp = max(max_disp, mag)
                except (ValueError, IndexError):
                    pass

        if in_stress and line.startswith(" -1"):
            parts = line.split()
            if len(parts) >= 7:
                try:
                    sxx = float(parts[2])
                    syy = float(parts[3])
                    szz = float(parts[4])
                    sxy = float(parts[5])
                    sxz = float(parts[6])
                    syz = float(parts[7]) if len(parts) > 7 else 0.0
                    vm = (
                        0.5
                        * (
                            (sxx - syy) ** 2
                            + (syy - szz) ** 2
                            + (szz - sxx) ** 2
                            + 6 * (sxy**2 + sxz**2 + syz**2)
                        )
                    ) ** 0.5
                    stress_values.append(vm)
                    max_stress = max(max_stress, vm)
                except (ValueError, IndexError):
                    pass

    sf = yield_strength_mpa / max_stress if max_stress > 0 else float("inf")

    return FEAResult(
        mesh_density="",
        max_von_mises_mpa=max_stress,
        max_displacement_mm=max_disp,
        element_count=len(stress_values),
        stress_per_element=stress_values,
        yield_safety_factor=sf,
        filtered_max_stress_mpa=max_stress,
    )


# ---------------------------------------------------------------------------
# Singularity detection
# ---------------------------------------------------------------------------


def detect_singularities(
    result: FEAResult,
    mesh_info: MeshInfo | None = None,
) -> list[SingularityFlag]:
    """Flag top-5% stress elements that are likely singularities.

    Simple heuristic: elements where stress > 2x median of all stresses
    AND in the top 5% are flagged as potential singularities near sharp
    re-entrant features.
    """
    stresses = result.stress_per_element
    if not stresses:
        return []

    sorted_s = sorted(stresses)
    n = len(sorted_s)
    median = sorted_s[n // 2]

    if median < 1e-9:
        return []

    # Top 5% threshold
    p95_idx = max(0, int(n * 0.95) - 1)
    p95_threshold = sorted_s[p95_idx]

    flags: list[SingularityFlag] = []
    for i, s in enumerate(stresses):
        if s >= p95_threshold and s > 2.0 * median:
            flags.append(
                SingularityFlag(
                    element_id=i,
                    stress_mpa=s,
                    reason="stress > 2x median in top 5%",
                )
            )

    return flags


def filter_stress_excluding_singularities(
    result: FEAResult,
    flags: list[SingularityFlag],
) -> float:
    """Return max stress after excluding flagged singular elements."""
    flagged_ids = {f.element_id for f in flags}
    filtered = [s for i, s in enumerate(result.stress_per_element) if i not in flagged_ids]
    return max(filtered) if filtered else result.max_von_mises_mpa


# ---------------------------------------------------------------------------
# Convergence check
# ---------------------------------------------------------------------------


def check_convergence(
    coarse_stress: float,
    fine_stress: float,
    threshold_pct: float = 10.0,
) -> tuple[float, bool]:
    """Check mesh convergence between two density levels.

    Returns (percent_difference, converged).
    """
    if fine_stress < 1e-9:
        return 0.0, True
    pct = abs(fine_stress - coarse_stress) / fine_stress * 100
    return pct, pct < threshold_pct


# ---------------------------------------------------------------------------
# Full L2 pipeline
# ---------------------------------------------------------------------------


def run_l2_fea(
    step_path: Path,
    subsystem: Subsystem,
    interfaces: list[Interface],
    material: Material,
    work_dir: Path,
) -> FEAReport:
    """Run the complete L2 FEA pipeline on a STEP file.

    1. Mesh at two densities (coarse = 2x min_feature, fine = 1x min_feature)
    2. Build boundary conditions from interface loads
    3. Run CalculiX twice
    4. Parse results, detect singularities, check convergence

    Returns an FEAReport with pass/fail and safety factor.
    """
    report = FEAReport(subsystem_name=subsystem.name)
    work_dir.mkdir(parents=True, exist_ok=True)

    min_feat = subsystem.manufacturing.min_feature_size_mm
    coarse_size = max(2.0 * min_feat, 1.0)
    fine_size = max(1.0 * min_feat, 0.5)

    # Mesh at coarse density
    log.info("Meshing %s at %.1f mm (coarse)", subsystem.name, coarse_size)
    coarse_dir = work_dir / "coarse"
    coarse_mesh = mesh_step(step_path, coarse_size, coarse_dir)

    # Extract nodes and build BCs
    mesh_nodes = extract_mesh_nodes(coarse_mesh.path)
    bcs = map_interface_bcs(subsystem, interfaces, mesh_nodes)

    if not bcs:
        log.warning("No boundary conditions for %s — skipping FEA", subsystem.name)
        report.passed = True
        report.safety_factor = float("inf")
        return report

    # Build node sets from BCs
    node_sets: dict[str, list[int]] = {}
    for bc in bcs:
        if bc.node_set_name not in node_sets:
            near = find_nodes_near_frame(
                mesh_nodes,
                _get_frame_for_bc(bc, interfaces, subsystem),
                5.0,
            )
            node_sets[bc.node_set_name] = near

    # Coarse run
    coarse_inp = build_inp(coarse_mesh.path, material, bcs, coarse_dir / "analysis.inp", node_sets)
    coarse_frd = run_ccx(coarse_inp)
    coarse_result = parse_frd(coarse_frd, material.yield_strength_mpa)
    coarse_result.mesh_density = "coarse"
    coarse_result.element_count = coarse_mesh.element_count

    # Singularity detection on coarse
    coarse_flags = detect_singularities(coarse_result, coarse_mesh)
    coarse_result.singularity_flags = coarse_flags
    coarse_result.filtered_max_stress_mpa = filter_stress_excluding_singularities(
        coarse_result, coarse_flags
    )

    # Mesh at fine density
    log.info("Meshing %s at %.1f mm (fine)", subsystem.name, fine_size)
    fine_dir = work_dir / "fine"
    fine_mesh = mesh_step(step_path, fine_size, fine_dir)

    fine_nodes = extract_mesh_nodes(fine_mesh.path)
    fine_bcs = map_interface_bcs(subsystem, interfaces, fine_nodes)
    fine_node_sets: dict[str, list[int]] = {}
    for bc in fine_bcs:
        if bc.node_set_name not in fine_node_sets:
            near = find_nodes_near_frame(
                fine_nodes,
                _get_frame_for_bc(bc, interfaces, subsystem),
                5.0,
            )
            fine_node_sets[bc.node_set_name] = near

    fine_inp = build_inp(
        fine_mesh.path, material, fine_bcs, fine_dir / "analysis.inp", fine_node_sets
    )
    fine_frd = run_ccx(fine_inp)
    fine_result = parse_frd(fine_frd, material.yield_strength_mpa)
    fine_result.mesh_density = "fine"
    fine_result.element_count = fine_mesh.element_count

    fine_flags = detect_singularities(fine_result, fine_mesh)
    fine_result.singularity_flags = fine_flags
    fine_result.filtered_max_stress_mpa = filter_stress_excluding_singularities(
        fine_result, fine_flags
    )

    # Convergence
    conv_pct, converged = check_convergence(
        coarse_result.filtered_max_stress_mpa,
        fine_result.filtered_max_stress_mpa,
    )

    # Assemble report
    report.coarse = coarse_result
    report.fine = fine_result
    report.convergence_pct = conv_pct
    report.converged = converged
    report.filtered_max_stress_mpa = fine_result.filtered_max_stress_mpa
    report.safety_factor = (
        material.yield_strength_mpa / fine_result.filtered_max_stress_mpa
        if fine_result.filtered_max_stress_mpa > 0
        else float("inf")
    )
    report.passed = report.safety_factor >= 1.0 and converged

    log.info(
        "L2 FEA %s: max_stress=%.1f MPa, SF=%.2f, conv=%.1f%%, %s",
        subsystem.name,
        report.filtered_max_stress_mpa,
        report.safety_factor,
        report.convergence_pct,
        "PASS" if report.passed else "FAIL",
    )

    return report


def _get_frame_for_bc(
    bc: FEABoundaryCondition,
    interfaces: list[Interface],
    subsystem: Subsystem,
) -> Any:
    """Find the coordinate frame associated with a BC's node set."""
    from orchestrator.spec import CoordinateFrame

    # Extract interface ID from node set name (ifc_XXXX)
    ifc_id = bc.node_set_name.replace("ifc_", "")
    for ifc in interfaces:
        if ifc.id == ifc_id:
            if ifc.subsystem_a == subsystem.name:
                return ifc.frame_a
            return ifc.frame_b
    return CoordinateFrame()


# ---------------------------------------------------------------------------
# L3 stub
# ---------------------------------------------------------------------------


def run_l3_fea(*args: Any, **kwargs: Any) -> None:
    """L3 high-fidelity FEA — not yet implemented.

    Future: nonlinear materials, fatigue (S-N curves), refined meshing.
    """
    raise NotImplementedError("L3 high-fidelity FEA is not yet implemented")
