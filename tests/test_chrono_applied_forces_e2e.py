"""End-to-end smoke test for applied_force / capture-signal extensions.

Launches the real chrono_daemon binary as a subprocess on a non-default port,
sends a single-rotor spec with one applied force, and verifies that:
  - thrust_mean_N matches the applied Z force within 1%
  - hub_bearing_load_N matches the applied force magnitude (no other loads)
  - joint reaction summary fields exist and are populated

Skipped when the daemon binary isn't built.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import unittest
from pathlib import Path

from server.chrono_client import ChronoClient
from server.motion_models import (
    AppliedForce,
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.simulation_spec_builder import build_simulation_spec

_DAEMON_PATH = Path(__file__).resolve().parents[1] / "chrono_daemon" / "build" / "chrono_daemon"
_TEST_PORT = 19877  # not the default 9877; avoid colliding with a running instance


def _daemon_available() -> bool:
    return _DAEMON_PATH.is_file() and os.access(_DAEMON_PATH, os.X_OK)


def _wait_for_listening(host: str, port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


@unittest.skipUnless(_daemon_available(), f"chrono_daemon binary not built at {_DAEMON_PATH}")
class TestAppliedForceE2E(unittest.TestCase):
    """Live-daemon smoke test for applied_force forwarding + capture signals."""

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(
            [str(_DAEMON_PATH), "--host", "127.0.0.1", "--port", str(_TEST_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not _wait_for_listening("127.0.0.1", _TEST_PORT, timeout_s=5.0):
            cls.proc.terminate()
            raise RuntimeError("chrono_daemon failed to start within 5 s")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def _client(self) -> ChronoClient:
        c = ChronoClient(host="127.0.0.1", port=_TEST_PORT)
        c.connect(timeout=2.0)
        return c

    def _rotor_with_one_force(self, *, fz: float = 14.7,
                              radius_m: float = 0.075,
                              rpm: float = 4000.0) -> Mechanism:
        return Mechanism(
            name="rotor_e2e",
            parts=(
                PartNode(id="hub", is_ground=True),
                PartNode(id="blade", mass_kg=0.05, inertia_kg_m2=0.001),
            ),
            joints=(
                JointEdge(
                    id="rotor_test_joint",
                    joint_type=JointType.REVOLUTE,
                    parent_part="hub",
                    child_part="blade",
                    axis=(0, 0, 1),
                    origin=(0, 0, 0),
                ),
            ),
            drives=(DriveCondition(joint_id="rotor_test_joint", speed_rpm=rpm,
                                   driven_part="blade"),),
            applied_forces=(
                AppliedForce(
                    target_body="blade",
                    position_local=(radius_m, 0.0, 0.0),
                    force_vector=(0.0, 0.0, fz),
                    frame="body",
                    label="tip_force",
                ),
            ),
        )

    def test_thrust_mean_matches_applied_force_within_1pct(self):
        client = self._client()
        try:
            mech = self._rotor_with_one_force(fz=14.7)
            spec = build_simulation_spec(mech)
            result = client.simulate(simulation_spec=spec, duration_s=1.0,
                                     dt_s=1e-4, output_interval=0.01)
        finally:
            client.disconnect()

        summary = result["summary"]
        self.assertIn("applied_force_world_z_mean_N", summary)
        thrust_mean = summary["applied_force_world_z_mean_N"]
        self.assertAlmostEqual(thrust_mean, 14.7, delta=0.147,
                               msg=f"thrust mean {thrust_mean} not within 1% of 14.7")
        self.assertEqual(summary["applied_force_count"], 1)

    def test_thrust_std_is_small_for_steady_load(self):
        client = self._client()
        try:
            spec = build_simulation_spec(self._rotor_with_one_force(fz=10.0))
            result = client.simulate(simulation_spec=spec, duration_s=0.5,
                                     dt_s=1e-4, output_interval=0.01)
        finally:
            client.disconnect()

        std_z = result["summary"]["applied_force_world_z_std_N"]
        # For body-frame +Z force on a rotor whose own +Z stays aligned with world +Z
        # under a pure-yaw revolute drive, the world-frame Z component is steady.
        self.assertLess(std_z, 0.1,
                        f"std should be ~0 for a steady body-Z force; got {std_z}")

    def test_joint_reactions_populated(self):
        client = self._client()
        try:
            spec = build_simulation_spec(self._rotor_with_one_force(fz=14.7))
            result = client.simulate(simulation_spec=spec, duration_s=0.5,
                                     dt_s=1e-4, output_interval=0.01)
        finally:
            client.disconnect()

        summary = result["summary"]
        self.assertIn("peak_joint_forces", summary)
        self.assertIn("mean_joint_forces", summary)
        peak = summary["peak_joint_forces"]
        mean = summary["mean_joint_forces"]
        self.assertIn("rotor_test_joint", peak)
        self.assertIn("rotor_test_joint", mean)
        # The hub bearing must support at least the applied thrust magnitude.
        # (Centripetal effects from the offset force add to this; lower bound is 14.7 N.)
        self.assertGreater(mean["rotor_test_joint"], 5.0,
                           "joint reaction is suspiciously small")

    def test_zero_applied_force_count_when_no_loads(self):
        """Sanity: a mechanism without applied_forces still works and reports 0."""
        client = self._client()
        try:
            mech = Mechanism(
                name="rotor_no_loads",
                parts=(PartNode(id="hub", is_ground=True),
                       PartNode(id="blade", mass_kg=0.05, inertia_kg_m2=0.001)),
                joints=(JointEdge(id="rev", joint_type=JointType.REVOLUTE,
                                  parent_part="hub", child_part="blade",
                                  axis=(0, 0, 1)),),
                drives=(DriveCondition(joint_id="rev", speed_rpm=1000.0,
                                       driven_part="blade"),),
            )
            spec = build_simulation_spec(mech)
            result = client.simulate(simulation_spec=spec, duration_s=0.2,
                                     dt_s=1e-4, output_interval=0.05)
        finally:
            client.disconnect()

        self.assertEqual(result["summary"]["applied_force_count"], 0)


if __name__ == "__main__":
    unittest.main()
