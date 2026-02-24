"""Solver adapters for parametric design studies.

Each solver implements the SolverAdapter ABC and is registered in SOLVERS.
Real BEMT+XFOIL and OpenFOAM implementations are stubs for now.

Geometry script contract
========================
Solvers that need 3D geometry (OpenFOAM, future FEA) use a **geometry script** —
a Python file that runs in FreeCAD headless mode (``FreeCADCmd``) and produces an STL.

The LLM writes this script during ``study.create`` and stores it at
``studies/<study_id>/geometry.py``. The script must:

1. Read a JSON file path from ``sys.argv[1]`` containing the variant params + fixed params.
2. Build 3D geometry using FreeCAD Python API (Part, PartDesign, Sketcher).
3. Export the result as STL to the path given in ``sys.argv[2]``.
4. Exit 0 on success, non-zero on failure.

Example geometry script::

    import json, sys
    import FreeCAD, Part

    with open(sys.argv[1]) as f:
        p = json.load(f)
    # p = {"angle": 15.0, "chord": 25.0, "blades": 3, ...}

    doc = FreeCAD.newDocument("variant")
    # ... build geometry from p ...
    Part.export([doc.getObject("Body")], sys.argv[2])
    sys.exit(0)

The solver calls: ``FreeCADCmd geometry.py /tmp/params.json /tmp/variant.stl``
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import struct
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("solidmind.study_solvers")


class SolverAdapter(ABC):
    """Base class for simulation solvers."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable solver name."""

    @abstractmethod
    def available(self) -> bool:
        """Check if the solver's dependencies are installed."""

    @abstractmethod
    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        """Estimated wall-clock seconds per variant. Used for time estimates."""

    @abstractmethod
    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        """Return a list of validation error strings (empty = ok)."""

    @abstractmethod
    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        """Run the solver and return metric name → value."""

    def describe_pipeline(self) -> str:
        """Human-readable description of what this solver does per variant."""
        return "run solver"


class MockSolver(SolverAdapter):
    """Deterministic mock solver for testing. Returns metrics based on params."""

    def name(self) -> str:
        return "mock"

    def available(self) -> bool:
        return True

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        return 0.01

    def describe_pipeline(self) -> str:
        return "evaluate analytical function"

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        return []

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        # Simple parabolic objective: maximize when all params near their midpoints
        total = 0.0
        for k, v in params.items():
            if isinstance(v, (int, float)):
                total += float(v)
        return {"objective": -((total - 50) ** 2) + 2500, "total_param": total}


# ---------------------------------------------------------------------------
# BEMT + XFOIL helpers
# ---------------------------------------------------------------------------

def _flat_plate_polar(alpha_deg: float, Re: float) -> tuple[float, float]:
    """Flat-plate aerodynamic model used as fallback when XFOIL fails.

    Returns (Cl, Cd).
    """
    alpha_rad = math.radians(alpha_deg)
    # Lift: thin-airfoil 2*pi*alpha with post-stall cosine decay
    cl_linear = 2.0 * math.pi * alpha_rad
    stall_deg = 12.0
    if abs(alpha_deg) > stall_deg:
        # Cosine decay beyond stall
        decay = math.cos(math.radians(abs(alpha_deg) - stall_deg))
        decay = max(decay, 0.0)
        cl = math.copysign(2.0 * math.pi * math.radians(stall_deg) * decay, alpha_deg)
    else:
        cl = cl_linear

    # Drag: turbulent flat-plate friction + induced drag
    cf = 0.074 / (max(Re, 1000.0) ** 0.2)  # Prandtl turbulent skin friction
    cd_induced = cl * cl / (math.pi * 6.0)  # AR ~ 6 approximation
    cd = cf + cd_induced

    return cl, cd


def _parse_xfoil_polar(polar_text: str) -> tuple[float, float] | None:
    """Parse XFOIL polar accumulation file, return last data line's (Cl, Cd).

    Returns None if no valid data found.
    """
    data_started = False
    last_cl: float | None = None
    last_cd: float | None = None

    for line in polar_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("------"):
            data_started = True
            continue
        if not data_started or not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        try:
            # Columns: alpha, CL, CD, CDp, CM, Top_Xtr, Bot_Xtr
            last_cl = float(parts[1])
            last_cd = float(parts[2])
        except (ValueError, IndexError):
            continue

    if last_cl is not None and last_cd is not None:
        return last_cl, last_cd
    return None


