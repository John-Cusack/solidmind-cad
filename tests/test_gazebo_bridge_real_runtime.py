"""Optional end-to-end tests for Gazebo bridge real runtime."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest

from gazebo_bridge.bridge_server import GazeboBridgeServer
from server.gazebo_client import GazeboClient


def _has_gz() -> bool:
    return shutil.which("gz") is not None


@unittest.skipUnless(
    os.environ.get("SOLIDMIND_RUN_GAZEBO_E2E") == "1" and _has_gz(),
    "Set SOLIDMIND_RUN_GAZEBO_E2E=1 and install Gazebo CLI (gz) to run this test.",
)
class TestGazeboBridgeRealRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.server = GazeboBridgeServer(
            host="127.0.0.1",
            port=0,
            runtime_mode="real",
            world_name=os.environ.get("SOLIDMIND_GAZEBO_WORLD", "default"),
            enable_px4=False,
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
  <model name="e2e_quad">
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
        try:
            os.unlink(self.sdf_path)
        except OSError:
            pass

    def test_spawn_and_batch_simulate(self) -> None:
        diag = self.client.diagnose()
        self.assertTrue(diag.get("connected", False))

        spawned = self.client.spawn_model(sdf_path=self.sdf_path, model_name="e2e_quad")
        self.assertTrue(spawned.get("spawned"))

        sim = self.client.simulate(
            mechanism={"name": "e2e", "parts": [{"id": "base"}], "joints": [], "drives": []},
            duration_s=0.2,
            dt_s=0.02,
            output_interval=0.1,
            sdf_path=self.sdf_path,
            profile={},
        )
        self.assertIn("time_series", sim)
        self.assertGreater(len(sim["time_series"]), 0)

