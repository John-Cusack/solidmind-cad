"""Phase 2 e2e: SolidMind bridge talks MAVLink to a real PX4 SITL.

This test verifies the full bridge → MavlinkController → PX4 wire is
connected against a live PX4 process.  The test does NOT attempt to
arm or fly the drone — that orchestration is Phase 5 work.  It only
proves that:

1. ``Px4Manager`` can attach to an externally-running PX4
2. ``handle_teleop_start`` with ``controller_type="px4_offboard"``
   creates a MavlinkController, connects to PX4, and starts the
   setpoint stream
3. ``handle_teleop_command`` calls reach the MavlinkController and
   update its streaming setpoint without errors
4. ``handle_teleop_stop`` cleans up the MAVLink connection without
   leaking daemon threads

Verifying that the drone actually moves requires arming + OFFBOARD
mode + takeoff, which is added as bridge primitives in Phase 5.

Gating
------
The test is skipped unless ALL of these hold:

- ``SOLIDMIND_RUN_PX4_E2E=1`` is set in the environment
- ``pymavlink`` is importable
- A PX4 SITL process is reachable on UDP 14540 — the operator has run
  ``cd ~/repos/PX4-Autopilot && make px4_sitl gz_x500`` in another
  terminal and left it running

Run with::

    SOLIDMIND_RUN_PX4_E2E=1 python -m unittest tests.test_gazebo_px4_real_runtime
"""

from __future__ import annotations

import os
import socket
import threading
import time
import unittest

try:
    import pymavlink  # noqa: F401

    _HAS_PYMAVLINK = True
except ImportError:
    _HAS_PYMAVLINK = False

from gazebo_bridge.bridge_server import GazeboBridgeServer
from server.gazebo_client import GazeboClient


def _px4_reachable(timeout_s: float = 0.5) -> bool:
    """Probe UDP 14540 by sending a zero-byte datagram and looking for any reply.

    PX4 streams HEARTBEATs at 1 Hz on this port, so a brief listen is
    enough to confirm something is talking MAVLink.  We avoid a full
    pymavlink connection here so the gate stays cheap.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout_s)
        sock.bind(("127.0.0.1", 0))
        sock.sendto(b"", ("127.0.0.1", 14540))
        try:
            data, _ = sock.recvfrom(2048)
            return len(data) > 0
        except (TimeoutError, OSError):
            return False
        finally:
            sock.close()
    except OSError:
        return False


_PX4_E2E_ENABLED = (
    os.environ.get("SOLIDMIND_RUN_PX4_E2E") == "1" and _HAS_PYMAVLINK and _px4_reachable()
)


@unittest.skipUnless(
    _PX4_E2E_ENABLED,
    (
        "Set SOLIDMIND_RUN_PX4_E2E=1, install pymavlink, and run "
        "'make px4_sitl gz_x500' from PX4-Autopilot before this test."
    ),
)
class TestGazeboPx4RealRuntime(unittest.TestCase):
    """Bridge ↔ real PX4 wire-connectivity test."""

    def setUp(self) -> None:
        # Tell Px4Manager to attach to the externally-running PX4 rather
        # than fork its own.  The bridge will still create a
        # MavlinkController per session and connect to UDP 14540.
        self._old_external = os.environ.get("SOLIDMIND_PX4_EXTERNAL")
        os.environ["SOLIDMIND_PX4_EXTERNAL"] = "1"

        self.world_name = os.environ.get("SOLIDMIND_GAZEBO_WORLD", "default")
        self.server = GazeboBridgeServer(
            host="127.0.0.1",
            port=0,
            runtime_mode="real",
            world_name=self.world_name,
            enable_px4=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 5.0
        while self.server.port == 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreater(self.server.port, 0, "bridge failed to bind")

        self.client = GazeboClient(host="127.0.0.1", port=self.server.port)
        self.client.connect(timeout=2.0)

    def tearDown(self) -> None:
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.server.shutdown()
        self.thread.join(timeout=2.0)

        if self._old_external is None:
            os.environ.pop("SOLIDMIND_PX4_EXTERNAL", None)
        else:
            os.environ["SOLIDMIND_PX4_EXTERNAL"] = self._old_external

    # ------------------------------------------------------------------

    def test_bridge_attaches_mavlink_to_running_px4(self) -> None:
        """Bridge teleop_start with px4_offboard connects to real PX4."""
        # 1. Tell the bridge PX4 is running externally.
        px4_status = self.client.px4_start()
        self.assertTrue(
            px4_status.get("status", {}).get("running"),
            f"px4_start failed: {px4_status}",
        )
        self.assertEqual(
            px4_status.get("status", {}).get("mode"),
            "external",
            f"expected external mode, got {px4_status.get('status', {})}",
        )

        # 2. Start a teleop session.  The bridge will spawn a
        #    MavlinkController, connect to PX4 on UDP 14540, and start
        #    the setpoint streamer.  This is the hot path that Phase 2
        #    exists to enable.
        start = self.client.teleop_start(
            mechanism={"name": "x500_external"},
            profile={"controller_type": "px4_offboard"},
            verify=False,
        )
        self.assertEqual(start.get("status"), "started", f"teleop_start: {start}")
        session_id = start["session_id"]

        try:
            # 3. Push a few teleop commands.  These flow through
            #    Px4OffboardController.command_to_setpoint, which calls
            #    MavlinkController.set_velocity, which updates the
            #    streamed setpoint.  PX4 won't act on them (not armed,
            #    not in OFFBOARD), but the wire must accept them
            #    without raising.
            for vx in (0.0, 0.5, 1.0, 0.0):
                resp = self.client.teleop_command(
                    session_id=session_id,
                    vx_mps=vx,
                    vy_mps=0.0,
                    vz_mps=0.0,
                    yaw_rate_rps=0.0,
                )
                self.assertTrue(resp.get("applied"), f"teleop_command rejected: {resp}")

            # 4. Telemetry should report the latest setpoint echo.
            state = self.client.teleop_state(session_id)
            self.assertIn("state", state)
        finally:
            stop = self.client.teleop_stop(session_id)
            self.assertTrue(stop.get("stopped"), f"teleop_stop: {stop}")

        # 5. After stop, the px4 status remains 'running' (we don't own
        #    the external process), but no session lingers.
        diag = self.client.diagnose()
        self.assertEqual(
            diag.get("active_sessions", -1),
            0,
            f"sessions leaked after stop: {diag}",
        )

    def test_teleop_stop_cleans_up_mavlink_threads(self) -> None:
        """teleop_stop must join the MAVLink rx + setpoint threads."""
        self.client.px4_start()
        start = self.client.teleop_start(
            mechanism={"name": "x500_external"},
            profile={"controller_type": "px4_offboard"},
            verify=False,
        )
        session_id = start["session_id"]

        # Count mavlink-* threads before stop.
        def _count_mavlink_threads() -> int:
            return sum(1 for t in threading.enumerate() if t.name.startswith("mavlink-"))

        # Allow the rx + tx threads a moment to spin up.
        time.sleep(0.5)
        active_before = _count_mavlink_threads()
        self.assertGreaterEqual(
            active_before,
            2,
            "expected mavlink-rx and mavlink-tx threads after teleop_start",
        )

        self.client.teleop_stop(session_id)

        # Daemon threads should join cleanly within ~3s; allow a little
        # slack so this isn't flaky on slower CI.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if _count_mavlink_threads() == 0:
                break
            time.sleep(0.1)
        self.assertEqual(
            _count_mavlink_threads(),
            0,
            "MAVLink threads leaked after teleop_stop",
        )


if __name__ == "__main__":
    unittest.main()
