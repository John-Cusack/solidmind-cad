"""Parameterized PX4 airframe generator for FreeCAD-built drones.

Phase 4 of the PX4 SITL platform.  Takes a ``SimModel`` plus the
``drone_config["rotors"]`` list (per-rotor body-frame X/Y plus
turning direction) and produces a PX4 airframe init shell script
that can be dropped into ``ROMFS/px4fmu_common/init.d-posix/airframes/``
to give the drone its own ``SYS_AUTOSTART`` ID with motor allocation
matrix, hover throttle, and seeded PID gains derived from the
geometry.

The generator is split into three concerns:

1. **Math** — rotor extraction, arm length, hover throttle, PID seeds,
   stable SYS_AUTOSTART hashing.  Pure functions, easy to unit-test.
2. **Format** — render an ``AirframeParams`` instance as the shell
   script PX4 reads at boot.
3. **Registration** — write the script to a configurable PX4 install
   path.  Side-effect-only; mockable in tests.

The reference airframe is X500 (``ROMFS/.../airframes/4001_gz_x500``).
PID and hover-throttle defaults are scaled from X500 so a drone with
similar mass and arm length produces near-identical params, while a
larger or smaller drone gets sensibly adjusted gains.

After this generator drops a new file, PX4 must be rebuilt
(``make px4_sitl <airframe_name>``) for the airframe to be available
to ``PX4_SIM_MODEL``.  Phase 5 will automate the rebuild step; for
now it's the operator's responsibility.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- Reference airframe (X500) constants --------------------------------
# These are used as the baseline for scaling PID gains and hover throttle
# to drones with different mass/arm length.  Source:
# PX4-Autopilot/ROMFS/px4fmu_common/init.d-posix/airframes/4001_gz_x500
# and PX4-Autopilot/ROMFS/px4fmu_common/init.d/rc.mc_defaults.
_X500_MASS_KG = 2.0
# Mean radial distance from origin to rotor centres in the stock X500
# layout (front rotors at PY=±0.22 m, rear rotors at PY=±0.20 m, all at
# PX=±0.13 m).  Computed from the 4001_gz_x500 airframe init script.
_X500_ARM_LENGTH_M = 0.2470
_X500_MC_ROLLRATE_P = 0.15
_X500_MC_PITCHRATE_P = 0.15
_X500_MC_YAWRATE_P = 0.20
_X500_HOVER_THROTTLE = 0.60

# Default motor parameters that align with the canonical Gazebo
# multicopter motor model plugin defaults.  Phase 4's airframe generator
# uses these unless BEMT data provides per-drone overrides.
_DEFAULT_MOTOR_CONSTANT = 8.54858e-06    # N*s²
_DEFAULT_MOMENT_CONSTANT = 0.05          # ratio (yaw torque / thrust)
_DEFAULT_MAX_ROT_VELOCITY = 1000.0       # rad/s

_GRAVITY_MS2 = 9.81

# SYS_AUTOSTART range reserved for SolidMind-generated airframes.
# Stock PX4 airframes use IDs below 50000; we keep ourselves out of
# that range to avoid collisions with future PX4 releases.
_AUTOSTART_BASE = 50000
_AUTOSTART_RANGE = 1000


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


class AirframeGeneratorError(Exception):
    """Raised when an airframe params file cannot be generated."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------
# Data shapes
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RotorParams:
    """One rotor's body-frame position + direction, ready for PX4 CA params."""

    px_m: float
    py_m: float
    pz_m: float = 0.0
    direction: int = 1  # +1 = ccw, -1 = cw
    moment_constant: float = _DEFAULT_MOMENT_CONSTANT
    motor_constant: float = _DEFAULT_MOTOR_CONSTANT
    max_rot_velocity: float = _DEFAULT_MAX_ROT_VELOCITY

    @property
    def km_signed(self) -> float:
        """``CA_ROTOR{N}_KM`` = direction × moment_constant."""
        return float(self.direction) * float(self.moment_constant)


