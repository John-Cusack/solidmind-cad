#!/usr/bin/env python3
"""Real integration test: import URDF into Isaac Sim and validate assembly.

Usage:
    python3 scripts/test_urdf_import.py <urdf_path> [--output-dir DIR] [--pose-test]

Requires Isaac bridge running on localhost:9878.
Start it with: scripts/run_isaac_bridge.sh

This is a standalone script (NOT unittest) meant for interactive development.
It imports a URDF, counts joints/links, takes screenshots, and optionally
runs pose tests to verify the robot assembles correctly.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time


def _send_command(sock: socket.socket, cmd: dict) -> dict:
    """Send a JSON command and read the response."""
    payload = json.dumps(cmd) + "\n"
    sock.sendall(payload.encode("utf-8"))

    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Bridge closed connection")
        buf += chunk
    return json.loads(buf.decode("utf-8").strip())


def connect_bridge(host: str = "localhost", port: int = 9878, timeout: float = 5.0) -> socket.socket:
    """Connect to the Isaac bridge, fail fast if not running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except (ConnectionRefusedError, OSError) as exc:
        print(f"ERROR: Cannot connect to Isaac bridge at {host}:{port}")
        print(f"  Start it with: scripts/run_isaac_bridge.sh")
        print(f"  ({exc})")
        sys.exit(1)

    # Verify with ping
    resp = _send_command(sock, {"cmd": "ping"})
    if not resp.get("ok"):
        print(f"ERROR: Bridge ping failed: {resp}")
        sys.exit(1)
    print(f"Connected to Isaac bridge (version: {resp.get('version', '?')})")
    return sock


def import_urdf(sock: socket.socket, urdf_path: str, config: dict | None = None) -> dict:
    """Import a URDF file into Isaac Sim."""
    cmd: dict = {
        "cmd": "simulate",
        "urdf_path": os.path.abspath(urdf_path),
        "duration_s": 0.01,  # Minimal sim just to import
        "dt_s": 0.001,
    }
    if config:
        cmd["import_config"] = config

    print(f"Importing URDF: {urdf_path}")
    resp = _send_command(sock, cmd)
    if not resp.get("ok"):
        print(f"ERROR: URDF import failed: {resp.get('error', resp)}")
        sys.exit(1)
    return resp


def take_screenshots(
    sock: socket.socket,
    output_dir: str,
    prefix: str = "urdf_test",
) -> list[str]:
    """Take screenshots from 4 angles, save to output_dir."""
    os.makedirs(output_dir, exist_ok=True)
    views = [
        ("iso", {"camera_position": [0.5, -0.5, 0.4], "camera_target": [0, 0, 0.15]}),
        ("front", {"camera_position": [0.6, 0, 0.15], "camera_target": [0, 0, 0.15]}),
        ("top", {"camera_position": [0, 0, 0.8], "camera_target": [0, 0, 0.15]}),
        ("side", {"camera_position": [0, -0.6, 0.15], "camera_target": [0, 0, 0.15]}),
    ]
    saved: list[str] = []
    for view_name, cam_params in views:
        cmd = {"cmd": "screenshot", **cam_params}
        resp = _send_command(sock, cmd)
        if resp.get("ok") and resp.get("image_path"):
            src = resp["image_path"]
            dst = os.path.join(output_dir, f"{prefix}_{view_name}.png")
            # Copy the file (bridge saves to temp)
            if os.path.exists(src):
                import shutil
                shutil.copy2(src, dst)
                saved.append(dst)
                print(f"  Screenshot: {dst}")
            else:
                print(f"  WARNING: Screenshot file not found: {src}")
        else:
            print(f"  WARNING: Screenshot '{view_name}' failed: {resp.get('error', '?')}")
    return saved


def set_joint_positions(
    sock: socket.socket,
    positions: dict[str, float],
) -> dict:
    """Set joint positions (radians) via teleop or direct command."""
    cmd = {
        "cmd": "set_joint_positions",
        "positions": positions,
    }
    return _send_command(sock, cmd)


def validate_import(result: dict, expected_joints: int = 18, expected_links: int = 19) -> bool:
    """Check import results against expected counts."""
    ok = True
    joint_count = result.get("joint_count", result.get("dof_count", 0))
    link_count = result.get("link_count", 0)

    print(f"\n--- Import Validation ---")
    print(f"  Joints: {joint_count} (expected: {expected_joints})")
    print(f"  Links:  {link_count} (expected: >= {expected_links})")

    if joint_count != expected_joints:
        print(f"  FAIL: Joint count mismatch")
        ok = False
    if link_count < expected_links:
        print(f"  FAIL: Too few links")
        ok = False

    # Print joint details if available
    joint_names = result.get("joint_names", [])
    if joint_names:
        print(f"\n  Joint names ({len(joint_names)}):")
        for jn in sorted(joint_names):
            print(f"    - {jn}")

    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Test URDF import in Isaac Sim")
    parser.add_argument("urdf_path", help="Path to URDF file")
    parser.add_argument("--output-dir", default="./urdf_test_output",
                        help="Directory for screenshots (default: ./urdf_test_output)")
    parser.add_argument("--pose-test", action="store_true",
                        help="Run pose test (zero pose + standing pose)")
    parser.add_argument("--expected-joints", type=int, default=18,
                        help="Expected number of revolute joints (default: 18)")
    parser.add_argument("--expected-links", type=int, default=19,
                        help="Expected minimum link count (default: 19)")
    args = parser.parse_args()

    if not os.path.exists(args.urdf_path):
        print(f"ERROR: URDF file not found: {args.urdf_path}")
        sys.exit(1)

    print(f"=== URDF Import Test ===")
    print(f"URDF: {args.urdf_path}")
    print(f"Output: {args.output_dir}")
    print()

    sock = connect_bridge()

    try:
        # Import URDF
        result = import_urdf(sock, args.urdf_path, config={
            "fix_base": True,
            "merge_fixed_joints": False,
        })

        # Validate counts
        passed = validate_import(result, args.expected_joints, args.expected_links)

        # Take screenshots
        print(f"\n--- Screenshots (default pose) ---")
        take_screenshots(sock, args.output_dir, prefix="default")

        # Pose test
        if args.pose_test:
            print(f"\n--- Zero Pose ---")
            # Build zero position dict
            joint_names = result.get("joint_names", [])
            if joint_names:
                zero_pos = {jn: 0.0 for jn in joint_names}
                set_joint_positions(sock, zero_pos)
                time.sleep(0.5)
                take_screenshots(sock, args.output_dir, prefix="zero_pose")

                print(f"\n--- Standing Pose ---")
                import math
                standing_pos = {}
                for jn in joint_names:
                    if "femur" in jn.lower():
                        standing_pos[jn] = math.radians(-50)
                    elif "tibia" in jn.lower():
                        standing_pos[jn] = math.radians(115)
                    else:
                        standing_pos[jn] = 0.0
                set_joint_positions(sock, standing_pos)
                time.sleep(0.5)
                take_screenshots(sock, args.output_dir, prefix="standing")

        # Final report
        print(f"\n{'='*40}")
        if passed:
            print(f"RESULT: PASS")
        else:
            print(f"RESULT: FAIL")
        print(f"Screenshots saved to: {args.output_dir}")
        print(f"{'='*40}")

        sys.exit(0 if passed else 1)

    finally:
        sock.close()


if __name__ == "__main__":
    main()
