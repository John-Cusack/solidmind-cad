"""Elmer FEM solver for steady-state thermal analysis.

Generates Elmer Solver Input (.sif) files, runs ElmerGrid for mesh
conversion, and parses .vtu output via meshio.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from server.analysis_models import (
    AnalysisCheck,
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    FieldResult,
    MeshInfo,
    ScalarFieldSummary,
)
from server.analysis_solvers import FieldSolver

log = logging.getLogger("solidmind.analysis_solver_elmer")

# Thermal BC type → Elmer .sif keyword mapping
_BC_TYPE_MAP: dict[str, str] = {
    "temperature": "Temperature",
    "heat_flux": "Heat Flux",
    "convection": "Heat Transfer Coefficient",
}


class ElmerSolver(FieldSolver):
    """Elmer FEM solver — steady-state thermal and conjugate heat transfer."""

    def name(self) -> str:
        return "elmer"

    def analysis_types(self) -> list[AnalysisType]:
        return [AnalysisType.THERMAL, AnalysisType.CONJUGATE_HEAT]

    def available(self) -> tuple[bool, str]:
        solver = shutil.which("ElmerSolver")
        grid = shutil.which("ElmerGrid")
        if solver and grid:
            return True, f"ElmerSolver at {solver}, ElmerGrid at {grid}"
        missing: list[str] = []
        if not solver:
            missing.append("ElmerSolver")
        if not grid:
            missing.append("ElmerGrid")
        return False, (
            f"{', '.join(missing)} not found. "
            "Install with: apt install elmer (or from https://www.elmerfem.org/)"
        )

    def write_input(
        self,
        spec: AnalysisSpec,
        mesh_info: MeshInfo,
        work_dir: Path,
    ) -> Path:
        """Convert Gmsh mesh and write Elmer .sif input file."""
        # Convert Gmsh .msh → Elmer mesh database
        mesh_db = work_dir / "mesh_db"
        self._run_elmer_grid(mesh_info.path, mesh_db)

        # Read body/boundary tags from ElmerGrid's mesh.names
        all_tags = self._read_all_body_tags(mesh_db)
        boundary_tags = self._read_boundary_tags(mesh_db)

        if spec.analysis_type == AnalysisType.CONJUGATE_HEAT:
            solid_tag = all_tags.get("solid", 1)
            fluid_tag = all_tags.get("fluid", 2)
            sif_lines = self._build_conjugate_sif(
                spec, mesh_info, mesh_db,
                solid_tag=solid_tag,
                fluid_tag=fluid_tag,
                boundary_tags=boundary_tags,
            )
        else:
            body_tag = self._read_body_tag(mesh_db)
            sif_lines = self._build_thermal_sif(spec, mesh_info, mesh_db, body_tag)

        sif_path = work_dir / "case.sif"
        sif_path.write_text("\n".join(sif_lines))
        return sif_path

    @staticmethod
    def _read_boundary_tags(mesh_db: Path) -> dict[str, int]:
        """Read boundary name → tag mapping from ElmerGrid mesh.names."""
        tags: dict[str, int] = {}
        names_file = mesh_db / "mesh.names"
        if not names_file.exists():
            return tags
        in_boundaries = False
        for line in names_file.read_text().splitlines():
            stripped = line.strip()
            if "names for boundaries" in stripped.lower():
                in_boundaries = True
                continue
            if stripped.startswith("!") and "names for" in stripped.lower():
                in_boundaries = False
                continue
            if in_boundaries and stripped.startswith("$"):
                parts = stripped.lstrip("$ ").split("=")
                if len(parts) == 2:
                    name = parts[0].strip()
                    try:
                        tags[name] = int(parts[1].strip())
                    except ValueError:
                        pass
        return tags

    @staticmethod
    def _read_body_tag(mesh_db: Path) -> int:
        """Read the volume body tag from ElmerGrid's mesh.names file."""
        tags = ElmerSolver._read_all_body_tags(mesh_db)
        # For single-body thermal: return the first volume tag
        if "volume" in tags:
            return tags["volume"]
        if "solid" in tags:
            return tags["solid"]
        # Return first tag found, or fallback to 1
        for name, tag in tags.items():
            return tag
        return 1

    @staticmethod
    def _read_all_body_tags(mesh_db: Path) -> dict[str, int]:
        """Read all body tags from ElmerGrid's mesh.names file.

        Returns dict mapping body name → Elmer body tag.
        For CHT meshes this includes 'solid' and 'fluid'.
        """
        tags: dict[str, int] = {}
        names_file = mesh_db / "mesh.names"
        if not names_file.exists():
            return tags
        in_bodies = False
        for line in names_file.read_text().splitlines():
            stripped = line.strip()
            if "names for bodies" in stripped.lower():
                in_bodies = True
                continue
            if "names for boundaries" in stripped.lower():
                in_bodies = False
                continue
            if in_bodies and stripped.startswith("$"):
                # Format: $ solid = 3
                parts = stripped.lstrip("$ ").split("=")
                if len(parts) == 2:
                    name = parts[0].strip()
                    try:
                        tags[name] = int(parts[1].strip())
                    except ValueError:
                        pass
        return tags

    def run(self, input_path: Path, work_dir: Path) -> float:
        """Execute ElmerSolver."""
        t0 = time.monotonic()
        result = subprocess.run(
            ["ElmerSolver", str(input_path)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=600,
        )
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            log.error(
                "ElmerSolver failed (rc=%d): %s",
                result.returncode,
                result.stderr[:500],
            )
            raise RuntimeError(
                f"ElmerSolver failed (rc={result.returncode}): "
                f"{result.stderr[:500]}"
            )

        return elapsed

    def parse_results(
        self,
        work_dir: Path,
        spec: AnalysisSpec,
    ) -> FieldResult:
        """Parse Elmer .vtu output for temperature field."""
        import numpy as np

        vtu_path = _find_vtu(work_dir)
        if vtu_path is None:
            return _failed_result("No .vtu output found — solver may have failed")

        try:
            import meshio
            mesh = meshio.read(str(vtu_path))
        except Exception as exc:
            return _failed_result(f"Failed to read .vtu: {exc}")

        # Extract temperature field
        temp = _extract_field(mesh, "temperature")
        if temp is None:
            return _failed_result("No temperature field in .vtu output")

        t_min = float(np.min(temp))
        t_max = float(np.max(temp))
        t_mean = float(np.mean(temp))

        # Location of max temperature
        max_idx = int(np.argmax(temp))
        max_loc = tuple(float(v) for v in mesh.points[max_idx])

        fields: list[ScalarFieldSummary] = [
            ScalarFieldSummary(
                field_name="temperature",
                min_val=round(t_min, 2),
                max_val=round(t_max, 2),
                mean_val=round(t_mean, 2),
                unit="K",
                max_location_xyz=(
                    round(max_loc[0], 4),
                    round(max_loc[1], 4),
                    round(max_loc[2], 4),
                ),
            ),
        ]

        # Try to extract heat flux magnitude
        flux = _extract_flux_magnitude(mesh)
        if flux is not None:
            f_min = float(np.min(flux))
            f_max = float(np.max(flux))
            f_mean = float(np.mean(flux))
            flux_max_idx = int(np.argmax(flux))
            flux_loc = tuple(float(v) for v in mesh.points[flux_max_idx])
            fields.append(
                ScalarFieldSummary(
                    field_name="heat_flux",
                    min_val=round(f_min, 2),
                    max_val=round(f_max, 2),
                    mean_val=round(f_mean, 2),
                    unit="W/m^2",
                    max_location_xyz=(
                        round(flux_loc[0], 4),
                        round(flux_loc[1], 4),
                        round(flux_loc[2], 4),
                    ),
                ),
            )

        # Compute safety factor and checks
        checks, safety_factor = self._evaluate_thermal(
            t_max, t_min, spec,
        )

        analysis_id = f"thermal_{uuid.uuid4().hex[:8]}"
        return FieldResult(
            analysis_id=analysis_id,
            status=_overall_status(checks),
            safety_factor=round(safety_factor, 2),
            max_von_mises_mpa=0.0,
            max_displacement_mm=0.0,
            checks=tuple(checks),
            scalar_fields=tuple(fields),
            solver_name="elmer",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_elmer_grid(self, msh_path: str, out_dir: Path) -> None:
        """Convert Gmsh .msh → Elmer mesh database via ElmerGrid."""
        result = subprocess.run(
            [
                "ElmerGrid", "14", "2",
                str(msh_path),
                "-out", str(out_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ElmerGrid failed (rc={result.returncode}): "
                f"{result.stderr[:500]}"
            )

    def _build_sif(
        self,
        spec: AnalysisSpec,
        mesh_info: MeshInfo,
        mesh_db: Path,
        body_tag: int = 1,
    ) -> list[str]:
        """Generate Elmer .sif file content (test-facing entry point)."""
        if spec.analysis_type == AnalysisType.CONJUGATE_HEAT:
            return self._build_conjugate_sif(
                spec, mesh_info, mesh_db,
                solid_tag=body_tag, fluid_tag=body_tag + 1,
            )
        return self._build_thermal_sif(spec, mesh_info, mesh_db, body_tag)

    def _build_thermal_sif(
        self,
        spec: AnalysisSpec,
        mesh_info: MeshInfo,
        mesh_db: Path,
        body_tag: int = 1,
    ) -> list[str]:
        """Generate Elmer .sif for pure conduction/convection thermal."""
        mat = spec.material
        lines: list[str] = []

        # Header
        lines.append("Header")
        lines.append(f'  Mesh DB "." "{mesh_db.name}"')
        lines.append("End")
        lines.append("")

        # Simulation
        lines.append("Simulation")
        lines.append("  Coordinate System = Cartesian")
        lines.append("  Simulation Type = Steady state")
        lines.append("  Steady State Max Iterations = 1")
        lines.append('  Post File = "case.vtu"')
        lines.append("  Output Intervals(1) = 1")
        lines.append("End")
        lines.append("")

        # Body — Target Bodies maps to ElmerGrid volume tag
        lines.append("Body 1")
        lines.append(f"  Target Bodies(1) = {body_tag}")
        lines.append("  Equation = 1")
        lines.append("  Material = 1")
        lines.append("End")
        lines.append("")

        # Material
        lines.append("Material 1")
        lines.append(f"  Density = {mat.density_kg_m3}")
        lines.append(f"  Heat Conductivity = {mat.thermal_conductivity_w_mk}")
        if mat.specific_heat_j_kgk > 0:
            lines.append(f"  Heat Capacity = {mat.specific_heat_j_kgk}")
        lines.append("End")
        lines.append("")

        # Solver
        lines.append("Solver 1")
        lines.append("  Equation = Heat Equation")
        lines.append("  Variable = Temperature")
        lines.append('  Procedure = "HeatSolve" "HeatSolver"')
        lines.append("  Linear System Solver = Iterative")
        lines.append("  Linear System Iterative Method = BiCGStab")
        lines.append("  Linear System Preconditioning = ILU0")
        lines.append("  Linear System Max Iterations = 500")
        lines.append("  Linear System Convergence Tolerance = 1.0e-8")
        lines.append("  Steady State Convergence Tolerance = 1.0e-6")
        lines.append("End")
        lines.append("")

        # Equation
        lines.append("Equation 1")
        lines.append("  Active Solvers(1) = 1")
        lines.append("End")
        lines.append("")

        # Boundary conditions
        for i, bc in enumerate(spec.boundary_conditions):
            bc_lines = self._bc_to_sif(i + 1, bc, mesh_info)
            lines.extend(bc_lines)
            lines.append("")

        return lines

    def _build_conjugate_sif(
        self,
        spec: AnalysisSpec,
        mesh_info: MeshInfo,
        mesh_db: Path,
        solid_tag: int = 1,
        fluid_tag: int = 2,
        boundary_tags: dict[str, int] | None = None,
    ) -> list[str]:
        """Generate Elmer .sif for coupled Navier-Stokes + Heat (CHT).

        Two-body setup:
          Body 1 = solid (the CAD part, conduction only)
          Body 2 = fluid domain (air/water, flow + convection)

        Elmer solves the flow field in the fluid domain, computes wall
        heat transfer at the solid-fluid interface automatically, and
        solves conduction inside the solid — all in one coupled run.
        """
        if boundary_tags is None:
            boundary_tags = {}
        mat = spec.material
        lines: list[str] = []

        # Extract flow parameters from BCs
        thermal_bcs = [bc for bc in spec.boundary_conditions if bc.bc_type in (
            "temperature", "heat_flux", "convection",
        )]

        # Fluid properties (default: air at 20°C)
        fluid_density = 1.225
        fluid_viscosity = 1.789e-5
        fluid_conductivity = 0.026
        fluid_capacity = 1006.0
        inlet_vx, inlet_vy, inlet_vz = 0.0, 0.0, 0.0
        inlet_temp = 300.0
        for bc in spec.boundary_conditions:
            if bc.bc_type == "velocity_inlet":
                fluid_density = bc.value.get("fluid_density_kg_m3", fluid_density)
                fluid_viscosity = bc.value.get("fluid_viscosity_pa_s", fluid_viscosity)
                fluid_conductivity = bc.value.get(
                    "fluid_conductivity_w_mk", fluid_conductivity,
                )
                fluid_capacity = bc.value.get(
                    "fluid_capacity_j_kgk", fluid_capacity,
                )
                inlet_vx = bc.value.get("vx_m_s", 0.0)
                inlet_vy = bc.value.get("vy_m_s", 0.0)
                inlet_vz = bc.value.get("vz_m_s", 0.0)
                inlet_temp = bc.value.get("temperature_k", 300.0)

        # Header
        lines.append("Header")
        lines.append(f'  Mesh DB "." "{mesh_db.name}"')
        lines.append("End")
        lines.append("")

        # Simulation — coupled needs more outer iterations
        lines.append("Simulation")
        lines.append("  Coordinate System = Cartesian")
        lines.append("  Simulation Type = Steady state")
        lines.append("  Steady State Max Iterations = 50")
        lines.append('  Post File = "case.vtu"')
        lines.append("  Output Intervals(1) = 1")
        lines.append("End")
        lines.append("")

        # Body 1: solid domain
        lines.append("Body 1")
        lines.append(f"  Target Bodies(1) = {solid_tag}")
        lines.append("  Equation = 1")
        lines.append("  Material = 1")
        lines.append("End")
        lines.append("")

        # Body 2: fluid domain
        lines.append("Body 2")
        lines.append(f"  Target Bodies(1) = {fluid_tag}")
        lines.append("  Equation = 2")
        lines.append("  Material = 2")
        lines.append("End")
        lines.append("")

        # Material 1: solid
        lines.append("Material 1")
        lines.append(f"  Density = {mat.density_kg_m3}")
        lines.append(f"  Heat Conductivity = {mat.thermal_conductivity_w_mk}")
        if mat.specific_heat_j_kgk > 0:
            lines.append(f"  Heat Capacity = {mat.specific_heat_j_kgk}")
        lines.append("End")
        lines.append("")

        # Material 2: fluid (air by default)
        lines.append("Material 2")
        lines.append(f"  Density = {fluid_density}")
        lines.append(f"  Viscosity = {fluid_viscosity}")
        lines.append(f"  Heat Conductivity = {fluid_conductivity}")
        lines.append(f"  Heat Capacity = {fluid_capacity}")
        lines.append("End")
        lines.append("")

        # Solver 1: Navier-Stokes (fluid domain only)
        lines.append("Solver 1")
        lines.append("  Equation = Navier-Stokes")
        lines.append('  Procedure = "FlowSolve" "FlowSolver"')
        lines.append("  Variable = Flow Solution[Velocity:3 Pressure:1]")
        lines.append("  Stabilize = True")
        lines.append("  Linear System Solver = Iterative")
        lines.append("  Linear System Iterative Method = BiCGStabl")
        lines.append("  Linear System Preconditioning = ILU1")
        lines.append("  Linear System Max Iterations = 500")
        lines.append("  Linear System Convergence Tolerance = 1.0e-6")
        lines.append("  Nonlinear System Max Iterations = 20")
        lines.append("  Nonlinear System Convergence Tolerance = 1.0e-5")
        lines.append("  Steady State Convergence Tolerance = 1.0e-5")
        lines.append("End")
        lines.append("")

        # Solver 2: Heat Equation (both domains, coupled to flow)
        lines.append("Solver 2")
        lines.append("  Equation = Heat Equation")
        lines.append("  Variable = Temperature")
        lines.append('  Procedure = "HeatSolve" "HeatSolver"')
        lines.append("  Linear System Solver = Iterative")
        lines.append("  Linear System Iterative Method = BiCGStab")
        lines.append("  Linear System Preconditioning = ILU0")
        lines.append("  Linear System Max Iterations = 500")
        lines.append("  Linear System Convergence Tolerance = 1.0e-8")
        lines.append("  Steady State Convergence Tolerance = 1.0e-6")
        lines.append("End")
        lines.append("")

        # Equation 1: solid — heat only (no flow)
        lines.append("Equation 1")
        lines.append("  Active Solvers(1) = 2")
        lines.append("  Convection = None")
        lines.append("End")
        lines.append("")

        # Equation 2: fluid — flow + heat (advection-diffusion)
        lines.append("Equation 2")
        lines.append("  Active Solvers(2) = 1 2")
        lines.append("  Convection = Computed")
        lines.append("End")
        lines.append("")

        # Boundary conditions — use ElmerGrid boundary tags from mesh.names
        bc_idx = 1

        # BC 1: Fluid inlet (velocity + temperature)
        inlet_tag = boundary_tags.get("fluid_inlet", bc_idx)
        lines.append(f"Boundary Condition {bc_idx}")
        lines.append(f"  Target Boundaries(1) = {inlet_tag}")
        lines.append(f"  Velocity 1 = {inlet_vx}")
        lines.append(f"  Velocity 2 = {inlet_vy}")
        lines.append(f"  Velocity 3 = {inlet_vz}")
        lines.append(f"  Temperature = {inlet_temp}")
        lines.append("End")
        lines.append("")
        bc_idx += 1

        # BC 2: Fluid outlet (pressure = 0)
        outlet_tag = boundary_tags.get("fluid_outlet", bc_idx)
        lines.append(f"Boundary Condition {bc_idx}")
        lines.append(f"  Target Boundaries(1) = {outlet_tag}")
        lines.append("  Pressure = 0")
        lines.append("End")
        lines.append("")
        bc_idx += 1

        # BC 3: Fluid outer walls (no-slip, adiabatic)
        wall_tag = boundary_tags.get("fluid_walls", bc_idx)
        lines.append(f"Boundary Condition {bc_idx}")
        lines.append(f"  Target Boundaries(1) = {wall_tag}")
        lines.append("  Velocity 1 = 0")
        lines.append("  Velocity 2 = 0")
        lines.append("  Velocity 3 = 0")
        lines.append("End")
        lines.append("")
        bc_idx += 1

        # BC 4: Solid-fluid interface (no-slip wall, coupled heat transfer)
        iface_tag = boundary_tags.get("interface", bc_idx)
        lines.append(f"Boundary Condition {bc_idx}")
        lines.append(f"  Target Boundaries(1) = {iface_tag}")
        lines.append("  Velocity 1 = 0")
        lines.append("  Velocity 2 = 0")
        lines.append("  Velocity 3 = 0")
        lines.append("End")
        lines.append("")
        bc_idx += 1

        # BC 5+: Thermal BCs on solid surfaces (heat source, etc.)
        for bc in thermal_bcs:
            bc_lines = self._bc_to_sif(bc_idx, bc, mesh_info)
            lines.extend(bc_lines)
            lines.append("")
            bc_idx += 1

        return lines

    def _bc_to_sif(
        self,
        bc_index: int,
        bc: BoundaryCondition,
        mesh_info: MeshInfo,
    ) -> list[str]:
        """Convert a BoundaryCondition to Elmer .sif Boundary Condition block."""
        lines: list[str] = []
        lines.append(f"Boundary Condition {bc_index}")

        # Resolve physical group tag for the BC faces
        tag = self._resolve_bc_tag(bc, mesh_info, bc_index)
        lines.append(f"  Target Boundaries(1) = {tag}")

        if bc.bc_type == "temperature":
            temp_k = bc.value.get("temperature_k", 293.15)
            lines.append(f"  Temperature = {temp_k}")

        elif bc.bc_type == "heat_flux":
            flux = bc.value.get("flux_w_m2", 0.0)
            lines.append("  Heat Flux BC = Logical True")
            lines.append(f"  Heat Flux = {flux}")

        elif bc.bc_type == "convection":
            htc = bc.value.get("htc_w_m2k", 10.0)
            t_amb = bc.value.get("t_ambient_k", 293.15)
            lines.append(f"  Heat Transfer Coefficient = {htc}")
            lines.append(f"  External Temperature = {t_amb}")

        lines.append("End")
        return lines

    def _resolve_bc_tag(
        self,
        bc: BoundaryCondition,
        mesh_info: MeshInfo,
        fallback_index: int,
    ) -> int:
        """Get Gmsh physical group tag for the BC faces."""
        if mesh_info.physical_groups:
            for face_ref in bc.faces:
                tag = mesh_info.physical_groups.get(face_ref)
                if tag is not None:
                    return tag
        return fallback_index

    def _evaluate_thermal(
        self,
        t_max: float,
        t_min: float,
        spec: AnalysisSpec,
    ) -> tuple[list[AnalysisCheck], float]:
        """Evaluate thermal results against limits."""
        checks: list[AnalysisCheck] = []
        safety_factor = 99.0

        # Check temperature limit from BC values
        max_temp_limit = 0.0
        for bc in spec.boundary_conditions:
            limit = bc.value.get("max_temperature_k", 0.0)
            if limit > 0:
                max_temp_limit = max(max_temp_limit, limit)

        if max_temp_limit > 0 and t_max > 0:
            safety_factor = max_temp_limit / t_max
            if t_max > max_temp_limit:
                checks.append(AnalysisCheck(
                    name="overtemperature",
                    status=CheckStatus.FAIL,
                    message=(
                        f"Max temperature {t_max:.1f} K exceeds limit "
                        f"{max_temp_limit:.1f} K"
                    ),
                    measured=t_max,
                    limit=max_temp_limit,
                    suggestion="Increase cooling or reduce heat input",
                ))
            elif safety_factor < 1.2:
                checks.append(AnalysisCheck(
                    name="overtemperature",
                    status=CheckStatus.WARN,
                    message=(
                        f"Max temperature {t_max:.1f} K close to limit "
                        f"{max_temp_limit:.1f} K (margin {safety_factor:.2f})"
                    ),
                    measured=t_max,
                    limit=max_temp_limit,
                    suggestion="Consider additional cooling margin",
                ))
            else:
                checks.append(AnalysisCheck(
                    name="overtemperature",
                    status=CheckStatus.PASS,
                    message=(
                        f"Max temperature {t_max:.1f} K within limit "
                        f"{max_temp_limit:.1f} K (margin {safety_factor:.2f})"
                    ),
                    measured=t_max,
                    limit=max_temp_limit,
                ))

        # Thermal gradient check
        gradient = t_max - t_min
        if gradient > 200:
            checks.append(AnalysisCheck(
                name="thermal_gradient",
                status=CheckStatus.WARN,
                message=(
                    f"High thermal gradient: {gradient:.1f} K "
                    f"(from {t_min:.1f} K to {t_max:.1f} K). "
                    "May cause thermal stress."
                ),
                measured=gradient,
                suggestion="Consider thermal stress analysis",
            ))
        else:
            checks.append(AnalysisCheck(
                name="thermal_gradient",
                status=CheckStatus.PASS,
                message=(
                    f"Thermal gradient {gradient:.1f} K "
                    f"(from {t_min:.1f} K to {t_max:.1f} K)"
                ),
                measured=gradient,
            ))

        if not checks:
            checks.append(AnalysisCheck(
                name="thermal_solved",
                status=CheckStatus.PASS,
                message=f"Thermal solution converged. T range: {t_min:.1f}–{t_max:.1f} K",
                measured=t_max,
            ))

        return checks, safety_factor


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _find_vtu(work_dir: Path) -> Path | None:
    """Find the Elmer .vtu output file (searches subdirectories too)."""
    # Check work_dir first, then subdirs (Elmer writes into mesh_db/)
    for pattern in ("case*.vtu", "*.vtu", "**/case*.vtu", "**/*.vtu"):
        candidates = list(work_dir.glob(pattern))
        if candidates:
            return candidates[0]
    return None


def _extract_field(mesh: Any, name: str) -> Any:
    """Extract a named field from meshio point_data (case-insensitive)."""
    for key, data in mesh.point_data.items():
        if key.lower() == name.lower():
            return data
    return None


def _extract_flux_magnitude(mesh: Any) -> Any:
    """Try to extract heat flux magnitude from meshio output."""
    import numpy as np

    # Elmer may output flux as vector components or scalar magnitude
    for key, data in mesh.point_data.items():
        lower = key.lower()
        if "heat flux" in lower or "flux" in lower:
            if data.ndim == 2 and data.shape[1] >= 2:
                return np.linalg.norm(data, axis=1)
            return np.abs(data)
    return None


def _overall_status(checks: list[AnalysisCheck]) -> CheckStatus:
    """Derive overall status from individual checks."""
    if any(c.status == CheckStatus.FAIL for c in checks):
        return CheckStatus.FAIL
    if any(c.status == CheckStatus.WARN for c in checks):
        return CheckStatus.WARN
    return CheckStatus.PASS


def _failed_result(message: str) -> FieldResult:
    return FieldResult(
        analysis_id="",
        status=CheckStatus.FAIL,
        safety_factor=0.0,
        max_von_mises_mpa=0.0,
        max_displacement_mm=0.0,
        checks=(
            AnalysisCheck(
                name="solver_error",
                status=CheckStatus.FAIL,
                message=message,
                suggestion="Check Elmer installation and input files",
            ),
        ),
        scalar_fields=(),
        solver_name="elmer",
    )
