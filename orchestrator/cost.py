"""Cost tracking and budget enforcement for orchestrator runs."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.spec import CostPolicy

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CostEntry:
    """A single cost event."""

    stage: str
    subsystem: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    description: str = ""


@dataclass(slots=True)
class CostTracker:
    """Tracks cumulative cost across an orchestrator run."""

    policy: CostPolicy = field(default_factory=CostPolicy)
    entries: list[CostEntry] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    @property
    def stage_costs(self) -> dict[str, float]:
        costs: dict[str, float] = {}
        for e in self.entries:
            costs[e.stage] = costs.get(e.stage, 0.0) + e.cost_usd
        return costs

    def record(self, entry: CostEntry) -> None:
        """Record a cost event and check budgets."""
        self.entries.append(entry)
        log.debug(
            "Cost: +$%.4f (%s/%s) total=$%.4f",
            entry.cost_usd, entry.stage, entry.subsystem, self.total_cost_usd,
        )

    def check_budget(self) -> tuple[bool, list[str]]:
        """Check if cost is within budget.

        Returns (ok, issues). Issues include warnings at warn_at_pct
        and hard failures at budget limits.
        """
        issues: list[str] = []
        total = self.total_cost_usd
        max_run = self.policy.max_run_cost_usd

        # Hard limit
        if total > max_run:
            issues.append(
                f"Run cost ${total:.2f} exceeds budget ${max_run:.2f}"
            )
            return False, issues

        # Warning threshold
        warn_threshold = max_run * self.policy.warn_at_pct / 100.0
        if total > warn_threshold:
            issues.append(
                f"Run cost ${total:.2f} exceeds {self.policy.warn_at_pct}% "
                f"warning threshold (${warn_threshold:.2f})"
            )

        # Per-stage limits
        for stage, cost in self.stage_costs.items():
            if cost > self.policy.max_stage_cost_usd:
                issues.append(
                    f"Stage '{stage}' cost ${cost:.2f} exceeds "
                    f"stage budget ${self.policy.max_stage_cost_usd:.2f}"
                )
                return False, issues

        return True, issues

    def check_can_proceed(self, estimated_cost_usd: float = 0.0) -> tuple[bool, str]:
        """Check if there's enough budget remaining for an estimated cost."""
        remaining = self.policy.max_run_cost_usd - self.total_cost_usd
        if estimated_cost_usd > remaining:
            return False, (
                f"Estimated cost ${estimated_cost_usd:.2f} exceeds "
                f"remaining budget ${remaining:.2f}"
            )
        return True, f"${remaining:.2f} remaining"

    def save(self, path: Path) -> None:
        """Save cost log to JSON."""
        data = {
            "total_cost_usd": self.total_cost_usd,
            "policy": {
                "max_run_cost_usd": self.policy.max_run_cost_usd,
                "max_stage_cost_usd": self.policy.max_stage_cost_usd,
                "warn_at_pct": self.policy.warn_at_pct,
            },
            "stage_costs": self.stage_costs,
            "entries": [
                {
                    "stage": e.stage,
                    "subsystem": e.subsystem,
                    "provider": e.provider,
                    "prompt_tokens": e.prompt_tokens,
                    "completion_tokens": e.completion_tokens,
                    "cost_usd": e.cost_usd,
                    "description": e.description,
                }
                for e in self.entries
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path, policy: CostPolicy | None = None) -> CostTracker:
        """Load cost log from JSON."""
        data = json.loads(path.read_text())
        tracker = cls(policy=policy or CostPolicy())
        for ed in data.get("entries", []):
            tracker.entries.append(CostEntry(
                stage=ed.get("stage", ""),
                subsystem=ed.get("subsystem", ""),
                provider=ed.get("provider", ""),
                prompt_tokens=ed.get("prompt_tokens", 0),
                completion_tokens=ed.get("completion_tokens", 0),
                cost_usd=ed.get("cost_usd", 0.0),
                description=ed.get("description", ""),
            ))
        return tracker


# ---------------------------------------------------------------------------
# Token → cost estimation
# ---------------------------------------------------------------------------

# Approximate pricing per 1M tokens (input/output)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
}


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate USD cost from token counts and model name."""
    input_rate, output_rate = _PRICING.get(model, (3.0, 15.0))
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000
