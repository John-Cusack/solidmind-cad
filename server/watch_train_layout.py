"""Watch going train layout computation.

Computes gear ratios, tooth counts, modules, and pivot positions for a
complete going train from a target beat rate. Solves the constrained
combinatorial problem of finding tooth/pinion combinations that produce
the correct total ratio while respecting spatial constraints.
"""
from __future__ import annotations

import itertools
import math
from typing import Any


def _total_ratio_needed(
    beat_rate_bph: int,
    center_wheel_rev_per_hour: float,
    fourth_wheel_rev_per_min: float,
    escape_wheel_teeth: int,
) -> float:
    """Compute the total gear ratio from center wheel to escape wheel.

    The escape wheel turns at: beat_rate / (2 * escape_teeth) per unit time.
    The center wheel turns at center_wheel_rev_per_hour.
    """
    # Escape wheel rev/hour = beat_rate / (2 * escape_teeth)
    escape_rev_per_hour = beat_rate_bph / (2.0 * escape_wheel_teeth)
    # Total ratio = escape_rev_per_hour / center_wheel_rev_per_hour
    return escape_rev_per_hour / center_wheel_rev_per_hour


def _find_train_combinations(
    total_ratio: float,
    num_stages: int,
    min_pinion: int,
    max_pinion: int,
    min_wheel: int = 60,
    max_wheel: int = 100,
) -> list[list[tuple[int, int]]]:
    """Find tooth count combinations that produce the target total ratio.

    Each stage is (wheel_teeth, pinion_leaves). The product of all
    wheel/pinion ratios must equal total_ratio within 0.1%.

    Returns a list of valid combinations, each being a list of
    (wheel_teeth, pinion_leaves) tuples.
    """
    results = []
    pinion_range = range(min_pinion, max_pinion + 1)
    wheel_range = range(min_wheel, max_wheel + 1)

    if num_stages == 3:
        # 3-stage train: center→third, third→fourth, fourth→escape
        for combo in itertools.product(
            itertools.product(wheel_range, pinion_range),
            itertools.product(wheel_range, pinion_range),
            itertools.product(wheel_range, pinion_range),
        ):
            ratio = 1.0
            for w, p in combo:
                ratio *= w / p
            if abs(ratio - total_ratio) / total_ratio < 0.001:
                results.append(list(combo))
                if len(results) >= 20:
                    return results
    elif num_stages == 2:
        for combo in itertools.product(
            itertools.product(wheel_range, pinion_range),
            itertools.product(wheel_range, pinion_range),
        ):
            ratio = 1.0
            for w, p in combo:
                ratio *= w / p
            if abs(ratio - total_ratio) / total_ratio < 0.001:
                results.append(list(combo))
                if len(results) >= 20:
                    return results
    else:
        # 4-stage: rare but possible
        for combo in itertools.product(
            itertools.product(wheel_range, pinion_range),
            itertools.product(wheel_range, pinion_range),
            itertools.product(wheel_range, pinion_range),
            itertools.product(wheel_range, pinion_range),
        ):
            ratio = 1.0
            for w, p in combo:
                ratio *= w / p
            if abs(ratio - total_ratio) / total_ratio < 0.001:
                results.append(list(combo))
                if len(results) >= 20:
                    return results

    return results


def _score_combination(
    combo: list[tuple[int, int]],
    module_range: tuple[float, float],
    movement_diameter: float,
) -> float:
    """Score a train combination. Lower is better.

    Prefers: standard pinion counts (8, 10), moderate wheel sizes,
    modules in the middle of the range, fits within movement diameter.
    """
    score = 0.0
    module_mid = (module_range[0] + module_range[1]) / 2.0

    for wheel, pinion in combo:
        # Prefer standard pinion counts
        if pinion in (8, 10):
            score -= 5.0
        elif pinion in (7, 9, 12):
            score -= 2.0

        # Prefer moderate wheel sizes
        if 64 <= wheel <= 80:
            score -= 3.0
        elif wheel > 90:
            score += 5.0

        # Check if pitch diameters fit
        pd_wheel = module_mid * wheel
        pd_pinion = module_mid * pinion
        center_dist = (pd_wheel + pd_pinion) / 2.0
        if center_dist * 2 > movement_diameter * 0.8:
            score += 20.0  # Too big

    return score


