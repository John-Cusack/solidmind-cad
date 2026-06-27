"""Tests for orchestrator.state — state machine transitions with v3 states."""

from __future__ import annotations

import unittest

from orchestrator.spec import FailureCode, SpecStatus
from orchestrator.state import _FAILURE_RETRY_TARGET, _TRANSITIONS, StateMachine


class TestTransitionTable(unittest.TestCase):
    """Verify the transition table covers all states."""

    def test_all_statuses_in_transitions(self) -> None:
        for status in SpecStatus:
            self.assertIn(status, _TRANSITIONS, f"{status} missing from _TRANSITIONS")

    def test_new_states_present(self) -> None:
        self.assertIn(SpecStatus.NORMALIZING, _TRANSITIONS)
        self.assertIn(SpecStatus.LAYOUT_FROZEN, _TRANSITIONS)
        self.assertIn(SpecStatus.RELEASE_PACKAGING, _TRANSITIONS)

    def test_terminal_states_have_no_exits(self) -> None:
        self.assertEqual(_TRANSITIONS[SpecStatus.DONE], set())
        self.assertEqual(_TRANSITIONS[SpecStatus.FAILED], set())

    def test_every_failure_code_has_retry_target(self) -> None:
        for code in FailureCode:
            self.assertIn(
                code,
                _FAILURE_RETRY_TARGET,
                f"FailureCode.{code.name} missing from _FAILURE_RETRY_TARGET",
            )

    def test_every_retry_target_is_valid_state(self) -> None:
        for code, target in _FAILURE_RETRY_TARGET.items():
            self.assertIsInstance(
                target, SpecStatus, f"Retry target for {code} is not a SpecStatus"
            )


class TestNewTransitions(unittest.TestCase):
    """Test the new v3 transition paths."""

    def test_draft_to_normalizing(self) -> None:
        sm = StateMachine()
        sm.transition(SpecStatus.NORMALIZING, reason="start")
        self.assertEqual(sm.current, SpecStatus.NORMALIZING)

    def test_normalizing_to_council(self) -> None:
        sm = StateMachine(current=SpecStatus.NORMALIZING)
        sm.transition(SpecStatus.COUNCIL_REVIEW, reason="normalized")
        self.assertEqual(sm.current, SpecStatus.COUNCIL_REVIEW)

    def test_council_to_layout_frozen(self) -> None:
        sm = StateMachine(current=SpecStatus.COUNCIL_REVIEW)
        sm.transition(SpecStatus.LAYOUT_FROZEN, reason="skeleton approved")
        self.assertEqual(sm.current, SpecStatus.LAYOUT_FROZEN)

    def test_layout_frozen_to_interfaces_frozen(self) -> None:
        sm = StateMachine(current=SpecStatus.LAYOUT_FROZEN)
        sm.transition(SpecStatus.INTERFACES_FROZEN, reason="ICDs complete")
        self.assertEqual(sm.current, SpecStatus.INTERFACES_FROZEN)

    def test_scoring_to_release_packaging(self) -> None:
        sm = StateMachine(current=SpecStatus.SCORING)
        sm.transition(SpecStatus.RELEASE_PACKAGING, reason="thresholds met")
        self.assertEqual(sm.current, SpecStatus.RELEASE_PACKAGING)

    def test_release_packaging_to_awaiting_human(self) -> None:
        sm = StateMachine(current=SpecStatus.RELEASE_PACKAGING)
        sm.transition(SpecStatus.AWAITING_HUMAN, reason="package ready")
        self.assertEqual(sm.current, SpecStatus.AWAITING_HUMAN)

    def test_full_happy_path(self) -> None:
        """Walk through all stages in the happy path."""
        sm = StateMachine()
        path = [
            SpecStatus.NORMALIZING,
            SpecStatus.COUNCIL_REVIEW,
            SpecStatus.LAYOUT_FROZEN,
            SpecStatus.INTERFACES_FROZEN,
            SpecStatus.BUILDING,
            SpecStatus.GEOMETRY_VALIDATING,
            SpecStatus.SCORING,
            SpecStatus.RELEASE_PACKAGING,
            SpecStatus.AWAITING_HUMAN,
            SpecStatus.DONE,
        ]
        for state in path:
            sm.transition(state, reason=f"→ {state.value}")
        self.assertEqual(sm.current, SpecStatus.DONE)
        self.assertEqual(len(sm.history), len(path))


