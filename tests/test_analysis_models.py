"""Tests for analysis data models — construction and serialization round-trips."""

from __future__ import annotations

import unittest

from server.analysis_models import (
    AnalysisCheck,
    AnalysisSpec,
    AnalysisType,
    BoundaryCondition,
    CheckStatus,
    FaceGroup,
    FieldResult,
    Material,
    MeshInfo,
    ScalarFieldSummary,
)


class TestMaterial(unittest.TestCase):
    def test_construction_and_round_trip(self) -> None:
        m = Material(
            name="test_steel",
            youngs_modulus_mpa=200_000,
            poissons_ratio=0.3,
            density_kg_m3=7800,
            yield_strength_mpa=250,
        )
        d = m.to_dict()
        m2 = Material.from_dict(d)
        self.assertEqual(m, m2)
        self.assertEqual(d["name"], "test_steel")
        self.assertEqual(d["youngs_modulus_mpa"], 200_000)

    def test_optional_thermal_fields(self) -> None:
        m = Material(
            name="al",
            youngs_modulus_mpa=70_000,
            poissons_ratio=0.33,
            density_kg_m3=2700,
            yield_strength_mpa=276,
            thermal_conductivity_w_mk=167,
        )
        d = m.to_dict()
        m2 = Material.from_dict(d)
        self.assertEqual(m2.thermal_conductivity_w_mk, 167)
        self.assertEqual(m2.specific_heat_j_kgk, 0.0)


class TestBoundaryCondition(unittest.TestCase):
    def test_fixed_bc(self) -> None:
        bc = BoundaryCondition(bc_type="fixed", faces=("Face1", "Face2"))
        d = bc.to_dict()
        self.assertEqual(d["bc_type"], "fixed")
        self.assertEqual(d["faces"], ["Face1", "Face2"])
        bc2 = BoundaryCondition.from_dict(d)
        self.assertEqual(bc, bc2)

    def test_force_bc(self) -> None:
        bc = BoundaryCondition(
            bc_type="force",
            faces=("Face5",),
            value={"fx": 0, "fy": 0, "fz": -100},
        )
        d = bc.to_dict()
        bc2 = BoundaryCondition.from_dict(d)
        self.assertEqual(bc2.value["fz"], -100)


class TestFaceGroup(unittest.TestCase):
    def test_round_trip(self) -> None:
        fg = FaceGroup(name="support", face_refs=("Face1", "Face3"))
        d = fg.to_dict()
        fg2 = FaceGroup.from_dict(d)
        self.assertEqual(fg, fg2)


class TestAnalysisSpec(unittest.TestCase):
    def test_round_trip(self) -> None:
        mat = Material(
            name="steel",
            youngs_modulus_mpa=200_000,
            poissons_ratio=0.3,
            density_kg_m3=7800,
            yield_strength_mpa=250,
        )
        bc = BoundaryCondition(bc_type="fixed", faces=("Face1",))
        spec = AnalysisSpec(
            analysis_type=AnalysisType.STRUCTURAL,
            body="Body",
            material=mat,
            boundary_conditions=(bc,),
            mesh_size=2.0,
            solver="calculix",
        )
        d = spec.to_dict()
        spec2 = AnalysisSpec.from_dict(d)
        self.assertEqual(spec.analysis_type, spec2.analysis_type)
        self.assertEqual(spec.body, spec2.body)
        self.assertEqual(spec.material, spec2.material)
        self.assertEqual(spec.mesh_size, spec2.mesh_size)


class TestScalarFieldSummary(unittest.TestCase):
    def test_round_trip(self) -> None:
        sf = ScalarFieldSummary(
            field_name="von_mises_stress",
            min_val=0.5,
            max_val=150.3,
            mean_val=45.2,
            unit="MPa",
            max_location_xyz=(10.0, 5.0, 3.0),
        )
        d = sf.to_dict()
        sf2 = ScalarFieldSummary.from_dict(d)
        self.assertEqual(sf.field_name, sf2.field_name)
        self.assertAlmostEqual(sf.max_val, sf2.max_val)
        self.assertEqual(sf.max_location_xyz, sf2.max_location_xyz)


class TestAnalysisCheck(unittest.TestCase):
    def test_round_trip(self) -> None:
        c = AnalysisCheck(
            name="yield_check",
            status=CheckStatus.WARN,
            message="Close to yield",
            measured=200.0,
            limit=250.0,
            face_group="Face5",
            suggestion="Add fillet",
        )
        d = c.to_dict()
        c2 = AnalysisCheck.from_dict(d)
        self.assertEqual(c, c2)
        self.assertEqual(d["status"], "warn")


class TestFieldResult(unittest.TestCase):
    def test_round_trip(self) -> None:
        check = AnalysisCheck(
            name="yield_check",
            status=CheckStatus.PASS,
            message="OK",
            measured=100.0,
            limit=250.0,
        )
        field = ScalarFieldSummary(
            field_name="von_mises_stress",
            min_val=0.0,
            max_val=100.0,
            mean_val=50.0,
            unit="MPa",
        )
        result = FieldResult(
            analysis_id="test_001",
            status=CheckStatus.PASS,
            safety_factor=2.5,
            max_von_mises_mpa=100.0,
            max_displacement_mm=0.05,
            checks=(check,),
            scalar_fields=(field,),
            solver_name="calculix",
            solve_time_s=1.5,
        )
        d = result.to_dict()
        result2 = FieldResult.from_dict(d)
        self.assertEqual(result.analysis_id, result2.analysis_id)
        self.assertEqual(result.status, result2.status)
        self.assertEqual(result.safety_factor, result2.safety_factor)
        self.assertEqual(len(result2.checks), 1)
        self.assertEqual(len(result2.scalar_fields), 1)


class TestMeshInfo(unittest.TestCase):
    def test_round_trip(self) -> None:
        mi = MeshInfo(
            path="/tmp/mesh.msh",
            num_nodes=1000,
            num_elements=500,
            element_type="tet4",
            physical_groups={"Face1": 1, "Face3": 2},
        )
        d = mi.to_dict()
        mi2 = MeshInfo.from_dict(d)
        self.assertEqual(mi, mi2)


if __name__ == "__main__":
    unittest.main()
