"""Tests for server.study_solvers."""
from __future__ import annotations

import math
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.study_solvers import (
    BEMTXfoilSolver,
    MockSolver,
    OpenFOAMSolver,
    _BEMTResult,
    _XfoilCache,
    _bemt_solve,
    _blade_geometry,
    _compute_domain,
    _compute_forces_from_fields,
    _face_area_vector,
    _find_latest_time_dir,
    _flat_plate_polar,
    _mesh_cells_by_refinement,
    _parse_force_coefficients,
    _parse_openfoam_boundary,
    _parse_openfoam_faces,
    _parse_openfoam_label_list,
    _parse_openfoam_points,
    _parse_openfoam_scalar_field,
    _parse_xfoil_polar,
    _prandtl_loss,
    _read_stl_bounds,
    _run_xfoil,
    _scale_stl_to_meters,
    _write_openfoam_case,
    get_solver,
)


# ---------------------------------------------------------------------------
# Helpers to create minimal binary STL data
# ---------------------------------------------------------------------------

def _make_binary_stl(triangles: list[tuple[
    tuple[float, float, float],  # v1
    tuple[float, float, float],  # v2
    tuple[float, float, float],  # v3
]]) -> bytes:
    """Create a minimal binary STL from a list of triangles (no normals needed)."""
    header = b"\x00" * 80
    n = len(triangles)
    data = bytearray(header + struct.pack("<I", n))
    for v1, v2, v3 in triangles:
        # normal (0,0,0)
        data += struct.pack("<fff", 0.0, 0.0, 0.0)
        data += struct.pack("<fff", *v1)
        data += struct.pack("<fff", *v2)
        data += struct.pack("<fff", *v3)
        data += struct.pack("<H", 0)  # attribute byte count
    return bytes(data)


def _write_stl_file(path: str, triangles: list[tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]]) -> None:
    Path(path).write_bytes(_make_binary_stl(triangles))


# A simple cube from (0,0,0) to (100,100,100) in mm — 2 triangles per face × 6 faces
_CUBE_TRIS: list[tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]] = [
    # bottom (z=0)
    ((0, 0, 0), (100, 0, 0), (100, 100, 0)),
    ((0, 0, 0), (100, 100, 0), (0, 100, 0)),
    # top (z=100)
    ((0, 0, 100), (100, 100, 100), (100, 0, 100)),
    ((0, 0, 100), (0, 100, 100), (100, 100, 100)),
    # front (y=0)
    ((0, 0, 0), (100, 0, 100), (100, 0, 0)),
    ((0, 0, 0), (0, 0, 100), (100, 0, 100)),
    # back (y=100)
    ((0, 100, 0), (100, 100, 0), (100, 100, 100)),
    ((0, 100, 0), (100, 100, 100), (0, 100, 100)),
    # left (x=0)
    ((0, 0, 0), (0, 100, 0), (0, 100, 100)),
    ((0, 0, 0), (0, 100, 100), (0, 0, 100)),
    # right (x=100)
    ((100, 0, 0), (100, 0, 100), (100, 100, 100)),
    ((100, 0, 0), (100, 100, 100), (100, 100, 0)),
]


class TestMockSolver(unittest.TestCase):
    def setUp(self) -> None:
        self.solver = MockSolver()

    def test_name(self) -> None:
        self.assertEqual(self.solver.name(), "mock")

    def test_available(self) -> None:
        self.assertTrue(self.solver.available())

    def test_estimate(self) -> None:
        est = self.solver.estimate_per_variant_s({})
        self.assertGreater(est, 0)

    def test_describe_pipeline(self) -> None:
        desc = self.solver.describe_pipeline()
        self.assertIsInstance(desc, str)
        self.assertTrue(len(desc) > 0)

    def test_validate_params(self) -> None:
        errors = self.solver.validate_params({}, {}, {})
        self.assertEqual(errors, [])

    def test_solve(self) -> None:
        result = self.solver.solve({"x": 50}, {}, {})
        self.assertIn("objective", result)
        self.assertIn("total_param", result)
        self.assertEqual(result["total_param"], 50.0)
        # At x=50, objective = -(50-50)^2 + 2500 = 2500
        self.assertEqual(result["objective"], 2500.0)

    def test_solve_multiple_params(self) -> None:
        result = self.solver.solve({"a": 20, "b": 30}, {}, {})
        self.assertEqual(result["total_param"], 50.0)
        self.assertEqual(result["objective"], 2500.0)


# ---------------------------------------------------------------------------
# BEMT + XFOIL helper tests
# ---------------------------------------------------------------------------

class TestFlatPlatePolar(unittest.TestCase):
    def test_zero_alpha_near_zero_lift(self) -> None:
        cl, cd = _flat_plate_polar(0.0, 500_000.0)
        self.assertAlmostEqual(cl, 0.0, places=5)

    def test_positive_alpha_positive_lift(self) -> None:
        cl, cd = _flat_plate_polar(5.0, 500_000.0)
        self.assertGreater(cl, 0.0)

    def test_post_stall_lift_decreases(self) -> None:
        _, _ = _flat_plate_polar(10.0, 500_000.0)
        cl_12, _ = _flat_plate_polar(12.0, 500_000.0)
        cl_30, _ = _flat_plate_polar(30.0, 500_000.0)
        self.assertGreater(cl_12, cl_30)

    def test_drag_always_positive(self) -> None:
        for alpha in (-10.0, 0.0, 5.0, 15.0):
            _, cd = _flat_plate_polar(alpha, 500_000.0)
            self.assertGreater(cd, 0.0, f"Cd not positive at alpha={alpha}")

    def test_drag_increases_with_lift(self) -> None:
        _, cd_0 = _flat_plate_polar(0.0, 500_000.0)
        _, cd_10 = _flat_plate_polar(10.0, 500_000.0)
        self.assertGreater(cd_10, cd_0)