class TestInvalidTransitions(unittest.TestCase):
    def test_cannot_skip_normalizing(self) -> None:
        sm = StateMachine()
        with self.assertRaises(ValueError):
            sm.transition(SpecStatus.COUNCIL_REVIEW)

    def test_cannot_go_from_done(self) -> None:
        sm = StateMachine(current=SpecStatus.DONE)
        with self.assertRaises(ValueError):
            sm.transition(SpecStatus.DRAFT)

    def test_cannot_skip_layout_frozen(self) -> None:
        sm = StateMachine(current=SpecStatus.COUNCIL_REVIEW)
        with self.assertRaises(ValueError):
            sm.transition(SpecStatus.INTERFACES_FROZEN)


class TestFailureRouting(unittest.TestCase):
    def test_skeleton_conflict_routes_to_layout_frozen(self) -> None:
        # SKELETON_CONFLICT routes to LAYOUT_FROZEN.
        # Test from INTERFACES_FROZEN which can reach LAYOUT_FROZEN.
        sm = StateMachine(current=SpecStatus.INTERFACES_FROZEN)
        target = sm.record_failure(
            FailureCode.SKELETON_CONFLICT,
            subsystem="housing",
            max_retries=2,
        )
        self.assertEqual(target, SpecStatus.LAYOUT_FROZEN)

    def test_icd_incomplete_routes_to_interfaces_frozen(self) -> None:
        self.assertEqual(
            _FAILURE_RETRY_TARGET[FailureCode.ICD_INCOMPLETE],
            SpecStatus.INTERFACES_FROZEN,
        )

    def test_assembly_access_fail_routes_to_layout_frozen(self) -> None:
        self.assertEqual(
            _FAILURE_RETRY_TARGET[FailureCode.ASSEMBLY_ACCESS_FAIL],
            SpecStatus.LAYOUT_FROZEN,
        )

    def test_budget_exceeded_always_terminal(self) -> None:
        sm = StateMachine(current=SpecStatus.BUILDING)
        target = sm.record_failure(
            FailureCode.BUDGET_EXCEEDED,
            subsystem="all",
            max_retries=99,
        )
        self.assertEqual(target, SpecStatus.FAILED)
        self.assertEqual(sm.current, SpecStatus.FAILED)

    def test_retry_exhaustion(self) -> None:
        sm = StateMachine(current=SpecStatus.BUILDING)
        # First retry succeeds in routing
        target = sm.record_failure(
            FailureCode.WORKER_TIMEOUT,
            subsystem="gear",
            max_retries=1,
        )
        self.assertEqual(target, SpecStatus.BUILDING)

        # Second retry exhausts budget → FAILED
        target = sm.record_failure(
            FailureCode.WORKER_TIMEOUT,
            subsystem="gear",
            max_retries=1,
        )
        self.assertEqual(target, SpecStatus.FAILED)


class TestFailureRetryBackwardCompat(unittest.TestCase):
    """Ensure all v2 failure codes still have retry targets."""

    def test_v2_codes(self) -> None:
        v2_codes = [
            FailureCode.WORKER_TIMEOUT,
            FailureCode.WORKER_TOOL_ERROR,
            FailureCode.MISSING_ARTIFACT,
            FailureCode.MANIFEST_HASH_MISMATCH,
            FailureCode.INTERFACE_DIM_MISMATCH,
            FailureCode.CLEARANCE_COLLISION,
            FailureCode.ENVELOPE_VIOLATION,
            FailureCode.ME_CHECK_FAIL,
            FailureCode.OBJECTIVE_THRESHOLD,
            FailureCode.BUDGET_EXCEEDED,
        ]
        for code in v2_codes:
            self.assertIn(code, _FAILURE_RETRY_TARGET)


if __name__ == "__main__":
    unittest.main()
