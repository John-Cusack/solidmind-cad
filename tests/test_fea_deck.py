"""CI-safe unit tests for the L2 FEA deck generation (no ccx/gmsh required).

These lock in the fixes that made ``run_l2_fea`` work on CalculiX 2.21:
- the deck must contain only solid C3D10 elements (gmsh also emits T3D3 line and
  CPS6 plane-stress blocks, which crash ccx's ``gen3delem``),
- numeric card values must be bounded-width (full-precision float reprs overflow
  ccx's fixed-width card fields and corrupt the parse).
"""

from __future__ import annotations

import unittest

from orchestrator.fea import _ccx_num, _extract_volume_elements

# A miniature gmsh-style Abaqus mesh: node block + three element blocks, only one
# of which (C3D10) is a solid volume element that CalculiX can use.
_MESH = """\
*NODE
1, 0.0, 0.0, 0.0
2, 1.0, 0.0, 0.0
3, 0.0, 1.0, 0.0
*ELEMENT, type=T3D3, ELSET=Line1
101, 1, 2, 3
*ELEMENT, type=CPS6, ELSET=Surface1
201, 1, 2, 3, 1, 2, 3
*ELEMENT, type=C3D10, ELSET=Volume1
301, 1, 2, 3, 1, 2, 3, 1, 2, 3, 1
302, 2, 3, 1, 2, 3, 1, 2, 3, 1, 2
"""


class TestExtractVolumeElements(unittest.TestCase):
    def test_keeps_only_c3d10(self) -> None:
        text, ids = _extract_volume_elements(_MESH)
        self.assertIn("C3D10", text)
        self.assertNotIn("T3D3", text)
        self.assertNotIn("CPS6", text)

    def test_returns_real_element_ids(self) -> None:
        _, ids = _extract_volume_elements(_MESH)
        self.assertEqual(ids, ["301", "302"])

    def test_preserves_node_block(self) -> None:
        # The node coordinates must survive — only non-C3D10 *element* data drops.
        text, _ = _extract_volume_elements(_MESH)
        self.assertIn("*NODE", text)
        for node_line in ("1, 0.0, 0.0, 0.0", "2, 1.0, 0.0, 0.0", "3, 0.0, 1.0, 0.0"):
            self.assertIn(node_line, text)

    def test_drops_non_volume_element_data(self) -> None:
        text, _ = _extract_volume_elements(_MESH)
        self.assertNotIn("101, 1, 2, 3", text)  # T3D3 data line
        self.assertNotIn("201,", text)  # CPS6 data line


class TestCcxNum(unittest.TestCase):
    def test_bounds_field_width(self) -> None:
        # Full-precision repr is 22 chars and overflows ccx's fixed-width field.
        self.assertEqual(len(repr(2700 * 1e-12)), 22)
        self.assertLessEqual(len(_ccx_num(2700 * 1e-12)), 13)

    def test_preserves_engineering_value(self) -> None:
        for raw in (2700 * 1e-12, 7850 * 1e-12, 206000.0, 0.33, -500.0):
            self.assertAlmostEqual(float(_ccx_num(raw)), raw, delta=abs(raw) * 1e-5 + 1e-15)


if __name__ == "__main__":
    unittest.main()
