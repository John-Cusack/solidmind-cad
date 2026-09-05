"""Tests for the analytic acoustic physics and the bearing stage."""

from __future__ import annotations

import copy
import math
import tempfile
import unittest
from pathlib import Path

from server import acoustics
from server.dg_acoustic import build_mesh, commit_mesh
from server.dg_store import load_revision
from server.eval_stages import STAGES, StageError


class TestCoherence(unittest.TestCase):
    def test_snr_coherence_monotone_and_bounded(self) -> None:
        self.assertEqual(acoustics.snr_coherence2(0.0), 0.0)
        low = acoustics.snr_coherence2(0.1)
        high = acoustics.snr_coherence2(100.0)
        self.assertLess(low, high)
        self.assertLess(high, 1.0)

    def test_coherence_length_shrinks_with_turbulence_and_frequency(self) -> None:
        calm = acoustics.coherence_length_m(250.0, 300.0, 1e-8)
        rough = acoustics.coherence_length_m(250.0, 300.0, 1e-5)
        self.assertLess(rough, calm)
        high_f = acoustics.coherence_length_m(1000.0, 300.0, 1e-5)
        self.assertLess(high_f, rough)

    def test_coherence_length_in_register_band(self) -> None:
        # Assumptions register: 1-10 m at a few hundred Hz over a few hundred
        # metres, daytime convective (medium confidence).
        rho0 = acoustics.coherence_length_m(250.0, 300.0, 1e-5)
        self.assertGreater(rho0, 1.0)
        self.assertLess(rho0, 10.0)

    def test_turbulence_coherence_decays_with_separation(self) -> None:
        args = (250.0, 300.0, 1e-5)
        self.assertEqual(acoustics.turbulence_coherence2(250.0, 0.0, 300.0, 1e-5), 1.0)
        near = acoustics.turbulence_coherence2(args[0], 0.5, args[1], args[2])
        far = acoustics.turbulence_coherence2(args[0], 5.0, args[1], args[2])
        self.assertLess(far, near)
        self.assertLessEqual(far, 1.0)

    def test_no_turbulence_when_disabled(self) -> None:
        with_turb = acoustics.band_coherence2(
            250.0, snr_linear=1.0, separation_m=5.0, path_m=300.0, cn2=1e-5
        )
        without = acoustics.band_coherence2(
            250.0,
            snr_linear=1.0,
            separation_m=5.0,
            path_m=300.0,
            cn2=1e-5,
            include_turbulence=False,
        )
        self.assertLess(with_turb, without)
        self.assertAlmostEqual(without, acoustics.snr_coherence2(1.0))


class TestDelayVariance(unittest.TestCase):
    def test_variance_falls_with_integration_time(self) -> None:
        def g(_f: float) -> float:
            return 0.5

        short = acoustics.tdoa_variance_s2(
            f_lo_hz=100, f_hi_hz=400, integration_time_s=0.1, coherence2_at=g
        )
        long = acoustics.tdoa_variance_s2(
            f_lo_hz=100, f_hi_hz=400, integration_time_s=1.0, coherence2_at=g
        )
        self.assertAlmostEqual(short / long, 10.0, places=6)

    def test_variance_falls_with_coherence(self) -> None:
        lo = acoustics.tdoa_variance_s2(
            f_lo_hz=100, f_hi_hz=400, integration_time_s=0.5, coherence2_at=lambda _f: 0.2
        )
        hi = acoustics.tdoa_variance_s2(
            f_lo_hz=100, f_hi_hz=400, integration_time_s=0.5, coherence2_at=lambda _f: 0.9
        )
        self.assertLess(hi, lo)

    def test_zero_coherence_is_uninformative(self) -> None:
        var = acoustics.tdoa_variance_s2(
            f_lo_hz=100, f_hi_hz=400, integration_time_s=0.5, coherence2_at=lambda _f: 0.0
        )
        self.assertEqual(var, 1.0)  # clamped, not infinite (JCS rejects non-finite)

    def test_closed_form_agreement_for_flat_coherence(self) -> None:
        # Flat gamma^2 reduces to 3 / (8 pi^2 T (f2^3 - f1^3) w).
        f1, f2, t, g = 100.0, 400.0, 0.5, 0.5
        w = g / (1 - g)
        expected = 3.0 / (8 * math.pi**2 * t * (f2**3 - f1**3) * w)
        got = acoustics.tdoa_variance_s2(
            f_lo_hz=f1, f_hi_hz=f2, integration_time_s=t, coherence2_at=lambda _f: g
        )
        self.assertAlmostEqual(got / expected, 1.0, places=3)

    def test_bad_band_rejected(self) -> None:
        with self.assertRaises(ValueError):
            acoustics.tdoa_variance_s2(
                f_lo_hz=400, f_hi_hz=100, integration_time_s=1.0, coherence2_at=lambda _f: 0.5
            )


