"""Map frozen interface loads + coordinate frames to face-tagged boundary conditions.

The batch (outer-loop) FEA path starts from a STEP file and a set of frozen
interfaces, each carrying a ``CoordinateFrame`` and ``LoadCase``s. The shared
structural engine applies boundary conditions to *named STEP faces* (``Face1``,
``Face2``, …), so this module bridges the two: it locates the STEP face nearest
each interface frame (a mesh-independent geometric query over the OCC surfaces)
and resolves the interface loads into a world-frame total force on that face.

This replaces the older node-proximity mapping. Node sets were mesh-dependent and
forced the BCs to be recomputed at every mesh density; a face reference is fixed
by the geometry and is exactly what the shared ``BoundaryCondition`` consumes.
"""

from __future__ import annotations

import logging
import math

from orchestrator.spec import CoordinateFrame, Interface, LoadCase, Subsystem
from server.analysis_models import BoundaryCondition

log = logging.getLogger(__name__)


def _load_magnitude(lc: LoadCase) -> float:
    """Sum of absolute load values for a load case."""
    return (
        abs(lc.torque_nm)
        + abs(lc.axial_force_n)
        + abs(lc.radial_force_n)
        + abs(lc.bending_moment_nm)
    )


def surface_geometry(
    step_path: str | object,
) -> dict[int, tuple[tuple[float, float, float], float]]:
    """Return ``{1-based OCC surface index: (centroid, characteristic_radius)}``.

    Opens the STEP once via gmsh's OCC kernel. The characteristic radius (half the
    largest bounding-box extent of the face) is used to convert interface
    torques/moments into an equivalent face force, the same modelling intent the
    node-based mapper had — now derived from geometry instead of mesh nodes.
    """
    try:
        import gmsh
    except ImportError:
        log.warning("gmsh not installed — cannot map frames to faces")
        return {}

    started = gmsh.isInitialized()
    if not started:
        gmsh.initialize()
    gmsh.option.setNumber("General.Verbosity", 1)
    # Work inside a private model so we never importShapes into — or clear() — a
    # caller's in-progress gmsh model. Restore the caller's current model on exit.
    prev_model = gmsh.model.getCurrent() if started else None
    model_name = "__solidmind_surface_geometry__"
    try:
        gmsh.model.add(model_name)
        gmsh.model.occ.importShapes(str(step_path))
        gmsh.model.occ.synchronize()
        out: dict[int, tuple[tuple[float, float, float], float]] = {}
        for idx, (dim, tag) in enumerate(gmsh.model.getEntities(dim=2), start=1):
            cx, cy, cz = gmsh.model.occ.getCenterOfMass(dim, tag)
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.occ.getBoundingBox(dim, tag)
            extent = max(xmax - xmin, ymax - ymin, zmax - zmin)
            out[idx] = ((cx, cy, cz), max(extent * 0.5, 1e-6))
        return out
    except Exception as exc:  # noqa: BLE001 — a bad STEP shouldn't crash scoring
        log.warning("Surface enumeration failed for %s: %s", step_path, exc)
        return {}
    finally:
        try:
            gmsh.model.remove()  # drop our private model only
        except Exception:  # noqa: BLE001
            pass
        if prev_model:
            try:
                gmsh.model.setCurrent(prev_model)
            except Exception:  # noqa: BLE001
                pass
        if not started:
            gmsh.finalize()


#: How far beyond a face's own characteristic radius a frame may sit and still be
#: considered "on" that face. Mirrors the retired node-proximity search radius.
FACE_MATCH_MARGIN_MM = 5.0


def map_frame_to_face(
    geometry: dict[int, tuple[tuple[float, float, float], float]],
    frame: CoordinateFrame,
    *,
    exclude: set[str] | None = None,
    margin_mm: float = FACE_MATCH_MARGIN_MM,
) -> str | None:
    """Return the ``FaceN`` whose centroid is nearest the frame origin, or None.

    A face is a candidate only when the frame origin lies within the face's own
    characteristic radius + ``margin_mm`` of its centroid — a frame sitting on the
    *mating partner's* geometry (far from any of this part's faces) yields no BC,
    as the retired node-proximity mapper did, instead of fabricating one on the
    globally-nearest face. ``exclude`` skips already-claimed faces so a load is
    never placed on a support face (whose reaction would absorb it).
    """
    exclude = exclude or set()
    ox, oy, oz = frame.origin_mm
    best_idx: int | None = None
    best_d2: float | None = None
    for idx, ((x, y, z), r) in geometry.items():
        if f"Face{idx}" in exclude:
            continue
        d2 = (x - ox) ** 2 + (y - oy) ** 2 + (z - oz) ** 2
        if d2 > (r + margin_mm) ** 2:  # frame is not on/near this face
            continue
        if best_d2 is None or d2 < best_d2:
            best_idx, best_d2 = idx, d2
    return f"Face{best_idx}" if best_idx is not None else None


def _axis_radial_distance(point: tuple[float, float, float], frame: CoordinateFrame) -> float:
    """Perpendicular distance from ``point`` to the frame's Z axis through its origin.

    This is the torque/bending lever arm — the radial offset of the load
    application point from the rotation axis — replacing the geometry-agnostic
    bounding-box estimate.
    """
    px, py, pz = point
    ox, oy, oz = frame.origin_mm
    az = frame.axis_z
    rx, ry, rz = px - ox, py - oy, pz - oz
    axial = rx * az[0] + ry * az[1] + rz * az[2]
    perp = (rx - axial * az[0], ry - axial * az[1], rz - axial * az[2])
    return math.hypot(perp[0], perp[1], perp[2])


