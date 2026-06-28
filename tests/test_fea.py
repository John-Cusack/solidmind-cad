"""Unit tests for orchestrator.fea — no ccx or gmsh required."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.fea import (
    FEAResult,
    SingularityFlag,
    build_inp,
    check_convergence,
    detect_singularities,
    filter_stress_excluding_singularities,
)
from orchestrator.fea_bc_mapper import (
    FEABoundaryCondition,
    extract_mesh_nodes,
    find_nodes_near_frame,
)
from orchestrator.materials import Material
from orchestrator.scorer import _needs_fea
from orchestrator.spec import (
    CoordinateFrame,
    Interface,
    LoadCase,
    MasterSpec,
    Objective,
)


class TestFEABoundaryCondition(unittest.TestCase):
    def test_creation(self):
        bc = FEABoundaryCondition(
            node_set_name="ifc_abc",
            bc_type="fixed",
            values=[0.0, 0.0, 0.0],
        )
        self.assertEqual(bc.bc_type, "fixed")
        self.assertEqual(bc.node_set_name, "ifc_abc")

    def test_force_bc(self):
        bc = FEABoundaryCondition(
            node_set_name="ifc_load",
            bc_type="force",
            values=[0.0, 0.0, -1000.0],
        )
        self.assertEqual(bc.bc_type, "force")
        self.assertAlmostEqual(bc.values[2], -1000.0)


class TestFindNodesNearFrame(unittest.TestCase):
    def test_basic(self):
        nodes = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (10.0, 0.0, 0.0),
            4: (0.5, 0.5, 0.0),
        }
        frame = CoordinateFrame(origin_mm=[0.0, 0.0, 0.0])
        result = find_nodes_near_frame(nodes, frame, 2.0)
        self.assertIn(1, result)
        self.assertIn(2, result)
        self.assertIn(4, result)
        self.assertNotIn(3, result)

    def test_offset_frame(self):
        nodes = {
            1: (10.0, 10.0, 10.0),
            2: (10.1, 10.0, 10.0),
            3: (0.0, 0.0, 0.0),
        }
        frame = CoordinateFrame(origin_mm=[10.0, 10.0, 10.0])
        result = find_nodes_near_frame(nodes, frame, 0.5)
        self.assertIn(1, result)
        self.assertIn(2, result)
        self.assertNotIn(3, result)


class TestExtractMeshNodes(unittest.TestCase):
    def test_parse(self):
        inp_text = """\
*NODE
1, 0.0, 0.0, 0.0
2, 1.0, 0.0, 0.0
3, 0.5, 0.866, 0.0
*ELEMENT, TYPE=C3D10
1, 1, 2, 3, 1, 2, 3, 1, 2, 3, 1
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".inp", delete=False) as f:
            f.write(inp_text)
            f.flush()
            nodes = extract_mesh_nodes(f.name)

        self.assertEqual(len(nodes), 3)
        self.assertAlmostEqual(nodes[2][0], 1.0)


class TestCheckConvergence(unittest.TestCase):
    def test_converged(self):
        pct, ok = check_convergence(100.0, 105.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(pct, 100 * 5.0 / 105.0, places=1)

    def test_not_converged(self):
        pct, ok = check_convergence(100.0, 130.0)
        self.assertFalse(ok)
        self.assertGreater(pct, 10.0)

    def test_zero_fine(self):
        pct, ok = check_convergence(0.0, 0.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(pct, 0.0)


class TestDetectSingularities(unittest.TestCase):
    def test_with_spike(self):
        # 100 elements at ~50 MPa, 5 elements at 200 MPa
        stresses = [50.0] * 95 + [200.0] * 5
        result = FEAResult(
            mesh_density="test",
            max_von_mises_mpa=200.0,
            stress_per_element=stresses,
        )
        flags = detect_singularities(result)
        self.assertGreater(len(flags), 0)
        for f in flags:
            self.assertAlmostEqual(f.stress_mpa, 200.0)

    def test_uniform_no_flags(self):
        stresses = [50.0] * 100
        result = FEAResult(
            mesh_density="test",
            max_von_mises_mpa=50.0,
            stress_per_element=stresses,
        )
        flags = detect_singularities(result)
        self.assertEqual(len(flags), 0)

    def test_empty_stresses(self):
        result = FEAResult(mesh_density="test", stress_per_element=[])
        flags = detect_singularities(result)
        self.assertEqual(len(flags), 0)


class TestFilterSingularities(unittest.TestCase):
    def test_filter(self):
        stresses = [10.0, 20.0, 30.0, 500.0]
        result = FEAResult(
            mesh_density="test",
            max_von_mises_mpa=500.0,
            stress_per_element=stresses,
        )
        flags = [SingularityFlag(element_id=3, stress_mpa=500.0, reason="test")]
        filtered = filter_stress_excluding_singularities(result, flags)
        self.assertAlmostEqual(filtered, 30.0)


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


class TestBuildInp(unittest.TestCase):
    def test_generates_cards(self):
        mesh_text = """\
*NODE
1, 0.0, 0.0, 0.0
2, 10.0, 0.0, 0.0
3, 5.0, 8.66, 0.0
4, 5.0, 2.89, 5.0
*ELEMENT, TYPE=C3D10
1, 1, 2, 3, 4, 1, 2, 3, 4, 1, 2
"""
        mat = Material("Test", 200_000, 0.3, 400, 7800)
        bcs = [
            FEABoundaryCondition("FIXED", "fixed"),
            FEABoundaryCondition("LOAD", "force", [0.0, 0.0, -100.0]),
        ]

        with tempfile.TemporaryDirectory() as td:
            mesh_inp = Path(td) / "mesh.inp"
            mesh_inp.write_text(mesh_text)
            out_inp = Path(td) / "analysis.inp"

            node_sets = {"FIXED": [1, 2], "LOAD": [3, 4]}
            build_inp(mesh_inp, mat, bcs, out_inp, node_sets)

            content = out_inp.read_text()

        self.assertIn("*MATERIAL", content)
        self.assertIn("*ELASTIC", content)
        self.assertIn("200000", content)
        self.assertIn("*BOUNDARY", content)
        # Solid C3D10 nodes have 3 translational DOF — fixing 1-6 segfaults ccx.
        self.assertIn("FIXED, 1, 3", content)
        self.assertNotIn("FIXED, 1, 6", content)
        self.assertIn("*CLOAD", content)
        # Load value is bounded-width formatted (-100.0 -> -100) so it fits ccx's
        # fixed-width card field.
        self.assertIn("LOAD, 3, -100", content)
        # EALL is an explicit element list, not GENERATE-to-huge (which referenced
        # phantom element ids and segfaulted the solver).
        self.assertIn("*ELSET, ELSET=EALL", content)
        self.assertNotIn("GENERATE", content)
        self.assertIn("*STEP", content)
        self.assertIn("*END STEP", content)
        self.assertIn("*NSET, NSET=FIXED", content)
        self.assertIn("*NSET, NSET=LOAD", content)


if __name__ == "__main__":
    unittest.main()
