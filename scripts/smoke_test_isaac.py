#!/usr/bin/env python3
"""Standalone smoke-test client for the Isaac bridge server.

Connects via TCP, sends commands, and prints responses.  No Claude Code
or MCP server needed — just the bridge running on localhost.

Usage examples::

    # Ping the bridge
    python3 scripts/smoke_test_isaac.py

    # Import a URDF and diagnose the prim tree
    python3 scripts/smoke_test_isaac.py --urdf hexapod_sim_pkg/Hexapod_v2_1DOF.urdf

    # Import URDF + capture viewport screenshot
    python3 scripts/smoke_test_isaac.py --urdf hexapod_sim_pkg/Hexapod_v2_1DOF.urdf --screenshot

    # Diagnose a specific prim path
    python3 scripts/smoke_test_isaac.py --diagnose-path /Hexapod_v2_1DOF

    # Send a reload command
    python3 scripts/smoke_test_isaac.py --reload
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
from typing import Any


def _send_command(
    host: str,
    port: int,
    cmd: str,
    args: dict[str, Any] | None = None,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Send a single command to the bridge and return the parsed response."""
    payload = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
    with socket.create_connection((host, port), timeout=10) as sock:
        # Use a longer timeout for recv — URDF import can take minutes
        sock.settimeout(timeout)
        sock.sendall(payload.encode("utf-8"))
        # Read until newline
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    return json.loads(line)


def _print_response(label: str, resp: dict[str, Any]) -> None:
    ok = resp.get("ok", False)
    status = "OK" if ok else "ERROR"
    print(f"\n{'=' * 60}")
    print(f"[{status}] {label}")
    print("=" * 60)
    print(json.dumps(resp, indent=2, default=str))


def _print_prim_tree(prims: list[dict[str, Any]]) -> None:
    """Pretty-print the prim tree from a diagnose response."""
    print(f"\nPrim tree ({len(prims)} prims):")
    print("-" * 60)
    for p in prims:
        path = p.get("path", "?")
        type_name = p.get("type", "")
        schemas = p.get("applied_schemas", [])
        indent = "  " * (path.count("/") - 1)
        name = path.rsplit("/", 1)[-1] or path
        schema_str = f"  schemas=[{', '.join(schemas)}]" if schemas else ""
        print(f"{indent}{name}  ({type_name}){schema_str}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Smoke-test the Isaac bridge server",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bridge host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9878,
        help="Bridge port (default: 9878)",
    )
    parser.add_argument(
        "--urdf",
        help="URDF file to import (triggers import_urdf + diagnose)",
    )
    parser.add_argument(
        "--diagnose-path",
        help="Prim path to diagnose (default: / or imported root)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Send a reload command",
    )
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="Capture a viewport screenshot and save to isaac_screenshot.png",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Socket timeout in seconds (default: 120)",
    )
    args = parser.parse_args(argv)

    # 1. Ping
    print(f"Connecting to {args.host}:{args.port}...")
    try:
        resp = _send_command(args.host, args.port, "ping", timeout=args.timeout)
    except ConnectionRefusedError:
        print(f"ERROR: Cannot connect to {args.host}:{args.port} — is the bridge running?")
        sys.exit(1)
    _print_response("ping", resp)

    if not resp.get("ok"):
        print("Ping failed — aborting.")
        sys.exit(1)

    isaac_available = resp.get("result", {}).get("capabilities", {}).get("isaac_available", False)
    print(f"\nIsaac available: {isaac_available}")

    # 2. Reload (if requested)
    if args.reload:
        resp = _send_command(args.host, args.port, "reload", timeout=args.timeout)
        _print_response("reload", resp)

    # 3. Import URDF (if provided)
    prim_path: str | None = None
    if args.urdf:
        urdf_abs = os.path.abspath(args.urdf)
        print(f"\nImporting URDF: {urdf_abs} (this can take a while)...")
        resp = _send_command(
            args.host,
            args.port,
            "import_urdf",
            {"urdf_path": urdf_abs},
            timeout=args.timeout,
        )
        _print_response("import_urdf", resp)
        if resp.get("ok"):
            result = resp["result"]
            prim_path = result.get("prim_path")
            print(f"\n  prim_path:   {prim_path}")
            print(f"  joint_count: {result.get('joint_count')}")
            print(f"  link_count:  {result.get('link_count')}")

    # 4. Diagnose
    diagnose_path = args.diagnose_path or prim_path
    if diagnose_path or args.urdf:
        diag_args: dict[str, Any] = {}
        if diagnose_path:
            diag_args["prim_path"] = diagnose_path
        resp = _send_command(
            args.host,
            args.port,
            "diagnose",
            diag_args,
            timeout=args.timeout,
        )
        _print_response("diagnose", resp)
        if resp.get("ok") and resp.get("result", {}).get("prims"):
            _print_prim_tree(resp["result"]["prims"])
            print(f"\nType counts: {json.dumps(resp['result'].get('type_counts', {}), indent=2)}")

    # 5. Screenshot (if requested)
    if args.screenshot:
        print("\nCapturing viewport screenshot...")
        resp = _send_command(
            args.host,
            args.port,
            "screenshot",
            {"width": 1280, "height": 720},
            timeout=args.timeout,
        )
        _print_response(
            "screenshot",
            {
                "ok": resp.get("ok"),
                "result": {
                    k: v for k, v in (resp.get("result") or {}).items() if k != "image_base64"
                }
                if resp.get("ok")
                else resp.get("error"),
            },
        )
        if resp.get("ok") and resp.get("result", {}).get("image_base64"):
            out_path = "isaac_screenshot.png"
            png_data = base64.b64decode(resp["result"]["image_base64"])
            with open(out_path, "wb") as f:
                f.write(png_data)
            print(f"\n  Saved {len(png_data)} bytes to {out_path}")
        elif not resp.get("ok"):
            print("  Screenshot failed (see error above)")

    print("\nDone.")


if __name__ == "__main__":
    main()