def _world_force_for_loads(
    loads: list[LoadCase], frame: CoordinateFrame, radius_mm: float
) -> tuple[float, float, float]:
    """Resolve a frame's load cases into a single world-frame total force (N).

    Axial acts along frame Z, radial along frame X. Torque and bending moment are
    converted to an equivalent force using ``radius_mm`` (the load point's radial
    distance from the frame axis) — ``F = M·1000 / r`` (N·mm → N) — applied
    tangentially (frame Y) and along frame X respectively. Force is a *total* over
    the face; the solver distributes it across the face's nodes.
    """
    fx = fy = fz = 0.0
    ax, ay, az = frame.axis_x, frame.axis_y, frame.axis_z
    for lc in loads:
        if _load_magnitude(lc) < 1e-9:
            continue
        if abs(lc.axial_force_n) > 1e-9:
            fx += az[0] * lc.axial_force_n
            fy += az[1] * lc.axial_force_n
            fz += az[2] * lc.axial_force_n
        if abs(lc.radial_force_n) > 1e-9:
            fx += ax[0] * lc.radial_force_n
            fy += ax[1] * lc.radial_force_n
            fz += ax[2] * lc.radial_force_n
        if abs(lc.torque_nm) > 1e-9:
            f_t = (lc.torque_nm * 1000.0) / radius_mm
            fx += ay[0] * f_t
            fy += ay[1] * f_t
            fz += ay[2] * f_t
        if abs(lc.bending_moment_nm) > 1e-9:
            f_b = (lc.bending_moment_nm * 1000.0) / radius_mm
            fx += ax[0] * f_b
            fy += ax[1] * f_b
            fz += ax[2] * f_b
    return fx, fy, fz


def map_interface_bcs(
    subsystem: Subsystem,
    interfaces: list[Interface],
    geometry: dict[int, tuple[tuple[float, float, float], float]],
) -> list[BoundaryCondition]:
    """Map frozen interface loads to face-tagged ``BoundaryCondition``s.

    - zero-load interface → a fixed support on its nearest face,
    - otherwise → a total force resolved from the interface's load cases.

    Fixed supports are assigned *first* and claim their faces, so a loaded
    interface can never be placed on a support face (the reaction would silently
    absorb the load → a meaningless zero-stress "pass"). A load set with no support
    is left support-free on purpose: the singular solve fails closed downstream
    rather than fabricating an anchor by dropping one of the real loads.
    """
    bcs: list[BoundaryCondition] = []
    if not geometry:
        return bcs

    # Resolve each interface that belongs to this subsystem to its frame + load flag.
    items: list[tuple[Interface, CoordinateFrame, bool]] = []
    for ifc in interfaces:
        if ifc.subsystem_a == subsystem.name:
            frame = ifc.frame_a
        elif ifc.subsystem_b == subsystem.name:
            frame = ifc.frame_b
        else:
            continue
        loaded = sum(_load_magnitude(lc) for lc in ifc.loads) >= 1e-9
        items.append((ifc, frame, loaded))

    used_faces: set[str] = set()

    # Pass 1: fixed supports claim their faces first.
    for _ifc, frame, loaded in items:
        if loaded:
            continue
        face = map_frame_to_face(geometry, frame, exclude=used_faces)
        if face is None:
            continue
        used_faces.add(face)
        bcs.append(BoundaryCondition(bc_type="fixed", faces=(face,), value={}))

    # Pass 2: loaded interfaces, never landing on a face already used as a support.
    for ifc, frame, loaded in items:
        if not loaded:
            continue
        face = map_frame_to_face(geometry, frame, exclude=used_faces)
        if face is None:
            continue
        centroid, char_r = geometry[int(face.removeprefix("Face"))]
        lever = _axis_radial_distance(centroid, frame)
        if lever < 1e-6:  # load point on the axis — no meaningful arm; fall back
            lever = char_r
        fx, fy, fz = _world_force_for_loads(list(ifc.loads), frame, lever)
        if abs(fx) + abs(fy) + abs(fz) < 1e-9:
            continue
        used_faces.add(face)
        bcs.append(
            BoundaryCondition(bc_type="force", faces=(face,), value={"fx": fx, "fy": fy, "fz": fz})
        )

    # A loaded model with no support is genuinely under-constrained — we do NOT
    # fabricate an anchor by demoting one of the real loads (that silently drops a
    # load and under-predicts stress). It is left to fail closed: CalculiX raises on
    # the singular system → StructuralSolveError → the variant is flagged fea_error.
    return bcs


def has_loaded_interface(subsystem: Subsystem, interfaces: list[Interface]) -> bool:
    """True if any interface on this subsystem carries a nonzero load."""
    for ifc in interfaces:
        if ifc.subsystem_a != subsystem.name and ifc.subsystem_b != subsystem.name:
            continue
        if sum(_load_magnitude(lc) for lc in ifc.loads) >= 1e-9:
            return True
    return False
