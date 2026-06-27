"""Integration tests for L2 FEA pipeline — requires ccx and gmsh."""
from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

_FIXTURE_STEP = Path(__file__).parent / "fixtures" / "simple_cylinder.step"


def _has_gmsh() -> bool:
    return importlib.util.find_spec("gmsh") is not None


def _has_meshio() -> bool:
    return importlib.util.find_spec("meshio") is not None


def _create_cylinder_step(dest: Path) -> None:
    """Create a cylinder STEP file using Gmsh OCC, or copy the committed fixture."""
    if _has_gmsh():
        import gmsh
        gmsh.initialize()
        try:
            gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 50, 5)
            gmsh.model.occ.synchronize()
            gmsh.write(str(dest))
        finally:
            gmsh.finalize()
    elif _FIXTURE_STEP.exists():
        shutil.copy2(_FIXTURE_STEP, dest)
    else:
        raise unittest.SkipTest("No gmsh and no fixture STEP file")


@unittest.skipUnless(
    shutil.which("ccx") and _has_gmsh(),
    "ccx and/or gmsh not available",
)
class TestFEAIntegration(unittest.TestCase):
    """Full L2 pipeline test with a simple cylinder under axial load."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="fea_test_")
        cls.step_path = Path(cls._tmpdir) / "cylinder.step"
        _create_cylinder_step(cls.step_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def test_mesh_step(self):
        from orchestrator.fea import mesh_step

        out_dir = Path(self._tmpdir) / "mesh_test"
        info = mesh_step(self.step_path, 3.0, out_dir)
        self.assertGreater(info.element_count, 0)
        self.assertGreater(info.node_count, 0)
        self.assertTrue(info.path.exists())

    def test_full_pipeline(self):
        from orchestrator.fea import run_l2_fea
        from orchestrator.materials import resolve_material
        from orchestrator.spec import (
            CoordinateFrame,
            Interface,
            LoadCase,
            ManufacturingSpec,
            Subsystem,
        )

        material = resolve_material("steel")
        self.assertIsNotNone(material)

        sub = Subsystem(
            name="test_cylinder",
            material="steel",
            manufacturing=ManufacturingSpec(min_feature_size_mm=2.0),
        )

        # Bottom end fixed (z=0), top end loaded (z=50)
        ifc_fixed = Interface(
            id="fixed_end",
            name="bottom",
            subsystem_a="test_cylinder",
            port_a="bottom",
            subsystem_b="ground",
            port_b="mount",
            frame_a=CoordinateFrame(origin_mm=[0.0, 0.0, 0.0]),
            loads=[],  # no loads = fixed support
        )

        # Axial load at top: F = 1000 N
        # Analytical stress: sigma = F/A = 1000 / (pi * 5^2) ~ 12.7 MPa
        ifc_loaded = Interface(
            id="loaded_end",
            name="top",
            subsystem_a="test_cylinder",
            port_a="top",
            subsystem_b="load",
            port_b="apply",
            frame_a=CoordinateFrame(origin_mm=[0.0, 0.0, 50.0]),
            loads=[LoadCase(name="axial", axial_force_n=1000.0)],
        )
        sub.interfaces = ["fixed_end", "loaded_end"]

        work_dir = Path(self._tmpdir) / "fea_run"

        report = run_l2_fea(
            self.step_path,
            sub,
            [ifc_fixed, ifc_loaded],
            material,
            work_dir,
        )

        # Should pass — steel cylinder under 1000N is well within yield
        self.assertTrue(report.passed, f"FEA should pass: SF={report.safety_factor:.2f}")
        self.assertGreater(report.safety_factor, 1.0)
        self.assertGreater(report.filtered_max_stress_mpa, 0)

        # Stress should be in the right ballpark (analytical ~ 12.7 MPa)
        # Allow generous range due to stress concentrations and BCs
        self.assertLess(report.filtered_max_stress_mpa, 200.0,
                        "Stress unreasonably high for simple axial load")

    def test_scorer_integration(self):
        """Verify score_run() produces L2 VerificationResults with stress objective."""
        from orchestrator.scorer import VerificationLevel, score_run
        from orchestrator.spec import (
            CoordinateFrame,
            Interface,
            LoadCase,
            ManufacturingSpec,
            MasterSpec,
            Objective,
            Subsystem,
        )
        from orchestrator.validator import ValidationReport

        sub = Subsystem(
            name="bracket",
            material="aluminum",
            manufacturing=ManufacturingSpec(min_feature_size_mm=2.0),
            interfaces=["mount", "load_point"],
        )

        spec = MasterSpec(
            name="bracket_test",
            objectives=[
                Objective(name="max_stress", direction="minimize", unit="MPa"),
            ],
            subsystems=[sub],
            interfaces=[
                Interface(
                    id="mount",
                    subsystem_a="bracket",
                    port_a="base",
                    subsystem_b="wall",
                    port_b="surface",
                    frame_a=CoordinateFrame(origin_mm=[0, 0, 0]),
                    loads=[],
                ),
                Interface(
                    id="load_point",
                    subsystem_a="bracket",
                    port_a="tip",
                    subsystem_b="payload",
                    port_b="attach",
                    frame_a=CoordinateFrame(origin_mm=[0, 0, 50]),
                    loads=[LoadCase(axial_force_n=500)],
                ),
            ],
        )

        # Create a mock run directory with the STEP file
        run_dir = Path(self._tmpdir) / "scorer_run"
        variant_out = run_dir / "bracket_0" / "output"
        variant_out.mkdir(parents=True)
        shutil.copy2(self.step_path, variant_out / "bracket.step")

        report = ValidationReport(
            subsystem_name="bracket",
            worker_id="bracket_0",
            overall_pass=True,
        )

        scoring = score_run(spec, [report], run_dir=run_dir)

        l2_results = [
            vr for vr in scoring.verification_results
            if vr.level == VerificationLevel.L2_COARSE_FEA
        ]
        self.assertGreater(len(l2_results), 0, "Expected L2 verification results")

        # Should have stress, convergence, and safety factor checks
        check_names = {vr.check_name for vr in l2_results}
        self.assertIn("max_von_mises", check_names)
        self.assertIn("safety_factor", check_names)


if __name__ == "__main__":
    unittest.main()