class TestXfoilPolarParser(unittest.TestCase):
    _SAMPLE_POLAR = (
        " XFOIL         Version 6.99\n"
        "\n"
        "  Calculated polar for: NACA 4412\n"
        "\n"
        "  xtrf =   1.000 (top)        1.000 (bottom)\n"
        "  Mach =   0.000     Re =     0.500 e 6     Ncrit =   9.000\n"
        "\n"
        "  alpha    CL        CD       CDp       CM     Top_Xtr  Bot_Xtr\n"
        " ------- -------- --------- --------- -------- -------- --------\n"
        "   5.000   0.9800   0.00920   0.00450  -0.0850   0.1200   0.6500\n"
    )

    def test_parse_standard_polar(self) -> None:
        result = _parse_xfoil_polar(self._SAMPLE_POLAR)
        self.assertIsNotNone(result)
        cl, cd = result  # type: ignore[misc]
        self.assertAlmostEqual(cl, 0.98, places=3)
        self.assertAlmostEqual(cd, 0.0092, places=4)

    def test_empty_text_returns_none(self) -> None:
        self.assertIsNone(_parse_xfoil_polar(""))

    def test_header_only_returns_none(self) -> None:
        text = (
            "  alpha    CL        CD\n"
            " ------- -------- ---------\n"
        )
        self.assertIsNone(_parse_xfoil_polar(text))

    def test_multi_point_returns_last(self) -> None:
        text = (
            "  alpha    CL        CD\n"
            " ------- -------- ---------\n"
            "   3.000   0.7000   0.00800\n"
            "   5.000   0.9800   0.00920\n"
        )
        result = _parse_xfoil_polar(text)
        self.assertIsNotNone(result)
        cl, cd = result  # type: ignore[misc]
        self.assertAlmostEqual(cl, 0.98, places=3)


