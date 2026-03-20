"""Map frozen interface loads + coordinate frames to CalculiX boundary conditions."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from orchestrator.spec import CoordinateFrame, Interface, LoadCase, Subsystem


@dataclass(slots=True)
class FEABoundaryCondition:
    """A single CalculiX boundary condition."""

    node_set_name: str
    bc_type: str  # "fixed" | "force" | "moment"
    values: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


def extract_mesh_nodes(inp_path: str | object) -> dict[int, tuple[float, float, float]]:
    """Parse *NODE section from a Gmsh-generated .inp file.

    Returns {node_id: (x, y, z)}.
    """
    from pathlib import Path
    path = Path(inp_path)
    nodes: dict[int, tuple[float, float, float]] = {}
    in_node_section = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("*NODE"):
            in_node_section = True
            continue
        if stripped.startswith("*") and in_node_section:
            break
        if in_node_section and stripped and not stripped.startswith("**"):
            parts = stripped.split(",")
            if len(parts) >= 4:
                nid = int(parts[0].strip())
                x = float(parts[1].strip())
                y = float(parts[2].strip())
                z = float(parts[3].strip())
                nodes[nid] = (x, y, z)
    return nodes


def find_nodes_near_frame(
    nodes: dict[int, tuple[float, float, float]],
    frame: CoordinateFrame,
    radius_mm: float,
) -> list[int]:
    """Return node IDs within radius_mm of frame origin."""
    ox, oy, oz = frame.origin_mm
    r2 = radius_mm * radius_mm
    result = []
    for nid, (x, y, z) in nodes.items():
        dx, dy, dz = x - ox, y - oy, z - oz
        if dx * dx + dy * dy + dz * dz <= r2:
            result.append(nid)
    return result


def _load_magnitude(lc: LoadCase) -> float:
    """Sum of absolute load values for a load case."""
    return (
        abs(lc.torque_nm)
        + abs(lc.axial_force_n)
        + abs(lc.radial_force_n)
        + abs(lc.bending_moment_nm)
    )


def map_interface_bcs(
    subsystem: Subsystem,
    interfaces: list[Interface],
    mesh_nodes: dict[int, tuple[float, float, float]],
    search_radius_mm: float = 5.0,
) -> list[FEABoundaryCondition]:
    """Map frozen interface loads to CalculiX boundary conditions.

    Heuristics:
    - If only one interface: cantilever — one end fixed, other loaded
    - If interface loads are all zero: treat as fixed support
    - Otherwise: apply loads from LoadCase

    Load contract:
    - Interface loads are interpreted as total loads at the interface.
    - For nodal BC export, total force/moment-equivalent force is distributed
      across the selected interface node set.
    """
    bcs: list[FEABoundaryCondition] = []

    for ifc in interfaces:
        # Determine which frame applies to this subsystem
        if ifc.subsystem_a == subsystem.name:
            frame = ifc.frame_a
        elif ifc.subsystem_b == subsystem.name:
            frame = ifc.frame_b
        else:
            continue

        # Find nearby nodes
        near_nodes = find_nodes_near_frame(mesh_nodes, frame, search_radius_mm)
        if not near_nodes:
            continue

        set_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"ifc_{ifc.id}")

        # Determine if fixed or loaded
        total_load = sum(_load_magnitude(lc) for lc in ifc.loads)

        if total_load < 1e-9:
            # No loads — fixed support
            bcs.append(FEABoundaryCondition(
                node_set_name=set_name,
                bc_type="fixed",
                values=[0.0, 0.0, 0.0],
            ))
        else:
            # Apply loads from each load case
            for lc in ifc.loads:
                if _load_magnitude(lc) < 1e-9:
                    continue
                ax = frame.axis_x
                ay = frame.axis_y
                az = frame.axis_z
                n_nodes = max(len(near_nodes), 1)

                # Axial force along frame Z
                if abs(lc.axial_force_n) > 1e-9:
                    f_per_node = lc.axial_force_n / n_nodes
                    bcs.append(FEABoundaryCondition(
                        node_set_name=set_name,
                        bc_type="force",
                        values=[
                            az[0] * f_per_node,
                            az[1] * f_per_node,
                            az[2] * f_per_node,
                        ],
                    ))

                # Radial force along frame X
                if abs(lc.radial_force_n) > 1e-9:
                    f_per_node = lc.radial_force_n / n_nodes
                    bcs.append(FEABoundaryCondition(
                        node_set_name=set_name,
                        bc_type="force",
                        values=[
                            ax[0] * f_per_node,
                            ax[1] * f_per_node,
                            ax[2] * f_per_node,
                        ],
                    ))

                # Torque: tangential force at radius from axis
                if abs(lc.torque_nm) > 1e-9:
                    # Estimate radius from node positions
                    ox, oy, oz = frame.origin_mm
                    radii = []
                    for nid in near_nodes:
                        nx, ny, nz = mesh_nodes[nid]
                        # Distance from axis (project onto frame XY plane)
                        dx, dy = nx - ox, ny - oy
                        r = math.sqrt(dx * dx + dy * dy)
                        if r > 1e-6:
                            radii.append(r)
                    avg_r = sum(radii) / len(radii) if radii else 1.0
                    # F = T / r, applied along frame Y (tangential)
                    f_tangential = (lc.torque_nm * 1000) / avg_r  # Nm -> N*mm
                    f_per_node = f_tangential / n_nodes
                    bcs.append(FEABoundaryCondition(
                        node_set_name=set_name,
                        bc_type="force",
                        values=[
                            ay[0] * f_per_node,
                            ay[1] * f_per_node,
                            ay[2] * f_per_node,
                        ],
                    ))

                # Bending moment: couple forces along frame Y
                if abs(lc.bending_moment_nm) > 1e-9:
                    ox, oy, oz = frame.origin_mm
                    radii = []
                    for nid in near_nodes:
                        nx, ny, nz = mesh_nodes[nid]
                        dx, dy = nx - ox, ny - oy
                        r = math.sqrt(dx * dx + dy * dy)
                        if r > 1e-6:
                            radii.append(r)
                    avg_r = sum(radii) / len(radii) if radii else 1.0
                    f_couple = (lc.bending_moment_nm * 1000) / avg_r
                    f_per_node = f_couple / n_nodes
                    bcs.append(FEABoundaryCondition(
                        node_set_name=set_name,
                        bc_type="force",
                        values=[
                            ax[0] * f_per_node,
                            ax[1] * f_per_node,
                            ax[2] * f_per_node,
                        ],
                    ))

    # Cantilever heuristic: if only one interface with loads and no fixed BC,
    # warn but don't auto-fix
    return bcs
