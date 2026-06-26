"""Data models for field-problem analysis (structural, thermal, EM).

All models are frozen dataclasses with __slots__ for consistency with the rest
of the codebase.  Every model has ``to_dict()`` / ``from_dict()`` for JSON
round-tripping.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any


class AnalysisType(str, Enum):
    STRUCTURAL = "structural"
    THERMAL = "thermal"
    CONJUGATE_HEAT = "conjugate_heat"
    ELECTROMAGNETIC = "electromagnetic"
    AERODYNAMIC = "aerodynamic"
    HYDRODYNAMIC = "hydrodynamic"


class AnalysisStatus(str, Enum):
    PENDING = "pending"
    MESHING = "meshing"
    SOLVING = "solving"
    DONE = "done"
    FAILED = "failed"


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class FailureMode(str, Enum):
    """Typed mechanism of failure for a structural check.

    Lets the Interpret step dispatch on a value instead of parsing a free-text
    ``name`` string.  Set drawn from the ROADMAP §Interpret taxonomy.
    """
    STRESS_CONCENTRATION = "stress_concentration"
    YIELD = "yield"
    FATIGUE = "fatigue"
    BUCKLING = "buckling"
    CONTACT = "contact"
    DEFLECTION = "deflection"
    RESONANCE = "resonance"
    THERMAL = "thermal"
    WEAR = "wear"
    CORROSION = "corrosion"


@dataclass(frozen=True, slots=True)
class ReflectExpectations:
    """Pre-simulation expectations filed before calling an ``analysis.*`` tool.

    The Reflect step of the inner loop fills this in *before* the solver runs:
    which failure modes are being checked, where the hotspot is expected, and
    the plausible peak-stress band.  Interpret later compares the real result
    against it.  Tuples are used so the model stays frozen + hashable.
    """
    part_class: str
    failure_modes_to_check: tuple[FailureMode, ...]
    expected_hotspot: str
    expected_peak_stress_mpa: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_class": self.part_class,
            "failure_modes_to_check": [m.value for m in self.failure_modes_to_check],
            "expected_hotspot": self.expected_hotspot,
            "expected_peak_stress_mpa": list(self.expected_peak_stress_mpa),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReflectExpectations:
        band = d["expected_peak_stress_mpa"]
        return cls(
            part_class=d["part_class"],
            failure_modes_to_check=tuple(
                FailureMode(m) for m in d["failure_modes_to_check"]
            ),
            expected_hotspot=d["expected_hotspot"],
            expected_peak_stress_mpa=(band[0], band[1]),
        )


@dataclass(frozen=True, slots=True)
class Material:
    name: str
    youngs_modulus_mpa: float
    poissons_ratio: float
    density_kg_m3: float
    yield_strength_mpa: float
    thermal_conductivity_w_mk: float = 0.0
    specific_heat_j_kgk: float = 0.0
    thermal_expansion_1_k: float = 0.0
    electrical_conductivity_s_m: float = 0.0
    relative_permeability: float = 1.0
    relative_permittivity: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "youngs_modulus_mpa": self.youngs_modulus_mpa,
            "poissons_ratio": self.poissons_ratio,
            "density_kg_m3": self.density_kg_m3,
            "yield_strength_mpa": self.yield_strength_mpa,
            "thermal_conductivity_w_mk": self.thermal_conductivity_w_mk,
            "specific_heat_j_kgk": self.specific_heat_j_kgk,
            "thermal_expansion_1_k": self.thermal_expansion_1_k,
            "electrical_conductivity_s_m": self.electrical_conductivity_s_m,
            "relative_permeability": self.relative_permeability,
            "relative_permittivity": self.relative_permittivity,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Material:
        return cls(
            name=d["name"],
            youngs_modulus_mpa=d["youngs_modulus_mpa"],
            poissons_ratio=d["poissons_ratio"],
            density_kg_m3=d["density_kg_m3"],
            yield_strength_mpa=d["yield_strength_mpa"],
            thermal_conductivity_w_mk=d.get("thermal_conductivity_w_mk", 0.0),
            specific_heat_j_kgk=d.get("specific_heat_j_kgk", 0.0),
            thermal_expansion_1_k=d.get("thermal_expansion_1_k", 0.0),
            electrical_conductivity_s_m=d.get("electrical_conductivity_s_m", 0.0),
            relative_permeability=d.get("relative_permeability", 1.0),
            relative_permittivity=d.get("relative_permittivity", 1.0),
        )


@dataclass(frozen=True, slots=True)
class FaceGroup:
    """A named group of faces for applying boundary conditions."""
    name: str
    face_refs: tuple[str, ...]  # e.g. ("Face1", "Face3")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "face_refs": list(self.face_refs)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FaceGroup:
        return cls(name=d["name"], face_refs=tuple(d["face_refs"]))


@dataclass(frozen=True, slots=True)
class BoundaryCondition:
    """A boundary condition applied to a set of faces."""
    bc_type: str  # "fixed", "force", "pressure", "displacement"
    faces: tuple[str, ...]  # face references ("Face1", ...)
    value: dict[str, float] = dc_field(default_factory=dict)
    # e.g. {"fx": 0, "fy": 0, "fz": -100} for force
    # e.g. {"pressure_mpa": 5.0} for pressure
    # e.g. {} for fixed (all DOFs constrained)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bc_type": self.bc_type,
            "faces": list(self.faces),
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BoundaryCondition:
        return cls(
            bc_type=d["bc_type"],
            faces=tuple(d["faces"]),
            value=d.get("value", {}),
        )


@dataclass(frozen=True, slots=True)
class AnalysisSpec:
    """Full specification for a field analysis."""
    analysis_type: AnalysisType
    body: str
    material: Material
    boundary_conditions: tuple[BoundaryCondition, ...]
    mesh_size: float = 0.0  # 0 = auto
    solver: str = ""  # empty = auto-select

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_type": self.analysis_type.value,
            "body": self.body,
            "material": self.material.to_dict(),
            "boundary_conditions": [bc.to_dict() for bc in self.boundary_conditions],
            "mesh_size": self.mesh_size,
            "solver": self.solver,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisSpec:
        return cls(
            analysis_type=AnalysisType(d["analysis_type"]),
            body=d["body"],
            material=Material.from_dict(d["material"]),
            boundary_conditions=tuple(
                BoundaryCondition.from_dict(bc) for bc in d["boundary_conditions"]
            ),
            mesh_size=d.get("mesh_size", 0.0),
            solver=d.get("solver", ""),
        )


@dataclass(frozen=True, slots=True)
class ScalarFieldSummary:
    """Summary statistics for a scalar field (stress, displacement, etc.)."""
    field_name: str
    min_val: float
    max_val: float
    mean_val: float
    unit: str
    max_location_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "min": self.min_val,
            "max": self.max_val,
            "mean": self.mean_val,
            "unit": self.unit,
            "max_location_xyz": list(self.max_location_xyz),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScalarFieldSummary:
        loc = d.get("max_location_xyz", [0.0, 0.0, 0.0])
        return cls(
            field_name=d["field_name"],
            min_val=d["min"],
            max_val=d["max"],
            mean_val=d["mean"],
            unit=d["unit"],
            max_location_xyz=(loc[0], loc[1], loc[2]),
        )


@dataclass(frozen=True, slots=True)
class AnalysisCheck:
    """A single pass/warn/fail check with remediation guidance."""
    name: str
    status: CheckStatus
    message: str
    measured: float = 0.0
    limit: float = 0.0
    face_group: str = ""
    suggestion: str = ""
    failure_mode: FailureMode | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "measured": self.measured,
            "limit": self.limit,
            "face_group": self.face_group,
            "suggestion": self.suggestion,
            "failure_mode": self.failure_mode.value if self.failure_mode else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisCheck:
        fm = d.get("failure_mode")
        return cls(
            name=d["name"],
            status=CheckStatus(d["status"]),
            message=d["message"],
            measured=d.get("measured", 0.0),
            limit=d.get("limit", 0.0),
            face_group=d.get("face_group", ""),
            suggestion=d.get("suggestion", ""),
            failure_mode=FailureMode(fm) if fm else None,
        )


@dataclass(frozen=True, slots=True)
class FieldResult:
    """Complete result from a field analysis."""
    analysis_id: str
    status: CheckStatus  # overall pass/warn/fail
    safety_factor: float
    max_von_mises_mpa: float
    max_displacement_mm: float
    checks: tuple[AnalysisCheck, ...]
    scalar_fields: tuple[ScalarFieldSummary, ...]
    solver_name: str = ""
    solve_time_s: float = 0.0
    failure_mode: FailureMode | None = None
    candidates: tuple[str, ...] = ()  # fix-option labels for the Decide step

    @property
    def factor_of_safety(self) -> float:
        """Alias for ``safety_factor`` (matches the iteration-loop contract)."""
        return self.safety_factor

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "status": self.status.value,
            "safety_factor": self.safety_factor,
            "max_von_mises_mpa": self.max_von_mises_mpa,
            "max_displacement_mm": self.max_displacement_mm,
            "checks": [c.to_dict() for c in self.checks],
            "scalar_fields": [sf.to_dict() for sf in self.scalar_fields],
            "solver_name": self.solver_name,
            "solve_time_s": self.solve_time_s,
            "failure_mode": self.failure_mode.value if self.failure_mode else None,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldResult:
        fm = d.get("failure_mode")
        return cls(
            analysis_id=d["analysis_id"],
            status=CheckStatus(d["status"]),
            safety_factor=d["safety_factor"],
            max_von_mises_mpa=d["max_von_mises_mpa"],
            max_displacement_mm=d["max_displacement_mm"],
            checks=tuple(AnalysisCheck.from_dict(c) for c in d["checks"]),
            scalar_fields=tuple(
                ScalarFieldSummary.from_dict(sf) for sf in d["scalar_fields"]
            ),
            solver_name=d.get("solver_name", ""),
            solve_time_s=d.get("solve_time_s", 0.0),
            failure_mode=FailureMode(fm) if fm else None,
            candidates=tuple(d.get("candidates", ())),
        )


@dataclass(frozen=True, slots=True)
class MeshInfo:
    """Metadata about a generated mesh."""
    path: str
    num_nodes: int
    num_elements: int
    element_type: str  # "tet4", "tet10"
    physical_groups: dict[str, int] = dc_field(default_factory=dict)
    # maps face ref (e.g. "Face1") → gmsh physical group tag
    body_tags: dict[str, int] = dc_field(default_factory=dict)
    # maps body name (e.g. "solid", "fluid") → gmsh physical group tag

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "num_nodes": self.num_nodes,
            "num_elements": self.num_elements,
            "element_type": self.element_type,
            "physical_groups": self.physical_groups,
            "body_tags": self.body_tags,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MeshInfo:
        return cls(
            path=d["path"],
            num_nodes=d["num_nodes"],
            num_elements=d["num_elements"],
            element_type=d["element_type"],
            physical_groups=d.get("physical_groups", {}),
            body_tags=d.get("body_tags", {}),
        )


# ---------------------------------------------------------------------------
# Aerodynamic analysis models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowConditions:
    """Freestream flow conditions for aerodynamic analysis."""
    velocity_m_s: float
    density_kg_m3: float = 1.225  # ISA sea level
    viscosity_pa_s: float = 1.789e-5  # ISA sea level
    angle_of_attack_deg: float = 0.0
    sideslip_deg: float = 0.0
    mach: float = 0.0  # 0 = auto-compute from velocity

    def to_dict(self) -> dict[str, Any]:
        return {
            "velocity_m_s": self.velocity_m_s,
            "density_kg_m3": self.density_kg_m3,
            "viscosity_pa_s": self.viscosity_pa_s,
            "angle_of_attack_deg": self.angle_of_attack_deg,
            "sideslip_deg": self.sideslip_deg,
            "mach": self.mach,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FlowConditions:
        return cls(
            velocity_m_s=d["velocity_m_s"],
            density_kg_m3=d.get("density_kg_m3", 1.225),
            viscosity_pa_s=d.get("viscosity_pa_s", 1.789e-5),
            angle_of_attack_deg=d.get("angle_of_attack_deg", 0.0),
            sideslip_deg=d.get("sideslip_deg", 0.0),
            mach=d.get("mach", 0.0),
        )


@dataclass(frozen=True, slots=True)
class AeroReference:
    """Reference values for non-dimensionalizing aerodynamic coefficients."""
    area_m2: float
    chord_m: float = 0.0
    span_m: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "area_m2": self.area_m2,
            "chord_m": self.chord_m,
            "span_m": self.span_m,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AeroReference:
        return cls(
            area_m2=d["area_m2"],
            chord_m=d.get("chord_m", 0.0),
            span_m=d.get("span_m", 0.0),
        )


@dataclass(frozen=True, slots=True)
class RotorSpec:
    """Rotor definition for multi-rotor aerodynamic analysis (DUST)."""
    rotor_id: str
    center_xyz: tuple[float, float, float]
    axis: tuple[float, float, float]  # rotation axis (unit vector)
    radius_m: float
    rpm: float
    num_blades: int = 2
    chord_m: float = 0.0  # average chord, 0 = auto from geometry
    collective_deg: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rotor_id": self.rotor_id,
            "center_xyz": list(self.center_xyz),
            "axis": list(self.axis),
            "radius_m": self.radius_m,
            "rpm": self.rpm,
            "num_blades": self.num_blades,
            "chord_m": self.chord_m,
            "collective_deg": self.collective_deg,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RotorSpec:
        center = d.get("center_xyz", [0, 0, 0])
        axis = d.get("axis", [0, 0, 1])
        return cls(
            rotor_id=d["rotor_id"],
            center_xyz=(center[0], center[1], center[2]),
            axis=(axis[0], axis[1], axis[2]),
            radius_m=d["radius_m"],
            rpm=d["rpm"],
            num_blades=d.get("num_blades", 2),
            chord_m=d.get("chord_m", 0.0),
            collective_deg=d.get("collective_deg", 10.0),
        )


@dataclass(frozen=True, slots=True)
class AeroResult:
    """Result from an aerodynamic analysis."""
    analysis_id: str
    status: CheckStatus
    cl: float  # lift coefficient
    cd: float  # drag coefficient
    cs: float  # side force coefficient
    cmx: float  # roll moment coefficient
    cmy: float  # pitch moment coefficient
    cmz: float  # yaw moment coefficient
    l_over_d: float  # lift-to-drag ratio
    lift_n: float  # dimensional lift force
    drag_n: float  # dimensional drag force
    checks: tuple[AnalysisCheck, ...]
    rotor_forces: dict[str, dict[str, float]] = dc_field(default_factory=dict)
    # rotor_id → {"thrust_n", "torque_nm", "power_w", "ct", "cq"}
    solver_name: str = ""
    solve_time_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "status": self.status.value,
            "cl": self.cl,
            "cd": self.cd,
            "cs": self.cs,
            "cmx": self.cmx,
            "cmy": self.cmy,
            "cmz": self.cmz,
            "l_over_d": self.l_over_d,
            "lift_n": self.lift_n,
            "drag_n": self.drag_n,
            "checks": [c.to_dict() for c in self.checks],
            "rotor_forces": self.rotor_forces,
            "solver_name": self.solver_name,
            "solve_time_s": self.solve_time_s,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AeroResult:
        return cls(
            analysis_id=d["analysis_id"],
            status=CheckStatus(d["status"]),
            cl=d["cl"],
            cd=d["cd"],
            cs=d.get("cs", 0.0),
            cmx=d.get("cmx", 0.0),
            cmy=d.get("cmy", 0.0),
            cmz=d.get("cmz", 0.0),
            l_over_d=d["l_over_d"],
            lift_n=d["lift_n"],
            drag_n=d["drag_n"],
            checks=tuple(AnalysisCheck.from_dict(c) for c in d["checks"]),
            rotor_forces=d.get("rotor_forces", {}),
            solver_name=d.get("solver_name", ""),
            solve_time_s=d.get("solve_time_s", 0.0),
        )
