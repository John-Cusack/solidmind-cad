"""Full pipeline E2E integration test.

Defines a 20T:40T gear pair mechanism once, then drives it through every
available engine tier — verifying physics at each stage and cross-validating
results across engines.

Always-run tiers:
  - Tier 1 (analytical, pure Python)
  - Gazebo stub (in-process GazeboBridgeServer + StubGazeboRuntime)

Conditionally-run tiers (skip gracefully):
  - Isaac degraded (bridge subprocess, no GPU)
  - Chrono real (C++ daemon)
  - FEA coupling (Elmer + Gmsh)
"""
from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from tests.conftest import GazeboStubBridge, mechanism_factory, unused_tcp_port

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Skip decorators
# ---------------------------------------------------------------------------

requires_chrono = unittest.skipUnless(
    os.path.isfile("chrono_daemon/build/chrono_daemon")
    or os.path.isfile("chrono_daemon/run.sh"),
    "Chrono daemon not built",
)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_command(host: str, port: int, cmd: str, args: dict | None = None) -> dict:
    """Send a single command to a bridge and return the parsed response."""
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


class _ChronoDaemon:
    """Launch the real Chrono daemon binary as a subprocess."""

    def __init__(self, port: int) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self._proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 10.0) -> None:
        binary = _PROJECT_ROOT / "chrono_daemon" / "build" / "chrono_daemon"
        run_sh = _PROJECT_ROOT / "chrono_daemon" / "run.sh"
        if binary.is_file():
            cmd = [str(binary), "--port", str(self.port)]
        elif run_sh.is_file():
            cmd = ["bash", str(run_sh), "--port", str(self.port)]
        else:
            raise FileNotFoundError("Chrono daemon binary not found")

        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                _, stderr = self._proc.communicate(timeout=2)
                raise RuntimeError(
                    f"Chrono daemon exited early (rc={self._proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect((self.host, self.port))
                s.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"Chrono daemon did not start within {timeout_s}s")

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((self.host, self.port))
            s.sendall(b'{"cmd":"shutdown","args":{}}\n')
            s.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)

    def send(self, cmd: str, args: dict | None = None) -> dict:
        return _send_command(self.host, self.port, cmd, args)

    def __enter__(self) -> _ChronoDaemon:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class _IsaacBridge:
    """Launch the Isaac bridge subprocess (degraded / headless)."""

    def __init__(self, port: int) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self._proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 10.0) -> None:
        self._proc = subprocess.Popen(
            [
                "python3", "-m", "isaac_bridge.bridge_server",
                "--port", str(self.port), "--headless",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                _, stderr = self._proc.communicate(timeout=2)
                raise RuntimeError(
                    f"Isaac bridge exited early (rc={self._proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect((self.host, self.port))
                s.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"Isaac bridge did not start within {timeout_s}s")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)

    def send(self, cmd: str, args: dict | None = None) -> dict:
        return _send_command(self.host, self.port, cmd, args)

    def __enter__(self) -> _IsaacBridge:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestFullPipelineE2E(unittest.TestCase):
    """Drive a 20T:40T gear pair through every available engine tier."""

    # Shared state populated by setUpClass
    _mech_id: str = ""
    _propagation: dict[str, Any] = {}
    _tier1_speeds: dict[str, float] = {}

    @classmethod
    def setUpClass(cls) -> None:
        from server import motion_store
        from server.tools_motion import (
            motion_define_mechanism,
            motion_propagate_motion,
            motion_validate,
        )

        motion_store.clear()

        mech_dict = mechanism_factory("gear_pair")
        result = motion_define_mechanism(mech_dict)
        assert result["ok"], f"Failed to define mechanism: {result}"
        cls._mech_id = result["mechanism_id"]

        # Run Tier 1 validation
        val = motion_validate(cls._mech_id)
        assert val["ok"], f"Tier 1 validation failed: {val}"

        # Run Tier 1 propagation
        prop = motion_propagate_motion(cls._mech_id)
        assert prop["ok"], f"Propagation failed: {prop}"
        cls._propagation = prop

        # Extract speeds for cross-validation
        cls._tier1_speeds = {
            part_id: state["rpm"]
            for part_id, state in prop["states"].items()
        }

    @classmethod
    def tearDownClass(cls) -> None:
        from server import motion_store
        motion_store.clear()

    # -----------------------------------------------------------------------
    # Tier 1: Analytical (always run, pure Python)
    # -----------------------------------------------------------------------

    def test_01_tier1_analytical_speeds(self) -> None:
        """gear_a = 1000 RPM, gear_b = 500 RPM (20T:40T, ratio=0.5).

        Tier 1 uses unsigned ratio convention: child = parent * ratio.
        With ratio = teeth_parent / teeth_child = 20/40 = 0.5,
        gear_b = 1000 * 0.5 = 500 RPM (no sign flip in analytical BFS).
        Chrono uses signed ratio (-0.5) and gives -500 RPM.
        """
        self.assertAlmostEqual(self._tier1_speeds["gear_a"], 1000.0, places=0)
        self.assertAlmostEqual(self._tier1_speeds["gear_b"], 500.0, places=0)

    def test_02_tier1_validation_passes(self) -> None:
        """Tier 1 validators produce no blockers."""
        from server.tools_motion import motion_validate

        result = motion_validate(self._mech_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["blockers"], [])

    def test_03_tier1_power_conservation(self) -> None:
        """Verify torque propagation through gear mesh.

        Tier 1 torque BFS: child_torque = parent_torque / ratio × efficiency.
        With ratio=0.5 and eff=1.0: gear_b_torque = 5.0 / 0.5 = 10.0 Nm.
        Power: P = T × ω.  gear_a: 5.0 × (1000×2π/60) = 523.6W.
                           gear_b: 10.0 × (500×2π/60) = 523.6W.
        """
        gear_a_torque = self._propagation["states"]["gear_a"]["torque_nm"]
        gear_b_torque = self._propagation["states"]["gear_b"]["torque_nm"]
        gear_a_rpm = self._propagation["states"]["gear_a"]["rpm"]
        gear_b_rpm = self._propagation["states"]["gear_b"]["rpm"]

        # Torque scales inversely with ratio (0.5): gear_b = gear_a / 0.5
        self.assertAlmostEqual(gear_a_torque, 5.0, delta=0.01)
        self.assertAlmostEqual(gear_b_torque, 10.0, delta=0.01)

        # Power conservation: T1×ω1 ≈ T2×ω2
        p_a = abs(gear_a_torque) * abs(gear_a_rpm) * 2 * math.pi / 60
        p_b = abs(gear_b_torque) * abs(gear_b_rpm) * 2 * math.pi / 60
        self.assertAlmostEqual(p_a, p_b, delta=0.1)

    # -----------------------------------------------------------------------
    # Gazebo Stub (always run, in-process)
    # -----------------------------------------------------------------------

    def test_10_gazebo_stub_simulate(self) -> None:
        """Gazebo stub returns time_series with joint_efforts."""
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port) as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("time_series", result)
        self.assertGreater(len(result["time_series"]), 0)
        self.assertIn("summary", result)
        # Joint efforts present
        last_entry = result["time_series"][-1]
        self.assertIn("joint_efforts", last_entry)
        # Peak joint forces in summary
        self.assertIn("peak_joint_forces", result["summary"])

    def test_11_gazebo_stub_to_fea_bcs(self) -> None:
        """Simulation result → fixed + force BCs, safety factor scaling."""
        from server.analysis_sim_coupling import bcs_from_simulation

        # Minimal sim result with known peak force
        sim_result: dict[str, Any] = {
            "summary": {
                "peak_joint_forces": {"joint_0": 5.0},
                "simulation_time_s": 0.5,
            },
            "time_series": [
                {"t": 0.0, "joint_efforts": [3.0]},
                {"t": 0.1, "joint_efforts": [5.0]},
            ],
        }

        # safety_factor=1.0
        bcs_1x = bcs_from_simulation(
            sim_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
            safety_factor=1.0,
        )
        self.assertEqual(len(bcs_1x), 2)  # fixed + force
        self.assertEqual(bcs_1x[0]["bc_type"], "fixed")
        self.assertEqual(bcs_1x[1]["bc_type"], "force")
        fz_1x = bcs_1x[1]["value"]["fz"]
        self.assertAlmostEqual(fz_1x, -5.0, places=3)

        # safety_factor=2.0 should double the force
        bcs_2x = bcs_from_simulation(
            sim_result,
            body="gear_a",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
            safety_factor=2.0,
        )
        fz_2x = bcs_2x[1]["value"]["fz"]
        self.assertAlmostEqual(fz_2x, 2.0 * fz_1x, places=3)

    def test_12_propagation_to_fea_bcs(self) -> None:
        """Tier 1 torques → force BCs via bcs_from_propagation."""
        from server.analysis_sim_coupling import bcs_from_propagation

        bcs = bcs_from_propagation(
            self._propagation,
            body="gear_b",
            fixed_faces=["Face1"],
            load_faces=["Face2"],
            load_direction=(0.0, 0.0, -1.0),
        )
        # Should have fixed + force
        self.assertGreaterEqual(len(bcs), 1)  # at least fixed
        fixed = [b for b in bcs if b["bc_type"] == "fixed"]
        force = [b for b in bcs if b["bc_type"] == "force"]
        self.assertEqual(len(fixed), 1)
        # Force present if gear_b has nonzero torque
        gear_b_torque = self._propagation["states"]["gear_b"]["torque_nm"]
        if abs(gear_b_torque) > 1e-9:
            self.assertEqual(len(force), 1)
            self.assertNotEqual(force[0]["value"]["fz"], 0.0)

    # -----------------------------------------------------------------------
    # Isaac Degraded (skip if bridge can't start)
    # -----------------------------------------------------------------------

    def test_20_isaac_degraded_simulate(self) -> None:
        """Isaac bridge (degraded) accepts revolute-only mechanism.

        Isaac bridge v1 doesn't support gear_mesh joints, so we use a
        simple revolute mechanism instead.  Verifies the bridge launches,
        accepts connections, and returns a time series.
        """
        port = unused_tcp_port()
        try:
            bridge = _IsaacBridge(port)
            bridge.start(timeout_s=10.0)
        except (RuntimeError, FileNotFoundError) as exc:
            self.skipTest(f"Isaac bridge unavailable: {exc}")

        try:
            # Isaac bridge only supports revolute/prismatic/fixed joints
            mech = {
                "name": "simple_arm",
                "parts": [
                    {"id": "base", "is_ground": True},
                    {"id": "arm"},
                ],
                "joints": [
                    {
                        "id": "shoulder",
                        "joint_type": "revolute",
                        "parent_part": "base",
                        "child_part": "arm",
                    },
                ],
                "drives": [],
            }
            resp = bridge.send("simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
            })
            self.assertTrue(resp["ok"], resp)
            result = resp["result"]
            self.assertIn("time_series", result)
            self.assertGreater(len(result["time_series"]), 0)
            self.assertIn("summary", result)
        finally:
            bridge.stop()

    # -----------------------------------------------------------------------
    # Chrono Real (skip if daemon not built)
    # -----------------------------------------------------------------------

    @requires_chrono
    def test_30_chrono_simulate(self) -> None:
        """Chrono daemon: gear_a=1000, gear_b=-500 RPM."""
        from server import motion_store
        from server.simulation_spec_builder import (
            add_derived_speeds,
            build_simulation_spec,
            validate_simulation_spec,
        )

        mech = motion_store.get(self._mech_id)
        self.assertIsNotNone(mech, "Mechanism not in store")

        spec = build_simulation_spec(mech)
        issues = validate_simulation_spec(spec)
        self.assertEqual(issues, [], f"Spec validation failed: {issues}")

        port = unused_tcp_port()
        with _ChronoDaemon(port) as daemon:
            resp = daemon.send("simulate", {
                "simulation_spec": spec,
                "duration_s": 0.5,
                "dt_s": 0.001,
                "output_interval": 0.05,
            })

        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        add_derived_speeds(result, spec)

        ss = result["summary"]["steady_state_speeds"]
        self.assertAlmostEqual(ss["gear_a"], 1000.0, places=0)
        self.assertAlmostEqual(ss["gear_b"], -500.0, places=0)

        # Store for cross-validation
        self.__class__._chrono_speeds = ss

    @requires_chrono
    def test_40_cross_validate_tier1_vs_chrono(self) -> None:
        """Cross-validate: speed magnitudes agree between Tier 1 and Chrono.

        Tier 1 uses unsigned gear ratio (0.5) → gear_b = 500 RPM.
        Chrono uses signed ratio (-0.5) → gear_b = -500 RPM.

        Speed magnitudes should agree; only sign differs (Tier 1 is
        unsigned, Chrono encodes counter-rotation as negative).
        """
        chrono_speeds = getattr(self.__class__, "_chrono_speeds", None)
        if chrono_speeds is None:
            self.skipTest("test_30 did not run or failed")

        # gear_a (driven) should match in both
        self.assertAlmostEqual(
            self._tier1_speeds["gear_a"], chrono_speeds["gear_a"], delta=1.0,
            msg="gear_a should agree between Tier 1 and Chrono",
        )

        # Chrono gives the physically correct signed speed
        self.assertAlmostEqual(
            chrono_speeds["gear_a"], 1000.0, delta=1.0,
        )
        self.assertAlmostEqual(
            chrono_speeds["gear_b"], -500.0, delta=1.0,
        )

        # Magnitude agreement — the "never again" regression guard
        self.assertAlmostEqual(
            abs(self._tier1_speeds["gear_b"]),
            abs(chrono_speeds["gear_b"]),
            delta=1.0,
            msg="Tier 1 and Chrono speed magnitudes must agree",
        )

    # -----------------------------------------------------------------------
    # FEA Coupling: sim forces → thermal FEA (skip if Elmer/Gmsh missing)
    # -----------------------------------------------------------------------

    @requires_elmer
    def test_50_sim_forces_to_thermal_fea(self) -> None:
        """Stub sim forces → heat flux BC → real Elmer thermal → T gradient.

        Full loop:
        1. Create 10×10×10mm box via Gmsh OCC (no FreeCAD)
        2. Get peak joint force from Gazebo stub sim
        3. Convert force to heat flux: Q = F × v_sliding
        4. Set T=300K on Face6, heat_flux on Face1
        5. Run real Elmer thermal solver
        6. Verify T_max > 300K, physically reasonable gradient
        """
        import gmsh

        from server.analysis_mesh import mesh_step_to_msh
        from server.analysis_models import (
            AnalysisSpec,
            AnalysisType,
            BoundaryCondition,
            Material,
        )
        from server.analysis_solver_elmer import ElmerSolver

        # Step 1: Gazebo stub → peak joint force
        port = unused_tcp_port()
        mech = mechanism_factory("gear_pair")
        with GazeboStubBridge(port) as bridge:
            sim_resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
            })
        self.assertTrue(sim_resp["ok"], sim_resp)
        peak_forces = sim_resp["result"]["summary"].get("peak_joint_forces", {})
        peak_force = max(
            (float(v) for v in peak_forces.values()),
            default=5.0,
        )
        self.assertGreater(peak_force, 0, "Expected nonzero peak force from stub")

        # Step 2: Convert force to heat flux (frictional heating at gear mesh)
        # Q = F × v_sliding; assume v_sliding ≈ 1 m/s for simplicity
        v_sliding_mps = 1.0
        heat_flux_w_m2 = peak_force * v_sliding_mps * 1000  # scale up for visible gradient

        # Step 3: Create box geometry + mesh
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            step_path = work_dir / "box.step"

            gmsh.initialize()
            try:
                gmsh.model.occ.addBox(0, 0, 0, 10, 10, 10)
                gmsh.model.occ.synchronize()
                gmsh.write(str(step_path))
            finally:
                gmsh.finalize()

            mesh_info = mesh_step_to_msh(
                step_path=str(step_path),
                face_groups={
                    "bc_0_heat_flux": ["Face1"],
                    "bc_1_temperature": ["Face6"],
                },
                mesh_size=3.0,
                msh_version=2.2,
            )

            # Step 4: Run Elmer thermal
            mat = Material(
                name="steel",
                youngs_modulus_mpa=200_000,
                poissons_ratio=0.3,
                density_kg_m3=7800,
                yield_strength_mpa=250,
                thermal_conductivity_w_mk=51.9,
                specific_heat_j_kgk=490,
            )
            bcs = (
                BoundaryCondition(
                    bc_type="heat_flux",
                    faces=("Face1",),
                    value={"flux_w_m2": heat_flux_w_m2},
                ),
                BoundaryCondition(
                    bc_type="temperature",
                    faces=("Face6",),
                    value={"temperature_k": 300},
                ),
            )
            spec = AnalysisSpec(
                analysis_type=AnalysisType.THERMAL,
                body="TestBox",
                material=mat,
                boundary_conditions=bcs,
            )

            solver = ElmerSolver()
            input_path = solver.write_input(spec, mesh_info, work_dir)
            solver.run(input_path, work_dir)
            result = solver.parse_results(work_dir, spec)

        # Step 5: Verify physics
        temp_fields = [
            f for f in result.scalar_fields
            if f.field_name == "temperature"
        ]
        self.assertEqual(len(temp_fields), 1, "Expected 1 temperature field")
        tf = temp_fields[0]

        # T_max should exceed ambient (heat flux adds energy)
        self.assertGreater(tf.max_val, 300.0, "T_max should exceed 300K (ambient)")
        # T_min should be near Dirichlet BC value
        self.assertAlmostEqual(tf.min_val, 300.0, delta=5.0)
        # There should be a gradient (max > min)
        self.assertGreater(tf.max_val, tf.min_val)


if __name__ == "__main__":
    unittest.main()
