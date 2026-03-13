#!/usr/bin/env python3
"""Load the hexapod URDF in Isaac Sim and display it (non-headless).

Usage (run with regular Python — connects to Isaac bridge via TCP):
    # Terminal 1: start bridge non-headless
    ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh \
        scripts/run_isaac_bridge.sh

    # Terminal 2: send import command
    python3 scripts/show_model_isaac.py
"""
from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path


def send_cmd(sock: socket.socket, cmd: str, args: dict | None = None) -> dict:
    """Send a newline-delimited JSON command and return the response."""
    payload = {"cmd": cmd}
    if args:
        payload["args"] = args
    sock.sendall(json.dumps(payload).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("Bridge closed connection")
        buf += chunk
    return json.loads(buf.split(b"\n", 1)[0])


def main() -> int:
    host = "127.0.0.1"
    port = 9878
    urdf_path = str(Path(__file__).resolve().parent.parent / "hexapod_18dof_v2_pkg" / "Hexapod_18DOF.urdf")

    print(f"Connecting to Isaac bridge at {host}:{port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((host, port))
    except ConnectionRefusedError:
        print(
            "ERROR: Cannot connect to Isaac bridge.\n"
            "Start it first with:\n"
            "  ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh "
            "scripts/run_isaac_bridge.sh"
        )
        return 1

    # Ping
    resp = send_cmd(sock, "ping")
    print(f"Ping: {resp}")

    # Import URDF
    print(f"\nImporting URDF: {urdf_path}")
    resp = send_cmd(sock, "import_urdf", {
        "urdf_path": urdf_path,
        "import_config": {
            "merge_fixed_joints": True,
            "fix_base": False,
        },
    })
    if resp.get("ok"):
        result = resp.get("result", {})
        print(f"OK — prim_path: {result.get('prim_path')}")
        print(f"     joint_count: {result.get('joint_count')}")
        print(f"     link_count: {result.get('link_count')}")
        joint_names = result.get("joint_names", [])
        print(f"     joints ({len(joint_names)}): {joint_names}")
    else:
        err = resp.get("error", {})
        print(f"FAILED: {err.get('code')} — {err.get('message')}")
        sock.close()
        return 1

    # Take a screenshot so it shows in the terminal too
    print("\nTaking screenshot...")
    resp = send_cmd(sock, "screenshot", {
        "width": 1280,
        "height": 720,
        "preset": "iso",
    })
    if resp.get("ok"):
        path = resp["result"].get("path", "")
        print(f"Screenshot saved: {path}")
    else:
        print(f"Screenshot failed: {resp.get('error', {}).get('message')}")

    sock.close()
    print("\nModel is loaded in Isaac Sim GUI. Check the viewport window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
