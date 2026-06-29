"""Analytical thermal screening (Tier 1) — gate thermal FEA behind cheap physics.

The structural analogue (:mod:`server.screen_stress`) answers "is this obviously
bad, and do I even need FEA?" for stress; this is the thermal rung of the same
ladder. Pure lumped-parameter heat transfer: a series conduction/convection
resistance network for the steady-state hot-spot temperature, and the Biot
number as the validity gate for the single-temperature (lumped) assumption.

The Screen step of the inner loop calls :func:`screen_thermal` to decide whether
a part runs hot *and* whether a full field solve is even warranted — without ever
invoking gmsh or Elmer.

Units are SI throughout (metres, watts, kelvin) because thermal inputs are
naturally SI, unlike the mm-based stress screen. A WARN result means the screen
is not definitive — either the part is marginal, or internal gradients are
significant (Biot > 0.1) and the lumped estimate can't be trusted, so run
``analysis.thermal_check`` (FEA).
"""

from __future__ import annotations

import math
from typing import Any

from server.analysis_models import AnalysisCheck, CheckStatus, FailureMode

# Above this Biot number the lumped (uniform-temperature) assumption breaks down
# and internal conduction gradients matter — the classic textbook threshold.
_BIOT_LUMPED_LIMIT = 0.1

_SUGGESTION_OVERTEMP = (
    "lower dissipated power, or raise convective rejection "
    "(more surface area / fins, higher airflow coefficient)"
)
_SUGGESTION_GRADIENT = (
    "internal gradients significant (Bi>0.1) — the lumped estimate is unreliable; "
    "run analysis.thermal_check (FEA) to resolve the temperature field"
)
_SUGGESTION_NO_LIMIT = (
    "supply max_temperature_k so the screen can judge the part — without a "
    "temperature limit it can only report the result, not pass/fail it"
)


def convection_resistance_kw(coeff_w_m2k: float, area_m2: float) -> float:
    """Convective thermal resistance ``R = 1 / (h A)`` (W/m^2K, m^2 → K/W)."""
    if coeff_w_m2k <= 0.0 or area_m2 <= 0.0:
        raise ValueError("convection coefficient and area must be positive")
    return 1.0 / (coeff_w_m2k * area_m2)


def conduction_resistance_kw(length_m: float, area_m2: float, conductivity_w_mk: float) -> float:
    """Conductive thermal resistance ``R = L / (k A)`` (m, m^2, W/mK → K/W)."""
    if length_m < 0.0 or area_m2 <= 0.0 or conductivity_w_mk <= 0.0:
        raise ValueError("conduction length>=0, area>0, conductivity>0 required")
    return length_m / (conductivity_w_mk * area_m2)


def biot_number(coeff_w_m2k: float, char_length_m: float, conductivity_w_mk: float) -> float:
    """Biot number ``Bi = h L_c / k_solid`` (dimensionless).

    ``char_length_m`` is the characteristic length (volume / surface area for a
    general body, or half-thickness for a slab). ``Bi < 0.1`` validates the
    lumped-capacitance / uniform-temperature assumption.
    """
    if char_length_m < 0.0 or conductivity_w_mk <= 0.0:
        raise ValueError("characteristic length>=0 and solid conductivity>0 required")
    return coeff_w_m2k * char_length_m / conductivity_w_mk


def convective_equilibrium_temp_k(
    power_w: float, coeff_w_m2k: float, area_m2: float, ambient_k: float
) -> float:
    """Steady convective surface temperature ``T = T_inf + Q / (h A)``."""
    return ambient_k + power_w * convection_resistance_kw(coeff_w_m2k, area_m2)


def lumped_time_constant_s(
    density_kg_m3: float,
    volume_m3: float,
    specific_heat_j_kgk: float,
    coeff_w_m2k: float,
    area_m2: float,
) -> float:
    """Lumped-capacitance time constant ``tau = rho V c / (h A)`` (seconds).

    The transient reaches ~63% of its total rise in one ``tau`` and is
    effectively steady after ~5 ``tau``. Only meaningful when the Biot number is
    below the lumped limit.
    """
    if density_kg_m3 <= 0.0 or volume_m3 <= 0.0 or specific_heat_j_kgk <= 0.0:
        raise ValueError("density, volume, and specific heat must be positive")
    return density_kg_m3 * volume_m3 * specific_heat_j_kgk / (coeff_w_m2k * area_m2)