def _run_xfoil(
    airfoil: str,
    alpha_deg: float,
    Re: float,
    *,
    max_iter: int = 100,
    ncrit: float = 9.0,
    timeout_s: float = 10.0,
) -> tuple[float, float] | None:
    """Run XFOIL subprocess for a single angle of attack.

    *airfoil* is either a NACA designation (e.g. ``"NACA4412"``) or a path
    to a ``.dat`` coordinate file.

    Returns ``(Cl, Cd)`` on success, ``None`` on timeout or convergence failure.
    """
    if shutil.which("xfoil") is None:
        return None

    with tempfile.TemporaryDirectory(prefix="xfoil_") as td:
        polar_path = str(Path(td) / "polar.dat")

        # Build XFOIL command sequence
        cmds: list[str] = []
        if airfoil.upper().startswith("NACA"):
            cmds.append(airfoil)  # e.g. "NACA4412"
        else:
            cmds.append(f"LOAD {airfoil}")
            cmds.append("")  # accept default name

        cmds += [
            "OPER",
            f"VISC {Re:.0f}",
            f"ITER {max_iter}",
            f"VPAR",
            f"N {ncrit:.1f}",
            "",  # back to OPER
            f"PACC",
            polar_path,
            "",  # no dump file
            f"ALFA {alpha_deg:.4f}",
            "",  # newline
            "PACC",  # toggle off accumulation
            "",
            "QUIT",
        ]
        stdin_text = "\n".join(cmds) + "\n"

        try:
            subprocess.run(
                ["xfoil"],
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            return None

        polar_file = Path(polar_path)
        if not polar_file.exists():
            return None

        return _parse_xfoil_polar(polar_file.read_text())


class _XfoilCache:
    """Simple in-memory cache for XFOIL Cl/Cd lookups.

    Re is rounded to nearest 100 and alpha to 2 decimals for cache hits
    across adjacent radial stations.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, float, float], tuple[float, float]] = {}

    @staticmethod
    def _key(airfoil: str, Re: float, alpha_deg: float) -> tuple[str, float, float]:
        return airfoil, round(Re, -2), round(alpha_deg, 2)

    def get(self, airfoil: str, Re: float, alpha_deg: float) -> tuple[float, float] | None:
        return self._store.get(self._key(airfoil, Re, alpha_deg))

    def put(self, airfoil: str, Re: float, alpha_deg: float, cl: float, cd: float) -> None:
        self._store[self._key(airfoil, Re, alpha_deg)] = (cl, cd)


def _blade_geometry(
    r_frac: float,
    *,
    chord_root_mm: float,
    chord_tip_mm: float,
    twist_root_deg: float,
    twist_tip_deg: float,
    hub_r_frac: float = 0.15,
) -> tuple[float, float]:
    """Linear interpolation of chord and twist from hub to tip.

    *r_frac* is the fractional radial position (0 = center, 1 = tip).
    Stations inside the hub are clamped to root values.
    Returns ``(chord_mm, twist_deg)``.
    """
    if r_frac <= hub_r_frac:
        return chord_root_mm, twist_root_deg

    t = (r_frac - hub_r_frac) / (1.0 - hub_r_frac)
    chord = chord_root_mm + t * (chord_tip_mm - chord_root_mm)
    twist = twist_root_deg + t * (twist_tip_deg - twist_root_deg)
    return chord, twist


def _prandtl_loss(
    r: float,
    R: float,
    r_hub: float,
    num_blades: int,
    phi_rad: float,
) -> float:
    """Combined Prandtl tip + hub loss factor.

    Returns F clamped to [0.001, 1.0].
    """
    sin_phi = abs(math.sin(phi_rad))
    if sin_phi < 1e-10:
        return 1.0

    # Tip loss
    f_tip_arg = (num_blades / 2.0) * (R - r) / (r * sin_phi) if r > 1e-10 else 50.0
    f_tip_arg = min(f_tip_arg, 50.0)  # prevent overflow in exp
    F_tip = (2.0 / math.pi) * math.acos(min(1.0, math.exp(-f_tip_arg)))

    # Hub loss
    f_hub_arg = (num_blades / 2.0) * (r - r_hub) / (r * sin_phi) if r > 1e-10 else 50.0
    f_hub_arg = min(f_hub_arg, 50.0)
    F_hub = (2.0 / math.pi) * math.acos(min(1.0, math.exp(-f_hub_arg)))

    F = F_tip * F_hub
    return max(0.001, min(1.0, F))


@dataclass(frozen=True, slots=True)
class _BEMTResult:
    """Results from a single BEMT solve."""

    thrust_N: float
    torque_Nm: float
    power_W: float
    efficiency: float
    Ct: float
    Cq: float
    stations_converged: int
    stations_total: int


def _bemt_solve(
    *,
    diameter_mm: float,
    num_blades: int,
    rpm: float,
    rho: float,
    forward_velocity_mps: float,
    chord_root_mm: float,
    chord_tip_mm: float,
    twist_root_deg: float,
    twist_tip_deg: float,
    blade_pitch_deg: float = 0.0,
    airfoil: str = "NACA4412",
    Re: float = 500_000.0,
    radial_stations: int = 15,
    hub_r_frac: float = 0.15,
    xfoil_cache: _XfoilCache | None = None,
) -> _BEMTResult:
    """Run a Blade Element Momentum Theory solve.

    Returns a :class:`_BEMTResult` with thrust, torque, power and efficiency.
    """
    R_m = (diameter_mm / 2.0) / 1000.0  # tip radius in meters
    r_hub_m = R_m * hub_r_frac
    omega = rpm * 2.0 * math.pi / 60.0  # rad/s
    V = forward_velocity_mps
    n_rps = rpm / 60.0  # revolutions per second
    D_m = diameter_mm / 1000.0

    if rpm <= 0 or R_m <= 0:
        return _BEMTResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, radial_stations)

    cache = xfoil_cache or _XfoilCache()
    total_thrust = 0.0
    total_torque = 0.0
    stations_converged = 0

    # Radial stations from hub to tip (excluding center and exact tip)
    for i in range(radial_stations):
        r_frac = hub_r_frac + (1.0 - hub_r_frac) * (i + 0.5) / radial_stations
        r_m = r_frac * R_m
        dr = (1.0 - hub_r_frac) * R_m / radial_stations

        chord_mm, twist_deg = _blade_geometry(
            r_frac,
            chord_root_mm=chord_root_mm,
            chord_tip_mm=chord_tip_mm,
            twist_root_deg=twist_root_deg,
            twist_tip_deg=twist_tip_deg,
            hub_r_frac=hub_r_frac,
        )
        chord_m = chord_mm / 1000.0
        theta_rad = math.radians(twist_deg + blade_pitch_deg)

        # Solidity at this station
        sigma = num_blades * chord_m / (2.0 * math.pi * r_m) if r_m > 1e-10 else 0.0

        # Local Reynolds number scaling
        V_tip = omega * r_m
        V_local = math.sqrt(V * V + V_tip * V_tip)
        Re_local = Re * (V_local * chord_m) / (max(V_tip, 0.1) * (chord_root_mm / 1000.0)) if chord_root_mm > 0 else Re

        # BEM iteration — initialize a with small value in hover to
        # avoid the phi=0 singularity when V_axial starts at zero.
        a = 0.05 if V < 0.1 else 0.0
        a_p = 0.0
        converged = False

        for _iteration in range(100):
            # Velocity triangle
            V_axial = V * (1.0 + a) if V > 0.1 else V_tip * a
            V_tan = omega * r_m * (1.0 - a_p)

            if V_axial < 1e-10 and V_tan < 1e-10:
                break

            phi = math.atan2(V_axial, V_tan) if V_tan > 1e-10 else math.pi / 2.0
            alpha_deg = math.degrees(theta_rad - phi)

            # Clamp alpha to reasonable range
            alpha_deg = max(-20.0, min(20.0, alpha_deg))

            # Get Cl, Cd from cache or XFOIL (fallback to flat-plate)
            cached = cache.get(airfoil, Re_local, alpha_deg)
            if cached is not None:
                cl, cd = cached
            else:
                xfoil_result = _run_xfoil(airfoil, alpha_deg, Re_local)
                if xfoil_result is not None:
                    cl, cd = xfoil_result
                else:
                    cl, cd = _flat_plate_polar(alpha_deg, Re_local)
                cache.put(airfoil, Re_local, alpha_deg, cl, cd)

            # Force coefficients in wind-aligned frame
            sin_phi = math.sin(phi)
            cos_phi = math.cos(phi)
            Cn = cl * cos_phi + cd * sin_phi  # normal (thrust direction)
            Ct_local = cl * sin_phi - cd * cos_phi  # tangential (torque direction)

            # Prandtl loss
            F = _prandtl_loss(r_m, R_m, r_hub_m, num_blades, phi)

            # New induction factors
            denom_a = 4.0 * F * sin_phi * sin_phi + sigma * Cn
            denom_ap = 4.0 * F * sin_phi * cos_phi - sigma * Ct_local

            if abs(denom_a) < 1e-12 or abs(denom_ap) < 1e-12:
                break

            a_new = (sigma * Cn) / denom_a
            a_p_new = (sigma * Ct_local) / denom_ap

            # Glauert correction for high thrust (a > 0.4)
            if a_new > 0.4:
                # Simplified Glauert: a = 0.5*(2+K*(1-2*ac)-sqrt(...))
                # Use empirical correction
                a_new = 0.4 + (a_new - 0.4) * 0.5

            # Clamp
            a_new = max(0.0, min(0.95, a_new))
            a_p_new = max(-0.5, min(0.5, a_p_new))

            # Relaxation
            relax = 0.3
            a_old, a_p_old = a, a_p
            a = a_old + relax * (a_new - a_old)
            a_p = a_p_old + relax * (a_p_new - a_p_old)

            if abs(a - a_old) < 1e-4 and abs(a_p - a_p_old) < 1e-4:
                converged = True
                break

        if converged:
            stations_converged += 1

        # Integrate forces at this station using final induction factors
        V_axial_final = V * (1.0 + a) if V > 0.1 else V_tip * a
        V_tan_final = omega * r_m * (1.0 - a_p)
        W_sq = V_axial_final ** 2 + V_tan_final ** 2

        dT = 0.5 * rho * W_sq * chord_m * Cn * num_blades * dr
        dQ = 0.5 * rho * W_sq * chord_m * Ct_local * num_blades * r_m * dr

        total_thrust += dT
        total_torque += dQ

    power = total_torque * omega
    power = max(power, 1e-12)  # avoid division by zero

    # Non-dimensional coefficients: Ct = T / (rho * n^2 * D^4)
    n_sq_D4 = n_rps * n_rps * D_m ** 4 if n_rps > 0 else 1.0
    n_sq_D5 = n_rps * n_rps * D_m ** 5 if n_rps > 0 else 1.0
    Ct = total_thrust / (rho * n_sq_D4) if n_sq_D4 > 1e-12 else 0.0
    Cq = total_torque / (rho * n_sq_D5) if n_sq_D5 > 1e-12 else 0.0

    # Efficiency
    if V > 0.1:
        # Forward flight: propulsive efficiency
        efficiency = (total_thrust * V) / power if power > 1e-12 else 0.0
    else:
        # Hover: figure of merit  FM = Ct^(3/2) / (sqrt(2) * Cq)
        if abs(Cq) > 1e-12 and Ct > 0:
            efficiency = Ct ** 1.5 / (math.sqrt(2.0) * abs(Cq))
        else:
            efficiency = 0.0

    efficiency = max(0.0, min(1.0, efficiency))

    return _BEMTResult(
        thrust_N=total_thrust,
        torque_Nm=total_torque,
        power_W=power,
        efficiency=efficiency,
        Ct=Ct,
        Cq=Cq,
        stations_converged=stations_converged,
        stations_total=radial_stations,
    )


class BEMTXfoilSolver(SolverAdapter):
    """BEMT + XFOIL solver for propeller/rotor analysis.

    Pipeline per variant:
    1. Build blade element geometry from params (chord, twist, airfoil)
    2. Run XFOIL at each radial station to get Cl/Cd polars
    3. BEM loop: integrate thrust and torque across blade span
    4. Return thrust_N, torque_Nm, power_W, efficiency

    Falls back to flat-plate aerodynamics per station when XFOIL fails to
    converge, so the solver always produces results even if XFOIL is flaky.
    """

    def name(self) -> str:
        return "bemt_xfoil"

    def available(self) -> bool:
        return shutil.which("xfoil") is not None

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        # ~2-10s depending on radial stations and XFOIL convergence
        stations = config_params.get("radial_stations", 15)
        return float(stations) * 0.5

    def describe_pipeline(self) -> str:
        return (
            "1. Generate blade element geometry from design params\n"
            "2. Run XFOIL at each radial station for Cl/Cd polars\n"
            "3. BEM integration loop for thrust/torque/power\n"
            "4. Extract performance metrics"
        )

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        merged = {**fixed, **params}

        # Required params
        for key in ("rpm", "num_blades", "diameter_mm", "airfoil"):
            if key not in merged and key not in config_params:
                errors.append(f"'{key}' required in params or fixed_params")

        # Re can be in any dict
        if "Re" not in merged and "Re" not in config_params:
            errors.append("Reynolds number (Re) required in params, fixed_params, or solver config")

        # chord: accept chord_root_mm or chord_mm
        if "chord_root_mm" not in merged and "chord_mm" not in merged:
            if "chord_root_mm" not in config_params and "chord_mm" not in config_params:
                errors.append("'chord_root_mm' (or 'chord_mm') required")

        # Range checks
        rpm = merged.get("rpm", config_params.get("rpm"))
        if rpm is not None and rpm <= 0:
            errors.append("rpm must be > 0")

        blades = merged.get("num_blades", config_params.get("num_blades"))
        if blades is not None and blades < 1:
            errors.append("num_blades must be >= 1")

        diameter = merged.get("diameter_mm", config_params.get("diameter_mm"))
        if diameter is not None and diameter <= 0:
            errors.append("diameter_mm must be > 0")

        return errors

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        merged = {**config_params, **fixed, **params}

        # Extract with smart defaults
        chord_root = merged.get("chord_root_mm", merged.get("chord_mm", 25.0))
        chord_tip = merged.get("chord_tip_mm", chord_root * 0.5)
        twist_root = merged.get("twist_root_deg", merged.get("twist_deg", 15.0))
        twist_tip = merged.get("twist_tip_deg", twist_root * 0.3)

        result = _bemt_solve(
            diameter_mm=float(merged["diameter_mm"]),
            num_blades=int(merged["num_blades"]),
            rpm=float(merged["rpm"]),
            rho=float(merged.get("rho", 1.225)),
            forward_velocity_mps=float(merged.get("forward_velocity_mps", 0.0)),
            chord_root_mm=float(chord_root),
            chord_tip_mm=float(chord_tip),
            twist_root_deg=float(twist_root),
            twist_tip_deg=float(twist_tip),
            blade_pitch_deg=float(merged.get("blade_pitch_deg", 0.0)),
            airfoil=str(merged.get("airfoil", "NACA4412")),
            Re=float(merged.get("Re", 500_000)),
            radial_stations=int(merged.get("radial_stations", 15)),
        )

        return {
            "thrust_N": result.thrust_N,
            "torque_Nm": result.torque_Nm,
            "power_W": result.power_W,
            "efficiency": result.efficiency,
            "Ct": result.Ct,
            "Cq": result.Cq,
            "stations_converged": float(result.stations_converged),
            "stations_total": float(result.stations_total),
        }


# ---------------------------------------------------------------------------
# STL utilities
# ---------------------------------------------------------------------------

def _read_stl_bounds(stl_path: str) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Read bounding box from a binary STL file.

    Returns (min_xyz, max_xyz) tuples.
    Raises RuntimeError on empty or corrupt STL.
    """
    path = Path(stl_path)
    data = path.read_bytes()

    if len(data) < 84:
        raise RuntimeError("STL is empty or corrupt")

    # Binary STL: 80-byte header + 4-byte triangle count
    n_triangles = struct.unpack_from("<I", data, 80)[0]
    if n_triangles == 0:
        raise RuntimeError("STL is empty or corrupt")

    expected_size = 84 + n_triangles * 50  # each facet = 50 bytes
    if len(data) < expected_size:
        raise RuntimeError("STL is empty or corrupt")

    x_min = y_min = z_min = float("inf")
    x_max = y_max = z_max = float("-inf")

    offset = 84
    for _ in range(n_triangles):
        # Skip normal (12 bytes), read 3 vertices (36 bytes), skip attr (2 bytes)
        for v in range(3):
            vx, vy, vz = struct.unpack_from("<fff", data, offset + 12 + v * 12)
            x_min = min(x_min, vx)
            y_min = min(y_min, vy)
            z_min = min(z_min, vz)
            x_max = max(x_max, vx)
            y_max = max(y_max, vy)
            z_max = max(z_max, vz)
        offset += 50

    return (x_min, y_min, z_min), (x_max, y_max, z_max)


def _scale_stl_to_meters(src_path: str, dst_path: str) -> None:
    """Read binary STL, scale all vertex coords mm → m (×0.001), write to dst."""
    data = bytearray(Path(src_path).read_bytes())

    if len(data) < 84:
        raise RuntimeError("STL is empty or corrupt")

    n_triangles = struct.unpack_from("<I", data, 80)[0]
    if n_triangles == 0:
        raise RuntimeError("STL is empty or corrupt")

    offset = 84
    for _ in range(n_triangles):
        # Scale normal (12 bytes) and 3 vertices (36 bytes) — 4 vectors total
        for i in range(4):
            pos = offset + i * 12
            x, y, z = struct.unpack_from("<fff", data, pos)
            struct.pack_into("<fff", data, pos, x * 0.001, y * 0.001, z * 0.001)
        offset += 50

    Path(dst_path).write_bytes(bytes(data))


# ---------------------------------------------------------------------------
# Domain sizing
# ---------------------------------------------------------------------------

def _compute_domain(
    bounds_min: tuple[float, float, float],
    bounds_max: tuple[float, float, float],
) -> dict[str, float]:
    """Compute CFD domain extents from STL bounds (in meters).

    Domain: 5× upstream, 10× downstream, 5× cross-stream.
    Returns dict with x_min, x_max, y_min, y_max, z_min, z_max, char_length.
    """
    dx = bounds_max[0] - bounds_min[0]
    dy = bounds_max[1] - bounds_min[1]
    dz = bounds_max[2] - bounds_min[2]
    char_length = math.sqrt(dx * dx + dy * dy + dz * dz)

    if char_length < 1e-12:
        raise RuntimeError("STL bounding box has zero volume")

    cx = (bounds_min[0] + bounds_max[0]) / 2
    cy = (bounds_min[1] + bounds_max[1]) / 2
    cz = (bounds_min[2] + bounds_max[2]) / 2

    return {
        "x_min": cx - 5 * char_length,
        "x_max": cx + 10 * char_length,
        "y_min": cy - 5 * char_length,
        "y_max": cy + 5 * char_length,
        "z_min": cz - 5 * char_length,
        "z_max": cz + 5 * char_length,
        "char_length": char_length,
    }


def _mesh_cells_by_refinement(
    refinement: int,
    domain: dict[str, float],
) -> tuple[int, int, int]:
    """Compute base mesh cell counts from refinement level and domain size.

    Level 1 → char_length/10 cell size, level 4 → char_length/40.
    """
    divisor = {1: 10, 2: 20, 3: 30, 4: 40}.get(refinement, 20)
    cell_size = domain["char_length"] / divisor

    nx = max(1, round((domain["x_max"] - domain["x_min"]) / cell_size))
    ny = max(1, round((domain["y_max"] - domain["y_min"]) / cell_size))
    nz = max(1, round((domain["z_max"] - domain["z_min"]) / cell_size))

    return nx, ny, nz


# ---------------------------------------------------------------------------
# OpenFOAM case directory templates
# ---------------------------------------------------------------------------

_SUPPORTED_TURBULENCE_MODELS = ("kOmegaSST", "kEpsilon", "SpalartAllmaras")

_REFINEMENT_TO_SURFACE_LEVELS: dict[int, tuple[int, int]] = {
    1: (1, 2),
    2: (2, 3),
    3: (3, 4),
    4: (4, 5),
}

_MAX_ITERS_BY_REFINEMENT: dict[int, int] = {
    1: 500,
    2: 1000,
    3: 2000,
    4: 3000,
}

_TRANSPORT_PROPERTIES = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      transportProperties;
}

