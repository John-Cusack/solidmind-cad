"""Seeded-defect benchmark — the decision gate (Addendum A.2).

The architecture's central claim is that an LLM earns its place by compiling
open-world engineering knowledge into executable, validity-checked model
edits. This module is the experiment that can falsify it.

Each **problem** is a study whose frame carries a known defect (or, for
controls, none). Every arm sees the same inputs — the completed inner-loop
results — and returns a typed action. Scoring has two independent axes:

- **Diagnosis** as a confusion matrix. The controls row carries the
  **false-alarm rate**, which matters more than the hit rate: a prescriber
  that constantly cries "model inadequacy" burns weeks on phantom mechanisms.
- **Prescription** by *executing* the returned action and re-running. Three of
  the four action types do not execute into a re-run, so the answer key names
  the correct action per class rather than assuming every diagnosis becomes
  an edit.

Answer key (Addendum A.2):

==========================  ==================================  ==================
defect class                correct action                      scored by
==========================  ==================================  ==================
control (none)              ``no_action``                       the action itself
missing_design_variable     ``patch(study.variables)``          re-run improvement
inadequate_topology         ``patch(design_graph)``             re-run improvement
missing_mechanism           ``patch(model_registry)``           re-run improvement
wrong_objective             ``patch(study.objectives)``         the action itself
                            or ``escalate``
==========================  ==================================  ==================

**The missing-mechanism class is seeded by ablation**, not by asking for an
invention: a term is removed from an already-working chain, so the restoring
patch executes immediately and improvement is measurable. The ablated term is
withheld from the catalog the arms can see (``visible_catalog``), so
recovering it is an open-vocabulary proposal rather than spotting the one
unused entry in a list — otherwise the class silently becomes multiple choice
and flatters whichever arm is best at reading menus.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from server.dg_acoustic import commit_mesh
from server.dg_binding import Binding
from server.dg_import import import_brief_file
from server.diagnose import Diagnosis, diagnose
from server.eval_models import EvalRequest, EvalResult
from server.evaluator import evaluate_request
from server.model_registry import TERM_CATALOG, ModelTerm, ablate, commit_chain, full_chain
from server.paths import repo_root
from server.prescribe import ActionType, PatchTarget, TypedAction
from server.study_drivers import EvalOutcome, GridDriver
from server.study_models import DesignVariable, ObjectiveConfig, SolverConfig, Study

LATCH_BRIEF = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"


class DefectClass(str, Enum):
    CONTROL = "control"
    MISSING_DESIGN_VARIABLE = "missing_design_variable"
    INADEQUATE_TOPOLOGY = "inadequate_topology"
    MISSING_MECHANISM = "missing_mechanism"
    WRONG_OBJECTIVE = "wrong_objective"


# The answer key: what a correct prescriber returns for each class.
ANSWER_KEY: dict[DefectClass, tuple[tuple[ActionType, PatchTarget | None], ...]] = {
    DefectClass.CONTROL: ((ActionType.NO_ACTION, None),),
    DefectClass.MISSING_DESIGN_VARIABLE: ((ActionType.PATCH, PatchTarget.STUDY_VARIABLES),),
    DefectClass.INADEQUATE_TOPOLOGY: ((ActionType.PATCH, PatchTarget.DESIGN_GRAPH),),
    DefectClass.MISSING_MECHANISM: (
        (ActionType.PATCH, PatchTarget.MODEL_REGISTRY),
        (ActionType.REQUEST_MEASUREMENT, None),
    ),
    DefectClass.WRONG_OBJECTIVE: (
        (ActionType.PATCH, PatchTarget.STUDY_OBJECTIVES),
        # Adding the missing capability requirement is an equally correct fix,
        # and it is gated identically (it defines success, not where to look).
        (ActionType.PATCH, PatchTarget.STUDY_CONSTRAINTS),
        (ActionType.ESCALATE, None),
    ),
}


@dataclass(slots=True)
class Problem:
    """One benchmark problem: a study, its artifacts, and its seeded defect."""

    problem_id: str
    domain: str
    defect: DefectClass
    study: Study
    revision: str
    models_revision: str | None = None
    hidden_terms: dict[str, set[str]] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    def visible_catalog(self) -> dict[str, dict[str, ModelTerm]]:
        """The registry an arm is allowed to see (ablated terms withheld)."""
        return {
            stage: {
                tid: t for tid, t in terms.items() if tid not in self.hidden_terms.get(stage, set())
            }
            for stage, terms in TERM_CATALOG.items()
        }


@dataclass(slots=True)
class ArmResult:
    problem_id: str
    arm: str
    action: TypedAction
    diagnosis_correct: bool
    prescription_correct: bool | None  # None when the class is not re-run scored
    improvement: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "arm": self.arm,
            "action": self.action.to_dict(),
            "diagnosis_correct": self.diagnosis_correct,
            "prescription_correct": self.prescription_correct,
            "improvement": self.improvement,
        }


# An arm maps (problem, diagnosis, results) to a typed action.
Arm = Callable[[Problem, Diagnosis, list[EvalResult]], TypedAction]


class InProcessBackend:
    """Evaluation backend that calls the evaluator in-process (fast, same code)."""

    def __init__(self, problem: Problem, *, root: Path | None = None) -> None:
        self.problem = problem
        self.root = root
        self.results: list[EvalResult] = []

    def evaluate(self, bindings: dict[str, Any], tag: str) -> EvalOutcome:
        del tag
        study = self.problem.study
        request = EvalRequest(
            revision=self.problem.revision,
            bindings=tuple(Binding(path=p, value=v) for p, v in sorted(bindings.items())),
            scenario=study.scenario or "latch_hold",
            models=tuple(study.models or ("analytic_latch",)),
            models_revision=self.problem.models_revision,
        )
        result, code, _ = evaluate_request(request, root=self.root)
        self.results.append(result)
        if code != 0 or not result.ok:
            failure = result.failure or {}
            return EvalOutcome(ok=False, error=f"{failure.get('class')}: {failure.get('message')}")
        return EvalOutcome(
            ok=True,
            metrics={k: v.value for k, v in result.metrics.items()},
            flags=dict(result.flags),
        )


def run_inner_loop(problem: Problem, *, root: Path | None = None) -> list[EvalResult]:
    """Run the study's grid sweep; returns the evaluation results."""
    backend = InProcessBackend(problem, root=root)
    GridDriver().run(
        problem.study,
        backend,
        on_variant=lambda _v: None,
        cancelled=lambda: False,
    )
    return backend.results