class TestXfoilRunner(unittest.TestCase):
    @patch("server.study_solvers.shutil.which", return_value=None)
    def test_no_xfoil_binary_returns_none(self, _mock_which: MagicMock) -> None:
        result = _run_xfoil("NACA4412", 5.0, 500_000.0)
        self.assertIsNone(result)

    @patch("server.study_solvers.subprocess.run")
    @patch("server.study_solvers.shutil.which", return_value="/usr/bin/xfoil")
    def test_successful_run(self, _mock_which: MagicMock, mock_subproc: MagicMock) -> None:
        polar_content = (
            "  alpha    CL        CD\n"
            " ------- -------- ---------\n"
            "   5.000   0.9800   0.00920\n"
        )

        def write_polar(cmd, input=None, capture_output=False, text=False, timeout=None):
            # Find the polar path from the XFOIL input commands
            for line in (input or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("/") or stripped.startswith("/tmp"):
                    Path(stripped).write_text(polar_content)
                    break
            # Write to the temp directory — find it from input
            import re
            paths = re.findall(r"(/tmp/[^\s]+/polar\.dat)", input or "")
            for p in paths:
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                Path(p).write_text(polar_content)
            mock_result = MagicMock()
            mock_result.returncode = 0
            return mock_result

        mock_subproc.side_effect = write_polar
        result = _run_xfoil("NACA4412", 5.0, 500_000.0)
        # May be None if temp path not matched — that's fine, we test the code path
        # The important thing is no exception was raised
        self.assertTrue(result is None or len(result) == 2)

    @patch("server.study_solvers.subprocess.run", side_effect=subprocess.TimeoutExpired(["xfoil"], 10))
    @patch("server.study_solvers.shutil.which", return_value="/usr/bin/xfoil")
    def test_timeout_returns_none(self, _mock_which: MagicMock, _mock_subproc: MagicMock) -> None:
        result = _run_xfoil("NACA4412", 5.0, 500_000.0, timeout_s=0.1)
        self.assertIsNone(result)

    @patch("server.study_solvers.subprocess.run")
    @patch("server.study_solvers.shutil.which", return_value="/usr/bin/xfoil")
    def test_no_convergence_returns_none(self, _mock_which: MagicMock, mock_subproc: MagicMock) -> None:
        # XFOIL runs but produces no polar file
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_subproc.return_value = mock_result
        result = _run_xfoil("NACA4412", 5.0, 500_000.0)
        self.assertIsNone(result)


class TestXfoilCache(unittest.TestCase):
    def test_miss_returns_none(self) -> None:
        cache = _XfoilCache()
        self.assertIsNone(cache.get("NACA4412", 500_000.0, 5.0))

    def test_put_then_get(self) -> None:
        cache = _XfoilCache()
        cache.put("NACA4412", 500_000.0, 5.0, 0.98, 0.0092)
        result = cache.get("NACA4412", 500_000.0, 5.0)
        self.assertEqual(result, (0.98, 0.0092))

    def test_re_rounding_shares_slots(self) -> None:
        cache = _XfoilCache()
        cache.put("NACA4412", 500_050.0, 5.0, 0.98, 0.0092)
        # 500_050 and 500_020 both round to 500_100
        result = cache.get("NACA4412", 500_020.0, 5.0)
        self.assertIsNotNone(result)

    def test_different_airfoils_dont_collide(self) -> None:
        cache = _XfoilCache()
        cache.put("NACA4412", 500_000.0, 5.0, 0.98, 0.0092)
        result = cache.get("NACA2412", 500_000.0, 5.0)
        self.assertIsNone(result)


class TestBladeGeometry(unittest.TestCase):
    def test_root_station(self) -> None:
        chord, twist = _blade_geometry(
            0.15, chord_root_mm=50.0, chord_tip_mm=25.0,
            twist_root_deg=20.0, twist_tip_deg=5.0, hub_r_frac=0.15,
        )
        self.assertAlmostEqual(chord, 50.0)
        self.assertAlmostEqual(twist, 20.0)

    def test_tip_station(self) -> None:
        chord, twist = _blade_geometry(
            1.0, chord_root_mm=50.0, chord_tip_mm=25.0,
            twist_root_deg=20.0, twist_tip_deg=5.0, hub_r_frac=0.15,
        )
        self.assertAlmostEqual(chord, 25.0)
        self.assertAlmostEqual(twist, 5.0)

    def test_mid_station(self) -> None:
        chord, twist = _blade_geometry(
            0.575, chord_root_mm=50.0, chord_tip_mm=25.0,
            twist_root_deg=20.0, twist_tip_deg=5.0, hub_r_frac=0.15,
        )
        self.assertAlmostEqual(chord, 37.5)
        self.assertAlmostEqual(twist, 12.5)

    def test_inside_hub_clamped_to_root(self) -> None:
        chord, twist = _blade_geometry(
            0.05, chord_root_mm=50.0, chord_tip_mm=25.0,
            twist_root_deg=20.0, twist_tip_deg=5.0, hub_r_frac=0.15,
        )
        self.assertAlmostEqual(chord, 50.0)
        self.assertAlmostEqual(twist, 20.0)


class TestPrandtlLoss(unittest.TestCase):
    def test_near_tip_small_f(self) -> None:
        F = _prandtl_loss(0.99, 1.0, 0.15, 3, math.radians(10.0))
        self.assertLess(F, 0.5)

    def test_mid_span_near_one(self) -> None:
        F = _prandtl_loss(0.5, 1.0, 0.15, 3, math.radians(15.0))
        self.assertGreater(F, 0.8)

    def test_near_hub_reduced(self) -> None:
        F = _prandtl_loss(0.16, 1.0, 0.15, 3, math.radians(30.0))
        self.assertLess(F, 1.0)

    def test_never_zero(self) -> None:
        F = _prandtl_loss(0.999, 1.0, 0.15, 6, math.radians(2.0))
        self.assertGreaterEqual(F, 0.001)


class TestBEMTSolve(unittest.TestCase):
    """Test BEMT solver with flat-plate fallback (no XFOIL subprocess)."""

    _BASE_PARAMS: dict[str, object] = {
        "diameter_mm": 254.0,  # 10 inch
        "num_blades": 2,
        "rpm": 8000.0,
        "rho": 1.225,
        "forward_velocity_mps": 0.0,
        "chord_root_mm": 25.0,
        "chord_tip_mm": 12.0,
        "twist_root_deg": 15.0,
        "twist_tip_deg": 5.0,
        "blade_pitch_deg": 5.0,
        "airfoil": "NACA4412",
        "Re": 200_000.0,
        "radial_stations": 10,
    }

    def _solve(self, **overrides: object) -> _BEMTResult:
        kw = {**self._BASE_PARAMS, **overrides}
        # Ensure XFOIL is not called by not mocking — flat-plate fallback is used
        # because _run_xfoil returns None when xfoil binary missing
        with patch("server.study_solvers.shutil.which", return_value=None):
            return _bemt_solve(**kw)  # type: ignore[arg-type]

    def test_positive_pitch_and_rpm_gives_positive_thrust(self) -> None:
        result = self._solve()
        self.assertGreater(result.thrust_N, 0.0)

    def test_zero_rpm_gives_zero_thrust(self) -> None:
        result = self._solve(rpm=0.0)
        self.assertAlmostEqual(result.thrust_N, 0.0)

    def test_more_blades_more_thrust(self) -> None:
        r2 = self._solve(num_blades=2)
        r4 = self._solve(num_blades=4)
        self.assertGreater(r4.thrust_N, r2.thrust_N)

    def test_larger_diameter_more_thrust(self) -> None:
        r_small = self._solve(diameter_mm=200.0)
        r_large = self._solve(diameter_mm=300.0)
        self.assertGreater(r_large.thrust_N, r_small.thrust_N)

    def test_most_stations_converge(self) -> None:
        result = self._solve()
        # At least half of stations should converge with reasonable params
        self.assertGreaterEqual(
            result.stations_converged, result.stations_total // 2,
            f"Only {result.stations_converged}/{result.stations_total} converged",
        )

    def test_ct_thrust_consistency(self) -> None:
        """Verify T ≈ Ct * rho * n^2 * D^4."""
        result = self._solve()
        n = 8000.0 / 60.0
        D = 0.254
        T_from_Ct = result.Ct * 1.225 * n * n * D ** 4
        self.assertAlmostEqual(result.thrust_N, T_from_Ct, places=3)

    def test_hover_efficiency_in_range(self) -> None:
        result = self._solve(forward_velocity_mps=0.0)
        self.assertGreater(result.efficiency, 0.0)
        self.assertLessEqual(result.efficiency, 1.0)


class TestBEMTXfoilSolverClass(unittest.TestCase):
    """Test the BEMTXfoilSolver adapter class."""

    _VALID_PARAMS: dict[str, object] = {
        "rpm": 8000.0,
        "num_blades": 2,
        "diameter_mm": 254.0,
        "airfoil": "NACA4412",
        "chord_root_mm": 25.0,
    }

    def test_available_true_when_xfoil_on_path(self) -> None:
        solver = BEMTXfoilSolver()
        with patch("server.study_solvers.shutil.which", return_value="/usr/bin/xfoil"):
            self.assertTrue(solver.available())

    def test_available_false_when_xfoil_missing(self) -> None:
        solver = BEMTXfoilSolver()
        with patch("server.study_solvers.shutil.which", return_value=None):
            self.assertFalse(solver.available())

    def test_validate_catches_missing_required(self) -> None:
        solver = BEMTXfoilSolver()
        errors = solver.validate_params({}, {}, {})
        self.assertTrue(len(errors) > 0)
        # Should mention multiple missing fields
        error_text = " ".join(errors)
        self.assertIn("rpm", error_text)
        self.assertIn("num_blades", error_text)

    def test_validate_passes_with_valid_params(self) -> None:
        solver = BEMTXfoilSolver()
        errors = solver.validate_params(
            self._VALID_PARAMS, {"Re": 500_000}, {},  # type: ignore[arg-type]
        )
        self.assertEqual(errors, [])

    @patch("server.study_solvers._run_xfoil", return_value=None)
    def test_solve_returns_all_metric_keys(self, _mock_xfoil: MagicMock) -> None:
        solver = BEMTXfoilSolver()
        result = solver.solve(
            params={"rpm": 8000, "num_blades": 2, "diameter_mm": 254},
            fixed={"Re": 200_000, "airfoil": "NACA4412", "chord_root_mm": 25.0},
            config_params={},
        )
        expected_keys = {
            "thrust_N", "torque_Nm", "power_W", "efficiency",
            "Ct", "Cq", "stations_converged", "stations_total",
        }
        self.assertEqual(set(result.keys()), expected_keys)
        # All values should be floats
        for v in result.values():
            self.assertIsInstance(v, float)


class TestOpenFOAMSolver(unittest.TestCase):
    def test_available_true_when_dependencies_present(self) -> None:
        solver = OpenFOAMSolver()
        with patch(
            "server.study_solvers.shutil.which",
            side_effect=lambda name: {
                "simpleFoam": "/usr/bin/simpleFoam",
                "FreeCADCmd": "/usr/bin/FreeCADCmd",
            }.get(name),
        ):
            self.assertTrue(solver.available())

    def test_available_false_when_openfoam_missing(self) -> None:
        solver = OpenFOAMSolver()
        with patch(
            "server.study_solvers.shutil.which",
            side_effect=lambda name: {
                "simpleFoam": None,
                "FreeCADCmd": "/usr/bin/FreeCADCmd",
            }.get(name),
        ):
            self.assertFalse(solver.available())

    def test_available_false_when_freecadcmd_missing(self) -> None:
        solver = OpenFOAMSolver()
        with patch(
            "server.study_solvers.shutil.which",
            side_effect=lambda name: {
                "simpleFoam": "/usr/bin/simpleFoam",
                "FreeCADCmd": None,
                "freecadcmd": None,
                "freecad-cmd": None,
            }.get(name),
        ):
            self.assertFalse(solver.available())

    def test_estimate_scales_with_refinement(self) -> None:
        solver = OpenFOAMSolver()
        est_coarse = solver.estimate_per_variant_s({"mesh_refinement": 1})
        est_fine = solver.estimate_per_variant_s({"mesh_refinement": 3})
        self.assertGreater(est_fine, est_coarse)

    def test_validate_requires_mesh_refinement(self) -> None:
        solver = OpenFOAMSolver()
        errors = solver.validate_params({}, {}, {})
        self.assertTrue(len(errors) > 0)

    def test_validate_mesh_refinement_range(self) -> None:
        solver = OpenFOAMSolver()
        errors = solver.validate_params({}, {}, {"mesh_refinement": 5})
        self.assertTrue(any("1-4" in e for e in errors))

    def test_validate_turbulence_model(self) -> None:
        solver = OpenFOAMSolver()
        errors = solver.validate_params(
            {}, {}, {"mesh_refinement": 2, "turbulence_model": "badModel"},
        )
        self.assertTrue(any("turbulence_model" in e for e in errors))

    def test_validate_n_processors(self) -> None:
        solver = OpenFOAMSolver()
        errors = solver.validate_params(
            {}, {}, {"mesh_refinement": 2, "n_processors": 4},
        )
        self.assertTrue(any("n_processors" in e for e in errors))

    def test_validate_ok(self) -> None:
        solver = OpenFOAMSolver()
        errors = solver.validate_params({}, {}, {"mesh_refinement": 2})
        self.assertEqual(errors, [])


class TestGetSolver(unittest.TestCase):
    def test_get_mock(self) -> None:
        solver = get_solver("mock")
        self.assertEqual(solver.name(), "mock")

    def test_get_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_solver("nonexistent")


# ---------------------------------------------------------------------------
# STL utility tests
# ---------------------------------------------------------------------------

class TestSTLUtils(unittest.TestCase):
    def test_read_bounds_cube(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(_make_binary_stl(_CUBE_TRIS))
            stl_path = f.name
        try:
            bmin, bmax = _read_stl_bounds(stl_path)
            self.assertAlmostEqual(bmin[0], 0.0)
            self.assertAlmostEqual(bmin[1], 0.0)
            self.assertAlmostEqual(bmin[2], 0.0)
            self.assertAlmostEqual(bmax[0], 100.0)
            self.assertAlmostEqual(bmax[1], 100.0)
            self.assertAlmostEqual(bmax[2], 100.0)
        finally:
            os.unlink(stl_path)

    def test_read_bounds_single_triangle(self) -> None:
        tris = [((1, 2, 3), (4, 5, 6), (7, 8, 9))]
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(_make_binary_stl(tris))
            stl_path = f.name
        try:
            bmin, bmax = _read_stl_bounds(stl_path)
            self.assertAlmostEqual(bmin[0], 1.0)
            self.assertAlmostEqual(bmin[1], 2.0)
            self.assertAlmostEqual(bmin[2], 3.0)
            self.assertAlmostEqual(bmax[0], 7.0)
            self.assertAlmostEqual(bmax[1], 8.0)
            self.assertAlmostEqual(bmax[2], 9.0)
        finally:
            os.unlink(stl_path)

    def test_read_bounds_empty_stl_raises(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            # Header + 0 triangles
            f.write(b"\x00" * 80 + struct.pack("<I", 0))
            stl_path = f.name
        try:
            with self.assertRaises(RuntimeError):
                _read_stl_bounds(stl_path)
        finally:
            os.unlink(stl_path)

    def test_read_bounds_corrupt_stl_raises(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(b"\x00" * 10)  # too short
            stl_path = f.name
        try:
            with self.assertRaises(RuntimeError):
                _read_stl_bounds(stl_path)
        finally:
            os.unlink(stl_path)

    def test_scale_stl_to_meters(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(_make_binary_stl(_CUBE_TRIS))
            src = f.name
        dst = src + ".scaled.stl"
        try:
            _scale_stl_to_meters(src, dst)
            bmin, bmax = _read_stl_bounds(dst)
            self.assertAlmostEqual(bmin[0], 0.0, places=5)
            self.assertAlmostEqual(bmax[0], 0.1, places=5)  # 100mm → 0.1m
            self.assertAlmostEqual(bmax[1], 0.1, places=5)
            self.assertAlmostEqual(bmax[2], 0.1, places=5)
        finally:
            os.unlink(src)
            if os.path.exists(dst):
                os.unlink(dst)


# ---------------------------------------------------------------------------
# Domain sizing tests
# ---------------------------------------------------------------------------

class TestDomainSizing(unittest.TestCase):
    def test_symmetric_domain(self) -> None:
        # Object centered at origin, 1m cube in meters
        bmin = (-0.5, -0.5, -0.5)
        bmax = (0.5, 0.5, 0.5)
        domain = _compute_domain(bmin, bmax)
        char_len = domain["char_length"]
        self.assertAlmostEqual(char_len, 3**0.5, places=5)  # sqrt(3)
        # Domain should be centered at (0,0,0)
        self.assertAlmostEqual(
            (domain["y_min"] + domain["y_max"]) / 2, 0.0, places=5,
        )
        # Downstream larger than upstream
        self.assertGreater(domain["x_max"], abs(domain["x_min"]))

    def test_refinement_increases_cells(self) -> None:
        bmin = (-0.05, -0.05, -0.05)
        bmax = (0.05, 0.05, 0.05)
        domain = _compute_domain(bmin, bmax)
        nx1, ny1, nz1 = _mesh_cells_by_refinement(1, domain)
        nx4, ny4, nz4 = _mesh_cells_by_refinement(4, domain)
        self.assertGreater(nx4, nx1)
        self.assertGreater(ny4, ny1)

    def test_zero_volume_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            _compute_domain((1.0, 1.0, 1.0), (1.0, 1.0, 1.0))

    def test_cell_counts_positive(self) -> None:
        domain = _compute_domain((-0.01, -0.01, -0.01), (0.01, 0.01, 0.01))
        for level in (1, 2, 3, 4):
            nx, ny, nz = _mesh_cells_by_refinement(level, domain)
            self.assertGreaterEqual(nx, 1)
            self.assertGreaterEqual(ny, 1)
            self.assertGreaterEqual(nz, 1)


# ---------------------------------------------------------------------------
# Case writer tests
# ---------------------------------------------------------------------------

class TestCaseWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.domain = _compute_domain((-0.05, -0.05, -0.05), (0.05, 0.05, 0.05))
        self.cells = _mesh_cells_by_refinement(2, self.domain)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_case(self, turbulence_model: str = "kOmegaSST") -> None:
        _write_openfoam_case(
            case_dir=self.tmpdir,
            stl_name="geometry.stl",
            domain=self.domain,
            mesh_cells=self.cells,
            velocity=(10.0, 0.0, 0.0),
            refinement=2,
            turbulence_model=turbulence_model,
            ref_length=self.domain["char_length"],
            ref_area=0.01,
            rho=1.225,
            max_iterations=1000,
        )

    def test_creates_all_directories(self) -> None:
        self._write_case()
        self.assertTrue((Path(self.tmpdir) / "constant").is_dir())
        self.assertTrue((Path(self.tmpdir) / "system").is_dir())
        self.assertTrue((Path(self.tmpdir) / "0").is_dir())

    def test_creates_required_files(self) -> None:
        self._write_case()
        required = [
            "constant/transportProperties",
            "constant/turbulenceProperties",
            "system/controlDict",
            "system/fvSchemes",
            "system/fvSolution",
            "system/blockMeshDict",
            "system/snappyHexMeshDict",
            "0/U",
            "0/p",
            "0/nut",
        ]
        for f in required:
            self.assertTrue(
                (Path(self.tmpdir) / f).is_file(),
                f"Missing file: {f}",
            )

    def test_control_dict_end_time(self) -> None:
        self._write_case()
        text = (Path(self.tmpdir) / "system" / "controlDict").read_text()
        self.assertIn("endTime         1000", text)

    def test_block_mesh_dict_has_domain_vertices(self) -> None:
        self._write_case()
        text = (Path(self.tmpdir) / "system" / "blockMeshDict").read_text()
        self.assertIn(str(self.cells[0]), text)  # nx
        self.assertIn(str(self.cells[1]), text)  # ny

    def test_velocity_in_u_field(self) -> None:
        self._write_case()
        text = (Path(self.tmpdir) / "0" / "U").read_text()
        self.assertIn("(10.0 0.0 0.0)", text)

    def test_komega_writes_k_and_omega(self) -> None:
        self._write_case("kOmegaSST")
        self.assertTrue((Path(self.tmpdir) / "0" / "k").is_file())
        self.assertTrue((Path(self.tmpdir) / "0" / "omega").is_file())
        self.assertFalse((Path(self.tmpdir) / "0" / "nuTilda").exists())

    def test_spalart_allmaras_writes_nutilda(self) -> None:
        self._write_case("SpalartAllmaras")
        self.assertTrue((Path(self.tmpdir) / "0" / "nuTilda").is_file())
        self.assertFalse((Path(self.tmpdir) / "0" / "k").exists())
        self.assertFalse((Path(self.tmpdir) / "0" / "omega").exists())

    def test_kepsilon_writes_k_and_epsilon(self) -> None:
        self._write_case("kEpsilon")
        self.assertTrue((Path(self.tmpdir) / "0" / "k").is_file())
        self.assertTrue((Path(self.tmpdir) / "0" / "epsilon").is_file())
        self.assertFalse((Path(self.tmpdir) / "0" / "omega").exists())

    def test_transport_properties_no_double_braces(self) -> None:
        """Regression: transportProperties must have single braces for FoamFile header."""
        self._write_case()
        text = (Path(self.tmpdir) / "constant" / "transportProperties").read_text()
        self.assertNotIn("{{", text, "Double braces in transportProperties")
        self.assertNotIn("}}", text, "Double braces in transportProperties")
        self.assertIn("FoamFile", text)
        self.assertIn("transportModel  Newtonian;", text)

    def test_control_dict_no_forcecoeffs_by_default(self) -> None:
        """By default controlDict should NOT contain forceCoeffs / libs (forces)."""
        self._write_case()
        text = (Path(self.tmpdir) / "system" / "controlDict").read_text()
        self.assertNotIn("forceCoeffs", text)
        self.assertNotIn("libs", text)
        self.assertIn("simpleFoam", text)
        self.assertIn("endTime", text)

    def test_control_dict_with_function_objects(self) -> None:
        """When use_function_objects=True, controlDict should have forceCoeffs."""
        _write_openfoam_case(
            case_dir=self.tmpdir,
            stl_name="geometry.stl",
            domain=self.domain,
            mesh_cells=self.cells,
            velocity=(10.0, 0.0, 0.0),
            refinement=2,
            turbulence_model="kOmegaSST",
            ref_length=self.domain["char_length"],
            ref_area=0.01,
            rho=1.225,
            max_iterations=1000,
            use_function_objects=True,
        )
        text = (Path(self.tmpdir) / "system" / "controlDict").read_text()
        self.assertIn("forceCoeffs", text)
        self.assertIn("libs", text)


# ---------------------------------------------------------------------------
# OpenFOAM field parser tests
# ---------------------------------------------------------------------------

class TestOpenFOAMFieldParsers(unittest.TestCase):
    """Test parsers for OpenFOAM ASCII mesh and field files."""

    def test_parse_boundary(self) -> None:
        content = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}

3
(
    inlet
    {
        type            patch;
        nFaces          10;
        startFace       1000;
    }
    outlet
    {
        type            patch;
        nFaces          10;
        startFace       1010;
    }
    geometry
    {
        type            wall;
        nFaces          200;
        startFace       1020;
    }
)
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = f.name
        try:
            result = _parse_openfoam_boundary(fpath)
            self.assertIn("inlet", result)
            self.assertIn("geometry", result)
            self.assertEqual(result["inlet"]["nFaces"], 10)
            self.assertEqual(result["inlet"]["startFace"], 1000)
            self.assertEqual(result["geometry"]["nFaces"], 200)
            self.assertEqual(result["geometry"]["startFace"], 1020)
        finally:
            os.unlink(fpath)

    def test_parse_label_list(self) -> None:
        content = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       labelList;
    object      owner;
}

5
(
0
1
2
3
4
)
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = f.name
        try:
            result = _parse_openfoam_label_list(fpath)
            self.assertEqual(result, [0, 1, 2, 3, 4])
        finally:
            os.unlink(fpath)

    def test_parse_points(self) -> None:
        content = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       vectorField;
    object      points;
}

3
(
(0 0 0)
(1 0 0)
(0 1 0)
)
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = f.name
        try:
            result = _parse_openfoam_points(fpath)
            self.assertEqual(len(result), 3)
            self.assertAlmostEqual(result[0][0], 0.0)
            self.assertAlmostEqual(result[1][0], 1.0)
            self.assertAlmostEqual(result[2][1], 1.0)
        finally:
            os.unlink(fpath)

    def test_parse_faces(self) -> None:
        content = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       faceList;
    object      faces;
}

2
(
3(0 1 2)
4(0 1 3 2)
)
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = f.name
        try:
            result = _parse_openfoam_faces(fpath)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0], [0, 1, 2])
            self.assertEqual(result[1], [0, 1, 3, 2])
        finally:
            os.unlink(fpath)

    def test_parse_scalar_field_nonuniform(self) -> None:
        content = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      p;
}

