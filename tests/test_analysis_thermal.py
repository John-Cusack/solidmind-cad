"""Tests for Elmer thermal analysis integration."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.analysis_models import (
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    Material,
    MeshInfo,
)
from server.analysis_solvers import MockFieldSolver, get_solver

# Module under test for tool-level patching
import server.tools_analysis as tools_mod


def _thermal_material() -> Material:
    return Material(
        name="copper_c11000",
        youngs_modulus_mpa=117_000,
        poissons_ratio=0.34,
        density_kg_m3=8940,
        yield_strength_mpa=69,
        thermal_conductivity_w_mk=391,
        specific_heat_j_kgk=385,
        thermal_expansion_1_k=16.5e-6,
        electrical_conductivity_s_m=5.96e7,
    )


def _make_mesh_info() -> MeshInfo:
    return MeshInfo(
        path="/tmp/test.msh",
        num_nodes=100,
        num_elements=200,
        element_type="tet4",
        physical_groups={"Face1": 1, "Face2": 2},
    )


# ---------------------------------------------------------------------------
# Tool-level validation tests (MockFieldSolver, no Elmer needed)
# ---------------------------------------------------------------------------


class TestThermalCheckValidation(unittest.TestCase):
    """Test analysis_thermal_check input validation and mock pipeline."""

    def test_unknown_material(self) -> None:
        from server.tools_analysis import analysis_thermal_check

        result = analysis_thermal_check(
            body="TestBody",
            material="unobtanium",
            boundary_conditions=[
                {"bc_type": "temperature", "faces": ["Face1"], "value": {"temperature_k": 400}},
            ],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_MATERIAL")

    def test_no_thermal_bcs(self) -> None:
        from server.tools_analysis import analysis_thermal_check

        result = analysis_thermal_check(
            body="TestBody",
            material="copper",
            boundary_conditions=[
                {"bc_type": "fixed", "faces": ["Face1"]},
            ],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_THERMAL_BCS")

    def test_zero_conductivity(self) -> None:
        from server.tools_analysis import analysis_thermal_check

        # Material with zero thermal conductivity
        mat_dict = {
            "name": "insulator",
            "youngs_modulus_mpa": 1000,
            "poissons_ratio": 0.3,
            "density_kg_m3": 1000,
            "yield_strength_mpa": 50,
            "thermal_conductivity_w_mk": 0.0,
        }
        result = analysis_thermal_check(
            body="TestBody",
            material=mat_dict,
            boundary_conditions=[
                {"bc_type": "temperature", "faces": ["Face1"], "value": {"temperature_k": 400}},
            ],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ZERO_CONDUCTIVITY")

    def test_no_bcs(self) -> None:
        from server.tools_analysis import analysis_thermal_check

        result = analysis_thermal_check(
            body="TestBody",
            material="copper",
            boundary_conditions=[],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_BCS")

    def test_end_to_end_mock(self) -> None:
        """Full pipeline with MockFieldSolver — no Elmer needed."""
        from server.tools_analysis import analysis_thermal_check

        with (
            patch.object(tools_mod, "cad_export_body") as mock_export,
            patch.object(tools_mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = _make_mesh_info()

            result = analysis_thermal_check(
                body="Heatsink",
                material="copper",
                boundary_conditions=[
                    {
                        "bc_type": "temperature",
                        "faces": ["Face1"],
                        "value": {"temperature_k": 350},
                    },
                    {
                        "bc_type": "temperature",
                        "faces": ["Face2"],
                        "value": {"temperature_k": 300},
                    },
                ],
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("scalar_fields", result)
        # Should have a temperature field
        temp_fields = [
            f for f in result["scalar_fields"]
            if f["field_name"] == "temperature"
        ]
        self.assertEqual(len(temp_fields), 1)
        self.assertEqual(temp_fields[0]["unit"], "K")

    def test_convection_bc_format(self) -> None:
        """Convection BC properly parsed and forwarded."""
        from server.tools_analysis import analysis_thermal_check

        with (
            patch.object(tools_mod, "cad_export_body") as mock_export,
            patch.object(tools_mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = _make_mesh_info()

            result = analysis_thermal_check(
                body="Fin",
                material="aluminum",
                boundary_conditions=[
                    {
                        "bc_type": "temperature",
                        "faces": ["Face1"],
                        "value": {"temperature_k": 400},
                    },
                    {
                        "bc_type": "convection",
                        "faces": ["Face2"],
                        "value": {"htc_w_m2k": 25.0, "t_ambient_k": 293.15},
                    },
                ],
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["solver_name"], "mock")

    def test_multiple_bcs(self) -> None:
        """Mixed temperature + heat_flux + convection BCs."""
        from server.tools_analysis import analysis_thermal_check

        with (
            patch.object(tools_mod, "cad_export_body") as mock_export,
            patch.object(tools_mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = _make_mesh_info()

            result = analysis_thermal_check(
                body="Block",
                material="steel",
                boundary_conditions=[
                    {
                        "bc_type": "temperature",
                        "faces": ["Face1"],
                        "value": {"temperature_k": 500},
                    },
                    {
                        "bc_type": "heat_flux",
                        "faces": ["Face2"],
                        "value": {"flux_w_m2": 10000},
                    },
                    {
                        "bc_type": "convection",
                        "faces": ["Face3"],
                        "value": {"htc_w_m2k": 50, "t_ambient_k": 300},
                    },
                ],
                solver="mock",
            )

        self.assertTrue(result["ok"])

    def test_max_temperature_k_propagated(self) -> None:
        """max_temperature_k should be injected into BC values."""
        from server.tools_analysis import analysis_thermal_check

        with (
            patch.object(tools_mod, "cad_export_body") as mock_export,
            patch.object(tools_mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = _make_mesh_info()

            result = analysis_thermal_check(
                body="Block",
                material="copper",
                boundary_conditions=[
                    {
                        "bc_type": "temperature",
                        "faces": ["Face1"],
                        "value": {"temperature_k": 350},
                    },
                    {
                        "bc_type": "temperature",
                        "faces": ["Face2"],
                        "value": {"temperature_k": 300},
                    },
                ],
                solver="mock",
                max_temperature_k=500,
            )

        self.assertTrue(result["ok"])
        # Check that overtemperature check exists
        checks = result.get("checks", [])
        overtemp = [c for c in checks if c["name"] == "overtemperature"]
        self.assertEqual(len(overtemp), 1)
        self.assertEqual(overtemp[0]["limit"], 500)


# ---------------------------------------------------------------------------
# ElmerSolver unit tests (no binary needed)
# ---------------------------------------------------------------------------


class TestElmerSolverUnit(unittest.TestCase):
    """Unit tests for ElmerSolver class without requiring the binary."""

    def test_available_detection(self) -> None:
        from server.analysis_solver_elmer import ElmerSolver

        solver = ElmerSolver()
        # With both binaries missing
        with patch.object(shutil, "which", return_value=None):
            ok, msg = solver.available()
            self.assertFalse(ok)
            self.assertIn("ElmerSolver", msg)

        # With both present
        with patch.object(shutil, "which", return_value="/usr/bin/ElmerSolver"):
            ok, msg = solver.available()
            self.assertTrue(ok)

    def test_name_and_types(self) -> None:
        from server.analysis_solver_elmer import ElmerSolver

        solver = ElmerSolver()
        self.assertEqual(solver.name(), "elmer")
        types = solver.analysis_types()
        self.assertIn(AnalysisType.THERMAL, types)
        self.assertIn(AnalysisType.CONJUGATE_HEAT, types)

    def test_write_sif_structure(self) -> None:
        """Verify .sif file has correct sections and keywords."""
        from server.analysis_solver_elmer import ElmerSolver

        solver = ElmerSolver()
        mat = _thermal_material()
        spec = AnalysisSpec(
            analysis_type=AnalysisType.THERMAL,
            body="TestBody",
            material=mat,
            boundary_conditions=(
                BoundaryCondition(
                    bc_type="temperature",
                    faces=("Face1",),
                    value={"temperature_k": 400},
                ),
                BoundaryCondition(
                    bc_type="convection",
                    faces=("Face2",),
                    value={"htc_w_m2k": 25, "t_ambient_k": 293.15},
                ),
            ),
            mesh_size=0.0,
        )
        mesh_info = _make_mesh_info()

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            mesh_db = work_dir / "mesh_db"
            mesh_db.mkdir()

            # Call internal _build_sif
            sif_lines = solver._build_sif(spec, mesh_info, mesh_db)
            sif_text = "\n".join(sif_lines)

            # Check required sections
            self.assertIn("Header", sif_text)
            self.assertIn("Simulation", sif_text)
            self.assertIn("Body 1", sif_text)
            self.assertIn("Material 1", sif_text)
            self.assertIn("Solver 1", sif_text)
            self.assertIn("Equation 1", sif_text)
            self.assertIn("Boundary Condition 1", sif_text)
            self.assertIn("Boundary Condition 2", sif_text)

            # Check material properties
            self.assertIn("Heat Conductivity = 391", sif_text)
            self.assertIn("Heat Capacity = 385", sif_text)
            self.assertIn("Density = 8940", sif_text)

            # Check solver keywords
            self.assertIn("Heat Equation", sif_text)
            self.assertIn("HeatSolve", sif_text)
            self.assertIn("BiCGStab", sif_text)

    def test_bc_mapping(self) -> None:
        """Verify BC types map to correct Elmer keywords."""
        from server.analysis_solver_elmer import ElmerSolver

        solver = ElmerSolver()
        mesh_info = _make_mesh_info()

        # Temperature BC
        bc_temp = BoundaryCondition(
            bc_type="temperature",
            faces=("Face1",),
            value={"temperature_k": 500},
        )
        lines = solver._bc_to_sif(1, bc_temp, mesh_info)
        text = "\n".join(lines)
        self.assertIn("Temperature = 500", text)

        # Heat flux BC
        bc_flux = BoundaryCondition(
            bc_type="heat_flux",
            faces=("Face2",),
            value={"flux_w_m2": 5000},
        )
        lines = solver._bc_to_sif(2, bc_flux, mesh_info)
        text = "\n".join(lines)
        self.assertIn("Heat Flux = 5000", text)

        # Convection BC
        bc_conv = BoundaryCondition(
            bc_type="convection",
            faces=("Face1",),
            value={"htc_w_m2k": 25, "t_ambient_k": 300},
        )
        lines = solver._bc_to_sif(3, bc_conv, mesh_info)
        text = "\n".join(lines)
        self.assertIn("Heat Transfer Coefficient = 25", text)
        self.assertIn("External Temperature = 300", text)

    def test_parse_vtu_mock(self) -> None:
        """Verify .vtu parsing with synthetic meshio data."""
        from server.analysis_solver_elmer import ElmerSolver

        solver = ElmerSolver()
        mat = _thermal_material()
        spec = AnalysisSpec(
            analysis_type=AnalysisType.THERMAL,
            body="TestBody",
            material=mat,
            boundary_conditions=(
                BoundaryCondition(
                    bc_type="temperature",
                    faces=("Face1",),
                    value={"temperature_k": 400},
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Create a mock .vtu file using meshio
            try:
                import meshio
                import numpy as np
            except ImportError:
                self.skipTest("meshio or numpy not installed")

            # Synthetic mesh: 4 points (one tet)
            points = np.array([
                [0, 0, 0],
                [1, 0, 0],
                [0, 1, 0],
                [0, 0, 1],
            ], dtype=float)
            cells = [("tetra", np.array([[0, 1, 2, 3]]))]
            temp_data = np.array([300.0, 350.0, 375.0, 400.0])

            mesh = meshio.Mesh(
                points=points,
                cells=cells,
                point_data={"temperature": temp_data},
            )
            vtu_path = work_dir / "case.vtu"
            meshio.write(str(vtu_path), mesh)

            result = solver.parse_results(work_dir, spec)

        self.assertEqual(result.solver_name, "elmer")
        # Should have temperature field
        temp_fields = [
            f for f in result.scalar_fields
            if f.field_name == "temperature"
        ]
        self.assertEqual(len(temp_fields), 1)
        self.assertAlmostEqual(temp_fields[0].min_val, 300.0, places=1)
        self.assertAlmostEqual(temp_fields[0].max_val, 400.0, places=1)
        # Max location should be at point [0, 0, 1]
        self.assertAlmostEqual(temp_fields[0].max_location_xyz[2], 1.0, places=2)


# ---------------------------------------------------------------------------
# MockFieldSolver thermal tests
# ---------------------------------------------------------------------------


class TestMockThermalResult(unittest.TestCase):
    """Test MockFieldSolver thermal path."""

    def test_mock_thermal_returns_temperature_field(self) -> None:
        solver = MockFieldSolver()
        self.assertIn(AnalysisType.THERMAL, solver.analysis_types())

        mat = _thermal_material()
        spec = AnalysisSpec(
            analysis_type=AnalysisType.THERMAL,
            body="TestBody",
            material=mat,
            boundary_conditions=(
                BoundaryCondition(
                    bc_type="temperature",
                    faces=("Face1",),
                    value={"temperature_k": 400},
                ),
                BoundaryCondition(
                    bc_type="temperature",
                    faces=("Face2",),
                    value={"temperature_k": 300},
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            mesh_info = _make_mesh_info()
            solver.write_input(spec, mesh_info, work_dir)
            solver.run(work_dir / "mock.inp", work_dir)
            result = solver.parse_results(work_dir, spec)

        self.assertEqual(result.solver_name, "mock")
        temp_fields = [f for f in result.scalar_fields if f.field_name == "temperature"]
        self.assertEqual(len(temp_fields), 1)
        self.assertAlmostEqual(temp_fields[0].min_val, 300.0, places=0)
        self.assertAlmostEqual(temp_fields[0].max_val, 400.0, places=0)

    def test_mock_structural_unchanged(self) -> None:
        """Structural path still works after thermal additions."""
        solver = MockFieldSolver()
        mat = _thermal_material()
        spec = AnalysisSpec(
            analysis_type=AnalysisType.STRUCTURAL,
            body="TestBody",
            material=mat,
            boundary_conditions=(
                BoundaryCondition(
                    bc_type="fixed",
                    faces=("Face1",),
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            solver.write_input(spec, _make_mesh_info(), work_dir)
            solver.run(work_dir / "mock.inp", work_dir)
            result = solver.parse_results(work_dir, spec)

        self.assertGreater(result.max_von_mises_mpa, 0)
        vm_fields = [f for f in result.scalar_fields if f.field_name == "von_mises_stress"]
        self.assertEqual(len(vm_fields), 1)


# ---------------------------------------------------------------------------
# Solver registry test
# ---------------------------------------------------------------------------


class TestThermalSolverRegistry(unittest.TestCase):
    """Verify thermal solver auto-selection."""

    def test_mock_selected_for_thermal(self) -> None:
        solver = get_solver("", AnalysisType.THERMAL)
        self.assertIsNotNone(solver)
        # Mock should always be available for thermal
        self.assertIn(AnalysisType.THERMAL, solver.analysis_types())

    def test_elmer_not_selected_for_structural(self) -> None:
        """Elmer should not be auto-selected for structural analysis."""
        solver = get_solver("", AnalysisType.STRUCTURAL)
        if solver is not None:
            self.assertNotEqual(solver.name(), "elmer")


# ---------------------------------------------------------------------------
# Material EM property tests
# ---------------------------------------------------------------------------


class TestMaterialEMProperties(unittest.TestCase):
    """Verify EM properties on Material dataclass."""

    def test_em_fields_exist(self) -> None:
        mat = _thermal_material()
        self.assertAlmostEqual(mat.electrical_conductivity_s_m, 5.96e7, places=0)
        self.assertAlmostEqual(mat.relative_permeability, 1.0)
        self.assertAlmostEqual(mat.relative_permittivity, 1.0)

    def test_em_fields_roundtrip(self) -> None:
        mat = _thermal_material()
        d = mat.to_dict()
        self.assertIn("electrical_conductivity_s_m", d)
        self.assertIn("relative_permeability", d)
        self.assertIn("relative_permittivity", d)

        mat2 = Material.from_dict(d)
        self.assertAlmostEqual(mat2.electrical_conductivity_s_m, mat.electrical_conductivity_s_m)
        self.assertAlmostEqual(mat2.relative_permeability, mat.relative_permeability)

    def test_em_fields_backward_compatible(self) -> None:
        """from_dict should work without EM fields (backward compat)."""
        d = {
            "name": "old_mat",
            "youngs_modulus_mpa": 200_000,
            "poissons_ratio": 0.3,
            "density_kg_m3": 7800,
            "yield_strength_mpa": 250,
        }
        mat = Material.from_dict(d)
        self.assertEqual(mat.electrical_conductivity_s_m, 0.0)
        self.assertEqual(mat.relative_permeability, 1.0)
        self.assertEqual(mat.relative_permittivity, 1.0)

    def test_material_library_em_values(self) -> None:
        """Check that metal materials have EM values populated."""
        from server.analysis_materials import get_material

        copper = get_material("copper")
        self.assertIsNotNone(copper)
        self.assertGreater(copper.electrical_conductivity_s_m, 0)

        steel = get_material("steel_1018")
        self.assertIsNotNone(steel)
        self.assertGreater(steel.electrical_conductivity_s_m, 0)
        self.assertGreater(steel.relative_permeability, 1.0)

        pla = get_material("pla")
        self.assertIsNotNone(pla)
        self.assertEqual(pla.electrical_conductivity_s_m, 0.0)
        self.assertEqual(pla.relative_permeability, 1.0)


# ---------------------------------------------------------------------------
# Real Elmer solver tests (skipped if not installed)
# ---------------------------------------------------------------------------

_ELMER_AVAILABLE = bool(
    shutil.which("ElmerSolver") and shutil.which("ElmerGrid")
)


def _create_box_step(
    work_dir: Path,
    size: tuple[float, float, float] = (10, 10, 10),
    mesh_size: float = 3.0,
    face_groups: dict[str, list[str]] | None = None,
) -> tuple[Path, "MeshInfo"]:
    """Create a box STEP via Gmsh OCC + mesh it. Returns (step_path, mesh_info)."""
    import gmsh

    from server.analysis_mesh import mesh_step_to_msh

    step_path = work_dir / "box.step"
    gmsh.initialize()
    try:
        gmsh.model.occ.addBox(0, 0, 0, *size)
        gmsh.model.occ.synchronize()
        gmsh.write(str(step_path))
    finally:
        gmsh.finalize()

    if face_groups is None:
        face_groups = {}

    mesh_info = mesh_step_to_msh(
        step_path=str(step_path),
        face_groups=face_groups,
        mesh_size=mesh_size,
        msh_version=2.2,
    )
    return step_path, mesh_info


def _run_elmer(
    work_dir: Path,
    mesh_info: "MeshInfo",
    material: Material,
    bcs: tuple[BoundaryCondition, ...],
) -> "FieldResult":
    """Run the full Elmer pipeline: write_input → run → parse_results."""
    from server.analysis_solver_elmer import ElmerSolver

    solver = ElmerSolver()
    spec = AnalysisSpec(
        analysis_type=AnalysisType.THERMAL,
        body="TestBody",
        material=material,
        boundary_conditions=bcs,
    )
    input_path = solver.write_input(spec, mesh_info, work_dir)
    solver.run(input_path, work_dir)
    return solver.parse_results(work_dir, spec)


def _get_temp_field(result: "FieldResult") -> "ScalarFieldSummary":
    """Extract the temperature ScalarFieldSummary from a FieldResult."""
    from server.analysis_models import ScalarFieldSummary

    fields = [f for f in result.scalar_fields if f.field_name == "temperature"]
    assert len(fields) == 1, f"Expected 1 temperature field, got {len(fields)}"
    return fields[0]


@unittest.skipUnless(_ELMER_AVAILABLE, "ElmerSolver/ElmerGrid not installed")
class TestElmerRealFeatures(unittest.TestCase):
    """Real-solver integration tests — full Gmsh→ElmerGrid→ElmerSolver→VTU pipeline."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import gmsh  # noqa: F401
            import meshio  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("gmsh or meshio not installed")

    # ---- Test 1: linear gradient (enhanced) ----

    def test_linear_gradient(self) -> None:
        """Cube with T=400K / T=300K on opposing faces → linear gradient."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _, mesh_info = _create_box_step(
                work_dir,
                face_groups={
                    "bc_0_temperature": ["Face1"],
                    "bc_1_temperature": ["Face6"],
                },
            )

            result = _run_elmer(
                work_dir,
                mesh_info,
                _thermal_material(),
                bcs=(
                    BoundaryCondition(
                        bc_type="temperature",
                        faces=("Face1",),
                        value={"temperature_k": 400},
                    ),
                    BoundaryCondition(
                        bc_type="temperature",
                        faces=("Face6",),
                        value={"temperature_k": 300},
                    ),
                ),
            )

        self.assertNotEqual(result.status, CheckStatus.FAIL)
        tf = _get_temp_field(result)
        self.assertAlmostEqual(tf.min_val, 300, delta=5)
        self.assertAlmostEqual(tf.max_val, 400, delta=5)
        # Mean should be midpoint for linear gradient
        self.assertAlmostEqual(tf.mean_val, 350, delta=10)

    # ---- Test 2: heat flux + convection equilibrium ----

    def test_heat_flux_equilibrium(self) -> None:
        """Heat flux on Face1 + convection on Face6 → equilibrium T ≈ T_amb + Q/h."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            _, mesh_info = _create_box_step(
                work_dir,
                face_groups={
                    "bc_0_heat_flux": ["Face1"],
                    "bc_1_convection": ["Face6"],
                },
            )

            result = _run_elmer(
                work_dir,
                mesh_info,
                _thermal_material(),
                bcs=(
                    BoundaryCondition(
                        bc_type="heat_flux",
                        faces=("Face1",),
                        value={"flux_w_m2": 1000},
                    ),
                    BoundaryCondition(
                        bc_type="convection",
                        faces=("Face6",),
                        value={"htc_w_m2k": 100, "t_ambient_k": 293},
                    ),
                ),
            )

        self.assertNotEqual(result.status, CheckStatus.FAIL)
        tf = _get_temp_field(result)
        # At equilibrium: Q_in = Q_out → T_surface ≈ T_amb + Q/h = 293 + 10 = 303 K
        # Allow wider tolerance for 3D FEM discretization
        self.assertGreater(tf.max_val, 293)
        self.assertLess(tf.max_val, 320)

    # ---- Test 3: material comparison (conductivity) ----

    def test_material_comparison_conductivity(self) -> None:
        """Same BCs, different materials → both converge to same T range for 1D Dirichlet."""
        results: dict[str, float] = {}
        materials = {
            "copper": Material(
                name="copper", youngs_modulus_mpa=117_000, poissons_ratio=0.34,
                density_kg_m3=8940, yield_strength_mpa=69,
                thermal_conductivity_w_mk=391, specific_heat_j_kgk=385,
            ),
            "steel": Material(
                name="steel", youngs_modulus_mpa=200_000, poissons_ratio=0.3,
                density_kg_m3=7800, yield_strength_mpa=250,
                thermal_conductivity_w_mk=51.9, specific_heat_j_kgk=490,
            ),
        }

        for mat_name, mat in materials.items():
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = Path(tmpdir)
                _, mesh_info = _create_box_step(
                    work_dir,
                    face_groups={
                        "bc_0_temperature": ["Face1"],
                        "bc_1_temperature": ["Face6"],
                    },
                )

                result = _run_elmer(
                    work_dir,
                    mesh_info,
                    mat,
                    bcs=(
                        BoundaryCondition(
                            bc_type="temperature",
                            faces=("Face1",),
                            value={"temperature_k": 400},
                        ),
                        BoundaryCondition(
                            bc_type="temperature",
                            faces=("Face6",),
                            value={"temperature_k": 300},
                        ),
                    ),
                )

            self.assertNotEqual(result.status, CheckStatus.FAIL)
            tf = _get_temp_field(result)
            self.assertAlmostEqual(tf.min_val, 300, delta=5)
            self.assertAlmostEqual(tf.max_val, 400, delta=5)
            results[mat_name] = tf.mean_val

        # For 1D steady Dirichlet-Dirichlet, T is independent of k → same solution
        self.assertAlmostEqual(
            results["copper"], results["steel"], delta=5,
        )

    # ---- Test 4: convection cooling ----

    def test_convection_cooling(self) -> None:
        """T=500K on Face1, convection on all other faces → cooling effect."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            conv_faces = ["Face2", "Face3", "Face4", "Face5", "Face6"]
            _, mesh_info = _create_box_step(
                work_dir,
                face_groups={
                    "bc_0_temperature": ["Face1"],
                    "bc_1_convection": conv_faces,
                },
            )

            result = _run_elmer(
                work_dir,
                mesh_info,
                _thermal_material(),
                bcs=(
                    BoundaryCondition(
                        bc_type="temperature",
                        faces=("Face1",),
                        value={"temperature_k": 500},
                    ),
                    BoundaryCondition(
                        bc_type="convection",
                        faces=tuple(conv_faces),
                        value={"htc_w_m2k": 50, "t_ambient_k": 293},
                    ),
                ),
            )

        self.assertNotEqual(result.status, CheckStatus.FAIL)
        tf = _get_temp_field(result)
        # T_max = 500K (Dirichlet), T_min > 293K (convection doesn't reach ambient)
        self.assertAlmostEqual(tf.max_val, 500, delta=5)
        self.assertGreater(tf.min_val, 293)
        self.assertLess(tf.min_val, 500)
        # Mean should be pulled below 500 by cooling
        self.assertLess(tf.mean_val, 500)

    # ---- Test 5: iterate with increasing flux ----

    def test_iterate_increasing_flux(self) -> None:
        """Increasing heat flux → monotonically increasing max temperature."""
        fluxes = [500, 2000, 5000]
        max_temps: list[float] = []

        for flux in fluxes:
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = Path(tmpdir)
                _, mesh_info = _create_box_step(
                    work_dir,
                    face_groups={
                        "bc_0_heat_flux": ["Face1"],
                        "bc_1_convection": ["Face6"],
                    },
                )

                result = _run_elmer(
                    work_dir,
                    mesh_info,
                    _thermal_material(),
                    bcs=(
                        BoundaryCondition(
                            bc_type="heat_flux",
                            faces=("Face1",),
                            value={"flux_w_m2": flux},
                        ),
                        BoundaryCondition(
                            bc_type="convection",
                            faces=("Face6",),
                            value={"htc_w_m2k": 100, "t_ambient_k": 293},
                        ),
                    ),
                )

            self.assertNotEqual(result.status, CheckStatus.FAIL)
            tf = _get_temp_field(result)
            self.assertGreater(tf.max_val, 293)
            max_temps.append(tf.max_val)

        # Strictly increasing
        for i in range(len(max_temps) - 1):
            self.assertLess(
                max_temps[i], max_temps[i + 1],
                f"Expected T_max({fluxes[i]}) < T_max({fluxes[i + 1]}), "
                f"got {max_temps[i]} >= {max_temps[i + 1]}",
            )

    # ---- Test 6: cylinder geometry ----

    def test_cylinder_geometry(self) -> None:
        """Non-box geometry: cylinder with T=400K/300K on end faces."""
        import gmsh

        from server.analysis_mesh import mesh_step_to_msh

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            step_path = work_dir / "cylinder.step"

            gmsh.initialize()
            try:
                gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 50, 5)
                gmsh.model.occ.synchronize()
                gmsh.write(str(step_path))
            finally:
                gmsh.finalize()

            # Gmsh OCC cylinder: Face1 = bottom disk, Face2 = lateral, Face3 = top disk
            mesh_info = mesh_step_to_msh(
                step_path=str(step_path),
                face_groups={
                    "bc_0_temperature": ["Face1"],
                    "bc_1_temperature": ["Face3"],
                },
                mesh_size=3.0,
                msh_version=2.2,
            )

            result = _run_elmer(
                work_dir,
                mesh_info,
                _thermal_material(),
                bcs=(
                    BoundaryCondition(
                        bc_type="temperature",
                        faces=("Face1",),
                        value={"temperature_k": 400},
                    ),
                    BoundaryCondition(
                        bc_type="temperature",
                        faces=("Face3",),
                        value={"temperature_k": 300},
                    ),
                ),
            )

        self.assertNotEqual(result.status, CheckStatus.FAIL)
        tf = _get_temp_field(result)
        self.assertAlmostEqual(tf.min_val, 300, delta=5)
        self.assertAlmostEqual(tf.max_val, 400, delta=5)
        self.assertAlmostEqual(tf.mean_val, 350, delta=10)

    # ---- Test 7: full tool pipeline with overtemperature check ----

    def test_overtemperature_check_with_real_solver(self) -> None:
        """Full analysis_thermal_check tool → real solver → safety evaluation."""
        import gmsh

        from server.analysis_mesh import mesh_step_to_msh
        from server.tools_analysis import analysis_thermal_check

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            step_path = work_dir / "box.step"

            gmsh.initialize()
            try:
                gmsh.model.occ.addBox(0, 0, 0, 10, 10, 10)
                gmsh.model.occ.synchronize()
                gmsh.write(str(step_path))
            finally:
                gmsh.finalize()

            # Mock only the FreeCAD export (no FreeCAD running), mesh + solve are real
            with patch.object(tools_mod, "cad_export_body") as mock_export:
                mock_export.return_value = {"ok": True, "path": str(step_path)}

                result = analysis_thermal_check(
                    body="TestBox",
                    material="copper",
                    boundary_conditions=[
                        {
                            "bc_type": "temperature",
                            "faces": ["Face1"],
                            "value": {"temperature_k": 400},
                        },
                        {
                            "bc_type": "temperature",
                            "faces": ["Face6"],
                            "value": {"temperature_k": 300},
                        },
                    ],
                    solver="elmer",
                    max_temperature_k=350,
                )

        self.assertTrue(result["ok"], f"Expected ok=True, got: {result}")
        self.assertEqual(result["solver_name"], "elmer")

        # Should have an overtemperature check that fails (400K > 350K limit)
        checks = result.get("checks", [])
        overtemp = [c for c in checks if c["name"] == "overtemperature"]
        self.assertEqual(len(overtemp), 1, f"Expected overtemperature check, got: {checks}")
        self.assertEqual(overtemp[0]["status"], "fail")
        self.assertLess(result["safety_factor"], 1.0)

    # ---- Test 8: mesh convergence ----

    def test_mesh_convergence(self) -> None:
        """Coarse vs fine mesh → both converge to same T_mean (linear problem)."""
        means: dict[str, float] = {}

        for label, ms in [("coarse", 5.0), ("fine", 1.5)]:
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = Path(tmpdir)
                _, mesh_info = _create_box_step(
                    work_dir,
                    mesh_size=ms,
                    face_groups={
                        "bc_0_temperature": ["Face1"],
                        "bc_1_temperature": ["Face6"],
                    },
                )

                result = _run_elmer(
                    work_dir,
                    mesh_info,
                    _thermal_material(),
                    bcs=(
                        BoundaryCondition(
                            bc_type="temperature",
                            faces=("Face1",),
                            value={"temperature_k": 400},
                        ),
                        BoundaryCondition(
                            bc_type="temperature",
                            faces=("Face6",),
                            value={"temperature_k": 300},
                        ),
                    ),
                )

            self.assertNotEqual(result.status, CheckStatus.FAIL)
            tf = _get_temp_field(result)
            self.assertAlmostEqual(tf.min_val, 300, delta=5)
            self.assertAlmostEqual(tf.max_val, 400, delta=5)
            means[label] = tf.mean_val

        # Both should give nearly identical T_mean (mesh-independent for linear problem)
        self.assertAlmostEqual(
            means["coarse"], means["fine"], delta=2,
        )


if __name__ == "__main__":
    unittest.main()
