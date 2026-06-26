"""Decide + Interpret helpers for the inner loop.

- :func:`interpret_compare_to_expectations` (Interpret step) compares a real
  result against the Reflect-step expectations.
- :func:`from_failure` (Decide step) turns a failing :class:`AnalysisCheck`
  into a concrete, typed fix proposal keyed on its ``FailureMode``.

Both are pure and self-contained so the orchestration loop (and the foam-dart
example's ``iterate_until_pass``) can call them without a solver.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.analysis_models import (
    AnalysisCheck,
    FailureMode,
    FieldResult,
    ReflectExpectations,
)


@dataclass(frozen=True, slots=True)
class FixProposal:
    """A concrete geometry change that addresses a failure mechanism."""
    op: str          # cad.* operation, e.g. "add_fillet", "thicken_wall"
    target: str      # feature / face group the op applies to
    param: str       # parameter to change (e.g. "radius_mm")
    delta: float     # signed change to apply
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "target": self.target,
            "param": self.param,
            "delta": self.delta,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixProposal:
        return cls(
            op=d["op"],
            target=d["target"],
            param=d["param"],
            delta=d["delta"],
            rationale=d["rationale"],
        )


@dataclass(frozen=True, slots=True)
class Comparison:
    """Outcome of comparing a result against pre-sim expectations."""
    hotspot_matches_expectation: bool
    peak_within_expected_band: bool
    observed_failure_mode: FailureMode | None
    expected_failure_modes: tuple[FailureMode, ...]
    mode_was_expected: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hotspot_matches_expectation": self.hotspot_matches_expectation,
            "peak_within_expected_band": self.peak_within_expected_band,
            "observed_failure_mode": (
                self.observed_failure_mode.value if self.observed_failure_mode else None
            ),
            "expected_failure_modes": [m.value for m in self.expected_failure_modes],
            "mode_was_expected": self.mode_was_expected,
            "message": self.message,
        }


def interpret_compare_to_expectations(
    result: FieldResult, expectations: ReflectExpectations
) -> Comparison:
    """Compare a solved result against the Reflect-step expectations.

    Surfaces three signals: did the hotspot land where expected, was the peak
    stress in the predicted band, and was the failure mode one we were checking
    for. A surprise on any of these is what the loop should learn from.
    """
    hotspot = expectations.expected_hotspot.lower()
    hotspot_match = any(
        hotspot in (c.face_group or "").lower() or hotspot in (c.name or "").lower()
        for c in result.checks
    )

    lo, hi = expectations.expected_peak_stress_mpa
    within_band = lo <= result.max_von_mises_mpa <= hi

    observed = result.failure_mode
    mode_expected = (
        observed in expectations.failure_modes_to_check if observed is not None else False
    )

    notes: list[str] = []
    notes.append("hotspot as expected" if hotspot_match else "hotspot NOT where expected")
    notes.append(
        f"peak {result.max_von_mises_mpa:.1f} MPa "
        + ("within" if within_band else "outside")
        + f" band {lo:.0f}-{hi:.0f}"
    )
    if observed is not None:
        notes.append(
            f"mode {observed.value} "
            + ("was" if mode_expected else "was NOT")
            + " in the checklist"
        )

    return Comparison(
        hotspot_matches_expectation=hotspot_match,
        peak_within_expected_band=within_band,
        observed_failure_mode=observed,
        expected_failure_modes=tuple(expectations.failure_modes_to_check),
        mode_was_expected=mode_expected,
        message="; ".join(notes),
    )


# Mechanism → fix mapping. Each entry is (op, param, delta, rationale-template).
_FIX_BY_MODE: dict[FailureMode, tuple[str, str, float, str]] = {
    FailureMode.STRESS_CONCENTRATION: (
        "add_fillet", "radius_mm", 0.5,
        "add or enlarge the root fillet at {target} to cut the stress-concentration factor",
    ),
    FailureMode.YIELD: (
        "thicken_wall", "wall_mm", 1.0,
        "increase the section thickness at {target} to lower peak stress",
    ),
    FailureMode.BUCKLING: (
        "increase_section", "section_mm", 1.0,
        "increase second moment of area (or shorten the unsupported length) at {target}",
    ),
    FailureMode.DEFLECTION: (
        "increase_section", "section_mm", 1.0,
        "stiffen the section at {target} to reduce deflection",
    ),
    FailureMode.FATIGUE: (
        "add_fillet", "radius_mm", 0.5,
        "smooth the transition at {target} to raise fatigue life",
    ),
}


def from_failure(check: AnalysisCheck) -> FixProposal | None:
    """Turn a failing check into a concrete fix, dispatched on its FailureMode.

    Returns ``None`` when there is no typed failure mode to act on. Unmapped
    modes get a conservative "manual review" proposal rather than a silent drop.
    """
    mode = check.failure_mode
    if mode is None:
        return None
    target = check.face_group or check.name or "hotspot"
    mapped = _FIX_BY_MODE.get(mode)
    if mapped is None:
        return FixProposal(
            op="review",
            target=target,
            param="",
            delta=0.0,
            rationale=f"no automated fix mapped for {mode.value}; manual review at {target}",
        )
    op, param, delta, template = mapped
    return FixProposal(
        op=op, target=target, param=param, delta=delta,
        rationale=template.format(target=target),
    )
