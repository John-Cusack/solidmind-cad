"""MCP tool implementations for field-problem analysis (analysis.* tools)."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from server.analysis_materials import get_material, list_materials
from server.analysis_mesh import mesh_step_to_cht_msh, mesh_step_to_msh
from server.analysis_models import (
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    FailureMode,
    FieldResult,
    Material,
    ReflectExpectations,
)
from server.analysis_solvers import (
    FieldSolver,
    get_solver,
    list_solvers,
)
from server.analysis_store import save_result
from server.screen_stress import screen_stress
from server.screen_thermal import screen_thermal
from server.tools_cad import cad_export_body

log = logging.getLogger("solidmind.tools_analysis")


def _resolve_material(material: str | dict[str, Any]) -> Material | None:
    """Resolve a material from a key string or inline dict."""
    if isinstance(material, str):
        return get_material(material)
    if isinstance(material, dict):
        return Material.from_dict(material)
    return None


def _resolve_screen_material(material: str | dict[str, Any]) -> Material | None:
    """Resolve a material for screening, accepting a minimal inline dict.

    Screening only needs yield + Young's modulus, so an inline dict may carry
    just ``{yield_strength_mpa, youngs_modulus_mpa}`` (the documented schema);
    the structural-only fields default. A string falls back to the catalog.
    """
    if isinstance(material, dict):
        return Material(
            name=material.get("name", "custom"),
            youngs_modulus_mpa=float(material["youngs_modulus_mpa"]),
            poissons_ratio=float(material.get("poissons_ratio", 0.3)),
            density_kg_m3=float(material.get("density_kg_m3", 0.0)),
            yield_strength_mpa=float(material["yield_strength_mpa"]),
        )
    return get_material(material)


def _resolve_thermal_screen_material(material: str | dict[str, Any]) -> Material | None:
    """Resolve a material for thermal screening, accepting a minimal inline dict.

    Thermal screening only needs conductivity / density / specific heat, so an
    inline dict may carry just those (the documented schema); the structural
    fields default. A string falls back to the catalog. Unlike the structural
    ``Material.from_dict``, this never requires structural keys — so the
    documented thermal-only inline-dict path can't raise a stray ``KeyError``.
    """
    if isinstance(material, dict):
        return Material(
            name=material.get("name", "custom"),
            youngs_modulus_mpa=float(material.get("youngs_modulus_mpa", 0.0)),
            poissons_ratio=float(material.get("poissons_ratio", 0.3)),
            density_kg_m3=float(material.get("density_kg_m3", 0.0)),
            yield_strength_mpa=float(material.get("yield_strength_mpa", 0.0)),
            thermal_conductivity_w_mk=float(material.get("thermal_conductivity_w_mk", 0.0)),
            specific_heat_j_kgk=float(material.get("specific_heat_j_kgk", 0.0)),
        )
    return get_material(material)


def _infer_failure_mode(result: FieldResult) -> FailureMode | None:
    """Infer a typed failure mode from a solved result's own signals.

    Real solvers don't know geometry intent, so this is deliberately simple: a
    displacement-governed failure is DEFLECTION; any other non-pass is treated
    as YIELD. This gives the Interpret/Decide steps a real typed value to
    dispatch on without round-tripping the caller's expectations (which would be
    circular).
    """
    if result.status == CheckStatus.PASS:
        return None
    for c in result.checks:
        if c.status != CheckStatus.PASS and (
            "displacement" in c.name.lower() or "deflection" in c.name.lower()
        ):
            return FailureMode.DEFLECTION
    return FailureMode.YIELD


def _structural_solver_chain(
    primary_solver: FieldSolver,
    *,
    allow_fallback: bool,
) -> list[FieldSolver]:
    chain_names: list[str] = [primary_solver.name()]
    if allow_fallback:
        if primary_solver.name() == "cudss":
            chain_names.extend(["cholmod", "calculix"])
        elif primary_solver.name() == "cholmod":
            chain_names.append("calculix")

    result: list[FieldSolver] = []
    seen: set[str] = set()
    for name in chain_names:
        if name in seen:
            continue
        seen.add(name)
        solver = get_solver(name, AnalysisType.STRUCTURAL)
        if solver is None:
            continue
        ok, _ = solver.available()
        if ok:
            result.append(solver)
    return result


def _run_structural_once(
    field_solver: FieldSolver,
    spec: AnalysisSpec,
    mesh_info: Any,
    analysis_id: str,
) -> tuple[FieldResult, float]:
    work_dir = Path(tempfile.mkdtemp(prefix=f"solidmind_{analysis_id}_{field_solver.name()}_"))
    t0 = time.monotonic()

    if field_solver.supports_direct_solve():
        result = field_solver.solve_direct(spec, mesh_info, work_dir)
        elapsed = time.monotonic() - t0
        solve_time = result.solve_time_s if result.solve_time_s > 0 else elapsed
        return result, solve_time

    input_path = field_solver.write_input(spec, mesh_info, work_dir)
    solve_time = field_solver.run(input_path, work_dir)
    result = field_solver.parse_results(work_dir, spec)
    return result, solve_time


def _solve_structural_with_fallback(
    *,
    analysis_id: str,
    body: str,
    material: Material,
    boundary_conditions: tuple[BoundaryCondition, ...],
    mesh_size: float,
    mesh_info: Any,
    primary_solver: FieldSolver,
    allow_fallback: bool,
) -> tuple[FieldSolver, FieldResult, float]:
    attempts: list[str] = []
    chain = _structural_solver_chain(primary_solver, allow_fallback=allow_fallback)
    if not chain:
        raise RuntimeError("No available structural solver in fallback chain")

    for solver in chain:
        spec = AnalysisSpec(
            analysis_type=AnalysisType.STRUCTURAL,
            body=body,
            material=material,
            boundary_conditions=boundary_conditions,
            mesh_size=mesh_size,
            solver=solver.name(),
        )
        try:
            result, solve_time = _run_structural_once(solver, spec, mesh_info, analysis_id)
            return solver, result, solve_time
        except Exception as exc:
            msg = f"{solver.name()}: {exc}"
            attempts.append(msg)
            log.warning("Structural solve failed for %s", msg, exc_info=True)

    raise RuntimeError("; ".join(attempts))


class StructuralSolveError(Exception):
    """Raised when the shared structural pipeline fails at a known stage.

    Carries a stable ``code`` so the MCP tool boundary can surface the same
    ``{ok: false, error: {code, message}}`` contract it always has, while batch
    callers (the orchestrator adapter) get a typed exception to translate into
    their own error type.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_requested_solver(solver: str) -> FieldSolver | None:
    """Resolve an explicit structural solver name, or None if none was requested.

    Raises ``StructuralSolveError`` (stable ``code``) when a named solver is
    unknown or unavailable, so both the MCP tool (fast-fail before the STEP
    export) and the shared engine reject a bad solver the same way.
    """
    name = solver.strip()
    if not name:
        return None
    found = get_solver(name, AnalysisType.STRUCTURAL)
    if found is None:
        available = list_solvers()
        if not any(s["available"] for s in available):
            raise StructuralSolveError(
                "NO_SOLVER",
                "No structural solver available. Install CalculiX: apt install calculix-ccx",
            )
        raise StructuralSolveError(
            "SOLVER_NOT_FOUND",
            f"Solver {solver!r} not found. Available: {[s['name'] for s in available]}",
        )
    ok, diag = found.available()
    if not ok:
        raise StructuralSolveError("SOLVER_UNAVAILABLE", diag)
    return found


