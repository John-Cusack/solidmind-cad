"""Result types for a TCK run.

A conformance run is not pass/fail — it is per-check, per-tier, with skips
that mean "the engine never claimed this".  An engine that implements only the
required floor should finish with a clean report, not a wall of failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    INFO = "info"


@dataclass(slots=True)
class Check:
    """One assertion about the engine."""

    name: str
    outcome: Outcome
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "outcome": self.outcome.value, "detail": self.detail}


@dataclass(slots=True)
class TierResult:
    """All checks in one tier."""

    tier: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, outcome: Outcome, detail: str = "") -> Check:
        check = Check(name=name, outcome=outcome, detail=detail)
        self.checks.append(check)
        return check

    def ok(self, name: str, detail: str = "") -> Check:
        return self.add(name, Outcome.PASS, detail)

    def fail(self, name: str, detail: str) -> Check:
        return self.add(name, Outcome.FAIL, detail)

    def skip(self, name: str, detail: str) -> Check:
        return self.add(name, Outcome.SKIP, detail)

    def info(self, name: str, detail: str) -> Check:
        return self.add(name, Outcome.INFO, detail)

    def expect(
        self,
        name: str,
        condition: bool,
        detail: str = "",
        *,
        pass_detail: str = "",
    ) -> Check:
        """Record *name* as pass/fail on *condition*.

        ``detail`` explains a failure; a passing check stays quiet unless
        ``pass_detail`` says otherwise, so a clean report reads as a list of
        claims rather than a list of near-misses.
        """
        if condition:
            return self.ok(name, pass_detail)
        return self.fail(name, detail)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.outcome is Outcome.FAIL]

    @property
    def passed(self) -> bool:
        return not self.failures

    def counts(self) -> dict[str, int]:
        counts = {outcome.value: 0 for outcome in Outcome}
        for check in self.checks:
            counts[check.outcome.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "passed": self.passed,
            "counts": self.counts(),
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(slots=True)
class TckReport:
    """Everything one run learned about one engine."""

    engine: str = "unknown"
    engine_version: str = ""
    address: str = ""
    tiers: list[TierResult] = field(default_factory=list)

    def tier(self, name: str) -> TierResult:
        result = TierResult(tier=name)
        self.tiers.append(result)
        return result

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.tiers)

    @property
    def failures(self) -> list[tuple[str, Check]]:
        return [(t.tier, c) for t in self.tiers for c in t.failures]

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "address": self.address,
            "passed": self.passed,
            "tiers": [t.to_dict() for t in self.tiers],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def render(self) -> str:
        """Human-readable summary — what gets pasted into a bug report."""
        symbols = {
            Outcome.PASS: "PASS",
            Outcome.FAIL: "FAIL",
            Outcome.SKIP: "skip",
            Outcome.INFO: "info",
        }
        lines = [
            f"Engine Integration Contract TCK — {self.engine} {self.engine_version}".rstrip(),
            f"  at {self.address}",
            "",
        ]
        for tier in self.tiers:
            counts = tier.counts()
            status = "PASSED" if tier.passed else "FAILED"
            lines.append(
                f"[{status}] {tier.tier}  "
                f"({counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped)"
            )
            for check in tier.checks:
                detail = f" — {check.detail}" if check.detail else ""
                lines.append(f"    {symbols[check.outcome]}  {check.name}{detail}")
            lines.append("")

        lines.append("RESULT: " + ("conformant" if self.passed else "NOT conformant"))
        if not self.passed:
            lines.append("")
            lines.append("Failures:")
            for tier_name, check in self.failures:
                lines.append(f"  {tier_name} / {check.name}: {check.detail}")
        return "\n".join(lines)
