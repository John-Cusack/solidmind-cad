"""Parameterized locomotion reward function.

Implements the Walk These Ways multiplicative composition:
``total = r_positive * exp(r_negative / temperature)``.

Reward weights are auto-scaled by robot mass, joint count, and
standing height from ``URDFAnalysis``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RewardTerm:
    """A single reward term with name, weight, and tracking sigma."""

    name: str
    weight: float
    sigma: float = 0.25


@dataclass(frozen=True, slots=True)
class RewardConfig:
    """Full reward configuration for locomotion training."""

    positive_terms: tuple[RewardTerm, ...] = (
        RewardTerm(name="track_lin_vel_xy", weight=1.0, sigma=0.25),
        RewardTerm(name="track_ang_vel_z", weight=0.5, sigma=0.25),
    )
    negative_terms: tuple[RewardTerm, ...] = (
        RewardTerm(name="lin_vel_z", weight=-2.0),
        RewardTerm(name="ang_vel_xy", weight=-0.05),
        RewardTerm(name="orientation", weight=-1.0),
        RewardTerm(name="joint_torques", weight=-1e-4),
        RewardTerm(name="action_rate", weight=-0.01),
    )
    temperature: float = 0.1


def scale_reward_config(
    base_config: RewardConfig,
    *,
    total_mass_kg: float,
    num_joints: int,
    standing_height_m: float,
) -> RewardConfig:
    """Auto-scale reward weights based on robot properties.

    Scaling heuristics:
    - ``joint_torques`` weight scales inversely with mass (heavier robots
      produce larger torques naturally).
    - ``action_rate`` scales inversely with joint count.
    - ``lin_vel_z`` and ``orientation`` scale with standing height
      (taller robots need tighter stabilization).
    """
    mass_scale = max(0.1, 1.0 / total_mass_kg) if total_mass_kg > 0 else 1.0
    joint_scale = max(0.1, 6.0 / num_joints) if num_joints > 0 else 1.0
    height_scale = max(0.5, standing_height_m / 0.125) if standing_height_m > 0 else 1.0

    scaled_negative: list[RewardTerm] = []
    for term in base_config.negative_terms:
        w = term.weight
        if term.name == "joint_torques":
            w *= mass_scale
        elif term.name == "action_rate":
            w *= joint_scale
        elif term.name in ("lin_vel_z", "orientation"):
            w *= height_scale
        scaled_negative.append(RewardTerm(
            name=term.name,
            weight=w,
            sigma=term.sigma,
        ))

    return RewardConfig(
        positive_terms=base_config.positive_terms,
        negative_terms=tuple(scaled_negative),
        temperature=base_config.temperature,
    )


def reward_config_to_dict(config: RewardConfig) -> dict[str, Any]:
    """Serialize reward config to a JSON-compatible dict."""
    return {
        "positive_terms": [
            {"name": t.name, "weight": t.weight, "sigma": t.sigma}
            for t in config.positive_terms
        ],
        "negative_terms": [
            {"name": t.name, "weight": t.weight, "sigma": t.sigma}
            for t in config.negative_terms
        ],
        "temperature": config.temperature,
    }
