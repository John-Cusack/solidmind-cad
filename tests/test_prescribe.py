"""Tests for typed prescription actions and the Addendum A.1 patch gates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server import json_pointer, prescribe
from server.dg_import import import_brief_file
from server.dg_store import load_revision
from server.model_registry import (
    ModelChain,
    ablate,
    commit_chain,
    full_chain,
    load_chain,
)
from server.paths import repo_root
from server.prescribe import (
    ActionType,
    Gate,
    PatchOp,
    PatchTarget,
    PrescriptionError,
    TypedAction,
    apply_action,
    check_gates,
    gate_for,
    validate_action,
)

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"


class TestGateTable(unittest.TestCase):
    def test_solution_side_is_free(self) -> None:
        self.assertIs(gate_for(PatchTarget.DESIGN_GRAPH), Gate.FREE)
        self.assertIs(gate_for(PatchTarget.STUDY_VARIABLES), Gate.FREE)
        self.assertIs(gate_for(PatchTarget.STUDY_BOUNDS), Gate.FREE)

    def test_scoring_side_is_human_gated(self) -> None:
        self.assertIs(gate_for(PatchTarget.STUDY_OBJECTIVES), Gate.HUMAN)
        self.assertIs(gate_for(PatchTarget.STUDY_CONSTRAINTS), Gate.HUMAN)
        self.assertIs(gate_for(PatchTarget.SCENARIO), Gate.HUMAN)

    def test_model_registry_is_calibration_gated(self) -> None:
        self.assertIs(gate_for(PatchTarget.MODEL_REGISTRY), Gate.CALIBRATION)


class TestActionSchema(unittest.TestCase):
    def test_patch_needs_patches(self) -> None:
        with self.assertRaises(PrescriptionError):
            validate_action(TypedAction(type=ActionType.PATCH))

    def test_non_patch_cannot_carry_patches(self) -> None:
        with self.assertRaises(PrescriptionError):
            validate_action(
                TypedAction(
                    type=ActionType.NO_ACTION,
                    patches=(PatchOp(target=PatchTarget.DESIGN_GRAPH, path=FILLET, value=1.0),),
                )
            )

    def test_escalate_needs_a_code(self) -> None:
        with self.assertRaises(PrescriptionError):
            validate_action(TypedAction(type=ActionType.ESCALATE))
        validate_action(prescribe.escalate("competing_objectives"))

    def test_request_measurement_needs_payload(self) -> None:
        with self.assertRaises(PrescriptionError):
            validate_action(TypedAction(type=ActionType.REQUEST_MEASUREMENT))
        validate_action(prescribe.request_measurement({"what": "coherence vs separation"}))

    def test_no_action_is_legal_and_empty(self) -> None:
        action = prescribe.no_action(("objective_flat",))
        validate_action(action)
        self.assertEqual(action.patches, ())

    def test_serde_round_trip(self) -> None:
        action = prescribe.patch(
            PatchOp(target=PatchTarget.DESIGN_GRAPH, path=FILLET, value=0.5),
            finding_codes=("optimum_at_bound",),
        )
        self.assertEqual(TypedAction.from_dict(action.to_dict()).to_dict(), action.to_dict())

    def test_bad_target_and_op_rejected(self) -> None:
        with self.assertRaises(PrescriptionError):
            PatchOp.from_dict({"target": "the_vibes", "path": "/x"})
        with self.assertRaises(PrescriptionError):
            PatchOp.from_dict({"target": "design_graph", "path": "/x", "op": "obliterate"})
        with self.assertRaises(PrescriptionError):
            PatchOp.from_dict({"target": "design_graph", "path": ""})


class PrescribeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"
        self.revision = import_brief_file(FIXTURE, root=self.root)


class TestGateEnforcement(PrescribeBase):
    def test_human_gated_patch_blocked_without_approval(self) -> None:
        action = prescribe.patch(
            PatchOp(target=PatchTarget.STUDY_OBJECTIVES, path="primary_metric", value="x")
        )
        self.assertEqual(len(check_gates(action)), 1)
        outcome = apply_action(action, revision=self.revision, root=self.root)
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.blocked, ["study.objectives:primary_metric"])

    def test_human_gated_patch_applies_with_approval(self) -> None:
        action = prescribe.patch(
            PatchOp(target=PatchTarget.STUDY_OBJECTIVES, path="primary_metric", value="x"),
            approved_by="john",
        )
        self.assertEqual(check_gates(action), [])
        outcome = apply_action(action, revision=self.revision, root=self.root)
        self.assertTrue(outcome.applied)
        self.assertIn("study.objectives", outcome.study_updates)

    def test_scenario_patch_is_gated(self) -> None:
        action = prescribe.patch(PatchOp(target=PatchTarget.SCENARIO, path="cn2", value=1e-9))
        self.assertFalse(apply_action(action, revision=self.revision, root=self.root).applied)

    def test_free_targets_need_no_approval(self) -> None:
        action = prescribe.patch(
            PatchOp(target=PatchTarget.STUDY_BOUNDS, path="fillet_mm", value={"max": 1.0})
        )
        outcome = apply_action(action, revision=self.revision, root=self.root)
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.blocked, [])


class TestDesignGraphPatches(PrescribeBase):
    def test_set_existing_parameter_creates_child_revision(self) -> None:
        action = prescribe.patch(PatchOp(target=PatchTarget.DESIGN_GRAPH, path=FILLET, value=0.45))
        outcome = apply_action(action, revision=self.revision, root=self.root)
        self.assertTrue(outcome.applied)
        self.assertNotEqual(outcome.revision, self.revision)
        _structure, params, manifest = load_revision(outcome.revision, root=self.root)
        self.assertEqual(json_pointer.get(params, FILLET)["value"], 0.45)
        self.assertEqual(manifest.parent, self.revision)
        # The original revision is untouched — artifacts are immutable.
        _s0, params0, _m0 = load_revision(self.revision, root=self.root)
        self.assertEqual(json_pointer.get(params0, FILLET)["value"], 0.0)

    def test_add_new_parameter_declares_it(self) -> None:
        path = "/parts/latch_sear/specs/rib_mm"
        action = prescribe.patch(
            PatchOp(
                target=PatchTarget.DESIGN_GRAPH,
                path=path,
                value={"value": 2.0, "unit": "mm"},
                op="add",
            )
        )
        outcome = apply_action(action, revision=self.revision, root=self.root)
        structure, params, _ = load_revision(outcome.revision, root=self.root)
        self.assertEqual(json_pointer.get(params, path)["value"], 2.0)
        # A new parameter must be declared or it could never be bound.
        spec = next(s for s in structure["param_specs"] if s["path"] == path)
        self.assertEqual(spec["unit"], "mm")
        self.assertEqual(spec["provenance"], "prescribed")

    def test_remove_parameter_drops_declaration(self) -> None:
        action = prescribe.patch(PatchOp(target=PatchTarget.DESIGN_GRAPH, path=FILLET, op="remove"))
        outcome = apply_action(action, revision=self.revision, root=self.root)
        structure, params, _ = load_revision(outcome.revision, root=self.root)
        with self.assertRaises(json_pointer.JsonPointerError):
            json_pointer.get(params, FILLET)
        self.assertFalse(any(s["path"] == FILLET for s in structure["param_specs"]))

    def test_design_patch_without_revision_raises(self) -> None:
        action = prescribe.patch(PatchOp(target=PatchTarget.DESIGN_GRAPH, path=FILLET, value=1.0))
        with self.assertRaises(PrescriptionError):
            apply_action(action, root=self.root)


class TestModelRegistryPatches(PrescribeBase):
    def test_restoring_a_term_produces_a_new_chain(self) -> None:
        ablated = commit_chain(
            ablate(full_chain(), "analytic_acoustic_bearing", "coherence_loss"), root=self.root
        )
        action = prescribe.patch(
            PatchOp(
                target=PatchTarget.MODEL_REGISTRY,
                path="analytic_acoustic_bearing:coherence_loss",
                op="add",
            )
        )
        outcome = apply_action(action, models_revision=ablated, root=self.root)
        self.assertTrue(outcome.applied)
        self.assertNotEqual(outcome.models_revision, ablated)
        chain = load_chain(outcome.models_revision, root=self.root)
        self.assertIn("coherence_loss", chain.terms_for("analytic_acoustic_bearing"))

    def test_uncalibrated_term_carries_a_calibration_requirement(self) -> None:
        action = prescribe.patch(
            PatchOp(
                target=PatchTarget.MODEL_REGISTRY,
                path="analytic_acoustic_bearing:coherence_loss",
                op="add",
            )
        )
        outcome = apply_action(
            action, models_revision=commit_chain(ModelChain(), root=self.root), root=self.root
        )
        self.assertEqual(outcome.calibration_required, ["analytic_acoustic_bearing:coherence_loss"])

    def test_calibrated_term_needs_no_requirement(self) -> None:
        action = prescribe.patch(
            PatchOp(target=PatchTarget.MODEL_REGISTRY, path="analytic_latch:bending", op="add")
        )
        outcome = apply_action(
            action, models_revision=commit_chain(ModelChain(), root=self.root), root=self.root
        )
        self.assertEqual(outcome.calibration_required, [])

    def test_bad_registry_path_rejected(self) -> None:
        for bad in ("no_colon", "analytic_latch:ghost", "ghost_stage:bending"):
            with self.assertRaises(PrescriptionError):
                apply_action(
                    prescribe.patch(PatchOp(target=PatchTarget.MODEL_REGISTRY, path=bad, op="add")),
                    models_revision=commit_chain(ModelChain(), root=self.root),
                    root=self.root,
                )


class TestNonPatchActions(PrescribeBase):
    def test_non_patch_actions_change_nothing(self) -> None:
        for action in (
            prescribe.no_action(),
            prescribe.escalate("ambiguous_requirement"),
            prescribe.request_measurement({"what": "coherence"}),
        ):
            outcome = apply_action(action, revision=self.revision, root=self.root)
            self.assertFalse(outcome.applied)
            self.assertEqual(outcome.revision, self.revision)


if __name__ == "__main__":
    unittest.main()
