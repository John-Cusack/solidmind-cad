#!/usr/bin/env python3
"""Demo: walk the hexapod using the RL-trained policy in Isaac Sim.

Usage (regular Python — connects to running bridge):
    python3 scripts/demo_rl_walk.py

Or start the bridge first:
    ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh \
        scripts/run_isaac_bridge.sh &
    python3 scripts/demo_rl_walk.py
"""

from __future__ import annotations

import json
import os
import socket
import time


class BridgeConn:
    """Minimal TCP client for Isaac bridge."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9878) -> None:
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None

    def connect(self, timeout: float = 10.0) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock.settimeout(120.0)

    def send(self, cmd: str, args: dict | None = None) -> dict:
        msg = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
        assert self._sock is not None
        self._sock.sendall(msg.encode())
        buf = b""
        while b"\n" not in buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Bridge closed connection")
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])

    def close(self) -> None:
        if self._sock:
            self._sock.close()


def main() -> int:
    urdf_path = os.path.abspath("hexapod_18dof_v2_pkg/Hexapod_18DOF.urdf")
    policy_dir = os.path.abspath("training_runs/hex18_full/deployed")
    policy_pt = os.path.join(policy_dir, "policy.pt")

    if not os.path.isfile(urdf_path):
        print(f"ERROR: URDF not found: {urdf_path}")
        return 1
    if not os.path.isfile(policy_pt):
        print(f"ERROR: Policy not found: {policy_pt}")
        print("Run training first: ../isaacsim/...python.sh -m rl_training.train ...")
        return 1

    print(f"URDF: {urdf_path}")
    print(f"Policy: {policy_dir}")

    # Connect to bridge
    conn = BridgeConn()
    print("Connecting to Isaac bridge on localhost:9878...")
    try:
        conn.connect()
    except (ConnectionRefusedError, OSError) as exc:
        print(f"ERROR: Cannot connect — {exc}")
        print("Start the bridge first:")
        print("  ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh \\")
        print("    scripts/run_isaac_bridge.sh")
        return 1

    resp = conn.send("ping")
    if not resp.get("ok"):
        print("ERROR: Ping failed")
        return 1
    caps = resp.get("result", {}).get("capabilities", {})
    print(f"Connected. Isaac available: {caps.get('isaac_available')}")

    # Minimal mechanism (the URDF defines the actual robot)
    mechanism = {
        "name": "Hexapod_18DOF",
        "parts": [{"id": "robot", "is_ground": False}],
        "joints": [],
        "drives": [],
    }

    # RL direct policy profile
    profile = {
        "controller_type": "rl_direct",
        "policy_path": policy_pt,
        "joint_names": [
            "hip_yaw_L1",
            "hip_pitch_L1",
            "knee_L1",
            "hip_yaw_L2",
            "hip_pitch_L2",
            "knee_L2",
            "hip_yaw_L3",
            "hip_pitch_L3",
            "knee_L3",
            "hip_yaw_R1",
            "hip_pitch_R1",
            "knee_R1",
            "hip_yaw_R2",
            "hip_pitch_R2",
            "knee_R2",
            "hip_yaw_R3",
            "hip_pitch_R3",
            "knee_R3",
        ],
        "leg_joint_names": [
            "hip_yaw_L1",
            "hip_pitch_L1",
            "knee_L1",
            "hip_yaw_L2",
            "hip_pitch_L2",
            "knee_L2",
            "hip_yaw_L3",
            "hip_pitch_L3",
            "knee_L3",
            "hip_yaw_R1",
            "hip_pitch_R1",
            "knee_R1",
            "hip_yaw_R2",
            "hip_pitch_R2",
            "knee_R2",
            "hip_yaw_R3",
            "hip_pitch_R3",
            "knee_R3",
        ],
        "dofs_per_leg": 3,
    }

    print("Starting teleop session with rl_direct controller...")
    resp = conn.send(
        "teleop_start",
        {
            "mechanism": mechanism,
            "profile": profile,
            "urdf_path": urdf_path,
        },
    )
    if not resp.get("ok"):
        err = resp.get("error", {})
        print(f"ERROR: teleop_start failed — {err.get('code', '?')}: {err.get('message', '?')}")
        if "details" in err:
            print(f"  Details: {json.dumps(err['details'], indent=2)}")
        conn.close()
        return 1

    session_id = resp["result"]["session_id"]
    ct = resp["result"].get("controller_type", "?")
    print(f"Session: {session_id}")
    print(f"Controller: {ct}")
    print()

    # Walk forward for 10 seconds
    print("Walking forward at 0.2 m/s for 10 seconds...")
    walk_duration = 10.0
    cmd_hz = 10.0
    t_start = time.time()

    while time.time() - t_start < walk_duration:
        resp = conn.send(
            "teleop_command",
            {
                "session_id": session_id,
                "vx_mps": 0.2,
                "yaw_rate_rps": 0.0,
                "body_height_m": 0.0,
            },
        )
        if not resp.get("ok"):
            print(f"  Command error: {resp.get('error', {}).get('message', '?')}")
            break
        time.sleep(1.0 / cmd_hz)

    # Stop
    print("Stopping...")
    conn.send(
        "teleop_command",
        {
            "session_id": session_id,
            "vx_mps": 0.0,
            "yaw_rate_rps": 0.0,
            "body_height_m": 0.0,
        },
    )
    time.sleep(2.0)

    # Turn in place for 5 seconds
    print("Turning at 0.5 rad/s for 5 seconds...")
    t_start = time.time()
    while time.time() - t_start < 5.0:
        conn.send(
            "teleop_command",
            {
                "session_id": session_id,
                "vx_mps": 0.0,
                "yaw_rate_rps": 0.5,
                "body_height_m": 0.0,
            },
        )
        time.sleep(1.0 / cmd_hz)

    # Stop and hold
    print("Stopping — holding position for 3 seconds...")
    conn.send(
        "teleop_command",
        {
            "session_id": session_id,
            "vx_mps": 0.0,
            "yaw_rate_rps": 0.0,
            "body_height_m": 0.0,
        },
    )
    time.sleep(3.0)

    # End session
    print("Ending session...")
    conn.send("teleop_stop", {"session_id": session_id})
    conn.close()
    print("Done!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
