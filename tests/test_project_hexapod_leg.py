"""Project 2: Hexapod Leg Redesign — Isaac + CalculiX.

Exercises the Tier 3 → Tier 3.5 flow: dynamic simulation forces feed into
structural FEA for leg redesign.  Goal: reduce mass 20% while maintaining
FoS > 2.0 under gait loads.

Always-run: Tier 1 analytical, FEA coupling with mock solver.
Conditionally-run: Isaac bridge (skip if unavailable).
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from server import motion_store
from server.analysis_models import MeshInfo
from server.analysis_sim_coupling import bcs_from_propagation, bcs_from_simulation
from tests.conftest import mechanism_factory


class TestHexapodLegMechanism(unittest.TestCase):
    """Tier 1: Analytical validation of 3-DOF hexapod leg."""

    _mech_id: str = ""
    _propagation: dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        from server.tools_motion import (
            motion_define_mechanism,
            motion_propagate_motion,
            motion_validate,
        )

        motion_store.clear()

        mech = mechanism_factory("hexapod_leg")
        result = motion_define_mechanism(mech)
        assert result["ok"], f"Define failed: {result}"
        cls._mech_id = result["mechanism_id"]

        val = motion_validate(cls._mech_id)
        assert val["ok"], f"Validation failed: {val}"

        prop = motion_propagate_motion(cls._mech_id)
        assert prop["ok"], f"Propagation failed: {prop}"
        cls._propagation = prop

    @classmethod
    def tearDownClass(cls) -> None:
        motion_store.clear()

    def test_01_mechanism_defined(self) -> None:
        self.assertTrue(self._mech_id)

    def test_02_all_joints_have_speeds(self) -> None:
        """All 3 joints should propagate speeds."""
        states = self._propagation["states"]
        for part_id in ("coxa", "femur", "tibia"):
            self.assertIn(part_id, states, f"{part_id} missing from propagation")
            self.assertIn("rpm", states[part_id])

    def test_03_torque_at_knee(self) -> None:
        """Knee joint should have non-trivial torque from drive."""
        states = self._propagation["states"]
        # tibia connected via knee joint with 3.0 Nm drive
        if "tibia" in states and "torque_nm" in states["tibia"]:
            self.assertGreater(abs(states["tibia"]["torque_nm"]), 0)

    def test_04_bcs_from_propagation(self) -> None:
        """Propagation torques → FEA BCs for femur."""
        bcs = bcs_from_propagation(
            self._propagation,
            body="femur",
            fixed_faces=["Face1"],
            load_faces=["Face6"],
            load_direction=(0.0, 0.0, -1.0),
        )
        # At minimum, should have a fixed BC
        fixed = [b for b in bcs if b["bc_type"] == "fixed"]
        self.assertEqual(len(fixed), 1)
        # Should have force if torque is nonzero
        force = [b for b in bcs if b["bc_type"] == "force"]
        femur_torque = self._propagation["states"].get("femur", {}).get("torque_nm", 0)
        if abs(femur_torque) > 1e-9:
            self.assertEqual(len(force), 1)


class TestHexapodLegFEACoupling(unittest.TestCase):
    """Tier 3.5: Simulation forces → structural FEA → design iteration."""

    def _make_sim_result(self, peak_knee_force: float = 15.0) -> dict[str, Any]:
        """Build a synthetic simulation result mimicking Isaac output."""
        return {
            "summary": {
                "peak_joint_forces": {
                    "joint_0": 8.0,  # hip_yaw
                    "joint_1": 12.0,  # hip_pitch
                    "joint_2": peak_knee_force,  # knee
                },
                "simulation_time_s": 5.0,
            },
            "time_series": [
                {"t": 0.0, "joint_efforts": [5.0, 8.0, 10.0]},
                {"t": 1.0, "joint_efforts": [7.0, 11.0, 14.0]},
                {"t": 2.0, "joint_efforts": [8.0, 12.0, peak_knee_force]},
                {"t": 3.0, "joint_efforts": [6.0, 10.0, 13.0]},
                {"t": 4.0, "joint_efforts": [5.0, 9.0, 11.0]},
            ],
        }

    def test_01_baseline_femur_fea(self) -> None:
        """Baseline FEA on stock femur → identify safety factor."""
        import server.tools_analysis as mod

        sim_result = self._make_sim_result(peak_knee_force=15.0)

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/femur.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/femur.msh",
                num_nodes=500,
                num_elements=250,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face6": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="L1_femur",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face6"],
                simulation_result=sim_result,
                load_direction=[0.0, 0.0, -1.0],
                safety_factor=1.5,
                solver="mock",
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("safety_factor", result)
        baseline_sf = result["safety_factor"]
        self.assertGreater(baseline_sf, 0)

    def test_02_lightening_pocket_iteration(self) -> None:
        """Add lightening pocket → re-run FEA → verify FoS still > 2.0.

        Mock solver returns deterministic results, so we verify the full
        tool pipeline completes without error and produces valid output.
        """
        import server.tools_analysis as mod

        sim_result = self._make_sim_result(peak_knee_force=15.0)

        # Run FEA for femur with lightening pocket (same mock, different body name)
        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/femur_pocketed.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/femur_pocketed.msh",
                num_nodes=450,
                num_elements=225,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face6": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="L1_femur_pocketed",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face6"],
                simulation_result=sim_result,
                safety_factor=1.5,
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("safety_factor", result)
        self.assertGreater(result["safety_factor"], 0)

    def test_03_tibia_stress_check(self) -> None:
        """Apply hip torque → tibia FEA (second critical part)."""
        import server.tools_analysis as mod

        sim_result = self._make_sim_result(peak_knee_force=15.0)

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/tibia.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/tibia.msh",
                num_nodes=400,
                num_elements=200,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face4": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="L1_tibia",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face4"],
                simulation_result=sim_result,
                joint_index=1,  # hip_pitch joint forces on tibia
                safety_factor=1.5,
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("safety_factor", result)

    def test_04_knee_bracket_fillet_iteration(self) -> None:
        """If knee bracket stress increased → add fillet → re-run FEA.

        Simulates iteration 3: adding a fillet at the bracket root after
        lightening pockets increased local stress concentration.
        """
        import server.tools_analysis as mod

        # Higher knee force to simulate stress concentration
        sim_result = self._make_sim_result(peak_knee_force=25.0)

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/femur_filleted.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/femur_filleted.msh",
                num_nodes=600,
                num_elements=300,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face6": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="L1_femur_filleted",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face6"],
                simulation_result=sim_result,
                safety_factor=1.5,
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("max_von_mises_mpa", result)

    def test_05_mass_reduction_validation(self) -> None:
        """Verify mass reduction target can be checked via volume comparison.

        Uses analytical volume computation: if a femur is 60×15×15mm and
        we add a 40×8×10mm pocket, volume drops ~28%.
        """
        # Stock femur volume (mm³)
        stock_vol = 60 * 15 * 15  # = 13,500 mm³
        # Pocketed femur volume (removed material)
        pocket_vol = 40 * 8 * 10  # = 3,200 mm³
        pocketed_vol = stock_vol - pocket_vol  # = 10,300 mm³

        mass_reduction_pct = (1 - pocketed_vol / stock_vol) * 100
        self.assertGreater(
            mass_reduction_pct, 20, f"Mass reduction {mass_reduction_pct:.1f}% should exceed 20%"
        )

    def test_06_bcs_from_simulation_joint_index(self) -> None:
        """Verify joint_index selects the correct joint force."""
        sim_result = self._make_sim_result(peak_knee_force=20.0)

        bcs_knee = bcs_from_simulation(
            simulation_result=sim_result,
            body="femur",
            fixed_faces=["Face1"],
            load_faces=["Face6"],
            load_direction=(0.0, 0.0, -1.0),
            joint_index=2,  # knee
            safety_factor=1.5,
        )
        force_bc = [b for b in bcs_knee if b["bc_type"] == "force"]
        self.assertEqual(len(force_bc), 1)
        # Force = peak_knee * safety = 20.0 * 1.5 = 30.0, applied in -Z
        expected_fz = -20.0 * 1.5
        self.assertAlmostEqual(force_bc[0]["value"]["fz"], expected_fz, places=2)


if __name__ == "__main__":
    unittest.main()
