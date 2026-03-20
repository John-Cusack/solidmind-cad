"""Map FreeCAD Face<N> references to Gmsh surface entity tags.

Primary strategy: index correspondence — STEP export via OCC kernel preserves
face ordering, so Face1 → surface entity 1, Face2 → surface entity 2, etc.

Fallback: geometric matching by center point, normal, and area when index
correspondence fails (e.g. after boolean operations that split faces).
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("solidmind.analysis_face_map")


def map_face_refs_to_gmsh(
    face_refs: list[str],
    topology: list[dict[str, Any]],
    gmsh_surfaces: list[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """Map FreeCAD face references to Gmsh surface entity tags.

    Parameters
    ----------
    face_refs : list[str]
        Face references like ["Face1", "Face3"].
    topology : list[dict]
        Face topology from ``cad_get_body_topology``.  Each entry has:
        ``{"name": "Face1", "center": [x,y,z], "normal": [x,y,z], "area": float}``.
    gmsh_surfaces : list[tuple[int, int]] | None
        Gmsh surface entities ``(dim, tag)`` from ``gmsh.model.getEntities(2)``.
        If None, uses 1-based index mapping.

    Returns
    -------
    dict[str, int]
        Maps face ref → Gmsh surface tag.  Missing refs are omitted with a warning.
    """
    result: dict[str, int] = {}

    # Build lookup from topology
    topo_by_name: dict[str, dict[str, Any]] = {}
    for face in topology:
        topo_by_name[face["name"]] = face

    for ref in face_refs:
        idx = _parse_face_index(ref)
        if idx is None:
            log.warning("Cannot parse face reference: %s", ref)
            continue

        if gmsh_surfaces is not None and idx <= len(gmsh_surfaces):
            # Primary: index correspondence
            result[ref] = gmsh_surfaces[idx - 1][1]
        elif gmsh_surfaces is None:
            # No gmsh info — use 1-based index directly
            result[ref] = idx
        else:
            log.warning(
                "Face ref %s (index %d) out of range (%d surfaces)",
                ref, idx, len(gmsh_surfaces),
            )

    return result


def match_faces_geometric(
    face_refs: list[str],
    topology: list[dict[str, Any]],
    gmsh_face_data: list[dict[str, Any]],
    tolerance: float = 0.1,
) -> dict[str, int]:
    """Geometric fallback: match faces by center + normal + area.

    Parameters
    ----------
    face_refs : list[str]
        Face references to match.
    topology : list[dict]
        FreeCAD face topology (name, center, normal, area).
    gmsh_face_data : list[dict]
        Gmsh face data with keys: tag, center, normal, area.
    tolerance : float
        Position tolerance in mm for center matching.

    Returns
    -------
    dict[str, int]
        Maps face ref → Gmsh surface tag for matched faces.
    """
    result: dict[str, int] = {}

    topo_by_name: dict[str, dict[str, Any]] = {}
    for face in topology:
        topo_by_name[face["name"]] = face

    for ref in face_refs:
        fc_face = topo_by_name.get(ref)
        if fc_face is None:
            log.warning("Face %s not found in topology", ref)
            continue

        fc_center = fc_face.get("center", [0, 0, 0])
        fc_normal = fc_face.get("normal", [0, 0, 1])
        fc_area = fc_face.get("area", 0)

        best_tag: int | None = None
        best_dist = float("inf")

        for gf in gmsh_face_data:
            gc = gf.get("center", [0, 0, 0])
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(fc_center, gc)))
            if dist > tolerance:
                continue

            # Check normal alignment (dot product close to ±1)
            gn = gf.get("normal", [0, 0, 1])
            dot = abs(sum(a * b for a, b in zip(fc_normal, gn)))
            if dot < 0.9:
                continue

            # Check area similarity (within 20%)
            ga = gf.get("area", 0)
            if fc_area > 0 and ga > 0:
                ratio = min(fc_area, ga) / max(fc_area, ga)
                if ratio < 0.8:
                    continue

            if dist < best_dist:
                best_dist = dist
                best_tag = gf["tag"]

        if best_tag is not None:
            result[ref] = best_tag
        else:
            log.warning("No geometric match for %s", ref)

    return result


def _parse_face_index(ref: str) -> int | None:
    """Extract 1-based index from 'Face<N>'."""
    if ref.startswith("Face"):
        try:
            return int(ref[4:])
        except ValueError:
            return None
    return None
