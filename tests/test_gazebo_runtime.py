"""Unit tests for Gazebo bridge runtime implementations."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from gazebo_bridge.runtime_gazebo import (
    GazeboRuntimeError,
    RealGazeboRuntime,
    StubGazeboRuntime,
    create_runtime,
)


def _make_temp_model(suffix: str = ".sdf") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(b"<sdf version='1.10'><model name='m'><link name='base'/></model></sdf>")
        return f.name


class TestCreateRuntime(unittest.TestCase):
    def test_env_selects_stub_runtime(self) -> None:
        with patch.dict(os.environ, {"SOLIDMIND_GAZEBO_RUNTIME": "stub"}, clear=False):
            rt = create_runtime()
        self.assertIsInstance(rt, StubGazeboRuntime)
        self.assertNotIsInstance(rt, RealGazeboRuntime)

    def test_explicit_real_runtime(self) -> None:
        with patch("gazebo_bridge.runtime_gazebo.shutil.which", return_value="/usr/bin/gz"):
            rt = create_runtime(runtime_mode="real")
        self.assertIsInstance(rt, RealGazeboRuntime)


class TestStubRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = StubGazeboRuntime(world_name="default", enable_px4=True)
        self.model_path = _make_temp_model()

    def tearDown(self) -> None:
        try:
            os.unlink(self.model_path)
        except OSError:
            pass

    def test_spawn_model_success(self) -> None:
        result = self.runtime.handle_spawn_model(
            {"sdf_path": self.model_path, "model_name": "drone"}
        )
        self.assertTrue(result["spawned"])
        self.assertEqual(result["model_name"], "drone")
        self.assertEqual(result["entity_id"], 1)

    def test_simulate_returns_time_series(self) -> None:
        result = self.runtime.handle_simulate(
            {
                "duration_s": 0.2,
                "dt_s": 0.01,
                "output_interval": 0.05,
                "mechanism": {"parts": [{"id": "frame"}]},
            }
        )
        self.assertIn("time_series", result)
        self.assertGreaterEqual(len(result["time_series"]), 2)
        self.assertEqual(result["summary"]["engine_mode"], "stub")

    def test_teleop_lifecycle_multirotor(self) -> None:
        started = self.runtime.handle_teleop_start(
            {
                "mechanism": {"parts": [{"id": "frame"}]},
                "profile": {"controller_type": "multirotor_direct"},
                "sdf_path": self.model_path,
            }
        )
        session_id = started["session_id"]
        self.assertEqual(started["controller_type"], "multirotor_direct")

        cmd = self.runtime.handle_teleop_command(
            {
                "session_id": session_id,
                "vx_mps": 0.4,
                "vy_mps": 0.1,
                "vz_mps": 0.2,
                "yaw_rate_rps": 0.3,
            }
        )
        self.assertTrue(cmd["applied"])
        self.assertIn("rotor_setpoints", cmd["state"])

        state = self.runtime.handle_teleop_state({"session_id": session_id})
        self.assertEqual(state["tick_count"], 1)
        self.assertAlmostEqual(state["state"]["vx_mps"], 0.4)

        stopped = self.runtime.handle_teleop_stop({"session_id": session_id})
        self.assertTrue(stopped["stopped"])

    def test_invalid_controller_type(self) -> None:
        with self.assertRaises(GazeboRuntimeError) as ctx:
            self.runtime.handle_teleop_start(
                {
                    "mechanism": {},
                    "profile": {"controller_type": "invalid"},
                    "sdf_path": self.model_path,
                }
            )
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")


class TestRealRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.model_path = _make_temp_model()
        self.calls: list[list[str]] = []

        def _runner(cmd: list[str]) -> tuple[int, str, str]:
            self.calls.append(cmd)
            if cmd[:3] == ["gz", "service", "-l"]:
                return 0, "/world/default/control\n/world/default/create\n", ""
            if "/world/default/create" in cmd:
                return 0, "data: true", ""
            if "/world/default/control" in cmd:
                return 0, "data: true", ""
            return 1, "", "unsupported"

        self.runner = _runner

    def tearDown(self) -> None:
        try:
            os.unlink(self.model_path)
        except OSError:
            pass

    def test_real_runtime_uses_gz_services(self) -> None:
        with patch("gazebo_bridge.runtime_gazebo.shutil.which", return_value="/usr/bin/gz"):
            runtime = RealGazeboRuntime(world_name="default", command_runner=self.runner)

        spawned = runtime.handle_spawn_model({"sdf_path": self.model_path, "model_name": "quad"})
        self.assertTrue(spawned["spawned"])
        self.assertTrue(any("/world/default/create" in " ".join(c) for c in self.calls))

        sim = runtime.handle_simulate(
            {
                "duration_s": 0.2,
                "dt_s": 0.02,
                "output_interval": 0.1,
                "sdf_path": self.model_path,
                "mechanism": {"parts": [{"id": "frame"}]},
            }
        )
        self.assertEqual(sim["summary"]["engine_mode"], "gazebo_real")
        self.assertTrue(any("/world/default/control" in " ".join(c) for c in self.calls))

    def test_diagnose_reports_worlds(self) -> None:
        with patch("gazebo_bridge.runtime_gazebo.shutil.which", return_value="/usr/bin/gz"):
            runtime = RealGazeboRuntime(world_name="default", command_runner=self.runner)
        diag = runtime.handle_diagnose({})
        self.assertTrue(diag["connected"])
        self.assertIn("default", diag["worlds"])