transportModel  Newtonian;
nu              [0 2 -1 0 0 0 0] 1.5e-05;
"""

_TURBULENCE_PROPERTIES_RAS = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      turbulenceProperties;
}}

simulationType  RAS;

RAS
{{
    RASModel        {model};
    turbulence      on;
    printCoeffs     on;
}}
"""

_CONTROL_DICT_BASE = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      controlDict;
}}

application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {end_time};
deltaT          1;
writeControl    timeStep;
writeInterval   {write_interval};
purgeWrite      2;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;
"""

_CONTROL_DICT_FORCECOEFFS = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      controlDict;
}}

application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {end_time};
deltaT          1;
writeControl    timeStep;
writeInterval   {write_interval};
purgeWrite      2;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

functions
{{
    forceCoeffs
    {{
        type            forceCoeffs;
        libs            (forces);
        writeControl    timeStep;
        writeInterval   1;
        log             true;
        patches         (geometry);
        rho             rhoInf;
        rhoInf          {rho};
        liftDir         (0 0 1);
        dragDir         (1 0 0);
        pitchAxis       (0 1 0);
        magUInf         {mag_u_inf};
        lRef            {l_ref};
        Aref            {a_ref};
        CofR            (0 0 0);
    }}
}}
"""

_FV_SCHEMES = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      fvSchemes;
}

