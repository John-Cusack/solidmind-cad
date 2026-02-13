"""Solver adapters for parametric design studies.

Each solver implements the SolverAdapter ABC and is registered in SOLVERS.
Real BEMT+XFOIL and OpenFOAM implementations are stubs for now.

Geometry script contract
========================
Solvers that need 3D geometry (OpenFOAM, future FEA) use a **geometry script** —
a Python file that runs in FreeCAD headless mode (``FreeCADCmd``) and produces an STL.

The LLM writes this script during ``study.create`` and stores it at
``studies/<study_id>/geometry.py``. The script must:

1. Read a JSON file path from ``sys.argv[1]`` containing the variant params + fixed params.
2. Build 3D geometry using FreeCAD Python API (Part, PartDesign, Sketcher).
3. Export the result as STL to the path given in ``sys.argv[2]``.
4. Exit 0 on success, non-zero on failure.

Example geometry script::

    import json, sys
    import FreeCAD, Part

    with open(sys.argv[1]) as f:
        p = json.load(f)
    # p = {"angle": 15.0, "chord": 25.0, "blades": 3, ...}

    doc = FreeCAD.newDocument("variant")
    # ... build geometry from p ...
    Part.export([doc.getObject("Body")], sys.argv[2])
    sys.exit(0)

The solver calls: ``FreeCADCmd geometry.py /tmp/params.json /tmp/variant.stl``
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

log = logging.getLogger("solidmind.study_solvers")


class SolverAdapter(ABC):
    """Base class for simulation solvers."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable solver name."""

    @abstractmethod
    def available(self) -> bool:
        """Check if the solver's dependencies are installed."""

    @abstractmethod
    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        """Estimated wall-clock seconds per variant. Used for time estimates."""

    @abstractmethod
    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        """Return a list of validation error strings (empty = ok)."""

    @abstractmethod
    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        """Run the solver and return metric name → value."""

    def describe_pipeline(self) -> str:
        """Human-readable description of what this solver does per variant."""
        return "run solver"


class MockSolver(SolverAdapter):
    """Deterministic mock solver for testing. Returns metrics based on params."""

    def name(self) -> str:
        return "mock"

    def available(self) -> bool:
        return True

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        return 0.01

    def describe_pipeline(self) -> str:
        return "evaluate analytical function"

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        return []

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        # Simple parabolic objective: maximize when all params near their midpoints
        total = 0.0
        for k, v in params.items():
            if isinstance(v, (int, float)):
                total += float(v)
        return {"objective": -((total - 50) ** 2) + 2500, "total_param": total}


class BEMTXfoilSolver(SolverAdapter):
    """BEMT + XFOIL solver for propeller/rotor analysis.

    Stub implementation — real solver will call XFOIL subprocess
    for airfoil Cl/Cd and run a BEM loop for thrust/torque/efficiency.

    Pipeline per variant:
    1. Build blade element geometry from params (chord, twist, airfoil)
    2. Run XFOIL at each radial station to get Cl/Cd polars
    3. BEM loop: integrate thrust and torque across blade span
    4. Return thrust_N, torque_Nm, power_W, efficiency
    """

    def name(self) -> str:
        return "bemt_xfoil"

    def available(self) -> bool:
        # TODO: check for xfoil binary on PATH
        return False

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        # ~2-10s depending on radial stations and XFOIL convergence
        stations = config_params.get("radial_stations", 15)
        return float(stations) * 0.5

    def describe_pipeline(self) -> str:
        return (
            "1. Generate blade element geometry from design params\n"
            "2. Run XFOIL at each radial station for Cl/Cd polars\n"
            "3. BEM integration loop for thrust/torque/power\n"
            "4. Extract performance metrics"
        )

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if "Re" not in config_params and "Re" not in fixed:
            errors.append("Reynolds number (Re) required in solver params or fixed_params")
        return errors

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        raise NotImplementedError("BEMT+XFOIL solver not yet implemented")