def screen_thermal(
    *,
    power_w: float,
    convection: dict[str, Any],
    conduction: dict[str, Any] | None = None,
    biot: dict[str, Any] | None = None,
    transient: dict[str, Any] | None = None,
    max_temperature_k: float = 0.0,
    target_fos: float = 2.0,
    name: str = "analytical thermal screen",
) -> AnalysisCheck:
    """Analytical first-pass thermal screen → an :class:`AnalysisCheck`.

    Builds a series resistance network (optional internal conduction drop +
    convective rejection), computes the steady hot-spot temperature, and — when a
    ``biot`` block is supplied — checks whether the lumped assumption holds.

    Inputs (SI):

    - ``power_w`` — heat dissipated / applied to the part (W).
    - ``convection`` — ``{coeff_w_m2k, area_m2, ambient_k?}`` (ambient default
      293.15 K). The convective heat-rejection path to ambient.
    - ``conduction`` — optional ``{length_m, area_m2, conductivity_w_mk}`` internal
      conduction resistance in series before the convective surface (e.g. junction
      → case). Omit for a part cooled directly at its surface.
    - ``biot`` — optional ``{char_length_m, conductivity_w_mk}`` to gate the lumped
      assumption. ``Bi > 0.1`` forces a WARN ("run FEA").
    - ``transient`` — optional ``{density_kg_m3, volume_m3, specific_heat_j_kgk}``
      to report the lumped time constant in the message (informational).
    - ``max_temperature_k`` — temperature limit. ``0`` means "report only": the
      result can't be PASSed without a limit, so the status is WARN.

    Status: FAIL when the hot-spot exceeds the limit (FoS on temperature rise
    < 1), WARN when below ``target_fos``, when the Biot gate is violated, or when
    no limit was supplied, PASS only with a limit, clear margin, and a valid
    lumped assumption. No solver is invoked.
    """
    if power_w < 0.0:
        raise ValueError("power_w must be non-negative")

    ambient_k = float(convection.get("ambient_k", 293.15))
    r_conv = convection_resistance_kw(
        float(convection["coeff_w_m2k"]), float(convection["area_m2"])
    )

    r_cond = 0.0
    if conduction is not None:
        r_cond = conduction_resistance_kw(
            float(conduction["length_m"]),
            float(conduction["area_m2"]),
            float(conduction["conductivity_w_mk"]),
        )

    r_total = r_cond + r_conv
    hot_temp_k = ambient_k + power_w * r_total
    surface_temp_k = convective_equilibrium_temp_k(
        power_w, float(convection["coeff_w_m2k"]), float(convection["area_m2"]), ambient_k
    )
    temp_rise = hot_temp_k - ambient_k

    # Biot validity gate for the lumped assumption.
    bi = math.nan
    lumped_invalid = False
    if biot is not None:
        bi = biot_number(
            float(convection["coeff_w_m2k"]),
            float(biot["char_length_m"]),
            float(biot["conductivity_w_mk"]),
        )
        lumped_invalid = bi > _BIOT_LUMPED_LIMIT

    # Temperature factor of safety on the *rise* above ambient (the part of the
    # temperature the design controls). Only meaningful when a limit was given.
    temp_known = max_temperature_k > 0.0
    fos = math.inf
    if temp_known:
        allowable_rise = max_temperature_k - ambient_k
        if allowable_rise <= 0.0:
            raise ValueError("max_temperature_k must exceed ambient")
        fos = allowable_rise / temp_rise if temp_rise > 0.0 else math.inf

    # Never PASS without a temperature limit: with no limit the screen can't
    # certify the part is cool enough, only report the number and flag Biot. A
    # default-0 max_temperature_k must not read as "comfortably cool, skip FEA".
    if temp_known and fos < 1.0:
        status = CheckStatus.FAIL
    elif not temp_known:
        status = CheckStatus.WARN
    elif fos < target_fos or lumped_invalid:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.PASS

    # Suggestion follows the governing reason: a missing limit first, then an
    # actual/near over-temperature, then the gradient gate.
    if not temp_known:
        suggestion = _SUGGESTION_GRADIENT if lumped_invalid else _SUGGESTION_NO_LIMIT
    elif lumped_invalid and fos >= target_fos:
        suggestion = _SUGGESTION_GRADIENT
    else:
        suggestion = _SUGGESTION_OVERTEMP

    msg = f"T_hot={hot_temp_k:.1f} K ({hot_temp_k - 273.15:.1f} °C)"
    if r_cond > 0.0:
        msg += f", T_surface={surface_temp_k:.1f} K"
    msg += f", rise={temp_rise:.1f} K"
    if temp_known:
        msg += f", FoS={fos:.2f} vs target {target_fos:.1f} (limit {max_temperature_k:.1f} K)"
    else:
        msg += ", no temperature limit supplied (report only)"
    if biot is not None:
        verdict = "lumped invalid, run FEA" if lumped_invalid else "lumped valid"
        msg += f", Bi={bi:.3f} ({verdict})"
    if transient is not None:
        tau = lumped_time_constant_s(
            float(transient["density_kg_m3"]),
            float(transient["volume_m3"]),
            float(transient["specific_heat_j_kgk"]),
            float(convection["coeff_w_m2k"]),
            float(convection["area_m2"]),
        )
        msg += f", tau={tau:.1f} s (~{5 * tau:.0f} s to steady)"

    limit = max(max_temperature_k, 0.0)
    return AnalysisCheck(
        name=name,
        status=status,
        message=msg,
        measured=round(hot_temp_k, 3),
        limit=round(limit, 3),
        suggestion=suggestion,
        failure_mode=FailureMode.THERMAL,
    )