ddtSchemes
{
    default         steadyState;
}

gradSchemes
{
    default         Gauss linear;
}

divSchemes
{
    default         none;
    div(phi,U)      bounded Gauss linearUpwind grad(U);
    div(phi,k)      bounded Gauss upwind;
    div(phi,omega)  bounded Gauss upwind;
    div(phi,epsilon) bounded Gauss upwind;
    div(phi,nuTilda) bounded Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}

laplacianSchemes
{
    default         Gauss linear corrected;
}

interpolationSchemes
{
    default         linear;
}

snGradSchemes
{
    default         corrected;
}

wallDist
{
    method          meshWave;
}
"""

_FV_SOLUTION = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      fvSolution;
}

solvers
{
    p
    {
        solver          GAMG;
        tolerance       1e-06;
        relTol          0.1;
        smoother        GaussSeidel;
    }

    "(U|k|omega|epsilon|nuTilda)"
    {
        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-06;
        relTol          0.1;
    }
}

SIMPLE
{
    nNonOrthogonalCorrectors 0;
    consistent      yes;

    residualControl
    {
        p               1e-4;
        U               1e-4;
        "(k|omega|epsilon|nuTilda)" 1e-4;
    }
}

relaxationFactors
{
    fields
    {
        p               0.3;
    }
    equations
    {
        U               0.7;
        k               0.7;
        omega           0.7;
        epsilon         0.7;
        nuTilda         0.7;
    }
}
"""

_BLOCK_MESH_DICT = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      blockMeshDict;
}}

convertToMeters 1;

vertices
(
    ({x_min} {y_min} {z_min})
    ({x_max} {y_min} {z_min})
    ({x_max} {y_max} {z_min})
    ({x_min} {y_max} {z_min})
    ({x_min} {y_min} {z_max})
    ({x_max} {y_min} {z_max})
    ({x_max} {y_max} {z_max})
    ({x_min} {y_max} {z_max})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);

edges
(
);

boundary
(
    inlet
    {{
        type patch;
        faces
        (
            (0 4 7 3)
        );
    }}
    outlet
    {{
        type patch;
        faces
        (
            (1 2 6 5)
        );
    }}
    farfield
    {{
        type patch;
        faces
        (
            (0 1 5 4)
            (2 3 7 6)
            (0 3 2 1)
            (4 5 6 7)
        );
    }}
);
"""

_SNAPPY_HEX_MESH_DICT = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      snappyHexMeshDict;
}}

castellatedMesh true;
snap            true;
addLayers       false;

geometry
{{
    geometry.stl
    {{
        type triSurfaceMesh;
        name geometry;
    }}
}}

castellatedMeshControls
{{
    maxLocalCells   100000;
    maxGlobalCells  2000000;
    minRefinementCells 10;
    maxLoadUnbalance 0.10;
    nCellsBetweenLevels 3;

    features
    (
    );

    refinementSurfaces
    {{
        geometry
        {{
            level ({surf_min} {surf_max});
        }}
    }}

    resolveFeatureAngle 30;

    refinementRegions
    {{
    }}

    locationInMesh ({loc_x} {loc_y} {loc_z});
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch    3;
    tolerance       2.0;
    nSolveIter      100;
    nRelaxIter      5;
    nFeatureSnapIter 10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes   true;
    layers
    {{
    }}
    expansionRatio  1.0;
    finalLayerThickness 0.3;
    minThickness    0.1;
    nGrow           0;
    featureAngle    60;
    nRelaxIter      3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals  3;
    nSmoothThickness 10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle 90;
    nBufferCellsNoExtrude 0;
    nLayerIter      50;
}}

meshQualityControls
{{
    maxNonOrtho     65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave      80;
    minVol          1e-13;
    minTetQuality   -1e30;
    minArea         -1;
    minTwist        0.02;
    minDeterminant  0.001;
    minFaceWeight   0.05;
    minVolRatio     0.01;
    minTriangleTwist -1;
    nSmoothScale    4;
    errorReduction  0.75;
}}

writeFlags
(
    scalarLevels
    layerSets
    layerFields
);

mergeTolerance 1e-6;
"""

# Boundary condition templates for 0/ fields

_U_FIELD = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       volVectorField;
    object      U;
}}

dimensions      [0 1 -1 0 0 0 0];

internalField   uniform ({vx} {vy} {vz});

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform ({vx} {vy} {vz});
    }}
    outlet
    {{
        type            zeroGradient;
    }}
    geometry
    {{
        type            noSlip;
    }}
    farfield
    {{
        type            slip;
    }}
}}
"""

_P_FIELD = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      p;
}

dimensions      [0 2 -2 0 0 0 0];

internalField   uniform 0;

boundaryField
{
    inlet
    {
        type            zeroGradient;
    }
    outlet
    {
        type            fixedValue;
        value           uniform 0;
    }
    geometry
    {
        type            zeroGradient;
    }
    farfield
    {
        type            slip;
    }
}
"""

_K_FIELD = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      k;
}}

dimensions      [0 2 -2 0 0 0 0];

internalField   uniform {k_val};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {k_val};
    }}
    outlet
    {{
        type            zeroGradient;
    }}
    geometry
    {{
        type            kqRWallFunction;
        value           uniform {k_val};
    }}
    farfield
    {{
        type            slip;
    }}
}}
"""

_OMEGA_FIELD = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      omega;
}}

dimensions      [0 0 -1 0 0 0 0];

internalField   uniform {omega_val};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {omega_val};
    }}
    outlet
    {{
        type            zeroGradient;
    }}
    geometry
    {{
        type            omegaWallFunction;
        value           uniform {omega_val};
    }}
    farfield
    {{
        type            slip;
    }}
}}
"""

