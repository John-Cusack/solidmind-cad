"""Master spec dataclasses — the contract between orchestrator, council, and workers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class SpecStatus(str, Enum):
    DRAFT = "draft"
    NORMALIZING = "normalizing"
    COUNCIL_REVIEW = "council_review"
    LAYOUT_FROZEN = "layout_frozen"
    INTERFACES_FROZEN = "interfaces_frozen"
    BUILDING = "building"
    GEOMETRY_VALIDATING = "geometry_validating"
    SCORING = "scoring"
    RELEASE_PACKAGING = "release_packaging"
    AWAITING_HUMAN = "awaiting_human"
    DONE = "done"
    FAILED = "failed"


class ComplexityClass(str, Enum):
    S = "S"  # gears, pins, spacers — 5 min timeout
    M = "M"  # brackets, carriers, covers — 10 min timeout
    L = "L"  # housings, complex assemblies — 15 min timeout


class WorkerMode(str, Enum):
    SUBAGENT = "subagent"        # MVP — Claude Code Agent tool, free on Max
    CLAUDE_CODE = "claude_code"  # `claude --print` subprocess (headless/CI)
    DOCKER = "docker"            # future — container with Claude Code + FreeCAD
    API = "api"                  # future — direct API calls, any provider


class SubsystemKind(str, Enum):
    GENERATED = "generated"   # built by workers
    CATALOG = "catalog"       # purchased, specific supplier part
    STANDARD = "standard"     # off-the-shelf (bolts, bearings, seals)


class FailureCode(str, Enum):
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    WORKER_TOOL_ERROR = "WORKER_TOOL_ERROR"
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    MANIFEST_HASH_MISMATCH = "MANIFEST_HASH_MISMATCH"
    INTERFACE_DIM_MISMATCH = "INTERFACE_DIM_MISMATCH"
    # MEASUREMENT_DRIFT: the worker's claimed measurements disagree
    # with what the orchestrator measures by re-importing the STEP
    # file.  INTERFACE_DIM_MISMATCH is claimed-vs-spec; this one is
    # claimed-vs-measured.  Raised by orchestrator/measure.py.
    MEASUREMENT_DRIFT = "MEASUREMENT_DRIFT"
    CLEARANCE_COLLISION = "CLEARANCE_COLLISION"
    ENVELOPE_VIOLATION = "ENVELOPE_VIOLATION"
    ME_CHECK_FAIL = "ME_CHECK_FAIL"
    OBJECTIVE_THRESHOLD = "OBJECTIVE_THRESHOLD"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SKELETON_CONFLICT = "SKELETON_CONFLICT"
    ICD_INCOMPLETE = "ICD_INCOMPLETE"
    MASS_OVER_BUDGET = "MASS_OVER_BUDGET"
    ASSEMBLY_ACCESS_FAIL = "ASSEMBLY_ACCESS_FAIL"


# ---------------------------------------------------------------------------
# Complexity class defaults
# ---------------------------------------------------------------------------

_COMPLEXITY_DEFAULTS: dict[ComplexityClass, dict[str, Any]] = {
    ComplexityClass.S: {"timeout_sec": 300, "max_retries": 2, "max_cost_usd": 2.0},
    ComplexityClass.M: {"timeout_sec": 600, "max_retries": 2, "max_cost_usd": 5.0},
    ComplexityClass.L: {"timeout_sec": 900, "max_retries": 1, "max_cost_usd": 10.0},
}


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Objective:
    """A single optimization objective."""

    name: str  # e.g. "mass", "max_stress", "machine_time"
    direction: str  # "minimize" | "maximize"
    unit: str  # e.g. "kg", "MPa", "minutes"
    weight: float = 1.0
    threshold: float | None = None  # hard constraint


# ---------------------------------------------------------------------------
# Interface (enriched with frames, mating, tolerances, validation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CoordinateFrame:
    """Local coordinate frame at an interface boundary."""

    origin_mm: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    axis_x: list[float] = field(default_factory=lambda: [1.0, 0.0, 0.0])
    axis_y: list[float] = field(default_factory=lambda: [0.0, 1.0, 0.0])
    axis_z: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0])


@dataclass(frozen=True, slots=True)
class MatingSemantic:
    """How two parts physically connect at an interface."""

    type: str = ""  # cylindrical_fit | planar_contact | bolt_pattern | gear_mesh | spline
    engagement_length_mm: float | None = None
    orientation_rule: str = ""  # e.g. "axis_z aligned, axis_x clocked"


@dataclass(frozen=True, slots=True)
class ToleranceSchema:
    """Dimensional and geometric tolerances at an interface."""

    fit_class: str = ""  # e.g. "H7/h6"
    dimensional: dict[str, Any] = field(default_factory=dict)
    # e.g. {"diameter_mm": {"nominal": 8, "upper": 0.015, "lower": 0}}
    geometric: dict[str, Any] = field(default_factory=dict)
    # e.g. {"concentricity_mm": 0.01, "perpendicularity_mm": 0.02}


@dataclass(frozen=True, slots=True)
class LoadCase:
    """A named load condition at an interface."""

    name: str = "operating"
    torque_nm: float = 0.0
    axial_force_n: float = 0.0
    radial_force_n: float = 0.0
    bending_moment_nm: float = 0.0


@dataclass(frozen=True, slots=True)
class ValidationCheckPoint:
    """A single measurement the orchestrator performs to verify an interface."""

    feature: str = ""  # e.g. "bore_diameter", "bolt_circle_diameter"
    expected_mm: float = 0.0
    tolerance_mm: float = 0.01


@dataclass(frozen=True, slots=True)
class ValidationMethod:
    """How the orchestrator verifies an interface after build."""

    measurement_tool: str = "cad_measure_between"
    check_points: list[ValidationCheckPoint] = field(default_factory=list)
    pass_rule: str = "all checks within tolerance"


@dataclass(frozen=True, slots=True)
class Interface:
    """A dimensional contract between two subsystems — immutable after freeze."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    subsystem_a: str = ""
    port_a: str = ""
    subsystem_b: str = ""
    port_b: str = ""

    # Geometry
    geometry: dict[str, Any] = field(default_factory=dict)

    # Coordinate frames (required for freeze)
    frame_a: CoordinateFrame = field(default_factory=CoordinateFrame)
    frame_b: CoordinateFrame = field(default_factory=CoordinateFrame)

    # Mating semantics
    mating: MatingSemantic = field(default_factory=MatingSemantic)

    # Tolerances
    tolerances: ToleranceSchema = field(default_factory=ToleranceSchema)

    # Load cases
    loads: list[LoadCase] = field(default_factory=list)

    # Validation
    validation: ValidationMethod = field(default_factory=ValidationMethod)

    # ICD extensions (MVP)
    datum_scheme: str = ""
    ctqs: list[str] = field(default_factory=list)
    inspection: dict[str, Any] = field(default_factory=dict)

    # Extended interface fields
    runout_or_concentricity: float | None = None  # mm
    preload: dict[str, Any] = field(default_factory=dict)
    backlash: dict[str, Any] = field(default_factory=dict)
    surface_requirements: dict[str, Any] = field(default_factory=dict)
    retention: str = ""
    lubrication: str = ""
    service_requirements: dict[str, Any] = field(default_factory=dict)
    thermal_allowance: dict[str, Any] = field(default_factory=dict)

    def is_complete(self) -> bool:
        """Check if interface has all required fields for freeze."""
        has_frames = (
            self.frame_a.origin_mm != [0, 0, 0] or self.frame_b.origin_mm != [0, 0, 0]
        )
        has_mating = bool(self.mating.type)
        has_geometry = bool(self.geometry)
        has_validation = bool(self.validation.check_points)
        return has_frames and has_mating and has_geometry and has_validation


