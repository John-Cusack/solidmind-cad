"""Data models for parametric design optimization studies."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import Any, Literal


class StudyStatus(str, Enum):
    DRAFT = "draft"
    RUNNING_COARSE = "running_coarse"
    COARSE_DONE = "coarse_done"
    RUNNING_REFINED = "running_refined"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


VariableType = Literal["continuous", "discrete", "categorical"]
OptDirection = Literal["maximize", "minimize"]


@dataclass(frozen=True, slots=True)
class DesignVariable:
    """A single design variable to sweep over."""

    name: str
    var_type: VariableType
    min_val: float | None = None
    max_val: float | None = None
    coarse_step: float | None = None
    fine_step: float | None = None
    categories: tuple[str, ...] = ()
    pinned_values: tuple[float, ...] = ()

    def expand_coarse(self) -> list[float | str]:
        """Generate coarse sweep values for this variable."""
        if self.var_type == "categorical":
            return list(self.categories)

        if self.min_val is None or self.max_val is None:
            return list(self.pinned_values) if self.pinned_values else []

        step = self.coarse_step
        if step is None or step <= 0:
            # Default: ~5 steps across range
            rng = self.max_val - self.min_val
            step = rng / 5 if rng > 0 else 1.0

        values: list[float] = []
        v = self.min_val
        while v <= self.max_val + step * 1e-9:
            values.append(round(v, 10))
            v += step

        # Merge pinned values (from research) that aren't already present
        for pv in self.pinned_values:
            if self.min_val <= pv <= self.max_val and not any(
                math.isclose(pv, ev, rel_tol=1e-9) for ev in values
            ):
                values.append(pv)

        return sorted(values)

    def expand_refined(self, center: float, *, num_steps: int = 5) -> list[float | str]:
        """Generate refined sweep values around a center point."""
        if self.var_type == "categorical":
            return list(self.categories)

        if self.min_val is None or self.max_val is None:
            return [center]

        step = self.fine_step
        if step is None or step <= 0:
            coarse = self.coarse_step or ((self.max_val - self.min_val) / 5)
            step = coarse / 5

        half = num_steps // 2
        values: list[float] = []
        for i in range(-half, half + 1):
            v = round(center + i * step, 10)
            if self.min_val <= v <= self.max_val:
                values.append(v)

        return sorted(set(values))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "var_type": self.var_type,
        }
        if self.min_val is not None:
            d["min_val"] = self.min_val
        if self.max_val is not None:
            d["max_val"] = self.max_val
        if self.coarse_step is not None:
            d["coarse_step"] = self.coarse_step
        if self.fine_step is not None:
            d["fine_step"] = self.fine_step
        if self.categories:
            d["categories"] = list(self.categories)
        if self.pinned_values:
            d["pinned_values"] = list(self.pinned_values)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DesignVariable:
        return cls(
            name=d["name"],
            var_type=d["var_type"],
            min_val=d.get("min_val"),
            max_val=d.get("max_val"),
            coarse_step=d.get("coarse_step"),
            fine_step=d.get("fine_step"),
            categories=tuple(d.get("categories", ())),
            pinned_values=tuple(d.get("pinned_values", ())),
        )


@dataclass(frozen=True, slots=True)
class SolverConfig:
    """Configuration for the simulation solver."""

    solver_type: str  # "bemt_xfoil" | "openfoam"
    params: dict[str, Any] = dc_field(default_factory=dict)
    timeout_s: float = 300.0
    geometry_script: str | None = None  # path to FreeCAD headless geometry generator

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "solver_type": self.solver_type,
            "params": self.params,
            "timeout_s": self.timeout_s,
        }
        if self.geometry_script is not None:
            d["geometry_script"] = self.geometry_script
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SolverConfig:
        return cls(
            solver_type=d["solver_type"],
            params=d.get("params", {}),
            timeout_s=d.get("timeout_s", 300.0),
            geometry_script=d.get("geometry_script"),
        )


@dataclass(frozen=True, slots=True)
class ObjectiveConfig:
    """Defines what to optimize for."""

    primary_metric: str
    direction: OptDirection = "maximize"
    constraint_bounds: dict[str, tuple[float | None, float | None]] = dc_field(
        default_factory=dict,
    )
    weights: dict[str, float] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_metric": self.primary_metric,
            "direction": self.direction,
            "constraint_bounds": {k: list(v) for k, v in self.constraint_bounds.items()},
            "weights": self.weights,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObjectiveConfig:
        bounds: dict[str, tuple[float | None, float | None]] = {}
        for k, v in d.get("constraint_bounds", {}).items():
            lo = v[0] if v[0] is not None else None
            hi = v[1] if len(v) > 1 and v[1] is not None else None
            bounds[k] = (lo, hi)
        return cls(
            primary_metric=d["primary_metric"],
            direction=d.get("direction", "maximize"),
            constraint_bounds=bounds,
            weights=d.get("weights", {}),
        )


@dataclass(slots=True)
class Variant:
    """A single design point with its evaluation results."""

    variant_id: str
    params: dict[str, Any]
    phase: Literal["coarse", "refined"]
    status: Literal["pending", "running", "done", "failed"] = "pending"
    metrics: dict[str, float] = dc_field(default_factory=dict)
    solver_time_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "variant_id": self.variant_id,
            "params": self.params,
            "phase": self.phase,
            "status": self.status,
            "metrics": self.metrics,
            "solver_time_s": self.solver_time_s,
        }
        if self.error:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Variant:
        return cls(
            variant_id=d["variant_id"],
            params=d["params"],
            phase=d["phase"],
            status=d.get("status", "pending"),
            metrics=d.get("metrics", {}),
            solver_time_s=d.get("solver_time_s", 0.0),
            error=d.get("error"),
        )


@dataclass(slots=True)
class Study:
    """Top-level mutable container for a parametric study."""

    id: str
    name: str
    variables: list[DesignVariable]
    solver: SolverConfig
    objective: ObjectiveConfig
    fixed_params: dict[str, Any] = dc_field(default_factory=dict)
    status: StudyStatus = StudyStatus.DRAFT
    coarse_variants: list[Variant] = dc_field(default_factory=list)
    refined_variants: list[Variant] = dc_field(default_factory=list)
    best_variant_id: str | None = None
    pid: int | None = None
    error: str | None = None
    started_at: float | None = None  # time.time() when run started
    finished_at: float | None = None

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "variables": [v.to_dict() for v in self.variables],
            "solver": self.solver.to_dict(),
            "objective": self.objective.to_dict(),
            "fixed_params": self.fixed_params,
            "status": self.status.value,
            "coarse_variants": [v.to_dict() for v in self.coarse_variants],
            "refined_variants": [v.to_dict() for v in self.refined_variants],
        }
        if self.best_variant_id:
            d["best_variant_id"] = self.best_variant_id
        if self.pid is not None:
            d["pid"] = self.pid
        if self.error:
            d["error"] = self.error
        if self.started_at is not None:
            d["started_at"] = self.started_at
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Study:
        return cls(
            id=d["id"],
            name=d["name"],
            variables=[DesignVariable.from_dict(v) for v in d["variables"]],
            solver=SolverConfig.from_dict(d["solver"]),
            objective=ObjectiveConfig.from_dict(d["objective"]),
            fixed_params=d.get("fixed_params", {}),
            status=StudyStatus(d.get("status", "draft")),
            coarse_variants=[Variant.from_dict(v) for v in d.get("coarse_variants", [])],
            refined_variants=[Variant.from_dict(v) for v in d.get("refined_variants", [])],
            best_variant_id=d.get("best_variant_id"),
            pid=d.get("pid"),
            error=d.get("error"),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
        )