def _find_freecadcmd() -> str | None:
    """Find FreeCADCmd binary on PATH."""
    for name in ("FreeCADCmd", "freecadcmd", "freecad-cmd"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _run_geometry_script(
    script_path: str,
    params: dict[str, Any],
    fixed: dict[str, Any],
    output_stl: str,
    timeout_s: float = 120.0,
) -> None:
    """Run a geometry script in FreeCAD headless mode to produce an STL.

    Raises RuntimeError if the script fails.
    """
    freecadcmd = _find_freecadcmd()
    if not freecadcmd:
        raise RuntimeError("FreeCADCmd not found on PATH")

    # Write merged params to a temp JSON file
    merged = {**fixed, **params}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(merged, f)
        params_path = f.name

    try:
        result = subprocess.run(
            [freecadcmd, script_path, params_path, output_stl],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Geometry script failed (exit {result.returncode}):\n"
                f"stderr: {result.stderr[:500]}"
            )
        if not Path(output_stl).exists():
            raise RuntimeError(f"Geometry script did not produce {output_stl}")
    finally:
        Path(params_path).unlink(missing_ok=True)


class OpenFOAMSolver(SolverAdapter):
    """OpenFOAM CFD solver using FreeCAD headless for geometry generation.

    Pipeline per variant:
    1. Run geometry script in FreeCAD headless (FreeCADCmd) to produce STL
    2. Set up OpenFOAM case directory from template
    3. blockMesh → snappyHexMesh (mesh around STL)
    4. simpleFoam (steady-state RANS) or pimpleFoam (transient)
    5. Extract forces/moments from postProcessing/forces/
    6. Return lift_N, drag_N, moment_Nm, Cl, Cd, etc.

    Required config_params:
        mesh_refinement (int): 1-4, controls mesh density and solve time
        geometry_script (str): path to FreeCAD headless geometry script (set by study)

    Optional config_params:
        turbulence_model (str): "kOmegaSST" (default), "kEpsilon", "SpalartAllmaras"
        n_processors (int): parallel decomposition (default 1)
    """

    # Rough time estimates by mesh refinement level
    _TIME_BY_REFINEMENT: dict[int, float] = {
        1: 120.0,   # ~2 min — coarse mesh, quick feasibility
        2: 300.0,   # ~5 min — standard
        3: 900.0,   # ~15 min — fine mesh
        4: 2400.0,  # ~40 min — very fine
    }

    def name(self) -> str:
        return "openfoam"

    def available(self) -> bool:
        has_openfoam = shutil.which("simpleFoam") is not None
        has_freecad = _find_freecadcmd() is not None
        return has_openfoam and has_freecad

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        level = config_params.get("mesh_refinement", 2)
        # Add ~30s for geometry generation via FreeCAD headless
        return self._TIME_BY_REFINEMENT.get(level, 300.0) + 30.0

    def describe_pipeline(self) -> str:
        return (
            "1. Run geometry script in FreeCAD headless (FreeCADCmd) → STL\n"
            "2. Set up OpenFOAM case directory\n"
            "3. blockMesh + snappyHexMesh (mesh generation around STL)\n"
            "4. simpleFoam RANS simulation\n"
            "5. Extract forces from postProcessing/\n"
            "6. Return aerodynamic coefficients"
        )

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if "mesh_refinement" not in config_params:
            errors.append("mesh_refinement level required in solver params")
        # geometry_script is checked at solve time (set after study.create)
        return errors

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        geometry_script = config_params.get("geometry_script")
        if not geometry_script:
            raise RuntimeError("geometry_script path required in solver config_params")
        if not Path(geometry_script).is_file():
            raise RuntimeError(f"Geometry script not found: {geometry_script}")

        # Create variant working directory
        case_dir = tempfile.mkdtemp(prefix="openfoam_variant_")
        stl_path = str(Path(case_dir) / "geometry.stl")

        try:
            # Step 1: Generate STL via FreeCAD headless
            log.info("Generating geometry via FreeCAD headless...")
            _run_geometry_script(
                script_path=geometry_script,
                params=params,
                fixed=fixed,
                output_stl=stl_path,
                timeout_s=config_params.get("geometry_timeout_s", 120.0),
            )

            # Steps 2-5: OpenFOAM pipeline
            # TODO: Implement case setup, meshing, solving, and force extraction
            # For now, raise to make it clear this is not yet complete
            raise NotImplementedError(
                "OpenFOAM case setup and solving not yet implemented. "
                f"Geometry STL generated successfully at {stl_path}"
            )

        finally:
            # Clean up temp case directory
            # In production, keep for debugging with a config flag
            if config_params.get("cleanup_cases", True):
                shutil.rmtree(case_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Solver registry
# ---------------------------------------------------------------------------

SOLVERS: dict[str, SolverAdapter] = {
    "mock": MockSolver(),
    "bemt_xfoil": BEMTXfoilSolver(),
    "openfoam": OpenFOAMSolver(),
}


def get_solver(solver_type: str) -> SolverAdapter:
    """Look up a solver by type string. Raises KeyError if unknown."""
    if solver_type not in SOLVERS:
        raise KeyError(f"Unknown solver type: {solver_type!r}. Available: {list(SOLVERS)}")
    return SOLVERS[solver_type]
