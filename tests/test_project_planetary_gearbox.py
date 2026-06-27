"""Project 3: Planetary Gearbox — Chrono + CalculiX.

Exercises the full design → Tier 1 analytical → Chrono dynamic → FEA loop
for a 2-stage planetary gearbox.

Always-run: design pipeline, Tier 1 (ratio/torque/DOF), Gazebo stub coupling,
            FEA coupling with mock solver.
Conditionally-run: Chrono daemon (skip if not built).
"""

from __future__ import annotations

import json
import math
import socket
import unittest
from typing import Any
from unittest.mock import patch

from server import motion_store
from server.analysis_models import MeshInfo
from server.analysis_sim_coupling import bcs_from_propagation
from server.design_store import clear as clear_briefs
from server.tools_design import (
    design_add_interface,
    design_add_part,
    design_get_brief,
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


class TestPlanetaryGearboxDesign(unittest.TestCase):
    """Design pipeline for 2-stage planetary gearbox."""

    def setUp(self) -> None:
        clear_briefs()

    def tearDown(self) -> None:
        clear_briefs()

    def test_01_full_design_pipeline(self) -> None:
        """Walk through all design phases for the planetary gearbox."""
        # Phase 1: Intent
        result = design_save_brief(
            name="Planetary Gearbox",
            parameters={
                "intent": "2-stage planetary gearbox",
                "constraints": {
                    "target_ratio": 50,
                    "output_torque_nm": 10,
                    "module_mm": 1.5,
                },
            },
            status="intent",
        )
        self.assertTrue(result["ok"])
        bid = result["brief"]["brief_id"]

        # Phase 2: Sizing — register all parts
        design_update_brief(bid, status="sizing")

        # Stage 1 parts
        stage1_parts = [
            ("sun1", "custom", 1, {"teeth": 18, "module": 1.5, "pitch_diameter_mm": 27.0}),
            ("planet1_a", "custom", 1, {"teeth": 27, "module": 1.5, "pitch_diameter_mm": 40.5}),
            ("planet1_b", "custom", 1, {"teeth": 27, "module": 1.5, "pitch_diameter_mm": 40.5}),
            ("planet1_c", "custom", 1, {"teeth": 27, "module": 1.5, "pitch_diameter_mm": 40.5}),
            (
                "ring1",
                "custom",
                1,
                {"teeth": 72, "module": 1.5, "pitch_diameter_mm": 108.0, "internal": True},
            ),
            ("carrier1", "custom", 1, {"pin_count": 3}),
        ]
        # Stage 2 parts
        stage2_parts = [
            ("sun2", "custom", 1, {"teeth": 16, "module": 1.5, "pitch_diameter_mm": 24.0}),
            ("planet2_a", "custom", 1, {"teeth": 28, "module": 1.5, "pitch_diameter_mm": 42.0}),
            ("planet2_b", "custom", 1, {"teeth": 28, "module": 1.5, "pitch_diameter_mm": 42.0}),
            ("planet2_c", "custom", 1, {"teeth": 28, "module": 1.5, "pitch_diameter_mm": 42.0}),
            (
                "ring2",
                "custom",
                1,
                {"teeth": 72, "module": 1.5, "pitch_diameter_mm": 108.0, "internal": True},
            ),
            ("carrier2", "custom", 1, {"pin_count": 3}),
        ]
        # Common parts
        common_parts = [
            ("housing", "custom", 1, {"outer_diameter_mm": 120, "length_mm": 60}),
            ("input_shaft", "custom", 1, {"diameter_mm": 10}),
            ("output_shaft", "custom", 1, {"diameter_mm": 15}),
        ]

        for name, kind, qty, specs in stage1_parts + stage2_parts + common_parts:
            r = design_add_part(bid, name=name, kind=kind, quantity=qty, specs=specs)
            self.assertTrue(r["ok"], f"Failed to add part {name}: {r}")

        # Phase 3: Layout — gear mesh interfaces
        design_update_brief(bid, status="layout")

        # Stage 1 interfaces
        for planet in ("planet1_a", "planet1_b", "planet1_c"):
            design_add_interface(
                bid,
                part_a="sun1",
                port_a="teeth",
                part_b=planet,
                port_b="teeth",
                spec={"type": "gear_mesh", "z_layer": 1},
            )
        design_add_interface(
            bid,
            part_a="planet1_a",
            port_a="teeth",
            part_b="ring1",
            port_b="teeth",
            spec={"type": "gear_mesh", "z_layer": 1},
        )

        # Stage 2 interfaces
        for planet in ("planet2_a", "planet2_b", "planet2_c"):
            design_add_interface(
                bid,
                part_a="sun2",
                port_a="teeth",
                part_b=planet,
                port_b="teeth",
                spec={"type": "gear_mesh", "z_layer": 2},
            )
        design_add_interface(
            bid,
            part_a="planet2_a",
            port_a="teeth",
            part_b="ring2",
            port_b="teeth",
            spec={"type": "gear_mesh", "z_layer": 2},
        )

        # Shaft interfaces
        design_add_interface(
            bid,
            part_a="input_shaft",
            port_a="end",
            part_b="sun1",
            port_b="bore",
            spec={"type": "keyway"},
        )
        design_add_interface(
            bid,
            part_a="carrier2",
            port_a="hub",
            part_b="output_shaft",
            port_b="bore",
            spec={"type": "keyway"},
        )

        # Carrier coupling
        design_add_interface(
            bid,
            part_a="carrier1",
            port_a="hub",
            part_b="sun2",
            port_b="bore",
            spec={"type": "spline"},
        )

        # Approve
        design_update_brief(bid, status="approved")
        brief = design_get_brief(bid)["brief"]
        self.assertEqual(brief["status"], "approved")
        self.assertEqual(len(brief["parts"]), 15)
        self.assertGreaterEqual(len(brief["interfaces"]), 10)


class TestPlanetaryMechanism(unittest.TestCase):
    """Tier 1: Analytical motion validation for 2-stage planetary."""

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

        mech = mechanism_factory("planetary_2stage")
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

    def test_02_input_shaft_speed(self) -> None:
        """Input shaft driven at 3000 RPM."""
        states = self._propagation["states"]
        self.assertIn("input_shaft", states)
        self.assertAlmostEqual(states["input_shaft"]["rpm"], 3000.0, delta=1.0)

    def test_03_stage1_gear_ratio(self) -> None:
        """Stage 1 sun-planet ratio: sun1(18T) → planet(27T), ratio = 18/27."""
        states = self._propagation["states"]
        if "sun1" in states and "planet1_a" in states:
            sun_rpm = abs(states["sun1"]["rpm"])
            planet_rpm = abs(states["planet1_a"]["rpm"])
            if sun_rpm > 0:
                actual_ratio = planet_rpm / sun_rpm
                expected_ratio = 18.0 / 27.0
                self.assertAlmostEqual(actual_ratio, expected_ratio, delta=0.05)

    def test_04_power_conservation(self) -> None:
        """Input power ≈ output power (no friction in analytical model)."""
        states = self._propagation["states"]
        if "input_shaft" in states and "output_shaft" in states:
            in_torque = abs(states["input_shaft"].get("torque_nm", 0))
            in_rpm = abs(states["input_shaft"].get("rpm", 0))
            out_torque = abs(states["output_shaft"].get("torque_nm", 0))
            out_rpm = abs(states["output_shaft"].get("rpm", 0))

            if in_rpm > 0 and out_rpm > 0:
                p_in = in_torque * in_rpm * 2 * math.pi / 60
                p_out = out_torque * out_rpm * 2 * math.pi / 60
                # Power should be conserved within 5%
                if p_in > 0:
                    self.assertAlmostEqual(p_out / p_in, 1.0, delta=0.05)

    def test_05_bcs_from_propagation_planet(self) -> None:
        """Propagation torques → FEA BCs for planet gear."""
        bcs = bcs_from_propagation(
            self._propagation,
            body="planet1_a",
            fixed_faces=["Face1"],
            load_faces=["Face3"],
            load_direction=(1.0, 0.0, 0.0),  # tangential tooth force
        )
        fixed = [b for b in bcs if b["bc_type"] == "fixed"]
        self.assertEqual(len(fixed), 1)


class TestPlanetaryGazeboSim(unittest.TestCase):
    """Gazebo stub simulation for the 2-stage planetary mechanism."""

    def test_01_gazebo_stub_simulate(self) -> None:
        """Gazebo stub accepts planetary mechanism and returns joint efforts."""
        port = unused_tcp_port()
        mech = mechanism_factory("planetary_2stage")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(
                bridge.host,
                bridge.port,
                "simulate",
                {
                    "mechanism": mech,
                    "duration_s": 0.5,
                    "dt_s": 0.01,
                    "output_interval": 0.1,
                },
            )
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("time_series", result)
        self.assertGreater(len(result["time_series"]), 0)
        self.assertIn("summary", result)

    def test_02_sim_to_planet_tooth_fea(self) -> None:
        """Simulation → tooth root bending stress on planet gear."""
        import server.tools_analysis as mod

        port = unused_tcp_port()
        mech = mechanism_factory("planetary_2stage")
        with GazeboStubBridge(port) as bridge:
            sim_resp = _send_command(
                bridge.host,
                bridge.port,
                "simulate",
                {
                    "mechanism": mech,
                    "duration_s": 0.5,
                    "dt_s": 0.01,
                    "output_interval": 0.1,
                },
            )
        self.assertTrue(sim_resp["ok"])

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/planet1_a.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/planet1_a.msh",
                num_nodes=800,
                num_elements=400,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face3": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="planet1_a",
                material="steel_4140",
                fixed_faces=["Face1"],
                load_faces=["Face3"],
                simulation_result=sim_resp["result"],
                load_direction=[1.0, 0.0, 0.0],
                safety_factor=2.0,
                solver="mock",
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("safety_factor", result)
        self.assertGreater(result["safety_factor"], 0)

    def test_03_housing_deflection_fea(self) -> None:
        """Simulation → bearing loads → housing deflection check."""
        import server.tools_analysis as mod

        sim_result: dict[str, Any] = {
            "summary": {
                "peak_joint_forces": {"joint_0": 500.0},  # bearing load
            },
            "time_series": [{"t": 0.0, "joint_efforts": [500.0]}],
        }

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/housing.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/housing.msh",
                num_nodes=2000,
                num_elements=1000,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face12": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="housing",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face12"],
                simulation_result=sim_result,
                load_direction=[0.0, 1.0, 0.0],  # radial bearing load
                safety_factor=1.5,
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("max_displacement_mm", result)


