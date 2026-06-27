"""Analytical structural screening (Tier 1) — gate FEA behind cheap mechanics.

The ``motion.*`` pipeline has an analytical → kinematic → dynamic tier ladder;
this is the structural analogue's first rung. Pure functions only: beam bending
(``sigma = M c / I``), a handbook stress-concentration-factor lookup, and an
Euler buckling bound. The Screen step of the inner loop calls
:func:`screen_stress` to answer "is this obviously bad, and do I even need FEA?"
without ever invoking gmsh or CalculiX.

SCF tables are coarse handbook approximations (Peterson / Roark ranges) intended
for screening, not final sizing — a WARN result means "run the real solver".
"""

from __future__ import annotations

import math
from typing import Any

from server.analysis_models import (
    AnalysisCheck,
    CheckStatus,
    FailureMode,
    ReflectExpectations,
)
from server.section_properties import compute_section

# Per-mode remediation text, built once (read by screen_stress and reused by
# decide.from_failure via AnalysisCheck.suggestion).
_SUGGESTIONS: dict[FailureMode, str] = {
    FailureMode.STRESS_CONCENTRATION: "add or enlarge the fillet/round at the hotspot to lower Kt",
    FailureMode.YIELD: "increase the section (thicker wall / deeper beam) or lower the load",
    FailureMode.BUCKLING: "increase second moment of area or shorten the unsupported length",
}

# --- Stress-concentration-factor lookup tables -----------------------------
# Each table maps a geometric ratio to Kt, sorted ascending by ratio. Values
# bracket published Peterson curves for the stated feature/loading; linear
# interpolation between rows, clamped at the ends.

# Shoulder fillet in bending, keyed by r/d (fillet radius / smaller width).
_FILLET_KT_BENDING: tuple[tuple[float, float], ...] = (
    (0.02, 2.80),
    (0.05, 2.20),
    (0.10, 1.85),
    (0.15, 1.62),
    (0.20, 1.48),
    (0.30, 1.32),
    (0.50, 1.18),
)
# Transverse circular hole in a plate (tension), keyed by d/w (hole / width).
_HOLE_KT_TENSION: tuple[tuple[float, float], ...] = (
    (0.00, 3.00),
    (0.10, 3.03),
    (0.20, 3.14),
    (0.30, 3.36),
    (0.40, 3.74),
    (0.50, 4.32),
)
# U-notch in bending, keyed by r/d (notch radius / net width).
_NOTCH_KT_BENDING: tuple[tuple[float, float], ...] = (
    (0.02, 3.00),
    (0.05, 2.50),
    (0.10, 2.10),
    (0.20, 1.70),
    (0.30, 1.50),
    (0.50, 1.30),
)

_SCF_TABLES = {
    "fillet": _FILLET_KT_BENDING,
    "hole": _HOLE_KT_TENSION,
    "notch": _NOTCH_KT_BENDING,
}

# Sharp re-entrant corner (zero radius): screening cap. Real Kt is unbounded;
# 3.0 is enough to drive any sane section below target FoS, flagging it.
_SHARP_CORNER_KT = 3.0


def _interp(table: tuple[tuple[float, float], ...], x: float) -> float:
    if x <= table[0][0]:
        return table[0][1]
    if x >= table[-1][0]:
        return table[-1][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:], strict=False):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return table[-1][1]  # unreachable given the clamps above


def stress_concentration_factor(feature: str, ratio: float) -> float:
    """Handbook Kt for a geometric feature at a given ratio.

    ``feature`` is one of "fillet", "hole", "notch"; ``ratio`` is r/d for
    fillet/notch or d/w for a hole. A non-positive ratio (sharp corner) returns
    a conservative screening cap.
    """
    key = feature.lower()
    if key not in _SCF_TABLES:
        raise ValueError(f"unknown SCF feature {feature!r}; expected one of {sorted(_SCF_TABLES)}")
    if ratio <= 0.0:
        return _SHARP_CORNER_KT
    return _interp(_SCF_TABLES[key], ratio)


def beam_bending_stress_mpa(moment_nmm: float, c_mm: float, i_mm4: float) -> float:
    """Nominal bending stress ``sigma = M c / I`` (N·mm, mm, mm^4 → MPa)."""
    if i_mm4 <= 0.0:
        raise ValueError("section second moment of area must be positive")
    return moment_nmm * c_mm / i_mm4


def euler_buckling_load_n(
    youngs_mpa: float, i_mm4: float, length_mm: float, end_fixity: float = 1.0
) -> float:
    """Euler critical load ``P_cr = pi^2 E I / (K L)^2`` (MPa, mm^4, mm → N)."""
    if length_mm <= 0.0 or end_fixity <= 0.0:
        raise ValueError("length and end_fixity must be positive")
    kl = end_fixity * length_mm
    return math.pi**2 * youngs_mpa * i_mm4 / (kl**2)


