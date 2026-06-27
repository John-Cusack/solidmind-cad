"""Sim -> FEA boundary condition generation tests.

Tests the coupling between motion simulation results and FEA boundary conditions
via analysis_sim_coupling.py.
"""
from __future__ import annotations

import json
import socket
import unittest

from server.analysis_sim_coupling import (
    bcs_from_propagation,
    bcs_from_simulation,
    summarize_sim_forces,
)
from tests.conftest import GazeboStubBridge, mechanism_factory, unused_tcp_port


def _simulate_via_bridge(port: int, mech: dict, duration: float = 1.0) -> dict:
    """Run a simulation through the real bridge and return the result."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.connect(("127.0.0.1", port))
    try:
        msg = json.dumps({
            "cmd": "simulate",
            "args": {
                "mechanism": mech,
                "duration_s": duration,
                "dt_s": 0.01,
                "output_interval": 0.1,
            },
        }) + "\n"
        sock.sendall(msg.encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        resp = json.loads(data.decode().strip())
        assert resp["ok"], f"Simulation failed: {resp}"
        return resp["result"]
    finally:
        sock.close()


class TestGazeboStubSimulateToBCs(unittest.TestCase):
    """motion_simulate(gazebo stub) -> bcs_from_simulation() -> verify BCs."""

    def test_simulate_to_bcs(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port):
            sim_result = _simulate_via_bridge(port, mech)

        bcs = bcs_from_simulation(
            simulation_result=sim_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
        )
        # Should have at least fixed + force BCs
        self.assertGreaterEqual(len(bcs), 1)

        # Check fixed BC
        fixed_bcs = [bc for bc in bcs if bc["bc_type"] == "fixed"]
        self.assertEqual(len(fixed_bcs), 1)
        self.assertEqual(fixed_bcs[0]["faces"], ["Face1"])

        # Check force BC — stub produces non-zero joint efforts
        force_bcs = [bc for bc in bcs if bc["bc_type"] == "force"]
        self.assertEqual(len(force_bcs), 1)
        self.assertEqual(force_bcs[0]["faces"], ["Face2"])
        # Force should be non-zero
        fz = force_bcs[0]["value"]["fz"]
        self.assertNotEqual(fz, 0.0)


class TestPropagationToBCs(unittest.TestCase):
    """motion_propagate_motion() -> bcs_from_propagation() -> verify BC structure."""

    def test_propagation_to_bcs(self):
        # Simulate a propagation result
        propagation_result = {
            "ok": True,
            "states": {
                "gear_a": {
                    "speed_rpm": 1000,
                    "torque_nm": 5.0,
                    "power_w": 523.6,
                },
                "gear_b": {
                    "speed_rpm": 500,
                    "torque_nm": 10.0,
                    "power_w": 523.6,
                },
            },
        }

        bcs = bcs_from_propagation(
            propagation_result=propagation_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face3"],
            load_direction=(1.0, 0.0, 0.0),
        )

        self.assertGreaterEqual(len(bcs), 2)

        fixed_bcs = [bc for bc in bcs if bc["bc_type"] == "fixed"]
        self.assertEqual(len(fixed_bcs), 1)

        force_bcs = [bc for bc in bcs if bc["bc_type"] == "force"]
        self.assertEqual(len(force_bcs), 1)
        # Force in X direction from torque_nm=5.0
        fx = force_bcs[0]["value"]["fx"]
        self.assertGreater(abs(fx), 0.0)
        # fy and fz should be ~0
        self.assertAlmostEqual(force_bcs[0]["value"]["fy"], 0.0, places=3)
        self.assertAlmostEqual(force_bcs[0]["value"]["fz"], 0.0, places=3)


class TestSafetyFactorDoublesForce(unittest.TestCase):
    """bcs_from_simulation(..., safety_factor=2.0) produces 2x the force of factor=1.0."""

    def test_safety_factor_doubles_force(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port):
            sim_result = _simulate_via_bridge(port, mech)

        bcs_1x = bcs_from_simulation(
            simulation_result=sim_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
            safety_factor=1.0,
        )
        bcs_2x = bcs_from_simulation(
            simulation_result=sim_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
            safety_factor=2.0,
        )

        force_1x = [bc for bc in bcs_1x if bc["bc_type"] == "force"]
        force_2x = [bc for bc in bcs_2x if bc["bc_type"] == "force"]
        self.assertEqual(len(force_1x), 1)
        self.assertEqual(len(force_2x), 1)

        fz_1x = abs(force_1x[0]["value"]["fz"])
        fz_2x = abs(force_2x[0]["value"]["fz"])
        self.assertGreater(fz_1x, 0.0)
        self.assertAlmostEqual(fz_2x, fz_1x * 2.0, places=3)


class TestNoEffortsGraceful(unittest.TestCase):
    """Simulation result with empty joint_efforts -> only fixed BCs returned."""

    def test_no_efforts(self):
        sim_result = {
            "time_series": [
                {"t": 0.0, "parts": {"gear_a": {"omega_rpm": 0}}},
                {"t": 1.0, "parts": {"gear_a": {"omega_rpm": 120}}},
            ],
            "summary": {
                "simulation_time_s": 1.0,
                "steady_state_speeds": {"gear_a": 120.0},
                "peak_joint_forces": {},
                "engine_mode": "stub",
            },
        }

        bcs = bcs_from_simulation(
            simulation_result=sim_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
        )

        # Should have fixed BC but no force BC (no effort data)
        fixed_bcs = [bc for bc in bcs if bc["bc_type"] == "fixed"]
        force_bcs = [bc for bc in bcs if bc["bc_type"] == "force"]
        self.assertEqual(len(fixed_bcs), 1)
        self.assertEqual(len(force_bcs), 0)


class TestForceSummaryBothPaths(unittest.TestCase):
    """summarize_sim_forces() works on both sim and propagation results."""

    def test_summary_from_simulation(self):
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port):
            sim_result = _simulate_via_bridge(port, mech)

        summary = summarize_sim_forces(sim_result)
        self.assertTrue(summary["has_joint_efforts"])
        self.assertIsInstance(summary["peak_joint_forces"], dict)
        self.assertGreater(summary["num_timesteps"], 0)

    def test_summary_from_propagation(self):
        prop_result = {
            "states": {
                "gear_a": {"torque_nm": 5.0, "speed_rpm": 1000},
            },
            "time_series": [],
            "summary": {},
        }
        summary = summarize_sim_forces(prop_result)
        self.assertTrue(summary["has_analytical_torques"])
        self.assertIn("gear_a", summary["part_torques_nm"])


if __name__ == "__main__":
    unittest.main()