# --- problem builders ------------------------------------------------------


def _latch_study(
    *,
    revision: str,
    name: str,
    variables: list[DesignVariable],
    objective: ObjectiveConfig,
) -> Study:
    return Study(
        id=Study.new_id(),
        name=name,
        variables=variables,
        solver=SolverConfig(solver_type="evaluator"),
        objective=objective,
        driver="grid",
        revision=revision,
        scenario="latch_hold",
        models=["analytic_latch"],
    )


def _mesh_study(
    *,
    revision: str,
    name: str,
    variables: list[DesignVariable],
    objective: ObjectiveConfig,
) -> Study:
    return Study(
        id=Study.new_id(),
        name=name,
        variables=variables,
        solver=SolverConfig(solver_type="evaluator"),
        objective=objective,
        driver="grid",
        revision=revision,
        scenario="acoustic_convective",
        models=["analytic_acoustic_bearing"],
    )


def _aperture_var(min_val: float = 0.5, max_val: float = 6.0, step: float = 0.5) -> DesignVariable:
    return DesignVariable(
        name="aperture_m",
        var_type="continuous",
        min_val=min_val,
        max_val=max_val,
        coarse_step=step,
        fine_step=step / 2,
        path="/array/aperture_m",
    )


def build_problems(*, root: Path | None = None) -> list[Problem]:
    """The benchmark set: both domains, four defect classes plus controls."""
    latch_rev = import_brief_file(LATCH_BRIEF, root=root)
    mesh_rev = commit_mesh(root=root)
    full = commit_chain(full_chain(), root=root)
    problems: list[Problem] = []

    fillet = DesignVariable(
        name="fillet_mm",
        var_type="continuous",
        min_val=0.0,
        max_val=0.6,
        coarse_step=0.1,
        fine_step=0.05,
        path="/parts/latch_sear/specs/fillet_mm",
    )

    # --- controls: a well-framed study in each domain -----------------------
    problems.append(
        Problem(
            problem_id="latch_control",
            domain="mechanical",
            defect=DefectClass.CONTROL,
            study=_latch_study(
                revision=latch_rev,
                name="latch fillet sweep (control)",
                variables=[fillet],
                objective=ObjectiveConfig(primary_metric="latch_fos", direction="maximize"),
            ),
            revision=latch_rev,
            models_revision=full,
        )
    )
    problems.append(
        Problem(
            problem_id="mesh_control",
            domain="acoustic",
            defect=DefectClass.CONTROL,
            study=_mesh_study(
                revision=mesh_rev,
                name="aperture sweep (control)",
                variables=[_aperture_var()],
                objective=ObjectiveConfig(primary_metric="position_margin", direction="maximize"),
            ),
            revision=mesh_rev,
            models_revision=full,
        )
    )

    # --- missing design variable -------------------------------------------
    # The sweep varies a parameter the model does not consume, so the
    # objective never moves: the dominant variable is missing from the frame.
    problems.append(
        Problem(
            problem_id="latch_missing_variable",
            domain="mechanical",
            defect=DefectClass.MISSING_DESIGN_VARIABLE,
            study=_latch_study(
                revision=latch_rev,
                name="latch sweep over an inert parameter",
                variables=[
                    DesignVariable(
                        name="barrel_wall_mm",
                        var_type="continuous",
                        min_val=2.0,
                        max_val=4.0,
                        coarse_step=0.5,
                        path="/parts/barrel/specs/wall_mm",
                    )
                ],
                objective=ObjectiveConfig(primary_metric="latch_fos", direction="maximize"),
            ),
            revision=latch_rev,
            models_revision=full,
            notes={"missing_variable": "/parts/latch_sear/specs/fillet_mm"},
        )
    )

    # --- inadequate topology ------------------------------------------------
    # Two nodes on a baseline with the target nearly collinear: no aperture
    # buys a usable fix. The parameterization cannot express the answer —
    # a third node is a design-graph change.
    collinear = commit_mesh(
        root=root,
        node_positions={"node_a": (-100.0, 0.0), "node_b": (100.0, 0.0)},
        target_xy=(400.0, 12.0),
        position_requirement_m=25.0,
    )
    problems.append(
        Problem(
            problem_id="mesh_topology",
            domain="acoustic",
            defect=DefectClass.INADEQUATE_TOPOLOGY,
            study=_mesh_study(
                revision=collinear,
                name="aperture sweep on a degenerate baseline",
                variables=[_aperture_var()],
                objective=ObjectiveConfig(
                    primary_metric="position_margin",
                    direction="maximize",
                    constraint_bounds={"detected": (1.0, None)},
                ),
            ),
            revision=collinear,
            models_revision=full,
            notes={"fix": "add a third node off the baseline"},
        )
    )

    # --- missing mechanism (ablation-seeded) --------------------------------
    ablated = commit_chain(
        ablate(full_chain(), "analytic_acoustic_bearing", "coherence_loss"), root=root
    )
    problems.append(
        Problem(
            problem_id="mesh_missing_mechanism",
            domain="acoustic",
            defect=DefectClass.MISSING_MECHANISM,
            study=_mesh_study(
                revision=mesh_rev,
                name="aperture sweep without turbulence decorrelation",
                variables=[_aperture_var()],
                objective=ObjectiveConfig(primary_metric="position_margin", direction="maximize"),
            ),
            revision=mesh_rev,
            models_revision=ablated,
            hidden_terms={"analytic_acoustic_bearing": {"coherence_loss"}},
            notes={"ablated": "analytic_acoustic_bearing:coherence_loss"},
        )
    )

    # --- wrong objective ----------------------------------------------------
    # The study optimizes a build-cost proxy (array span) with no capability
    # requirement attached, so the "best" design is the cheapest one that
    # cannot actually detect anything. Note that minimizing bearing sigma
    # would NOT be a wrong objective here: with fixed geometry it is a
    # monotone transform of position error and selects the same design.
    problems.append(
        Problem(
            problem_id="mesh_wrong_objective",
            domain="acoustic",
            defect=DefectClass.WRONG_OBJECTIVE,
            study=_mesh_study(
                revision=mesh_rev,
                name="aperture sweep minimizing array span only",
                variables=[_aperture_var()],
                objective=ObjectiveConfig(primary_metric="aperture_m", direction="minimize"),
            ),
            revision=mesh_rev,
            models_revision=full,
            notes={"better_objective": "position_margin", "missing_constraint": "detected"},
        )
    )
    return problems


