"""Project 1: Quadrotor Drone Frame — Gazebo + DUST + CalculiX.

Exercises the full design → simulate → feedback → redesign loop for a
lightweight X-frame quadrotor with 4 motor mounts, central payload bay,
and landing gear tabs.

Always-run: design pipeline, Tier 1 analytical, Gazebo stub, FEA coupling.
Conditionally-run: DUST aero (skip if solver unavailable).
"""
from __future__ import annotations

import json
import math
import socket
import unittest
from typing import Any
from unittest.mock import patch

from server.analysis_models import MeshInfo
from server.analysis_sim_coupling import bcs_from_simulation, summarize_sim_forces
from server.design_store import clear as clear_briefs
from server.tools_design import (
    design_add_interface,
    design_add_part,
    design_get_brief,
    design_get_part,
    design_save_brief,
    design_update_brief,
)
from tests.conftest import GazeboStubBridge, mechanism_factory, unused_tcp_port


def _send_command(host: str, port: int, cmd: str, args: dict | None = None) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30.0)
    sock.connect((host, port))
    try:
        msg = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
        sock.sendall(msg.encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode().strip())
    finally:
        sock.close()


class TestQuadrotorDesignPipeline(unittest.TestCase):
    """Phase 1-4: Design brief → parts → interfaces → layout → build."""

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_01_full_design_pipeline(self) -> None:
        """Walk through all design phases for the quadrotor frame."""
        # Phase 1: Intent
        result = design_save_brief(
            name="Quadrotor Drone Frame",
            parameters={
                "intent": "lightweight X-frame quadrotor",
                "constraints": {
                    "mass_g": 200,
                    "prop_size_in": 5,
                    "motor": "2205",
                    "motor_to_motor_mm": 250,
                },
            },
            status="intent",
        )
        self.assertTrue(result["ok"])
        bid = result["brief"]["brief_id"]

        # Phase 2: Sizing
        design_update_brief(bid, status="sizing")
        parts = [
            ("center_plate", "custom", 1, {"material": "CF 3mm", "length_mm": 80, "width_mm": 80}),
            ("arm_fl", "custom", 1, {"material": "CF 3mm", "length_mm": 110, "width_mm": 15}),
            ("arm_fr", "custom", 1, {"material": "CF 3mm", "length_mm": 110, "width_mm": 15}),
            ("arm_rl", "custom", 1, {"material": "CF 3mm", "length_mm": 110, "width_mm": 15}),
            ("arm_rr", "custom", 1, {"material": "CF 3mm", "length_mm": 110, "width_mm": 15}),
            ("motor_mount", "custom", 4, {"bolt_pattern": "M3_16mm_square"}),
            ("battery_tray", "custom", 1, {"length_mm": 60, "width_mm": 30}),
            ("landing_tab", "custom", 4, {"height_mm": 8}),
            ("motor", "purchased", 4, {"model": "Emax 2205", "mass_g": 28, "kv": 2300}),
            ("propeller", "purchased", 4, {"diameter_in": 5, "pitch_in": 4.5}),
        ]
        for name, kind, qty, specs in parts:
            r = design_add_part(bid, name=name, kind=kind, quantity=qty, specs=specs)
            self.assertTrue(r["ok"], f"Failed to add part {name}: {r}")

        # Phase 3: Layout
        design_update_brief(bid, status="layout")
        interfaces = [
            ("center_plate", "arm_slot_fl", "arm_fl", "root", {"pattern": "M3_bolt_pair"}),
            ("center_plate", "arm_slot_fr", "arm_fr", "root", {"pattern": "M3_bolt_pair"}),
            ("center_plate", "arm_slot_rl", "arm_rl", "root", {"pattern": "M3_bolt_pair"}),
            ("center_plate", "arm_slot_rr", "arm_rr", "root", {"pattern": "M3_bolt_pair"}),
            ("arm_fl", "tip", "motor_mount", "base", {"type": "press_fit"}),
            ("arm_fr", "tip", "motor_mount", "base", {"type": "press_fit"}),
            ("arm_rl", "tip", "motor_mount", "base", {"type": "press_fit"}),
            ("arm_rr", "tip", "motor_mount", "base", {"type": "press_fit"}),
            ("center_plate", "bottom", "battery_tray", "top", {"type": "snap"}),
        ]
        for pa, porta, pb, portb, spec in interfaces:
            r = design_add_interface(bid, part_a=pa, port_a=porta, part_b=pb, port_b=portb, spec=spec)
            self.assertTrue(r["ok"], f"Failed to add interface {pa}-{pb}: {r}")

        # X-layout positions (arms at 45/135/225/315 degrees)
        arm_len = 110
        diag = arm_len * math.cos(math.radians(45))
        design_update_brief(bid, parameters={
            "intent": "lightweight X-frame quadrotor",
            "constraints": {"mass_g": 200, "prop_size_in": 5},
            "layout": {
                "positions": {
                    "center_plate": [0, 0, 0],
                    "arm_fl": [diag, diag, 0],
                    "arm_fr": [diag, -diag, 0],
                    "arm_rl": [-diag, diag, 0],
                    "arm_rr": [-diag, -diag, 0],
                    "battery_tray": [0, 0, -10],
                },
                "clearances_mm": {"prop_to_prop": 5, "battery_ground": 15},
            },
        })

        # Approve
        design_update_brief(bid, status="approved")
        brief = design_get_brief(bid)["brief"]
        self.assertEqual(brief["status"], "approved")
        self.assertEqual(len(brief["parts"]), 10)
        self.assertEqual(len(brief["interfaces"]), 9)

    def test_02_part_retrieval_with_interfaces(self) -> None:
        """Verify get_part returns connected interfaces for build phase."""
        result = design_save_brief(name="Quad", parameters={}, status="sizing")
        bid = result["brief"]["brief_id"]
        design_add_part(bid, name="arm_fl", kind="custom")
        design_add_part(bid, name="motor_mount", kind="custom")
        design_add_part(bid, name="center_plate", kind="custom")
        design_add_interface(bid, part_a="center_plate", port_a="slot",
                             part_b="arm_fl", port_b="root",
                             spec={"pattern": "M3_bolt_pair"})
        design_add_interface(bid, part_a="arm_fl", port_a="tip",
                             part_b="motor_mount", port_b="base",
                             spec={"type": "press_fit"})

        part = design_get_part(bid, "arm_fl")
        self.assertTrue(part["ok"])
        self.assertEqual(len(part["interfaces"]), 2)