_EPSILON_FIELD = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      epsilon;
}}

dimensions      [0 2 -3 0 0 0 0];

internalField   uniform {epsilon_val};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {epsilon_val};
    }}
    outlet
    {{
        type            zeroGradient;
    }}
    geometry
    {{
        type            epsilonWallFunction;
        value           uniform {epsilon_val};
    }}
    farfield
    {{
        type            slip;
    }}
}}
"""

_NUT_FIELD = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      nut;
}}

dimensions      [0 2 -1 0 0 0 0];

internalField   uniform 0;

boundaryField
{{
    inlet
    {{
        type            calculated;
        value           uniform 0;
    }}
    outlet
    {{
        type            calculated;
        value           uniform 0;
    }}
    geometry
    {{
        type            nutkWallFunction;
        value           uniform 0;
    }}
    farfield
    {{
        type            calculated;
        value           uniform 0;
    }}
}}
"""

_NU_TILDA_FIELD = """\
FoamFile
{{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      nuTilda;
}}

dimensions      [0 2 -1 0 0 0 0];

internalField   uniform {nu_tilda_val};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {nu_tilda_val};
    }}
    outlet
    {{
        type            zeroGradient;
    }}
    geometry
    {{
        type            fixedValue;
        value           uniform 0;
    }}
    farfield
    {{
        type            slip;
    }}
}}
"""