# ---------------------------------------------------------------------------
# Manufacturing and runtime policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReleaseRequirements:
    """Release documentation requirements for a subsystem."""

    drawing_required: bool = False
    inspection_required: bool = False
    bom_line_type: str = ""  # "manufactured" | "purchased" | "standard"
    revision_controlled: bool = False


@dataclass(frozen=True, slots=True)
class ManufacturingSpec:
    """Manufacturing constraints for a subsystem."""

    process: str = ""  # CNC_turning, CNC_milling, injection_molding, 3D_print, etc.
    min_feature_size_mm: float = 0.5
    min_wall_mm: float = 1.0
    notes: str = ""
    tolerance_general: str = ""  # e.g. "ISO 2768-m"
    tolerance_critical: str = ""  # e.g. "±0.01 mm"
    surface_finish_ra_um: float | None = None
    coating: str = ""


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Timeout and retry budget for a worker."""

    timeout_sec: int = 600
    max_retries: int = 2
    max_cost_usd: float = 5.0

    @classmethod
    def from_complexity(cls, complexity: ComplexityClass) -> RuntimePolicy:
        defaults = _COMPLEXITY_DEFAULTS[complexity]
        return cls(**defaults)


# ---------------------------------------------------------------------------
# Subsystem
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Subsystem:
    """A chunk of work assigned to one or more competing workers."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    description: str = ""
    envelope_mm: list[float] = field(default_factory=list)
    mass_budget_kg: float | None = None
    material: str = ""
    interfaces: list[str] = field(default_factory=list)  # interface IDs
    specs: dict[str, Any] = field(default_factory=dict)
    worker_count: int = 1
    complexity_class: ComplexityClass = ComplexityClass.M
    runtime_policy: RuntimePolicy | None = None  # derived from complexity if None
    manufacturing: ManufacturingSpec = field(default_factory=ManufacturingSpec)
    kind: SubsystemKind = SubsystemKind.GENERATED
    standard: str = ""            # e.g. "ISO 4762 M5x20" for standard parts
    supplier_part: str = ""       # e.g. "SKF 6201-2Z" for catalog parts
    assembly_constraints: dict[str, Any] = field(default_factory=dict)
    quantity: int = 1
    release: ReleaseRequirements = field(default_factory=ReleaseRequirements)

    def effective_runtime_policy(self) -> RuntimePolicy:
        return self.runtime_policy or RuntimePolicy.from_complexity(self.complexity_class)


