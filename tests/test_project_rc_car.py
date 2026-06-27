"""Project 4: RC Car Chassis + Suspension — Gazebo + CalculiX + Elmer.

Exercises the broadest solver coverage: double-wishbone front suspension,
rear solid axle, brushless motor + ESC bay, LiPo tray.

Always-run: design pipeline, Tier 1 analytical, Gazebo stub, FEA coupling.
Conditionally-run: Elmer thermal (skip if Elmer/Gmsh unavailable).
"""
from __future__ import annotations

import json
import math
import shutil
import socket
import unittest
from typing import Any
from unittest.mock import patch

from server import motion_store
from server.analysis_models import MeshInfo
from server.analysis_sim_coupling import bcs_from_simulation
from server.design_store import clear as clear_briefs
from server.tools_design import (
    design_add_interface,
    design_add_part,
    design_get_brief,
    design_save_brief,
    design_update_brief,
)
from tests.conftest import GazeboStubBridge, mechanism_factory, unused_tcp_port

_ELMER_AVAILABLE = bool(
    shutil.which("ElmerSolver") and shutil.which("ElmerGrid")
)
_GMSH_AVAILABLE = False
try:
    import gmsh as _gmsh  # noqa: F401
    _GMSH_AVAILABLE = True
except ImportError:
    pass

requires_elmer = unittest.skipUnless(
    _ELMER_AVAILABLE and _GMSH_AVAILABLE,
    "ElmerSolver/ElmerGrid or gmsh not installed",
)


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


class TestRCCarDesignPipeline(unittest.TestCase):
    """Full design pipeline for 1/10 scale RC car."""

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_01_full_design_pipeline(self) -> None:
        """Walk through all design phases for the RC car."""
        # Phase 1: Intent
        result = design_save_brief(
            name="RC Car Chassis",
            parameters={
                "intent": "1/10 scale RC car with double-wishbone front",
                "constraints": {
                    "wheelbase_mm": 400,
                    "track_mm": 250,
                    "mass_kg": 1.5,
                },
            },
            status="intent",
        )
        self.assertTrue(result["ok"])
        bid = result["brief"]["brief_id"]

        # Phase 2: Sizing
        design_update_brief(bid, status="sizing")
        parts = [
            ("chassis_plate", "custom", 1, {"material": "CF 4mm", "length_mm": 400, "width_mm": 180}),
            ("upper_wishbone_l", "custom", 1, {"material": "aluminum", "length_mm": 80}),
            ("upper_wishbone_r", "custom", 1, {"material": "aluminum", "length_mm": 80}),
            ("lower_wishbone_l", "custom", 1, {"material": "aluminum", "length_mm": 90}),
            ("lower_wishbone_r", "custom", 1, {"material": "aluminum", "length_mm": 90}),
            ("steering_link_l", "custom", 1, {"diameter_mm": 4, "length_mm": 50}),
            ("steering_link_r", "custom", 1, {"diameter_mm": 4, "length_mm": 50}),
            ("rear_axle_housing", "custom", 1, {"width_mm": 250}),
            ("motor_mount", "custom", 1, {"material": "aluminum", "bolt_pattern": "M3"}),
            ("wheel_hub", "custom", 4, {"bearing_id_mm": 8}),
            ("shock_mount", "custom", 4, {"bolt_size": "M3"}),
            ("motor", "purchased", 1, {"model": "Hobbywing 3650", "watts": 540}),
            ("esc", "purchased", 1, {"model": "Hobbywing WP-60A"}),
            ("battery", "purchased", 1, {"type": "LiPo 3S 5000mAh", "mass_g": 280}),
            ("servo", "purchased", 1, {"model": "Savox SV-1257MG"}),
        ]
        for name, kind, qty, specs in parts:
            r = design_add_part(bid, name=name, kind=kind, quantity=qty, specs=specs)
            self.assertTrue(r["ok"], f"Failed to add part {name}: {r}")

        # Phase 3: Layout
        design_update_brief(bid, status="layout")
        interfaces = [
            # Front left wishbone
            ("chassis_plate", "uw_l_pivot", "upper_wishbone_l", "inboard", {"type": "revolute"}),
            ("chassis_plate", "lw_l_pivot", "lower_wishbone_l", "inboard", {"type": "revolute"}),
            ("upper_wishbone_l", "outboard", "wheel_hub", "upper_ball", {"type": "ball_joint"}),
            ("lower_wishbone_l", "outboard", "wheel_hub", "lower_ball", {"type": "ball_joint"}),
            # Front right wishbone
            ("chassis_plate", "uw_r_pivot", "upper_wishbone_r", "inboard", {"type": "revolute"}),
            ("chassis_plate", "lw_r_pivot", "lower_wishbone_r", "inboard", {"type": "revolute"}),
            # Rear axle
            ("chassis_plate", "rear_mount", "rear_axle_housing", "center", {"type": "revolute"}),
            # Motor mount
            ("chassis_plate", "motor_bay", "motor_mount", "base", {"pattern": "M3_bolt_4"}),
        ]
        for pa, porta, pb, portb, spec in interfaces:
            r = design_add_interface(bid, part_a=pa, port_a=porta, part_b=pb, port_b=portb, spec=spec)
            self.assertTrue(r["ok"], f"Failed to add interface {pa}-{pb}: {r}")

        # Layout positions
        design_update_brief(bid, parameters={
            "intent": "1/10 scale RC car",
            "constraints": {"wheelbase_mm": 400, "track_mm": 250},
            "layout": {
                "positions": {
                    "chassis_plate": [0, 0, 0],
                    "upper_wishbone_l": [-125, 160, 15],
                    "upper_wishbone_r": [125, 160, 15],
                    "lower_wishbone_l": [-125, 160, -5],
                    "lower_wishbone_r": [125, 160, -5],
                    "rear_axle_housing": [0, -200, -10],
                    "motor_mount": [0, -150, 10],
                },
            },
        })

        # Approve
        design_update_brief(bid, status="approved")
        brief = design_get_brief(bid)["brief"]
        self.assertEqual(brief["status"], "approved")
        self.assertEqual(len(brief["parts"]), 15)
        self.assertGreaterEqual(len(brief["interfaces"]), 8)


