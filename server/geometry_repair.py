from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RepairRecommendation:
    playbook_id: str
    trigger: str
    actions: list[str]

    def to_notice(self) -> dict[str, Any]:
        return {
            "code": "REPAIR_RECOMMENDATION",
            "severity": "notice",
            "message": f"Apply playbook '{self.playbook_id}' for trigger '{self.trigger}'.",
            "context": {
                "playbook_id": self.playbook_id,
                "trigger": self.trigger,
                "actions": list(self.actions),
            },
            "recommended_actions": list(self.actions),
        }


def recommend_repairs(
    *,
    execution_trace: Any,
    verification_report: Any,
    planning_plan: dict[str, Any] | None,
) -> list[RepairRecommendation]:
    """Generate deterministic repair recommendations.

    Input objects are treated structurally (dict/dataclass), so this helper can
    be used from both execution and tests without strict runtime coupling.
    """
    recommendations: list[RepairRecommendation] = []

    # 1) Execution failures
    failed_steps = []
    steps = getattr(execution_trace, "steps", None)
    if isinstance(steps, list):
        for step in steps:
            if getattr(step, "status", "") == "failed":
                failed_steps.append(step)

    if failed_steps:
        recommendations.append(
            RepairRecommendation(
                playbook_id="topology_drift_cascade",
                trigger="execution_failed",
                actions=[
                    "Rollback to last passing checkpoint.",
                    "Rebuild failing phase using datum/origin references.",
                    "Re-run with topology-sensitive features delayed.",
                ],
            )
        )

    # 2) Verification failures
    results = getattr(verification_report, "results", None)
    if isinstance(results, list):
        for result in results:
            check_type = getattr(result, "check_type", "")
            passed = bool(getattr(result, "passed", True))
            if passed:
                continue

            if check_type == "internal_radius":
                recommendations.append(
                    RepairRecommendation(
                        playbook_id="fillet_failure",
                        trigger="internal_radius_violation",
                        actions=[
                            "Split fillet operations by radius group.",
                            "Apply larger radii first.",
                            "Explicitly select failing edges (disable auto-chain).",
                        ],
                    )
                )
            elif check_type in ("pocket_depth_ratio", "hole_depth_ratio"):
                recommendations.append(
                    RepairRecommendation(
                        playbook_id="cnc_dfm_violation",
                        trigger=check_type,
                        actions=[
                            "Re-parameterize feature depth/width ratio.",
                            "Consider stepped geometry or process update.",
                        ],
                    )
                )
            elif check_type in ("overhang_angle", "bridge_span", "wall_thickness"):
                recommendations.append(
                    RepairRecommendation(
                        playbook_id="fdm_dfm_violation",
                        trigger=check_type,
                        actions=[
                            "Re-orient build or allow supports.",
                            "Increase wall thickness / add reinforcement.",
                            "Reduce unsupported span.",
                        ],
                    )
                )

    # 3) Planning playbook hints if available
    if isinstance(planning_plan, dict):
        directives = planning_plan.get("repair_directives", [])
        if isinstance(directives, list):
            for directive in directives:
                if not isinstance(directive, dict):
                    continue
                pid = str(directive.get("playbook_id", "")).strip()
                trigger = str(directive.get("trigger", "")).strip()
                actions = directive.get("actions", [])
                if pid and trigger and isinstance(actions, list):
                    recommendations.append(
                        RepairRecommendation(
                            playbook_id=pid,
                            trigger=trigger,
                            actions=[str(a) for a in actions],
                        )
                    )

    # deterministic de-dup by (playbook_id, trigger)
    uniq: dict[tuple[str, str], RepairRecommendation] = {}
    for rec in recommendations:
        uniq[(rec.playbook_id, rec.trigger)] = rec

    return [uniq[k] for k in sorted(uniq.keys())]
