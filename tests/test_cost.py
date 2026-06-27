"""Tests for orchestrator.cost."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.cost import (
    CostEntry,
    CostTracker,
    estimate_cost,
)
from orchestrator.spec import CostPolicy


class TestCostTracker(unittest.TestCase):
    def test_empty_tracker(self) -> None:
        tracker = CostTracker()
        self.assertEqual(tracker.total_cost_usd, 0.0)
        ok, issues = tracker.check_budget()
        self.assertTrue(ok)

    def test_record_and_total(self) -> None:
        tracker = CostTracker()
        tracker.record(CostEntry(stage="council", cost_usd=2.0))
        tracker.record(CostEntry(stage="building", cost_usd=3.0))
        self.assertAlmostEqual(tracker.total_cost_usd, 5.0)

    def test_stage_costs(self) -> None:
        tracker = CostTracker()
        tracker.record(CostEntry(stage="council", cost_usd=2.0))
        tracker.record(CostEntry(stage="council", cost_usd=1.0))
        tracker.record(CostEntry(stage="building", cost_usd=3.0))
        self.assertAlmostEqual(tracker.stage_costs["council"], 3.0)
        self.assertAlmostEqual(tracker.stage_costs["building"], 3.0)

    def test_budget_exceeded(self) -> None:
        policy = CostPolicy(max_run_cost_usd=5.0)
        tracker = CostTracker(policy=policy)
        tracker.record(CostEntry(stage="council", cost_usd=6.0))
        ok, issues = tracker.check_budget()
        self.assertFalse(ok)
        self.assertTrue(any("exceeds budget" in i for i in issues))

    def test_warning_threshold(self) -> None:
        policy = CostPolicy(max_run_cost_usd=10.0, warn_at_pct=80)
        tracker = CostTracker(policy=policy)
        tracker.record(CostEntry(stage="council", cost_usd=9.0))
        ok, issues = tracker.check_budget()
        self.assertTrue(ok)  # still within budget
        self.assertTrue(any("warning" in i.lower() for i in issues))

    def test_stage_budget_exceeded(self) -> None:
        policy = CostPolicy(max_run_cost_usd=100.0, max_stage_cost_usd=5.0)
        tracker = CostTracker(policy=policy)
        tracker.record(CostEntry(stage="building", cost_usd=6.0))
        ok, issues = tracker.check_budget()
        self.assertFalse(ok)
        self.assertTrue(any("stage" in i.lower() for i in issues))

    def test_can_proceed(self) -> None:
        policy = CostPolicy(max_run_cost_usd=10.0)
        tracker = CostTracker(policy=policy)
        tracker.record(CostEntry(stage="council", cost_usd=7.0))
        ok, msg = tracker.check_can_proceed(estimated_cost_usd=2.0)
        self.assertTrue(ok)
        ok, msg = tracker.check_can_proceed(estimated_cost_usd=5.0)
        self.assertFalse(ok)

    def test_json_round_trip(self) -> None:
        policy = CostPolicy(max_run_cost_usd=50.0)
        tracker = CostTracker(policy=policy)
        tracker.record(
            CostEntry(
                stage="council",
                subsystem="gear",
                provider="anthropic",
                cost_usd=2.5,
                prompt_tokens=1000,
                completion_tokens=500,
            )
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        try:
            tracker.save(path)
            loaded = CostTracker.load(path, policy=policy)
            self.assertAlmostEqual(loaded.total_cost_usd, 2.5)
            self.assertEqual(len(loaded.entries), 1)
            self.assertEqual(loaded.entries[0].stage, "council")
        finally:
            path.unlink(missing_ok=True)


class TestEstimateCost(unittest.TestCase):
    def test_sonnet_pricing(self) -> None:
        cost = estimate_cost("claude-sonnet-4-20250514", 1_000_000, 100_000)
        # $3/M input + $15/M output = $3 + $1.5 = $4.5
        self.assertAlmostEqual(cost, 4.5, places=1)

    def test_unknown_model_uses_default(self) -> None:
        cost = estimate_cost("unknown-model", 1_000_000, 100_000)
        # Default rates: $3/M + $15/M
        self.assertAlmostEqual(cost, 4.5, places=1)


if __name__ == "__main__":
    unittest.main()
