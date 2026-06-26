"""TICKET-B: analytical stress-screening tier (analysis.screen_stress).

Pure-math tests for the screening core plus the MCP wrapper. No solver is ever
invoked — the whole point of the tier is to gate FEA cheaply.
"""
from __future__ import annotations

import unittest

from server.analysis_models import CheckStatus, FailureMode, ReflectExpectations
from server.screen_stress import (
    beam_bending_stress_mpa,
    euler_buckling_load_n,
    screen_stress,
    stress_concentration_factor,
)
from server.tools_analysis import analysis_screen_stress


class TestAnalyticalCore(unittest.TestCase):
    def test_cantilever_bending_stress_matches_hand_calc(self) -> None:
        # 10 mm wide x 5 mm deep cantilever, 1000 N·mm moment.
        # I = b h^3 / 12 = 10*125/12 = 104.1667 mm^4 ; c = 2.5 mm
        # sigma = M c / I = 1000*2.5/104.1667 = 24.0 MPa
        i_mm4 = 10 * 5 ** 3 / 12.0
        sigma = beam_bending_stress_mpa(1000.0, 2.5, i_mm4)
        self.assertAlmostEqual(sigma, 24.0, places=2)

    def test_scf_fillet_known_ratio(self) -> None:
        # r/d = 0.10 → table row exactly 1.85
        self.assertAlmostEqual(stress_concentration_factor("fillet", 0.10), 1.85, places=3)
        # interpolated midpoint between 0.10 (1.85) and 0.15 (1.62)
        self.assertAlmostEqual(
            stress_concentration_factor("fillet", 0.125), (1.85 + 1.62) / 2, places=3
        )

    def test_scf_sharp_corner_capped(self) -> None:
        self.assertEqual(stress_concentration_factor("fillet", 0.0), 3.0)
        self.assertEqual(stress_concentration_factor("notch", -1.0), 3.0)

    def test_scf_unknown_feature_raises(self) -> None:
        with self.assertRaises(ValueError):
            stress_concentration_factor("groove", 0.1)

    def test_euler_buckling_load(self) -> None:
        # P_cr = pi^2 E I / (K L)^2
        p = euler_buckling_load_n(70000.0, 100.0, 50.0, end_fixity=1.0)
        self.assertAlmostEqual(p, 3.14159265 ** 2 * 70000.0 * 100.0 / (50.0 ** 2), places=1)


class TestScreenStress(unittest.TestCase):
    def test_zero_fillet_underthick_fails_with_stress_concentration(self) -> None:
        # Thin section, big moment, sharp corner → must FAIL on stress conc.
        check = screen_stress(
            section={"type": "rectangle", "width_mm": 6.0, "height_mm": 2.0},
            load={"moment_nmm": 1500.0},
            yield_strength_mpa=55.0,  # PLA-ish
            stress_concentration={"feature": "fillet", "ratio": 0.0},
            target_fos=2.0,
        )
        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertEqual(check.failure_mode, FailureMode.STRESS_CONCENTRATION)

    def test_generous_section_passes(self) -> None:
        check = screen_stress(
            section={"type": "rectangle", "width_mm": 20.0, "height_mm": 12.0},
            load={"moment_nmm": 1500.0},
            yield_strength_mpa=55.0,
            stress_concentration={"feature": "fillet", "ratio": 0.30},
            target_fos=2.0,
        )
        self.assertEqual(check.status, CheckStatus.PASS)

    def test_marginal_section_warns(self) -> None:
        # Tune so 1.0 <= FoS < 2.0 → WARN (run FEA to confirm).
        check = screen_stress(
            section={"type": "rectangle", "width_mm": 10.0, "height_mm": 6.0},
            load={"moment_nmm": 3000.0},
            yield_strength_mpa=55.0,
            target_fos=2.0,
        )
        self.assertEqual(check.status, CheckStatus.WARN)
        self.assertEqual(check.failure_mode, FailureMode.YIELD)

    def test_buckling_governs_when_lower_fos(self) -> None:
        check = screen_stress(
            section={"type": "circle", "diameter_mm": 4.0},
            load={"moment_nmm": 50.0},      # trivial bending
            yield_strength_mpa=200.0,
            youngs_modulus_mpa=200000.0,
            buckling={"length_mm": 300.0, "compressive_force_n": 500.0},
            target_fos=2.0,
        )
        self.assertEqual(check.failure_mode, FailureMode.BUCKLING)
        self.assertEqual(check.status, CheckStatus.FAIL)

    def test_force_length_load_equivalent_to_moment(self) -> None:
        a = screen_stress(
            section={"type": "rectangle", "width_mm": 10.0, "height_mm": 5.0},
            load={"force_n": 20.0, "length_mm": 50.0},  # M = 1000 N·mm
            yield_strength_mpa=55.0,
        )
        b = screen_stress(
            section={"type": "rectangle", "width_mm": 10.0, "height_mm": 5.0},
            load={"moment_nmm": 1000.0},
            yield_strength_mpa=55.0,
        )
        self.assertAlmostEqual(a.measured, b.measured, places=3)

    def test_expectations_band_annotated_in_message(self) -> None:
        exp = ReflectExpectations(
            part_class="latch",
            failure_modes_to_check=(FailureMode.YIELD,),
            expected_hotspot="tooth_root",
            expected_peak_stress_mpa=(5.0, 10.0),
        )
        check = screen_stress(
            section={"type": "rectangle", "width_mm": 6.0, "height_mm": 2.0},
            load={"moment_nmm": 1500.0},
            yield_strength_mpa=55.0,
            expectations=exp,
        )
        self.assertIn("outside expected band", check.message)

    def test_invalid_section_raises(self) -> None:
        with self.assertRaises(ValueError):
            screen_stress(
                section={"type": "rectangle", "width_mm": 0.0, "height_mm": 5.0},
                load={"moment_nmm": 100.0},
                yield_strength_mpa=55.0,
            )


class TestScreenStressTool(unittest.TestCase):
    def test_tool_returns_check_dict(self) -> None:
        out = analysis_screen_stress(
            section={"type": "rectangle", "width_mm": 6.0, "height_mm": 2.0},
            load={"moment_nmm": 1500.0},
            material="pla",
            stress_concentration={"feature": "fillet", "ratio": 0.0},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["failure_mode"], "stress_concentration")

    def test_tool_unknown_material(self) -> None:
        out = analysis_screen_stress(
            section={"type": "rectangle", "width_mm": 6.0, "height_mm": 2.0},
            load={"moment_nmm": 100.0},
            material="unobtainium",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "UNKNOWN_MATERIAL")

    def test_tool_invalid_input(self) -> None:
        out = analysis_screen_stress(
            section={"type": "rectangle", "width_mm": 6.0},  # missing height
            load={"moment_nmm": 100.0},
            material="pla",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
