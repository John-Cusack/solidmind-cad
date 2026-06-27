"""Tests for cross-category coupling: simulation → FEA boundary conditions."""
from __future__ import annotations

import unittest

from server.analysis_sim_coupling import (
    bcs_from_propagation,
    bcs_from_simulation,
    summarize_sim_forces,
)


class TestBcsFromPropagation(unittest.TestCase):
    """Test BC generation from motion.propagate_motion results."""

    def _make_propagation_result(self, torque_nm: float = 5.0) -> dict:
        return {
            "ok": True,
            "states": {
                "gear_1": {"rpm": 100, "torque_nm": torque_nm, "power_w": 52.36},
                "gear_2": {"rpm": 50, "torque_nm": torque_nm * 2, "power_w": 52.36},
            },
            "efficiency": 1.0,
        }

    def test_basic_bcs(self) -> None:
        result = self._make_propagation_result(torque_nm=5.0)
        bcs = bcs_from_propagation(
            propagation_result=result,
            body="gear_1",
            fixed_faces=["Face1"],
            load_faces=["Face5"],
        )
        self.assertEqual(len(bcs), 2)
        self.assertEqual(bcs[0]["bc_type"], "fixed")
        self.assertEqual(bcs[0]["faces"], ["Face1"])
        self.assertEqual(bcs[1]["bc_type"], "force")
        self.assertEqual(bcs[1]["faces"], ["Face5"])
        # Force should be non-zero
        fz = bcs[1]["value"]["fz"]
        self.assertNotEqual(fz, 0.0)

    def test_no_torque_no_load(self) -> None:
        result = self._make_propagation_result(torque_nm=0.0)
        bcs = bcs_from_propagation(
            propagation_result=result,
            body="gear_1",
            fixed_faces=["Face1"],
            load_faces=["Face5"],
        )
        # Should only have fixed BC, no load since torque is 0
        self.assertEqual(len(bcs), 1)
        self.assertEqual(bcs[0]["bc_type"], "fixed")

    def test_custom_direction(self) -> None:
        result = self._make_propagation_result(torque_nm=10.0)
        bcs = bcs_from_propagation(
            propagation_result=result,
            body="gear_1",
            fixed_faces=["Face1"],
            load_faces=["Face3"],
            load_direction=(1.0, 0.0, 0.0),
        )
        force_bc = bcs[1]
        self.assertNotEqual(force_bc["value"]["fx"], 0.0)
        self.assertAlmostEqual(force_bc["value"]["fy"], 0.0)
        self.assertAlmostEqual(force_bc["value"]["fz"], 0.0)


class TestBcsFromSimulation(unittest.TestCase):
    """Test BC generation from motion.simulate results."""

    def _make_sim_result(
        self,
        *,
        with_efforts: bool = True,
        with_peak_forces: bool = True,
    ) -> dict:
        time_series = [
            {"t": 0.0, "parts": {"leg": {"omega_rpm": 0}}, "joint_efforts": [0.0, 0.0, 0.0]},
            {"t": 0.5, "parts": {"leg": {"omega_rpm": 50}}, "joint_efforts": [20.0, 35.0, 15.0]},
            {"t": 1.0, "parts": {"leg": {"omega_rpm": 100}}, "joint_efforts": [45.0, 30.0, 10.0]},
        ]
        if not with_efforts:
            for entry in time_series:
                del entry["joint_efforts"]

        summary: dict = {
            "simulation_time_s": 1.0,
            "steady_state_speeds": {"leg": 100},
            "engine_mode": "isaac_urdf",
        }
        if with_peak_forces:
            summary["peak_joint_forces"] = {
                "joint_0": 45.0,
                "joint_1": 35.0,
                "joint_2": 15.0,
            }

        return {
            "ok": True,
            "time_series": time_series,
            "summary": summary,
            "backend_used": "isaac",
        }

    def test_from_peak_forces(self) -> None:
        sim = self._make_sim_result()
        bcs = bcs_from_simulation(
            simulation_result=sim,
            body="leg_bracket",
            fixed_faces=["Face1"],
            load_faces=["Face6"],
            safety_factor=1.5,
        )
        self.assertEqual(len(bcs), 2)
        self.assertEqual(bcs[0]["bc_type"], "fixed")
        force_bc = bcs[1]
        self.assertEqual(force_bc["bc_type"], "force")
        # Max peak force is 45N * 1.5 safety = 67.5N
        fz = force_bc["value"]["fz"]
        self.assertAlmostEqual(abs(fz), 67.5, places=1)

    def test_specific_joint_index(self) -> None:
        sim = self._make_sim_result()
        bcs = bcs_from_simulation(
            simulation_result=sim,
            body="leg",
            fixed_faces=["Face1"],
            load_faces=["Face3"],
            joint_index=1,
            safety_factor=1.0,
        )
        force_bc = bcs[1]
        # Joint 1 peak = 35N, safety_factor=1.0
        fz = force_bc["value"]["fz"]
        self.assertAlmostEqual(abs(fz), 35.0, places=1)

    def test_fallback_to_time_series(self) -> None:
        sim = self._make_sim_result(with_peak_forces=False)
        bcs = bcs_from_simulation(
            simulation_result=sim,
            body="leg",
            fixed_faces=["Face1"],
            load_faces=["Face3"],
            safety_factor=1.0,
        )
        self.assertEqual(len(bcs), 2)
        # Should extract max effort from time series (45.0 from joint_0 at t=1.0)
        force_bc = bcs[1]
        fz = force_bc["value"]["fz"]
        self.assertAlmostEqual(abs(fz), 45.0, places=1)

    def test_no_effort_data(self) -> None:
        sim = self._make_sim_result(with_efforts=False, with_peak_forces=False)
        bcs = bcs_from_simulation(
            simulation_result=sim,
            body="leg",
            fixed_faces=["Face1"],
            load_faces=["Face3"],
        )
        # Only fixed BC — no force data available
        self.assertEqual(len(bcs), 1)
        self.assertEqual(bcs[0]["bc_type"], "fixed")