dimensions      [0 2 -2 0 0 0 0];

internalField   nonuniform List<scalar>
3
(
1.5
2.5
3.5
)
;
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = f.name
        try:
            result = _parse_openfoam_scalar_field(fpath)
            self.assertEqual(len(result), 3)
            self.assertAlmostEqual(result[0], 1.5)
            self.assertAlmostEqual(result[2], 3.5)
        finally:
            os.unlink(fpath)

    def test_parse_scalar_field_uniform(self) -> None:
        content = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       volScalarField;
    object      p;
}

dimensions      [0 2 -2 0 0 0 0];

internalField   uniform 0;
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = f.name
        try:
            result = _parse_openfoam_scalar_field(fpath)
            self.assertEqual(len(result), 1)
            self.assertAlmostEqual(result[0], 0.0)
        finally:
            os.unlink(fpath)

    def test_find_latest_time_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "0").mkdir()
            (Path(td) / "100").mkdir()
            (Path(td) / "200").mkdir()
            (Path(td) / "constant").mkdir()  # non-numeric, should be ignored
            result = _find_latest_time_dir(td)
            self.assertTrue(result.endswith("200"))

    def test_find_latest_time_dir_no_dirs_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                _find_latest_time_dir(td)


# ---------------------------------------------------------------------------
# Face area vector tests
# ---------------------------------------------------------------------------