# --- arms ------------------------------------------------------------------


def checklist_arm(problem: Problem, diagnosis: Diagnosis, results: list[EvalResult]) -> TypedAction:
    """The deterministic baseline: map findings to the action they imply.

    Deliberately mechanical. It fires on what it can see — flat objectives,
    bounds, no-op bindings, infeasibility — and has no way to propose a
    mechanism it was never shown, which is precisely the asymmetry the gate
    is measuring.
    """
    from server import prescribe

    del results
    codes = diagnosis.codes

    if diagnosis.has("no_feasible_variants"):
        return prescribe.escalate("no_feasible_design", finding_codes=tuple(sorted(codes)))
    if diagnosis.has("objective_flat") or diagnosis.has("unchanged_fingerprint"):
        return prescribe.patch(
            prescribe.PatchOp(
                target=PatchTarget.STUDY_VARIABLES,
                path="variables",
                value={"add": "a variable the model consumes"},
                op="add",
            ),
            finding_codes=tuple(sorted(codes)),
        )
    if diagnosis.has("optimum_at_bound"):
        evidence = next(f.evidence for f in diagnosis.findings if f.code == "optimum_at_bound")
        return prescribe.patch(
            prescribe.PatchOp(
                target=PatchTarget.STUDY_BOUNDS,
                path=str(evidence.get("variable", "")),
                value={"widen": evidence.get("bound")},
            ),
            finding_codes=tuple(sorted(codes)),
        )
    if diagnosis.has("validity_domain_hit"):
        return prescribe.request_measurement(
            {
                "what": "validity envelope",
                "paths": sorted(
                    next(
                        f.evidence for f in diagnosis.findings if f.code == "validity_domain_hit"
                    ).get("paths", [])
                ),
            },
            finding_codes=tuple(sorted(codes)),
        )
    return prescribe.no_action(finding_codes=tuple(sorted(codes)))