class TestThreshold(unittest.TestCase):
    def test_anomaly_rises_as_coherence_falls(self) -> None:
        kwargs = {"bandwidth_hz": 300.0, "integration_time_s": 0.5, "delay_spread_s": 0.02}
        strong = acoustics.anomaly_probability(coherence2=0.8, **kwargs)
        weak = acoustics.anomaly_probability(coherence2=0.01, **kwargs)
        self.assertLess(strong, 0.01)
        self.assertGreater(weak, strong)
        self.assertLessEqual(weak, 1.0)

    def test_zero_coherence_is_certain_anomaly(self) -> None:
        self.assertEqual(
            acoustics.anomaly_probability(
                bandwidth_hz=300.0, integration_time_s=0.5, coherence2=0.0, delay_spread_s=0.02
            ),
            1.0,
        )

    def test_effective_variance_blends_toward_prior(self) -> None:
        crlb, spread = 1e-12, 0.02
        prior = spread**2 / 12.0
        self.assertAlmostEqual(
            acoustics.effective_tdoa_variance(
                crlb_variance_s2=crlb, anomaly_prob=0.0, delay_spread_s=spread
            ),
            crlb,
        )
        self.assertAlmostEqual(
            acoustics.effective_tdoa_variance(
                crlb_variance_s2=crlb, anomaly_prob=1.0, delay_spread_s=spread
            ),
            prior,
        )
        mid = acoustics.effective_tdoa_variance(
            crlb_variance_s2=crlb, anomaly_prob=0.5, delay_spread_s=spread
        )
        self.assertGreater(mid, crlb)
        self.assertLess(mid, prior)


class TestGeometry(unittest.TestCase):
    def test_bearing_sigma_falls_with_aperture(self) -> None:
        small = acoustics.bearing_sigma_rad(tdoa_sigma_s=1e-5, aperture_m=0.5)
        large = acoustics.bearing_sigma_rad(tdoa_sigma_s=1e-5, aperture_m=2.0)
        self.assertAlmostEqual(small / large, 4.0, places=6)

    def test_endfire_is_unobservable(self) -> None:
        sigma = acoustics.bearing_sigma_rad(
            tdoa_sigma_s=1e-6, aperture_m=1.0, bearing_rad=math.pi / 2
        )
        self.assertAlmostEqual(sigma, math.pi / 2)

    def test_fusion_improves_with_better_bearings(self) -> None:
        nodes = [(-100.0, 0.0), (100.0, 0.0)]
        coarse, _ = acoustics.fuse_bearings(nodes, (0.0, 300.0), [0.02, 0.02])
        fine, _ = acoustics.fuse_bearings(nodes, (0.0, 300.0), [0.002, 0.002])
        self.assertAlmostEqual(coarse / fine, 10.0, places=4)

    def test_collinear_geometry_is_singular(self) -> None:
        # Target on the baseline: both bearing lines coincide, no fix.
        rms, gdop = acoustics.fuse_bearings([(-100.0, 0.0), (100.0, 0.0)], (0.0, 0.0), [0.01, 0.01])
        self.assertTrue(math.isinf(rms) or rms > 1e6)
        del gdop

    def test_third_node_improves_gdop(self) -> None:
        target = (0.0, 300.0)
        two, gdop2 = acoustics.fuse_bearings([(-100.0, 0.0), (100.0, 0.0)], target, [0.01, 0.01])
        three, gdop3 = acoustics.fuse_bearings(
            [(-100.0, 0.0), (100.0, 0.0), (0.0, 250.0)], target, [0.01, 0.01, 0.01]
        )
        self.assertLess(three, two)
        self.assertLess(gdop3, gdop2)

    def test_needs_two_nodes(self) -> None:
        with self.assertRaises(ValueError):
            acoustics.fuse_bearings([(0.0, 0.0)], (0.0, 100.0), [0.01])