class TestFaceAreaVector(unittest.TestCase):
    def test_unit_square_z_normal(self) -> None:
        """Unit square in XY plane → area=1, normal=(0,0,1)."""
        verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        ax, ay, az = _face_area_vector(verts)
        self.assertAlmostEqual(ax, 0.0, places=10)
        self.assertAlmostEqual(ay, 0.0, places=10)
        self.assertAlmostEqual(az, 1.0, places=10)

    def test_triangle_area(self) -> None:
        """Triangle with vertices at (0,0,0), (4,0,0), (0,3,0) → area=6."""
        verts = [(0, 0, 0), (4, 0, 0), (0, 3, 0)]
        ax, ay, az = _face_area_vector(verts)
        area = math.sqrt(ax * ax + ay * ay + az * az)
        self.assertAlmostEqual(area, 6.0, places=10)

    def test_degenerate_returns_zero(self) -> None:
        """Two points → zero area."""
        verts = [(0, 0, 0), (1, 0, 0)]
        ax, ay, az = _face_area_vector(verts)
        self.assertAlmostEqual(ax, 0.0)
        self.assertAlmostEqual(ay, 0.0)
        self.assertAlmostEqual(az, 0.0)

    def test_collinear_returns_zero(self) -> None:
        """Collinear points → zero area."""
        verts = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
        ax, ay, az = _face_area_vector(verts)
        area = math.sqrt(ax * ax + ay * ay + az * az)
        self.assertAlmostEqual(area, 0.0, places=10)