def solve_structural_from_step(
    *,
    step_path: str,
    material: Material,
    boundary_conditions: Sequence[BoundaryCondition],
    mesh_size: float = 0.0,
    solver: str = "",
    body_label: str = "",
    analysis_id: str = "",
    persist: bool = True,
    mesh_order: int = 1,
) -> FieldResult:
    """Mesh a STEP file, solve a static structural step, return a ``FieldResult``.

    This is the shared engine behind both structural front doors:
    ``analysis_stress_check`` (live FreeCAD body → STEP, then this) and the
    orchestrator's batch ``run_l2_fea`` (STEP file → this, run twice for a mesh
    convergence study). Solver selection, the cuDSS→CHOLMOD→CalculiX fallback
    chain, failure-mode inference, and persistence all live here so neither
    caller re-implements them.

    ``mesh_order`` defaults to 1 (linear tet4 — what the validated foam-dart
    reference and the direct in-process solvers stand on); the batch scoring path
    passes ``mesh_order=2`` for quadratic tet10 accuracy. Raises
    ``StructuralSolveError`` (with a stable ``code``) on solver/mesh failure.
    """
    if not analysis_id:
        analysis_id = f"analysis_{uuid.uuid4().hex[:8]}"

    requested_solver_name = solver.strip()
    explicit_solver = _resolve_requested_solver(solver)

    # Mesh — one face group per BC, named to match the BC's index/type.
    face_groups: dict[str, list[str]] = {}
    for i, bc in enumerate(boundary_conditions):
        face_groups[f"bc_{i}_{bc.bc_type}"] = list(bc.faces)
    try:
        mesh_info = mesh_step_to_msh(
            step_path=step_path,
            face_groups=face_groups,
            mesh_size=mesh_size,
            order=mesh_order,
        )
    except Exception as exc:
        raise StructuralSolveError("MESH_FAILED", f"Meshing failed: {exc}") from exc

    dof_count = int(mesh_info.num_nodes) * 3
    field_solver = explicit_solver
    if field_solver is None:
        if mesh_order >= 2:
            # Quadratic tet10 is only supported by CalculiX — the in-process direct
            # solvers (CHOLMOD/cuDSS) are tet4-only — so don't let DOF-based
            # auto-selection hand the mesh to a solver that will reject it.
            field_solver = get_solver("calculix", AnalysisType.STRUCTURAL)
            if field_solver is None or not field_solver.available()[0]:
                raise StructuralSolveError(
                    "NO_TET10_SOLVER",
                    "Quadratic (tet10) FEA requires CalculiX; install calculix-ccx "
                    "(the in-process direct solvers are tet4-only).",
                )
        else:
            field_solver = get_solver("", AnalysisType.STRUCTURAL, dof_count=dof_count)
            if field_solver is None:
                raise StructuralSolveError(
                    "NO_SOLVER",
                    "No structural solver available. Install CalculiX: apt install calculix-ccx",
                )

    try:
        used_solver, result, solve_time = _solve_structural_with_fallback(
            analysis_id=analysis_id,
            body=body_label or Path(step_path).stem,
            material=material,
            boundary_conditions=tuple(boundary_conditions),
            mesh_size=mesh_size,
            mesh_info=mesh_info,
            primary_solver=field_solver,
            allow_fallback=not bool(requested_solver_name),
        )
    except Exception as exc:
        raise StructuralSolveError("SOLVE_FAILED", f"Solver failed: {exc}") from exc

    # Patch in the analysis_id and the real solver name/time, preserving every
    # other field the solver computed (filtered peak, failure_mode, candidates).
    result = dataclasses.replace(
        result,
        analysis_id=analysis_id,
        solver_name=used_solver.name(),
        solve_time_s=solve_time,
    )

    # Tag a typed failure mode inferred from the result's own signals, so the
    # Interpret/Decide steps have a real value to dispatch on.
    inferred = _infer_failure_mode(result)
    if inferred is not None:
        result = dataclasses.replace(result, failure_mode=inferred)

    if persist:
        try:
            save_result(result)
        except Exception:
            log.warning("Failed to persist analysis result", exc_info=True)

    return result


def analysis_stress_check(
    *,
    body: str,
    material: str | dict[str, Any],
    boundary_conditions: list[dict[str, Any]],
    mesh_size: float = 0.0,
    solver: str = "",
    doc: str | None = None,
) -> dict[str, Any]:
    """Run structural stress analysis on a body.

    Exports STEP → meshes with Gmsh → solves with CalculiX (or selected solver)
    → returns pass/fail with actionable results. A typed ``failure_mode`` is
    inferred from the result and stamped on it for the Interpret/Decide steps.

    Load contract (v1 direct-solver rollout):
    - ``force`` values are interpreted as total force (N) over selected faces.
    - ``pressure_mpa`` values are pressure intensity (MPa == N/mm^2).
    """
    # Resolve material
    mat = _resolve_material(material)
    if mat is None:
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_MATERIAL",
                "message": f"Material not found: {material!r}. Use analysis.list_materials to see available materials.",
            },
        }

    # Parse boundary conditions
    bcs: list[BoundaryCondition] = []
    for bc_dict in boundary_conditions:
        bcs.append(BoundaryCondition.from_dict(bc_dict))

    if not bcs:
        return {
            "ok": False,
            "error": {
                "code": "NO_BCS",
                "message": "At least one boundary condition is required (e.g. fixed support + applied load).",
            },
        }

    # Reject a bad solver name before the (expensive) STEP export, so the caller
    # gets SOLVER_NOT_FOUND rather than a downstream export/mesh error.
    try:
        _resolve_requested_solver(solver)
    except StructuralSolveError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}

    # Export STEP
    try:
        export_result = cad_export_body(body=body, format="step", doc=doc)
        if not export_result.get("ok"):
            return export_result
        step_path = export_result.get("path", "")
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "EXPORT_FAILED",
                "message": f"Failed to export body as STEP: {exc}",
            },
        }

    # Mesh → solve → parse via the shared engine.
    try:
        result = solve_structural_from_step(
            step_path=step_path,
            material=mat,
            boundary_conditions=bcs,
            mesh_size=mesh_size,
            solver=solver,
            body_label=body,
        )
    except StructuralSolveError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}

    return {"ok": True, **result.to_dict()}


