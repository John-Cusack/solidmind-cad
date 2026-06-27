#!/usr/bin/env python3
"""Deployment test for trained RL policy on Isaac Sim.

Connects to a running Isaac bridge (non-headless), starts a teleop
session with the rl_direct controller, sends forward velocity commands,
and monitors joint positions to verify the hexapod is moving.

Usage:
    # First, start the bridge non-headless:
    ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh \
        scripts/run_isaac_bridge.sh

    # Then run this test:
    python3 scripts/test_policy_deploy.py
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 9878
TIMEOUT = 120.0

POLICY_DIR = Path("training_runs/hex18_fixed_rewards/deployed")
URDF_PATH = Path("hexapod_18dof_v3_pkg/hexapod_18dof.urdf").resolve()


def send(cmd: str, args: dict | None = None) -> dict:
    """Send a command to the Isaac bridge and return the response."""
    payload = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
    with socket.create_connection((HOST, PORT), timeout=10) as sock:
        sock.settimeout(TIMEOUT)
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0])


def main() -> int:
    print("=" * 60)
    print("Policy Deployment Test")
    print("=" * 60)

    # Verify policy exists
    policy_path = POLICY_DIR / "policy.pt"
    config_path = POLICY_DIR / "deployment_config.json"
    if not policy_path.is_file():
        print(f"ERROR: Policy not found at {policy_path}")
        return 1
    print(f"Policy: {policy_path}")
    print(f"Config: {config_path}")

    deploy_cfg = json.loads(config_path.read_text())
    print(f"  action_scale: {deploy_cfg.get('action_scale')}")
    print(f"  normalized_policy: {deploy_cfg.get('normalized_policy')}")
    print(f"  default_joint_positions: {deploy_cfg.get('default_joint_positions', [])[:3]}...")
    print()

    # 1. Ping bridge
    print("[1/5] Pinging Isaac bridge...")
    try:
        resp = send("ping")
    except ConnectionRefusedError:
        print(f"ERROR: Cannot connect to {HOST}:{PORT}")
        print("Start the bridge first:")
        print("  ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh \\")
        print("    scripts/run_isaac_bridge.sh")
        return 1

    if not resp.get("ok"):
        print(f"ERROR: Ping failed: {resp}")
        return 1
    print("  Bridge is running.")

    # 2. Start teleop with rl_direct controller
    print("\n[2/5] Starting teleop session (rl_direct controller)...")
    joint_names = deploy_cfg["joint_names"]

    # Split joint names into left/right for the profile
    left_joints = [n for n in joint_names if "_L" in n]
    right_joints = [n for n in joint_names if "_R" in n]
    # Tripod A/B: alternating legs (L1, L3, R2 vs L2, R1, R3)
    tripod_a = [n for n in joint_names if any(s in n for s in ("_L1", "_L3", "_R2"))]
    tripod_b = [n for n in joint_names if any(s in n for s in ("_L2", "_R1", "_R3"))]

    profile = {
        "controller_type": "rl_direct",
        "joint_names": joint_names,
        "leg_joint_names": joint_names,
        "left_legs": left_joints,
        "right_legs": right_joints,
        "tripod_a": tripod_a,
        "tripod_b": tripod_b,
        "policy_path": str(policy_path.resolve()),
        "alpha": deploy_cfg.get("action_scale", 0.5),
        "vx_max_mps": 0.5,
        "yaw_max_rps": 1.0,
        "height_max_m": 0.05,
        "slew_vx_mps2": 1.0,
        "slew_yaw_rps2": 2.0,
        "slew_height_mps2": 0.1,
        "leg_phase_offsets": [0.0, 0.5, 0.0, 0.5, 0.0, 0.5],
    }

    # Build a minimal mechanism for teleop_start
    mechanism = {
        "name": "hexapod_18dof_deploy_test",
        "parts": [{"id": "base", "type": "ground"}],
        "joints": [],
    }

    resp = send("teleop_start", {
        "mechanism": mechanism,
        "profile": profile,
        "urdf_path": str(URDF_PATH),
        "import_config": {
            "robot_type": "mobile",
            "fix_base": False,
            "merge_fixed_joints": True,
            "default_drive_stiffness": 400.0,
            "default_drive_damping": 30.0,
            "spawn_height": 0.18,
        },
    })

    if not resp.get("ok"):
        print(f"ERROR: teleop_start failed: {json.dumps(resp, indent=2)}")
        return 1

    result = resp["result"]
    session_id = result["session_id"]
    print(f"  Session: {session_id}")
    print(f"  Controller: {result.get('controller_type', '?')}")
    print(f"  Joints resolved: {result.get('joints_resolved', '?')}")

    # 3. Let it settle (stand) for 2 seconds with zero commands
    print("\n[3/5] Settling (zero velocity, 2s)...")
    time.sleep(2.0)

    resp = send("teleop_state", {"session_id": session_id})
    if resp.get("ok"):
        state = resp["result"]
        print(f"  Ticks: {state.get('tick_count', 0)}")
        targets = state.get("last_joint_targets_rad", {})
        if targets:
            vals = list(targets.values())
            print(f"  Joint targets (first 3): {[f'{v:.3f}' for v in vals[:3]]}")
            all_zero = all(abs(v) < 0.001 for v in vals)
            print(f"  All near zero: {all_zero}")
            if all_zero:
                print("  WARNING: Joint targets are all ~0 — policy may not be loading correctly")

    # 4. Send forward velocity command
    print("\n[4/5] Sending vx=0.2 m/s for 5 seconds...")
    resp = send("teleop_command", {
        "session_id": session_id,
        "vx_mps": 0.2,
        "yaw_rate_rps": 0.0,
        "body_height_m": 0.0,
    })
    if not resp.get("ok"):
        print(f"  WARNING: teleop_command failed: {resp}")

    # Monitor joint state every second
    for t in range(5):
        time.sleep(1.0)
        resp = send("teleop_state", {"session_id": session_id})
        if resp.get("ok"):
            state = resp["result"]
            targets = state.get("last_joint_targets_rad", {})
            vals = list(targets.values())
            tick = state.get("tick_count", 0)
            clamps = state.get("limit_clamp_count", 0)
            # Check if joints are actually moving (not all the same)
            if vals:
                spread = max(vals) - min(vals)
                print(
                    f"  t={t+1}s | ticks={tick} | clamps={clamps} | "
                    f"joint_spread={spread:.3f} rad | "
                    f"targets[:3]=[{', '.join(f'{v:.3f}' for v in vals[:3])}]"
                )

    # 5. Final check and stop
    print("\n[5/5] Stopping teleop...")
    resp = send("teleop_state", {"session_id": session_id})
    if resp.get("ok"):
        state = resp["result"]
        targets = state.get("last_joint_targets_rad", {})
        vals = list(targets.values())
        all_near_zero = all(abs(v) < 0.01 for v in vals) if vals else True
        some_nonzero = any(abs(v) > 0.05 for v in vals) if vals else False

        print("\n  Final joint targets:")
        for name, val in list(targets.items())[:6]:
            print(f"    {name}: {val:.4f} rad")
        if len(targets) > 6:
            print(f"    ... ({len(targets) - 6} more)")

        print()
        if all_near_zero:
            print("  RESULT: FAIL — All joints near zero. Policy not producing meaningful outputs.")
            print("  Check: Is default_joint_positions loaded? Is the policy normalized correctly?")
        elif some_nonzero:
            print("  RESULT: PASS — Joints are producing non-zero targets!")
            print("  Visual check: Look at the Isaac Sim viewport to confirm walking motion.")
        else:
            print("  RESULT: UNCLEAR — Joints have small but nonzero values. Visual check needed.")

    resp = send("teleop_stop", {"session_id": session_id})
    if resp.get("ok"):
        print("  Session stopped.")
    else:
        print(f"  Stop failed: {resp}")

    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