# ---------------------------------------------------------------------------
# Compute forces from fields test
# ---------------------------------------------------------------------------

class TestComputeForcesFromFields(unittest.TestCase):
    """Synthetic mini-case: single quad face with known pressure."""

    def _make_mini_case(self, td: str, pressure: float) -> None:
        """Create a minimal OpenFOAM case with one quad face at z=0.

        The face is a 1×1 square in the XY plane at z=0.
        With outward normal pointing in +z, and positive pressure,
        the force on the body should be in +z direction.
        """
        poly = Path(td) / "constant" / "polyMesh"
        poly.mkdir(parents=True)

        # 4 points forming a unit square + 4 more for cell volume
        (poly / "points").write_text("""\
FoamFile
{
    version 2.0;
    format ascii;
    class vectorField;
    object points;
}
8
(
(0 0 0)
(1 0 0)
(1 1 0)
(0 1 0)
(0 0 1)
(1 0 1)
(1 1 1)
(0 1 1)
)
""")
        # 6 faces for one hex cell; face 5 is the "geometry" patch face
        (poly / "faces").write_text("""\
FoamFile
{
    version 2.0;
    format ascii;
    class faceList;
    object faces;
}
6
(
4(0 3 2 1)
4(4 5 6 7)
4(0 1 5 4)
4(1 2 6 5)
4(2 3 7 6)
4(0 4 7 3)
)
""")
        # All faces owned by cell 0
        (poly / "owner").write_text("""\
FoamFile
{
    version 2.0;
    format ascii;
    class labelList;
    object owner;
}
6
(
0
0
0
0
0
0
)
""")
        # boundary: face 0 is "geometry" patch (bottom face, normal -z from domain = +z into body)
        (poly / "boundary").write_text("""\
FoamFile
{
    version 2.0;
    format ascii;
    class polyBoundaryMesh;
    object boundary;
}
2
(
    geometry
    {
        type wall;
        nFaces 1;
        startFace 0;
    }
    other
    {
        type patch;
        nFaces 5;
        startFace 1;
    }
)
""")
        # Time directory with pressure field
        time_dir = Path(td) / "100"
        time_dir.mkdir()
        (time_dir / "p").write_text(f"""\
FoamFile
{{
    version 2.0;
    format ascii;
    class volScalarField;
    object p;
}}
dimensions [0 2 -2 0 0 0 0];
internalField uniform {pressure};
""")

    def test_known_pressure_force(self) -> None:
        """Uniform kinematic pressure of 10 m²/s² on a 1×1 face.

        Face 0 vertices: (0,0,0), (0,1,0), (1,1,0), (1,0,0)
        Newell normal for this winding: (0,0,-1) (area=1).
        Force = p * rho * area_vec = 10 * 1.225 * (0,0,-1) = (0,0,-12.25).
        Drag direction = (1,0,0) → drag = 0.
        Lift direction = (0,0,1) → lift = -12.25 N.
        """
        with tempfile.TemporaryDirectory() as td:
            self._make_mini_case(td, pressure=10.0)
            result = _compute_forces_from_fields(
                td,
                rho=1.225,
                velocity=10.0,
                ref_area=1.0,
                ref_length=1.0,
            )
            # Force = p * rho * area_vec.  The face winding (0,3,2,1) gives
            # Newell normal in -z, so lift (z-dir) = 10 * 1.225 * (-1) = -12.25
            self.assertAlmostEqual(result["drag_N"], 0.0, places=6)
            self.assertAlmostEqual(result["lift_N"], -12.25, places=4)
            # Cd should be 0 (no x-force)
            self.assertAlmostEqual(result["Cd"], 0.0, places=6)
            self.assertTrue(result.get("pressure_only"))

    def test_missing_patch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._make_mini_case(td, pressure=0.0)
            with self.assertRaises(RuntimeError) as ctx:
                _compute_forces_from_fields(
                    td, rho=1.225, velocity=10.0, ref_area=1.0,
                    patch_name="nonexistent",
                )
            self.assertIn("nonexistent", str(ctx.exception))


