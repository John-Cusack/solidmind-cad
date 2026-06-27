"""Calibration-first physics for the foam-dart spring launcher.

Transparent and honest by design. The chain is:

    E_spring = 1/2 k x^2                 (energy stored by compressing the spring)
    E_dart   = efficiency * E_spring     (fraction that reaches the dart)
    v        = sqrt(2 E_dart / m_dart)   (muzzle velocity)
    range    = projectile_range(v, angle, launch_height)

The single lumped ``efficiency`` absorbs spring mass, plunger friction, the air
column ahead of the dart, and barrel losses. So a predicted-vs-measured gap is a
*calibration* result, not a model failure. The defensible claim is the
*relationship*: with efficiency fixed, ``v ∝ x`` and (no-drag limit)
``range ∝ x^2``. Calibrate efficiency from one measured shot, then predict the
others and report relative error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

GRAVITY_M_S2 = 9.80665


@dataclass(frozen=True, slots=True)
class LauncherSpec:
    """Physical configuration of one launcher + dart.

    ``efficiency`` is a placeholder until calibrated from a measured shot — the
    README tells the user to treat it (and the spring constant) as values to
    measure, not truths.
    """

    spring_k_n_per_m: float
    dart_mass_kg: float
    launch_angle_deg: float = 12.0
    launch_height_m: float = 0.15
    efficiency: float = 0.45
    gravity_m_s2: float = GRAVITY_M_S2

    def validated(self) -> LauncherSpec:
        if self.spring_k_n_per_m <= 0.0:
            raise ValueError("spring_k_n_per_m must be positive")
        if self.dart_mass_kg <= 0.0:
            raise ValueError("dart_mass_kg must be positive")
        if not (0.0 < self.efficiency <= 1.0):
            raise ValueError("efficiency must be in (0, 1]")
        if not (0.0 <= self.launch_angle_deg <= 90.0):
            raise ValueError("launch_angle_deg must be in [0, 90]")
        if self.launch_height_m < 0.0:
            raise ValueError("launch_height_m must be non-negative")
        return self


def spring_energy_j(spring_k_n_per_m: float, pullback_m: float) -> float:
    """Stored spring energy ``E = 1/2 k x^2`` (J)."""
    if spring_k_n_per_m <= 0.0:
        raise ValueError("spring_k_n_per_m must be positive")
    if pullback_m < 0.0:
        raise ValueError("pullback_m must be non-negative")
    return 0.5 * spring_k_n_per_m * pullback_m**2


def muzzle_velocity_m_s(spec: LauncherSpec, pullback_m: float) -> float:
    """Muzzle velocity from the energy chain (m/s)."""
    spec.validated()
    e_dart = spec.efficiency * spring_energy_j(spec.spring_k_n_per_m, pullback_m)
    return math.sqrt(2.0 * e_dart / spec.dart_mass_kg)


def projectile_range_m(
    velocity_m_s: float,
    launch_angle_deg: float,
    launch_height_m: float,
    gravity_m_s2: float = GRAVITY_M_S2,
) -> float:
    """No-drag projectile range from a launch height (m).

    Solves ``y(t) = h + v sinθ t - 1/2 g t^2 = 0`` for the positive root and
    returns the horizontal distance ``v cosθ t``.
    """
    if velocity_m_s < 0.0:
        raise ValueError("velocity must be non-negative")
    theta = math.radians(launch_angle_deg)
    vy = velocity_m_s * math.sin(theta)
    vx = velocity_m_s * math.cos(theta)
    # Time to return to ground (y = 0) from height h.
    t_flight = (vy + math.sqrt(vy * vy + 2.0 * gravity_m_s2 * launch_height_m)) / gravity_m_s2
    return vx * t_flight


def predicted_range_m(spec: LauncherSpec, pullback_m: float) -> float:
    """Predicted range for a pullback at the spec's current efficiency (m)."""
    v = muzzle_velocity_m_s(spec, pullback_m)
    return projectile_range_m(v, spec.launch_angle_deg, spec.launch_height_m, spec.gravity_m_s2)


def _velocity_for_range(
    target_range_m: float,
    launch_angle_deg: float,
    launch_height_m: float,
    gravity_m_s2: float = GRAVITY_M_S2,
) -> float:
    """Invert :func:`projectile_range_m` for muzzle velocity (bisection).

    Range is monotonic increasing in velocity, so bisection converges cleanly.
    """
    if target_range_m <= 0.0:
        return 0.0
    lo, hi = 0.0, 1.0
    # Expand the upper bound until it overshoots the target.
    while projectile_range_m(hi, launch_angle_deg, launch_height_m, gravity_m_s2) < target_range_m:
        hi *= 2.0
        if hi > 1.0e6:
            raise ValueError("target range not reachable")
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        r = projectile_range_m(mid, launch_angle_deg, launch_height_m, gravity_m_s2)
        if r < target_range_m:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def calibrate_efficiency(spec: LauncherSpec, pullback_m: float, measured_range_m: float) -> float:
    """Fit ``efficiency`` from a single measured shot.

    Inverts the measured range for the required muzzle velocity, then solves the
    energy chain for efficiency: ``efficiency = m v^2 / (k x^2)``. Raises if the
    implied efficiency falls outside (0, 1] — that means the spring constant or
    dart mass is wrong, which is exactly the kind of disagreement the example
    exists to surface.
    """
    if pullback_m <= 0.0:
        raise ValueError("pullback_m must be positive to calibrate")
    if measured_range_m <= 0.0:
        raise ValueError("measured_range_m must be positive")
    v_required = _velocity_for_range(
        measured_range_m, spec.launch_angle_deg, spec.launch_height_m, spec.gravity_m_s2
    )
    eff = spec.dart_mass_kg * v_required**2 / (spec.spring_k_n_per_m * pullback_m**2)
    if not (0.0 < eff <= 1.0):
        raise ValueError(
            f"calibrated efficiency {eff:.3f} outside (0, 1] — check the spring "
            f"constant ({spec.spring_k_n_per_m} N/m) and dart mass "
            f"({spec.dart_mass_kg} kg); one of them disagrees with the measured shot"
        )
    return eff


def calibrate_from_shot(
    spec: LauncherSpec, pullback_mm: float, measured_range_m: float
) -> LauncherSpec:
    """Return a new spec whose efficiency is fitted to the measured shot."""
    eff = calibrate_efficiency(spec, pullback_mm / 1000.0, measured_range_m)
    return LauncherSpec(
        spring_k_n_per_m=spec.spring_k_n_per_m,
        dart_mass_kg=spec.dart_mass_kg,
        launch_angle_deg=spec.launch_angle_deg,
        launch_height_m=spec.launch_height_m,
        efficiency=eff,
        gravity_m_s2=spec.gravity_m_s2,
    )


def predict_table(spec: LauncherSpec, pullbacks_mm: list[float]) -> list[dict[str, Any]]:
    """Predicted muzzle velocity + range for each pullback setting."""
    rows: list[dict[str, Any]] = []
    for x_mm in pullbacks_mm:
        x_m = x_mm / 1000.0
        v = muzzle_velocity_m_s(spec, x_m)
        rows.append(
            {
                "pullback_mm": x_mm,
                "muzzle_velocity_m_s": round(v, 4),
                "predicted_range_m": round(predicted_range_m(spec, x_m), 4),
            }
        )
    return rows