def watch_gear_train_layout(
    beat_rate_bph: int = 21600,
    escape_wheel_teeth: int = 15,
    barrel_teeth: int = 80,
    module_range: tuple[float, float] = (0.08, 0.12),
    movement_diameter: float = 36.0,
    center_wheel_rev_per_hour: float = 1.0,
    fourth_wheel_rev_per_min: float = 1.0,
    preferred_num_stages: int = 3,
    min_pinion_leaves: int = 7,
    max_pinion_leaves: int = 12,
) -> dict[str, Any]:
    """Compute a complete going train layout.

    Returns per-stage tooth counts, modules, pitch diameters, center
    distances, and ratios. Also: total ratio, beat error, suggested bore
    positions, and interference check.
    """
    if beat_rate_bph not in (18000, 21600, 28800, 36000):
        raise ValueError(
            f"beat_rate_bph must be 18000/21600/28800/36000, got {beat_rate_bph}"
        )

    total_ratio = _total_ratio_needed(
        beat_rate_bph, center_wheel_rev_per_hour,
        fourth_wheel_rev_per_min, escape_wheel_teeth,
    )

    # For a standard going train (center→third→fourth→escape):
    # The fourth wheel turns at fourth_wheel_rev_per_min rev/min = 60 rev/hour
    # Center wheel turns at 1 rev/hour (carries minute hand)
    # So center→fourth ratio = 60
    # Fourth→escape ratio = total_ratio / 60
    # For 21600 bph, 15T escape: total = 21600/(2*15)/1 = 720
    # center→fourth = 60, fourth→escape = 12

    # Find combinations
    # For 3-stage: we're looking for center→third × third→fourth × fourth→escape = total_ratio
    # But actually we want center→third × third→fourth = 60 (center→fourth)
    # and fourth→escape = total_ratio / 60

    # Let's decompose: center→fourth ratio and fourth→escape ratio
    center_to_fourth_ratio = 60.0 / center_wheel_rev_per_hour  # typically 60
    fourth_to_escape_ratio = total_ratio / center_to_fourth_ratio

    stages = []
    module_mid = (module_range[0] + module_range[1]) / 2.0

    if preferred_num_stages == 3:
        # Stage 1: center wheel → third pinion
        # Stage 2: third wheel → fourth pinion
        # Stage 3: fourth wheel → escape pinion
        # Stage 1 × Stage 2 = center_to_fourth_ratio
        # Stage 3 = fourth_to_escape_ratio

        # Find stage 3 first (fourth → escape)
        best_stage3 = None
        best_err3 = float("inf")
        for w in range(60, 101):
            for p in range(min_pinion_leaves, max_pinion_leaves + 1):
                ratio = w / p
                err = abs(ratio - fourth_to_escape_ratio) / fourth_to_escape_ratio
                if err < best_err3:
                    best_err3 = err
                    best_stage3 = (w, p)

        # Find stages 1 and 2 that multiply to center_to_fourth_ratio
        best_s1s2 = None
        best_err12 = float("inf")
        for w1 in range(60, 101):
            for p1 in range(min_pinion_leaves, max_pinion_leaves + 1):
                r1 = w1 / p1
                for w2 in range(60, 101):
                    for p2 in range(min_pinion_leaves, max_pinion_leaves + 1):
                        r2 = w2 / p2
                        err = abs(r1 * r2 - center_to_fourth_ratio) / center_to_fourth_ratio
                        if err < best_err12:
                            best_err12 = err
                            best_s1s2 = ((w1, p1), (w2, p2))

        if best_s1s2 and best_stage3:
            actual_total = (best_s1s2[0][0] / best_s1s2[0][1]) * \
                           (best_s1s2[1][0] / best_s1s2[1][1]) * \
                           (best_stage3[0] / best_stage3[1])

            stage_names = [
                "center_to_third",
                "third_to_fourth",
                "fourth_to_escape",
            ]
            combos = [best_s1s2[0], best_s1s2[1], best_stage3]

            for _, ((wheel, pinion), name) in enumerate(zip(combos, stage_names, strict=False)):
                ratio = wheel / pinion
                pd_wheel = module_mid * wheel
                pd_pinion = module_mid * pinion
                center_dist = (pd_wheel + pd_pinion) / 2.0

                stages.append({
                    "name": name,
                    "wheel_teeth": wheel,
                    "pinion_leaves": pinion,
                    "ratio": round(ratio, 4),
                    "module": module_mid,
                    "wheel_pitch_d": round(pd_wheel, 3),
                    "pinion_pitch_d": round(pd_pinion, 3),
                    "center_distance": round(center_dist, 3),
                })
    else:
        # 2-stage (unusual) or 4-stage
        combos_found = _find_train_combinations(
            total_ratio, preferred_num_stages,
            min_pinion_leaves, max_pinion_leaves,
        )
        if combos_found:
            best = min(combos_found, key=lambda c: _score_combination(c, module_range, movement_diameter))
            actual_total = 1.0
            for i, (wheel, pinion) in enumerate(best):
                ratio = wheel / pinion
                actual_total *= ratio
                pd_wheel = module_mid * wheel
                pd_pinion = module_mid * pinion
                center_dist = (pd_wheel + pd_pinion) / 2.0
                stages.append({
                    "name": f"stage_{i+1}",
                    "wheel_teeth": wheel,
                    "pinion_leaves": pinion,
                    "ratio": round(ratio, 4),
                    "module": module_mid,
                    "wheel_pitch_d": round(pd_wheel, 3),
                    "pinion_pitch_d": round(pd_pinion, 3),
                    "center_distance": round(center_dist, 3),
                })

    # Compute actual total ratio and beat error
    actual_ratio = 1.0
    for s in stages:
        actual_ratio *= s["ratio"]

    actual_escape_rph = actual_ratio * center_wheel_rev_per_hour
    actual_bph = actual_escape_rph * 2.0 * escape_wheel_teeth
    beat_error = actual_bph - beat_rate_bph

    # Suggest bore positions (simple linear layout along movement diameter)
    bore_positions = []
    if stages:
        movement_diameter * 0.3 / len(stages)
        # Center wheel at origin
        x = 0.0
        bore_positions.append({"name": "center_wheel", "x": 0.0, "y": 0.0})
        for i, s in enumerate(stages):
            angle = (i + 1) * (2 * math.pi / (len(stages) + 1))
            dist = s["center_distance"]
            x = dist * math.cos(angle)
            y = dist * math.sin(angle)
            parts = s["name"].split("_to_")
            bore_name = parts[-1] if len(parts) > 1 else f"bore_{i+1}"
            bore_positions.append({"name": bore_name, "x": round(x, 3), "y": round(y, 3)})

    # Interference check
    interference_ok = True
    for _, s in enumerate(stages):
        if s["wheel_pitch_d"] > movement_diameter * 0.8:
            interference_ok = False

    return {
        "ok": True,
        "stages": stages,
        "total_ratio": round(actual_ratio, 4),
        "target_ratio": round(total_ratio, 4),
        "beat_rate_bph": beat_rate_bph,
        "actual_bph": round(actual_bph, 1),
        "beat_error_bph": round(beat_error, 1),
        "barrel_teeth": barrel_teeth,
        "escape_wheel_teeth": escape_wheel_teeth,
        "bore_positions": bore_positions,
        "interference_ok": interference_ok,
        "movement_diameter": movement_diameter,
        "build_hint": (
            "Use bore_positions to place jewel bearing holes on the main plate. "
            "Build each wheel with geometry.spur_gear or geometry.tooth_slot + polar_pattern. "
            "Build each pinion with geometry.watch_pinion_profile + polar_pattern."
        ),
    }