@dataclass(slots=True, frozen=True)
class AirframeParams:
    """All the values a PX4 airframe init script needs.

    Constructed by ``generate_airframe_params``; consumed by
    ``format_airframe_init_script``.
    """

    name: str
    sys_autostart: int
    rotors: tuple[RotorParams, ...]
    mass_kg: float
    arm_length_m: float
    hover_throttle: float
    motor_min: int = 150
    motor_max: int = 1000
    mc_rollrate_p: float = _X500_MC_ROLLRATE_P
    mc_pitchrate_p: float = _X500_MC_PITCHRATE_P
    mc_yawrate_p: float = _X500_MC_YAWRATE_P

    @property
    def rotor_count(self) -> int:
        return len(self.rotors)


# ----------------------------------------------------------------------
# Math: SYS_AUTOSTART
# ----------------------------------------------------------------------


def compute_sys_autostart(model_name: str) -> int:
    """Stable hash of ``model_name`` into the SolidMind autostart range.

    Same input always produces the same ID, so re-generating an airframe
    keeps the same SYS_AUTOSTART (and PX4 doesn't get confused).  Range
    is ``[50000, 50999]`` — disjoint from PX4's stock airframes.
    """
    if not model_name:
        raise AirframeGeneratorError(
            "model_name must be non-empty", code="INVALID_MODEL_NAME",
        )
    digest = hashlib.sha1(model_name.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % _AUTOSTART_RANGE
    return _AUTOSTART_BASE + bucket


# ----------------------------------------------------------------------
# Math: rotor extraction
# ----------------------------------------------------------------------


def _normalize_direction(value: Any) -> int:
    """Accept 'ccw'/'cw'/+1/-1 and normalize to +1 (ccw) or -1 (cw)."""
    if value in ("ccw", "CCW", 1, "1", True):
        return 1
    if value in ("cw", "CW", -1, "-1"):
        return -1
    return 1


def extract_rotors(
    drone_config: dict[str, Any],
    sim_model: Any | None = None,
) -> tuple[RotorParams, ...]:
    """Build PX4 ``RotorParams`` from the ``drone_config["rotors"]`` list.

    Rotor positions are read from the rotor entry's ``position_m`` field
    (preferred), or computed from the linked link's placement in
    ``sim_model``.  When neither is available, raises.

    Frame convention
    ----------------
    Input ``position_m`` is in **Gazebo FLU body frame** (X forward,
    Y left, Z up) — the same convention used by SDF ``<pose>`` and by
    :class:`server.airframes.multicopter.Rotor`.  PX4's CA_ROTOR
    parameters are in **FRD body frame** (Y right, Z down), so this
    function negates Y and Z when emitting :class:`RotorParams`.  An
    FLU rotor at (0.13, -0.22, +0.06) becomes CA_ROTOR with
    PX=0.13, PY=+0.22, PZ=-0.06 — matching PX4's stock x500 layout.

    Without this conversion the mixer's moment-arm matrix is the
    mirror of the physical drone, the allocator can't solve for
    pure-thrust takeoff, and motor outputs saturate to zero — the
    drone arms but never lifts.

    Direction is normalized to ±1 (PX4 convention).  Per-rotor motor
    parameters (``motor_constant``, ``max_rot_velocity``) are inherited
    from the drone_config entry if provided, else use canonical defaults
    matching ``server.sim_export``'s SDF emission.
    """
    rotor_entries = drone_config.get("rotors") or []
    if not isinstance(rotor_entries, list) or not rotor_entries:
        raise AirframeGeneratorError(
            "drone_config['rotors'] must be a non-empty list",
            code="NO_ROTORS",
        )

    link_position_lookup: dict[str, tuple[float, float, float]] = {}
    if sim_model is not None:
        for link in getattr(sim_model, "links", []):
            # SimLink positions are in mm; convert to meters here.
            pos = getattr(link, "position", None)
            if pos is None:
                continue
            link_position_lookup[link.name] = (
                float(pos[0]) / 1000.0,
                float(pos[1]) / 1000.0,
                float(pos[2]) / 1000.0,
            )

    joint_to_child: dict[str, str] = {}
    if sim_model is not None:
        for joint in getattr(sim_model, "joints", []):
            joint_to_child[joint.name] = joint.child

    out: list[RotorParams] = []
    for idx, entry in enumerate(rotor_entries):
        if not isinstance(entry, dict):
            continue

        # Body-frame position: preferred via explicit position_m;
        # fallback via SimModel link lookup.
        pos_m = entry.get("position_m") or entry.get("position")
        if pos_m is None:
            joint_name = str(entry.get("joint", ""))
            link_name = entry.get("link") or joint_to_child.get(joint_name)
            if link_name and link_name in link_position_lookup:
                pos_m = link_position_lookup[link_name]
        if pos_m is None:
            raise AirframeGeneratorError(
                f"rotor {idx}: cannot resolve position. Provide "
                "'position_m' or supply a SimModel that contains the "
                "rotor's child link.",
                code="ROTOR_POSITION_MISSING",
            )

        # FLU (drone_config / SDF) → FRD (PX4 CA_ROTOR): negate Y, Z.
        px = float(pos_m[0])
        py = -float(pos_m[1])
        pz = -float(pos_m[2]) if len(pos_m) > 2 else 0.0

        out.append(RotorParams(
            px_m=px,
            py_m=py,
            pz_m=pz,
            direction=_normalize_direction(entry.get("direction", "ccw")),
            moment_constant=float(
                entry.get("moment_constant", _DEFAULT_MOMENT_CONSTANT),
            ),
            motor_constant=float(
                entry.get("motor_constant", _DEFAULT_MOTOR_CONSTANT),
            ),
            max_rot_velocity=float(
                entry.get("max_rot_velocity", _DEFAULT_MAX_ROT_VELOCITY),
            ),
        ))

    return tuple(out)


# ----------------------------------------------------------------------
# Math: arm length, hover throttle, PID seeds
# ----------------------------------------------------------------------


def compute_arm_length(rotors: tuple[RotorParams, ...]) -> float:
    """Mean radial distance from origin to the rotor tips, in metres."""
    if not rotors:
        raise AirframeGeneratorError(
            "Cannot compute arm length with zero rotors", code="NO_ROTORS",
        )
    radii = [
        math.hypot(r.px_m, r.py_m)
        for r in rotors
    ]
    return sum(radii) / len(radii)


def compute_total_mass(sim_model: Any) -> float:
    """Sum of ``link.mass_kg`` across the ``SimModel``."""
    total = 0.0
    for link in getattr(sim_model, "links", []):
        m = getattr(link, "mass_kg", None)
        if m is not None:
            total += float(m)
    if total <= 0.0:
        raise AirframeGeneratorError(
            "SimModel has no mass — every link should set mass_kg",
            code="NO_MASS",
        )
    return total


def compute_hover_throttle(
    mass_kg: float,
    rotors: tuple[RotorParams, ...],
    *,
    motor_min_rot_velocity: float = 150.0,
) -> float:
    """Fraction of full-throttle command required to balance gravity.

    PX4's ``MPC_THR_HOVER`` is a normalized 0..1 throttle command —
    ``0`` maps to ``SIM_GZ_EC_MIN`` (motor idle, ~150 rad/s) and ``1``
    maps to ``SIM_GZ_EC_MAX`` (= ``max_rot_velocity``).  Throttle to
    motor angular velocity is **linear**::

        ω(throttle) = ω_min + throttle · (ω_max − ω_min)

    But the Gazebo MulticopterMotorModel plugin produces thrust
    **quadratically** in ω (``T = K·ω²``).  Solving for the throttle
    that balances total weight on ``N`` identical rotors::

        ω_hover = sqrt(m·g / (N · K))
        throttle_hover = (ω_hover − ω_min) / (ω_max − ω_min)

    The legacy formula was ``m·g / (N · K · ω_max²)`` — that's
    ``(ω_hover/ω_max)²``, which equals the correct value only at
    ``throttle = 1.0`` (motor at max).  Everywhere else it under-
    estimates the hover throttle, leaving PX4 perpetually under-
    powered during AUTO_TAKEOFF.  For a 1.7 kg drone with default
    motor constants the legacy formula returned 0.488; the correct
    value is 0.61.

    Raises ``AirframeGeneratorError`` if the drone is too heavy
    (``ω_hover > ω_max``) or unrealistically light
    (``ω_hover < ω_min``) — both indicate the spec needs operator
    attention, not silent clipping.
    """
    if not rotors:
        raise AirframeGeneratorError("no rotors", code="NO_ROTORS")
    if mass_kg <= 0.0:
        raise AirframeGeneratorError("mass must be > 0", code="INVALID_MASS")

    # All canonical multirotors have identical rotors; use the first.
    K = rotors[0].motor_constant
    omega_max = rotors[0].max_rot_velocity
    omega_min = motor_min_rot_velocity
    n = len(rotors)
    if K <= 0.0 or omega_max <= 0.0:
        raise AirframeGeneratorError(
            "rotor motor_constant and max_rot_velocity must be > 0",
            code="INVALID_MOTOR_CONSTANTS",
        )

    omega_hover_sq = mass_kg * _GRAVITY_MS2 / (n * K)
    omega_hover = math.sqrt(omega_hover_sq)
    if omega_hover > omega_max:
        raise AirframeGeneratorError(
            f"drone too heavy: needs ω={omega_hover:.0f} rad/s to hover, "
            f"but rotor max is {omega_max:.0f}. Reduce mass or pick "
            "stronger motors.",
            code="HOVER_INFEASIBLE",
        )
    if omega_hover < omega_min:
        raise AirframeGeneratorError(
            f"drone too light: needs ω={omega_hover:.0f} rad/s to hover, "
            f"but rotor min is {omega_min:.0f}. Reduce motor_constant or "
            "lower SIM_GZ_EC_MIN.",
            code="HOVER_BELOW_MIN_VELOCITY",
        )
    return (omega_hover - omega_min) / (omega_max - omega_min)


def seed_pid_gains(
    *, mass_kg: float, arm_length_m: float, rotor_count: int,
) -> dict[str, float]:
    """Return rate-controller P-gain seeds scaled from the X500 baseline.

    Physical reasoning: for similar T/W and motor characteristics,
    rate-controller bandwidth scales inversely with arm length
    (inertia ∝ m·L², per-motor torque ∝ F·L, ratio ∝ 1/L).  In
    practice though, gains also depend on prop inertia, motor
    latency, and the chassis's actual inertia tensor — none of which
    we model.  Empirically, both pure-linear and pure-inverse
    scaling overshoot for drones meaningfully different from x500.

    We pick a compromise: **square-root inverse** scaling.  This
    halves the dynamic range vs. pure 1/L, getting "in the right
    neighborhood" for drones from 0.13 m to 0.5 m arm length, while
    being close enough to ``x500`` defaults that auto-tune can
    refine the rest:

        P_roll_new = P_x500 × √(arm_x500 / arm_new)
        P_yaw_new  = P_x500 × (arm_x500 / arm_new)^(1/4)

    For the SolidMind reference quadrotor (0.35 m arm vs x500's
    0.247 m), this gives P_roll = 0.126 — only a small reduction
    from x500's 0.15, meaning the seed is conservative.  Operators
    are expected to run PX4 auto-tune for any drone that diverges
    significantly from x500.
    """
    if arm_length_m <= 0.0:
        raise AirframeGeneratorError(
            "arm_length must be > 0", code="INVALID_ARM_LENGTH",
        )
    # Square-root inverse: gentler than pure 1/L, which over-corrects
    # in both directions when the dynamic range is large.
    scale = math.sqrt(_X500_ARM_LENGTH_M / arm_length_m)
    return {
        "mc_rollrate_p": _X500_MC_ROLLRATE_P * scale,
        "mc_pitchrate_p": _X500_MC_PITCHRATE_P * scale,
        "mc_yawrate_p": _X500_MC_YAWRATE_P * math.sqrt(scale),
    }


# ----------------------------------------------------------------------
# Top-level: generate
# ----------------------------------------------------------------------


def generate_airframe_params(
    *,
    model_name: str,
    sim_model: Any | None = None,
    drone_config: dict[str, Any],
    mass_kg_override: float | None = None,
) -> AirframeParams:
    """Produce a populated ``AirframeParams`` from sim model + config.

    ``model_name`` is the human-readable identifier — used both for the
    airframe filename and the stable SYS_AUTOSTART hash.  ``sim_model``
    supplies mass; can be omitted if ``mass_kg_override`` is given (e.g.
    in tests where SimModel is too heavy to construct).
    """
    rotors = extract_rotors(drone_config, sim_model=sim_model)
    arm_length = compute_arm_length(rotors)

    if mass_kg_override is not None:
        mass_kg = float(mass_kg_override)
    else:
        if sim_model is None:
            raise AirframeGeneratorError(
                "either sim_model or mass_kg_override required",
                code="NO_MASS_SOURCE",
            )
        mass_kg = compute_total_mass(sim_model)

    hover = compute_hover_throttle(mass_kg, rotors)
    pid = seed_pid_gains(
        mass_kg=mass_kg, arm_length_m=arm_length, rotor_count=len(rotors),
    )
    sys_autostart = compute_sys_autostart(model_name)

    return AirframeParams(
        name=model_name,
        sys_autostart=sys_autostart,
        rotors=rotors,
        mass_kg=mass_kg,
        arm_length_m=arm_length,
        hover_throttle=hover,
        mc_rollrate_p=pid["mc_rollrate_p"],
        mc_pitchrate_p=pid["mc_pitchrate_p"],
        mc_yawrate_p=pid["mc_yawrate_p"],
    )


# ----------------------------------------------------------------------
# Format: render the init script
# ----------------------------------------------------------------------


_NAME_SANITIZER = re.compile(r"[^A-Za-z0-9_]+")


def _sanitize_name(name: str) -> str:
    """Make ``name`` safe for use as a PX4 airframe filename component."""
    cleaned = _NAME_SANITIZER.sub("_", name).strip("_").lower()
    return cleaned or "drone"


def format_airframe_init_script(params: AirframeParams) -> str:
    """Render an ``AirframeParams`` as a PX4 airframe init shell script.

    Output is a ``/bin/sh`` script in the same idiom as PX4's stock
    airframe files: shell variable defaults at the top, then a
    sequence of ``param set-default`` lines, ending with the
    standard ``NAV_DLL_ACT 2`` (data-link-loss → land) directive.
    """
    sanitized = _sanitize_name(params.name)
    rotor_block = []
    for idx, rotor in enumerate(params.rotors):
        rotor_block.append(
            f"param set-default CA_ROTOR{idx}_PX {rotor.px_m:.6g}"
        )
        rotor_block.append(
            f"param set-default CA_ROTOR{idx}_PY {rotor.py_m:.6g}"
        )
        if rotor.pz_m != 0.0:
            rotor_block.append(
                f"param set-default CA_ROTOR{idx}_PZ {rotor.pz_m:.6g}"
            )
        rotor_block.append(
            f"param set-default CA_ROTOR{idx}_KM {rotor.km_signed:.6g}"
        )
        rotor_block.append("")

    sim_gz_block = []
    for idx in range(params.rotor_count):
        sim_gz_block.append(
            f"param set-default SIM_GZ_EC_FUNC{idx + 1} {101 + idx}"
        )
    sim_gz_block.append("")
    for idx in range(params.rotor_count):
        sim_gz_block.append(
            f"param set-default SIM_GZ_EC_MIN{idx + 1} {params.motor_min}"
        )
    sim_gz_block.append("")
    for idx in range(params.rotor_count):
        sim_gz_block.append(
            f"param set-default SIM_GZ_EC_MAX{idx + 1} {params.motor_max}"
        )

    rotor_lines = "\n".join(rotor_block)
    sim_gz_lines = "\n".join(sim_gz_block)

    return f"""#!/bin/sh
#
# @name SolidMind {params.name}
#
# @type Multirotor
#
# Generated by server.px4_airframe_generator.
# SYS_AUTOSTART = {params.sys_autostart}
# Mass = {params.mass_kg:.3f} kg, arm length = {params.arm_length_m:.3f} m,
# rotor count = {params.rotor_count}, hover throttle = {params.hover_throttle:.3f}.
#

. ${{R}}etc/init.d/rc.mc_defaults

PX4_SIMULATOR=${{PX4_SIMULATOR:=gz}}
PX4_GZ_WORLD=${{PX4_GZ_WORLD:=default}}
PX4_SIM_MODEL=${{PX4_SIM_MODEL:=gz_{sanitized.removeprefix("gz_")}}}

param set-default SIM_GZ_EN 1

param set-default CA_AIRFRAME 0
param set-default CA_ROTOR_COUNT {params.rotor_count}

{rotor_lines}
{sim_gz_lines}

param set-default MPC_THR_HOVER {params.hover_throttle:.3f}

param set-default MC_ROLLRATE_P {params.mc_rollrate_p:.4f}
param set-default MC_PITCHRATE_P {params.mc_pitchrate_p:.4f}
param set-default MC_YAWRATE_P {params.mc_yawrate_p:.4f}

param set-default NAV_DLL_ACT 2
"""


# ----------------------------------------------------------------------
# Registration: write the file
# ----------------------------------------------------------------------


def _resolve_install_path(install_path: Path | str | None) -> Path:
    """Resolve the PX4 install directory.

    Order of resolution:
    1. Explicit argument
    2. ``SOLIDMIND_PX4_INSTALL`` env var
    3. Default: ``~/repos/PX4-Autopilot``
    """
    if install_path is not None:
        return Path(install_path).expanduser()
    env = os.environ.get("SOLIDMIND_PX4_INSTALL")
    if env:
        return Path(env).expanduser()
    return Path.home() / "repos" / "PX4-Autopilot"


def airframe_filename(params: AirframeParams) -> str:
    """``<sys_autostart>_gz_<sanitized_name>`` — PX4 Gazebo convention.

    PX4's ``src/modules/simulation/gz_bridge/CMakeLists.txt`` discovers
    Gazebo-targeted airframes via the glob ``init.d-posix/airframes/*_gz_*``
    and the regex ``string(REGEX REPLACE ".*_gz_" "" model_name ...)``.
    Without the ``_gz_`` infix the auto-generated ``make px4_sitl gz_<model>``
    target never gets created.

    To stay idempotent, names that already contain ``gz_`` (e.g. when
    the caller pre-prefixes the model with the convention) are passed
    through unchanged.
    """
    sanitized = _sanitize_name(params.name)
    if not sanitized.startswith("gz_"):
        sanitized = f"gz_{sanitized}"
    return f"{params.sys_autostart}_{sanitized}"


def register_airframe(
    params: AirframeParams,
    *,
    install_path: Path | str | None = None,
    overwrite: bool = True,
) -> Path:
    """Write the airframe init script to PX4's airframes directory.

    Returns the absolute path of the written file.  Raises
    ``AirframeGeneratorError`` if the install path doesn't look like a
    PX4-Autopilot checkout.

    PX4 must be rebuilt (``make px4_sitl <airframe_name>``) for the new
    airframe to be available to ``PX4_SIM_MODEL``.  The bridge's
    ``Px4Manager.start`` does not auto-rebuild — operator's responsibility
    until Phase 5 wires that.
    """
    base = _resolve_install_path(install_path)
    airframes_dir = (
        base / "ROMFS" / "px4fmu_common" / "init.d-posix" / "airframes"
    )
    if not airframes_dir.is_dir():
        raise AirframeGeneratorError(
            f"PX4 airframes directory not found at {airframes_dir}. "
            "Set SOLIDMIND_PX4_INSTALL or pass install_path.",
            code="PX4_INSTALL_NOT_FOUND",
        )

    filename = airframe_filename(params)
    out_path = airframes_dir / filename
    if out_path.exists() and not overwrite:
        raise AirframeGeneratorError(
            f"Airframe already exists: {out_path}",
            code="AIRFRAME_EXISTS",
        )

    content = format_airframe_init_script(params)
    out_path.write_text(content, encoding="utf-8")
    out_path.chmod(0o755)
    return out_path