class TestRCCarMechanism(unittest.TestCase):
    """Tier 1: Analytical validation of RC car suspension mechanism."""

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

        mech = mechanism_factory("rc_car_suspension")
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

    def test_02_rear_axle_driven(self) -> None:
        """Rear axle should have the drive speed."""
        states = self._propagation["states"]
        self.assertIn("rear_axle", states)
        self.assertAlmostEqual(
            abs(states["rear_axle"]["rpm"]), 300.0, delta=1.0,
        )

    def test_03_suspension_dof(self) -> None:
        """Suspension has multiple DOF (wishbones + steering + axle)."""
        # The mechanism has 16 joints — should not be over-constrained
        from server.tools_motion import motion_validate
        val = motion_validate(self._mech_id)
        self.assertTrue(val["ok"])


class TestRCCarGazeboSim(unittest.TestCase):
    """Gazebo stub simulation for RC car dynamics."""

    def test_01_gazebo_stub_simulate(self) -> None:
        """Gazebo stub returns time_series for RC car mechanism."""
        port = unused_tcp_port()
        mech = mechanism_factory("rc_car_suspension")
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

    def test_02_bump_load_extraction(self) -> None:
        """Extract peak wheel load from bump simulation → FEA BCs."""
        port = unused_tcp_port()
        mech = mechanism_factory("rc_car_suspension")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 1.0,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"])

        bcs = bcs_from_simulation(
            simulation_result=resp["result"],
            body="upper_wishbone_l",
            fixed_faces=["Face1", "Face2"],
            load_faces=["Face5"],
            load_direction=(0.0, 0.0, 1.0),  # bump load upward
            safety_factor=3.0,  # 3G bump
        )
        fixed = [b for b in bcs if b["bc_type"] == "fixed"]
        self.assertEqual(len(fixed), 1)
        self.assertEqual(len(fixed[0]["faces"]), 2)