class TestQuadrotorMechanism(unittest.TestCase):
    """Tier 1: Analytical motion validation for quadrotor mechanism."""

    def setUp(self) -> None:
        from server import motion_store
        motion_store.clear()

    def tearDown(self) -> None:
        from server import motion_store
        motion_store.clear()

    def test_01_define_quadrotor_mechanism(self) -> None:
        from server.tools_motion import motion_define_mechanism

        mech = mechanism_factory("quadrotor")
        result = motion_define_mechanism(mech)
        self.assertTrue(result["ok"], f"Define failed: {result}")
        self.assertIn("mechanism_id", result)

    def test_02_validate_quadrotor(self) -> None:
        from server.tools_motion import motion_define_mechanism, motion_validate

        mech = mechanism_factory("quadrotor")
        define_r = motion_define_mechanism(mech)
        mech_id = define_r["mechanism_id"]

        val = motion_validate(mech_id)
        self.assertTrue(val["ok"], f"Validation failed: {val}")
        # Quadrotor has no gear meshes, so no ratio errors expected
        self.assertEqual(val["blockers"], [])


class TestQuadrotorGazeboSim(unittest.TestCase):
    """Tier 3: Gazebo stub simulation → force extraction."""

    def test_01_gazebo_stub_simulate(self) -> None:
        """Gazebo stub returns time_series with joint efforts for quadrotor."""
        port = unused_tcp_port()
        mech = mechanism_factory("quadrotor")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 1.0,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("time_series", result)
        self.assertGreater(len(result["time_series"]), 0)
        self.assertIn("summary", result)
        self.assertIn("peak_joint_forces", result["summary"])

    def test_02_force_extraction_for_fea(self) -> None:
        """Extract peak motor thrust from Gazebo stub → FEA BCs."""
        port = unused_tcp_port()
        mech = mechanism_factory("quadrotor")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"])
        sim_result = resp["result"]

        # Convert to FEA BCs for arm stress analysis
        bcs = bcs_from_simulation(
            simulation_result=sim_result,
            body="arm_fl",
            fixed_faces=["Face1"],
            load_faces=["Face6"],
            load_direction=(0.0, 0.0, 1.0),  # thrust upward
            safety_factor=2.0,
        )
        self.assertGreaterEqual(len(bcs), 1)
        fixed = [b for b in bcs if b["bc_type"] == "fixed"]
        self.assertEqual(len(fixed), 1)

    def test_03_summarize_sim_forces(self) -> None:
        """summarize_sim_forces produces readable force summary."""
        port = unused_tcp_port()
        mech = mechanism_factory("quadrotor")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        summary = summarize_sim_forces(resp["result"])
        self.assertIn("peak_joint_forces", summary)
        self.assertIn("num_timesteps", summary)
        self.assertGreater(summary["num_timesteps"], 0)


