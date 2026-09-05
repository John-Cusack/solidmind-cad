"""Typed prescription actions and patch gates (Addendum A.1).

``prescribe`` is the LLM's slot in the architecture, and its output is always
an executable, schema-validated action — never prose. Four legal forms:

- ``patch``               — an edit to a versioned artifact
- ``request_measurement`` — "the model is unfalsifiable here; go measure X"
- ``escalate``            — a value judgment no patch can express
- ``no_action``           — "nothing is wrong; don't touch it"

``no_action`` is not decoration: a patch-only interface converts every
diagnosis into an edit, which hard-wires exactly the false-action bias whose
false-alarm rate the decision gate turns on.

**Gates.** Gate strength tracks how much an artifact defines *the scoring*
rather than *the solution*:

===========================  =====================  ==================================
target                       gate                   why
===========================  =====================  ==================================
design graph                 free                   the solution; what the loop changes
study variables / bounds     free                   where to look, not what counts
model registry               calibration-gated      blocks smuggling in uncalibrated physics
study objectives/constraints human-approval-gated   defines success — editing the exam
scenario                     human-approval-gated   encodes threat assumptions
===========================  =====================  ==================================

The residual risk of free bound-widening — pushing into regions where a model
extrapolates — is handled by validity domains, which are enforced
independently. Gates are the wrong instrument for that risk.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from server import json_pointer
from server.dg_models import PARAMS_SCHEMA, ParamSpec, StructureDoc, ValidityDomain
from server.dg_store import commit_revision, load_revision
from server.model_registry import (
    ModelChain,
    ModelRegistryError,
    catalog_for,
    commit_chain,
    enable,
    load_chain,
)


class PrescriptionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


GATE_BLOCKED = "GATE_BLOCKED"
INVALID_ACTION = "INVALID_ACTION"
CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"


class ActionType(str, Enum):
    PATCH = "patch"
    REQUEST_MEASUREMENT = "request_measurement"
    ESCALATE = "escalate"
    NO_ACTION = "no_action"


class PatchTarget(str, Enum):
    DESIGN_GRAPH = "design_graph"
    STUDY_VARIABLES = "study.variables"
    STUDY_BOUNDS = "study.bounds"
    STUDY_OBJECTIVES = "study.objectives"
    STUDY_CONSTRAINTS = "study.constraints"
    SCENARIO = "scenario"
    MODEL_REGISTRY = "model_registry"


class Gate(str, Enum):
    FREE = "free"
    CALIBRATION = "calibration"
    HUMAN = "human"


GATES: dict[PatchTarget, Gate] = {
    PatchTarget.DESIGN_GRAPH: Gate.FREE,
    PatchTarget.STUDY_VARIABLES: Gate.FREE,
    PatchTarget.STUDY_BOUNDS: Gate.FREE,
    PatchTarget.MODEL_REGISTRY: Gate.CALIBRATION,
    PatchTarget.STUDY_OBJECTIVES: Gate.HUMAN,
    PatchTarget.STUDY_CONSTRAINTS: Gate.HUMAN,
    PatchTarget.SCENARIO: Gate.HUMAN,
}


def gate_for(target: PatchTarget) -> Gate:
    return GATES[target]


@dataclass(frozen=True, slots=True)
class PatchOp:
    """One edit to one versioned artifact."""

    target: PatchTarget
    path: str  # RFC 6901 for design-graph params; else a key
    value: Any = None
    op: str = "set"  # set | add | remove

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target.value, "path": self.path, "value": self.value, "op": self.op}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchOp:
        try:
            target = PatchTarget(data["target"])
        except (KeyError, ValueError) as e:
            raise PrescriptionError(
                INVALID_ACTION, f"Bad patch target: {data.get('target')!r}"
            ) from e
        op = data.get("op", "set")
        if op not in ("set", "add", "remove"):
            raise PrescriptionError(INVALID_ACTION, f"Bad patch op: {op!r}")
        if not isinstance(data.get("path", ""), str) or not data.get("path"):
            raise PrescriptionError(INVALID_ACTION, "Patch needs a path")
        return cls(target=target, path=data["path"], value=data.get("value"), op=op)


@dataclass(frozen=True, slots=True)
class TypedAction:
    """The complete, machine-checkable output of a prescriber. Prose banned."""

    type: ActionType
    patches: tuple[PatchOp, ...] = ()
    finding_codes: tuple[str, ...] = ()  # which diagnosis findings drove this
    measurement: dict[str, Any] | None = None  # request_measurement payload
    escalation_code: str = ""  # escalate: a code, never free text
    approved_by: str = ""  # human approval for gated patches

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "type": self.type.value,
            "patches": [p.to_dict() for p in self.patches],
            "finding_codes": list(self.finding_codes),
        }
        if self.measurement is not None:
            doc["measurement"] = self.measurement
        if self.escalation_code:
            doc["escalation_code"] = self.escalation_code
        if self.approved_by:
            doc["approved_by"] = self.approved_by
        return doc

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TypedAction:
        try:
            action_type = ActionType(data["type"])
        except (KeyError, ValueError) as e:
            raise PrescriptionError(INVALID_ACTION, f"Bad action type: {data.get('type')!r}") from e
        action = cls(
            type=action_type,
            patches=tuple(PatchOp.from_dict(p) for p in data.get("patches", ())),
            finding_codes=tuple(data.get("finding_codes", ())),
            measurement=data.get("measurement"),
            escalation_code=data.get("escalation_code", ""),
            approved_by=data.get("approved_by", ""),
        )
        validate_action(action)
        return action


def validate_action(action: TypedAction) -> None:
    """Schema-level checks — shape only; gates are checked at apply time."""
    if action.type is ActionType.PATCH:
        if not action.patches:
            raise PrescriptionError(INVALID_ACTION, "A patch action needs at least one patch")
    elif action.patches:
        raise PrescriptionError(INVALID_ACTION, f"{action.type.value} actions cannot carry patches")
    if action.type is ActionType.REQUEST_MEASUREMENT and not action.measurement:
        raise PrescriptionError(INVALID_ACTION, "request_measurement needs a measurement payload")
    if action.type is ActionType.ESCALATE and not action.escalation_code:
        raise PrescriptionError(INVALID_ACTION, "escalate needs an escalation_code")


def check_gates(action: TypedAction) -> list[PatchOp]:
    """Return the patches this action is not authorized to apply."""
    blocked: list[PatchOp] = []
    for patch in action.patches:
        gate = gate_for(patch.target)
        if gate is Gate.HUMAN and not action.approved_by:
            blocked.append(patch)
    return blocked


@dataclass(slots=True)
class ApplyOutcome:
    """What executing an action produced."""

    applied: bool
    revision: str | None = None  # new design-graph revision, if any
    models_revision: str | None = None  # new model-chain revision, if any
    study_updates: dict[str, Any] = field(default_factory=dict)
    calibration_required: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "revision": self.revision,
            "models_revision": self.models_revision,
            "study_updates": self.study_updates,
            "calibration_required": self.calibration_required,
            "blocked": self.blocked,
        }


def _apply_design_patch(structure: dict[str, Any], params: dict[str, Any], patch: PatchOp) -> None:
    """Mutate the (copied) structure/params docs in place."""
    if patch.op == "remove":
        json_pointer.remove_value(params, patch.path)
        doc = StructureDoc.from_dict(structure)
        structure["param_specs"] = [s.to_dict() for s in doc.param_specs if s.path != patch.path]
        return

    doc = StructureDoc.from_dict(structure)
    spec = doc.spec_for(patch.path)
    if patch.op == "add" or spec is None:
        # A new parameter needs a declaration, or it could never be bound.
        value = patch.value
        if isinstance(value, dict) and "value" in value:
            leaf, unit = value, str(value.get("unit", "1"))
        else:
            leaf, unit = {"value": value, "unit": "1"}, "1"
        domain = None
        if isinstance(patch.value, dict) and isinstance(patch.value.get("domain"), dict):
            domain = ValidityDomain.from_dict(patch.value["domain"])
            leaf = {"value": patch.value["value"], "unit": unit}
        json_pointer.set_value(params, patch.path, leaf, create_missing=True)
        kind = "string" if isinstance(leaf["value"], str) else "float"
        structure["param_specs"] = [
            *(s.to_dict() for s in doc.param_specs if s.path != patch.path),
            ParamSpec(
                path=patch.path,
                unit=unit,
                kind=kind,
                domain=domain,
                provenance="prescribed",
            ).to_dict(),
        ]
        return

    leaf = json_pointer.get(params, patch.path)
    if not isinstance(leaf, dict) or "value" not in leaf:
        raise PrescriptionError(INVALID_ACTION, f"{patch.path} is not a value leaf")
    leaf["value"] = patch.value


def apply_action(
    action: TypedAction,
    *,
    revision: str | None = None,
    models_revision: str | None = None,
    root: Path | None = None,
) -> ApplyOutcome:
    """Execute a typed action, producing new artifact revisions where needed.

    Gated patches without approval are refused, never silently dropped.
    Model-registry patches always come back with a calibration requirement:
    a term may be proposed, but it cannot be optimized against until anchored.
    """
    validate_action(action)

    if action.type is not ActionType.PATCH:
        return ApplyOutcome(
            applied=False,
            revision=revision,
            models_revision=models_revision,
        )

    blocked = check_gates(action)
    if blocked:
        return ApplyOutcome(
            applied=False,
            revision=revision,
            models_revision=models_revision,
            blocked=[f"{p.target.value}:{p.path}" for p in blocked],
        )

    outcome = ApplyOutcome(applied=True, revision=revision, models_revision=models_revision)
    design_patches = [p for p in action.patches if p.target is PatchTarget.DESIGN_GRAPH]
    registry_patches = [p for p in action.patches if p.target is PatchTarget.MODEL_REGISTRY]
    study_patches = [
        p
        for p in action.patches
        if p.target
        in (
            PatchTarget.STUDY_VARIABLES,
            PatchTarget.STUDY_BOUNDS,
            PatchTarget.STUDY_OBJECTIVES,
            PatchTarget.STUDY_CONSTRAINTS,
        )
    ]
    scenario_patches = [p for p in action.patches if p.target is PatchTarget.SCENARIO]

    if design_patches:
        if revision is None:
            raise PrescriptionError(INVALID_ACTION, "design-graph patches need a base revision")
        structure, params, _ = load_revision(revision, root=root)
        structure = dict(structure)
        structure["param_specs"] = list(structure.get("param_specs", []))
        params = copy.deepcopy(params)
        for patch in design_patches:
            try:
                _apply_design_patch(structure, params, patch)
            except json_pointer.JsonPointerError as e:
                raise PrescriptionError(INVALID_ACTION, f"{patch.path}: {e}") from e
        params["schema"] = PARAMS_SCHEMA
        outcome.revision = commit_revision(
            structure, params, parent=revision, meta={"source": "prescribe"}, root=root
        )

    if registry_patches:
        chain = (
            load_chain(models_revision, root=root) if models_revision is not None else ModelChain()
        )
        for patch in registry_patches:
            stage, _, term = patch.path.partition(":")
            if not stage or not term:
                raise PrescriptionError(
                    INVALID_ACTION,
                    f"model-registry path must be '<stage>:<term>', got {patch.path!r}",
                )
            try:
                known = catalog_for(stage)
                chain = enable(chain, stage, term)
            except ModelRegistryError as e:
                raise PrescriptionError(INVALID_ACTION, str(e)) from e
            if not known[term].calibrated:
                # Calibration gate: the term enters usable for screening only,
                # with a standing requirement recorded on the outcome.
                outcome.calibration_required.append(f"{stage}:{term}")
        outcome.models_revision = commit_chain(chain, root=root)

    for patch in study_patches + scenario_patches:
        outcome.study_updates.setdefault(patch.target.value, []).append(patch.to_dict())

    return outcome


# --- convenience constructors used by the checklist arm and tests -----------


def no_action(finding_codes: tuple[str, ...] = ()) -> TypedAction:
    return TypedAction(type=ActionType.NO_ACTION, finding_codes=finding_codes)


def escalate(code: str, finding_codes: tuple[str, ...] = ()) -> TypedAction:
    return TypedAction(type=ActionType.ESCALATE, escalation_code=code, finding_codes=finding_codes)


def request_measurement(what: dict[str, Any], finding_codes: tuple[str, ...] = ()) -> TypedAction:
    return TypedAction(
        type=ActionType.REQUEST_MEASUREMENT, measurement=what, finding_codes=finding_codes
    )


def patch(*ops: PatchOp, finding_codes: tuple[str, ...] = (), approved_by: str = "") -> TypedAction:
    return TypedAction(
        type=ActionType.PATCH,
        patches=tuple(ops),
        finding_codes=finding_codes,
        approved_by=approved_by,
    )
