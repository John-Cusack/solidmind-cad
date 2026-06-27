"""Normalizer — parse and validate user goals into structured objectives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedGoal:
    """A validated set of objectives and constraints derived from user input."""

    objectives: list[dict[str, Any]] = field(default_factory=list)
    global_constraints: dict[str, Any] = field(default_factory=dict)
    process_assumptions: list[str] = field(default_factory=list)
    duty_cycle: str = ""
    notes: str = ""


def validate_normalized_goal(goal: NormalizedGoal) -> tuple[bool, list[str]]:
    """Validate that a NormalizedGoal is well-formed.

    Returns (ok, issues). Every objective must have direction + unit,
    no duplicate names, and constraints must be non-empty.
    """
    issues: list[str] = []

    if not goal.objectives:
        issues.append("No objectives defined")

    seen_names: set[str] = set()
    for i, obj in enumerate(goal.objectives):
        name = obj.get("name", "")
        if not name:
            issues.append(f"Objective {i} missing 'name'")
        elif name in seen_names:
            issues.append(f"Duplicate objective name: '{name}'")
        else:
            seen_names.add(name)

        if not obj.get("direction"):
            issues.append(f"Objective '{name or i}' missing 'direction'")
        elif obj["direction"] not in ("minimize", "maximize"):
            issues.append(
                f"Objective '{name}' direction must be 'minimize' or 'maximize', "
                f"got '{obj['direction']}'"
            )

        if not obj.get("unit"):
            issues.append(f"Objective '{name or i}' missing 'unit'")

    if not goal.global_constraints:
        issues.append("No global constraints defined")

    return len(issues) == 0, issues


def normalize_from_dict(raw: dict[str, Any]) -> NormalizedGoal:
    """Parse a raw dict into a NormalizedGoal dataclass."""
    return NormalizedGoal(
        objectives=raw.get("objectives", []),
        global_constraints=raw.get("global_constraints", {}),
        process_assumptions=raw.get("process_assumptions", []),
        duty_cycle=raw.get("duty_cycle", ""),
        notes=raw.get("notes", ""),
    )


def goal_to_spec_fields(goal: NormalizedGoal) -> dict[str, Any]:
    """Extract MasterSpec-compatible fields from a NormalizedGoal.

    Returns a dict with keys that can be used to populate a MasterSpec:
    - objectives: list of Objective-compatible dicts
    - global_constraints: dict
    """
    from orchestrator.spec import Objective

    objectives = []
    for obj in goal.objectives:
        objectives.append(
            Objective(
                name=obj.get("name", ""),
                direction=obj.get("direction", ""),
                unit=obj.get("unit", ""),
                weight=obj.get("weight", 1.0),
                threshold=obj.get("threshold"),
            )
        )

    return {
        "objectives": objectives,
        "global_constraints": goal.global_constraints,
    }