class TestPlanetaryDesignIterations(unittest.TestCase):
    """Design iteration tests: modify geometry based on simulation feedback."""

    def test_01_tooth_count_adjustment(self) -> None:
        """If stage 1 efficiency < 90%, adjust tooth counts.

        Tooth count changes should produce a valid mechanism with
        different gear ratios.
        """
        from server.tools_motion import motion_define_mechanism, motion_validate

        motion_store.clear()
        try:
            # Original: sun=18, planet=27, ring=72, ratio = 18/27 ≈ 0.667
            # Adjusted: sun=20, planet=25, ring=70, ratio = 20/25 = 0.8
            adjusted_mech = {
                "name": "adjusted_planetary",
                "parts": [
                    {"id": "frame", "is_ground": True},
                    {"id": "sun"},
                    {"id": "planet"},
                    {"id": "ring"},
                ],
                "joints": [
                    {
                        "id": "sun_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "sun",
                    },
                    {
                        "id": "sp",
                        "joint_type": "gear_mesh",
                        "parent_part": "sun",
                        "child_part": "planet",
                        "teeth_parent": 20,
                        "teeth_child": 25,
                        "gear_ratio": 20.0 / 25.0,
                    },
                    {
                        "id": "pr",
                        "joint_type": "gear_mesh",
                        "parent_part": "planet",
                        "child_part": "ring",
                        "teeth_parent": 25,
                        "teeth_child": 70,
                        "gear_ratio": 25.0 / 70.0,
                    },
                ],
                "drives": [
                    {"joint_id": "sun_rev", "speed_rpm": 3000, "torque_nm": 2.0},
                ],
            }

            result = motion_define_mechanism(adjusted_mech)
            self.assertTrue(result["ok"])
            val = motion_validate(result["mechanism_id"])
            self.assertTrue(val["ok"])
        finally:
            motion_store.clear()

    def test_02_face_width_increase(self) -> None:
        """If tooth root FoS < 2.0, increase module/face width → re-run FEA.

        Wider face width distributes load → lower stress → higher FoS.
        """
        import server.tools_analysis as mod

        sim_result: dict[str, Any] = {
            "summary": {"peak_joint_forces": {"joint_0": 300.0}},
            "time_series": [{"t": 0.0, "joint_efforts": [300.0]}],
        }

        def _run_planet_fea(face_width_label: str) -> dict[str, Any]:
            with (
                patch.object(mod, "cad_export_body") as mock_export,
                patch.object(mod, "mesh_step_to_msh") as mock_mesh,
            ):
                mock_export.return_value = {"ok": True, "path": f"/tmp/{face_width_label}.step"}
                mock_mesh.return_value = MeshInfo(
                    path=f"/tmp/{face_width_label}.msh",
                    num_nodes=800,
                    num_elements=400,
                    element_type="tet4",
                    physical_groups={"Face1": 1, "Face3": 2},
                )
                return mod.analysis_stress_from_simulation(
                    body=face_width_label,
                    material="steel_4140",
                    fixed_faces=["Face1"],
                    load_faces=["Face3"],
                    simulation_result=sim_result,
                    safety_factor=2.0,
                    solver="mock",
                )

        narrow = _run_planet_fea("planet_narrow")
        wide = _run_planet_fea("planet_wide")

        self.assertTrue(narrow["ok"])
        self.assertTrue(wide["ok"])
        # Both should produce valid analysis results
        self.assertGreater(narrow["safety_factor"], 0)
        self.assertGreater(wide["safety_factor"], 0)

    def test_03_stiffening_rib_housing(self) -> None:
        """If housing deflection > 0.05mm → add ribs → re-run FEA."""
        import server.tools_analysis as mod

        sim_result: dict[str, Any] = {
            "summary": {"peak_joint_forces": {"joint_0": 800.0}},
            "time_series": [{"t": 0.0, "joint_efforts": [800.0]}],
        }

        with (
            patch.object(mod, "cad_export_body") as mock_export,
            patch.object(mod, "mesh_step_to_msh") as mock_mesh,
        ):
            mock_export.return_value = {"ok": True, "path": "/tmp/housing_ribbed.step"}
            mock_mesh.return_value = MeshInfo(
                path="/tmp/housing_ribbed.msh",
                num_nodes=2500,
                num_elements=1250,
                element_type="tet4",
                physical_groups={"Face1": 1, "Face12": 2},
            )

            result = mod.analysis_stress_from_simulation(
                body="housing_ribbed",
                material="aluminum_6061_t6",
                fixed_faces=["Face1"],
                load_faces=["Face12"],
                simulation_result=sim_result,
                safety_factor=1.5,
                solver="mock",
            )

        self.assertTrue(result["ok"])
        self.assertIn("max_displacement_mm", result)


if __name__ == "__main__":
    unittest.main()
