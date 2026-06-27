"""Gear train ratio solver.

Finds tooth count combinations that produce a target total ratio while
respecting module range and spatial constraints.  Works for any multi-stage
gear train — watch going trains, gearboxes, reduction drives, etc.
"""

from __future__ import annotations

import itertools
import math
from typing import Any


def _find_train_combinations(
    total_ratio: float,
    num_stages: int,
    min_pinion: int,
    max_pinion: int,
    min_wheel: int = 20,
    max_wheel: int = 200,
    tolerance: float = 0.001,
    max_results: int = 20,
) -> list[list[tuple[int, int]]]:
    """Find tooth count combinations that produce the target total ratio.

    Each stage is (wheel_teeth, pinion_teeth). The product of all
    wheel/pinion ratios must equal total_ratio within tolerance.
    """
    results: list[list[tuple[int, int]]] = []
    pinion_range = range(min_pinion, max_pinion + 1)
    wheel_range = range(min_wheel, max_wheel + 1)

    stages_iter = itertools.product(
        *(itertools.product(wheel_range, pinion_range) for _ in range(num_stages))
    )
    for combo in stages_iter:
        ratio = 1.0
        for w, p in combo:
            ratio *= w / p
        if abs(ratio - total_ratio) / total_ratio < tolerance:
            results.append(list(combo))
            if len(results) >= max_results:
                return results
    return results


def _score_combination(
    combo: list[tuple[int, int]],
    module_range: tuple[float, float],
    max_diameter: float,
) -> float:
    """Score a train combination. Lower is better."""
    score = 0.0
    module_mid = (module_range[0] + module_range[1]) / 2.0

    for wheel, pinion in combo:
        # Prefer balanced ratios per stage
        stage_ratio = wheel / pinion
        if 3.0 <= stage_ratio <= 10.0:
            score -= 3.0
        elif stage_ratio > 15.0:
            score += 10.0

        # Prefer common pinion counts
        if pinion in (8, 10, 12):
            score -= 2.0

        # Check spatial fit
        pd_wheel = module_mid * wheel
        if max_diameter > 0 and pd_wheel > max_diameter * 0.8:
            score += 20.0

    return score


def gear_train_solver(
    total_ratio: float,
    num_stages: int = 3,
    module_range: tuple[float, float] = (0.5, 2.0),
    max_diameter: float = 0.0,
    min_pinion_teeth: int = 7,
    max_pinion_teeth: int = 15,
    min_wheel_teeth: int = 20,
    max_wheel_teeth: int = 120,
    tolerance: float = 0.001,
) -> dict[str, Any]:
    """Solve for gear train tooth counts achieving a target ratio.

    Parameters
    ----------
    total_ratio : float
        Desired overall gear ratio (output_speed / input_speed).
    num_stages : int
        Number of gear stages (2-4).
    module_range : tuple
        (min_module, max_module) in mm.
    max_diameter : float
        Maximum allowable wheel pitch diameter (0 = no limit).
    min_pinion_teeth, max_pinion_teeth : int
        Range of acceptable pinion tooth counts.
    min_wheel_teeth, max_wheel_teeth : int
        Range of acceptable wheel tooth counts.
    tolerance : float
        Acceptable ratio error as a fraction (0.001 = 0.1%).

    Returns
    -------
    dict with stages, actual ratio, ratio error, bore positions, and build hint.
    """
    if num_stages < 2 or num_stages > 4:
        raise ValueError(f"num_stages must be 2-4, got {num_stages}")
    if total_ratio <= 0:
        raise ValueError(f"total_ratio must be positive, got {total_ratio}")

    combos = _find_train_combinations(
        total_ratio,
        num_stages,
        min_pinion_teeth,
        max_pinion_teeth,
        min_wheel_teeth,
        max_wheel_teeth,
        tolerance,
    )

    if not combos:
        return {
            "ok": False,
            "error": {
                "code": "NO_SOLUTION",
                "message": (
                    f"No tooth count combination found for ratio {total_ratio:.4f} "
                    f"with {num_stages} stages in the given ranges."
                ),
            },
        }

    best = min(combos, key=lambda c: _score_combination(c, module_range, max_diameter))
    module_mid = (module_range[0] + module_range[1]) / 2.0

    stages = []
    actual_ratio = 1.0
    for i, (wheel, pinion) in enumerate(best):
        ratio = wheel / pinion
        actual_ratio *= ratio
        pd_wheel = module_mid * wheel
        pd_pinion = module_mid * pinion
        center_dist = (pd_wheel + pd_pinion) / 2.0

        stages.append(
            {
                "stage": i + 1,
                "wheel_teeth": wheel,
                "pinion_teeth": pinion,
                "ratio": round(ratio, 4),
                "module": module_mid,
                "wheel_pitch_d": round(pd_wheel, 3),
                "pinion_pitch_d": round(pd_pinion, 3),
                "center_distance": round(center_dist, 3),
            }
        )

    ratio_error = (actual_ratio - total_ratio) / total_ratio

    # Suggest bore positions (radial layout)
    bore_positions = [{"name": "input", "x": 0.0, "y": 0.0}]
    for i, s in enumerate(stages):
        angle = (i + 1) * (2 * math.pi / (num_stages + 1))
        dist = s["center_distance"]
        bore_positions.append(
            {
                "name": f"stage_{i + 1}",
                "x": round(dist * math.cos(angle), 3),
                "y": round(dist * math.sin(angle), 3),
            }
        )

    return {
        "ok": True,
        "stages": stages,
        "total_ratio": round(actual_ratio, 4),
        "target_ratio": round(total_ratio, 4),
        "ratio_error_pct": round(ratio_error * 100, 3),
        "num_stages": num_stages,
        "bore_positions": bore_positions,
        "build_hint": (
            "Use bore_positions for bearing/shaft holes. "
            "Build each wheel with geometry.spur_gear or geometry.tooth_slot + polar_pattern. "
            "For low tooth count pinions (< 20), use geometry.epicycloidal_tooth_slot."
        ),
    }
