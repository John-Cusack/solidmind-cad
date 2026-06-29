"""Tests for the Tier-1 analytical thermal screen (server/screen_thermal.py).

Reference cases are textbook lumped-parameter heat transfer (Incropera, Fundamentals
of Heat and Mass Transfer): convective equilibrium temperature, series resistance
networks, the Biot-number lumped-validity criterion, and the lumped time constant.
"""

from __future__ import annotations

import unittest

from server.analysis_models import CheckStatus, FailureMode
from server.screen_thermal import (
    biot_number,
    conduction_resistance_kw,
    convection_resistance_kw,
    convective_equilibrium_temp_k,
    lumped_time_constant_s,
    screen_thermal,
)
from server.tools_analysis import analysis_screen_thermal


class TestAnalyticalCore(unittest.TestCase):
    def test_convection_resistance(self) -> None:
        # R = 1 / (h A) = 1 / (25 * 0.02) = 2.0 K/W
        self.assertAlmostEqual(convection_resistance_kw(25.0, 0.02), 2.0)

    def test_conduction_resistance(self) -> None:
        # R = L / (k A) = 0.005 / (200 * 0.001) = 0.025 K/W
        self.assertAlmostEqual(conduction_resistance_kw(0.005, 0.001, 200.0), 0.025)

    def test_convective_equilibrium_temp(self) -> None:
        # T = T_inf + Q/(hA) = 293.15 + 10*2.0 = 313.15 K
        self.assertAlmostEqual(
            convective_equilibrium_temp_k(10.0, 25.0, 0.02, 293.15), 313.15
        )

    def test_biot_number_lumped_valid(self) -> None:
        # Bi = h L_c / k = 100 * 0.01 / 200 = 0.005 (< 0.1, lumped valid)
        self.assertAlmostEqual(biot_number(100.0, 0.01, 200.0), 0.005)

    def test_biot_number_lumped_invalid(self) -> None:
        # Bi = 1000 * 0.05 / 15 = 3.333 (>> 0.1, lumped breaks down)
        self.assertAlmostEqual(biot_number(1000.0, 0.05, 15.0), 1000.0 * 0.05 / 15.0)

    def test_lumped_time_constant(self) -> None:
        # tau = rho V c / (h A) = 2700 * 1e-6 * 900 / (50 * 0.01) = 4.86 s
        self.assertAlmostEqual(
            lumped_time_constant_s(2700.0, 1e-6, 900.0, 50.0, 0.01), 4.86
        )

    def test_zero_area_raises(self) -> None:
        with self.assertRaises(ValueError):
            convection_resistance_kw(25.0, 0.0)


