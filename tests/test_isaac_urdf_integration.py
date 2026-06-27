"""End-to-end Isaac Sim integration tests for the URDF import pipeline.

Gated by ``SOLIDMIND_RUN_ISAAC_E2E=1`` environment variable.  Requires
the Isaac Sim runtime (``isaacsim/`` built from source).

Uses ``IsaacLifecycle`` with ``setUpClass``/``tearDownClass`` — SimulationApp
is expensive so we create it once per test class and call ``reload()`` between
individual tests to reset the World.

Run with::

    SOLIDMIND_RUN_ISAAC_E2E=1 \\
    ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh \\
    python3 -m unittest tests.test_isaac_urdf_integration -v
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

_RUN_E2E = os.environ.get("SOLIDMIND_RUN_ISAAC_E2E", "").strip() == "1"
_SKIP_REASON = "Set SOLIDMIND_RUN_ISAAC_E2E=1 to run Isaac integration tests"

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "simple_2body"
FIXTURE_URDF = str(FIXTURE_DIR / "simple_2body.urdf")


@unittest.skipUnless(_RUN_E2E, _SKIP_REASON)
class TestIsaacURDFIntegration(unittest.TestCase):
    """E2E tests: fixture URDF → Isaac import → physics → screenshot."""

    _lifecycle = None  # type: ignore[assignment]

    @classmethod
    def setUpClass(cls) -> None:
        from isaac_bridge.lifecycle import IsaacLifecycle

        cls._lifecycle = IsaacLifecycle(headless=True, port=0)
        cls._lifecycle.start(timeout=60.0)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._lifecycle is not None:
            cls._lifecycle.stop()

    def setUp(self) -> None:
        """Reset the World between tests."""
        self._lifecycle.reload()

    def test_import_simple_2body(self) -> None:
        """Import fixture URDF: prim_path not empty, >=1 joint, >=2 links."""
        result = self._lifecycle.import_urdf(FIXTURE_URDF)

        self.assertIn("prim_path", result)
        self.assertTrue(result["prim_path"], "prim_path should not be empty")
        self.assertGreaterEqual(result.get("joint_count", 0), 1)
        self.assertGreaterEqual(result.get("link_count", 0), 2)

    def test_import_and_diagnose(self) -> None:
        """Import then diagnose — verify prim tree has expected structure."""
        import_result = self._lifecycle.import_urdf(FIXTURE_URDF)
        prim_path = import_result.get("prim_path", "/")

        diag = self._lifecycle.diagnose(prim_path)
        self.assertIn("prims", diag)
        # Should have at least the root prim
        self.assertGreater(len(diag["prims"]), 0)

    def test_simulate_simple_2body(self) -> None:
        """Import + run 0.5s physics: time series returned with samples."""
        result = self._lifecycle.simulate(
            urdf_path=FIXTURE_URDF,
            duration_s=0.5,
            dt_s=0.002,
            output_interval=0.05,
        )

        self.assertIn("samples", result)
        self.assertGreater(len(result["samples"]), 0)
        # Each sample should have a time field
        for sample in result["samples"]:
            self.assertIn("time_s", sample)

    def test_reload_between_imports(self) -> None:
        """Import, reload, import again — verifies clean reset."""
        result1 = self._lifecycle.import_urdf(FIXTURE_URDF)
        self.assertTrue(result1.get("prim_path"))

        self._lifecycle.reload()

        result2 = self._lifecycle.import_urdf(FIXTURE_URDF)
        self.assertTrue(result2.get("prim_path"))

    def test_screenshot_after_import(self) -> None:
        """Import then screenshot — verify base64 PNG data returned."""
        self._lifecycle.import_urdf(FIXTURE_URDF)

        result = self._lifecycle.screenshot(width=256, height=256)

        self.assertIn("image_base64", result)
        self.assertGreater(len(result["image_base64"]), 100)


if __name__ == "__main__":
    unittest.main()