def analysis_screen_stress(
    *,
    section: dict[str, Any],
    load: dict[str, Any],
    material: str | dict[str, Any],
    stress_concentration: dict[str, Any] | None = None,
    buckling: dict[str, Any] | None = None,
    target_fos: float = 2.0,
    name: str = "analytical stress screen",
    expectations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tier-1 analytical stress screen — gate FEA without a solver.

    Beam-theory bending stress + handbook stress-concentration factor + Euler
    buckling bound. Returns an AnalysisCheck (pass/warn/fail). A WARN means the
    screen is not definitive — run ``analysis.stress_check`` (FEA). No gmsh /
    CalculiX is invoked.
    """
    try:
        mat = _resolve_screen_material(material)
    except (KeyError, ValueError, TypeError) as exc:
        return {
            "ok": False,
            "error": {"code": "INVALID_INPUT", "message": f"bad material: {exc}"},
        }
    if mat is None:
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_MATERIAL",
                "message": f"Material not found: {material!r}. Use analysis.list_materials to see available materials.",
            },
        }
    try:
        exp = ReflectExpectations.from_dict(expectations) if expectations else None
        check = screen_stress(
            section=section,
            load=load,
            yield_strength_mpa=mat.yield_strength_mpa,
            youngs_modulus_mpa=mat.youngs_modulus_mpa,
            stress_concentration=stress_concentration,
            buckling=buckling,
            target_fos=target_fos,
            name=name,
            expectations=exp,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return {
            "ok": False,
            "error": {"code": "INVALID_INPUT", "message": str(exc)},
        }
    return {"ok": True, **check.to_dict()}


def _backfill_thermal_props(
    block: dict[str, Any] | None, mat: Material | None, keys: tuple[str, ...]
) -> dict[str, Any] | None:
    """Fill missing material-derived scalars in a physics block from a Material.

    Lets the caller pass ``material="aluminum_6061_t6"`` and omit
    ``conductivity_w_mk`` / ``density_kg_m3`` / ``specific_heat_j_kgk`` from the
    conduction/biot/transient blocks. Caller-supplied values always win.
    """
    if block is None:
        return None
    if mat is None:
        return block
    src = {
        "conductivity_w_mk": mat.thermal_conductivity_w_mk,
        "density_kg_m3": mat.density_kg_m3,
        "specific_heat_j_kgk": mat.specific_heat_j_kgk,
    }
    filled = dict(block)
    for key in keys:
        if key not in filled:
            # Backfill unconditionally (even a 0.0). A material missing the field
            # then surfaces a clear "must be positive" ValueError from
            # screen_thermal rather than a cryptic KeyError on the absent key.
            filled[key] = src[key]
    return filled


def analysis_screen_thermal(
    *,
    power_w: float,
    convection: dict[str, Any],
    conduction: dict[str, Any] | None = None,
    biot: dict[str, Any] | None = None,
    transient: dict[str, Any] | None = None,
    material: str | dict[str, Any] | None = None,
    max_temperature_k: float = 0.0,
    target_fos: float = 2.0,
    name: str = "analytical thermal screen",
) -> dict[str, Any]:
    """Tier-1 analytical thermal screen — gate thermal FEA without a solver.

    Lumped-parameter heat transfer: a series conduction/convection resistance
    network for the steady hot-spot temperature, plus the Biot number as the
    validity gate for the single-temperature assumption. Returns an AnalysisCheck
    (pass/warn/fail). A WARN means the screen is not definitive — the part is
    marginal, or internal gradients are significant (Bi>0.1) — so run
    ``analysis.thermal_check`` (FEA). No gmsh / Elmer is invoked.

    ``material`` (key or inline dict) is optional: when supplied, its thermal
    conductivity / density / specific heat backfill any of those left out of the
    conduction / biot / transient blocks.
    """
    try:
        mat: Material | None = None
        if material is not None:
            mat = _resolve_thermal_screen_material(material)
            if mat is None:
                return {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_MATERIAL",
                        "message": (
                            f"Material not found: {material!r}. "
                            "Use analysis.list_materials to see available materials."
                        ),
                    },
                }
        check = screen_thermal(
            power_w=power_w,
            convection=convection,
            conduction=_backfill_thermal_props(conduction, mat, ("conductivity_w_mk",)),
            biot=_backfill_thermal_props(biot, mat, ("conductivity_w_mk",)),
            transient=_backfill_thermal_props(
                transient, mat, ("density_kg_m3", "specific_heat_j_kgk")
            ),
            max_temperature_k=max_temperature_k,
            target_fos=target_fos,
            name=name,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return {
            "ok": False,
            "error": {"code": "INVALID_INPUT", "message": str(exc)},
        }
    return {"ok": True, **check.to_dict()}


def analysis_stress_from_simulation(
    *,
    body: str,
    material: str | dict[str, Any],
    fixed_faces: list[str],
    load_faces: list[str],
    simulation_result: dict[str, Any] | None = None,
    propagation_result: dict[str, Any] | None = None,
    load_direction: list[float] | None = None,
    joint_index: int | None = None,
    safety_factor: float = 1.5,
    mesh_size: float = 0.0,
    solver: str = "",
    doc: str | None = None,
) -> dict[str, Any]:
    """Run stress analysis using forces extracted from simulation results.

    Bridges dynamics simulation (motion.*) to structural analysis (analysis.*).
    Accepts results from either ``motion.simulate`` (Tier 3 dynamic) or
    ``motion.propagate_motion`` (Tier 1 analytical).

    The tool automatically extracts peak forces/torques from the simulation
    data, converts them to FEA boundary conditions, and runs stress analysis.
    """
    from server.analysis_sim_coupling import (
        bcs_from_propagation,
        bcs_from_simulation,
        summarize_sim_forces,
    )

    if simulation_result is None and propagation_result is None:
        return {
            "ok": False,
            "error": {
                "code": "NO_SIM_DATA",
                "message": (
                    "Provide either simulation_result (from motion.simulate) "
                    "or propagation_result (from motion.propagate_motion)."
                ),
            },
        }

    direction = tuple(load_direction) if load_direction else (0.0, 0.0, -1.0)

    # Build BCs from simulation data
    if simulation_result is not None:
        bc_dicts = bcs_from_simulation(
            simulation_result=simulation_result,
            body=body,
            fixed_faces=fixed_faces,
            load_faces=load_faces,
            load_direction=direction,
            joint_index=joint_index,
            safety_factor=safety_factor,
        )
        force_summary = summarize_sim_forces(simulation_result)
    else:
        bc_dicts = bcs_from_propagation(
            propagation_result=propagation_result,
            body=body,
            fixed_faces=fixed_faces,
            load_faces=load_faces,
            load_direction=direction,
        )
        force_summary = {
            "backend": "analytical",
            "has_analytical_torques": True,
            "part_torques_nm": {
                k: v.get("torque_nm", 0) for k, v in propagation_result.get("states", {}).items()
            },
        }

    if not bc_dicts:
        return {
            "ok": False,
            "error": {
                "code": "NO_FORCES",
                "message": (
                    "Could not extract forces from simulation results. "
                    "Ensure the simulation ran successfully and produced force data."
                ),
            },
        }

    # Check that we have at least one load BC (not just fixed)
    has_load = any(bc["bc_type"] != "fixed" for bc in bc_dicts)
    if not has_load:
        return {
            "ok": False,
            "error": {
                "code": "NO_LOAD",
                "message": (
                    "No load forces could be extracted from simulation data. "
                    "The simulation may not have produced force/torque data for "
                    f"body {body!r}. Try using motion.propagate_motion for "
                    "analytical torque estimation."
                ),
            },
        }

    # Run stress check with derived BCs
    result = analysis_stress_check(
        body=body,
        material=material,
        boundary_conditions=bc_dicts,
        mesh_size=mesh_size,
        solver=solver,
        doc=doc,
    )

    # Attach force provenance to result
    if result.get("ok"):
        result["force_source"] = force_summary
        result["derived_boundary_conditions"] = bc_dicts

    return result


def analysis_aero_check(
    *,
    body: str,
    flow_conditions: dict[str, Any],
    fluid: str | None = None,
    reference: dict[str, Any] | None = None,
    rotors: list[dict[str, Any]] | None = None,
    mesh_size: float = 0.0,
    solver: str = "",
    doc: str | None = None,
) -> dict[str, Any]:
    """Run aerodynamic or hydrodynamic CFD analysis on a body.

    Works for any fluid: air (drone aero), water (marine/underwater), etc.
    For single-body external flow, uses SU2 (RANS CFD).
    For multi-rotor analysis, uses DUST (vortex particle method).
    Auto-selects solver based on whether rotors are defined.

    Exports STEP → surface mesh → solver → CL/CD/L÷D + rotor thrust/torque.
    """
    from server.analysis_fluids import get_fluid
    from server.analysis_models import (
        AeroReference,
        FlowConditions,
        RotorSpec,
    )

    # Parse flow conditions — support fluid shorthand
    if fluid and isinstance(flow_conditions, dict):
        fluid_props = get_fluid(fluid)
        if fluid_props:
            # Merge fluid properties as defaults
            merged = dict(fluid_props)
            merged.update(flow_conditions)
            flow_conditions = merged
        else:
            return {
                "ok": False,
                "error": {
                    "code": "UNKNOWN_FLUID",
                    "message": (
                        f"Unknown fluid {fluid!r}. Available: air, freshwater, "
                        "seawater, oil_sae30, glycerin."
                    ),
                },
            }

    try:
        flow = FlowConditions.from_dict(flow_conditions)
    except (KeyError, TypeError) as exc:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_FLOW",
                "message": f"Invalid flow_conditions: {exc}. Required: velocity_m_s.",
            },
        }

    # Parse reference values
    ref = (
        AeroReference.from_dict(reference) if reference else AeroReference(area_m2=1.0, chord_m=0.1)
    )

    # Parse rotors
    rotor_specs: list[RotorSpec] = []
    if rotors:
        for r in rotors:
            rotor_specs.append(RotorSpec.from_dict(r))

    # Auto-select solver
    if not solver:
        if rotor_specs:
            solver = "mock_dust"  # prefer DUST for multi-rotor
            # Try real DUST first
            dust_solver = get_solver("dust", AnalysisType.AERODYNAMIC)
            if dust_solver:
                ok, _ = dust_solver.available()
                if ok:
                    solver = "dust"
        else:
            solver = "su2"  # prefer SU2 for single-body
            su2_solver = get_solver("su2", AnalysisType.AERODYNAMIC)
            if su2_solver:
                ok, _ = su2_solver.available()
                if not ok:
                    solver = "mock_dust"  # fallback to mock

    field_solver = get_solver(solver, AnalysisType.AERODYNAMIC)
    if field_solver is None:
        return {
            "ok": False,
            "error": {
                "code": "NO_AERO_SOLVER",
                "message": (
                    "No aerodynamic solver available. Install SU2 (apt install su2) "
                    "or DUST (https://github.com/Music-and-Culture-Technology-Lab/dust)."
                ),
            },
        }

    ok_avail, diag = field_solver.available()
    if not ok_avail:
        return {
            "ok": False,
            "error": {"code": "SOLVER_UNAVAILABLE", "message": diag},
        }

    # Build boundary conditions for the solver
    bcs_list: list[dict[str, Any]] = [
        {"bc_type": "freestream", "faces": [], "value": flow.to_dict()},
        {"bc_type": "reference", "faces": [], "value": ref.to_dict()},
        {"bc_type": "wall", "faces": ["wall"]},
        {"bc_type": "farfield", "faces": ["farfield"]},
    ]
    for rotor in rotor_specs:
        bcs_list.append({"bc_type": "rotor", "faces": [], "value": rotor.to_dict()})

    bcs = tuple(BoundaryCondition.from_dict(bc) for bc in bcs_list)

    # Use a dummy material (not relevant for aero)
    mat = Material(
        name="air",
        youngs_modulus_mpa=0,
        poissons_ratio=0,
        density_kg_m3=flow.density_kg_m3,
        yield_strength_mpa=0,
    )

    spec = AnalysisSpec(
        analysis_type=AnalysisType.AERODYNAMIC,
        body=body,
        material=mat,
        boundary_conditions=bcs,
        mesh_size=mesh_size,
        solver=field_solver.name(),
    )

    analysis_id = f"aero_{uuid.uuid4().hex[:8]}"

    # Export STEP
    try:
        export_result = cad_export_body(body=body, format="step", doc=doc)
        if not export_result.get("ok"):
            return export_result
        step_path = export_result.get("path", "")
    except Exception as exc:
        return {
            "ok": False,
            "error": {"code": "EXPORT_FAILED", "message": f"Failed to export STEP: {exc}"},
        }

    # Mesh
    try:
        mesh_info = mesh_step_to_msh(
            step_path=step_path,
            mesh_size=mesh_size,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": {"code": "MESH_FAILED", "message": f"Meshing failed: {exc}"},
        }

    # Solve
    import tempfile

    work_dir = Path(tempfile.mkdtemp(prefix=f"solidmind_{analysis_id}_"))
    try:
        input_path = field_solver.write_input(spec, mesh_info, work_dir)
        solve_time = field_solver.run(input_path, work_dir)
        result = field_solver.parse_results(work_dir, spec)
    except Exception as exc:
        return {
            "ok": False,
            "error": {"code": "SOLVE_FAILED", "message": f"Solver failed: {exc}"},
        }

    response: dict[str, Any] = {
        "ok": True,
        "analysis_id": analysis_id,
        "solver_name": field_solver.name(),
        "solve_time_s": solve_time,
    }
    response.update(result.to_dict())
    response["analysis_id"] = analysis_id

    # For aero solvers, try to get richer AeroResult with rotor_forces
    # The solver may store it via parse_aero_results() if available
    if hasattr(field_solver, "parse_aero_results"):
        try:
            aero = field_solver.parse_aero_results(work_dir, spec)
            response.update(aero.to_dict())
            response["analysis_id"] = analysis_id
        except Exception:
            pass

    return response


def analysis_torque_sweep(
    *,
    body: str,
    materials: list[str | dict[str, Any]],
    fixed_faces: list[str],
    load_face: str,
    load_direction: list[float] | None = None,
    pitch_radius_mm: float,
    torques_nmm: list[float],
    mesh_size: float = 0.0,
    solver: str = "",
    doc: str | None = None,
) -> dict[str, Any]:
    """Sweep torque values across materials to find the failure envelope.

    Meshes the body once, then runs CalculiX for each material × torque
    combination.  Returns per-material safety factors at each torque level
    and the interpolated breaking torque (SF = 1.0 crossover).

    Parameters
    ----------
    body : str
        Body label in the active document.
    materials : list
        Material keys (e.g. ``["pla", "aluminum_6061_t6", "steel_4140"]``)
        or inline dicts.
    fixed_faces : list[str]
        Face references for fixed supports.
    load_face : str
        Single face where tangential tooth force is applied.
    load_direction : list[float] | None
        Unit vector for tangential force (default ``[0, 1, 0]``).
    pitch_radius_mm : float
        Pitch radius in mm — force = torque / pitch_radius.
    torques_nmm : list[float]
        Torque values to sweep (N·mm).
    mesh_size : float
        Target mesh element size in mm (0 = auto).
    solver : str
        Solver name (default: auto-select).
    doc : str | None
        Document name.
    """
    import math

    if not materials:
        return {
            "ok": False,
            "error": {"code": "NO_MATERIALS", "message": "Provide at least one material."},
        }
    if not torques_nmm:
        return {
            "ok": False,
            "error": {"code": "NO_TORQUES", "message": "Provide at least one torque value."},
        }
    if pitch_radius_mm <= 0:
        return {
            "ok": False,
            "error": {"code": "BAD_RADIUS", "message": "pitch_radius_mm must be > 0."},
        }

    direction = load_direction or [0.0, 1.0, 0.0]
    # Normalise direction
    mag = math.sqrt(sum(d * d for d in direction))
    if mag < 1e-12:
        return {
            "ok": False,
            "error": {"code": "BAD_DIRECTION", "message": "load_direction must be non-zero."},
        }
    direction = [d / mag for d in direction]

    # Resolve all materials up front
    resolved_mats: list[Material] = []
    for m in materials:
        mat = _resolve_material(m)
        if mat is None:
            return {
                "ok": False,
                "error": {
                    "code": "UNKNOWN_MATERIAL",
                    "message": f"Material not found: {m!r}. Use analysis.list_materials to see available materials.",
                },
            }
        resolved_mats.append(mat)

    requested_solver_name = solver.strip()
    explicit_solver: FieldSolver | None = None
    if requested_solver_name:
        explicit_solver = get_solver(requested_solver_name, AnalysisType.STRUCTURAL)
        if explicit_solver is None:
            return {
                "ok": False,
                "error": {
                    "code": "SOLVER_NOT_FOUND",
                    "message": f"Solver {solver!r} not found.",
                },
            }
        ok_avail, diag = explicit_solver.available()
        if not ok_avail:
            return {"ok": False, "error": {"code": "SOLVER_UNAVAILABLE", "message": diag}}

    # Export STEP once
    try:
        export_result = cad_export_body(body=body, format="step", doc=doc)
        if not export_result.get("ok"):
            return export_result
        step_path = export_result.get("path", "")
    except Exception as exc:
        return {"ok": False, "error": {"code": "EXPORT_FAILED", "message": str(exc)}}

    # We need to mesh once per torque level since BCs embed force values.
    # However, the geometry mesh is the same — only the .inp BCs change.
    # For simplicity (and correctness with the current pipeline), we mesh
    # once with a reference force, then scale results linearly.
    #
    # Run a single FEA at reference force = 1 N, then scale:
    #   stress(T) = stress_ref * (T / pitch_radius_mm) / F_ref
    # Since F_ref = 1 N: stress(T) = stress_ref * T / pitch_radius_mm

    ref_force = 1.0  # 1 N reference
    ref_bcs = [
        BoundaryCondition.from_dict({"bc_type": "fixed", "faces": fixed_faces}),
        BoundaryCondition.from_dict(
            {
                "bc_type": "force",
                "faces": [load_face],
                "value": {
                    "fx": direction[0] * ref_force,
                    "fy": direction[1] * ref_force,
                    "fz": direction[2] * ref_force,
                },
            }
        ),
    ]

    face_groups: dict[str, list[str]] = {}
    for i, bc in enumerate(ref_bcs):
        face_groups[f"bc_{i}_{bc.bc_type}"] = list(bc.faces)

    try:
        mesh_info = mesh_step_to_msh(
            step_path=step_path,
            face_groups=face_groups,
            mesh_size=mesh_size,
        )
    except Exception as exc:
        return {"ok": False, "error": {"code": "MESH_FAILED", "message": str(exc)}}

    dof_count = int(mesh_info.num_nodes) * 3
    primary_solver = explicit_solver
    if primary_solver is None:
        primary_solver = get_solver("", AnalysisType.STRUCTURAL, dof_count=dof_count)
        if primary_solver is None:
            return {
                "ok": False,
                "error": {"code": "NO_SOLVER", "message": "No structural solver available."},
            }
    assert primary_solver is not None

    # ── Poisson-ratio grouping optimization ──
    # For linear elasticity: K = E × K_hat(ν), u = (1/E) × K_hat⁻¹ × f,
    # σ = D × B × u.  When ν is the same, stress is INDEPENDENT of E —
    # only displacement scales with 1/E.  So we solve once per unique ν
    # and reuse the stress result for all materials in that group.
    #
    # Displacement scales as: disp(mat) = disp(ref) × E_ref / E_mat

    # Group materials by Poisson's ratio (rounded to 2 decimals)
    nu_groups: dict[float, list[Material]] = {}
    for mat in resolved_mats:
        nu_key = round(mat.poissons_ratio, 2)
        nu_groups.setdefault(nu_key, []).append(mat)

    # Pick one representative per group (highest E for best numerical conditioning)
    nu_representatives: dict[float, Material] = {}
    for nu_key, group in nu_groups.items():
        nu_representatives[nu_key] = max(group, key=lambda m: m.youngs_modulus_mpa)

    representatives = list(nu_representatives.values())
    log.info(
        "Torque sweep: %d materials → %d Poisson groups (%s)",
        len(resolved_mats),
        len(representatives),
        ", ".join(f"ν={k}:{len(v)} mats" for k, v in nu_groups.items()),
    )

    # Run FEA once per representative material (not per material)
    rep_ref_stress: dict[str, float] = {}
    rep_ref_disp: dict[str, float] = {}
    used_solver_names: set[str] = set()

    def _solve_one_material(mat: Material) -> tuple[str, float, float, str]:
        solve_id = f"sweep_{uuid.uuid4().hex[:8]}"
        chosen_solver, result, _ = _solve_structural_with_fallback(
            analysis_id=solve_id,
            body=body,
            material=mat,
            boundary_conditions=tuple(ref_bcs),
            mesh_size=mesh_size,
            mesh_info=mesh_info,
            primary_solver=primary_solver,
            allow_fallback=not bool(requested_solver_name),
        )
        return (
            mat.name,
            result.max_von_mises_mpa,
            result.max_displacement_mm,
            chosen_solver.name(),
        )

    max_workers = max(1, min(len(representatives), 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_solve_one_material, mat): mat.name for mat in representatives}
        for fut in concurrent.futures.as_completed(futures):
            mat_name = futures[fut]
            try:
                solved_name, stress, disp, used_name = fut.result()
                rep_ref_stress[solved_name] = stress
                rep_ref_disp[solved_name] = disp
                used_solver_names.add(used_name)
            except Exception as exc:
                log.warning("Sweep FEA failed for %s: %s", mat_name, exc)
                rep_ref_stress[mat_name] = 0.0
                rep_ref_disp[mat_name] = 0.0

    # Map each material to its representative's results, scaling displacement by E ratio
    mat_ref_stress: dict[str, float] = {}
    mat_ref_disp: dict[str, float] = {}
    for nu_key, group in nu_groups.items():
        rep = nu_representatives[nu_key]
        rep_stress = rep_ref_stress.get(rep.name, 0.0)
        rep_disp = rep_ref_disp.get(rep.name, 0.0)
        for mat in group:
            # Stress is identical for same ν (independent of E)
            mat_ref_stress[mat.name] = rep_stress
            # Displacement scales inversely with E
            if mat.youngs_modulus_mpa > 0 and rep.youngs_modulus_mpa > 0:
                mat_ref_disp[mat.name] = rep_disp * rep.youngs_modulus_mpa / mat.youngs_modulus_mpa
            else:
                mat_ref_disp[mat.name] = rep_disp

    # Build results table by scaling
    sorted_torques = sorted(torques_nmm)
    sweep_results: list[dict[str, Any]] = []

    for torque in sorted_torques:
        force = torque / pitch_radius_mm
        row: dict[str, Any] = {"torque_nmm": torque, "force_n": round(force, 3), "materials": {}}
        for mat in resolved_mats:
            ref_vm = mat_ref_stress[mat.name]
            ref_disp = mat_ref_disp[mat.name]
            scaled_vm = ref_vm * force / ref_force
            scaled_disp = ref_disp * force / ref_force
            sf = mat.yield_strength_mpa / scaled_vm if scaled_vm > 0 else 99.0
            status = "fail" if sf < 1.0 else ("warn" if sf < 1.5 else "pass")
            row["materials"][mat.name] = {
                "peak_stress_mpa": round(scaled_vm, 2),
                "displacement_mm": round(scaled_disp, 4),
                "safety_factor": round(sf, 2),
                "yield_mpa": mat.yield_strength_mpa,
                "status": status,
            }
        sweep_results.append(row)

    # Compute breaking torques via linear interpolation (SF=1.0 crossover)
    breaking_torques: dict[str, dict[str, Any]] = {}
    for mat in resolved_mats:
        ref_vm = mat_ref_stress[mat.name]
        if ref_vm > 0:
            # At force F, stress = ref_vm * F.  Yield when ref_vm * F = yield.
            # F_break = yield / ref_vm.  T_break = F_break * pitch_radius.
            f_break = mat.yield_strength_mpa / ref_vm
            t_break = f_break * pitch_radius_mm
            breaking_torques[mat.name] = {
                "breaking_torque_nmm": round(t_break, 1),
                "breaking_force_n": round(f_break, 3),
                "yield_mpa": mat.yield_strength_mpa,
            }
        else:
            breaking_torques[mat.name] = {
                "breaking_torque_nmm": None,
                "breaking_force_n": None,
                "yield_mpa": mat.yield_strength_mpa,
            }

    return {
        "ok": True,
        "body": body,
        "pitch_radius_mm": pitch_radius_mm,
        "solver": primary_solver.name() if len(used_solver_names) <= 1 else "mixed",
        "sweep": sweep_results,
        "breaking_torques": breaking_torques,
        "note": f"Poisson-grouped: {len(resolved_mats)} materials → {len(representatives)} FEA solves. Stress identical for same ν, displacement scaled by E ratio.",
    }


def analysis_thermal_check(
    *,
    body: str,
    material: str | dict[str, Any],
    boundary_conditions: list[dict[str, Any]],
    mesh_size: float = 0.0,
    solver: str = "",
    max_temperature_k: float = 0.0,
    doc: str | None = None,
) -> dict[str, Any]:
    """Run steady-state thermal analysis on a body.

    Exports STEP → meshes with Gmsh → solves with Elmer (or selected solver)
    → returns temperature field, heat flux, and thermal safety checks.

    Thermal BC types:
    - ``temperature``: fixed temperature (Dirichlet). value: {temperature_k}
    - ``heat_flux``: applied heat flux (Neumann). value: {flux_w_m2}
    - ``convection``: convective heat transfer. value: {htc_w_m2k, t_ambient_k}
    """
    # Resolve material
    mat = _resolve_material(material)
    if mat is None:
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_MATERIAL",
                "message": (
                    f"Material not found: {material!r}. "
                    "Use analysis.list_materials to see available materials."
                ),
            },
        }

    # Validate thermal conductivity
    if mat.thermal_conductivity_w_mk <= 0:
        return {
            "ok": False,
            "error": {
                "code": "ZERO_CONDUCTIVITY",
                "message": (
                    f"Material {mat.name!r} has zero thermal conductivity. "
                    "Thermal analysis requires a material with thermal_conductivity_w_mk > 0."
                ),
            },
        }

    # Parse boundary conditions
    bcs: list[BoundaryCondition] = []
    thermal_bc_types = {"temperature", "heat_flux", "convection"}
    for bc_dict in boundary_conditions:
        bcs.append(BoundaryCondition.from_dict(bc_dict))

    if not bcs:
        return {
            "ok": False,
            "error": {
                "code": "NO_BCS",
                "message": "At least one thermal boundary condition is required.",
            },
        }

    # Check that at least one BC is a thermal type
    has_thermal = any(bc.bc_type in thermal_bc_types for bc in bcs)
    if not has_thermal:
        return {
            "ok": False,
            "error": {
                "code": "NO_THERMAL_BCS",
                "message": (
                    "No thermal boundary conditions found. "
                    f"Use one of: {', '.join(sorted(thermal_bc_types))}."
                ),
            },
        }

    # Inject max_temperature_k into BC values if provided at top level
    if max_temperature_k > 0:
        patched: list[BoundaryCondition] = []
        for bc in bcs:
            val = dict(bc.value)
            val["max_temperature_k"] = max_temperature_k
            patched.append(
                BoundaryCondition(
                    bc_type=bc.bc_type,
                    faces=bc.faces,
                    value=val,
                )
            )
        bcs = patched

    # Resolve solver
    requested_solver_name = solver.strip()
    field_solver = None
    if requested_solver_name:
        field_solver = get_solver(requested_solver_name, AnalysisType.THERMAL)
        if field_solver is None:
            available = list_solvers()
            return {
                "ok": False,
                "error": {
                    "code": "SOLVER_NOT_FOUND",
                    "message": (
                        f"Solver {solver!r} not found. Available: {[s['name'] for s in available]}"
                    ),
                },
            }
        ok, diag = field_solver.available()
        if not ok:
            return {
                "ok": False,
                "error": {"code": "SOLVER_UNAVAILABLE", "message": diag},
            }
    else:
        field_solver = get_solver("", AnalysisType.THERMAL)
        if field_solver is None:
            return {
                "ok": False,
                "error": {
                    "code": "NO_SOLVER",
                    "message": ("No thermal solver available. Install Elmer: apt install elmer"),
                },
            }

    import uuid

    analysis_id = f"thermal_{uuid.uuid4().hex[:8]}"

    # Export STEP
    try:
        export_result = cad_export_body(body=body, format="step", doc=doc)
        if not export_result.get("ok"):
            return export_result
        step_path = export_result.get("path", "")
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "EXPORT_FAILED",
                "message": f"Failed to export body as STEP: {exc}",
            },
        }

    # Mesh
    try:
        face_groups: dict[str, list[str]] = {}
        for i, bc in enumerate(bcs):
            group_name = f"bc_{i}_{bc.bc_type}"
            face_groups[group_name] = list(bc.faces)

        mesh_info = mesh_step_to_msh(
            step_path=step_path,
            face_groups=face_groups,
            mesh_size=mesh_size,
            msh_version=2.2,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "MESH_FAILED",
                "message": f"Meshing failed: {exc}",
            },
        }

    # Build spec
    spec = AnalysisSpec(
        analysis_type=AnalysisType.THERMAL,
        body=body,
        material=mat,
        boundary_conditions=tuple(bcs),
        mesh_size=mesh_size,
        solver=field_solver.name(),
    )

    # Solve
    import tempfile

    work_dir = Path(tempfile.mkdtemp(prefix=f"solidmind_{analysis_id}_"))
    try:
        input_path = field_solver.write_input(spec, mesh_info, work_dir)
        solve_time = field_solver.run(input_path, work_dir)
        result = field_solver.parse_results(work_dir, spec)
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "SOLVE_FAILED",
                "message": f"Solver failed: {exc}",
            },
        }

    # Patch analysis_id and solve_time
    result = FieldResult(
        analysis_id=analysis_id,
        status=result.status,
        safety_factor=result.safety_factor,
        max_von_mises_mpa=result.max_von_mises_mpa,
        max_displacement_mm=result.max_displacement_mm,
        checks=result.checks,
        scalar_fields=result.scalar_fields,
        solver_name=field_solver.name(),
        solve_time_s=solve_time,
    )

    # Persist
    try:
        save_result(result)
    except Exception:
        log.warning("Failed to persist thermal analysis result", exc_info=True)

    return {"ok": True, **result.to_dict()}


def analysis_conjugate_thermal_check(
    *,
    body: str,
    material: str | dict[str, Any],
    flow_velocity: list[float],
    flow_faces_inlet: list[str],
    flow_faces_outlet: list[str],
    heat_source_faces: list[str],
    heat_flux_w_m2: float,
    flow_temperature_k: float = 300.0,
    fluid: str = "air",
    max_temperature_k: float = 0.0,
    mesh_size: float = 0.0,
    solver: str = "",
    doc: str | None = None,
) -> dict[str, Any]:
    """Run conjugate heat transfer: coupled Navier-Stokes + Heat in Elmer.

    Instead of guessing a convection coefficient (htc), this solves the
    actual airflow (or any fluid) around the part and computes wall heat
    transfer from first principles.  Elmer solves both the fluid momentum
    equations and the energy equation in a coupled loop.

    Use this when:
    - You need accurate htc from real flow geometry (ventilation slots,
      fins, complex shapes)
    - The flow is driven by vehicle motion or a fan
    - ``analysis.thermal_check`` with estimated htc is not accurate enough

    Fluid presets: ``air`` (default), ``water``.

    Boundary condition mapping:
    - ``flow_faces_inlet`` → velocity inlet (Dirichlet on V, T)
    - ``flow_faces_outlet`` → pressure outlet (P = 0)
    - ``heat_source_faces`` → heat flux on solid surface
    - All other solid faces → no-slip wall (V = 0) for the fluid domain
    """
    mat = _resolve_material(material)
    if mat is None:
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_MATERIAL",
                "message": (
                    f"Material not found: {material!r}. "
                    "Use analysis.list_materials to see available materials."
                ),
            },
        }

    if mat.thermal_conductivity_w_mk <= 0:
        return {
            "ok": False,
            "error": {
                "code": "ZERO_CONDUCTIVITY",
                "message": (f"Material {mat.name!r} has zero thermal conductivity."),
            },
        }

    if len(flow_velocity) != 3:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_INPUT",
                "message": "flow_velocity must be [vx, vy, vz] in m/s.",
            },
        }

    # Fluid properties by preset
    fluid_props: dict[str, dict[str, float]] = {
        "air": {
            "fluid_density_kg_m3": 1.225,
            "fluid_viscosity_pa_s": 1.789e-5,
            "fluid_conductivity_w_mk": 0.026,
            "fluid_capacity_j_kgk": 1006.0,
        },
        "water": {
            "fluid_density_kg_m3": 998.0,
            "fluid_viscosity_pa_s": 1.002e-3,
            "fluid_conductivity_w_mk": 0.598,
            "fluid_capacity_j_kgk": 4182.0,
        },
    }
    fp = fluid_props.get(fluid, fluid_props["air"])

    # Build BCs
    bcs: list[BoundaryCondition] = []

    # Velocity inlet
    bcs.append(
        BoundaryCondition(
            bc_type="velocity_inlet",
            faces=tuple(flow_faces_inlet),
            value={
                "vx_m_s": flow_velocity[0],
                "vy_m_s": flow_velocity[1],
                "vz_m_s": flow_velocity[2],
                "temperature_k": flow_temperature_k,
                **fp,
            },
        )
    )

    # Pressure outlet
    bcs.append(
        BoundaryCondition(
            bc_type="pressure_outlet",
            faces=tuple(flow_faces_outlet),
            value={"pressure_pa": 0.0},
        )
    )

    # Heat source on solid
    heat_value: dict[str, float] = {"flux_w_m2": heat_flux_w_m2}
    if max_temperature_k > 0:
        heat_value["max_temperature_k"] = max_temperature_k
    bcs.append(
        BoundaryCondition(
            bc_type="heat_flux",
            faces=tuple(heat_source_faces),
            value=heat_value,
        )
    )

    # Resolve solver — explicit override, or prefer Elmer, fall back to mock
    requested_solver = solver.strip()
    use_conjugate = False

    if requested_solver:
        field_solver = get_solver(requested_solver, AnalysisType.THERMAL)
        if field_solver is None:
            return {
                "ok": False,
                "error": {
                    "code": "SOLVER_NOT_FOUND",
                    "message": f"Solver {solver!r} not found.",
                },
            }
        # Only use conjugate mode with Elmer
        if requested_solver == "elmer":
            ok, diag = field_solver.available()
            if ok:
                use_conjugate = True
    else:
        field_solver = get_solver("elmer", AnalysisType.CONJUGATE_HEAT)
        if field_solver is not None:
            ok, diag = field_solver.available()
            if ok:
                use_conjugate = True
            else:
                field_solver = None

        if field_solver is None:
            field_solver = get_solver("mock", AnalysisType.THERMAL)
            if field_solver is None:
                return {
                    "ok": False,
                    "error": {
                        "code": "NO_SOLVER",
                        "message": (
                            "No conjugate heat solver available. Install Elmer: apt install elmer"
                        ),
                    },
                }

    if use_conjugate:
        spec = AnalysisSpec(
            analysis_type=AnalysisType.CONJUGATE_HEAT,
            body=body,
            material=mat,
            boundary_conditions=tuple(bcs),
            mesh_size=mesh_size,
            solver=field_solver.name(),
        )
    else:
        spec = AnalysisSpec(
            analysis_type=AnalysisType.THERMAL,
            body=body,
            material=mat,
            boundary_conditions=tuple(bcs),
            mesh_size=mesh_size,
            solver=field_solver.name(),
        )

    import uuid

    analysis_id = f"cht_{uuid.uuid4().hex[:8]}"

    # Export STEP
    try:
        export_result = cad_export_body(body=body, format="step", doc=doc)
        if not export_result.get("ok"):
            return export_result
        step_path = export_result.get("path", "")
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "EXPORT_FAILED",
                "message": f"Failed to export body as STEP: {exc}",
            },
        }

    # Mesh — use CHT mesher (solid + fluid domain) when Elmer is available
    try:
        face_groups: dict[str, list[str]] = {}
        for bc in bcs:
            if bc.bc_type == "heat_flux":
                face_groups["heat_source"] = list(bc.faces)

        if use_conjugate:
            mesh_info = mesh_step_to_cht_msh(
                step_path=step_path,
                face_groups=face_groups,
                mesh_size=mesh_size,
                msh_version=2.2,
            )
        else:
            # Fallback: solid-only mesh for mock solver
            all_face_groups: dict[str, list[str]] = {}
            for i, bc in enumerate(bcs):
                group_name = f"bc_{i}_{bc.bc_type}"
                all_face_groups[group_name] = list(bc.faces)
            mesh_info = mesh_step_to_msh(
                step_path=step_path,
                face_groups=all_face_groups,
                mesh_size=mesh_size,
                msh_version=2.2,
            )
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "MESH_FAILED",
                "message": f"Meshing failed: {exc}",
            },
        }

    # Solve
    import tempfile

    work_dir = Path(tempfile.mkdtemp(prefix=f"solidmind_{analysis_id}_"))
    try:
        input_path = field_solver.write_input(spec, mesh_info, work_dir)
        solve_time = field_solver.run(input_path, work_dir)
        result = field_solver.parse_results(work_dir, spec)
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "SOLVE_FAILED",
                "message": f"Solver failed: {exc}",
            },
        }

    result = FieldResult(
        analysis_id=analysis_id,
        status=result.status,
        safety_factor=result.safety_factor,
        max_von_mises_mpa=result.max_von_mises_mpa,
        max_displacement_mm=result.max_displacement_mm,
        checks=result.checks,
        scalar_fields=result.scalar_fields,
        solver_name=field_solver.name(),
        solve_time_s=solve_time,
    )

    try:
        save_result(result)
    except Exception:
        log.warning("Failed to persist CHT analysis result", exc_info=True)

    return {"ok": True, **result.to_dict()}


def analysis_list_materials(
    *,
    category: str | None = None,
) -> dict[str, Any]:
    """List available materials for analysis."""
    materials = list_materials(category)
    return {"ok": True, "materials": materials}


def analysis_list_solvers() -> dict[str, Any]:
    """List installed field solvers and their availability."""
    solvers = list_solvers()
    return {"ok": True, "solvers": solvers}