class TestScreenThermal(unittest.TestCase):
    BASE = {"power_w": 10.0, "convection": {"coeff_w_m2k": 25.0, "area_m2": 0.02}}

    def test_overtemperature_fails(self) -> None:
        # rise = 20 K, allowable = 303.15 - 293.15 = 10 K → FoS 0.5 → FAIL
        check = screen_thermal(**self.BASE, max_temperature_k=303.15)
        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertEqual(check.failure_mode, FailureMode.THERMAL)
        self.assertAlmostEqual(check.measured, 313.15, places=2)

    def test_comfortable_margin_passes(self) -> None:
        # allowable = 80 K vs rise 20 K → FoS 4 → PASS
        check = screen_thermal(**self.BASE, max_temperature_k=373.15)
        self.assertEqual(check.status, CheckStatus.PASS)

    def test_marginal_warns(self) -> None:
        # allowable = 30 K vs rise 20 K → FoS 1.5 (< target 2.0) → WARN
        check = screen_thermal(**self.BASE, max_temperature_k=323.15)
        self.assertEqual(check.status, CheckStatus.WARN)

    def test_biot_gate_warns_even_when_cool(self) -> None:
        # Comfortable on temperature, but Bi > 0.1 → WARN (run FEA).
        check = screen_thermal(
            **self.BASE,
            max_temperature_k=373.15,
            # Bi = h L_c / k = 25 * 0.1 / 15 = 0.167 (> 0.1, lumped breaks down).
            biot={"char_length_m": 0.1, "conductivity_w_mk": 15.0},
        )
        self.assertEqual(check.status, CheckStatus.WARN)
        self.assertIn("Bi=", check.message)
        self.assertIn("FEA", check.suggestion)

    def test_biot_valid_keeps_pass(self) -> None:
        check = screen_thermal(
            **self.BASE,
            max_temperature_k=373.15,
            biot={"char_length_m": 0.01, "conductivity_w_mk": 200.0},
        )
        self.assertEqual(check.status, CheckStatus.PASS)
        self.assertIn("lumped valid", check.message)

    def test_conduction_in_series_raises_hot_spot(self) -> None:
        # Adding an internal conduction drop makes the hot spot hotter than the
        # surface; both temperatures are reported.
        check = screen_thermal(
            power_w=5.0,
            convection={"coeff_w_m2k": 50.0, "area_m2": 0.005},
            conduction={"length_m": 0.005, "area_m2": 0.001, "conductivity_w_mk": 200.0},
        )
        # R_total = 0.025 + 4.0 = 4.025 → rise = 5 * 4.025 = 20.125 K
        self.assertAlmostEqual(check.measured, 293.15 + 20.125, places=2)
        self.assertIn("T_surface=", check.message)

    def test_no_limit_never_passes(self) -> None:
        # Without a temperature limit the screen can't certify the part is cool,
        # so it must WARN (report only) — never PASS, even for a hot part.
        check = screen_thermal(power_w=500.0, convection={"coeff_w_m2k": 5.0, "area_m2": 0.001})
        self.assertEqual(check.status, CheckStatus.WARN)
        self.assertIn("report only", check.message)
        self.assertIn("max_temperature_k", check.suggestion)

    def test_transient_time_constant_in_message(self) -> None:
        check = screen_thermal(
            **self.BASE,
            transient={"density_kg_m3": 2700.0, "volume_m3": 1e-6, "specific_heat_j_kgk": 900.0},
        )
        self.assertIn("tau=", check.message)

    def test_negative_power_raises(self) -> None:
        with self.assertRaises(ValueError):
            screen_thermal(power_w=-1.0, convection={"coeff_w_m2k": 25.0, "area_m2": 0.02})

    def test_limit_below_ambient_raises(self) -> None:
        with self.assertRaises(ValueError):
            screen_thermal(
                power_w=10.0,
                convection={"coeff_w_m2k": 25.0, "area_m2": 0.02, "ambient_k": 300.0},
                max_temperature_k=273.15,
            )


class TestScreenThermalTool(unittest.TestCase):
    def test_tool_returns_check_dict(self) -> None:
        res = analysis_screen_thermal(
            power_w=10.0,
            convection={"coeff_w_m2k": 25.0, "area_m2": 0.02},
            max_temperature_k=303.15,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["failure_mode"], "thermal")

    def test_tool_inline_thermal_material_dict(self) -> None:
        # A thermal-only inline dict (no structural keys) must not crash the tool;
        # it should resolve and backfill the biot conductivity.
        res = analysis_screen_thermal(
            power_w=10.0,
            convection={"coeff_w_m2k": 25.0, "area_m2": 0.02},
            material={"thermal_conductivity_w_mk": 200.0, "density_kg_m3": 2700.0},
            biot={"char_length_m": 0.01},
        )
        self.assertTrue(res["ok"])
        self.assertIn("Bi=", res["message"])

    def test_tool_material_missing_thermal_field_clear_error(self) -> None:
        # A transient block needing specific_heat the material lacks must surface
        # a clear INVALID_INPUT, not a cryptic KeyError on the absent key.
        res = analysis_screen_thermal(
            power_w=10.0,
            convection={"coeff_w_m2k": 25.0, "area_m2": 0.02},
            material={"thermal_conductivity_w_mk": 200.0},  # no specific_heat / density
            transient={"volume_m3": 1e-6},
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "INVALID_INPUT")
        self.assertIn("positive", res["error"]["message"])

    def test_tool_material_backfills_biot_conductivity(self) -> None:
        # Biot block omits conductivity_w_mk; the material supplies it.
        res = analysis_screen_thermal(
            power_w=10.0,
            convection={"coeff_w_m2k": 25.0, "area_m2": 0.02},
            material="aluminum_6061_t6",
            biot={"char_length_m": 0.01},
        )
        self.assertTrue(res["ok"])
        self.assertIn("Bi=", res["message"])

    def test_tool_unknown_material(self) -> None:
        res = analysis_screen_thermal(
            power_w=10.0,
            convection={"coeff_w_m2k": 25.0, "area_m2": 0.02},
            material="unobtainium",
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "UNKNOWN_MATERIAL")

    def test_tool_invalid_input(self) -> None:
        res = analysis_screen_thermal(power_w=10.0, convection={"coeff_w_m2k": 25.0})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
