"""Unit tests for the unified FEA adapter layer — no ccx or gmsh required.

The mesh→deck→solve→parse engine and the convergence study now live under
``server/`` (exercised by ``tests/test_fea_integration.py`` /
``tests/test_foam_dart_fea_e2e.py`` on the real-backend lane). What stays pure
and testable here is the orchestrator's batch adapter: convergence math,
material translation, frame→face BC mapping, and the gate in ``scorer``.
"""

from __future__ import annotations

import unittest

from orchestrator.fea import FEAError, FEAReport, _to_analysis_material, run_l2_fea
from orchestrator.fea_bc_mapper import (
    _world_force_for_loads,
    has_loaded_interface,
    map_frame_to_face,
    map_interface_bcs,
)
from orchestrator.materials import Material
from orchestrator.scorer import _needs_fea
from orchestrator.spec import (
    CoordinateFrame,
    Interface,
    LoadCase,
    MasterSpec,
    Objective,
    Subsystem,
)
from server.analysis_convergence import ConvergenceReport, relative_change
from server.analysis_models import FieldResult


class TestRelativeChange(unittest.TestCase):
    def test_converged(self):
        rel = relative_change(100.0, 105.0)
        self.assertAlmostEqual(rel, 5.0 / 105.0)

    def test_not_converged(self):
        rel = relative_change(100.0, 130.0)
        self.assertGreater(rel, 0.10)

    def test_zero_denominator_is_none(self):
        self.assertIsNone(relative_change(0.0, 0.0))
        self.assertIsNone(relative_change(5.0, 0.0))


class TestConvergenceReport(unittest.TestCase):
    def _result(self, vm: float, sf: float) -> FieldResult:
        from server.analysis_models import CheckStatus

        return FieldResult(
            analysis_id="t",
            status=CheckStatus.PASS,
            safety_factor=sf,
            max_von_mises_mpa=vm,
            max_displacement_mm=0.1,
            checks=(),
            scalar_fields=(),
        )

    def test_passed_requires_converge_and_yield(self):
        ok = ConvergenceReport(
            coarse=self._result(90.0, 4.0),
            fine=self._result(100.0, 3.6),
            peak_coarse_mpa=90.0,
            peak_fine_mpa=100.0,
            convergence_delta=0.05,
            converged=True,
        )
        self.assertTrue(ok.passed)
        self.assertAlmostEqual(ok.safety_factor, 3.6)

        diverged = ConvergenceReport(
            coarse=self._result(60.0, 6.0),
            fine=self._result(100.0, 3.6),
            peak_coarse_mpa=60.0,
            peak_fine_mpa=100.0,
            convergence_delta=0.4,
            converged=False,
        )
        self.assertFalse(diverged.passed)


class TestToAnalysisMaterial(unittest.TestCase):
    def test_field_mapping(self):
        m = Material("Test", 200_000, 0.3, 400, 7800)
        a = _to_analysis_material(m)
        self.assertEqual(a.name, "Test")
        self.assertAlmostEqual(a.youngs_modulus_mpa, 200_000)
        self.assertAlmostEqual(a.poissons_ratio, 0.3)
        self.assertAlmostEqual(a.yield_strength_mpa, 400)
        self.assertAlmostEqual(a.density_kg_m3, 7800)


class TestFEAReportDefaults(unittest.TestCase):
    def test_defaults(self):
        r = FEAReport(subsystem_name="x")
        self.assertFalse(r.passed)
        self.assertFalse(r.converged)
        self.assertEqual(r.peak_fine_mpa, 0.0)


class TestMapFrameToFace(unittest.TestCase):
    # A synthetic geometry map: {face index: (centroid, characteristic_radius)}.
    GEOM = {
        1: ((0.0, 0.0, 0.0), 5.0),  # Face1 at the origin
        2: ((0.0, 0.0, 50.0), 5.0),  # Face2 up the axis
        3: ((10.0, 0.0, 25.0), 5.0),  # Face3 off to the side
    }

    def test_picks_nearest_centroid(self):
        bottom = map_frame_to_face(self.GEOM, CoordinateFrame(origin_mm=[0.0, 0.0, 2.0]))
        self.assertEqual(bottom, "Face1")
        top = map_frame_to_face(self.GEOM, CoordinateFrame(origin_mm=[0.0, 0.0, 48.0]))
        self.assertEqual(top, "Face2")

    def test_empty_geometry_is_none(self):
        self.assertIsNone(map_frame_to_face({}, CoordinateFrame()))


