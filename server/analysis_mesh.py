"""Gmsh-based meshing for field analysis.

Loads a STEP file, creates physical groups for boundary condition faces,
and generates a tetrahedral mesh.
"""

from __future__ import annotations

import logging
import tempfile

from server.analysis_models import MeshInfo

log = logging.getLogger("solidmind.analysis_mesh")


_DEFAULT_FLUID_MARGIN = 3.0  # bounding box margin multiplier for fluid domain


def _gmsh_available() -> bool:
    try:
        import gmsh  # noqa: F401

        return True
    except ImportError:
        return False


def mesh_step_to_msh(
    step_path: str,
    face_groups: dict[str, list[str]] | None = None,
    mesh_size: float = 0.0,
    output_path: str | None = None,
    order: int = 1,
    msh_version: float = 0.0,
) -> MeshInfo:
    """Mesh a STEP file using Gmsh.

    Parameters
    ----------
    step_path : str
        Path to the input STEP file.
    face_groups : dict
        Maps group name → list of face refs (e.g. "fixed" → ["Face1", "Face3"]).
        Each group becomes a Gmsh physical group.  Face refs are matched by
        1-based surface index (OCC preserves ordering from STEP).
    mesh_size : float
        Target element size.  0 = Gmsh auto-sizing.
    output_path : str | None
        Where to write the .msh file.  None = auto temp file.
    order : int
        Element order (1 = linear tet4, 2 = quadratic tet10).

    Returns
    -------
    MeshInfo
        Metadata about the generated mesh.
    """
    if not _gmsh_available():
        raise RuntimeError("Gmsh is not installed. Install with: pip install gmsh")

    import gmsh

    gmsh.initialize()
    gmsh.option.setNumber("General.Verbosity", 1)
    try:
        gmsh.model.occ.importShapes(step_path)
        gmsh.model.occ.synchronize()

        # Get all surface entities
        surfaces = gmsh.model.getEntities(dim=2)

        # Create physical groups for face-based BCs
        physical_groups: dict[str, int] = {}
        if face_groups:
            for group_name, face_refs in face_groups.items():
                tags: list[int] = []
                for ref in face_refs:
                    # "Face<N>" → 1-based index
                    idx = int(ref.replace("Face", ""))
                    # Find the surface entity with matching index
                    if idx <= len(surfaces):
                        tags.append(surfaces[idx - 1][1])
                    else:
                        log.warning(
                            "Face ref %s out of range (have %d surfaces)",
                            ref,
                            len(surfaces),
                        )
                if tags:
                    pg = gmsh.model.addPhysicalGroup(2, tags)
                    gmsh.model.setPhysicalName(2, pg, group_name)
                    for ref in face_refs:
                        physical_groups[ref] = pg

        # Create a physical group for the volume (required for CalculiX)
        volumes = gmsh.model.getEntities(dim=3)
        if volumes:
            vol_tags = [v[1] for v in volumes]
            vol_pg = gmsh.model.addPhysicalGroup(3, vol_tags)
            gmsh.model.setPhysicalName(3, vol_pg, "volume")

        # Set mesh size
        if mesh_size > 0:
            gmsh.option.setNumber("Mesh.CharacteristicLengthMax", mesh_size)
            gmsh.option.setNumber("Mesh.CharacteristicLengthMin", mesh_size * 0.1)

        # Generate 3D mesh — retry without face groups if it fails
        try:
            gmsh.model.mesh.generate(3)
        except Exception as mesh_err:
            if face_groups and physical_groups:
                log.warning(
                    "Meshing failed with face groups, retrying without: %s",
                    mesh_err,
                )
                # Remove surface physical groups and retry
                gmsh.model.occ.synchronize()
                physical_groups.clear()
                gmsh.model.mesh.generate(3)
            else:
                raise

        if order == 2:
            gmsh.model.mesh.setOrder(2)

        # Determine output path
        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".msh")
            import os

            os.close(fd)

        if msh_version > 0:
            gmsh.option.setNumber("Mesh.MshFileVersion", msh_version)

        gmsh.write(output_path)

        # Gather stats
        node_tags, _, _ = gmsh.model.mesh.getNodes()
        num_nodes = len(node_tags)

        # Count 3D elements
        num_elements = 0
        element_type = "tet4" if order == 1 else "tet10"
        for vol_dim, vol_tag in volumes:
            elem_types, elem_tags, _ = gmsh.model.mesh.getElements(vol_dim, vol_tag)
            for et in elem_tags:
                num_elements += len(et)

        return MeshInfo(
            path=output_path,
            num_nodes=num_nodes,
            num_elements=num_elements,
            element_type=element_type,
            physical_groups=physical_groups,
        )
    finally:
        gmsh.finalize()


