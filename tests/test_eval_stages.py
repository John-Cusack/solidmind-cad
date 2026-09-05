"""Tests for the analytic latch stage — golden parity with run.py's screen."""

from __future__ import annotations

import json
import unittest

from server import jcs, json_pointer
from server.dg_binding import Binding, apply_bindings
from server.dg_import import import_brief
from server.eval_stages import STAGES, AnalyticLatchStage, StageError, Tier
from server.paths import repo_root
from server.screen_stress import screen_stress

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"

# run.py constants, pinned here for the parity test (run.py itself stays untouched).
LATCH_TOOTH_WIDTH_MM = 6.0
LATCH_TOOTH_LEN_MM = 2.5
LATCH_V1_ROOT_MM = 1.0
LATCH_IMPACT_FACTOR = 2.0
SPRING_K_N_PER_M = 300.0
MAX_COMPRESSION_M = 0.030
PLA_YIELD_MPA = 60.0


class StageBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        brief = json.loads(FIXTURE.read_text())
        cls.structure, cls.params = import_brief(brief)
        cls.stage = STAGES["analytic_latch"]

    def metrics_for(self, scenario: str, params: dict | None = None) -> dict[str, float]:
        inputs = self.stage.derive_inputs(self.structure, params or self.params, scenario)
        out = self.stage.run(inputs, seed=0)
        return {k: v.value for k, v in out.metrics.items()}


class TestGoldenParity(StageBase):
    def test_latch_hold_golden_chain(self) -> None:
        # The committed golden report: sigma_nom=22.5 MPa, Kt=3.00, peak=67.5,
        # FoS=0.89 — the static hold load (k * x_max = 9 N).
        m = self.metrics_for("latch_hold")
        self.assertAlmostEqual(m["sigma_nom_mpa"], 22.5)
        self.assertAlmostEqual(m["kt"], 3.0)
        self.assertAlmostEqual(m["peak_stress_mpa"], 67.5)
        self.assertAlmostEqual(m["latch_fos"], PLA_YIELD_MPA / 67.5)
        self.assertEqual(m["screen_status_code"], 0.0)  # FAIL

    def test_latch_catch_amplifies_by_impact_factor(self) -> None:
        hold = self.metrics_for("latch_hold")
        catch = self.metrics_for("latch_catch")
        self.assertAlmostEqual(
            catch["peak_stress_mpa"], hold["peak_stress_mpa"] * LATCH_IMPACT_FACTOR
        )

    def test_parity_with_run_py_screen_call(self) -> None:
        # Exactly the screen_parts latch call from run.py, built from its own
        # constants — the stage must produce an identical AnalysisCheck.
        hold_force_n = SPRING_K_N_PER_M * MAX_COMPRESSION_M
        expected = screen_stress(
            name="latch tooth_root",
            section={
                "type": "rectangle",
                "width_mm": LATCH_TOOTH_WIDTH_MM,
                "height_mm": LATCH_V1_ROOT_MM,
            },
            load={
                "force_n": hold_force_n * LATCH_IMPACT_FACTOR,
                "length_mm": LATCH_TOOTH_LEN_MM,
            },
            yield_strength_mpa=PLA_YIELD_MPA,
            stress_concentration={"feature": "fillet", "ratio": 0.0},
            target_fos=2.0,
        )
        inputs = self.stage.derive_inputs(self.structure, self.params, "latch_catch")
        out = self.stage.run(inputs, seed=0)
        self.assertEqual(out.checks[0], expected.to_dict())


class TestStageBehavior(StageBase):
    def test_fos_monotone_in_fillet(self) -> None:
        fos_values = []
        for fillet in (0.0, 0.1, 0.2, 0.3, 0.5):
            bound = apply_bindings(
                self.params, [Binding("/parts/latch_sear/specs/fillet_mm", fillet)]
            )
            fos_values.append(self.metrics_for("latch_hold", bound)["latch_fos"])
        self.assertEqual(fos_values, sorted(fos_values))
        self.assertLess(fos_values[0], 1.0)
        self.assertGreater(fos_values[-1], 2.0)

    def test_inputs_come_from_docs_not_constants(self) -> None:
        # Perturb the revision's tooth width; derived inputs must follow.
        bound = apply_bindings(
            self.params, [Binding("/parts/latch_sear/specs/tooth_width_mm", 12.0)]
        )
        inputs = self.stage.derive_inputs(self.structure, bound, "latch_hold")
        self.assertEqual(inputs["section"]["width_mm"], 12.0)
        # Doubling the width halves the stress relative to baseline.
        base = self.metrics_for("latch_hold")
        wide = self.metrics_for("latch_hold", bound)
        self.assertAlmostEqual(wide["peak_stress_mpa"], base["peak_stress_mpa"] / 2.0)

    def test_unknown_scenario_raises(self) -> None:
        with self.assertRaises(StageError):
            self.stage.derive_inputs(self.structure, self.params, "underwater")

    def test_missing_parameter_raises(self) -> None:
        params = json.loads(json.dumps(self.params))
        del params["parts"]["latch_sear"]["specs"]["root_mm"]
        with self.assertRaises(StageError):
            self.stage.derive_inputs(self.structure, params, "latch_hold")

    def test_outputs_are_jcs_safe(self) -> None:
        inputs = self.stage.derive_inputs(self.structure, self.params, "latch_hold")
        out = self.stage.run(inputs, seed=0)
        jcs.canonicalize(inputs)
        jcs.canonicalize({k: v.to_dict() for k, v in out.metrics.items()})
        jcs.canonicalize(out.checks)

    def test_registry_and_identity(self) -> None:
        self.assertIsInstance(self.stage, AnalyticLatchStage)
        self.assertEqual(self.stage.tier, Tier.ANALYTIC)
        self.assertEqual(self.stage.identity, "analytic_latch/v1")

    def test_derive_inputs_reads_impact_factor_from_doc(self) -> None:
        leaf = json_pointer.get(self.params, "/scenario_defaults/latch_impact_factor")
        self.assertEqual(leaf["value"], LATCH_IMPACT_FACTOR)


if __name__ == "__main__":
    unittest.main()
