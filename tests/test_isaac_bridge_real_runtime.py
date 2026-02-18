"""Optional end-to-end test for the Isaac bridge runtime.

Enabled only when SOLIDMIND_RUN_ISAAC_E2E=1.
"""
from __future__ import annotations

import os
import threading
import time
import unittest

from isaac_bridge.bridge_server import BridgeServer
from server.isaac_client import IsaacClient


@unittest.skipUnless(
    os.environ.get("SOLIDMIND_RUN_ISAAC_E2E") == "1",
    "Set SOLIDMIND_RUN_ISAAC_E2E=1 to run Isaac bridge runtime e2e test.",
)
class TestIsaacBridgeRealRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BridgeServer(host="127.0.0.1", port=0, headless=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 5.0
        while self.server.port == 0 and time.time() < deadline:
            time.sleep(0.01)
        if self.server.port == 0:
            self.fail("Bridge server failed to bind to an ephemeral port")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2.0)

    def test_simulate_and_teleop_lifecycle(self) -> None:
        client = IsaacClient(host="127.0.0.1", port=self.server.port)
        client.connect(timeout=2.0)

        mechanism = {
            "name": "e2e_mech",
            "parts": [{"id": "frame", "is_ground": True}, {"id": "link_a"}],
            "joints": [
                {
                    "id": "joint_a",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "link_a",
                }
            ],
            "drives": [{"joint_id": "joint_a", "speed_rpm": 180.0}],
        }
        sim = client.simulate(
            mechanism=mechanism,
            duration_s=0.2,
            dt_s=0.01,
            output_interval=0.05,
            profile={"mode": "test"},
        )
        self.assertIn("summary", sim)
        self.assertGreater(len(sim.get("time_series", [])), 0)

        started = client.teleop_start(mechanism=mechanism, profile={"speed": 0.4})
        session_id = started["session_id"]
        self.assertTrue(session_id)

        applied = client.teleop_command(
            session_id,
            vx_mps=0.4,
            yaw_rate_rps=0.1,
            body_height_m=0.02,
        )
        self.assertTrue(applied["applied"])

        state = client.teleop_state(session_id)
        self.assertAlmostEqual(state["state"]["vx_mps"], 0.4)

        stopped = client.teleop_stop(session_id)
        self.assertTrue(stopped["stopped"])

        client.disconnect()


if __name__ == "__main__":
    unittest.main()
