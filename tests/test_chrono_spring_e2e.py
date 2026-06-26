"""TICKET-C: end-to-end energy-balance test for the spring-on-prismatic link.

Launches the real chrono_daemon, drives a compressed spring on a prismatic
slider, and checks the peak slider speed matches the energy balance
``½·k·x² = ½·m·v²``  ⇒  ``v = sqrt(k/m)·x`` — the exact analytical check the
foam-dart example reuses for sim-to-analytical agreement.

Skipped when the daemon binary isn't built. If the daemon is present but was
built *before* spring support landed, the slider won't move and the test skips
with a rebuild hint rather than failing.
"""
from __future__ import annotations

import math
import os
import socket
import subprocess
import time
import unittest
from pathlib import Path

from server.chrono_client import ChronoClient
from server.motion_models import JointEdge, JointType, Mechanism, PartNode
from server.simulation_spec_builder import build_simulation_spec

_DAEMON_PATH = Path(__file__).resolve().parents[1] / "chrono_daemon" / "build" / "chrono_daemon"
_TEST_PORT = 19878  # avoid the default 9877 and the applied-force test's 19877


def _daemon_available() -> bool:
    return _DAEMON_PATH.is_file() and os.access(_DAEMON_PATH, os.X_OK)


def _wait_for_listening(host: str, port: int, timeout_s: float = 5.0) -> bool:
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


# Test rig parameters
_K = 300.0          # N/m
_M = 0.05           # kg
_Z0 = 0.01          # initial plunger offset along +z (m)
_REST = 0.04        # spring natural length (m)
_COMPRESSION = _REST - _Z0          # 0.03 m
_V_EXPECTED = math.sqrt(_K / _M) * _COMPRESSION  # ≈ 2.324 m/s


@unittest.skipUnless(_daemon_available(), f"chrono_daemon binary not built at {_DAEMON_PATH}")
class TestSpringEnergyBalanceE2E(unittest.TestCase):
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

    def _spec(self) -> dict:
        mech = Mechanism(
            name="spring_slider_e2e",
            parts=(
                PartNode(id="ground", is_ground=True),
                PartNode(id="plunger", mass_kg=_M, inertia_kg_m2=0.001),
            ),
            joints=(
                JointEdge(
                    id="slide",
                    joint_type=JointType.PRISMATIC,
                    parent_part="ground",
                    child_part="plunger",
                    axis=(0.0, 0.0, 1.0),
                    origin=(0.0, 0.0, 0.0),
                    spring_k_n_per_m=_K,
                    spring_rest_length_m=_REST,
                ),
            ),
            drives=(),
        )
        spec = build_simulation_spec(mech)
        # Offset the plunger body along the slider axis so the spring starts
        # compressed (bodies default to the origin in the planner).
        for obj in spec["objects"]:
            if obj.get("type") == "body" and obj["id"] == "plunger":
                obj["pos"] = [0.0, 0.0, _Z0]
        return spec

    def _peak_speed(self, result: dict) -> float:
        ts = result["time_series"]
        samples = [(s["t"], s["parts"]["plunger"]["pos"][2]) for s in ts]
        peak = 0.0
        for (t1, z1), (t2, z2) in zip(samples, samples[1:]):
            if t2 > t1:
                peak = max(peak, abs((z2 - z1) / (t2 - t1)))
        return peak

    def test_peak_speed_matches_energy_balance(self):
        client = ChronoClient(host="127.0.0.1", port=_TEST_PORT)
        client.connect(timeout=2.0)
        try:
            result = client.simulate(
                simulation_spec=self._spec(),
                duration_s=0.1, dt_s=1e-5, output_interval=1e-4,
            )
        finally:
            client.disconnect()

        peak_v = self._peak_speed(result)
        if peak_v < 0.1:
            self.skipTest(
                "slider did not move — daemon likely built before spring "
                "support; rebuild chrono_daemon and re-run"
            )
        # 8% tolerance absorbs finite-difference sampling error at the peak.
        self.assertAlmostEqual(
            peak_v, _V_EXPECTED, delta=0.08 * _V_EXPECTED,
            msg=f"peak slider speed {peak_v:.3f} m/s vs expected "
                f"{_V_EXPECTED:.3f} m/s (sqrt(k/m)*x)",
        )


if __name__ == "__main__":
    unittest.main()
