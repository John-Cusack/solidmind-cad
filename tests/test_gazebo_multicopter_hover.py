"""Tier 3 end-to-end multicopter hover test against a real Gazebo world.

Builds a minimal 4-rotor drone SDF using the *canonical* Gazebo
``MulticopterMotorModel`` plugin block (one plugin per rotor with
``robotNamespace``, ``jointName``, ``commandSubTopic``, etc.), spawns
it through the SolidMind Gazebo bridge in real-runtime mode, drives
all four motors via ``gz topic --pub`` to the
``gz.msgs.Actuators`` command topic, and asserts the drone has risen
under thrust.

This test deliberately bypasses ``server.sim_export.write_sdf``'s
``drone_config`` path because that path currently emits a non-canonical
plugin block (single plugin with multiple ``<rotor>`` children rather
than one plugin per rotor with the required gz schema). Fixing the
generator is tracked separately; this test isolates the question
"can our bridge spawn a flyable multicopter at all?" from "does our
generator produce a flyable SDF?" so failures are localized.

Gating:
- ``SOLIDMIND_RUN_GAZEBO_E2E=1`` must be set
- ``gz`` CLI must be on PATH
- A ``gz sim`` world must already be running (defaults to ``empty``;
  override with ``SOLIDMIND_GAZEBO_WORLD``). Launch with::

    gz sim -r -s --headless-rendering /usr/share/gz/gz-sim/worlds/empty.sdf

When any of these are missing the whole class is skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from gazebo_bridge.bridge_server import GazeboBridgeServer
from server.gazebo_client import GazeboClient


def _has_gz() -> bool:
    return shutil.which("gz") is not None


def _gz_world_running(world_name: str, timeout_s: float = 5.0) -> bool:
    """Return True iff ``/world/<world_name>/create`` service is advertised.

    ``gz service -l`` does a ~2 s discovery wait internally, so the timeout
    needs comfortable headroom or this gate will always skip the test.
    """
    try:
        out = subprocess.run(
            ["gz", "service", "-l"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return f"/world/{world_name}/create" in out.stdout


_WORLD = os.environ.get("SOLIDMIND_GAZEBO_WORLD", "empty")


@unittest.skipUnless(
    os.environ.get("SOLIDMIND_RUN_GAZEBO_E2E") == "1" and _has_gz() and _gz_world_running(_WORLD),
    (
        "Set SOLIDMIND_RUN_GAZEBO_E2E=1, install gz CLI, and start a "
        "gz sim world (default name 'empty', override via "
        "SOLIDMIND_GAZEBO_WORLD) before running this test."
    ),
)
class TestGazeboMulticopterHover(unittest.TestCase):
    """End-to-end smoke test: bridge spawn + canonical plugin + hover."""

    # Drone physical parameters (chosen so 4 rotors at ~700 rad/s lift the
    # base mass ~2x — comfortable margin over hover).
    _BASE_MASS = 0.6  # kg, central body
    _ROTOR_MASS = 0.05  # kg, each rotor disc
    _ARM_LEN = 0.20  # m, motor offset from origin
    _ROTOR_RADIUS = 0.10  # m, visual rotor disc radius
    _MOTOR_CONSTANT = 8.54858e-06  # N*s^2 (matches stock X3 quad)
    _HOVER_RAD_S = 700.0  # rad/s motor speed (full-throttle equivalent)

    def setUp(self) -> None:
        self.world_name = _WORLD
        self.model_name = f"hover_test_{int(time.time() * 1000) % 100000}"

        # Bring up the bridge in real mode pointed at the running gz world.
        self.server = GazeboBridgeServer(
            host="127.0.0.1",
            port=0,
            runtime_mode="real",
            world_name=self.world_name,
            enable_px4=False,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 5.0
        while self.server.port == 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreater(self.server.port, 0, "Gazebo bridge failed to bind")

        self.client = GazeboClient(host="127.0.0.1", port=self.server.port)
        self.client.connect(timeout=2.0)

        self.sdf_path = self._write_minimal_drone_sdf()
        self._publisher_stop = threading.Event()
        self._publisher_thread: threading.Thread | None = None

    def tearDown(self) -> None:
        if self._publisher_thread is not None:
            self._publisher_stop.set()
            self._publisher_thread.join(timeout=2.0)

        # Best-effort remove the spawned model so repeated runs don't collide.
        try:
            subprocess.run(
                [
                    "gz",
                    "service",
                    "-s",
                    f"/world/{self.world_name}/remove",
                    "--reqtype",
                    "gz.msgs.Entity",
                    "--reptype",
                    "gz.msgs.Boolean",
                    "--timeout",
                    "1000",
                    "--req",
                    f"name: '{self.model_name}', type: MODEL",
                ],
                capture_output=True,
                timeout=3,
                check=False,
            )
        except subprocess.SubprocessError:
            pass

        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.server.shutdown()
        self.thread.join(timeout=2.0)
        try:
            os.unlink(self.sdf_path)
        except OSError:
            pass

    def test_hover_with_canonical_multicopter_plugin(self) -> None:
        """Bridge spawns canonical drone; under throttle, z rises."""
        # 1. Spawn through the bridge — proves the bridge accepts SDFs that
        #    contain plugin blocks, not just bare links.
        spawned = self.client.spawn_model(
            sdf_path=self.sdf_path,
            model_name=self.model_name,
        )
        self.assertTrue(spawned.get("spawned"), f"spawn failed: {spawned}")

        # 2. Settle for half a sim second before reading the baseline pose.
        time.sleep(0.5)
        z_initial = self._read_model_z()
        self.assertIsNotNone(z_initial, "could not read initial pose")

        # 3. Start a background thread that publishes motor speeds at ~20 Hz
        #    (the multicopter motor model expects ongoing commands; one-shot
        #    publishes would let throttles decay back to zero).
        self._publisher_thread = threading.Thread(
            target=self._motor_speed_publisher_loop,
            args=([self._HOVER_RAD_S] * 4,),
            daemon=True,
        )
        self._publisher_thread.start()

        # 4. Let the drone respond to commanded throttle. The first
        #    `gz topic --pub` discovery wait is ~700ms; budget for that
        #    plus several seconds of physics so the lift is unambiguous.
        time.sleep(4.0)

        z_final = self._read_model_z()
        self.assertIsNotNone(z_final, "could not read final pose")

        delta_z = z_final - z_initial
        # 700 rad/s on 4 rotors with motorConstant=8.55e-06 ~= 16.7 N thrust
        # (~2.6× the drone's 6.3 N weight). Even with rotor spin-up time, the
        # drone should rise well over 0.3 m in 2.5 s.
        self.assertGreater(
            delta_z,
            0.3,
            (
                f"drone did not lift under throttle: z went from {z_initial:.3f} "
                f"to {z_final:.3f} (delta {delta_z:+.3f} m). "
                "Either the multicopter plugin failed to load, the spawned "
                "model is malformed, or the command topic is wrong."
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_minimal_drone_sdf(self) -> str:
        """Emit a minimal multicopter SDF with the canonical plugin schema.

        Geometry is intentionally simple boxes/cylinders — no STL meshes —
        so this test has no fixture dependencies.
        """
        ns = self.model_name
        # Per-rotor positions (X, Y) for a quad in plus-config.
        rotors = [
            ("rotor_0", self._ARM_LEN, 0.0, "ccw"),
            ("rotor_1", -self._ARM_LEN, 0.0, "ccw"),
            ("rotor_2", 0.0, self._ARM_LEN, "cw"),
            ("rotor_3", 0.0, -self._ARM_LEN, "cw"),
        ]

        rotor_links = []
        rotor_joints = []
        rotor_plugins = []
        for idx, (name, x, y, direction) in enumerate(rotors):
            rotor_links.append(f"""
    <link name="{name}">
      <pose>{x} {y} 0.02 0 0 0</pose>
      <inertial>
        <mass>{self._ROTOR_MASS}</mass>
        <inertia>
          <ixx>2.5e-5</ixx><iyy>2.5e-5</iyy><izz>5.0e-5</izz>
          <ixy>0</ixy><ixz>0</ixz><iyz>0</iyz>
        </inertia>
      </inertial>
      <visual name="visual">
        <geometry><cylinder><radius>{self._ROTOR_RADIUS}</radius><length>0.005</length></cylinder></geometry>
      </visual>
      <collision name="collision">
        <geometry><cylinder><radius>{self._ROTOR_RADIUS}</radius><length>0.005</length></cylinder></geometry>
      </collision>
    </link>""")
            rotor_joints.append(f"""
    <joint name="{name}_joint" type="revolute">
      <parent>base_link</parent>
      <child>{name}</child>
      <axis>
        <xyz>0 0 1</xyz>
        <limit><lower>-1e16</lower><upper>1e16</upper></limit>
        <dynamics><damping>0.001</damping></dynamics>
      </axis>
    </joint>""")
            rotor_plugins.append(f"""
    <plugin filename="gz-sim-multicopter-motor-model-system"
            name="gz::sim::systems::MulticopterMotorModel">
      <robotNamespace>{ns}</robotNamespace>
      <jointName>{name}_joint</jointName>
      <linkName>{name}</linkName>
      <turningDirection>{direction}</turningDirection>
      <timeConstantUp>0.0125</timeConstantUp>
      <timeConstantDown>0.025</timeConstantDown>
      <maxRotVelocity>1000.0</maxRotVelocity>
      <motorConstant>{self._MOTOR_CONSTANT}</motorConstant>
      <momentConstant>0.016</momentConstant>
      <commandSubTopic>command/motor_speed</commandSubTopic>
      <actuator_number>{idx}</actuator_number>
      <rotorDragCoefficient>8.06428e-05</rotorDragCoefficient>
      <rollingMomentCoefficient>1e-06</rollingMomentCoefficient>
      <motorSpeedPubTopic>motor_speed/{idx}</motorSpeedPubTopic>
      <rotorVelocitySlowdownSim>10</rotorVelocitySlowdownSim>
    </plugin>""")

        sdf = f"""<?xml version="1.0"?>
