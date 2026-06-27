"""Cross-category coupling: simulation results → FEA boundary conditions.

Converts force/torque data from motion.propagate_motion or motion.simulate
into boundary conditions suitable for analysis.stress_check.

This module is the bridge between dynamics simulation (motion.*) and
structural analysis (analysis.*).
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("solidmind.analysis_sim_coupling")


def bcs_from_propagation(
    propagation_result: dict[str, Any],
    body: str,
    fixed_faces: list[str],
    load_faces: list[str],
    load_direction: tuple[float, float, float] = (0.0, 0.0, -1.0),
) -> list[dict[str, Any]]:
    """Build FEA boundary conditions from motion.propagate_motion results.

    Uses analytical torque data (Tier 1) to derive equivalent forces on faces.

    Parameters
    ----------
    propagation_result : dict
        Result from ``motion_propagate_motion()``.  Must contain
        ``states[body].torque_nm``.
    body : str
        Part/body ID to extract torque for.
    fixed_faces : list[str]
        Face references to constrain (e.g. ["Face1"]).
    load_faces : list[str]
        Face references where the load is applied.
    load_direction : tuple
        Unit vector for load direction (default: -Z gravity).

    Returns
    -------
    list[dict]
        Boundary condition dicts ready for ``analysis.stress_check``.
    """
    states = propagation_result.get("states", {})
    part_state = states.get(body, {})
    torque_nm = part_state.get("torque_nm", 0.0)

    bcs: list[dict[str, Any]] = []

    # Fixed support
    if fixed_faces:
        bcs.append({
            "bc_type": "fixed",
            "faces": fixed_faces,
        })

    # Apply torque as equivalent tangential force
    # For a shaft with radius r: F = T / r
    # Default to 10mm radius if not specified — caller should override
    if load_faces and abs(torque_nm) > 1e-9:
        dx, dy, dz = load_direction
        mag = math.sqrt(dx**2 + dy**2 + dz**2)
        if mag > 0:
            dx, dy, dz = dx / mag, dy / mag, dz / mag

        # Convert torque to force (N). torque_nm is in N·m.
        # Use the torque directly as a moment if the solver supports it,
        # otherwise convert to equivalent force at a reference radius.
        force_n = torque_nm * 1000  # N·m → N·mm then /1mm = N at 1mm radius
        # Scale: for a typical gear/shaft, use torque as-is in N
        # The actual force depends on geometry — this provides a conservative estimate
        bcs.append({
            "bc_type": "force",
            "faces": load_faces,
            "value": {
                "fx": round(force_n * dx, 4),
                "fy": round(force_n * dy, 4),
                "fz": round(force_n * dz, 4),
            },
        })

    return bcs


def bcs_from_simulation(
    simulation_result: dict[str, Any],
    body: str,
    fixed_faces: list[str],
    load_faces: list[str],
    load_direction: tuple[float, float, float] = (0.0, 0.0, -1.0),
    joint_index: int | None = None,
    safety_factor: float = 1.5,
) -> list[dict[str, Any]]:
    """Build FEA boundary conditions from motion.simulate results.

    Uses peak joint efforts from dynamic simulation (Tier 3) to derive
    forces for FEA.  Falls back to analytical torques from the summary
    if effort data is not available.

    Parameters
    ----------
    simulation_result : dict
        Result from ``motion_simulate()``.  Should contain
        ``summary.peak_joint_forces`` and/or ``time_series[].joint_efforts``.
    body : str
        Body label for the part being analyzed.
    fixed_faces : list[str]
        Face references for fixed supports.
    load_faces : list[str]
        Face references where load is applied.
    load_direction : tuple
        Unit vector for load direction.
    joint_index : int | None
        If specified, use efforts from this joint index.
    safety_factor : float
        Multiply peak force by this factor (default 1.5).

    Returns
    -------
    list[dict]
        Boundary condition dicts for ``analysis.stress_check``.
    """
    bcs: list[dict[str, Any]] = []

    # Fixed support
    if fixed_faces:
        bcs.append({
            "bc_type": "fixed",
            "faces": fixed_faces,
        })

    # Try to get peak force from simulation
    summary = simulation_result.get("summary", {})
    peak_forces = summary.get("peak_joint_forces", {})

    force_n = 0.0

    if joint_index is not None and peak_forces:
        key = f"joint_{joint_index}"
        force_n = float(peak_forces.get(key, 0.0))
    elif peak_forces:
        # Use maximum across all joints
        force_n = max(float(v) for v in peak_forces.values()) if peak_forces else 0.0

    # Fallback: extract from time series
    if force_n == 0.0:
        time_series = simulation_result.get("time_series", [])
        for sample in time_series:
            efforts = sample.get("joint_efforts", [])
            if efforts:
                if joint_index is not None and joint_index < len(efforts):
                    val = abs(float(efforts[joint_index]))
                else:
                    val = max(abs(float(e)) for e in efforts) if efforts else 0.0
                force_n = max(force_n, val)

    # Fallback: use steady-state speed + mechanism data to estimate
    if force_n == 0.0:
        ss_speeds = summary.get("steady_state_speeds", {})
        if body in ss_speeds:
            # Rough estimate: can't compute force without torque/radius
            # Return empty load — caller should use propagation instead
            log.warning(
                "No force data in simulation result for body %s. "
                "Use motion.propagate_motion for analytical torque estimation.",
                body,
            )

    # Apply safety factor and direction
    if load_faces and force_n > 0:
        force_n *= safety_factor
        dx, dy, dz = load_direction
        mag = math.sqrt(dx**2 + dy**2 + dz**2)
        if mag > 0:
            dx, dy, dz = dx / mag, dy / mag, dz / mag

        bcs.append({
            "bc_type": "force",
            "faces": load_faces,
            "value": {
                "fx": round(force_n * dx, 4),
                "fy": round(force_n * dy, 4),
                "fz": round(force_n * dz, 4),
            },
        })

    return bcs


def summarize_sim_forces(
    simulation_result: dict[str, Any],
) -> dict[str, Any]:
    """Extract a force summary from simulation results for display.

    Works with results from any backend (Isaac, Gazebo, Chrono).
    """
    summary = simulation_result.get("summary", {})
    backend = simulation_result.get("backend_used", "unknown")
    time_series = simulation_result.get("time_series", [])

    peak_forces = summary.get("peak_joint_forces", {})

    # If no peak forces in summary, compute from time series
    if not peak_forces and time_series:
        peaks: dict[int, float] = {}
        for sample in time_series:
            efforts = sample.get("joint_efforts", [])
            for i, e in enumerate(efforts):
                val = abs(float(e))
                if i not in peaks or val > peaks[i]:
                    peaks[i] = val
        peak_forces = {f"joint_{i}": round(v, 4) for i, v in sorted(peaks.items())}

    # Analytical torques from propagation states
    states = simulation_result.get("states", {})
    part_torques: dict[str, float] = {}
    for part_id, state in states.items():
        if isinstance(state, dict) and "torque_nm" in state:
            part_torques[part_id] = state["torque_nm"]

    return {
        "backend": backend,
        "has_joint_efforts": bool(peak_forces),
        "peak_joint_forces": peak_forces,
        "has_analytical_torques": bool(part_torques),
        "part_torques_nm": part_torques,
        "num_timesteps": len(time_series),
    }