class TestSummarizeSimForces(unittest.TestCase):
    def test_with_peak_forces(self) -> None:
        sim = {
            "summary": {
                "peak_joint_forces": {"joint_0": 45.0, "joint_1": 35.0},
                "engine_mode": "isaac_urdf",
            },
            "time_series": [],
            "backend_used": "isaac",
        }
        summary = summarize_sim_forces(sim)
        self.assertEqual(summary["backend"], "isaac")
        self.assertTrue(summary["has_joint_efforts"])
        self.assertEqual(summary["peak_joint_forces"]["joint_0"], 45.0)

    def test_from_time_series(self) -> None:
        sim = {
            "summary": {},
            "time_series": [
                {"t": 0.0, "joint_efforts": [10.0, 20.0]},
                {"t": 0.5, "joint_efforts": [30.0, 15.0]},
            ],
            "backend_used": "gazebo",
        }
        summary = summarize_sim_forces(sim)
        self.assertTrue(summary["has_joint_efforts"])
        self.assertEqual(summary["peak_joint_forces"]["joint_0"], 30.0)
        self.assertEqual(summary["peak_joint_forces"]["joint_1"], 20.0)

    def test_propagation_result(self) -> None:
        sim = {
            "states": {
                "gear_1": {"torque_nm": 5.0},
                "gear_2": {"torque_nm": 10.0},
            },
            "summary": {},
            "time_series": [],
            "backend_used": "unknown",
        }
        summary = summarize_sim_forces(sim)
        self.assertTrue(summary["has_analytical_torques"])
        self.assertEqual(summary["part_torques_nm"]["gear_1"], 5.0)


class TestStressFromSimulationTool(unittest.TestCase):
    """Test the analysis.stress_from_simulation MCP tool."""

    def test_no_sim_data(self) -> None:
        from server.tools_analysis import analysis_stress_from_simulation

        result = analysis_stress_from_simulation(
            body="Body",
            material="steel",
            fixed_faces=["Face1"],
            load_faces=["Face5"],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_SIM_DATA")

    def test_with_propagation_no_forces(self) -> None:
        from server.tools_analysis import analysis_stress_from_simulation

        result = analysis_stress_from_simulation(
            body="Body",
            material="steel",
            fixed_faces=["Face1"],
            load_faces=["Face5"],
            propagation_result={
                "ok": True,
                "states": {"Body": {"rpm": 100, "torque_nm": 0, "power_w": 0}},
            },
        )
        self.assertFalse(result["ok"])
        # No torque → no load BC
        self.assertIn(result["error"]["code"], ("NO_FORCES", "NO_LOAD"))

    def test_with_propagation_has_forces(self) -> None:
        from unittest.mock import patch

        import server.tools_analysis as mod
        from server.analysis_models import MeshInfo
        from server.tools_analysis import analysis_stress_from_simulation

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/test.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/test.msh",
                num_nodes=100,
                num_elements=50,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face5": 2},
            )

            result = analysis_stress_from_simulation(
                body="gear_1",
                material="steel",
                fixed_faces=["Face1"],
                load_faces=["Face5"],
                propagation_result={
                    "ok": True,
                    "states": {
                        "gear_1": {"rpm": 100, "torque_nm": 5.0, "power_w": 52.36},
                    },
                },
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("force_source", result)
        self.assertIn("derived_boundary_conditions", result)
        self.assertGreater(result["safety_factor"], 0)


if __name__ == "__main__":
    unittest.main()