def _section_i_c(section: dict[str, Any]) -> tuple[float, float]:
    """Resolve a section dict to (I_mm4, c_mm).

    Either pass ``{i_mm4, c_mm}`` directly, or a ``{type, <dims>_mm}`` shape that
    is forwarded to :func:`server.section_properties.compute_section` (so every
    shape it supports — rectangle, circle, hollow_circle, i_beam, c_channel,
    angle, t_section, polygon — works here, with no duplicated formulas). The
    extreme-fibre distance is ``c = Ixx / Sx``.
    """
    if "i_mm4" in section and "c_mm" in section:
        return float(section["i_mm4"]), float(section["c_mm"])
    stype = section.get("type", "rectangle").lower()
    params = {(k[:-3] if k.endswith("_mm") else k): v for k, v in section.items() if k != "type"}
    try:
        props = compute_section(stype, **params)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"invalid section {section!r}: {exc}") from exc
    ixx, sx = props["Ixx"], props["Sx"]
    if ixx <= 0.0 or sx <= 0.0:
        raise ValueError(f"degenerate section {section!r} (Ixx={ixx}, Sx={sx})")
    return ixx, ixx / sx


def _bending_moment_nmm(load: dict[str, Any]) -> float:
    """Resolve a load dict to a bending moment (N·mm)."""
    if "moment_nmm" in load:
        return float(load["moment_nmm"])
    if "force_n" in load and "length_mm" in load:
        # Cantilever tip load: M = F · L.
        return float(load["force_n"]) * float(load["length_mm"])
    raise ValueError("load needs either moment_nmm or force_n + length_mm")


def screen_stress(
    *,
    section: dict[str, Any],
    load: dict[str, Any],
    yield_strength_mpa: float,
    youngs_modulus_mpa: float = 0.0,
    stress_concentration: dict[str, Any] | None = None,
    buckling: dict[str, Any] | None = None,
    target_fos: float = 2.0,
    name: str = "analytical stress screen",
    expectations: ReflectExpectations | None = None,
) -> AnalysisCheck:
    """Analytical first-pass structural screen → an :class:`AnalysisCheck`.

    Computes nominal bending stress, applies an optional stress-concentration
    factor, and (optionally) checks an Euler buckling bound. Status is FAIL when
    factor-of-safety < 1, WARN when below ``target_fos`` (run FEA to confirm),
    PASS otherwise. No solver is invoked.
    """
    if yield_strength_mpa <= 0.0:
        raise ValueError("yield_strength_mpa must be positive")

    i_mm4, c_mm = _section_i_c(section)
    moment_nmm = _bending_moment_nmm(load)
    sigma_nom = beam_bending_stress_mpa(moment_nmm, c_mm, i_mm4)

    kt = 1.0
    if stress_concentration is not None:
        kt = stress_concentration_factor(
            stress_concentration["feature"],
            float(stress_concentration.get("ratio", 0.0)),
        )
    peak_stress = sigma_nom * kt
    bending_fos = yield_strength_mpa / peak_stress if peak_stress > 0 else math.inf

    # Optional buckling check on a compressive member.
    buckling_fos = math.inf
    p_cr_n = math.inf
    if buckling is not None:
        if youngs_modulus_mpa <= 0.0:
            raise ValueError("youngs_modulus_mpa required for a buckling check")
        p_cr_n = euler_buckling_load_n(
            youngs_modulus_mpa,
            i_mm4,
            float(buckling["length_mm"]),
            float(buckling.get("end_fixity", 1.0)),
        )
        p_applied = float(buckling.get("compressive_force_n", 0.0))
        buckling_fos = p_cr_n / p_applied if p_applied > 0 else math.inf

    # Governing mode is whichever yields the lower factor of safety.
    if buckling_fos < bending_fos:
        fos = buckling_fos
        mode = FailureMode.BUCKLING
        measured, limit = float(buckling.get("compressive_force_n", 0.0)), p_cr_n
    else:
        fos = bending_fos
        mode = (
            FailureMode.STRESS_CONCENTRATION
            if stress_concentration is not None and kt > 1.0
            else FailureMode.YIELD
        )
        measured, limit = peak_stress, yield_strength_mpa

    if fos < 1.0:
        status = CheckStatus.FAIL
    elif fos < target_fos:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.PASS

    suggestion = _SUGGESTIONS[mode]

    msg = (
        f"sigma_nom={sigma_nom:.1f} MPa, Kt={kt:.2f}, peak={peak_stress:.1f} MPa, "
        f"FoS={fos:.2f} vs target {target_fos:.1f}"
    )
    if mode is FailureMode.BUCKLING:
        msg = f"P_cr={p_cr_n:.1f} N, buckling FoS={fos:.2f} vs target {target_fos:.1f}"

    if expectations is not None:
        lo, hi = expectations.expected_peak_stress_mpa
        if not (lo <= peak_stress <= hi) and mode is not FailureMode.BUCKLING:
            msg += f" — peak outside expected band {lo:.0f}–{hi:.0f} MPa"

    return AnalysisCheck(
        name=name,
        status=status,
        message=msg,
        measured=round(measured, 3),
        limit=round(limit, 3),
        suggestion=suggestion,
        failure_mode=mode,
    )
