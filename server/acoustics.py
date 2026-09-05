"""Analytic outdoor-acoustic bearing estimation — pure functions, screening grade.

The design-loop claim this implements: bearing error for a time-difference-of-
arrival array can be computed analytically from spectrum, SNR, coherence and
integration time, so waveform synthesis is not needed to iterate on a design.

Three pieces, in the order they bite:

1. **Knapp & Carter delay variance** (1976) — the Cramer-Rao bound for TDOA
   with the maximum-likelihood weighting::

       var(tau) = 1 / (8 pi^2 T INT f^2 [g/(1-g)] df),   g = |gamma(f)|^2

   The ``f^2`` weighting is why bandwidth at high frequency buys accuracy, and
   the ``g/(1-g)`` term is why coherence, not level, is the binding constraint.

2. **Turbulence-induced coherence loss.** Wind does three things to an outdoor
   array: local pressure fluctuations at the microphone (a noise term),
   refraction (shadow zones and ducting), and *decorrelation of the wavefront
   between microphones*. Only the third degrades the cross-correlation peak
   while leaving level intact, which is exactly the failure an SNR-only model
   reports as success. Transverse coherence over separation ``d`` is modelled
   with the Kolmogorov 5/3 law::

       gamma_turb(d) = exp(-(d / rho_0)^(5/3)),  rho_0 = (1.46 k^2 L Cn2)^(-3/5)

   These are **literature parameters, not calibrated** — the registry marks
   the term ``calibrated=False`` and the measurement campaign is what anchors
   it. Treat absolute numbers as screening-grade.

3. **Threshold / ambiguity.** The CRLB is a *local* bound: it is valid only
   above the SNR-coherence threshold where the correlation peak is
   unambiguous. Below it, estimators pick the wrong peak and the error is set
   by the a-priori delay spread, not by the bound — so a CRLB-only model is
   systematically optimistic exactly at the coverage boundary a design loop
   spends its time on. ``effective_tdoa_variance`` blends the bound with the
   ambiguity-limited variance in the Ziv-Zakai manner::

       var_eff = (1 - Pa) var_crlb + Pa var_prior

All formulas are screening-grade approximations with explicit assumptions:
stationarity over the integration window, Gaussian statistics, a single
dominant propagation path, and equal per-channel SNR.
"""

from __future__ import annotations

import math

SPEED_OF_SOUND_M_S = 343.0

# Coherence is clamped below 1 so the g/(1-g) weighting stays finite.
_MAX_COHERENCE2 = 1.0 - 1e-9
# Guard for the degenerate zero-information case.
_MAX_VARIANCE_S2 = 1.0


def snr_coherence2(snr_linear: float) -> float:
    """Coherence ceiling set by uncorrelated noise, equal SNR in both channels."""
    if snr_linear <= 0.0:
        return 0.0
    return (snr_linear / (1.0 + snr_linear)) ** 2


def coherence_length_m(
    freq_hz: float, path_m: float, cn2: float, *, c: float = SPEED_OF_SOUND_M_S
) -> float:
    """Transverse coherence length rho_0 for a spherical wave in turbulence.

    ``cn2`` is the effective acoustic index-of-refraction structure parameter
    (m^-2/3): ~1e-8 calm, ~1e-6 moderate convective, ~1e-5 strong.
    """
    if freq_hz <= 0.0 or path_m <= 0.0 or cn2 <= 0.0:
        return math.inf
    k = 2.0 * math.pi * freq_hz / c
    return (1.46 * k * k * path_m * cn2) ** (-0.6)


def turbulence_coherence2(
    freq_hz: float,
    separation_m: float,
    path_m: float,
    cn2: float,
    *,
    c: float = SPEED_OF_SOUND_M_S,
) -> float:
    """|gamma|^2 retained across ``separation_m`` after ``path_m`` of turbulence."""
    if separation_m <= 0.0:
        return 1.0
    rho0 = coherence_length_m(freq_hz, path_m, cn2, c=c)
    if not math.isfinite(rho0):
        return 1.0
    exponent = 2.0 * (separation_m / rho0) ** (5.0 / 3.0)
    if exponent > 700.0:  # exp underflow guard
        return 0.0
    return math.exp(-exponent)


def band_coherence2(
    freq_hz: float,
    *,
    snr_linear: float,
    separation_m: float,
    path_m: float,
    cn2: float,
    include_turbulence: bool = True,
    c: float = SPEED_OF_SOUND_M_S,
) -> float:
    """Total coherence at one frequency: noise-limited x turbulence-limited."""
    g = snr_coherence2(snr_linear)
    if include_turbulence:
        g *= turbulence_coherence2(freq_hz, separation_m, path_m, cn2, c=c)
    return min(g, _MAX_COHERENCE2)