<sdf version="1.10">
  <model name="{ns}">
    <pose>0 0 0.5 0 0 0</pose>
    <link name="base_link">
      <inertial>
        <mass>{self._BASE_MASS}</mass>
        <inertia>
          <ixx>2.0e-3</ixx><iyy>2.0e-3</iyy><izz>3.5e-3</izz>
          <ixy>0</ixy><ixz>0</ixz><iyz>0</iyz>
        </inertia>
      </inertial>
      <visual name="visual">
        <geometry><box><size>0.20 0.20 0.05</size></box></geometry>
      </visual>
      <collision name="collision">
        <geometry><box><size>0.20 0.20 0.05</size></box></geometry>
      </collision>
    </link>{"".join(rotor_links)}{"".join(rotor_joints)}{"".join(rotor_plugins)}
  </model>
</sdf>
"""
        fd, path = tempfile.mkstemp(suffix=".sdf", prefix="hover_test_")
        with os.fdopen(fd, "w") as f:
            f.write(sdf)
        return path

    def _motor_speed_publisher_loop(self, velocities: list[float]) -> None:
        """Republish gz.msgs.Actuators ~once per second until stop event.

        Each ``gz topic --pub`` invocation does a ~700ms transport-discovery
        wait, so high-frequency loops are not feasible from the CLI. The
        MulticopterMotorModel plugin retains its last received setpoint
        between messages (per ``timeConstantUp``/``timeConstantDown``), so
        one publish is functionally enough to lift the drone.  We
        republish at low rate purely as a robustness measure against
        subscriber re-resolution during the test.
        """
        topic = f"/{self.model_name}/command/motor_speed"
        velocity_lines = "\n".join(f"velocity: {v}" for v in velocities)
        while not self._publisher_stop.is_set():
            try:
                subprocess.run(
                    ["gz", "topic", "-t", topic, "-m", "gz.msgs.Actuators", "-p", velocity_lines],
                    capture_output=True,
                    timeout=2.0,
                    check=False,
                )
            except subprocess.SubprocessError:
                pass
            # Sleep responsively so tearDown can stop us promptly.
            for _ in range(10):
                if self._publisher_stop.is_set():
                    return
                time.sleep(0.1)

    def _read_model_z(self) -> float | None:
        """Return the model's Z position in meters, or None.

        Uses the world-scoped ``dynamic_pose/info`` topic so we don't
        collide with other gz sim worlds running on the same machine
        (gz model -m ... has no --world flag and picks arbitrarily).
        """
        topic = f"/world/{self.world_name}/dynamic_pose/info"
        try:
            out = subprocess.run(
                ["gz", "topic", "-e", "-t", topic, "-n", "1"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
        except subprocess.SubprocessError:
            return None

        # Pose_V message text format contains repeated `pose { ... }` blocks;
        # find the one whose `name:` matches our model and read position.z.
        text = out.stdout
        anchor = f'name: "{self.model_name}"'
        idx = text.find(anchor)
        if idx < 0:
            return None
        # Look ahead for the next "z:" inside a position block.
        block_end = text.find("\npose {", idx)
        if block_end < 0:
            block_end = len(text)
        block = text[idx:block_end]
        # The position block contains "position {\n  x: ...\n  y: ...\n  z: ...\n}"
        pos_idx = block.find("position {")
        if pos_idx < 0:
            return None
        for line in block[pos_idx:].splitlines():
            line = line.strip()
            if line.startswith("z:"):
                try:
                    return float(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
        return None