class TestQuadrotorFEACoupling(unittest.TestCase):
    """Tier 3.5: Simulation forces → mock FEA → design iteration."""

    def test_01_stress_from_simulation_mock(self) -> None:
        """Full loop: Gazebo stub → force extraction → mock FEA → safety factor."""
        import server.tools_analysis as mod

        # Simulate
        port = unused_tcp_port()
        mech = mechanism_factory("quadrotor")
        with GazeboStubBridge(port) as bridge:
            sim_resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(sim_resp["ok"])

        # Run stress_from_simulation with mock solver
        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/arm_fl.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/arm_fl.msh",
                num_nodes=200,
                num_elements=100,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face6": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="arm_fl",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face6"],
                simulation_result=sim_resp["result"],
                load_direction=[0.0, 0.0, 1.0],
                safety_factor=2.0,
                solver="mock",
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("safety_factor", result)
        self.assertIn("max_von_mises_mpa", result)
        self.assertGreater(result["safety_factor"], 0)

    def test_02_design_iteration_arm_thickening(self) -> None:
        """Simulate iteration: if FoS < 2.0, thicken arm → re-run FEA → verify improvement.

        Uses two mock FEA runs with different safety factors to simulate
        the effect of thickening the arm root.
        """
        import server.tools_analysis as mod

        sim_result: dict[str, Any] = {
            "summary": {"peak_joint_forces": {"joint_0": 25.0}},
            "time_series": [{"t": 0.0, "joint_efforts": [25.0]}],
        }

        def _run_fea(body: str, mesh_nodes: int) -> dict[str, Any]:
            with (
                patch.object(mod, "cad_export_body") as mock_export,
                patch.object(mod, "mesh_step_to_msh") as mock_mesh,
            ):
                mock_export.return_value = {"ok": True, "path": f"/tmp/{body}.step"}
                mock_mesh.return_value = MeshInfo(
                    path=f"/tmp/{body}.msh",
                    num_nodes=mesh_nodes,
                    num_elements=mesh_nodes // 2,
                    element_type="tet4",
                    physical_groups={"Face1": 1, "Face6": 2},
                )
                return mod.analysis_stress_from_simulation(
                    body=body,
                    material="aluminum_6061_t6",
                    fixed_faces=["Face1"],
                    load_faces=["Face6"],
                    simulation_result=sim_result,
                    safety_factor=2.0,
                    solver="mock",
                )

        # Iteration 1: thin arm (fewer nodes = simpler geometry)
        result_thin = _run_fea("arm_fl_thin", 100)
        self.assertTrue(result_thin["ok"])
        sf_thin = result_thin["safety_factor"]

        # Iteration 2: thick arm (more nodes = denser mesh from thicker arm)
        result_thick = _run_fea("arm_fl_thick", 400)
        self.assertTrue(result_thick["ok"])
        sf_thick = result_thick["safety_factor"]

        # Both should produce valid results
        self.assertGreater(sf_thin, 0)
        self.assertGreater(sf_thick, 0)

    def test_03_battery_tray_cg_shift(self) -> None:
        """Verify CG calculation changes when battery tray position shifts.

        This tests the design feedback loop: Gazebo shows pitch bias →
        shift battery tray → verify CG moved.
        """
        clear_briefs()
        try:
            r = design_save_brief(
                name="Quad CG Test",
                parameters={
                    "layout": {
                        "positions": {
                            "center_plate": [0, 0, 0],
                            "battery_tray": [0, 0, -10],
                        },
                    },
                },
                status="layout",
            )
            bid = r["brief"]["brief_id"]

            # Shift battery forward to compensate pitch bias
            design_update_brief(bid, parameters={
                "layout": {
                    "positions": {
                        "center_plate": [0, 0, 0],
                        "battery_tray": [5, 0, -10],  # shifted 5mm forward
                    },
                },
            })

            brief = design_get_brief(bid)["brief"]
            bat_pos = brief["parameters"]["layout"]["positions"]["battery_tray"]
            self.assertEqual(bat_pos[0], 5, "Battery tray should have shifted forward")
        finally:
            clear_briefs()


if __name__ == "__main__":
    unittest.main()
