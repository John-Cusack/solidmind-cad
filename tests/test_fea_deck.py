"""CI-safe unit tests for the surviving CalculiX deck generation (no ccx/gmsh).

The old orchestrator deck-builder filtered gmsh's mixed element blocks out of a
*text* deck and bounded card field widths by hand; that duplicate builder is gone
(unified onto the shared engine). The surviving ``CalculiXSolver._write_ccx_native``
is immune to the same ccx-2.21 failures by construction — it builds the deck from
the meshio mesh object and emits ONLY solid C3D4 tetrahedra, with bounded-width
node coordinates. These tests lock that in so the fix can't silently regress.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.analysis_solvers import CalculiXSolver


class _Block:
    """Stand-in for a meshio CellBlock (``.type`` + ``.data``)."""

    def __init__(self, type_: str, data: list[list[int]]) -> None:
        self.type = type_
        self.data = data


class _Mesh:
    """Stand-in for a meshio Mesh (``.points`` + ``.cells``)."""

    def __init__(self, points: list[tuple[float, float, float]], cells: list[_Block]) -> None:
        self.points = points
        self.cells = cells


_POINTS = [
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (1.0, 1.0, 1.0),
]
# A solid tetra block plus the lower-dimensional blocks gmsh also emits (a line
# and a surface triangle). Only the tetrahedra may reach the CalculiX deck.
_CELLS = [
    _Block("line", [[0, 1]]),
    _Block("triangle", [[0, 1, 2]]),
    _Block("tetra", [[0, 1, 2, 3], [1, 2, 3, 4]]),
]


class TestWriteCcxNative(unittest.TestCase):
    def _deck(self) -> str:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "mesh.inp"
            CalculiXSolver()._write_ccx_native(_Mesh(_POINTS, _CELLS), out)
            return out.read_text()

    def test_only_c3d4_emitted(self) -> None:
        deck = self._deck()
        self.assertIn("TYPE=C3D4", deck)
        # The line/triangle blocks (gmsh's T3D3 / CPS6) crash ccx's gen3delem if
        # they survive into the deck — they must be dropped.
        self.assertNotIn("T3D3", deck)
        self.assertNotIn("CPS6", deck)
        self.assertNotIn("TRIANGLE", deck.upper())

    def test_exactly_the_two_tetra_elements(self) -> None:
        data_lines = [ln for ln in self._deck().splitlines() if ln[:1].isdigit()]
        # 5 node lines (4 comma fields) + 2 tetra element lines (5 comma fields).
        tet_lines = [ln for ln in data_lines if len(ln.split(",")) == 5]
        self.assertEqual(len(tet_lines), 2)

    def test_lines_within_ccx_limit(self) -> None:
        # CalculiX has a 132-character line limit; node coords use bounded %.10g.
        for line in self._deck().splitlines():
            self.assertLessEqual(len(line), 132)

    def test_quadratic_tetra_emits_c3d10(self) -> None:
        # The batch path meshes quadratic tet10; meshio names those cells "tetra10",
        # which must map to CalculiX C3D10 (the line/triangle blocks still drop).
        points = _POINTS + [
            (0.5, 0.0, 0.0),
            (0.0, 0.5, 0.0),
            (0.0, 0.0, 0.5),
            (0.5, 0.5, 0.0),
            (0.0, 0.5, 0.5),
            (0.5, 0.0, 0.5),
        ]
        cells = [
            _Block("line3", [[0, 1, 5]]),
            _Block("triangle6", [[0, 1, 2, 5, 6, 7]]),
            _Block("tetra10", [[0, 1, 2, 3, 5, 6, 7, 8, 9, 10]]),
        ]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "mesh.inp"
            CalculiXSolver()._write_ccx_native(_Mesh(points, cells), out)
            deck = out.read_text()
        self.assertIn("TYPE=C3D10", deck)
        self.assertNotIn("C3D4", deck)
        self.assertNotIn("TRIANGLE", deck.upper())
        # The one C3D10 element line carries id + 10 nodes = 11 comma fields.
        tet_lines = [
            ln for ln in deck.splitlines() if ln[:1].isdigit() and len(ln.split(",")) == 11
        ]
        self.assertEqual(len(tet_lines), 1)


class TestExtractNsetFromElset(unittest.TestCase):
    """meshio writes *ELSET members as floats (1.0, 2.0, ...); the BC node-set
    extraction must tolerate that, or every CalculiX deck references an undefined
    node set and the solve dies with 'node set Nbc0 has not yet been defined'."""

    _INP = """\
*NODE
10, 0.0, 0.0, 0.0
20, 1.0, 0.0, 0.0
30, 0.0, 1.0, 0.0
40, 0.0, 0.0, 1.0
*ELEMENT, TYPE=R3D3
1, 10, 20, 30
2, 20, 30, 40
*ELEMENT, TYPE=C3D4
3, 10, 20, 30, 40
*ELSET, ELSET=bc_0_fixed
1.0, 2.0
"""

    def test_float_formatted_elset_yields_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "mesh_full.inp"
            inp.write_text(self._INP)
            nodes = CalculiXSolver()._extract_nset_from_elset(inp, "bc_0_fixed")
        # Elements 1 and 2 reference nodes 10,20,30,40.
        self.assertEqual(set(nodes), {10, 20, 30, 40})

    def test_parse_id_accepts_int_and_float(self) -> None:
        self.assertEqual(CalculiXSolver._parse_id("7"), 7)
        self.assertEqual(CalculiXSolver._parse_id("7.0"), 7)
        self.assertIsNone(CalculiXSolver._parse_id(""))
        self.assertIsNone(CalculiXSolver._parse_id("abc"))


if __name__ == "__main__":
    unittest.main()
