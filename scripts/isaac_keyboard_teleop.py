#!/usr/bin/env python3
"""Keyboard teleop client for the Isaac bridge.

Connects to a running Isaac bridge, starts (or attaches to) a teleop
session, and maps keyboard input to teleop commands at 20 Hz.

Controls::

    W / S   — forward / backward
    A / D   — turn left / turn right
    Q / E   — body height up / down
    Space   — stop all (zero command)
    Esc     — quit (sends teleop_stop)

Requires a running Isaac bridge (``scripts/run_isaac_bridge.sh``) and
an active teleop session started via ``motion.teleop_start``, OR pass
``--mechanism`` to auto-start one.

Usage::

    # Attach to an existing session
    python3 scripts/isaac_keyboard_teleop.py --session sess_abc123

    # Auto-start with a mechanism JSON file
    python3 scripts/isaac_keyboard_teleop.py --mechanism mechanism.json

    # Auto-start with a URDF (creates minimal mechanism)
    python3 scripts/isaac_keyboard_teleop.py --urdf hexapod_sim_pkg/Hexapod_v2_1DOF.urdf
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import termios
import time
import tty
from typing import Any

# ── TCP helpers ──────────────────────────────────────────────────────


class BridgeConnection:
    """Persistent TCP connection to the Isaac bridge."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._buf = b""

    def connect(self, timeout: float = 5.0) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=timeout)
        sock.settimeout(5.0)
        self._sock = sock
        self._buf = b""

    def send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._sock is not None
        payload = json.dumps({"cmd": cmd, "args": args or {}}, separators=(",", ":")) + "\n"
        self._sock.sendall(payload.encode("utf-8"))
        # Read until newline
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Bridge closed connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ── Terminal raw-mode helpers ────────────────────────────────────────


class RawTerminal:
    """Context manager for raw terminal mode (no echo, char-by-char)."""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._old_settings: list[Any] | None = None

    def __enter__(self) -> RawTerminal:
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)

    def read_key(self, timeout: float = 0.05) -> str | None:
        """Non-blocking key read. Returns char or None if no input."""
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if ready:
            ch = sys.stdin.read(1)
            # Handle escape sequences (arrow keys etc.)
            if ch == "\x1b":
                # Read remaining bytes of escape sequence
                more, _, _ = select.select([self._fd], [], [], 0.01)
                if more:
                    sys.stdin.read(1)  # [
                    sys.stdin.read(1)  # A/B/C/D
                return "ESC"
            return ch
        return None


# ── Key mapping ──────────────────────────────────────────────────────

_VX_STEP = 0.1  # m/s per key press
_YAW_STEP = 0.2  # rad/s per key press
_HEIGHT_STEP = 0.005  # m per key press
_SEND_HZ = 20
_SEND_INTERVAL = 1.0 / _SEND_HZ


def _apply_key(
    key: str,
    vx: float,
    yaw: float,
    height: float,
    vx_max: float,
    yaw_max: float,
    height_max: float,
) -> tuple[float, float, float, bool]:
    """Apply a key press. Returns (vx, yaw, height, should_quit)."""
    k = key.lower()
    if k == "w":
        vx = min(vx + _VX_STEP, vx_max)
    elif k == "s":
        vx = max(vx - _VX_STEP, -vx_max)
    elif k == "a":
        yaw = min(yaw + _YAW_STEP, yaw_max)
    elif k == "d":
        yaw = max(yaw - _YAW_STEP, -yaw_max)
    elif k == "q":
        height = min(height + _HEIGHT_STEP, height_max)
    elif k == "e":
        height = max(height - _HEIGHT_STEP, -height_max)
    elif k == " ":
        vx, yaw, height = 0.0, 0.0, 0.0
    elif k == "\x03" or key == "ESC":  # Ctrl-C or Esc
        return vx, yaw, height, True
    return vx, yaw, height, False


# ── HUD ──────────────────────────────────────────────────────────────


def _draw_hud(vx: float, yaw: float, height: float, tick: int, ok: bool) -> None:
    """Redraw the single-line HUD."""
    status = "OK" if ok else "!!"
    sys.stdout.write(
        f"\r[{status}] vx={vx:+.2f} m/s  yaw={yaw:+.2f} rad/s  h={height:+.3f} m  tick={tick}    "
    )
    sys.stdout.flush()


