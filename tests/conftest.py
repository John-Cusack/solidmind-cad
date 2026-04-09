"""Shared test fixtures for simulation engine integration tests."""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from typing import Any, Generator

import unittest


def unused_tcp_port() -> int:
    """Find a free TCP port using bind-and-release."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def mechanism_factory(kind: str = "gear_pair") -> dict[str, Any]:
    """Return a mechanism dict for testing.

    Supported kinds: gear_pair, hexapod, four_bar, planetary.
    """
    if kind == "gear_pair":
        return {
            "name": "test_gear_pair",
            "parts": [
                {"id": "gear_a"},
                {"id": "gear_b"},
                {"id": "frame", "is_ground": True},
            ],
            "joints": [
                {
                    "id": "mesh",
                    "joint_type": "gear_mesh",
                    "parent_part": "gear_a",
                    "child_part": "gear_b",
                    "teeth_parent": 20,
                    "teeth_child": 40,
                    "gear_ratio": 0.5,
                },
                {
                    "id": "rev_a",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "gear_a",
                },
                {
                    "id": "rev_b",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "gear_b",
                },
            ],
            "drives": [
                {"joint_id": "mesh", "speed_rpm": 1000, "torque_nm": 5.0},
            ],
        }

    if kind == "four_bar":
        return {
            "name": "test_four_bar",
            "parts": [
                {"id": "ground", "is_ground": True},
                {"id": "crank"},
                {"id": "coupler"},
                {"id": "rocker"},
            ],
            "joints": [
                {"id": "j1", "joint_type": "revolute", "parent_part": "ground", "child_part": "crank"},
                {"id": "j2", "joint_type": "revolute", "parent_part": "crank", "child_part": "coupler"},
                {"id": "j3", "joint_type": "revolute", "parent_part": "coupler", "child_part": "rocker"},
                {"id": "j4", "joint_type": "revolute", "parent_part": "rocker", "child_part": "ground"},
            ],
            "drives": [
                {"joint_id": "j1", "speed_rpm": 60},
            ],
        }

    if kind == "planetary":
        return {
            "name": "test_planetary",
            "parts": [
                {"id": "frame", "is_ground": True},
                {"id": "sun"},
                {"id": "planet_1"},
                {"id": "planet_2"},
                {"id": "planet_3"},
                {"id": "ring"},
                {"id": "carrier"},
            ],
            "joints": [
                {"id": "sun_rev", "joint_type": "revolute", "parent_part": "frame", "child_part": "sun"},
                {
                    "id": "sp1", "joint_type": "gear_mesh",
                    "parent_part": "sun", "child_part": "planet_1",
                    "teeth_parent": 18, "teeth_child": 9, "gear_ratio": 2.0,
                },
                {
                    "id": "sp2", "joint_type": "gear_mesh",
                    "parent_part": "sun", "child_part": "planet_2",
                    "teeth_parent": 18, "teeth_child": 9, "gear_ratio": 2.0,
                },
                {
                    "id": "sp3", "joint_type": "gear_mesh",
                    "parent_part": "sun", "child_part": "planet_3",
                    "teeth_parent": 18, "teeth_child": 9, "gear_ratio": 2.0,
                },
                {
                    "id": "pr1", "joint_type": "gear_mesh",
                    "parent_part": "planet_1", "child_part": "ring",
                    "teeth_parent": 9, "teeth_child": 36, "gear_ratio": 0.25,
                },
                {"id": "carrier_rev", "joint_type": "revolute", "parent_part": "frame", "child_part": "carrier"},
            ],
            "drives": [
                {"joint_id": "sun_rev", "speed_rpm": 1000, "torque_nm": 2.0},
            ],
        }

    if kind == "hexapod":
        parts: list[dict[str, Any]] = [{"id": "chassis", "is_ground": True}]
        joints: list[dict[str, Any]] = []
        for i in range(6):
            leg = f"leg_{i}"
            parts.append({"id": leg})
            joints.append({
                "id": f"hip_{i}",
                "joint_type": "revolute",
                "parent_part": "chassis",
                "child_part": leg,
                "axis": [0, 0, 1],
            })
        return {
            "name": "test_hexapod",
            "parts": parts,
            "joints": joints,
            "drives": [{"joint_id": "hip_0", "speed_rpm": 30}],
        }

    if kind == "quadrotor":
        # X-frame quadrotor: 4 continuous rotor joints + fixed arm joints
        return {
            "name": "test_quadrotor",
            "parts": [
                {"id": "center_plate", "is_ground": True},
                {"id": "arm_fl"},
                {"id": "arm_fr"},
                {"id": "arm_rl"},
                {"id": "arm_rr"},
                {"id": "motor_fl"},
                {"id": "motor_fr"},
                {"id": "motor_rl"},
                {"id": "motor_rr"},
            ],
            "joints": [
                {"id": "arm_fl_fix", "joint_type": "fixed", "parent_part": "center_plate", "child_part": "arm_fl"},
                {"id": "arm_fr_fix", "joint_type": "fixed", "parent_part": "center_plate", "child_part": "arm_fr"},
                {"id": "arm_rl_fix", "joint_type": "fixed", "parent_part": "center_plate", "child_part": "arm_rl"},
                {"id": "arm_rr_fix", "joint_type": "fixed", "parent_part": "center_plate", "child_part": "arm_rr"},
                {"id": "rotor_fl", "joint_type": "continuous", "parent_part": "arm_fl", "child_part": "motor_fl", "axis": [0, 0, 1]},
                {"id": "rotor_fr", "joint_type": "continuous", "parent_part": "arm_fr", "child_part": "motor_fr", "axis": [0, 0, 1]},
                {"id": "rotor_rl", "joint_type": "continuous", "parent_part": "arm_rl", "child_part": "motor_rl", "axis": [0, 0, 1]},
                {"id": "rotor_rr", "joint_type": "continuous", "parent_part": "arm_rr", "child_part": "motor_rr", "axis": [0, 0, 1]},
            ],
            "drives": [
                {"joint_id": "rotor_fl", "speed_rpm": 8000, "torque_nm": 0.15},
                {"joint_id": "rotor_fr", "speed_rpm": 8000, "torque_nm": 0.15},
                {"joint_id": "rotor_rl", "speed_rpm": 8000, "torque_nm": 0.15},
                {"joint_id": "rotor_rr", "speed_rpm": 8000, "torque_nm": 0.15},
            ],
        }

    if kind == "planetary_2stage":
        # 2-stage planetary gearbox: sun → 3 planets → ring per stage
        # Stage 1: sun1(18T) → planet(27T) → ring1(72T), ratio = 1 + 72/18 = 5.0
        # Stage 2: sun2(16T) → planet(28T) → ring2(72T), ratio = 1 + 72/16 = 5.5
        # Overall: 5.0 × 5.5 = 27.5 (carrier coupling approximation)
        return {
            "name": "test_planetary_2stage",
            "parts": [
                {"id": "housing", "is_ground": True},
                {"id": "input_shaft"},
                {"id": "sun1"},
                {"id": "planet1_a"},
                {"id": "planet1_b"},
                {"id": "planet1_c"},
                {"id": "ring1"},
                {"id": "carrier1"},
                {"id": "sun2"},
                {"id": "planet2_a"},
                {"id": "planet2_b"},
                {"id": "planet2_c"},
                {"id": "ring2"},
                {"id": "carrier2"},
                {"id": "output_shaft"},
            ],
            "joints": [
                # Stage 1
                {"id": "input_rev", "joint_type": "revolute", "parent_part": "housing", "child_part": "input_shaft"},
                {"id": "shaft_sun1", "joint_type": "fixed", "parent_part": "input_shaft", "child_part": "sun1"},
                {
                    "id": "s1_p1a", "joint_type": "gear_mesh",
                    "parent_part": "sun1", "child_part": "planet1_a",
                    "teeth_parent": 18, "teeth_child": 27, "gear_ratio": 18.0 / 27.0,
                },
                {
                    "id": "s1_p1b", "joint_type": "gear_mesh",
                    "parent_part": "sun1", "child_part": "planet1_b",
                    "teeth_parent": 18, "teeth_child": 27, "gear_ratio": 18.0 / 27.0,
                },
                {
                    "id": "s1_p1c", "joint_type": "gear_mesh",
                    "parent_part": "sun1", "child_part": "planet1_c",
                    "teeth_parent": 18, "teeth_child": 27, "gear_ratio": 18.0 / 27.0,
                },
                {
                    "id": "p1a_r1", "joint_type": "gear_mesh",
                    "parent_part": "planet1_a", "child_part": "ring1",
                    "teeth_parent": 27, "teeth_child": 72, "gear_ratio": 27.0 / 72.0,
                },
                {"id": "carrier1_rev", "joint_type": "revolute", "parent_part": "housing", "child_part": "carrier1"},
                # Stage 2 — carrier1 drives sun2
                {"id": "carrier1_sun2", "joint_type": "fixed", "parent_part": "carrier1", "child_part": "sun2"},
                {
                    "id": "s2_p2a", "joint_type": "gear_mesh",
                    "parent_part": "sun2", "child_part": "planet2_a",
                    "teeth_parent": 16, "teeth_child": 28, "gear_ratio": 16.0 / 28.0,
                },
                {
                    "id": "s2_p2b", "joint_type": "gear_mesh",
                    "parent_part": "sun2", "child_part": "planet2_b",
                    "teeth_parent": 16, "teeth_child": 28, "gear_ratio": 16.0 / 28.0,
                },
                {
                    "id": "s2_p2c", "joint_type": "gear_mesh",
                    "parent_part": "sun2", "child_part": "planet2_c",
                    "teeth_parent": 16, "teeth_child": 28, "gear_ratio": 16.0 / 28.0,
                },
                {
                    "id": "p2a_r2", "joint_type": "gear_mesh",
                    "parent_part": "planet2_a", "child_part": "ring2",
                    "teeth_parent": 28, "teeth_child": 72, "gear_ratio": 28.0 / 72.0,
                },
                {"id": "carrier2_rev", "joint_type": "revolute", "parent_part": "housing", "child_part": "carrier2"},
                # Output
                {"id": "carrier2_output", "joint_type": "fixed", "parent_part": "carrier2", "child_part": "output_shaft"},
            ],
            "drives": [
                {"joint_id": "input_rev", "speed_rpm": 3000, "torque_nm": 2.0},
            ],
        }

    if kind == "hexapod_leg":
        # Single hexapod leg: coxa → femur → tibia (3-DOF)
        return {
            "name": "test_hexapod_leg",
            "parts": [
                {"id": "chassis", "is_ground": True},
                {"id": "coxa"},
                {"id": "femur"},
                {"id": "tibia"},
            ],
            "joints": [
                {"id": "hip_yaw", "joint_type": "revolute", "parent_part": "chassis", "child_part": "coxa", "axis": [0, 0, 1]},
                {"id": "hip_pitch", "joint_type": "revolute", "parent_part": "coxa", "child_part": "femur", "axis": [0, 1, 0]},
                {"id": "knee", "joint_type": "revolute", "parent_part": "femur", "child_part": "tibia", "axis": [0, 1, 0]},
            ],
            "drives": [
                {"joint_id": "hip_yaw", "speed_rpm": 30, "torque_nm": 2.5},
                {"joint_id": "hip_pitch", "speed_rpm": 30, "torque_nm": 4.0},
                {"joint_id": "knee", "speed_rpm": 30, "torque_nm": 3.0},
            ],
        }

    if kind == "rc_car_suspension":
        # Simplified RC car: double-wishbone front + solid rear axle
        return {
            "name": "test_rc_car",
            "parts": [
                {"id": "chassis_plate", "is_ground": True},
                {"id": "upper_wishbone_l"},
                {"id": "upper_wishbone_r"},
                {"id": "lower_wishbone_l"},
                {"id": "lower_wishbone_r"},
                {"id": "steering_link_l"},
                {"id": "steering_link_r"},
                {"id": "wheel_hub_fl"},
                {"id": "wheel_hub_fr"},
                {"id": "rear_axle"},
                {"id": "wheel_hub_rl"},
                {"id": "wheel_hub_rr"},
                {"id": "motor_mount"},
            ],
            "joints": [
                # Front left wishbone
                {"id": "uw_l_pivot", "joint_type": "revolute", "parent_part": "chassis_plate", "child_part": "upper_wishbone_l", "axis": [1, 0, 0]},
                {"id": "lw_l_pivot", "joint_type": "revolute", "parent_part": "chassis_plate", "child_part": "lower_wishbone_l", "axis": [1, 0, 0]},
                {"id": "uw_l_hub", "joint_type": "revolute", "parent_part": "upper_wishbone_l", "child_part": "wheel_hub_fl", "axis": [0, 0, 1]},
                {"id": "lw_l_hub", "joint_type": "revolute", "parent_part": "lower_wishbone_l", "child_part": "wheel_hub_fl", "axis": [0, 0, 1]},
                {"id": "steer_l", "joint_type": "revolute", "parent_part": "steering_link_l", "child_part": "wheel_hub_fl", "axis": [0, 0, 1]},
                {"id": "steer_l_chassis", "joint_type": "prismatic", "parent_part": "chassis_plate", "child_part": "steering_link_l", "axis": [1, 0, 0]},
                # Front right wishbone
                {"id": "uw_r_pivot", "joint_type": "revolute", "parent_part": "chassis_plate", "child_part": "upper_wishbone_r", "axis": [1, 0, 0]},
                {"id": "lw_r_pivot", "joint_type": "revolute", "parent_part": "chassis_plate", "child_part": "lower_wishbone_r", "axis": [1, 0, 0]},
                {"id": "uw_r_hub", "joint_type": "revolute", "parent_part": "upper_wishbone_r", "child_part": "wheel_hub_fr", "axis": [0, 0, 1]},
                {"id": "lw_r_hub", "joint_type": "revolute", "parent_part": "lower_wishbone_r", "child_part": "wheel_hub_fr", "axis": [0, 0, 1]},
                {"id": "steer_r", "joint_type": "revolute", "parent_part": "steering_link_r", "child_part": "wheel_hub_fr", "axis": [0, 0, 1]},
                {"id": "steer_r_chassis", "joint_type": "prismatic", "parent_part": "chassis_plate", "child_part": "steering_link_r", "axis": [1, 0, 0]},
                # Rear axle
                {"id": "rear_axle_rev", "joint_type": "revolute", "parent_part": "chassis_plate", "child_part": "rear_axle", "axis": [1, 0, 0]},
                {"id": "rear_l_hub", "joint_type": "revolute", "parent_part": "rear_axle", "child_part": "wheel_hub_rl", "axis": [1, 0, 0]},
                {"id": "rear_r_hub", "joint_type": "revolute", "parent_part": "rear_axle", "child_part": "wheel_hub_rr", "axis": [1, 0, 0]},
                # Motor mount
                {"id": "motor_fix", "joint_type": "fixed", "parent_part": "chassis_plate", "child_part": "motor_mount"},
            ],
            "drives": [
                {"joint_id": "rear_axle_rev", "speed_rpm": 300, "torque_nm": 1.5},
            ],
        }

    raise ValueError(f"Unknown mechanism kind: {kind!r}")


class GazeboStubBridge:
    """Launch a real GazeboBridgeServer with StubGazeboRuntime in a daemon thread.

    Use as a context manager or call start()/stop() manually.
    """

    def __init__(self, port: int) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from gazebo_bridge.bridge_server import GazeboBridgeServer

        self._server = GazeboBridgeServer(
            host=self.host,
            port=self.port,
            runtime_mode="stub",
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="gazebo-stub-bridge",
        )
        self._thread.start()
        # Wait for server to be accepting connections
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
        raise RuntimeError(f"GazeboStubBridge did not start on port {self.port}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> GazeboStubBridge:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