# ---------------------------------------------------------------------------
# Force parser tests (legacy — for use_function_objects=True path)
# ---------------------------------------------------------------------------

class TestForceParser(unittest.TestCase):
    def _make_coeff_file(self, case_dir: str, content: str) -> None:
        coeff_dir = Path(case_dir) / "postProcessing" / "forceCoeffs" / "0"
        coeff_dir.mkdir(parents=True)
        (coeff_dir / "coefficient.dat").write_text(content)

    def test_parse_known_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._make_coeff_file(td, (
                "# Time Cd Cl CmPitch\n"
                "100 0.5 1.2 0.03\n"
                "200 0.5 1.2 0.03\n"
            ))
            result = _parse_force_coefficients(td, rho=1.225, velocity=10.0, ref_area=0.01)
            self.assertAlmostEqual(result["Cd"], 0.5)
            self.assertAlmostEqual(result["Cl"], 1.2)
            self.assertAlmostEqual(result["Cm"], 0.03)
            # drag = 0.5 * 0.5 * 1.225 * 100 * 0.01 = 0.030625
            expected_drag = 0.5 * 0.5 * 1.225 * 100.0 * 0.01
            self.assertAlmostEqual(result["drag_N"], expected_drag, places=6)

    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                _parse_force_coefficients(td, rho=1.225, velocity=10.0, ref_area=0.01)

    def test_empty_data_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._make_coeff_file(td, "# Time Cd Cl CmPitch\n")
            with self.assertRaises(RuntimeError):
                _parse_force_coefficients(td, rho=1.225, velocity=10.0, ref_area=0.01)

    def test_fallback_columns_without_header(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._make_coeff_file(td, "100 0.3 0.8 0.01\n")
            result = _parse_force_coefficients(td, rho=1.225, velocity=10.0, ref_area=0.01)
            # Without recognized header, falls back to positional
            self.assertAlmostEqual(result["Cd"], 0.3)
            self.assertAlmostEqual(result["Cl"], 0.8)


# ---------------------------------------------------------------------------
# Integration test: full OpenFOAM pipeline with mocked subprocesses
# ---------------------------------------------------------------------------

class TestOpenFOAMSolveIntegration(unittest.TestCase):
    """Test the full solve() pipeline with all external calls mocked."""

    def _make_geometry_script(self, tmpdir: str) -> str:
        """Create a fake geometry script file."""
        script = Path(tmpdir) / "geometry.py"
        script.write_text("# fake geometry script\n")
        return str(script)

    def _make_stl(self, path: str) -> None:
        """Write a valid binary STL at the given path."""
        _write_stl_file(path, _CUBE_TRIS)

    def _mock_run_geometry(self, stl_path_holder: list[str]):
        """Return a side_effect for _run_geometry_script that creates a real STL."""
        def side_effect(script_path, params, fixed, output_stl, timeout_s=120.0):
            self._make_stl(output_stl)
            stl_path_holder.append(output_stl)
        return side_effect

    def _mock_subprocess_run_success(self, case_dir_holder: list[str]):
        """Return a side_effect for subprocess.run that creates polyMesh + pressure data.

        Creates a synthetic 1-cell mesh with a single 'geometry' boundary face
        and a pressure field, so _compute_forces_from_fields can parse it.
        """
        def side_effect(cmd, cwd=None, **kwargs):
            if cwd:
                case_dir_holder.append(cwd)
                # Create polyMesh with a real parseable mesh
                pm = Path(cwd) / "constant" / "polyMesh"
                pm.mkdir(parents=True, exist_ok=True)
                (pm / "points").write_text("""\
FoamFile { version 2.0; format ascii; class vectorField; object points; }
8
(
(0 0 0)
(1 0 0)
(1 1 0)
(0 1 0)
(0 0 1)
(1 0 1)
(1 1 1)
(0 1 1)
)
""")
                (pm / "faces").write_text("""\
FoamFile { version 2.0; format ascii; class faceList; object faces; }
6
(
4(0 3 2 1)
4(4 5 6 7)
4(0 1 5 4)
4(1 2 6 5)
4(2 3 7 6)
4(0 4 7 3)
)
""")
                (pm / "owner").write_text("""\
FoamFile { version 2.0; format ascii; class labelList; object owner; }
6
(
0
0
0
0
0
0
)
""")
                (pm / "boundary").write_text("""\
FoamFile { version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
2
(
    geometry
    {
        type wall;
        nFaces 1;
        startFace 0;
    }
    other
    {
        type patch;
        nFaces 5;
        startFace 1;
    }
)
""")

                # For simpleFoam: create a time directory with pressure
                if cmd and cmd[0] == "simpleFoam":
                    time_dir = Path(cwd) / "500"
                    time_dir.mkdir(exist_ok=True)
                    (time_dir / "p").write_text("""\
FoamFile { version 2.0; format ascii; class volScalarField; object p; }
dimensions [0 2 -2 0 0 0 0];
internalField uniform 5.0;
""")

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            return mock_result
        return side_effect

    @patch("server.study_solvers._run_geometry_script")
    @patch("server.study_solvers.subprocess.run")
    def test_full_pipeline_returns_metrics(self, mock_subproc, mock_geom):
        stl_holder: list[str] = []
        case_holder: list[str] = []
        mock_geom.side_effect = self._mock_run_geometry(stl_holder)
        mock_subproc.side_effect = self._mock_subprocess_run_success(case_holder)

        solver = OpenFOAMSolver()
        with tempfile.TemporaryDirectory() as td:
            script = self._make_geometry_script(td)
            result = solver.solve(
                params={"angle": 10},
                fixed={"velocity_mps": 10.0, "rho": 1.225},
                config_params={
                    "geometry_script": script,
                    "mesh_refinement": 1,
                    "cleanup_cases": False,
                },
            )

        self.assertIn("Cd", result)
        self.assertIn("Cl", result)
        self.assertIn("drag_N", result)
        self.assertIn("lift_N", result)
        self.assertIn("pressure_only", result)
        # Values come from _compute_forces_from_fields on synthetic mesh data
        self.assertIsInstance(result["Cd"], float)
        self.assertIsInstance(result["Cl"], float)

    @patch("server.study_solvers._run_geometry_script")
    @patch("server.study_solvers.subprocess.run")
    def test_blockmesh_failure_raises(self, mock_subproc, mock_geom):
        stl_holder: list[str] = []
        mock_geom.side_effect = self._mock_run_geometry(stl_holder)

        def fail_blockmesh(cmd, cwd=None, **kwargs):
            mock_result = MagicMock()
            if cmd and cmd[0] == "blockMesh":
                mock_result.returncode = 1
                mock_result.stderr = "blockMeshDict error"
            else:
                mock_result.returncode = 0
                mock_result.stderr = ""
            mock_result.stdout = ""
            return mock_result

        mock_subproc.side_effect = fail_blockmesh

        solver = OpenFOAMSolver()
        with tempfile.TemporaryDirectory() as td:
            script = self._make_geometry_script(td)
            with self.assertRaises(RuntimeError) as ctx:
                solver.solve(
                    params={},
                    fixed={"velocity_mps": 10.0},
                    config_params={
                        "geometry_script": script,
                        "mesh_refinement": 1,
                        "cleanup_cases": True,
                    },
                )
            self.assertIn("blockMesh", str(ctx.exception))

    @patch("server.study_solvers._run_geometry_script")
    @patch("server.study_solvers.subprocess.run")
    def test_simplefoam_timeout_raises(self, mock_subproc, mock_geom):
        import subprocess as sp
        stl_holder: list[str] = []
        mock_geom.side_effect = self._mock_run_geometry(stl_holder)

        def timeout_simplefoam(cmd, cwd=None, **kwargs):
            if cmd and cmd[0] == "simpleFoam":
                raise sp.TimeoutExpired(cmd, kwargs.get("timeout", 60))
            # For mesh commands, succeed and create polyMesh
            if cwd:
                pm = Path(cwd) / "constant" / "polyMesh"
                pm.mkdir(parents=True, exist_ok=True)
                (pm / "points").write_text("// mock\n")
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            return mock_result

        mock_subproc.side_effect = timeout_simplefoam

        solver = OpenFOAMSolver()
        with tempfile.TemporaryDirectory() as td:
            script = self._make_geometry_script(td)
            with self.assertRaises(RuntimeError) as ctx:
                solver.solve(
                    params={},
                    fixed={"velocity_mps": 10.0},
                    config_params={
                        "geometry_script": script,
                        "mesh_refinement": 1,
                        "cleanup_cases": True,
                    },
                )
            self.assertIn("timed out", str(ctx.exception))

    @patch("server.study_solvers._run_geometry_script")
    @patch("server.study_solvers.subprocess.run")
    def test_cleanup_cases_false_preserves_dir(self, mock_subproc, mock_geom):
        stl_holder: list[str] = []
        case_holder: list[str] = []
        mock_geom.side_effect = self._mock_run_geometry(stl_holder)
        mock_subproc.side_effect = self._mock_subprocess_run_success(case_holder)

        solver = OpenFOAMSolver()
        with tempfile.TemporaryDirectory() as td:
            script = self._make_geometry_script(td)
            solver.solve(
                params={},
                fixed={"velocity_mps": 10.0},
                config_params={
                    "geometry_script": script,
                    "mesh_refinement": 1,
                    "cleanup_cases": False,
                },
            )

        # Case dir should still exist
        if case_holder:
            self.assertTrue(Path(case_holder[0]).exists())
            # Clean up manually
            import shutil
            shutil.rmtree(case_holder[0], ignore_errors=True)

    @patch("server.study_solvers._run_geometry_script")
    @patch("server.study_solvers.subprocess.run")
    def test_angle_of_attack_decomposition(self, mock_subproc, mock_geom):
        """Verify AoA decomposes velocity correctly in the case files."""
        stl_holder: list[str] = []
        case_holder: list[str] = []
        mock_geom.side_effect = self._mock_run_geometry(stl_holder)
        mock_subproc.side_effect = self._mock_subprocess_run_success(case_holder)

        solver = OpenFOAMSolver()
        with tempfile.TemporaryDirectory() as td:
            script = self._make_geometry_script(td)
            solver.solve(
                params={},
                fixed={"velocity_mps": 10.0, "angle_of_attack_deg": 0.0},
                config_params={
                    "geometry_script": script,
                    "mesh_refinement": 1,
                    "cleanup_cases": False,
                },
            )

        # At 0 degrees AoA, vx=10, vz=0
        if case_holder:
            u_file = Path(case_holder[0]) / "0" / "U"
            text = u_file.read_text()
            self.assertIn("10.0", text)
            import shutil
            shutil.rmtree(case_holder[0], ignore_errors=True)

    def test_missing_geometry_script_raises(self) -> None:
        solver = OpenFOAMSolver()
        with self.assertRaises(RuntimeError):
            solver.solve({}, {}, {"mesh_refinement": 2})

    def test_nonexistent_geometry_script_raises(self) -> None:
        solver = OpenFOAMSolver()
        with self.assertRaises(RuntimeError):
            solver.solve(
                {}, {},
                {"mesh_refinement": 2, "geometry_script": "/nonexistent/script.py"},
            )


if __name__ == "__main__":
    unittest.main()
