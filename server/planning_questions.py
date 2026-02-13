from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class QuestionBudgetResult:
    max_questions: int
    questions_asked: list[str]
    assumptions: list[str]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def evaluate_planning_question_budget(
    normalized_spec: dict[str, Any],
    process: str,
    archetype: str,
    max_questions: int = 2,
) -> QuestionBudgetResult:
    """Plan-only question budget engine.

    This engine does not ask interactively; it deterministically records which
    structural clarifications would be asked and then emits assumptions.
    """
    questions_asked: list[str] = []
    assumptions: list[str] = []

    planning = normalized_spec.get("planning", {})
    if not isinstance(planning, dict):
        planning = {}

    manufacturing = normalized_spec.get("manufacturing", {})
    if not isinstance(manufacturing, dict):
        manufacturing = {}

    # Question slot 1: process-critical setup
    if len(questions_asked) < max_questions:
        if process == "cnc":
            if _is_missing(planning.get("machine_mode")):
                questions_asked.append("machine_mode")
                assumptions.append("Assume CNC 3-axis machining and single setup unless specified otherwise.")
        elif process == "fdm":
            if _is_missing(planning.get("build_orientation")):
                questions_asked.append("build_orientation")
                assumptions.append("Assume Z-up build orientation with support minimization policy.")

    # Question slot 2: interface-critical detail
    if len(questions_asked) < max_questions:
        part_interfaces = None
        part = normalized_spec.get("part", {})
        if isinstance(part, dict):
            part_interfaces = part.get("interfaces")

        if part_interfaces in (None, [], ""):
            questions_asked.append("critical_interfaces")
            assumptions.append("Assume no additional critical interfaces beyond explicit hole/feature definitions.")

    # Archetype-specific assumptions when data is missing
    if archetype == "thin_wall" and _is_missing(planning.get("nominal_wall_mm")):
        assumptions.append("Assume nominal thin-wall thickness of 1.2 mm.")

    if process == "fdm" and _is_missing(planning.get("nozzle_diameter_mm")):
        assumptions.append("Assume nozzle diameter 0.4 mm.")

    if process == "fdm" and _is_missing(planning.get("layer_height_mm")):
        assumptions.append("Assume layer height 0.2 mm.")

    return QuestionBudgetResult(
        max_questions=max_questions,
        questions_asked=questions_asked[:max_questions],
        assumptions=assumptions,
    )