class TestWorldForceForLoads(unittest.TestCase):
    def test_axial_along_frame_z(self):
        frame = CoordinateFrame()  # identity axes
        fx, fy, fz = _world_force_for_loads([LoadCase(axial_force_n=100.0)], frame, radius_mm=5.0)
        self.assertAlmostEqual(fz, 100.0)
        self.assertAlmostEqual(fx, 0.0)
        self.assertAlmostEqual(fy, 0.0)

    def test_radial_along_frame_x(self):
        frame = CoordinateFrame()
        fx, _fy, _fz = _world_force_for_loads([LoadCase(radial_force_n=50.0)], frame, radius_mm=5.0)
        self.assertAlmostEqual(fx, 50.0)

    def test_torque_becomes_tangential_force(self):
        frame = CoordinateFrame()
        # F = T(N·m)*1000 / r(mm), along frame Y.
        _fx, fy, _fz = _world_force_for_loads([LoadCase(torque_nm=1.0)], frame, radius_mm=10.0)
        self.assertAlmostEqual(fy, 1000.0 / 10.0)


class TestMapInterfaceBcs(unittest.TestCase):
    GEOM = {
        1: ((0.0, 0.0, 0.0), 5.0),
        2: ((0.0, 0.0, 50.0), 5.0),
    }

    def _sub(self) -> Subsystem:
        return Subsystem(name="part")

    def test_zero_load_interface_is_fixed_face(self):
        ifc = Interface(
            id="mount",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0.0, 0.0, 0.0]),
            loads=[],
        )
        bcs = map_interface_bcs(self._sub(), [ifc], self.GEOM)
        self.assertEqual(len(bcs), 1)
        self.assertEqual(bcs[0].bc_type, "fixed")
        self.assertEqual(bcs[0].faces, ("Face1",))

    def test_loaded_interface_is_force_face(self):
        ifc = Interface(
            id="load",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0.0, 0.0, 50.0]),
            loads=[LoadCase(axial_force_n=1000.0)],
        )
        bcs = map_interface_bcs(self._sub(), [ifc], self.GEOM)
        self.assertEqual(len(bcs), 1)
        self.assertEqual(bcs[0].bc_type, "force")
        self.assertEqual(bcs[0].faces, ("Face2",))
        self.assertAlmostEqual(bcs[0].value["fz"], 1000.0)

    def test_no_geometry_yields_no_bcs(self):
        ifc = Interface(id="x", subsystem_a="part", loads=[])
        self.assertEqual(map_interface_bcs(self._sub(), [ifc], {}), [])

    def test_fixed_and_force_do_not_share_a_face(self):
        # Two interfaces both nearest Face1; the support claims it first, so the
        # load is pushed onto a different face (its reaction can't absorb it).
        geom = {1: ((0.0, 0.0, 0.0), 5.0), 2: ((0.0, 0.0, 6.0), 5.0)}
        fixed = Interface(
            id="m", subsystem_a="part", frame_a=CoordinateFrame(origin_mm=[0, 0, 0]), loads=[]
        )
        load = Interface(
            id="l",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0, 0, 1]),
            loads=[LoadCase(axial_force_n=500.0)],
        )
        bcs = map_interface_bcs(self._sub(), [fixed, load], geom)
        by_type = {b.bc_type: b.faces for b in bcs}
        self.assertEqual(by_type["fixed"], ("Face1",))
        self.assertEqual(by_type["force"], ("Face2",))

    def test_all_loaded_no_support_is_not_anchored(self):
        # No zero-load interface → we do NOT fabricate an anchor (that would drop a
        # real load and under-predict stress). The model is left force-only and
        # under-constrained, so the solve fails closed downstream.
        geom = {1: ((0.0, 0.0, 0.0), 5.0), 2: ((0.0, 0.0, 6.0), 5.0)}
        light = Interface(
            id="a",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0, 0, 0]),
            loads=[LoadCase(axial_force_n=100.0)],
        )
        heavy = Interface(
            id="b",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0, 0, 6]),
            loads=[LoadCase(axial_force_n=1000.0)],
        )
        bcs = map_interface_bcs(self._sub(), [light, heavy], geom)
        self.assertTrue(all(b.bc_type == "force" for b in bcs))
        self.assertEqual(len(bcs), 2)

    def test_frame_far_from_geometry_is_skipped(self):
        # A frame sitting on the mating partner (well beyond any face) yields no BC.
        geom = {1: ((0.0, 0.0, 0.0), 5.0)}
        far = Interface(
            id="x",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0, 0, 100]),
            loads=[LoadCase(axial_force_n=500.0)],
        )
        self.assertEqual(map_interface_bcs(self._sub(), [far], geom), [])