# ---------------------------------------------------------------------------
# Knowledge config
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class KnowledgeConfig:
    """How knowledge is distributed to workers."""

    global_paths: list[str] = field(default_factory=lambda: ["me_knowledge/"])
    project_path: str = ""
    share_mode: str = "project_slice"  # full | project_slice | none


# ---------------------------------------------------------------------------
# Cost policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CostPolicy:
    """Budget caps for the entire run."""

    max_run_cost_usd: float = 50.0
    max_stage_cost_usd: float = 20.0
    warn_at_pct: int = 80


# ---------------------------------------------------------------------------
# Provenance and artifacts
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProvenanceManifest:
    """Tracks origin and reproducibility of a worker run."""

    run_id: str = ""
    worker_id: str = ""
    spec_hash: str = ""
    prompt_hash: str = ""
    image_digest: str = ""
    tool_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    """A single output artifact with integrity metadata."""

    path: str = ""
    sha256: str = ""
    size_bytes: int = 0
    created_at: str = ""


# ---------------------------------------------------------------------------
# Worker result
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WorkerResult:
    """What a worker container delivers."""

    subsystem_name: str = ""
    worker_id: str = ""
    status: str = ""  # "success" | "failed" | "timeout"
    step_file: Path | None = None
    stl_files: list[Path] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    error: str | None = None
    failure_code: FailureCode | None = None

    # Worker-claimed values (advisory, not authoritative)
    claimed: dict[str, Any] = field(default_factory=dict)

    # Orchestrator-measured values (authoritative, filled by validator)
    measured: dict[str, Any] = field(default_factory=dict)

    # Scores (filled by scorer)
    scores: dict[str, float] = field(default_factory=dict)

    release_artifacts: dict[str, Any] = field(default_factory=dict)

    provenance: ProvenanceManifest = field(default_factory=ProvenanceManifest)
    artifact_manifest: list[ArtifactEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Assembly skeleton
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AssemblySkeleton:
    """Assembly-level spatial truth — datums, axes, reserved volumes."""

    datums: dict[str, list[float]] = field(default_factory=dict)
    shaft_axes: dict[str, dict[str, Any]] = field(default_factory=dict)
    bearing_spans: dict[str, dict[str, Any]] = field(default_factory=dict)
    reserved_volumes: dict[str, dict[str, Any]] = field(default_factory=dict)
    keepout_zones: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Master spec
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MasterSpec:
    """The complete contract that the council produces and workers consume."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    description: str = ""
    status: SpecStatus = SpecStatus.DRAFT
    worker_mode: WorkerMode = WorkerMode.CLAUDE_CODE

    global_constraints: dict[str, Any] = field(default_factory=dict)
    objectives: list[Objective] = field(default_factory=list)
    subsystems: list[Subsystem] = field(default_factory=list)
    interfaces: list[Interface] = field(default_factory=list)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    cost_policy: CostPolicy = field(default_factory=CostPolicy)
    skeleton: AssemblySkeleton = field(default_factory=AssemblySkeleton)

    # --- Lookups ---

    def get_subsystem(self, name: str) -> Subsystem | None:
        return next((s for s in self.subsystems if s.name == name), None)

    def get_interface(self, id: str) -> Interface | None:
        return next((i for i in self.interfaces if i.id == id), None)

    def interfaces_for(self, subsystem_name: str) -> list[Interface]:
        sub = self.get_subsystem(subsystem_name)
        if not sub:
            return []
        return [i for i in self.interfaces if i.id in sub.interfaces]

    # --- Feasibility checks ---

    def check_mass_budget(self) -> tuple[bool, str]:
        """Verify subsystem mass budgets sum to ≤ global max."""
        max_mass = self.global_constraints.get("max_mass_kg")
        if max_mass is None:
            return True, "no global mass constraint"
        total = sum(s.mass_budget_kg or 0 for s in self.subsystems)
        ok = total <= max_mass
        msg = f"subsystem mass total {total:.3f} kg vs budget {max_mass:.3f} kg"
        return ok, msg

    def check_dangling_refs(self) -> tuple[bool, list[str]]:
        """Verify all subsystem interface refs point to existing interfaces."""
        ifc_ids = {i.id for i in self.interfaces}
        dangling = []
        for sub in self.subsystems:
            for ref in sub.interfaces:
                if ref not in ifc_ids:
                    dangling.append(f"{sub.name}→{ref}")
        return len(dangling) == 0, dangling

    # --- Serialization ---

    def to_yaml(self) -> str:
        return yaml.dump(self._to_dict(), default_flow_style=False, sort_keys=False)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_yaml())

    @classmethod
    def load(cls, path: Path) -> MasterSpec:
        data = yaml.safe_load(path.read_text())
        return cls._from_dict(data)

    def _to_dict(self) -> dict[str, Any]:
        from orchestrator._serde import dc_to_dict
        return dc_to_dict(self)

    @staticmethod
    def _sub_to_dict(s: Subsystem) -> dict[str, Any]:
        from orchestrator._serde import dc_to_dict
        return dc_to_dict(s)

    @staticmethod
    def _ifc_to_dict(i: Interface) -> dict[str, Any]:
        from orchestrator._serde import dc_to_dict
        return dc_to_dict(i)

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> MasterSpec:
        from orchestrator._serde import dc_from_dict
        return dc_from_dict(cls, d)