def tdoa_variance_s2(
    *,
    f_lo_hz: float,
    f_hi_hz: float,
    integration_time_s: float,
    coherence2_at: callable[[float], float],
    n_points: int = 65,
) -> float:
    """Knapp-Carter delay variance over a band (trapezoid over ``n_points``).

    ``coherence2_at`` maps frequency to |gamma|^2. Deterministic: the same
    inputs always integrate over the same abscissae.
    """
    if f_hi_hz <= f_lo_hz or integration_time_s <= 0.0:
        raise ValueError("need f_hi > f_lo > 0 and a positive integration time")
    if n_points < 3 or n_points % 2 == 0:
        raise ValueError("n_points must be an odd integer >= 3")

    step = (f_hi_hz - f_lo_hz) / (n_points - 1)
    total = 0.0
    for i in range(n_points):
        f = f_lo_hz + i * step
        g = min(max(coherence2_at(f), 0.0), _MAX_COHERENCE2)
        weight = f * f * (g / (1.0 - g))
        total += weight * (0.5 if i in (0, n_points - 1) else 1.0)
    integral = total * step
    if integral <= 0.0:
        return _MAX_VARIANCE_S2
    var = 1.0 / (8.0 * math.pi**2 * integration_time_s * integral)
    return min(var, _MAX_VARIANCE_S2)


def correlator_output_snr(
    *, bandwidth_hz: float, integration_time_s: float, coherence2: float
) -> float:
    """Peak SNR after coherent integration (time-bandwidth processing gain)."""
    g = min(max(coherence2, 0.0), _MAX_COHERENCE2)
    if g <= 0.0:
        return 0.0
    return 2.0 * bandwidth_hz * integration_time_s * (g / (1.0 - g))


def anomaly_probability(
    *,
    bandwidth_hz: float,
    integration_time_s: float,
    coherence2: float,
    delay_spread_s: float,
) -> float:
    """P(the correlator locks onto the wrong peak) — union bound over cells.

    The number of resolvable delay cells is ``delay_spread * bandwidth``; each
    is a chance for noise to beat the true peak. This is the term the CRLB
    cannot see and the reason a bound-only model over-promises at the coverage
    boundary.
    """
    rho = correlator_output_snr(
        bandwidth_hz=bandwidth_hz,
        integration_time_s=integration_time_s,
        coherence2=coherence2,
    )
    cells = max(delay_spread_s * bandwidth_hz, 1.0)
    if rho <= 0.0:
        return 1.0
    tail = 0.5 * math.erfc(math.sqrt(rho / 4.0))
    return min(1.0, max(0.0, cells * tail))


def effective_tdoa_variance(
    *, crlb_variance_s2: float, anomaly_prob: float, delay_spread_s: float
) -> float:
    """Ziv-Zakai style blend of the local bound and the ambiguity-limited case."""
    prior_var = (delay_spread_s**2) / 12.0
    p = min(max(anomaly_prob, 0.0), 1.0)
    return (1.0 - p) * crlb_variance_s2 + p * prior_var


def bearing_sigma_rad(
    *,
    tdoa_sigma_s: float,
    aperture_m: float,
    bearing_rad: float = 0.0,
    c: float = SPEED_OF_SOUND_M_S,
) -> float:
    """Bearing error from delay error: tau = (d/c) sin(theta)."""
    if aperture_m <= 0.0:
        raise ValueError("aperture must be positive")
    cos_theta = math.cos(bearing_rad)
    if abs(cos_theta) < 1e-6:  # endfire: bearing is unobservable
        return math.pi / 2.0
    sigma = tdoa_sigma_s * c / (aperture_m * abs(cos_theta))
    return min(sigma, math.pi / 2.0)


def fuse_bearings(
    nodes: list[tuple[float, float]],
    target: tuple[float, float],
    sigma_theta_rad: list[float],
) -> tuple[float, float]:
    """Triangulate bearings from nodes; return (position_rms_m, gdop).

    Each node contributes information along the cross-range direction only,
    with standard deviation ``R_i * sigma_theta_i``. The 2x2 Fisher matrices
    are inverted in closed form.

    ``gdop`` is the standard bearings-only geometric dilution: the same
    inversion with *unit* angular variance, so it measures geometry alone
    (units: metres per radian) and is independent of sensor quality. Because
    each node adds a positive-semidefinite term, adding a node can only
    improve it — which is what makes it a usable topology signal.
    """
    if len(nodes) != len(sigma_theta_rad):
        raise ValueError("one bearing sigma per node")
    if len(nodes) < 2:
        raise ValueError("triangulation needs at least two nodes")

    j11 = j12 = j22 = 0.0  # weighted by actual bearing sigmas
    g11 = g12 = g22 = 0.0  # unit angular variance: geometry only
    contributing = 0
    for (nx, ny), sigma in zip(nodes, sigma_theta_rad, strict=True):
        dx, dy = target[0] - nx, target[1] - ny
        rng = math.hypot(dx, dy)
        if rng <= 0.0 or sigma <= 0.0:
            continue
        # Unit vector perpendicular to the line of sight — the direction a
        # bearing error moves the estimate.
        ux, uy = -dy / rng, dx / rng
        contributing += 1
        wg = 1.0 / (rng * rng)
        g11 += wg * ux * ux
        g12 += wg * ux * uy
        g22 += wg * uy * uy
        w = wg / (sigma * sigma)
        j11 += w * ux * ux
        j12 += w * ux * uy
        j22 += w * uy * uy

    if contributing < 2:
        return math.inf, math.inf
    det = j11 * j22 - j12 * j12
    det_g = g11 * g22 - g12 * g12
    if det <= 0.0 or det_g <= 0.0:
        return math.inf, math.inf
    rms = math.sqrt((j11 + j22) / det)  # trace of a 2x2 inverse
    gdop = math.sqrt((g11 + g22) / det_g)
    return rms, gdop
