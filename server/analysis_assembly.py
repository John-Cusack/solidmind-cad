"""In-process linear static structural assembly utilities.

This module defines the CAD-side load contract used by direct solvers:
- ``force`` values are interpreted as **total force (N)** over the selected face set.
  The total vector is distributed uniformly to all nodes in that set.
- ``pressure_mpa`` values are interpreted as pressure intensity (MPa == N/mm^2)
  applied over selected surface triangles and converted to nodal forces.

Only first-order tetrahedral volume meshes (tet4) are supported in v1.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, Mapping, Sequence

import numpy as np
try:
    from scipy import sparse
except Exception:  # pragma: no cover - optional runtime dependency
    sparse = None  # type: ignore[assignment]

from server.analysis_models import (
    AnalysisCheck,
    AnalysisSpec,
    BoundaryCondition,
    CheckStatus,
    FieldResult,
    Material,
    MeshInfo,
    ScalarFieldSummary,
)

log = logging.getLogger("solidmind.analysis_assembly")


@dataclass(frozen=True, slots=True)
class AssembledSystem:
    """Assembled structural system for direct solve."""

    K: Any
    f: np.ndarray
    free_dofs: np.ndarray
    fixed_dofs: np.ndarray
    node_coords: np.ndarray
    connectivity: np.ndarray
    element_dof_indices: np.ndarray
    element_centroids: np.ndarray
    B_matrices: np.ndarray
    D_matrix: np.ndarray
    topology_hash: str
    material_hash: str
    bc_hash: str
    element_order: str
    precision: str
    options_signature: str

    @property
    def num_nodes(self) -> int:
        return int(self.node_coords.shape[0])

    def factor_cache_key(self, solver_name: str) -> str:
        """Cache key for matrix factorization reuse."""
        payload = {
            "solver": solver_name,
            "topology_hash": self.topology_hash,
            "material_hash": self.material_hash,
            "bc_hash": self.bc_hash,
            "element_order": self.element_order,
            "precision": self.precision,
            "options": self.options_signature,
        }
        return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class MeshTopology:
    node_coords: np.ndarray
    connectivity: np.ndarray
    boundary_triangles: np.ndarray
    boundary_tags: np.ndarray


def assemble_system(
    mesh_info: MeshInfo,
    spec: AnalysisSpec,
    *,
    precision: str = "float64",
    solver_options: Mapping[str, Any] | None = None,
) -> AssembledSystem:
    """Assemble ``K u = f`` for linear static tet4 structural analysis."""
    topology = _read_tet4_mesh(mesh_info.path)

    fixed_nodes: set[int] = set()
    nodal_forces = np.zeros(topology.node_coords.shape[0] * 3, dtype=np.float64)

    # Total-force contract: force values represent total vector on the face set.
    for bc in spec.boundary_conditions:
        bc_nodes, bc_triangles = _nodes_and_triangles_for_bc(
            bc,
            mesh_info.physical_groups,
            topology.boundary_triangles,
            topology.boundary_tags,
        )

        if bc.bc_type == "fixed":
            fixed_nodes.update(bc_nodes)
            continue

        if bc.bc_type == "force":
            _apply_total_force_bc(nodal_forces, bc_nodes, bc)
            continue

        if bc.bc_type == "pressure":
            _apply_pressure_bc(nodal_forces, bc_triangles, topology.node_coords, bc)
            continue

    topology_hash = _topology_hash(topology.node_coords, topology.connectivity, mesh_info.element_type)
    material_hash = _material_hash(spec.material)
    bc_hash = _bc_hash(spec.boundary_conditions, fixed_nodes)
    options_signature = _options_hash(solver_options)

    return assemble_tet4_system(
        topology.node_coords,
        topology.connectivity,
        spec.material,
        fixed_nodes,
        nodal_forces,
        topology_hash=topology_hash,
        material_hash=material_hash,
        bc_hash=bc_hash,
        element_order=mesh_info.element_type,
        precision=precision,
        options_signature=options_signature,
    )


def assemble_tet4_system(
    node_coords: np.ndarray,
    connectivity: np.ndarray,
    material: Material,
    fixed_nodes: set[int],
    nodal_forces: np.ndarray,
    *,
    topology_hash: str = "",
    material_hash: str = "",
    bc_hash: str = "",
    element_order: str = "tet4",
    precision: str = "float64",
    options_signature: str = "",
) -> AssembledSystem:
    """Assemble a tet4 system from arrays (used by runtime and tests)."""
    if sparse is None:
        raise RuntimeError("scipy is required for direct solvers: pip install scipy")

    if element_order not in {"tet4", "tetra", "tetra4"}:
        raise RuntimeError(f"Direct solver v1 only supports tet4 meshes, got {element_order!r}")

    node_coords = np.asarray(node_coords, dtype=np.float64)
    connectivity = np.asarray(connectivity, dtype=np.int64)
    if connectivity.ndim != 2 or connectivity.shape[1] != 4:
        raise RuntimeError("Expected tet4 connectivity with shape (M, 4)")

    num_nodes = int(node_coords.shape[0])
    num_dofs = num_nodes * 3

    if nodal_forces.shape[0] != num_dofs:
        raise RuntimeError("Nodal force vector length does not match 3*num_nodes")

    D = _constitutive_matrix(material.youngs_modulus_mpa, material.poissons_ratio)
    B, volumes = _tet4_b_matrices(node_coords, connectivity)
    K_full, element_dof_indices = _assemble_global_stiffness(B, D, volumes, connectivity, num_dofs)

    fixed_node_ids = np.array(sorted(fixed_nodes), dtype=np.int64)
    fixed_dofs = _node_ids_to_dofs(fixed_node_ids)
    free_dofs = np.setdiff1d(np.arange(num_dofs, dtype=np.int64), fixed_dofs, assume_unique=False)

    if free_dofs.size == 0:
        raise RuntimeError("No free DOFs left after applying fixed boundary conditions")

    K = K_full[free_dofs][:, free_dofs].tocsc()
    f = nodal_forces[free_dofs].astype(np.float64, copy=False)

    element_centroids = np.mean(node_coords[connectivity], axis=1)

    return AssembledSystem(
        K=K,
        f=f,
        free_dofs=free_dofs,
        fixed_dofs=fixed_dofs,
        node_coords=node_coords,
        connectivity=connectivity,
        element_dof_indices=element_dof_indices,
        element_centroids=element_centroids,
        B_matrices=B,
        D_matrix=D,
        topology_hash=topology_hash,
        material_hash=material_hash,
        bc_hash=bc_hash,
        element_order=element_order,
        precision=precision,
        options_signature=options_signature,
    )


def build_field_result_from_solution(
    spec: AnalysisSpec,
    system: AssembledSystem,
    u_free: np.ndarray,
    *,
    solver_name: str,
    solve_time_s: float,
) -> FieldResult:
    """Convert solved displacements to the standard ``FieldResult`` shape."""
    vm, max_vm_idx, u_full, max_disp, max_disp_idx, mean_disp = recover_solution_fields(u_free, system)

    if vm.size > 0:
        max_vm = float(np.max(vm))
        min_vm = float(np.min(vm))
        mean_vm = float(np.mean(vm))
        max_vm_loc = tuple(float(v) for v in system.element_centroids[max_vm_idx])
    else:
        max_vm = 0.0
        min_vm = 0.0
        mean_vm = 0.0
        max_vm_loc = (0.0, 0.0, 0.0)

    if max_disp_idx >= 0:
        max_disp_loc = tuple(float(v) for v in system.node_coords[max_disp_idx])
    else:
        max_disp_loc = (0.0, 0.0, 0.0)

    yield_mpa = spec.material.yield_strength_mpa
    safety_factor = (yield_mpa / max_vm) if max_vm > 1e-12 else 99.0

    checks: list[AnalysisCheck] = []
    if max_vm > yield_mpa:
        checks.append(
            AnalysisCheck(
                name="yield_check",
                status=CheckStatus.FAIL,
                message=f"Max von Mises {max_vm:.1f} MPa EXCEEDS yield {yield_mpa:.1f} MPa",
                measured=max_vm,
                limit=yield_mpa,
                suggestion="Increase cross-section, add fillets, or use stronger material",
            )
        )
    elif max_vm > yield_mpa * 0.8:
        checks.append(
            AnalysisCheck(
                name="yield_check",
                status=CheckStatus.WARN,
                message=(
                    f"Max von Mises {max_vm:.1f} MPa is within 20% of yield "
                    f"{yield_mpa:.1f} MPa (SF={safety_factor:.2f})"
                ),
                measured=max_vm,
                limit=yield_mpa,
                suggestion="Consider increasing thickness or adding reinforcement",
            )
        )
    else:
        checks.append(
            AnalysisCheck(
                name="yield_check",
                status=CheckStatus.PASS,
                message=f"Max von Mises {max_vm:.1f} MPa < yield {yield_mpa:.1f} MPa (SF={safety_factor:.2f})",
                measured=max_vm,
                limit=yield_mpa,
            )
        )

    if max_disp > 1.0:
        checks.append(
            AnalysisCheck(
                name="displacement_check",
                status=CheckStatus.WARN,
                message=f"Max displacement {max_disp:.3f} mm may be excessive",
                measured=max_disp,
                suggestion="Increase stiffness or add supports",
            )
        )
    else:
        checks.append(
            AnalysisCheck(
                name="displacement_check",
                status=CheckStatus.PASS,
                message=f"Max displacement {max_disp:.3f} mm",
                measured=max_disp,
            )
        )

    overall = CheckStatus.PASS
    for c in checks:
        if c.status == CheckStatus.FAIL:
            overall = CheckStatus.FAIL
            break
        if c.status == CheckStatus.WARN:
            overall = CheckStatus.WARN

    fields = (
        ScalarFieldSummary(
            field_name="von_mises_stress",
            min_val=round(min_vm, 2),
            max_val=round(max_vm, 2),
            mean_val=round(mean_vm, 2),
            unit="MPa",
            max_location_xyz=max_vm_loc,
        ),
        ScalarFieldSummary(
            field_name="displacement",
            min_val=0.0,
            max_val=round(max_disp, 4),
            mean_val=round(mean_disp, 4),
            unit="mm",
            max_location_xyz=max_disp_loc,
        ),
    )

    return FieldResult(
        analysis_id="",
        status=overall,
        safety_factor=round(safety_factor, 2),
        max_von_mises_mpa=round(max_vm, 2),
        max_displacement_mm=round(max_disp, 4),
        checks=tuple(checks),
        scalar_fields=fields,
        solver_name=solver_name,
        solve_time_s=solve_time_s,
    )


def recover_solution_fields(
    u_free: np.ndarray,
    system: AssembledSystem,
) -> tuple[np.ndarray, int, np.ndarray, float, int, float]:
    """Recover element von Mises and nodal displacement metrics."""
    u_full = np.zeros(system.node_coords.shape[0] * 3, dtype=np.float64)
    u_full[system.free_dofs] = np.asarray(u_free, dtype=np.float64)

    u_elem = u_full[system.element_dof_indices]
    strain = np.einsum("mij,mj->mi", system.B_matrices, u_elem)
    stress = np.einsum("ij,mj->mi", system.D_matrix, strain)

    sxx = stress[:, 0]
    syy = stress[:, 1]
    szz = stress[:, 2]
    sxy = stress[:, 3]
    syz = stress[:, 4]
    szx = stress[:, 5]
    vm = np.sqrt(
        0.5
        * (
            (sxx - syy) ** 2
            + (syy - szz) ** 2
            + (szz - sxx) ** 2
            + 6.0 * (sxy**2 + syz**2 + szx**2)
        )
    )

    if vm.size > 0:
        max_vm_idx = int(np.argmax(vm))
    else:
        max_vm_idx = -1

    disp = u_full.reshape(-1, 3)
    disp_mag = np.linalg.norm(disp, axis=1)
    if disp_mag.size > 0:
        max_disp_idx = int(np.argmax(disp_mag))
        max_disp = float(np.max(disp_mag))
        mean_disp = float(np.mean(disp_mag))
    else:
        max_disp_idx = -1
        max_disp = 0.0
        mean_disp = 0.0

    return vm, max_vm_idx, u_full, max_disp, max_disp_idx, mean_disp


def _read_tet4_mesh(mesh_path: str) -> MeshTopology:
    try:
        import meshio
    except ImportError as exc:
        raise RuntimeError("meshio is required for direct solvers: pip install meshio") from exc

    mesh = meshio.read(mesh_path)

    node_coords = np.asarray(mesh.points[:, :3], dtype=np.float64)

    gmsh_phys = mesh.cell_data.get("gmsh:physical", [])
    if gmsh_phys and len(gmsh_phys) != len(mesh.cells):
        gmsh_phys = []

    tet_list: list[np.ndarray] = []
    tri_list: list[np.ndarray] = []
    tri_tag_list: list[np.ndarray] = []

    for idx, block in enumerate(mesh.cells):
        data = np.asarray(block.data, dtype=np.int64)
        tags = None
        if gmsh_phys:
            tags = np.asarray(gmsh_phys[idx], dtype=np.int64)

        if block.type == "tetra":
            tet_list.append(data)
        elif block.type in {"tetra10", "tetra20"}:
            raise RuntimeError("Direct solver v1 supports only first-order tetra (tet4)")
        elif block.type in {"triangle", "triangle6"}:
            tri_list.append(data[:, :3])
            if tags is None:
                tri_tag_list.append(np.zeros(data.shape[0], dtype=np.int64))
            else:
                tri_tag_list.append(tags)

    if not tet_list:
        raise RuntimeError("No tet4 volume elements found in mesh")

    connectivity = np.vstack(tet_list).astype(np.int64, copy=False)
    if tri_list:
        boundary_triangles = np.vstack(tri_list).astype(np.int64, copy=False)
        boundary_tags = np.concatenate(tri_tag_list).astype(np.int64, copy=False)
    else:
        boundary_triangles = np.zeros((0, 3), dtype=np.int64)
        boundary_tags = np.zeros((0,), dtype=np.int64)

    return MeshTopology(
        node_coords=node_coords,
        connectivity=connectivity,
        boundary_triangles=boundary_triangles,
        boundary_tags=boundary_tags,
    )


def _nodes_and_triangles_for_bc(
    bc: BoundaryCondition,
    physical_groups: Mapping[str, int],
    boundary_triangles: np.ndarray,
    boundary_tags: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    tags: set[int] = set()
    for face in bc.faces:
        tag = physical_groups.get(face)
        if tag is not None:
            tags.add(int(tag))

    if not tags:
        return np.array([], dtype=np.int64), np.zeros((0, 3), dtype=np.int64)

    mask = np.isin(boundary_tags, np.fromiter(tags, dtype=np.int64))
    tris = boundary_triangles[mask]
    if tris.size == 0:
        return np.array([], dtype=np.int64), np.zeros((0, 3), dtype=np.int64)
    nodes = np.unique(tris.reshape(-1))
    return nodes.astype(np.int64, copy=False), tris.astype(np.int64, copy=False)


def _apply_total_force_bc(
    nodal_forces: np.ndarray,
    node_ids: np.ndarray,
    bc: BoundaryCondition,
) -> None:
    if node_ids.size == 0:
        return

    fx = float(bc.value.get("fx", 0.0))
    fy = float(bc.value.get("fy", 0.0))
    fz = float(bc.value.get("fz", 0.0))
    total = np.array([fx, fy, fz], dtype=np.float64)
    per_node = total / float(node_ids.size)

    for nid in node_ids:
        base = int(nid) * 3
        nodal_forces[base:base + 3] += per_node


def _apply_pressure_bc(
    nodal_forces: np.ndarray,
    triangles: np.ndarray,
    node_coords: np.ndarray,
    bc: BoundaryCondition,
) -> None:
    if triangles.size == 0:
        return

    pressure = float(bc.value.get("pressure_mpa", 0.0))
    if abs(pressure) < 1e-12:
        return

    model_centroid = np.mean(node_coords, axis=0)

    for tri in triangles:
        p1, p2, p3 = node_coords[tri[0]], node_coords[tri[1]], node_coords[tri[2]]
        area_vec = 0.5 * np.cross(p2 - p1, p3 - p1)
        area = float(np.linalg.norm(area_vec))
        if area < 1e-15:
            continue

        unit_n = area_vec / (2.0 * area)
        tri_centroid = (p1 + p2 + p3) / 3.0

        # Pressure sign contract:
        # positive pressure_mpa is compressive inward (toward model centroid).
        outward = unit_n
        if float(np.dot(outward, tri_centroid - model_centroid)) < 0.0:
            outward = -outward
        inward = -outward

        total_force = pressure * area * inward  # MPa * mm^2 == N
        per_node = total_force / 3.0
        for nid in tri:
            base = int(nid) * 3
            nodal_forces[base:base + 3] += per_node


def _constitutive_matrix(E: float, nu: float) -> np.ndarray:
    if not (0.0 <= nu < 0.5):
        raise RuntimeError(f"Invalid Poisson ratio {nu}; expected 0 <= nu < 0.5")
    if E <= 0.0:
        raise RuntimeError(f"Invalid Young's modulus {E}; expected > 0")

    c = E / ((1.0 + nu) * (1.0 - 2.0 * nu))
    D = c * np.array(
        [
            [1.0 - nu, nu, nu, 0.0, 0.0, 0.0],
            [nu, 1.0 - nu, nu, 0.0, 0.0, 0.0],
            [nu, nu, 1.0 - nu, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, (1.0 - 2.0 * nu) / 2.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, (1.0 - 2.0 * nu) / 2.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, (1.0 - 2.0 * nu) / 2.0],
        ],
        dtype=np.float64,
    )
    return D


def _tet4_b_matrices(
    node_coords: np.ndarray,
    connectivity: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    xyz = node_coords[connectivity]  # (M, 4, 3)
    x1 = xyz[:, 0, :]
    x2 = xyz[:, 1, :]
    x3 = xyz[:, 2, :]
    x4 = xyz[:, 3, :]

    v1 = x2 - x1
    v2 = x3 - x1
    v3 = x4 - x1

    J = np.stack(
        [
            np.stack([v1[:, 0], v2[:, 0], v3[:, 0]], axis=1),
            np.stack([v1[:, 1], v2[:, 1], v3[:, 1]], axis=1),
            np.stack([v1[:, 2], v2[:, 2], v3[:, 2]], axis=1),
        ],
        axis=1,
    )  # (M,3,3)

    detJ = np.linalg.det(J)
    volumes = np.abs(detJ) / 6.0
    if np.any(volumes <= 1e-15):
        raise RuntimeError("Degenerate tetra elements detected (zero/near-zero volume)")

    invJ = np.linalg.inv(J)

    dN_ref = np.array(
        [
            [-1.0, -1.0, -1.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )

    grads = np.einsum("ni,mij->mnj", dN_ref, invJ)  # (M,4,3)

    n_elem = connectivity.shape[0]
    B = np.zeros((n_elem, 6, 12), dtype=np.float64)
    for a in range(4):
        dnx = grads[:, a, 0]
        dny = grads[:, a, 1]
        dnz = grads[:, a, 2]
        i = 3 * a

        B[:, 0, i + 0] = dnx
        B[:, 1, i + 1] = dny
        B[:, 2, i + 2] = dnz

        B[:, 3, i + 0] = dny
        B[:, 3, i + 1] = dnx

        B[:, 4, i + 1] = dnz
        B[:, 4, i + 2] = dny

        B[:, 5, i + 0] = dnz
        B[:, 5, i + 2] = dnx

    return B, volumes


def _assemble_global_stiffness(
    B: np.ndarray,
    D: np.ndarray,
    volumes: np.ndarray,
    connectivity: np.ndarray,
    num_dofs: int,
) -> tuple[Any, np.ndarray]:
    DB = np.einsum("ij,mjk->mik", D, B)
    Ke = np.einsum("mab,mbc,m->mac", np.transpose(B, (0, 2, 1)), DB, volumes)

    elem_dofs = np.empty((connectivity.shape[0], 12), dtype=np.int64)
    for a in range(4):
        base = connectivity[:, a] * 3
        elem_dofs[:, 3 * a + 0] = base + 0
        elem_dofs[:, 3 * a + 1] = base + 1
        elem_dofs[:, 3 * a + 2] = base + 2

    rows = np.repeat(elem_dofs, 12, axis=1).reshape(-1)
    cols = np.tile(elem_dofs, (1, 12)).reshape(-1)
    data = Ke.reshape(-1)

    K = sparse.coo_matrix((data, (rows, cols)), shape=(num_dofs, num_dofs)).tocsc()
    return K, elem_dofs


def _node_ids_to_dofs(node_ids: np.ndarray) -> np.ndarray:
    if node_ids.size == 0:
        return np.zeros((0,), dtype=np.int64)
    dofs = np.empty(node_ids.size * 3, dtype=np.int64)
    dofs[0::3] = node_ids * 3 + 0
    dofs[1::3] = node_ids * 3 + 1
    dofs[2::3] = node_ids * 3 + 2
    return np.unique(dofs)


def _topology_hash(node_coords: np.ndarray, connectivity: np.ndarray, element_order: str) -> str:
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(node_coords, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(connectivity, dtype=np.int64).tobytes())
    h.update(element_order.encode("utf-8"))
    return h.hexdigest()


def _material_hash(material: Material) -> str:
    payload = {
        "name": material.name,
        "E": float(material.youngs_modulus_mpa),
        "nu": float(material.poissons_ratio),
        "density": float(material.density_kg_m3),
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _bc_hash(boundary_conditions: Sequence[BoundaryCondition], fixed_nodes: set[int]) -> str:
    # Intentionally excludes force/pressure magnitudes to preserve factor reuse
    # for multi-RHS scenarios while still locking BC topology and fixed-DOF set.
    normalized: list[dict[str, Any]] = []
    for bc in boundary_conditions:
        normalized.append(
            {
                "bc_type": bc.bc_type,
                "faces": sorted(bc.faces),
                "value_keys": sorted(bc.value.keys()),
            }
        )

    payload = {
        "boundary_conditions": sorted(
            normalized,
            key=lambda x: (x["bc_type"], tuple(x["faces"]), tuple(x["value_keys"])),
        ),
        "fixed_nodes": sorted(int(n) for n in fixed_nodes),
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _options_hash(options: Mapping[str, Any] | None) -> str:
    if not options:
        return ""
    return _sha256_text(json.dumps(dict(options), sort_keys=True, separators=(",", ":"), default=str))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