# ── Main loop ────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Keyboard teleop for Isaac bridge")
    parser.add_argument("--host", default="127.0.0.1", help="Bridge host")
    parser.add_argument("--port", type=int, default=9878, help="Bridge port")
    parser.add_argument("--session", help="Existing teleop session ID to attach to")
    parser.add_argument("--mechanism", help="Mechanism JSON file (auto-starts session)")
    parser.add_argument("--urdf", help="URDF file (auto-starts session with minimal mechanism)")
    parser.add_argument("--profile", help="Profile JSON file (optional, for auto-start)")
    parser.add_argument("--import-config", help="Import config JSON file (optional)")
    parser.add_argument("--vx-max", type=float, default=0.5, help="Max forward velocity (m/s)")
    parser.add_argument("--yaw-max", type=float, default=1.0, help="Max yaw rate (rad/s)")
    parser.add_argument("--height-max", type=float, default=0.03, help="Max body height (m)")
    args = parser.parse_args(argv)

    # Connect
    conn = BridgeConnection(args.host, args.port)
    print(f"Connecting to {args.host}:{args.port}...")
    try:
        conn.connect()
    except (ConnectionRefusedError, OSError) as exc:
        print(f"ERROR: Cannot connect — {exc}")
        print("Is the Isaac bridge running? (scripts/run_isaac_bridge.sh)")
        sys.exit(1)

    resp = conn.send("ping")
    if not resp.get("ok"):
        print("ERROR: Ping failed")
        sys.exit(1)
    print(
        "Connected. Isaac available:",
        resp.get("result", {}).get("capabilities", {}).get("isaac_available"),
    )

    # Resolve or create session
    session_id = args.session
    owns_session = False

    if session_id is None:
        # Auto-start session
        start_args: dict[str, Any] = {}

        if args.mechanism:
            with open(args.mechanism) as f:
                start_args["mechanism"] = json.load(f)
        elif args.urdf:
            start_args["mechanism"] = {
                "name": "keyboard_teleop",
                "parts": [{"id": "robot", "is_ground": False}],
                "joints": [],
                "drives": [],
            }
            start_args["urdf_path"] = os.path.abspath(args.urdf)
        else:
            print("ERROR: Provide --session, --mechanism, or --urdf")
            conn.close()
            sys.exit(1)

        if args.profile:
            with open(args.profile) as f:
                start_args["profile"] = json.load(f)

        if args.import_config:
            with open(args.import_config) as f:
                start_args["import_config"] = json.load(f)

        print("Starting teleop session...")
        resp = conn.send("teleop_start", start_args)
        if not resp.get("ok"):
            err = resp.get("error", {})
            print(f"ERROR: teleop_start failed — {err.get('code', '?')}: {err.get('message', '?')}")
            conn.close()
            sys.exit(1)

        session_id = resp["result"]["session_id"]
        owns_session = True
        print(f"Session started: {session_id}")
        ct = resp["result"].get("controller_type", "?")
        print(f"Controller: {ct}")
    else:
        print(f"Attaching to session: {session_id}")

    # Print controls
    print()
    print("Controls:")
    print("  W/S  — forward/backward")
    print("  A/D  — turn left/right")
    print("  Q/E  — body height up/down")
    print("  Space — stop all")
    print("  Esc   — quit")
    print()

    vx, yaw, height = 0.0, 0.0, 0.0
    tick = 0
    last_ok = True

    try:
        with RawTerminal():
            last_send = time.monotonic()
            while True:
                key = RawTerminal.read_key(RawTerminal(), timeout=_SEND_INTERVAL / 2)
                if key is not None:
                    vx, yaw, height, quit_flag = _apply_key(
                        key,
                        vx,
                        yaw,
                        height,
                        args.vx_max,
                        args.yaw_max,
                        args.height_max,
                    )
                    if quit_flag:
                        break

                now = time.monotonic()
                if now - last_send >= _SEND_INTERVAL:
                    last_send = now
                    try:
                        resp = conn.send(
                            "teleop_command",
                            {
                                "session_id": session_id,
                                "vx_mps": round(vx, 4),
                                "yaw_rate_rps": round(yaw, 4),
                                "body_height_m": round(height, 5),
                            },
                        )
                        last_ok = resp.get("ok", False)
                        if last_ok:
                            tick += 1
                    except Exception:
                        last_ok = False

                    _draw_hud(vx, yaw, height, tick, last_ok)

    except KeyboardInterrupt:
        pass
    finally:
        # Clear the HUD line
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

        if owns_session:
            print(f"Stopping session {session_id}...")
            try:
                resp = conn.send("teleop_stop", {"session_id": session_id})
                if resp.get("ok"):
                    r = resp.get("result", {})
                    print(
                        f"Stopped. ticks={r.get('tick_count', '?')} "
                        f"clamps={r.get('limit_clamp_count', '?')}"
                    )
                else:
                    print(f"Stop failed: {resp.get('error', {}).get('message', '?')}")
            except Exception as exc:
                print(f"Stop error: {exc}")
        else:
            print("Detached (session still running).")

        conn.close()
        print("Done.")


if __name__ == "__main__":
    main()