def _write_openfoam_case(
    case_dir: str,
    stl_name: str,
    domain: dict[str, float],
    mesh_cells: tuple[int, int, int],
    velocity: tuple[float, float, float],
    refinement: int,
    turbulence_model: str,
    ref_length: float,
    ref_area: float,
    rho: float,
    max_iterations: int,
    *,
    use_function_objects: bool = False,
) -> None:
    """Write a complete OpenFOAM case directory."""
    case = Path(case_dir)

    # Create directory structure
    (case / "constant").mkdir(parents=True, exist_ok=True)
    (case / "system").mkdir(parents=True, exist_ok=True)
    (case / "0").mkdir(parents=True, exist_ok=True)

    vx, vy, vz = velocity
    mag_u = math.sqrt(vx * vx + vy * vy + vz * vz)
    nx, ny, nz = mesh_cells
    surf_min, surf_max = _REFINEMENT_TO_SURFACE_LEVELS.get(refinement, (2, 3))

    # locationInMesh: a point inside the domain but outside the STL geometry
    # Place it upstream of the object center
    loc_x = domain["x_min"] + 0.1 * (domain["x_max"] - domain["x_min"])
    loc_y = (domain["y_min"] + domain["y_max"]) / 2
    loc_z = (domain["z_min"] + domain["z_max"]) / 2

    # constant/
    (case / "constant" / "transportProperties").write_text(_TRANSPORT_PROPERTIES)
    (case / "constant" / "turbulenceProperties").write_text(
        _TURBULENCE_PROPERTIES_RAS.format(model=turbulence_model)
    )

    # system/
    write_interval = max(1, max_iterations // 5)
    if use_function_objects:
        ctrl_text = _CONTROL_DICT_FORCECOEFFS.format(
            end_time=max_iterations,
            write_interval=write_interval,
            rho=rho,
            mag_u_inf=mag_u,
            l_ref=ref_length,
            a_ref=ref_area,
        )
    else:
        ctrl_text = _CONTROL_DICT_BASE.format(
            end_time=max_iterations,
            write_interval=write_interval,
        )
    (case / "system" / "controlDict").write_text(ctrl_text)
    (case / "system" / "fvSchemes").write_text(_FV_SCHEMES)
    (case / "system" / "fvSolution").write_text(_FV_SOLUTION)
    (case / "system" / "blockMeshDict").write_text(
        _BLOCK_MESH_DICT.format(
            x_min=domain["x_min"], x_max=domain["x_max"],
            y_min=domain["y_min"], y_max=domain["y_max"],
            z_min=domain["z_min"], z_max=domain["z_max"],
            nx=nx, ny=ny, nz=nz,
        )
    )
    (case / "system" / "snappyHexMeshDict").write_text(
        _SNAPPY_HEX_MESH_DICT.format(
            surf_min=surf_min, surf_max=surf_max,
            loc_x=loc_x, loc_y=loc_y, loc_z=loc_z,
        )
    )

    # 0/ boundary conditions
    (case / "0" / "U").write_text(
        _U_FIELD.format(vx=vx, vy=vy, vz=vz)
    )
    (case / "0" / "p").write_text(_P_FIELD)
    (case / "0" / "nut").write_text(_NUT_FIELD.format())

    # Turbulence-specific fields
    nu = 1.5e-05  # kinematic viscosity (m^2/s)
    turbulence_intensity = 0.05
    k_val = 1.5 * (mag_u * turbulence_intensity) ** 2

    if turbulence_model in ("kOmegaSST", "kEpsilon"):
        (case / "0" / "k").write_text(_K_FIELD.format(k_val=k_val))
        if turbulence_model == "kOmegaSST":
            omega_val = k_val / (nu * 10)  # approximate
            (case / "0" / "omega").write_text(
                _OMEGA_FIELD.format(omega_val=omega_val)
            )
        else:
            epsilon_val = 0.09 * k_val ** 1.5 / (0.1 * ref_length)
            (case / "0" / "epsilon").write_text(
                _EPSILON_FIELD.format(epsilon_val=epsilon_val)
            )
    elif turbulence_model == "SpalartAllmaras":
        nu_tilda_val = 3 * nu
        (case / "0" / "nuTilda").write_text(
            _NU_TILDA_FIELD.format(nu_tilda_val=nu_tilda_val)
        )


# ---------------------------------------------------------------------------
# Subprocess runners
# ---------------------------------------------------------------------------

def _run_openfoam_cmd(
    cmd: list[str],
    case_dir: str,
    step_name: str,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run an OpenFOAM command and raise on failure."""
    try:
        result = subprocess.run(
            cmd,
            cwd=case_dir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{step_name} timed out after {timeout_s}s") from exc

    if result.returncode != 0:
        raise RuntimeError(f"{step_name} failed: {result.stderr[:500]}")

    return result


def _run_meshing(case_dir: str, timeout_s: float) -> None:
    """Run blockMesh then snappyHexMesh -overwrite."""
    _run_openfoam_cmd(["blockMesh"], case_dir, "blockMesh", timeout_s)

    poly_mesh = Path(case_dir) / "constant" / "polyMesh" / "points"
    if not poly_mesh.exists():
        raise RuntimeError("blockMesh produced no mesh")

    _run_openfoam_cmd(
        ["snappyHexMesh", "-overwrite"], case_dir, "snappyHexMesh", timeout_s,
    )

    if not poly_mesh.exists():
        raise RuntimeError("snappyHexMesh produced no mesh")


def _run_solver(case_dir: str, timeout_s: float) -> None:
    """Run simpleFoam."""
    result = _run_openfoam_cmd(["simpleFoam"], case_dir, "simpleFoam", timeout_s)

    if "FOAM FATAL ERROR" in result.stdout or "FOAM FATAL ERROR" in result.stderr:
        details = result.stderr[:500] if result.stderr else result.stdout[:500]
        raise RuntimeError(f"simpleFoam diverged: {details}")


# ---------------------------------------------------------------------------
# Result parser
# ---------------------------------------------------------------------------

def _parse_force_coefficients(
    case_dir: str,
    rho: float,
    velocity: float,
    ref_area: float,
) -> dict[str, float]:
    """Parse forceCoeffs output and return aerodynamic metrics."""
    coeff_dir = Path(case_dir) / "postProcessing" / "forceCoeffs"
    if not coeff_dir.exists():
        raise RuntimeError("No force coefficients output")

    # Find the time directory (usually "0")
    time_dirs = sorted(coeff_dir.iterdir())
    if not time_dirs:
        raise RuntimeError("No force coefficients output")

    dat_file = time_dirs[0] / "coefficient.dat"
    if not dat_file.exists():
        # Try alternative name
        dat_file = time_dirs[0] / "forceCoeffs.dat"
    if not dat_file.exists():
        raise RuntimeError("No force coefficients output")

    lines = dat_file.read_text().splitlines()

    # Parse header to find column indices
    header_line = ""
    data_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            header_line = stripped
        elif stripped:
            data_lines.append(stripped)

    if not data_lines:
        raise RuntimeError("No force coefficients output")

    # Parse header for column names
    col_names: list[str] = []
    if header_line:
        # Header format: "# Time Cd Cl CmPitch ..." or similar
        parts = header_line.lstrip("#").split()
        col_names = parts

    # Take last line as converged values
    last_vals = data_lines[-1].split()
    values = [float(v) for v in last_vals]

    # Map columns to coefficients
    cd = cl = cm = 0.0
    cd_idx = cl_idx = cm_idx = -1

    for i, name in enumerate(col_names):
        name_lower = name.lower()
        if name_lower == "cd":
            cd_idx = i
        elif name_lower == "cl":
            cl_idx = i
        elif name_lower in ("cmpitch", "cm"):
            cm_idx = i

    if cd_idx >= 0 and cd_idx < len(values):
        cd = values[cd_idx]
    elif len(values) >= 2:
        cd = values[1]  # fallback: second column

    if cl_idx >= 0 and cl_idx < len(values):
        cl = values[cl_idx]
    elif len(values) >= 3:
        cl = values[2]  # fallback: third column

    if cm_idx >= 0 and cm_idx < len(values):
        cm = values[cm_idx]
    elif len(values) >= 4:
        cm = values[3]  # fallback: fourth column

    # Check convergence: std/mean of Cd over last 20% of data
    if len(data_lines) >= 10:
        n_tail = max(2, len(data_lines) // 5)
        tail_cd: list[float] = []
        for dl in data_lines[-n_tail:]:
            vals = dl.split()
            if cd_idx >= 0 and cd_idx < len(vals):
                tail_cd.append(float(vals[cd_idx]))
            elif len(vals) >= 2:
                tail_cd.append(float(vals[1]))
        if tail_cd:
            mean_cd = sum(tail_cd) / len(tail_cd)
            if abs(mean_cd) > 1e-10:
                std_cd = (sum((v - mean_cd) ** 2 for v in tail_cd) / len(tail_cd)) ** 0.5
                if std_cd / abs(mean_cd) > 0.01:
                    log.warning(
                        "Force coefficients may not be converged "
                        "(Cd std/mean = %.4f)", std_cd / abs(mean_cd),
                    )

    # Dimensional forces
    q = 0.5 * rho * velocity * velocity
    drag_n = cd * q * ref_area
    lift_n = cl * q * ref_area

    return {
        "Cd": cd,
        "Cl": cl,
        "Cm": cm,
        "drag_N": drag_n,
        "lift_N": lift_n,
    }


# ---------------------------------------------------------------------------
# OpenFOAM ASCII field parsers (for force computation without function objects)
# ---------------------------------------------------------------------------

import re as _re


def _skip_foam_header(text: str) -> str:
    """Strip the ``FoamFile { ... }`` header block from OpenFOAM ASCII files."""
    # Find end of FoamFile block — first "}" after "FoamFile"
    m = _re.search(r"FoamFile\s*\{[^}]*\}", text)
    if m:
        return text[m.end():]
    return text


def _parse_openfoam_boundary(path: str) -> dict[str, dict[str, int]]:
    """Parse ``constant/polyMesh/boundary`` → {patch_name: {nFaces, startFace}}."""
    text = _skip_foam_header(Path(path).read_text())
    result: dict[str, dict[str, int]] = {}

    # Boundary format: count (\n patches...
    # Each patch: name { type ...; nFaces N; startFace N; }
    # Find all patch blocks
    patch_pattern = _re.compile(
        r"(\w+)\s*\{[^}]*?"
        r"nFaces\s+(\d+)\s*;"
        r"[^}]*?"
        r"startFace\s+(\d+)\s*;"
        r"[^}]*?\}",
        _re.DOTALL,
    )
    for m in patch_pattern.finditer(text):
        name = m.group(1)
        n_faces = int(m.group(2))
        start_face = int(m.group(3))
        result[name] = {"nFaces": n_faces, "startFace": start_face}

    return result


def _parse_openfoam_label_list(path: str) -> list[int]:
    """Parse an OpenFOAM label list file (e.g. ``owner``, ``neighbour``).

    Format: count ``(`` label0 label1 ... ``)``
    """
    text = _skip_foam_header(Path(path).read_text())
    # Find the count then the parenthesised list
    m = _re.search(r"(\d+)\s*\(([^)]*)\)", text, _re.DOTALL)
    if not m:
        return []
    return [int(x) for x in m.group(2).split()]


def _parse_openfoam_points(path: str) -> list[tuple[float, float, float]]:
    """Parse ``constant/polyMesh/points`` → list of (x, y, z)."""
    text = _skip_foam_header(Path(path).read_text())
    m = _re.search(r"(\d+)\s*\((.+)\)", text, _re.DOTALL)
    if not m:
        return []
    points: list[tuple[float, float, float]] = []
    for pm in _re.finditer(r"\(\s*([^\s]+)\s+([^\s]+)\s+([^\s]+)\s*\)", m.group(2)):
        points.append((float(pm.group(1)), float(pm.group(2)), float(pm.group(3))))
    return points


def _parse_openfoam_faces(path: str) -> list[list[int]]:
    """Parse ``constant/polyMesh/faces`` → list of vertex-index lists."""
    text = _skip_foam_header(Path(path).read_text())
    # Each face: N(v0 v1 v2 ...) — e.g. 4(0 1 5 4)
    m = _re.search(r"(\d+)\s*\((.+)\)", text, _re.DOTALL)
    if not m:
        return []
    faces: list[list[int]] = []
    for fm in _re.finditer(r"\d+\(([^)]+)\)", m.group(2)):
        faces.append([int(x) for x in fm.group(1).split()])
    return faces


def _parse_openfoam_scalar_field(path: str) -> list[float]:
    """Parse an OpenFOAM volScalarField (e.g. ``p``).

    Handles both ``uniform <value>`` and ``nonuniform List<scalar> N (...)``.
    """
    text = _skip_foam_header(Path(path).read_text())

    # Check for uniform
    um = _re.search(r"internalField\s+uniform\s+([^\s;]+)", text)
    if um:
        # Return a single-element list; caller can broadcast
        return [float(um.group(1))]

    # nonuniform List<scalar>
    m = _re.search(r"internalField\s+nonuniform\s+List<scalar>\s*\n?\s*(\d+)\s*\(([^)]*)\)", text, _re.DOTALL)
    if m:
        return [float(x) for x in m.group(2).split()]

    return []


def _find_latest_time_dir(case_dir: str) -> str:
    """Find the highest-numbered time directory in an OpenFOAM case."""
    best: float = -1.0
    best_path: str = ""
    case = Path(case_dir)
    for entry in case.iterdir():
        if not entry.is_dir():
            continue
        try:
            t = float(entry.name)
        except ValueError:
            continue
        if t > best:
            best = t
            best_path = str(entry)
    if not best_path:
        raise RuntimeError(f"No time directories found in {case_dir}")
    return best_path


# ---------------------------------------------------------------------------
# Face area vector (Newell's method) and force computation
# ---------------------------------------------------------------------------

def _face_area_vector(
    vertices: list[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """Compute area-weighted outward normal of a polygon using Newell's method.

    Returns ``(nx, ny, nz)`` where ``|n| = face area``.
    """
    n = len(vertices)
    if n < 3:
        return (0.0, 0.0, 0.0)

    ax = ay = az = 0.0
    for i in range(n):
        v_cur = vertices[i]
        v_next = vertices[(i + 1) % n]
        ax += (v_cur[1] - v_next[1]) * (v_cur[2] + v_next[2])
        ay += (v_cur[2] - v_next[2]) * (v_cur[0] + v_next[0])
        az += (v_cur[0] - v_next[0]) * (v_cur[1] + v_next[1])

    return (0.5 * ax, 0.5 * ay, 0.5 * az)


def _compute_forces_from_fields(
    case_dir: str,
    rho: float,
    velocity: float,
    ref_area: float,
    ref_length: float = 1.0,
    *,
    patch_name: str = "geometry",
    lift_dir: tuple[float, float, float] = (0, 0, 1),
    drag_dir: tuple[float, float, float] = (1, 0, 0),
    pitch_axis: tuple[float, float, float] = (0, 1, 0),
    cofr: tuple[float, float, float] = (0, 0, 0),
) -> dict[str, float]:
    """Compute aerodynamic forces by integrating pressure over a boundary patch.

    This avoids OpenFOAM's ``forces`` function object (which crashes on some
    packaged builds due to a sha1 IOstream bug).  Only pressure forces are
    included — viscous forces are neglected (acceptable for bluff-body flows
    where pressure drag dominates).

    Algorithm:
        1. Parse boundary → find *patch_name* startFace / nFaces.
        2. Parse points, faces, owner from ``constant/polyMesh/``.
        3. Parse pressure from the latest time directory.
        4. For each patch face: area_vec = Newell(verts), F += p * rho * area_vec.
        5. Project total force onto drag / lift / pitch-moment directions.
        6. Non-dimensionalise with dynamic pressure and reference area.
    """
    poly = Path(case_dir) / "constant" / "polyMesh"

    boundary = _parse_openfoam_boundary(str(poly / "boundary"))
    if patch_name not in boundary:
        raise RuntimeError(
            f"Patch '{patch_name}' not found in boundary. "
            f"Available: {list(boundary)}"
        )
    patch = boundary[patch_name]
    start = patch["startFace"]
    n_faces = patch["nFaces"]

    points = _parse_openfoam_points(str(poly / "points"))
    faces = _parse_openfoam_faces(str(poly / "faces"))
    owner = _parse_openfoam_label_list(str(poly / "owner"))

    time_dir = _find_latest_time_dir(case_dir)
    p_field = _parse_openfoam_scalar_field(str(Path(time_dir) / "p"))
    uniform_p = len(p_field) == 1

    # Accumulate pressure force on the patch
    fx = fy = fz = 0.0
    mx = my = mz = 0.0

    for i in range(n_faces):
        face_idx = start + i
        if face_idx >= len(faces):
            break
        face_verts = [points[vi] for vi in faces[face_idx]]
        ax, ay, az = _face_area_vector(face_verts)

        # Pressure at the cell owning this face
        cell_idx = owner[face_idx]
        p = p_field[0] if uniform_p else p_field[cell_idx]

        # OpenFOAM incompressible p is kinematic (p/rho).
        # Boundary normals point outward from domain (into body).
        # Force on body = p * rho * area_vec
        f_x = p * rho * ax
        f_y = p * rho * ay
        f_z = p * rho * az
        fx += f_x
        fy += f_y
        fz += f_z

        # Face centroid for moment
        cx = sum(v[0] for v in face_verts) / len(face_verts) - cofr[0]
        cy = sum(v[1] for v in face_verts) / len(face_verts) - cofr[1]
        cz = sum(v[2] for v in face_verts) / len(face_verts) - cofr[2]
        mx += cy * f_z - cz * f_y
        my += cz * f_x - cx * f_z
        mz += cx * f_y - cy * f_x

    # Project onto directions
    def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    drag_n = _dot((fx, fy, fz), drag_dir)
    lift_n = _dot((fx, fy, fz), lift_dir)
    moment_nm = _dot((mx, my, mz), pitch_axis)

    q = 0.5 * rho * velocity * velocity
    q_a = q * ref_area if q * ref_area > 1e-30 else 1e-30
    cd = drag_n / q_a
    cl = lift_n / q_a
    cm = moment_nm / (q_a * ref_length) if ref_length > 1e-30 else 0.0

    return {
        "Cd": cd,
        "Cl": cl,
        "Cm": cm,
        "drag_N": drag_n,
        "lift_N": lift_n,
        "moment_Nm": moment_nm,
        "pressure_only": True,
    }


def _find_freecadcmd() -> str | None:
    """Find FreeCADCmd binary on PATH."""
    for name in ("FreeCADCmd", "freecadcmd", "freecad-cmd"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _run_geometry_script(
    script_path: str,
    params: dict[str, Any],
    fixed: dict[str, Any],
    output_stl: str,
    timeout_s: float = 120.0,
) -> None:
    """Run a geometry script in FreeCAD headless mode to produce an STL.

    Raises RuntimeError if the script fails.
    """
    freecadcmd = _find_freecadcmd()
    if not freecadcmd:
        raise RuntimeError("FreeCADCmd not found on PATH")

    # Write merged params to a temp JSON file
    merged = {**fixed, **params}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(merged, f)
        params_path = f.name

    try:
        result = subprocess.run(
            [freecadcmd, script_path, params_path, output_stl],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Geometry script failed (exit {result.returncode}):\n"
                f"stderr: {result.stderr[:500]}"
            )
        if not Path(output_stl).exists():
            raise RuntimeError(f"Geometry script did not produce {output_stl}")
    finally:
        Path(params_path).unlink(missing_ok=True)


class OpenFOAMSolver(SolverAdapter):
    """OpenFOAM CFD solver using FreeCAD headless for geometry generation.

    Pipeline per variant:
    1. Run geometry script in FreeCAD headless (FreeCADCmd) to produce STL
    2. Set up OpenFOAM case directory from template
    3. blockMesh → snappyHexMesh (mesh around STL)
    4. simpleFoam (steady-state RANS) or pimpleFoam (transient)
    5. Extract forces/moments from postProcessing/forces/
    6. Return lift_N, drag_N, moment_Nm, Cl, Cd, etc.

    Required config_params:
        mesh_refinement (int): 1-4, controls mesh density and solve time
        geometry_script (str): path to FreeCAD headless geometry script (set by study)

    Optional config_params:
        turbulence_model (str): "kOmegaSST" (default), "kEpsilon", "SpalartAllmaras"
        n_processors (int): parallel decomposition (default 1)
    """

    # Rough time estimates by mesh refinement level
    _TIME_BY_REFINEMENT: dict[int, float] = {
        1: 120.0,   # ~2 min — coarse mesh, quick feasibility
        2: 300.0,   # ~5 min — standard
        3: 900.0,   # ~15 min — fine mesh
        4: 2400.0,  # ~40 min — very fine
    }

    def name(self) -> str:
        return "openfoam"

    def available(self) -> bool:
        has_openfoam = shutil.which("simpleFoam") is not None
        has_freecad = _find_freecadcmd() is not None
        return has_openfoam and has_freecad

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        level = config_params.get("mesh_refinement", 2)
        # Add ~30s for geometry generation via FreeCAD headless
        return self._TIME_BY_REFINEMENT.get(level, 300.0) + 30.0

    def describe_pipeline(self) -> str:
        return (
            "1. Run geometry script in FreeCAD headless (FreeCADCmd) → STL\n"
            "2. Set up OpenFOAM case directory\n"
            "3. blockMesh + snappyHexMesh (mesh generation around STL)\n"
            "4. simpleFoam RANS simulation\n"
            "5. Extract forces from postProcessing/\n"
            "6. Return aerodynamic coefficients"
        )

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if "mesh_refinement" not in config_params:
            errors.append("mesh_refinement level required in solver params")
        else:
            level = config_params["mesh_refinement"]
            if not isinstance(level, int) or level < 1 or level > 4:
                errors.append("mesh_refinement must be an integer 1-4")
        turb = config_params.get("turbulence_model", "kOmegaSST")
        if turb not in _SUPPORTED_TURBULENCE_MODELS:
            errors.append(
                f"turbulence_model must be one of {_SUPPORTED_TURBULENCE_MODELS}, "
                f"got {turb!r}"
            )
        n_proc = config_params.get("n_processors", 1)
        if n_proc > 1:
            errors.append("n_processors > 1 not yet supported (parallel decomposition)")
        # geometry_script is checked at solve time (set after study.create)
        return errors

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        geometry_script = config_params.get("geometry_script")
        if not geometry_script:
            raise RuntimeError("geometry_script path required in solver config_params")
        if not Path(geometry_script).is_file():
            raise RuntimeError(f"Geometry script not found: {geometry_script}")

        # Create variant working directory
        case_dir = tempfile.mkdtemp(prefix="openfoam_variant_")
        stl_path = str(Path(case_dir) / "geometry.stl")

        try:
            # Step 1: Generate STL via FreeCAD headless
            log.info("Generating geometry via FreeCAD headless...")
            _run_geometry_script(
                script_path=geometry_script,
                params=params,
                fixed=fixed,
                output_stl=stl_path,
                timeout_s=config_params.get("geometry_timeout_s", 120.0),
            )

            # Step 2: Scale STL mm → m and compute domain
            stl_m_path = Path(case_dir) / "constant" / "triSurface" / "geometry.stl"
            stl_m_path.parent.mkdir(parents=True, exist_ok=True)
            _scale_stl_to_meters(stl_path, str(stl_m_path))
            bounds_min, bounds_max = _read_stl_bounds(str(stl_m_path))

            domain = _compute_domain(bounds_min, bounds_max)
            refinement = config_params.get("mesh_refinement", 2)
            mesh_cells = _mesh_cells_by_refinement(refinement, domain)
            max_iters = _MAX_ITERS_BY_REFINEMENT.get(refinement, 1000)

            # Extract flow params
            velocity_mps = fixed.get("velocity_mps", 10.0)
            aoa_deg = fixed.get("angle_of_attack_deg", 0.0)
            rho = fixed.get("rho", 1.225)
            turbulence_model = config_params.get("turbulence_model", "kOmegaSST")

            # Velocity components from angle of attack
            vx = velocity_mps * math.cos(math.radians(aoa_deg))
            vy = 0.0
            vz = velocity_mps * math.sin(math.radians(aoa_deg))

            # Auto-compute reference values from STL bounds if not provided
            ref_length = fixed.get("reference_length_m")
            ref_area = fixed.get("reference_area_m2")
            if ref_length is None:
                ref_length = domain["char_length"]
            if ref_area is None:
                dy = bounds_max[1] - bounds_min[1]
                dz = bounds_max[2] - bounds_min[2]
                ref_area = dy * dz

            # Step 3: Write case directory
            _write_openfoam_case(
                case_dir=case_dir,
                stl_name="geometry.stl",
                domain=domain,
                mesh_cells=mesh_cells,
                velocity=(vx, vy, vz),
                refinement=refinement,
                turbulence_model=turbulence_model,
                ref_length=ref_length,
                ref_area=ref_area,
                rho=rho,
                max_iterations=max_iters,
            )

            # Step 4: Mesh and solve
            mesh_timeout = config_params.get("mesh_timeout_s", 600.0)
            solve_timeout = config_params.get(
                "solve_timeout_s",
                self._TIME_BY_REFINEMENT.get(refinement, 300.0),
            )

            _run_meshing(case_dir, timeout_s=mesh_timeout)
            _run_solver(case_dir, timeout_s=solve_timeout)

            # Step 5: Parse results — compute forces from raw fields
            # (avoids OpenFOAM forces library sha1 bug on some packaged builds)
            return _compute_forces_from_fields(
                case_dir, rho, velocity_mps, ref_area, ref_length,
                lift_dir=(0, 0, 1),
                drag_dir=(1, 0, 0),
            )

        finally:
            # Clean up temp case directory
            if config_params.get("cleanup_cases", True):
                shutil.rmtree(case_dir, ignore_errors=True)


class ChronoSolver(SolverAdapter):
    """Multibody dynamics solver via the Chrono daemon subprocess.

    Builds a mechanism from study params + fixed_params, sends it to the
    Chrono daemon for time-domain simulation, and extracts performance metrics.

    Required fixed_params:
        mechanism_template (dict): Base mechanism dict (parts, joints, drives).
            Study params override specific fields (e.g., gear teeth, RPM).
        duration_s (float): Simulation duration (default 1.0)

    The solver maps study params onto the mechanism template before simulation.
    For example, a param "sun_teeth" would update the sun gear's teeth count
    in the mechanism template.
    """

    def name(self) -> str:
        return "chrono"

    def available(self) -> bool:
        try:
            from server.chrono_client import ChronoClient
            client = ChronoClient()
            client.connect(timeout=1.0)
            ok = client.ping()
            client.disconnect()
            return ok
        except Exception:
            return False

    def estimate_per_variant_s(self, config_params: dict[str, Any]) -> float:
        duration = config_params.get("duration_s", 1.0)
        # Rough estimate: 10× real-time for typical MBS
        return duration * 10.0 + 2.0

    def describe_pipeline(self) -> str:
        return (
            "1. Build mechanism definition from params + template\n"
            "2. Send to Chrono daemon via TCP\n"
            "3. Run time-domain MBS simulation\n"
            "4. Extract peak torques, steady-state speeds, efficiency"
        )

    def validate_params(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if "mechanism_template" not in fixed:
            errors.append("mechanism_template required in fixed_params")
        return errors

    def solve(
        self,
        params: dict[str, Any],
        fixed: dict[str, Any],
        config_params: dict[str, Any],
    ) -> dict[str, float]:
        from server.chrono_client import ChronoClient, ChronoConnectionError

        template = fixed.get("mechanism_template", {})
        if not template:
            raise RuntimeError("mechanism_template required in fixed_params")

        # Deep copy and apply study params to the mechanism template
        import copy
        mechanism = copy.deepcopy(template)

        # Apply param overrides: e.g., "sun_teeth" → find joint with sun, update teeth
        # This is a simple key-matching approach; the LLM constructs the template
        # with param names that map directly to mechanism fields.
        for key, value in params.items():
            mechanism.setdefault("_study_params", {})[key] = value

        duration_s = fixed.get("duration_s", config_params.get("duration_s", 1.0))

        client = ChronoClient()
        try:
            client.connect(timeout=5.0)
            result = client.simulate(
                mechanism=mechanism,
                duration_s=duration_s,
                dt_s=config_params.get("dt_s", 0.001),
            )
        except ChronoConnectionError as exc:
            raise RuntimeError(f"Chrono daemon not available: {exc}") from exc
        finally:
            client.disconnect()

        # Extract metrics from summary
        summary = result.get("summary", {})
        metrics: dict[str, float] = {}

        peak_torques = summary.get("peak_torques", {})
        for part_id, torque in peak_torques.items():
            metrics[f"peak_torque_{part_id}_nm"] = float(torque)

        speeds = summary.get("steady_state_speeds", {})
        for part_id, rpm in speeds.items():
            metrics[f"speed_{part_id}_rpm"] = float(rpm)

        if "overall_efficiency" in summary:
            metrics["efficiency"] = float(summary["overall_efficiency"])

        return metrics


# ---------------------------------------------------------------------------
# Solver registry
# ---------------------------------------------------------------------------

SOLVERS: dict[str, SolverAdapter] = {
    "mock": MockSolver(),
    "bemt_xfoil": BEMTXfoilSolver(),
    "openfoam": OpenFOAMSolver(),
    "chrono": ChronoSolver(),
}


def get_solver(solver_type: str) -> SolverAdapter:
    """Look up a solver by type string. Raises KeyError if unknown."""
    if solver_type not in SOLVERS:
        raise KeyError(f"Unknown solver type: {solver_type!r}. Available: {list(SOLVERS)}")
    return SOLVERS[solver_type]
