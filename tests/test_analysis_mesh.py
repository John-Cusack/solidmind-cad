"""Tests for Gmsh meshing — skipped if gmsh is not installed."""
from __future__ import annotations

import unittest

try:
    import gmsh  # noqa: F401
    HAS_GMSH = True
except ImportError:
    HAS_GMSH = False

from server.analysis_mesh import _gmsh_available


class TestGmshAvailability(unittest.TestCase):
    def test_detection(self) -> None:
        self.assertEqual(_gmsh_available(), HAS_GMSH)


@unittest.skipUnless(HAS_GMSH, "gmsh not installed")
class TestMeshStepToMsh(unittest.TestCase):
    """Integration tests that require gmsh.

    These need an actual STEP file, so they are skipped in CI unless
    a test fixture is available.
    """

    def test_import_succeeds(self) -> None:
        from server.analysis_mesh import mesh_step_to_msh
        self.assertTrue(callable(mesh_step_to_msh))


if __name__ == "__main__":
    unittest.main()