def mesh_step_to_cht_msh(
    step_path: str,
    face_groups: dict[str, list[str]] | None = None,
    mesh_size: float = 0.0,
    fluid_mesh_size: float = 0.0,
    output_path: str | None = None,
    margin: float = _DEFAULT_FLUID_MARGIN,
    msh_version: float = 2.2,
) -> MeshInfo:
    """Create a two-domain mesh for conjugate heat transfer.

    Workflow:
    1. Import the solid STEP body
    2. Compute a bounding box with ``margin`` multiplier
    3. Create a fluid box around the solid
    4. Boolean-fragment (cut) so solid and fluid share interface nodes
    5. Assign physical groups: ``solid`` volume, ``fluid`` volume,
       ``interface`` surfaces (shared wall), plus user-specified face BCs
    6. Generate tetrahedral mesh
    7. Write .msh with body tags in ``MeshInfo.body_tags``

    The resulting mesh has two volume physical groups that ElmerGrid
    converts to separate Elmer bodies, enabling coupled Navier-Stokes
    (fluid) + Heat conduction (solid) with automatic interface coupling.

    Parameters
    ----------
    step_path : str
        Path to the solid body STEP file.
    face_groups : dict
        Maps group name → list of face refs for BCs.
    mesh_size : float
        Solid mesh element size (0 = auto).
    fluid_mesh_size : float
        Fluid domain mesh element size (0 = 2× solid size).
    margin : float
        Bounding box expansion multiplier. 3.0 means the fluid domain
        extends 3× the solid size in each direction.
    """
    if not _gmsh_available():
        raise RuntimeError("Gmsh is not installed. Install with: pip install gmsh")

    import gmsh

    gmsh.initialize()
    gmsh.option.setNumber("General.Verbosity", 1)
    try:
        # Step 1: Import solid
        gmsh.model.occ.importShapes(step_path)
        gmsh.model.occ.synchronize()

        # Get solid volume tags
        solid_vols = [tag for dim, tag in gmsh.model.getEntities(dim=3)]
        if not solid_vols:
            raise RuntimeError("No 3D volumes found in STEP file")

        # Record solid surface count before boolean
        solid_surfaces_before = gmsh.model.getEntities(dim=2)
        len(solid_surfaces_before)

        # Step 2: Compute bounding box → fluid domain
        x_min, y_min, z_min, x_max, y_max, z_max = gmsh.model.occ.getBoundingBox(
            3,
            solid_vols[0],
        )
        # If multiple solid volumes, expand to encompass all
        for sv in solid_vols[1:]:
            bx0, by0, bz0, bx1, by1, bz1 = gmsh.model.occ.getBoundingBox(3, sv)
            x_min, y_min, z_min = min(x_min, bx0), min(y_min, by0), min(z_min, bz0)
            x_max, y_max, z_max = max(x_max, bx1), max(y_max, by1), max(z_max, bz1)

        dx = x_max - x_min
        dy = y_max - y_min
        dz = z_max - z_min
        pad_x = dx * margin
        pad_y = dy * margin
        pad_z = dz * margin

        fluid_box = gmsh.model.occ.addBox(
            x_min - pad_x,
            y_min - pad_y,
            z_min - pad_z,
            dx + 2 * pad_x,
            dy + 2 * pad_y,
            dz + 2 * pad_z,
        )

        # Step 3: Boolean fragment — creates shared interface surfaces
        # fragment keeps both volumes but splits them at the interface
        out_map, obj_map = gmsh.model.occ.fragment(
            [(3, fluid_box)],
            [(3, sv) for sv in solid_vols],
        )
        gmsh.model.occ.synchronize()

        # Step 4: Identify solid vs fluid volumes
        # After fragment, we need to find which volumes are solid and which are fluid
        all_vols = gmsh.model.getEntities(dim=3)
        all_surfaces = gmsh.model.getEntities(dim=2)

        # The solid volumes are the ones that overlap with original solid bbox
        # (fragment may have renumbered them)
        solid_vol_tags: list[int] = []
        fluid_vol_tags: list[int] = []

        (x_min + x_max) / 2
        (y_min + y_max) / 2
        (z_min + z_max) / 2

        for dim, tag in all_vols:
            bx0, by0, bz0, bx1, by1, bz1 = gmsh.model.occ.getBoundingBox(dim, tag)
            (bx0 + bx1) / 2
            (by0 + by1) / 2
            (bz0 + bz1) / 2
            vol_dx = bx1 - bx0
            vol_dy = by1 - by0
            vol_dz = bz1 - bz0

            # Solid volumes are small (close to original size),
            # fluid volume is large (includes padding)
            if vol_dx < dx * 1.5 and vol_dy < dy * 1.5 and vol_dz < dz * 1.5:
                solid_vol_tags.append(tag)
            else:
                fluid_vol_tags.append(tag)

        if not solid_vol_tags:
            # Fallback: smallest volume is solid
            vol_sizes = []
            for dim, tag in all_vols:
                bx0, by0, bz0, bx1, by1, bz1 = gmsh.model.occ.getBoundingBox(dim, tag)
                vol_sizes.append(((bx1 - bx0) * (by1 - by0) * (bz1 - bz0), tag))
            vol_sizes.sort()
            solid_vol_tags = [vol_sizes[0][1]]
            fluid_vol_tags = [t for _, t in vol_sizes[1:]]

        if not fluid_vol_tags:
            raise RuntimeError(
                "Boolean fragment failed to create separate fluid domain. "
                f"Found {len(all_vols)} volumes, all classified as solid."
            )

        log.info(
            "CHT mesh: %d solid volume(s) %s, %d fluid volume(s) %s",
            len(solid_vol_tags),
            solid_vol_tags,
            len(fluid_vol_tags),
            fluid_vol_tags,
        )

        # Step 5: Physical groups
        physical_groups: dict[str, int] = {}
        body_tags: dict[str, int] = {}

        # Solid volume physical group
        solid_pg = gmsh.model.addPhysicalGroup(3, solid_vol_tags)
        gmsh.model.setPhysicalName(3, solid_pg, "solid")
        body_tags["solid"] = solid_pg

        # Fluid volume physical group
        fluid_pg = gmsh.model.addPhysicalGroup(3, fluid_vol_tags)
        gmsh.model.setPhysicalName(3, fluid_pg, "fluid")
        body_tags["fluid"] = fluid_pg

        # Interface surfaces: surfaces shared between solid and fluid
        # These are surfaces that bound both a solid vol and a fluid vol
        interface_tags: list[int] = []
        for _dim, surf_tag in all_surfaces:
            try:
                up, down = gmsh.model.getAdjacencies(2, surf_tag)
                # up = higher-dim entities this surface bounds
                parents = set(up)
                has_solid = bool(parents & set(solid_vol_tags))
                has_fluid = bool(parents & set(fluid_vol_tags))
                if has_solid and has_fluid:
                    interface_tags.append(surf_tag)
            except Exception:
                pass

        if interface_tags:
            iface_pg = gmsh.model.addPhysicalGroup(2, interface_tags)
            gmsh.model.setPhysicalName(2, iface_pg, "interface")
            physical_groups["interface"] = iface_pg
            log.info("CHT mesh: %d interface surfaces", len(interface_tags))

        # Find fluid outer boundary surfaces (on the bounding box)
        # These are surfaces that bound only fluid volumes and are on the box edges
        inlet_candidates: list[int] = []
        outlet_candidates: list[int] = []
        wall_candidates: list[int] = []

        x_min - pad_x
        x_min - pad_x + dx + 2 * pad_x
        box_y_min = y_min - pad_y
        box_y_max = y_min - pad_y + dy + 2 * pad_y
        z_min - pad_z
        z_min - pad_z + dz + 2 * pad_z

        for _dim, surf_tag in all_surfaces:
            if surf_tag in interface_tags:
                continue
            try:
                up, _ = gmsh.model.getAdjacencies(2, surf_tag)
                parents = set(up)
                if not (parents & set(fluid_vol_tags)):
                    continue
                # It's a fluid-only surface — classify by position
                sb = gmsh.model.occ.getBoundingBox(2, surf_tag)
                (sb[0] + sb[3]) / 2
                scy = (sb[1] + sb[4]) / 2
                (sb[2] + sb[5]) / 2
                # Check if it's on a box face (within tolerance)
                tol = max(dx, dy, dz) * 0.01
                if abs(scy - box_y_min) < tol:
                    inlet_candidates.append(surf_tag)
                elif abs(scy - box_y_max) < tol:
                    outlet_candidates.append(surf_tag)
                else:
                    wall_candidates.append(surf_tag)
            except Exception:
                pass

        if inlet_candidates:
            pg = gmsh.model.addPhysicalGroup(2, inlet_candidates)
            gmsh.model.setPhysicalName(2, pg, "fluid_inlet")
            physical_groups["fluid_inlet"] = pg

        if outlet_candidates:
            pg = gmsh.model.addPhysicalGroup(2, outlet_candidates)
            gmsh.model.setPhysicalName(2, pg, "fluid_outlet")
            physical_groups["fluid_outlet"] = pg

        if wall_candidates:
            pg = gmsh.model.addPhysicalGroup(2, wall_candidates)
            gmsh.model.setPhysicalName(2, pg, "fluid_walls")
            physical_groups["fluid_walls"] = pg

        # User-specified face groups (applied to solid surfaces by index)
        if face_groups:
            # After boolean fragment, solid surface indices may shift.
            # We try to resolve by original index (best effort).
            solid_surfaces = []
            for _dim, surf_tag in all_surfaces:
                try:
                    up, _ = gmsh.model.getAdjacencies(2, surf_tag)
                    if set(up) & set(solid_vol_tags):
                        solid_surfaces.append(surf_tag)
                except Exception:
                    pass

            for group_name, face_refs in face_groups.items():
                tags: list[int] = []
                for ref in face_refs:
                    idx = int(ref.replace("Face", ""))
                    if idx <= len(solid_surfaces):
                        tags.append(solid_surfaces[idx - 1])
                if tags:
                    pg = gmsh.model.addPhysicalGroup(2, tags)
                    gmsh.model.setPhysicalName(2, pg, group_name)
                    for ref in face_refs:
                        physical_groups[ref] = pg

        # Step 6: Mesh sizing
        auto_size = max(dx, dy, dz) / 10.0
        solid_size = mesh_size if mesh_size > 0 else auto_size
        fluid_size = fluid_mesh_size if fluid_mesh_size > 0 else solid_size * 2.0

        # Set size on solid volumes (finer)
        for sv in solid_vol_tags:
            pts = gmsh.model.getBoundary([(3, sv)], recursive=True)
            pt_tags = [t for d, t in pts if d == 0]
            if pt_tags:
                gmsh.model.mesh.setSize([(0, t) for t in pt_tags], solid_size)

        # Set size on fluid volumes (coarser)
        for fv in fluid_vol_tags:
            pts = gmsh.model.getBoundary([(3, fv)], recursive=True)
            pt_tags = [t for d, t in pts if d == 0]
            if pt_tags:
                gmsh.model.mesh.setSize([(0, t) for t in pt_tags], fluid_size)

        # Generate 3D mesh
        gmsh.model.mesh.generate(3)

        # Output
        if output_path is None:
            import os

            fd, output_path = tempfile.mkstemp(suffix=".msh")
            os.close(fd)

        gmsh.option.setNumber("Mesh.MshFileVersion", msh_version)
        gmsh.write(output_path)

        # Stats
        node_tags, _, _ = gmsh.model.mesh.getNodes()
        num_nodes = len(node_tags)

        num_elements = 0
        for dim, tag in all_vols:
            elem_types, elem_tags, _ = gmsh.model.mesh.getElements(dim, tag)
            for et in elem_tags:
                num_elements += len(et)

        return MeshInfo(
            path=output_path,
            num_nodes=num_nodes,
            num_elements=num_elements,
            element_type="tet4",
            physical_groups=physical_groups,
            body_tags=body_tags,
        )
    finally:
        gmsh.finalize()
