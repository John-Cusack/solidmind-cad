"""Orchestrator state machine — deterministic transitions with reason codes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestrator.spec import FailureCode, SpecStatus

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transition rules
# ---------------------------------------------------------------------------

# Valid transitions: current_state → set of allowed next states
_TRANSITIONS: dict[SpecStatus, set[SpecStatus]] = {
    SpecStatus.DRAFT: {SpecStatus.NORMALIZING, SpecStatus.FAILED},
    SpecStatus.NORMALIZING: {SpecStatus.COUNCIL_REVIEW, SpecStatus.FAILED},
    SpecStatus.COUNCIL_REVIEW: {SpecStatus.LAYOUT_FROZEN, SpecStatus.DRAFT, SpecStatus.FAILED},
    SpecStatus.LAYOUT_FROZEN: {
        SpecStatus.INTERFACES_FROZEN,
        SpecStatus.COUNCIL_REVIEW,
        SpecStatus.FAILED,
    },
    SpecStatus.INTERFACES_FROZEN: {
        SpecStatus.BUILDING,
        SpecStatus.LAYOUT_FROZEN,
        SpecStatus.FAILED,
    },
    SpecStatus.BUILDING: {SpecStatus.GEOMETRY_VALIDATING, SpecStatus.BUILDING, SpecStatus.FAILED},
    SpecStatus.GEOMETRY_VALIDATING: {
        SpecStatus.SCORING,
        SpecStatus.BUILDING,
        SpecStatus.COUNCIL_REVIEW,
        SpecStatus.FAILED,
    },
    SpecStatus.SCORING: {
        SpecStatus.RELEASE_PACKAGING,
        SpecStatus.COUNCIL_REVIEW,
        SpecStatus.BUILDING,
        SpecStatus.FAILED,
    },
    SpecStatus.RELEASE_PACKAGING: {
        SpecStatus.AWAITING_HUMAN,
        SpecStatus.INTERFACES_FROZEN,
        SpecStatus.GEOMETRY_VALIDATING,
        SpecStatus.SCORING,
        SpecStatus.FAILED,
    },
    SpecStatus.AWAITING_HUMAN: {SpecStatus.DONE, SpecStatus.COUNCIL_REVIEW, SpecStatus.FAILED},
    SpecStatus.DONE: set(),
    SpecStatus.FAILED: set(),
}

# Where each failure code routes to for retry
_FAILURE_RETRY_TARGET: dict[FailureCode, SpecStatus] = {
    FailureCode.WORKER_TIMEOUT: SpecStatus.BUILDING,
    FailureCode.WORKER_TOOL_ERROR: SpecStatus.BUILDING,
    FailureCode.MISSING_ARTIFACT: SpecStatus.BUILDING,
    FailureCode.MANIFEST_HASH_MISMATCH: SpecStatus.BUILDING,
    FailureCode.INTERFACE_DIM_MISMATCH: SpecStatus.BUILDING,
    FailureCode.MEASUREMENT_DRIFT: SpecStatus.BUILDING,
    FailureCode.CLEARANCE_COLLISION: SpecStatus.BUILDING,
    FailureCode.MASS_OVER_BUDGET: SpecStatus.BUILDING,
    FailureCode.ENVELOPE_VIOLATION: SpecStatus.COUNCIL_REVIEW,
    FailureCode.ME_CHECK_FAIL: SpecStatus.BUILDING,
    FailureCode.OBJECTIVE_THRESHOLD: SpecStatus.COUNCIL_REVIEW,
    FailureCode.BUDGET_EXCEEDED: SpecStatus.FAILED,
    FailureCode.SKELETON_CONFLICT: SpecStatus.LAYOUT_FROZEN,
    FailureCode.ICD_INCOMPLETE: SpecStatus.INTERFACES_FROZEN,
    FailureCode.ASSEMBLY_ACCESS_FAIL: SpecStatus.LAYOUT_FROZEN,
}


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StateEvent:
    """A single state transition in the run history."""

    timestamp: str
    from_state: str
    to_state: str
    reason: str = ""
    failure_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StateMachine:
    """Tracks orchestrator run state with validated transitions."""

    current: SpecStatus = SpecStatus.DRAFT
    history: list[StateEvent] = field(default_factory=list)
    retry_counts: dict[str, int] = field(default_factory=dict)  # "subsystem:failure_code" → count

    def transition(
        self,
        to: SpecStatus,
        *,
        reason: str = "",
        failure_code: FailureCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Attempt a state transition. Raises ValueError if invalid."""
        allowed = _TRANSITIONS.get(self.current, set())
        if to not in allowed:
            raise ValueError(
                f"Invalid transition: {self.current.value} → {to.value}. "
                f"Allowed: {sorted(s.value for s in allowed)}"
            )

        event = StateEvent(
            timestamp=datetime.now(UTC).isoformat(),
            from_state=self.current.value,
            to_state=to.value,
            reason=reason,
            failure_code=failure_code.value if failure_code else None,
            details=details or {},
        )
        self.history.append(event)
        log.info(
            "State: %s → %s (reason=%s, code=%s)",
            self.current.value,
            to.value,
            reason,
            failure_code,
        )
        self.current = to

    def record_failure(
        self,
        failure_code: FailureCode,
        subsystem: str,
        max_retries: int,
        *,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> SpecStatus:
        """Record a failure and determine the next state based on retry budget.

        Returns the state that was transitioned to.
        """
        key = f"{subsystem}:{failure_code.value}"
        count = self.retry_counts.get(key, 0) + 1
        self.retry_counts[key] = count

        if failure_code == FailureCode.BUDGET_EXCEEDED:
            # Always terminal
            self.transition(
                SpecStatus.FAILED,
                reason=reason or f"Budget exceeded for {subsystem}",
                failure_code=failure_code,
                details=details,
            )
            return SpecStatus.FAILED

        retry_target = _FAILURE_RETRY_TARGET.get(failure_code, SpecStatus.FAILED)

        if count > max_retries:
            log.warning(
                "Retry budget exhausted for %s (%d/%d). Failing.",
                key,
                count,
                max_retries,
            )
            self.transition(
                SpecStatus.FAILED,
                reason=f"Retry budget exhausted: {key} ({count}/{max_retries})",
                failure_code=failure_code,
                details=details,
            )
            return SpecStatus.FAILED

        log.info("Retry %d/%d for %s → routing to %s", count, max_retries, key, retry_target.value)
        self.transition(
            retry_target,
            reason=reason or f"Retry {count}/{max_retries} for {subsystem}",
            failure_code=failure_code,
            details={**(details or {}), "retry": count, "max_retries": max_retries},
        )
        return retry_target

    def retry_count(self, subsystem: str, failure_code: FailureCode) -> int:
        return self.retry_counts.get(f"{subsystem}:{failure_code.value}", 0)