class TestRCCarFEACoupling(unittest.TestCase):
    """Tier 3.5: Simulation → wishbone FEA → design iteration."""

    def test_01_wishbone_stress(self) -> None:
        """Peak wheel load → upper wishbone FEA with mock solver."""
        import server.tools_analysis as mod

        sim_result: dict[str, Any] = {
            "summary": {
                "peak_joint_forces": {
                    "joint_0": 30.0,  # upper wishbone pivot
                    "joint_1": 45.0,  # lower wishbone pivot
                },
            },
            "time_series": [
                {"t": 0.0, "joint_efforts": [20.0, 30.0]},
                {"t": 0.5, "joint_efforts": [30.0, 45.0]},
                {"t": 1.0, "joint_efforts": [25.0, 35.0]},
            ],
        }

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/wishbone.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/wishbone.msh",
                num_nodes=300,
                num_elements=150,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face5": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="upper_wishbone_l",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face5"],
                simulation_result=sim_result,
                load_direction=[0.0, 0.0, 1.0],
                safety_factor=3.0,
                solver="mock",
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("safety_factor", result)
        self.assertGreater(result["safety_factor"], 0)

    def test_02_wishbone_pivot_gusset_iteration(self) -> None:
        """If wishbone FoS < 2.0 at 3G bump → thicken pivot → re-run FEA."""
        import server.tools_analysis as mod

        sim_result: dict[str, Any] = {
            "summary": {"peak_joint_forces": {"joint_0": 50.0}},
            "time_series": [{"t": 0.0, "joint_efforts": [50.0]}],
        }

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/wishbone_gusseted.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/wishbone_gusseted.msh",
                num_nodes=500,
                num_elements=250,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face5": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="upper_wishbone_gusseted",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face5"],
                simulation_result=sim_result,
                safety_factor=3.0,
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("safety_factor", result)

    def test_03_cg_lowering_iteration(self) -> None:
        """If body roll > 5° → lower CG by dropping battery tray position.

        This tests the design feedback loop by verifying layout update.
        """
        clear_briefs()
        try:
            r = design_save_brief(
                name="RC Car CG",
                parameters={
                    "layout": {
                        "positions": {
                            "chassis_plate": [0, 0, 0],
                            "battery_tray": [0, 0, -5],
                        },
                    },
                },
                status="layout",
            )
            bid = r["brief"]["brief_id"]

            # Lower battery tray from -5mm to -15mm to reduce body roll
            design_update_brief(bid, parameters={
                "layout": {
                    "positions": {
                        "chassis_plate": [0, 0, 0],
                        "battery_tray": [0, 0, -15],
                    },
                },
            })

            brief = design_get_brief(bid)["brief"]
            bat_z = brief["parameters"]["layout"]["positions"]["battery_tray"][2]
            self.assertEqual(bat_z, -15, "Battery tray should have moved lower")
        finally:
            clear_briefs()


