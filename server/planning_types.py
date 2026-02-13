from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from server.jcs import canonicalize as jcs_canonicalize


@dataclass(frozen=True, slots=True)
class PlanningQuestionBudget:
    max_questions: int
    questions_asked: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlanningCheckpoint:
    checkpoint_id: str
    validations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlanningOperation:
    op_id: str
    op_type: str
    reference_support_type: str | None = None
    topology_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class PlanningPhase:
    phase_id: str
    goal: str
    checkpoints: list[PlanningCheckpoint] = field(default_factory=list)
    operations: list[PlanningOperation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RepairDirective:
    playbook_id: str
    trigger: str
    actions: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlanningContext:
    process: str
    archetype: str
    policy_key: str
    units: str = "mm"
    normalized_spec: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanningPlan:
    plan_version: str
    policy_key: str
    process: str
    archetype: str
    question_budget: PlanningQuestionBudget
    assumptions: list[str]
    phases: list[PlanningPhase]
    repair_directives: list[RepairDirective]


@dataclass(frozen=True, slots=True)
class PlanningPolicyConstraint:
    constraint_id: str
    severity: str
    metric: str
    operator: str
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    rationale: str = ""
    playbook_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningPolicyRepairPlaybook:
    playbook_id: str
    trigger: str
    steps: list[str]


@dataclass(frozen=True, slots=True)
class PlanningPolicyPhase:
    phase_id: str
    checkpoints: list[PlanningCheckpoint]


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    key: str
    process: str
    archetype: str
    required_parameters: list[dict[str, Any]]
    reference_strategy: dict[str, Any]
    phase_order: list[str]
    phase_policies: dict[str, PlanningPolicyPhase]
    dfm_constraints: list[PlanningPolicyConstraint]
    repair_playbooks: list[PlanningPolicyRepairPlaybook]


@dataclass(frozen=True, slots=True)
class PlanningPolicyManifest:
    version: str
    default_question_budget: int
    policies: dict[str, PlanningPolicy]


def planning_plan_to_dict(plan: PlanningPlan) -> dict[str, Any]:
    return {
        "plan_version": plan.plan_version,
        "policy_key": plan.policy_key,
        "process": plan.process,
        "archetype": plan.archetype,
        "question_budget": {
            "max_questions": int(plan.question_budget.max_questions),
            "questions_asked": list(plan.question_budget.questions_asked),
        },
        "assumptions": list(plan.assumptions),
        "phases": [
            {
                "phase_id": p.phase_id,
                "goal": p.goal,
                "checkpoints": [
                    {
                        "checkpoint_id": c.checkpoint_id,
                        "validations": list(c.validations),
                    }
                    for c in p.checkpoints
                ],
                "operations": [
                    {
                        "op_id": o.op_id,
                        "op_type": o.op_type,
                        "reference_support_type": o.reference_support_type,
                        "topology_sensitive": bool(o.topology_sensitive),
                    }
                    for o in p.operations
                ],
            }
            for p in plan.phases
        ],
        "repair_directives": [
            {
                "playbook_id": r.playbook_id,
                "trigger": r.trigger,
                "actions": list(r.actions),
            }
            for r in plan.repair_directives
        ],
    }


def compute_planning_plan_hash(plan: PlanningPlan) -> str:
    canonical_str = jcs_canonicalize(planning_plan_to_dict(plan))
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
