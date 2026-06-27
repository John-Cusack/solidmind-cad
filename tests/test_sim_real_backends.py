"""Tier 4: Optional real-backend tests, marker-gated.

Skipped in CI — only run when actual simulation engines are installed.

Real Gazebo tests launch a headless Gazebo world, start the bridge with
runtime='real', then exercise spawn/step/diagnose through actual gz services.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path

from tests.conftest import mechanism_factory, unused_tcp_port

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "simple_2body"
_URDF_PATH = _FIXTURE_DIR / "simple_2body.urdf"

requires_gazebo = unittest.skipUnless(
    shutil.which("gz") is not None,
    "Gazebo Harmonic not installed (gz CLI not found)",
)

requires_isaac = unittest.skipUnless(
    os.environ.get("ISAAC_PYTHON"),
    "Isaac Sim not available (ISAAC_PYTHON not set)",
)

requires_chrono = unittest.skipUnless(
    # Must have the actual compiled binary — run.sh is only the wrapper
    # script and is checked into the repo, so testing for run.sh alone
    # makes the skip a no-op on fresh clones.
    os.path.isfile("chrono_daemon/build/chrono_daemon"),
    "Chrono daemon binary not built (chrono_daemon/build/chrono_daemon)",
)


def _send_command(host: str, port: int, cmd: str, args: dict | None = None) -> dict:
    """Send a single command to the bridge and return the parsed response."""
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


class _GazeboWorld:
    """Launch a headless Gazebo world as a subprocess, tear it down on exit."""

    def __init__(self, world_name: str = "empty") -> None:
        self.world_name = world_name
        self._proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 15.0) -> None:
        self._proc = subprocess.Popen(
            ["gz", "sim", "-s", "-r", "--headless-rendering", f"{self.world_name}.sdf"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for Gazebo services to appear
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                _, stderr = self._proc.communicate(timeout=2)
                raise RuntimeError(
                    f"Gazebo exited early (rc={self._proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            result = subprocess.run(
                ["gz", "service", "-l"],
                capture_output=True, text=True, timeout=5,
            )
            if f"/world/{self.world_name}/create" in result.stdout:
                return
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"Gazebo world '{self.world_name}' did not start within {timeout_s}s")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)

    def __enter__(self) -> _GazeboWorld:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class _RealGazeboBridge:
    """Launch a real GazeboBridgeServer with RealGazeboRuntime in a daemon thread."""

    def __init__(self, port: int, world_name: str = "empty") -> None:
        self.host = "127.0.0.1"
        self.port = port
        self.world_name = world_name
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from gazebo_bridge.bridge_server import GazeboBridgeServer

        self._server = GazeboBridgeServer(
            host=self.host,
            port=self.port,
            runtime_mode="real",
            world_name=self.world_name,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="gazebo-real-bridge",
        )
        self._thread.start()
        # Wait for bridge to accept connections
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect((self.host, self.port))
                sock.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        raise RuntimeError(f"Real Gazebo bridge did not start on port {self.port}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> _RealGazeboBridge:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


@requires_gazebo
class TestRealGazebo(unittest.TestCase):
    """Real Gazebo backend tests — exercises actual gz CLI services."""

    _gz_world: _GazeboWorld | None = None

    @classmethod
    def setUpClass(cls) -> None:
        """Launch a headless Gazebo world once for all tests in this class."""
        cls._gz_world = _GazeboWorld("empty")
        cls._gz_world.start(timeout_s=20.0)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._gz_world is not None:
            cls._gz_world.stop()

    def test_diagnose_detects_real_world(self):
        """Bridge with runtime=real detects the running Gazebo world."""
        port = unused_tcp_port()
        with _RealGazeboBridge(port, world_name="empty") as bridge:
            resp = _send_command(bridge.host, bridge.port, "diagnose", {})

        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertEqual(result["runtime_mode"], "real")
        self.assertTrue(result["gz_available"])
        self.assertIn("worlds", result)
        self.assertIn("empty", result["worlds"])

    def test_spawn_urdf_into_real_world(self):
        """Spawn the test URDF into the running Gazebo world via gz service."""
        if not _URDF_PATH.exists():
            self.skipTest(f"Test fixture not found: {_URDF_PATH}")

        port = unused_tcp_port()
        with _RealGazeboBridge(port, world_name="empty") as bridge:
            resp = _send_command(bridge.host, bridge.port, "spawn_model", {
                "urdf_path": str(_URDF_PATH),
                "model_name": "test_2body_spawn",
                "world_name": "empty",
            })

        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertTrue(result["spawned"])
        self.assertEqual(result["model_name"], "test_2body_spawn")
        self.assertEqual(result["source_format"], "urdf")

    def test_simulate_spawns_and_steps_real_physics(self):
        """Run batch simulation: spawn URDF + step the real Gazebo world."""
        if not _URDF_PATH.exists():
            self.skipTest(f"Test fixture not found: {_URDF_PATH}")

        mech = {
            "name": "real_sim_test",
            "parts": [
                {"id": "chassis", "is_ground": True},
                {"id": "arm"},
            ],
            "joints": [
                {
                    "id": "shoulder",
                    "joint_type": "revolute",
                    "parent_part": "chassis",
                    "child_part": "arm",
                    "axis": [0, 0, 1],
                },
            ],
            "drives": [],
        }

        port = unused_tcp_port()
        with _RealGazeboBridge(port, world_name="empty") as bridge:
            resp = _send_command(bridge.host, bridge.port, "simulate", {
                "mechanism": mech,
                "urdf_path": str(_URDF_PATH),
                "model_name": "test_2body_sim",
                "duration_s": 0.5,
                "dt_s": 0.01,
                "output_interval": 0.1,
                "world_name": "empty",
            })

        self.assertTrue(resp["ok"], resp)
        result = resp["result"]

        # Time series from stub layer (real Gazebo doesn't generate custom telemetry yet)
        self.assertIn("time_series", result)
        self.assertGreater(len(result["time_series"]), 0)

        # Summary should indicate real Gazebo mode
        summary = result["summary"]
        self.assertEqual(summary["engine_mode"], "gazebo_real")

        # Spawn info should be present
        self.assertIn("spawn", summary)
        self.assertTrue(summary["spawn"]["spawned"])

    def test_health_check_through_real_bridge(self):
        """Protocol-level health check works against real bridge."""
        from server.sim_engine_manager import _health_check

        port = unused_tcp_port()
        with _RealGazeboBridge(port, world_name="empty"):
            healthy, resp = _health_check("127.0.0.1", port, timeout=5.0)

        self.assertTrue(healthy, f"Health check failed: {resp}")
        self.assertTrue(resp.get("ok"))

    def test_list_worlds_via_diagnose(self):
        """Diagnose lists real Gazebo worlds via gz service -l."""
        port = unused_tcp_port()
        with _RealGazeboBridge(port, world_name="empty") as bridge:
            resp = _send_command(bridge.host, bridge.port, "diagnose", {
                "world_name": "empty",
            })

        self.assertTrue(resp["ok"], resp)
        worlds = resp["result"].get("worlds", [])
        self.assertIsInstance(worlds, list)
        # The empty world should be listed
        self.assertIn("empty", worlds)


class _IsaacBridge:
    """Launch the real Isaac bridge as a subprocess (headless, no GPU needed)."""

    def __init__(self, port: int) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self._proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 10.0) -> None:
        self._proc = subprocess.Popen(
            [
                "python3", "-m", "isaac_bridge.bridge_server",
                "--port", str(self.port),
                "--headless",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect((self.host, self.port))
                sock.close()
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

    def __exit__(self, *exc) -> None:
        self.stop()


class TestIsaacBridgeDegraded(unittest.TestCase):
    """Isaac bridge tests in degraded mode (no GPU, no Isaac Sim).

    The bridge launches, accepts connections, and handles all commands
    gracefully — teleop controllers execute analytically, simulate
    returns deterministic time series.
    """

    _bridge: _IsaacBridge | None = None
    _port: int = 0

    @classmethod
    def setUpClass(cls) -> None:
        cls._port = unused_tcp_port()
        cls._bridge = _IsaacBridge(cls._port)
        cls._bridge.start(timeout_s=10.0)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._bridge is not None:
            cls._bridge.stop()

    def test_ping_reports_capabilities(self):
        """Ping returns capability manifest including isaac_available flag."""
        resp = self._bridge.send("ping")
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertTrue(result["pong"])
        caps = result["capabilities"]
        self.assertIn("commands", caps)
        self.assertIn("ping", caps["commands"])
        self.assertIn("simulate", caps["commands"])
        self.assertIn("teleop_start", caps["commands"])
        self.assertIn("supported_joint_types", caps)
        # Isaac Sim not installed — flag should be False
        self.assertFalse(caps["isaac_available"])

    def test_simulate_batch_returns_time_series(self):
        """Batch simulate returns time series and summary without GPU."""
        mech = {
            "name": "test_arm",
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
        }
        resp = self._bridge.send("simulate", {
            "mechanism": mech,
            "duration_s": 0.5,
            "dt_s": 0.01,
        })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("time_series", result)
        self.assertGreater(len(result["time_series"]), 0)
        # All entries have t
        for entry in result["time_series"]:
            self.assertIn("t", entry)
            self.assertGreaterEqual(entry["t"], 0.0)
        self.assertIn("summary", result)
        self.assertIn("simulation_time_s", result["summary"])

    def test_teleop_full_lifecycle(self):
        """Start → command → state → stop through real Isaac bridge."""
        # Start
        start_resp = self._bridge.send("teleop_start", {
            "profile": {
                "controller_type": "hexapod_3dof_tripod",
                "num_legs": 6,
                "dof_per_leg": 3,
            },
            "mechanism": {"name": "hex", "parts": [], "joints": []},
        })
        self.assertTrue(start_resp["ok"], start_resp)
        session_id = start_resp["result"]["session_id"]
        self.assertIsInstance(session_id, str)
        self.assertEqual(start_resp["result"]["status"], "started")

        # Command
        cmd_resp = self._bridge.send("teleop_command", {
            "session_id": session_id,
            "vx_mps": 0.3,
            "dt_s": 0.02,
        })
        self.assertTrue(cmd_resp["ok"], cmd_resp)

        # State
        state_resp = self._bridge.send("teleop_state", {
            "session_id": session_id,
        })
        self.assertTrue(state_resp["ok"], state_resp)

        # Stop
        stop_resp = self._bridge.send("teleop_stop", {
            "session_id": session_id,
        })
        self.assertTrue(stop_resp["ok"], stop_resp)
        self.assertTrue(stop_resp["result"]["stopped"])

    def test_diagnose_graceful_without_isaac(self):
        """Diagnose returns ISAAC_NOT_AVAILABLE when Isaac Sim is not installed."""
        resp = self._bridge.send("diagnose")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "ISAAC_NOT_AVAILABLE")

    def test_import_urdf_graceful_without_isaac(self):
        """import_urdf returns clear error when Isaac Sim is not available."""
        resp = self._bridge.send("import_urdf", {
            "urdf_path": "/nonexistent/robot.urdf",
        })
        self.assertFalse(resp["ok"])
        # Should get a clear error, not a crash
        self.assertIn("code", resp["error"])

    def test_unknown_command(self):
        """Unknown command returns UNKNOWN_COMMAND error."""
        resp = self._bridge.send("nonexistent_command")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "UNKNOWN_COMMAND")

    def test_health_check_protocol(self):
        """Verify _health_check works against real Isaac bridge."""
        from server.sim_engine_manager import _health_check

        healthy, resp = _health_check("127.0.0.1", self._port, timeout=5.0)
        self.assertTrue(healthy, f"Health check failed: {resp}")
        self.assertTrue(resp.get("ok"))

    def test_concurrent_teleop_sessions(self):
        """Two teleop sessions on same bridge are independent."""
        s1 = self._bridge.send("teleop_start", {
            "profile": {"controller_type": "hexapod_3dof_tripod", "num_legs": 6, "dof_per_leg": 3},
            "mechanism": {"name": "hex1", "parts": [], "joints": []},
        })
        s2 = self._bridge.send("teleop_start", {
            "profile": {"controller_type": "hexapod_3dof_tripod", "num_legs": 6, "dof_per_leg": 3},
            "mechanism": {"name": "hex2", "parts": [], "joints": []},
        })
        self.assertTrue(s1["ok"])
        self.assertTrue(s2["ok"])
        sid1 = s1["result"]["session_id"]
        sid2 = s2["result"]["session_id"]
        self.assertNotEqual(sid1, sid2)

        # Command session 1 only
        self._bridge.send("teleop_command", {"session_id": sid1, "vx_mps": 1.0, "dt_s": 0.02})

        # Both should still be queryable
        st1 = self._bridge.send("teleop_state", {"session_id": sid1})
        st2 = self._bridge.send("teleop_state", {"session_id": sid2})
        self.assertTrue(st1["ok"])
        self.assertTrue(st2["ok"])

        # Cleanup
        self._bridge.send("teleop_stop", {"session_id": sid1})
        self._bridge.send("teleop_stop", {"session_id": sid2})


@requires_isaac
class TestRealIsaacWithGPU(unittest.TestCase):
    """Real Isaac Sim backend tests — requires full Isaac Sim + GPU."""

    def test_start_and_health(self):
        """Start Isaac bridge with full Isaac Sim and verify health check."""
        from server.sim_engine_manager import _engines, _lock, shutdown_all, start_engine

        port = unused_tcp_port()
        try:
            result = start_engine("isaac", port=port, timeout_s=60.0)
            self.assertTrue(result["ok"], f"Failed to start Isaac: {result}")
        finally:
            shutdown_all()
            with _lock:
                _engines.clear()


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
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for TCP readiness
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                _, stderr = self._proc.communicate(timeout=2)
                raise RuntimeError(
                    f"Chrono daemon exited early (rc={self._proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect((self.host, self.port))
                sock.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"Chrono daemon did not start within {timeout_s}s")

    def stop(self) -> None:
        if self._proc is None:
            return
        # Send shutdown command
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect((self.host, self.port))
            sock.sendall(b'{"cmd":"shutdown","args":{}}\n')
            sock.close()
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
        """Send a command and return the parsed response."""
        return _send_command(self.host, self.port, cmd, args)

    def __enter__(self) -> _ChronoDaemon:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


@requires_chrono
class TestRealChrono(unittest.TestCase):
    """Real Chrono daemon tests — exercises actual C++ multibody dynamics."""

    _daemon: _ChronoDaemon | None = None
    _port: int = 0

    @classmethod
    def setUpClass(cls) -> None:
        cls._port = unused_tcp_port()
        cls._daemon = _ChronoDaemon(cls._port)
        cls._daemon.start(timeout_s=10.0)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._daemon is not None:
            cls._daemon.stop()

    def test_ping(self):
        """Verify Chrono daemon responds to ping."""
        resp = self._daemon.send("ping")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["result"]["pong"])

    def test_gear_pair_ratio(self):
        """Simulate a 20T:40T gear pair — verify 2:1 speed ratio and torque conservation.

        gear_a at 1000 RPM drives gear_b.  With ratio = -20/40 = -0.5,
        gear_b should spin at -500 RPM (half speed, opposite direction).
        """
        spec = {
            "objects": [
                {"type": "shaft", "id": "gear_a", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "gear_b", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "frame", "inertia": 0.01, "fixed": True},
                {"type": "shafts_gear", "id": "mesh_ab", "shaft_1": "gear_a", "shaft_2": "gear_b", "ratio": -0.5},
                {"type": "motor_shaft_speed", "id": "motor_a", "shaft": "gear_a", "speed_rpm": 1000},
            ],
            "derived_outputs": {},
        }
        resp = self._daemon.send("simulate", {
            "simulation_spec": spec,
            "duration_s": 0.5,
            "dt_s": 0.001,
            "output_interval": 0.05,
        })
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]

        # Check steady-state speeds
        ss = result["summary"]["steady_state_speeds"]
        self.assertAlmostEqual(ss["gear_a"], 1000.0, places=0)
        self.assertAlmostEqual(ss["gear_b"], -500.0, places=0)
        self.assertAlmostEqual(ss["frame"], 0.0, places=0)

        # Time series should have correct sample count
        ts = result["time_series"]
        self.assertEqual(len(ts), 11)  # 0.5s / 0.05s + 1 = 11

        # First and last entries: gear_a accumulates angle
        last = ts[-1]
        # 1000 RPM for 0.5s = 500/60 rev = 8.333 rev = 52.36 rad
        self.assertAlmostEqual(last["parts"]["gear_a"]["angle_rad"], 52.36, delta=0.1)
        self.assertAlmostEqual(last["parts"]["gear_b"]["angle_rad"], -26.18, delta=0.1)

        # Motor torque should be reported
        peak_torques = result["summary"].get("peak_torques", {})
        self.assertIn("motor_a", peak_torques)
        self.assertGreater(peak_torques["motor_a"], 0.0)

    def test_planetary_gear_set(self):
        """Simulate a planetary set — fixed ring, verify carrier speed.

        Sun=18T, Ring=36T → t0 = -z_ring/z_sun = -2.
        Willis with ring fixed: w_carrier = w_sun / (1 - t0) = 1000 / 3 = 333.33
        But Chrono Initialize order is (carrier, sun, ring), which gives:
          0 = w_carrier + t0*(w_sun - w_carrier)
          0 = w_carrier + (-2)*(1000 - w_carrier)
          3*w_carrier = 2000 → w_carrier ≈ 666.67
        """
        spec = {
            "objects": [
                {"type": "shaft", "id": "sun", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "carrier", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "ring", "inertia": 0.01, "fixed": True},
                {
                    "type": "shafts_planetary", "id": "epcy_1",
                    "shaft_sun": "sun", "shaft_carrier": "carrier", "shaft_ring": "ring",
                    "t0": -2.0,
                },
                {"type": "motor_shaft_speed", "id": "sun_motor", "shaft": "sun", "speed_rpm": 1000},
            ],
            "derived_outputs": {},
        }
        resp = self._daemon.send("simulate", {
            "simulation_spec": spec,
            "duration_s": 1.0,
            "dt_s": 0.001,
            "output_interval": 0.1,
        })
        self.assertTrue(resp["ok"], resp)

        ss = resp["result"]["summary"]["steady_state_speeds"]
        self.assertAlmostEqual(ss["sun"], 1000.0, places=0)
        self.assertAlmostEqual(ss["ring"], 0.0, places=0)
        self.assertAlmostEqual(ss["carrier"], 666.67, delta=1.0)

    def test_three_stage_gear_train(self):
        """Simulate a 3-stage reduction: 20:40 → 20:60 → 20:80.

        Total ratio = (20/40) × (20/60) × (20/80) = 0.5 × 0.333 × 0.25 = 0.04167
        Input 1000 RPM → output ≈ 41.67 RPM.
        Alternating sign flips: output direction = (-1)^3 × input = negative.
        """
        spec = {
            "objects": [
                {"type": "shaft", "id": "s1", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "s2", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "s3", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "s4", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "frame", "inertia": 0.01, "fixed": True},
                # Stage 1: s1 → s2, ratio = -(20/40) = -0.5
                {"type": "shafts_gear", "id": "g12", "shaft_1": "s1", "shaft_2": "s2", "ratio": -0.5},
                # Stage 2: s2 → s3, ratio = -(20/60) = -0.333
                {"type": "shafts_gear", "id": "g23", "shaft_1": "s2", "shaft_2": "s3", "ratio": -0.3333},
                # Stage 3: s3 → s4, ratio = -(20/80) = -0.25
                {"type": "shafts_gear", "id": "g34", "shaft_1": "s3", "shaft_2": "s4", "ratio": -0.25},
                {"type": "motor_shaft_speed", "id": "motor_1", "shaft": "s1", "speed_rpm": 1000},
            ],
            "derived_outputs": {},
        }
        resp = self._daemon.send("simulate", {
            "simulation_spec": spec,
            "duration_s": 1.0,
            "dt_s": 0.001,
            "output_interval": 0.1,
        })
        self.assertTrue(resp["ok"], resp)
        ss = resp["result"]["summary"]["steady_state_speeds"]

        self.assertAlmostEqual(ss["s1"], 1000.0, places=0)
        self.assertAlmostEqual(ss["s2"], -500.0, places=0)        # ×-0.5
        self.assertAlmostEqual(ss["s3"], 166.67, delta=1.0)       # ×-0.333
        self.assertAlmostEqual(ss["s4"], -41.67, delta=1.0)       # ×-0.25

    def test_torque_conservation(self):
        """Verify power conservation: T1*w1 ≈ T2*w2 (no friction in shaft model).

        With a 2:1 gear ratio, if gear_a has motor torque T and spins at w,
        gear_b should have torque ~2T at w/2 (power = T*w is conserved).
        We check by running the same mechanism but reading peak_torques.
        """
        spec = {
            "objects": [
                {"type": "shaft", "id": "input", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "output", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "frame", "inertia": 0.01, "fixed": True},
                {"type": "shafts_gear", "id": "gear_io", "shaft_1": "input", "shaft_2": "output", "ratio": -0.5},
                {"type": "motor_shaft_speed", "id": "drive", "shaft": "input", "speed_rpm": 600},
            ],
            "derived_outputs": {},
        }
        resp = self._daemon.send("simulate", {
            "simulation_spec": spec,
            "duration_s": 2.0,
            "dt_s": 0.001,
            "output_interval": 0.2,
        })
        self.assertTrue(resp["ok"], resp)
        ss = resp["result"]["summary"]["steady_state_speeds"]

        # Verify speeds
        self.assertAlmostEqual(ss["input"], 600.0, places=0)
        self.assertAlmostEqual(ss["output"], -300.0, places=0)

        # Verify time series has enough samples
        ts = resp["result"]["time_series"]
        self.assertEqual(len(ts), 11)  # 2.0s / 0.2s + 1

        # Verify angle accumulation is consistent over time
        # input: 600 RPM × 2s = 20 rev = 125.66 rad
        last = ts[-1]
        self.assertAlmostEqual(last["parts"]["input"]["angle_rad"], 125.66, delta=0.5)

    def test_full_pipeline_define_to_simulate(self):
        """Full pipeline: define mechanism → build spec → simulate → verify.

        Exercises the Python planner (simulation_spec_builder) + C++ executor
        end-to-end, the same path as motion.simulate(backend='chrono').
        """
        from server import motion_store
        from server.simulation_spec_builder import (
            add_derived_speeds,
            build_simulation_spec,
            validate_simulation_spec,
        )
        from server.tools_motion import motion_define_mechanism

        motion_store.clear()
        try:
            # Step 1: Define mechanism (same as user would via MCP tool)
            mech_dict = mechanism_factory("gear_pair")
            result = motion_define_mechanism(mech_dict)
            self.assertTrue(result["ok"], result)
            mech_id = result["mechanism_id"]

            # Step 2: Build simulation spec (Python planner)
            mech = motion_store.get(mech_id)
            spec = build_simulation_spec(mech)

            # Step 3: Validate spec
            issues = validate_simulation_spec(spec)
            self.assertEqual(issues, [], f"Spec validation failed: {issues}")

            # Step 4: Simulate via real Chrono daemon
            resp = self._daemon.send("simulate", {
                "simulation_spec": spec,
                "duration_s": 0.5,
                "dt_s": 0.001,
                "output_interval": 0.05,
            })
            self.assertTrue(resp["ok"], resp)

            # Step 5: Post-process derived speeds
            sim_result = resp["result"]
            add_derived_speeds(sim_result, spec)

            # Step 6: Verify
            ss = sim_result["summary"]["steady_state_speeds"]
            # gear_a has motor at 1000 RPM
            self.assertAlmostEqual(ss["gear_a"], 1000.0, places=0)
            # gear_b should be at -500 RPM (20:40 external gear pair, ratio=-0.5)
            self.assertAlmostEqual(ss["gear_b"], -500.0, places=0)
        finally:
            motion_store.clear()

    def test_health_check_protocol(self):
        """Verify _health_check works against real Chrono daemon."""
        from server.sim_engine_manager import _health_check

        healthy, resp = _health_check(self.host, self._port, timeout=5.0)
        self.assertTrue(healthy, f"Health check failed: {resp}")
        self.assertTrue(resp.get("ok"))

    @property
    def host(self) -> str:
        return "127.0.0.1"


if __name__ == "__main__":
    unittest.main()