class TestAcousticStage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.structure, cls.params = build_mesh()
        cls.stage = STAGES["analytic_acoustic_bearing"]

    def metrics(self, aperture: float, scenario: str = "acoustic_convective", stage=None):
        params = copy.deepcopy(self.params)
        params["array"]["aperture_m"]["value"] = aperture
        s = stage or self.stage
        return {
            k: v.value
            for k, v in s.run(s.derive_inputs(self.structure, params, scenario), 0).metrics.items()
        }

    def test_interior_optimum_under_turbulence(self) -> None:
        # The physics story: too small an aperture gives poor bearing
        # resolution; too large decorrelates and crosses the anomaly
        # threshold. The optimum is interior.
        series = {ap: self.metrics(ap)["position_rms_m"] for ap in (0.5, 1.0, 2.0, 4.0, 6.0)}
        best = min(series, key=lambda ap: series[ap])
        self.assertNotIn(best, (0.5, 6.0), f"expected an interior optimum, got {series}")

    def test_ablating_coherence_removes_the_penalty(self) -> None:
        ablated = self.stage.configure(frozenset({"crlb", "anomaly"}))
        full_series = [self.metrics(ap)["position_rms_m"] for ap in (2.0, 4.0, 6.0)]
        abl_series = [self.metrics(ap, stage=ablated)["position_rms_m"] for ap in (2.0, 4.0, 6.0)]
        # Full model degrades past the coherence wall; ablated improves forever.
        self.assertGreater(full_series[-1], full_series[0])
        self.assertLess(abl_series[-1], abl_series[0])
        # And it claims accuracy the real array cannot deliver.
        self.assertLess(abl_series[-1], full_series[-1] / 10)

    def test_anomaly_term_flags_threshold_crossing(self) -> None:
        self.assertGreater(self.metrics(6.0)["p_anomaly"], 0.5)
        self.assertLess(self.metrics(1.0)["p_anomaly"], 0.01)

    def test_calm_beats_convective(self) -> None:
        calm = self.metrics(3.0, "acoustic_calm")["position_rms_m"]
        convective = self.metrics(3.0, "acoustic_convective")["position_rms_m"]
        self.assertLess(calm, convective)

    def test_unknown_scenario_and_missing_nodes(self) -> None:
        with self.assertRaises(StageError):
            self.stage.derive_inputs(self.structure, self.params, "latch_hold")
        with self.assertRaises(ValueError):
            build_mesh(node_positions={"only": (0.0, 0.0)})

    def test_crlb_term_required(self) -> None:
        with self.assertRaises(StageError):
            self.stage.configure(frozenset({"coherence_loss"})).derive_inputs(
                self.structure, self.params, "acoustic_calm"
            )


class TestMeshRevision(unittest.TestCase):
    def test_commit_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            rev = commit_mesh(root=root)
            structure, params, manifest = load_revision(rev, root=root)
            self.assertIsNone(manifest.parent)
            self.assertEqual(sorted(c["id"] for c in structure["components"]), ["node_a", "node_b"])
            self.assertEqual(params["array"]["aperture_m"]["unit"], "m")
            # Placements live in the design graph, not the scenario.
            self.assertIn("nodes", params)
            paths = {s["path"] for s in structure["param_specs"]}
            self.assertIn("/nodes/node_a/x_m", paths)


if __name__ == "__main__":
    unittest.main()