class TestAxisRadialDistance(unittest.TestCase):
    def test_perpendicular_offset(self):
        from orchestrator.fea_bc_mapper import _axis_radial_distance

        frame = CoordinateFrame(origin_mm=[0, 0, 0])  # axis_z = +Z
        self.assertAlmostEqual(_axis_radial_distance((10.0, 0.0, 0.0), frame), 10.0)

    def test_point_on_axis_is_zero(self):
        from orchestrator.fea_bc_mapper import _axis_radial_distance

        frame = CoordinateFrame(origin_mm=[0, 0, 0])
        self.assertAlmostEqual(_axis_radial_distance((0.0, 0.0, 5.0), frame), 0.0)


class TestZeroPeakGuard(unittest.TestCase):
    """A load that produces no stress must fail closed, not report a vacuous pass."""

    def _zero_result(self):
        from server.analysis_models import CheckStatus

        return FieldResult(
            analysis_id="t",
            status=CheckStatus.PASS,
            safety_factor=float("inf"),
            max_von_mises_mpa=0.0,
            max_displacement_mm=0.0,
            checks=(),
            scalar_fields=(),
        )

    def test_force_bc_with_zero_peak_raises(self):
        import unittest.mock as mock

        import server.analysis_convergence as conv
        from server.analysis_models import BoundaryCondition
        from server.analysis_models import Material as AMaterial
        from server.tools_analysis import StructuralSolveError

        with mock.patch(
            "server.tools_analysis.solve_structural_from_step",
            side_effect=lambda **_: self._zero_result(),
        ):
            with self.assertRaises(StructuralSolveError) as cm:
                conv.run_convergence_study(
                    step_path="x.step",
                    material=AMaterial(
                        name="s",
                        youngs_modulus_mpa=200_000,
                        poissons_ratio=0.3,
                        density_kg_m3=7800,
                        yield_strength_mpa=400,
                    ),
                    boundary_conditions=[
                        BoundaryCondition(bc_type="force", faces=("Face1",), value={"fz": -10.0}),
                    ],
                    coarse_size=1.0,
                    fine_size=0.5,
                )
            self.assertEqual(cm.exception.code, "ZERO_PEAK_UNDER_LOAD")

    def test_no_load_zero_peak_is_allowed(self):
        import unittest.mock as mock

        import server.analysis_convergence as conv
        from server.analysis_models import BoundaryCondition
        from server.analysis_models import Material as AMaterial

        with mock.patch(
            "server.tools_analysis.solve_structural_from_step",
            side_effect=lambda **_: self._zero_result(),
        ):
            report = conv.run_convergence_study(
                step_path="x.step",
                material=AMaterial(
                    name="s",
                    youngs_modulus_mpa=200_000,
                    poissons_ratio=0.3,
                    density_kg_m3=7800,
                    yield_strength_mpa=400,
                ),
                boundary_conditions=[
                    BoundaryCondition(bc_type="fixed", faces=("Face1",), value={})
                ],
                coarse_size=1.0,
                fine_size=0.5,
            )
        self.assertTrue(report.converged)


class TestHasLoadedInterface(unittest.TestCase):
    def test_detects_load(self):
        sub = Subsystem(name="part")
        loaded = Interface(id="l", subsystem_a="part", loads=[LoadCase(axial_force_n=10.0)])
        unloaded = Interface(id="u", subsystem_a="part", loads=[])
        self.assertTrue(has_loaded_interface(sub, [loaded]))
        self.assertFalse(has_loaded_interface(sub, [unloaded]))
        self.assertFalse(has_loaded_interface(sub, []))


