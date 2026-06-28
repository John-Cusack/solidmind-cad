"""Tests for the foam-dart launcher's structural FEA rung.

The CI-safe units (latch profile geometry, face selection, screen-vs-FEA
classification, and the smoke guard) always run. The real CalculiX path is
guarded behind ``freecad_ready() + ccx + gmsh`` and skips in CI.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "foam_dart_spring_launcher"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

import run as launcher_run  # noqa: E402

from orchestrator.worker_builds import common  # noqa: E402
from orchestrator.worker_builds.foam_dart_launcher import latch_profile  # noqa: E402


def _solvers_ready() -> bool:
    return bool(
        common.freecad_ready()
        and shutil.which("ccx")
        and importlib.util.find_spec("gmsh") is not None
    )


class TestLatchProfile(unittest.TestCase):
    """The pure profile generator closes the wire and places the fillet."""

    def test_v1_sharp_root_is_six_lines(self) -> None:
        els, cons = latch_profile(root_mm=1.0, fillet_mm=0.0, tooth_len_mm=2.5)
        self.assertEqual([e["type"] for e in els], ["line"] * 6)
        self.assertEqual(len(cons), 6)
        self.assertTrue(all(c["type"] == "Coincident" for c in cons))

    def test_v2_fillet_inserts_one_arc(self) -> None:
        els, cons = latch_profile(root_mm=2.2, fillet_mm=0.66, tooth_len_mm=2.5)
        types = [e["type"] for e in els]
        self.assertEqual(types.count("arc"), 1)
        self.assertEqual(len(els), 7)
        self.assertEqual(len(cons), 7)

    def test_arc_tangent_points_match_root(self) -> None:
        # Arc center sits r inside the corner; its tangent points lie on the
        # column face (x=w) and the tooth underside (y=h-root).
        r = 0.66
        els, _ = latch_profile(
            root_mm=2.2, fillet_mm=r, tooth_len_mm=2.5, base_w_mm=8.0, base_h_mm=12.0
        )
        arc = next(e for e in els if e["type"] == "arc")
        self.assertAlmostEqual(arc["cx"], 8.0 + r)
        self.assertAlmostEqual(arc["cy"], 12.0 - 2.2 - r)
        self.assertAlmostEqual(arc["r"], r)

    def test_oversized_fillet_falls_back_to_sharp(self) -> None:
        # A fillet larger than the root can't fit — degrade to the sharp profile.
        els, _ = latch_profile(root_mm=1.0, fillet_mm=5.0, tooth_len_mm=2.5)
        self.assertNotIn("arc", [e["type"] for e in els])


class TestFaceSelection(unittest.TestCase):
    """Deterministic fixed/load face picks from a get_body_topology face list."""

    def test_picks_min_y_fixed_and_max_x_load(self) -> None:
        faces = [
            {"name": "Face1", "center": [4.0, 0.0, 3.0]},  # foot (min y) -> fixed
            {"name": "Face2", "center": [10.5, 11.0, 3.0]},  # tooth tip (max x) -> load
            {"name": "Face3", "center": [0.0, 6.0, 3.0]},
        ]
        fixed, load = launcher_run.select_latch_faces(faces)
        self.assertEqual(fixed, "Face1")
        self.assertEqual(load, "Face2")

    def test_empty_topology_returns_none(self) -> None:
        self.assertEqual(launcher_run.select_latch_faces([]), (None, None))


class TestScreenVsFea(unittest.TestCase):
    def test_within_tolerance(self) -> None:
        within, rel = launcher_run.screen_vs_fea(60.0, 68.0)  # ~12% gap
        self.assertTrue(within)
        self.assertLess(rel, launcher_run.FEA_SCREEN_TOL)

    def test_outside_tolerance_flagged(self) -> None:
        within, rel = launcher_run.screen_vs_fea(20.0, 60.0)  # ~67% gap
        self.assertFalse(within)

    def test_missing_fea_is_none(self) -> None:
        self.assertEqual(launcher_run.screen_vs_fea(60.0, None), (None, None))


class TestFeaConvergence(unittest.TestCase):
    """Mesh-refinement trend: small change => converged; large => singular."""

    def test_stable_peak_converges(self) -> None:
        converged, rel = launcher_run.fea_convergence(6.2, 6.6)  # ~6% change
        self.assertTrue(converged)
        self.assertLess(rel, launcher_run.FEA_CONVERGENCE_TOL)

    def test_climbing_peak_diverges(self) -> None:
        converged, rel = launcher_run.fea_convergence(20.2, 23.9)  # ~16% change
        self.assertFalse(converged)
        self.assertGreater(rel, launcher_run.FEA_CONVERGENCE_TOL)

    def test_missing_fine_is_none(self) -> None:
        self.assertEqual(launcher_run.fea_convergence(6.2, None), (None, None))


class TestFeaSmokeGuard(unittest.TestCase):
    """Smoke mode must not emit any real FEA numbers — only SKIPPED/banner."""

    def test_smoke_report_has_no_fea_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            rc = launcher_run.run(["--out", str(out), "--smoke"])
            self.assertEqual(rc, 0)
            report = (out / "validation_report.md").read_text()
        self.assertIn("PHYSICS NOT VALIDATED", report)
        self.assertIn("FEA SKIPPED", report)
        # The convergence section must degrade to SKIPPED — no solved σ table,
        # no convergence verdict leaked from a real solve.
        self.assertIn("_FEA SKIPPED_", report)
        self.assertNotIn("Δ on refine", report)
        self.assertNotIn("converges — confirms screen", report)
        self.assertNotIn("DIVERGING", report)


@unittest.skipUnless(_solvers_ready(), "needs FreeCAD addon + ccx + gmsh")
class TestFeaReal(unittest.TestCase):
    """Real CalculiX mesh-convergence study on the enriched latch.

    The filleted root (V2) must converge and confirm the analytical screen; the
    sharp root (V1) must diverge (a singularity FEA cannot resolve).
    """

    def test_v2_converges_and_confirms_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            (out / "step").mkdir(parents=True)
            (out / "stl").mkdir(parents=True)
            log = launcher_run.StepLog(lines=[])
            variants = launcher_run.build_latch_variants(out, log)
            self.assertIsNotNone(variants, "latch variants should build with the addon up")
            v2 = launcher_run.fea_latch(
                variant=variants["v2"],
                material="pla",
                hold_force_n=9.0,
                log=log,
                tag="V2 test",
            )
            self.assertIsNotNone(v2, "FEA should return a convergence result")
            # Converges at the fillet, and the converged value matches the screen.
            self.assertTrue(v2["converged"], f"V2 should converge, Δ={v2['convergence_delta']}")
            self.assertGreater(v2["peak_fine_mpa"], 0.0)
            mat = launcher_run.resolve_material("pla")
            v2_screen = launcher_run.screen_parts(
                hold_force_n=9.0,
                yield_mpa=mat.yield_strength_mpa,
                youngs_mpa=mat.youngs_modulus_mpa,
                latch_root_mm=launcher_run.LATCH_V2["root_mm"],
                latch_fillet_ratio=launcher_run.LATCH_V2["fillet_ratio"],
            )["latch_sear"].measured
            within, _ = launcher_run.screen_vs_fea(v2_screen, v2["peak_fine_mpa"])
            self.assertTrue(within, "converged V2 FEA should agree with the screen ±25%")

    def test_v1_diverges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            (out / "step").mkdir(parents=True)
            (out / "stl").mkdir(parents=True)
            log = launcher_run.StepLog(lines=[])
            variants = launcher_run.build_latch_variants(out, log)
            self.assertIsNotNone(variants)
            v1 = launcher_run.fea_latch(
                variant=variants["v1"],
                material="pla",
                hold_force_n=9.0,
                log=log,
                tag="V1 test",
            )
            self.assertIsNotNone(v1)
            # Sharp root: peak climbs under refinement → not converged (singular).
            self.assertFalse(v1["converged"], f"V1 should diverge, Δ={v1['convergence_delta']}")
            self.assertGreater(v1["peak_fine_mpa"], v1["peak_coarse_mpa"])


if __name__ == "__main__":
    unittest.main()