# --- scoring ---------------------------------------------------------------


def score_action(problem: Problem, action: TypedAction) -> bool:
    """Does this action match the answer key for the problem's defect class?"""
    accepted = ANSWER_KEY[problem.defect]
    for action_type, target in accepted:
        if action.type is not action_type:
            continue
        if target is None:
            return True
        if any(p.target is target for p in action.patches):
            return True
    return False


@dataclass(slots=True)
class ConfusionMatrix:
    """Diagnosis scoring, with the controls row broken out."""

    true_positive: int = 0  # defective problem, correctly actioned
    false_negative: int = 0  # defective problem, arm said no_action
    wrong_action: int = 0  # defective problem, wrong action type/target
    true_negative: int = 0  # control, arm said no_action
    false_positive: int = 0  # control, arm invented a problem

    @property
    def false_alarm_rate(self) -> float:
        controls = self.true_negative + self.false_positive
        return self.false_positive / controls if controls else 0.0

    @property
    def hit_rate(self) -> float:
        defective = self.true_positive + self.false_negative + self.wrong_action
        return self.true_positive / defective if defective else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "true_positive": self.true_positive,
            "false_negative": self.false_negative,
            "wrong_action": self.wrong_action,
            "true_negative": self.true_negative,
            "false_positive": self.false_positive,
            "hit_rate": round(self.hit_rate, 4),
            "false_alarm_rate": round(self.false_alarm_rate, 4),
        }


def score_arm(results: list[ArmResult], problems: dict[str, Problem]) -> ConfusionMatrix:
    matrix = ConfusionMatrix()
    for r in results:
        problem = problems[r.problem_id]
        is_control = problem.defect is DefectClass.CONTROL
        said_nothing = r.action.type is ActionType.NO_ACTION
        if is_control:
            if said_nothing:
                matrix.true_negative += 1
            else:
                matrix.false_positive += 1
        elif r.diagnosis_correct:
            matrix.true_positive += 1
        elif said_nothing:
            matrix.false_negative += 1
        else:
            matrix.wrong_action += 1
    return matrix


def run_benchmark(
    arms: dict[str, Arm],
    *,
    root: Path | None = None,
    problems: list[Problem] | None = None,
) -> dict[str, Any]:
    """Run every arm over every problem; return per-arm confusion matrices."""
    problem_list = problems if problems is not None else build_problems(root=root)
    by_id = {p.problem_id: p for p in problem_list}
    arm_results: dict[str, list[ArmResult]] = {name: [] for name in arms}

    for problem in problem_list:
        results = run_inner_loop(problem, root=root)
        diagnosis = diagnose(results, problem.study)
        for arm_name, arm in arms.items():
            action = arm(problem, diagnosis, results)
            correct = score_action(problem, action)
            arm_results[arm_name].append(
                ArmResult(
                    problem_id=problem.problem_id,
                    arm=arm_name,
                    action=action,
                    diagnosis_correct=correct,
                    prescription_correct=None,
                )
            )

    return {
        "problems": [
            {"problem_id": p.problem_id, "domain": p.domain, "defect": p.defect.value}
            for p in problem_list
        ],
        "arms": {
            name: {
                "matrix": score_arm(rs, by_id).to_dict(),
                "results": [r.to_dict() for r in rs],
            }
            for name, rs in arm_results.items()
        },
    }