class TestRunL2FeaGate(unittest.TestCase):
    """run_l2_fea must fail closed, never silently pass, when a load can't be set up."""

    def test_declared_load_that_maps_to_no_force_raises(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path

        from server.analysis_models import BoundaryCondition

        sub = Subsystem(name="part")
        loaded = Interface(
            id="l",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0, 0, 0]),
            loads=[LoadCase(axial_force_n=500.0)],
        )
        material = Material("steel", 200_000, 0.3, 400, 7800)
        # Geometry enumerates fine, but the mapper yields only a support (the load
        # collided with it / fell outside) — a setup failure, not an unloaded part.
        with (
            mock.patch(
                "orchestrator.fea.surface_geometry", return_value={1: ((0.0, 0.0, 0.0), 5.0)}
            ),
            mock.patch(
                "orchestrator.fea.map_interface_bcs",
                return_value=[BoundaryCondition(bc_type="fixed", faces=("Face1",), value={})],
            ),
        ):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(FEAError):
                    run_l2_fea(Path("x.step"), sub, [loaded], material, Path(td))

    def test_empty_geometry_raises(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path

        sub = Subsystem(name="part")
        material = Material("steel", 200_000, 0.3, 400, 7800)
        with mock.patch("orchestrator.fea.surface_geometry", return_value={}):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(FEAError):
                    run_l2_fea(Path("x.step"), sub, [], material, Path(td))


class TestFilteredPeak(unittest.TestCase):
    def test_singularity_spike_excluded(self):
        from server.analysis_result_parser import _filtered_peak_von_mises

        # 95 nodes at ~50 MPa, 5 spike nodes at 500 MPa (a sharp-corner artifact).
        vals = [50.0] * 95 + [500.0] * 5
        self.assertAlmostEqual(_filtered_peak_von_mises(vals, max_vm=500.0), 50.0)

    def test_uniform_field_keeps_peak(self):
        from server.analysis_result_parser import _filtered_peak_von_mises

        vals = [50.0] * 100
        self.assertAlmostEqual(_filtered_peak_von_mises(vals, max_vm=50.0), 50.0)

    def test_empty_falls_back(self):
        from server.analysis_result_parser import _filtered_peak_von_mises

        self.assertAlmostEqual(_filtered_peak_von_mises([], max_vm=7.0), 7.0)


class TestRunL2FeaFilteredGate(unittest.TestCase):
    """The batch SF/convergence verdict must use the filtered peak, not the raw one."""

    def _field(self, raw: float, filtered: float):
        from server.analysis_models import CheckStatus, FieldResult

        return FieldResult(
            analysis_id="t",
            status=CheckStatus.PASS,
            safety_factor=0.0,
            max_von_mises_mpa=raw,
            max_displacement_mm=0.1,
            checks=(),
            scalar_fields=(),
            filtered_peak_von_mises_mpa=filtered,
        )

    def test_filtered_peak_drives_safety_factor(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path

        from orchestrator.spec import ManufacturingSpec
        from server.analysis_convergence import ConvergenceReport
        from server.analysis_models import BoundaryCondition

        sub = Subsystem(name="part", manufacturing=ManufacturingSpec(min_feature_size_mm=2.0))
        loaded = Interface(
            id="l",
            subsystem_a="part",
            frame_a=CoordinateFrame(origin_mm=[0, 0, 0]),
            loads=[LoadCase(axial_force_n=500.0)],
        )
        material = Material("petg", 2100, 0.4, 400, 1270)  # yield 400 MPa
        # Raw peak is a 2000 MPa singularity (SF 0.2 → would FAIL); filtered peak is
        # the real 100 MPa (SF 4.0 → PASS, converged).
        report_study = ConvergenceReport(
            coarse=self._field(1800.0, 98.0),
            fine=self._field(2000.0, 100.0),
            peak_coarse_mpa=1800.0,
            peak_fine_mpa=2000.0,
            convergence_delta=0.1,
            converged=False,  # raw diverges
        )
        force_bc = BoundaryCondition(bc_type="force", faces=("Face1",), value={"fz": -500.0})
        with (
            mock.patch(
                "orchestrator.fea.surface_geometry", return_value={1: ((0.0, 0.0, 0.0), 5.0)}
            ),
            mock.patch("orchestrator.fea.map_interface_bcs", return_value=[force_bc]),
            mock.patch("orchestrator.fea.run_convergence_study", return_value=report_study),
        ):
            with tempfile.TemporaryDirectory() as td:
                rep = run_l2_fea(Path("x.step"), sub, [loaded], material, Path(td))
        self.assertAlmostEqual(rep.peak_fine_mpa, 100.0)
        self.assertAlmostEqual(rep.safety_factor, 4.0)  # 400 / 100, not 400 / 2000
        self.assertTrue(rep.converged)  # filtered peak converges (98 → 100)
        self.assertTrue(rep.passed)


class TestNeedsFea(unittest.TestCase):
    def test_stress_objective(self):
        spec = MasterSpec(
            objectives=[Objective(name="max_stress", direction="minimize", unit="MPa")],
        )
        self.assertTrue(_needs_fea(spec))

    def test_no_objective_no_loads(self):
        spec = MasterSpec(
            objectives=[Objective(name="mass", direction="minimize", unit="kg")],
        )
        self.assertFalse(_needs_fea(spec))

    def test_interface_with_loads(self):
        ifc = Interface(loads=[LoadCase(axial_force_n=500.0)])
        spec = MasterSpec(
            objectives=[Objective(name="mass", direction="minimize", unit="kg")],
            interfaces=[ifc],
        )
        self.assertTrue(_needs_fea(spec))

    def test_safety_factor_objective(self):
        spec = MasterSpec(
            objectives=[Objective(name="safety_factor", direction="maximize", unit="")],
        )
        self.assertTrue(_needs_fea(spec))

    def test_displacement_objective(self):
        spec = MasterSpec(
            objectives=[Objective(name="max_displacement", direction="minimize", unit="mm")],
        )
        self.assertTrue(_needs_fea(spec))


if __name__ == "__main__":
    unittest.main()
