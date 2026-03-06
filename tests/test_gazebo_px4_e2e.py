"""Optional PX4 lifecycle e2e tests for Gazebo bridge."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gazebo_bridge.bridge_server import GazeboBridgeServer
from server.gazebo_client import GazeboClient


@unittest.skipUnless(
    os.environ.get("SOLIDMIND_RUN_GAZEBO_PX4_E2E") == "1",
    "Set SOLIDMIND_RUN_GAZEBO_PX4_E2E=1 to run Gazebo PX4 e2e tests.",
)
class TestGazeboPx4E2E(unittest.TestCase):
    def setUp(self) -> None:
        runtime_mode = os.environ.get("SOLIDMIND_GAZEBO_PX4_RUNTIME", "stub")
        self._env = patch.dict(os.environ, {"SOLIDMIND_GAZEBO_PX4_FAKE": "1"}, clear=False)
        self._env.start()
        self.server = GazeboBridgeServer(
            host="127.0.0.1",
            port=0,
            runtime_mode=runtime_mode,
            world_name="default",
            enable_px4=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 5.0
        while self.server.port == 0 and time.time() < deadline:
            time.sleep(0.02)
        if self.server.port == 0:
            self.fail("Gazebo bridge server failed to bind")

        self.client = GazeboClient(host="127.0.0.1", port=self.server.port)
        self.client.connect(timeout=2.0)
        with tempfile.NamedTemporaryFile(suffix=".sdf", mode="w", delete=False) as f:
            f.write(
                """<?xml version='1.0'?>
<sdf version="1.10">
  <model name="px4_e2e">
    <link name="base"/>
  </model>
</sdf>
"""
            )
            self.sdf_path = f.name

    def tearDown(self) -> None:
        self.client.disconnect()
        self.server.shutdown()
        self.thread.join(timeout=2.0)
        self._env.stop()
        try:
            os.unlink(self.sdf_path)
        except OSError:
            pass

    def test_px4_lifecycle_and_offboard_teleop(self) -> None:
        started = self.client.px4_start()
        self.assertIn("status", started)
        self.assertTrue(started["status"].get("running", False))

        status = self.client.px4_status()
        self.assertTrue(status.get("running", False))

        teleop = self.client.teleop_start(
            mechanism={"name": "px4", "parts": [{"id": "base"}], "joints": [], "drives": []},
            profile={"controller_type": "px4_offboard"},
            sdf_path=self.sdf_path,
        )
        session_id = teleop["session_id"]
        self.assertTrue(session_id)

        applied = self.client.teleop_command(
            session_id=session_id,
            vx_mps=0.5,
            vy_mps=0.1,
            vz_mps=0.2,
            yaw_rate_rps=0.4,
            body_height_m=0.0,
        )
        self.assertTrue(applied.get("applied", False))

        state = self.client.teleop_state(session_id=session_id)
        self.assertIn("state", state)
        self.assertAlmostEqual(state["state"]["vx_mps"], 0.5)

        stopped = self.client.teleop_stop(session_id=session_id)
        self.assertTrue(stopped.get("stopped", False))

        px4_stopped = self.client.px4_stop()
        self.assertIn("status", px4_stopped)
        self.assertFalse(px4_stopped["status"].get("running", True))

