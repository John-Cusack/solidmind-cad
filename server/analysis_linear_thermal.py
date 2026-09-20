"""Constant-property linear tetrahedral BDF1 heat conduction in SI units.

Consistent capacity, isotropic conductivity and inward-positive Neumann flux.
Separate bodies retain separate nodal temperatures; contact belongs to the caller.
Matrices and two timestep factors are reused, not reconstructed for each trial.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import meshio
import numpy as np
from scipy import sparse
from scipy.sparse.linalg import splu


@dataclass(frozen=True, slots=True)
class LinearThermalStep:
    temperatures_k: np.ndarray
    wall_temps_k: dict[str, float]
    energy_change_j: float
    boundary_input_j: float
    balance_j: float
    residual_norm: float
    min_temperature_k: float
    max_temperature_k: float


class LinearThermalBody:
    """A single material on an unchanged SI mesh, with a bounded factor cache."""

    @classmethod
    def from_mesh(
        cls,
        mesh_path,
        boundary_tags: dict[str, int],
        *,
        density_kg_m3: float,
        heat_capacity_j_kg_k: float,
        conductivity_w_m_k: float,
    ) -> LinearThermalBody:
        parameters = (density_kg_m3, heat_capacity_j_kg_k, conductivity_w_m_k)
        if not all(np.isfinite(value) and value > 0 for value in parameters):
            raise ValueError("Thermal material constants must be finite and positive")
        mesh = meshio.read(mesh_path)
        if any(cell.type not in ("tetra", "triangle", "line", "vertex") for cell in mesh.cells):
            raise ValueError(
                "Only linear tetrahedral solids and triangular boundaries are supported"
            )
        points = np.asarray(mesh.points, dtype=np.float64)
        tetra = mesh.cells_dict.get("tetra")
        triangles = mesh.cells_dict.get("triangle")
        tags = mesh.cell_data_dict.get("gmsh:physical", {}).get("triangle")
        if (
            tetra is None
            or not len(tetra)
            or triangles is None
            or tags is None
            or points.ndim != 2
            or points.shape[1] != 3
            or not np.isfinite(points).all()
        ):
            raise ValueError(
                "A finite SI tetrahedral mesh with tagged boundary triangles is required"
            )
        body = cls()
        body.points = points
        body.tetrahedra = np.asarray(tetra, dtype=np.int32)
        body.node_count = len(points)
        body._factors = OrderedDict()
        body._responses = OrderedDict()
        body.factor_backend = "not_factorized"
        shape = (body.node_count, body.node_count)
        mass = sparse.csr_matrix(shape, dtype=np.float64)
        stiffness = sparse.csr_matrix(shape, dtype=np.float64)
        volume = 0.0
        consistent = np.ones((4, 4)) + np.eye(4)
        # Bound temporary gradients/COO arrays on million-element CAD meshes.
        for offset in range(0, len(tetra), 65536):
            cells = body.tetrahedra[offset : offset + 65536]
            xyz = points[cells]
            jacobian = np.transpose(xyz[:, 1:] - xyz[:, :1], (0, 2, 1))
            determinants = np.linalg.det(jacobian)
            scales = np.linalg.norm(jacobian, axis=(1, 2)) ** 3
            if np.any(np.abs(determinants) <= np.finfo(float).eps * scales):
                raise ValueError("Degenerate or numerically singular thermal tetrahedron")
            volumes = np.abs(determinants) / 6
            gradients = np.empty((len(cells), 4, 3))
            gradients[:, 1:] = np.linalg.inv(jacobian)
            gradients[:, 0] = -gradients[:, 1:].sum(axis=1)
            local_k = (
                conductivity_w_m_k
                * volumes[:, None, None]
                * np.einsum("nik,njk->nij", gradients, gradients)
            )
            local_m = (
                density_kg_m3 * heat_capacity_j_kg_k / 20 * volumes[:, None, None] * consistent
            )
            rows = np.repeat(cells, 4, axis=1).ravel()
            cols = np.tile(cells, (1, 4)).ravel()
            stiffness += sparse.coo_matrix((local_k.ravel(), (rows, cols)), shape=shape).tocsr()
            mass += sparse.coo_matrix((local_m.ravel(), (rows, cols)), shape=shape).tocsr()
            volume += float(volumes.sum())
        body._mass = mass.tocsc()
        body._stiffness = stiffness.tocsc()
        body._capacity_weights = np.asarray(mass.sum(axis=1)).ravel()
        if np.any(body._capacity_weights <= 0):
            raise ValueError("Mesh contains nodes outside its positive-volume thermal body")
        body.volume_m3 = volume
        body._patches = {}
        body.patch_areas_m2 = {}
        if len(set(boundary_tags.values())) != len(boundary_tags):
            raise ValueError("Thermal patch tags must be distinct")
        for name, tag in boundary_tags.items():
            faces = triangles[tags == tag]
            if not len(faces):
                raise ValueError(f"Thermal boundary patch {name!r} has no triangles")
            xyz = points[faces]
            areas = (
                np.linalg.norm(np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0]), axis=1) / 2
            )
            if np.any(areas <= 0):
                raise ValueError(f"Degenerate thermal boundary triangle in {name!r}")
            nodes, inverse = np.unique(faces.ravel(), return_inverse=True)
            weights = np.bincount(inverse, weights=np.repeat(areas / 3, 3))
            body._patches[name] = (nodes, weights)
            body.patch_areas_m2[name] = float(weights.sum())
        return body

    def uniform_temperature(self, temperature_k: float) -> np.ndarray:
        if not np.isfinite(temperature_k) or temperature_k <= 0:
            raise ValueError("Temperature must be finite and above absolute zero")
        return np.full(self.node_count, temperature_k, dtype=np.float64)

    def _temperature(self, temperatures_k) -> np.ndarray:
        values = np.asarray(temperatures_k, dtype=np.float64)
        if values.shape != (self.node_count,) or not np.isfinite(values).all():
            raise ValueError("Temperature field must cover every thermal node with finite values")
        return values

    def wall_temperatures(self, temperatures_k) -> dict[str, float]:
        values = self._temperature(temperatures_k)
        return {
            name: float(weights @ values[nodes] / self.patch_areas_m2[name])
            for name, (nodes, weights) in self._patches.items()
        }

    def energy_j(self, temperatures_k) -> float:
        return float(self._capacity_weights @ self._temperature(temperatures_k))

    def _factor(self, h: float):
        cached = self._factors.get(h)
        if cached is not None:
            self._factors.move_to_end(h)
            return cached
        matrix = self._stiffness + self._mass / h
        try:
            import sksparse.cholmod as cholmod
        except ImportError:
            factor = splu(matrix)
            solve = factor.solve
            self.factor_backend = "scipy_superlu"
        else:
            # Same version compatibility as the repository's structural adapter.
            if hasattr(cholmod, "cho_factor"):
                factor = cholmod.cho_factor(matrix, order="metis")
                solve = factor.solve
            else:
                factor = cholmod.cholesky(matrix, ordering_method="metis")
                solve = factor.solve_A
            self.factor_backend = "suitesparse_cholmod"
        cached = (matrix, solve)
        self._factors[h] = cached
        while len(self._factors) > 2:
            self._factors.popitem(last=False)
        return cached

    def step(
        self, temperatures_k, dt_s: float, flux_w_m2: dict[str, float], *, nsub: int = 1
    ) -> LinearThermalStep:
        before = self._temperature(temperatures_k)
        if (
            not np.isfinite(dt_s)
            or dt_s <= 0
            or isinstance(nsub, bool)
            or not isinstance(nsub, int)
            or nsub < 1
        ):
            raise ValueError("Positive duration and positive integral substep count are required")
        if set(flux_w_m2) != set(self._patches):
            raise ValueError("Heat flux must explicitly cover every named thermal patch")
        load = np.zeros(self.node_count)
        power = 0.0
        for name, (nodes, weights) in self._patches.items():
            value = float(flux_w_m2[name])
            if not np.isfinite(value):
                raise ValueError("Nonfinite thermal boundary flux")
            load[nodes] += value * weights
            power += value * self.patch_areas_m2[name]
        h = dt_s / nsub
        matrix, solve = self._factor(h)
        current = before
        residual_norm = 0.0
        low, high = float(before.min()), float(before.max())
        for _ in range(nsub):
            # Solve the increment to avoid subtracting large absolute-temperature
            # heat contents when the physical heat transfer is small.
            rhs = load - self._stiffness @ current
            increment = np.asarray(solve(rhs)).reshape(-1)
            residual = matrix @ increment - rhs
            relative = float(np.linalg.norm(residual) / max(np.linalg.norm(rhs), 1.0))
            if not np.isfinite(increment).all() or relative > 1e-7:
                raise ArithmeticError(f"Thermal linear solve residual {relative:g} is unacceptable")
            residual_norm = max(residual_norm, relative)
            current = current + increment
            low = min(low, float(current.min()))
            high = max(high, float(current.max()))
        change = float(self._capacity_weights @ (current - before))
        boundary = power * dt_s
        return LinearThermalStep(
            current,
            self.wall_temperatures(current),
            change,
            boundary,
            change - boundary,
            residual_norm,
            low,
            high,
        )

    def boundary_response(self, dt_s: float, *, nsub: int = 1) -> np.ndarray:
        """Patch-mean kelvin response to one watt inward on each named patch.

        This is a linear-operator probe, not a physical zero-kelvin initial state.
        It supplies the exact spatial response for an implicit contact solve.
        """
        key = (dt_s, nsub)
        if key in self._responses:
            self._responses.move_to_end(key)
            return self._responses[key]
        names = tuple(self._patches)
        response = np.empty((len(names), len(names)))
        zero = np.zeros(self.node_count)
        flux = dict.fromkeys(names, 0.0)
        for column, name in enumerate(names):
            flux[name] = 1.0 / self.patch_areas_m2[name]
            trial = self.step(zero, dt_s, flux, nsub=nsub)
            response[:, column] = [trial.wall_temps_k[patch] for patch in names]
            flux[name] = 0.0
        response.setflags(write=False)
        self._responses[key] = response
        while len(self._responses) > 2:
            self._responses.popitem(last=False)
        return response

    def write_vtu(self, path, temperatures_k) -> None:
        values = self._temperature(temperatures_k)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meshio.write(
            path,
            meshio.Mesh(
                self.points, [("tetra", self.tetrahedra)], point_data={"temperature": values}
            ),
        )