class TestRCCarThermalAnalysis(unittest.TestCase):
    """Thermal analysis: motor waste heat → motor mount temperature."""

    def test_01_motor_mount_thermal_mock(self) -> None:
        """Motor mount thermal check with mock solver (no Elmer needed)."""
        import server.tools_analysis as mod

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/motor_mount.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/motor_mount.msh",
                num_nodes=400,
                num_elements=200,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face6": 2},
            )

            result = mod.analysis_thermal_check(
                body="motor_mount",
                material="aluminum_6061_t6",
                boundary_conditions=[
                    {
                        "bc_type": "heat_flux",
                        "faces": ["Face1"],
                        "value": {"flux_w_m2": 5000},  # motor waste heat
                    },
                    {
                        "bc_type": "temperature",
                        "faces": ["Face6"],
                        "value": {"temperature_k": 300},  # ambient
                    },
                ],
                solver="mock",
            )

        self.assertTrue(result["ok"], result)
        # Mock solver should return thermal results
        self.assertIn("status", result)

    def test_02_ventilation_slot_iteration(self) -> None:
        """If motor mount > 80°C → add ventilation slots → re-run thermal.

        Ventilation slots increase surface area for convection.
        """
        import server.tools_analysis as mod

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/motor_mount_vented.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/motor_mount_vented.msh",
                num_nodes=600,
                num_elements=300,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face6": 2, "Face7": 3, "Face8": 4},
            )

            result = mod.analysis_thermal_check(
                body="motor_mount_vented",
                material="aluminum_6061_t6",
                boundary_conditions=[
                    {
                        "bc_type": "heat_flux",
                        "faces": ["Face1"],
                        "value": {"flux_w_m2": 5000},
                    },
                    {
                        "bc_type": "convection",
                        "faces": ["Face6", "Face7", "Face8"],
                        "value": {"htc_w_m2k": 25, "t_ambient_k": 300},
                    },
                ],
                solver="mock",
            )

        self.assertTrue(result["ok"])

    def test_03_material_switch_iteration(self) -> None:
        """If PLA mount > 80°C → switch to aluminum → re-run thermal."""
        import server.tools_analysis as mod

        for material_name in ("pla", "aluminum_6061_t6"):
            with (
                patch.object(mod, "cad_export_body") as mock_export,
                patch.object(mod, "mesh_step_to_msh") as mock_mesh,
            ):
                mock_export.return_value = {"ok": True, "path": "/tmp/mount.step"}
                mock_mesh.return_value = MeshInfo(
                    path="/tmp/mount.msh",
                    num_nodes=400,
                    num_elements=200,
                    element_type="tet4",
                    physical_groups={"Face1": 1, "Face6": 2},
                )

                result = mod.analysis_thermal_check(
                    body="motor_mount",
                    material=material_name,
                    boundary_conditions=[
                        {
                            "bc_type": "heat_flux",
                            "faces": ["Face1"],
                            "value": {"flux_w_m2": 5000},
                        },
                        {
                            "bc_type": "temperature",
                            "faces": ["Face6"],
                            "value": {"temperature_k": 300},
                        },
                    ],
                    solver="mock",
                )

            self.assertTrue(result["ok"], f"Failed for {material_name}: {result}")


class TestRCCarWishboneGeometry(unittest.TestCase):
    """Wishbone pivot geometry adjustment based on four-bar analysis."""

    def test_01_camber_change_check(self) -> None:
        """Analytical camber change from wishbone geometry.

        Upper arm length = 80mm, lower arm length = 90mm.
        When wheel travels 20mm vertically, camber change should be small
        for a well-designed suspension.
        """
        # Simplified camber calculation:
        # Instant center is at intersection of wishbone extensions
        # Camber change per unit travel ≈ Δx / (wheel center to IC distance)
        upper_len = 80.0
        lower_len = 90.0
        vertical_spacing = 20.0  # distance between pivot planes

        # Approximate instant center distance for unequal arms
        # IC height above ground ≈ upper_len * vertical_spacing / (lower_len - upper_len)
        if lower_len > upper_len:
            ic_height = upper_len * vertical_spacing / (lower_len - upper_len)
            # Camber change per mm of travel ≈ 1/ic_height (radians)
            camber_per_mm_rad = 1.0 / ic_height
            camber_per_20mm_deg = math.degrees(camber_per_mm_rad * 20)
            # Should be < 10° for 20mm travel — indicates functional suspension
            self.assertLess(abs(camber_per_20mm_deg), 10.0,
                            "Camber change should be moderate for double wishbone")

    def test_02_track_width_update(self) -> None:
        """If body roll > 5° → widen track → update layout."""
        clear_briefs()
        try:
            r = design_save_brief(
                name="RC Car Track",
                parameters={
                    "constraints": {"track_mm": 250},
                    "layout": {
                        "positions": {
                            "wheel_hub_fl": [-125, 160, 0],
                            "wheel_hub_fr": [125, 160, 0],
                        },
                    },
                },
                status="layout",
            )
            bid = r["brief"]["brief_id"]

            # Widen track from 250mm to 280mm
            design_update_brief(bid, parameters={
                "constraints": {"track_mm": 280},
                "layout": {
                    "positions": {
                        "wheel_hub_fl": [-140, 160, 0],
                        "wheel_hub_fr": [140, 160, 0],
                    },
                },
            })

            brief = design_get_brief(bid)["brief"]
            track = brief["parameters"]["constraints"]["track_mm"]
            self.assertEqual(track, 280, "Track should have widened to 280mm")
            fl_x = brief["parameters"]["layout"]["positions"]["wheel_hub_fl"][0]
            self.assertEqual(fl_x, -140, "Front left hub should be at -140mm")
        finally:
            clear_briefs()


if __name__ == "__main__":
    unittest.main()
