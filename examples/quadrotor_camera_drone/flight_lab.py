#!/usr/bin/env python3
"""Iterative flight tooling for the SolidMind quadrotor demo.

One CLI to manage a long-running PX4 SITL + Gazebo session and dispatch
flights against it.  The intent is that you start it once, leave it
running, and re-fly many times without rebuilding PX4 or restarting
Gazebo (which both take ~30 s).

Subcommands:

    status                  show SITL + Gazebo state
    start                   boot PX4 SITL + Gazebo GUI (NVIDIA EGL fix)
    stop                    kill SITL + Gazebo cleanly
    camera [--at LAT,LON]   reposition Gazebo viewing camera
    fly takeoff             arm, climb to ALT, hover, land
    fly square              5-waypoint square mission at altitude
    fly hover               arm, hover at ALT, land (no horizontal motion)
    fly figure8             figure-8 mission at altitude

All flight subcommands stream live altitude / mode telemetry to stdout
and exit cleanly when the drone touches down (or the timeout hits).

Default behaviour is intentionally idempotent: starting when SITL is
already running is a no-op; flying without an active SITL prints a
helpful message and exits.
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PX4_INSTALL = Path(os.environ.get(
    "SOLIDMIND_PX4_INSTALL",
    str(Path.home() / "repos" / "PX4-Autopilot")))
AIRFRAME = os.environ.get("FLIGHT_LAB_AIRFRAME", "gz_solidmind_quad")
MODEL_NAME = os.environ.get("FLIGHT_LAB_MODEL", "solidmind_quad_0")
SITL_LOG = Path("/tmp/flight_lab_sitl.log")
GUI_LOG = Path("/tmp/flight_lab_gui.log")

# NVIDIA EGL / Wayland / XWayland combination needs explicit vendor selection
# or libglvnd dispatches to Mesa for the NVIDIA GPU and rendering produces
# black frames.  These env vars resolved that on this machine.
NVIDIA_ENV = {
    "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
    "__EGL_VENDOR_LIBRARY_FILENAMES":
        "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "QT_QPA_PLATFORM": "xcb",
}


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------


def _pgrep(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                         text=True)
    return [int(p) for p in out.stdout.split() if p.isdigit()]


def _sitl_running() -> bool:
    return any(_pgrep("px4_sitl_default/bin/px4"))


def _gui_running() -> bool:
    return any(_pgrep("gz-sim-gui-client"))


def _server_running() -> bool:
    return any(_pgrep("gz-sim-main.*-r -s"))


def cmd_status(_args: argparse.Namespace) -> None:
    rows = [
        ("PX4 SITL",   _sitl_running(),    " ".join(map(str, _pgrep("px4_sitl_default/bin/px4")))),
        ("Gazebo srv", _server_running(),  " ".join(map(str, _pgrep("gz-sim-main.*-r -s")))),
        ("Gazebo GUI", _gui_running(),     " ".join(map(str, _pgrep("gz-sim-gui-client")))),
    ]
    width = max(len(r[0]) for r in rows)
    for name, alive, pids in rows:
        mark = "+" if alive else "-"
        print(f"  [{mark}] {name:<{width}}  {pids or '(none)'}")
    if _sitl_running():
        print(f"\n  airframe: {AIRFRAME}")
        print(f"  model:    {MODEL_NAME}")
        print("  mavlink:  udp:127.0.0.1:14540")


def cmd_start(args: argparse.Namespace) -> None:
    if _sitl_running():
        print("SITL already running — no-op")
        if not _gui_running() and not args.headless:
            print("(no GUI running; pass --gui to launch one)")
        return

    # Clean stale lock files
    for stale in ("/tmp/px4-sock-0", "/tmp/px4_lock-0"):
        try:
            Path(stale).unlink()
        except FileNotFoundError:
            pass

    env = os.environ.copy()
    env.update(NVIDIA_ENV)
    env["HEADLESS"] = "1" if args.headless else "0"

    # PX4 will auto-spawn gz-sim-server.  HEADLESS=0 is supposed to add the
    # GUI but on some systems it doesn't.  Start the server-side first,
    # then explicitly launch the GUI.
    print(f"booting PX4 SITL + Gazebo (model={AIRFRAME})")
    SITL_LOG.write_text("")
    sitl = subprocess.Popen(
        ["make", "px4_sitl_default", AIRFRAME],
        cwd=str(PX4_INSTALL),
        stdin=subprocess.DEVNULL,
        stdout=open(SITL_LOG, "w"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    print(f"  make pid: {sitl.pid}")

    # Wait for SITL boot
    deadline = time.time() + 120
    while time.time() < deadline:
        if SITL_LOG.exists() and any(
            "Startup script returned successfully" in line
            for line in SITL_LOG.read_text(errors="ignore").splitlines()
        ):
            print("  SITL booted")
            break
        time.sleep(2)
    else:
        print("  SITL boot timeout — check log:", SITL_LOG)
        return

    if args.headless:
        print("started headless (no GUI)")
        return

    # Launch GUI separately to get the NVIDIA EGL fix env vars
    print("launching Gazebo GUI")
    GUI_LOG.write_text("")
    subprocess.Popen(
        ["gz", "sim", "-g", "-v", "3"],
        stdin=subprocess.DEVNULL,
        stdout=open(GUI_LOG, "w"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    time.sleep(5)

    # Move camera to a sensible default viewing angle
    cmd_camera(argparse.Namespace(distance=20.0, height=12.0,
                                  follow=True))
    print("ready — `flight_lab.py fly square` to launch a flight")


def cmd_stop(_args: argparse.Namespace) -> None:
    """Kill SITL + Gazebo cleanly."""
    targets = [
        "make px4_sitl_default",
        "build/px4_sitl_default/bin/px4",
        "gz-sim-gui-client",
        "gz-sim-main",
    ]
    for pat in targets:
        pids = _pgrep(pat)
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(2)
    for stale in ("/tmp/px4-sock-0", "/tmp/px4_lock-0"):
        try:
            Path(stale).unlink()
        except FileNotFoundError:
            pass
    print("stopped")


# ---------------------------------------------------------------------------
# Camera control
# ---------------------------------------------------------------------------


def cmd_camera(args: argparse.Namespace) -> None:
    """Reposition Gazebo viewing camera."""
    if not _gui_running():
        print("no GUI running — `flight_lab.py start` first")
        return

    if args.follow:
        # Center on drone first
        subprocess.run(
            ["gz", "service", "-s", "/gui/move_to",
             "--reqtype", "gz.msgs.StringMsg",
             "--reptype", "gz.msgs.Boolean",
             "--timeout", "3000",
             "--req", f'data: "{MODEL_NAME}"'],
            capture_output=True, timeout=5)

    d = args.distance
    h = args.height
    # Camera at (+d, +d, h) looking back at origin.  Quaternion derived
    # from euler (yaw=225°, pitch=down) with Gazebo's positive-pitch-is-
    # downward convention.  The previous orientation looked at the sky.
    pose_req = (f'pose {{ position {{ x: {d} y: {d} z: {h} }} '
                f'orientation {{ w: -0.3713 x: -0.2235 y: -0.0926 z: 0.8964 }} }}')
    out = subprocess.run(
        ["gz", "service", "-s", "/gui/move_to/pose",
         "--reqtype", "gz.msgs.GUICamera",
         "--reptype", "gz.msgs.Boolean",
         "--timeout", "3000",
         "--req", pose_req],
        capture_output=True, text=True, timeout=5)
    if "data: true" in out.stdout:
        print(f"camera at ({-d:.0f}, {-d:.0f}, {h:.0f}) looking at origin")
    else:
        print(f"camera move failed: {out.stdout} {out.stderr}")


# ---------------------------------------------------------------------------
# Flight execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Waypoint:
    cmd: int
    north_m: float
    east_m: float
    alt_m: float


def _connect_mavlink():
    from pymavlink import mavutil  # local import — pymavlink is heavy
    m = mavutil.mavlink_connection("udp:127.0.0.1:14540")
    m.wait_heartbeat(timeout=10)
    return m


def _set_permissive_params(m) -> None:
    """Disable the SITL pre-flight checks that are noisy in this setup.

    Without these, force-arming gets rejected by ``Arming denied:
    Resolve system health failures first`` because PX4 reports
    ``Preflight Fail: No connection to the GCS`` and
    ``system power unavailable`` even though we're in SITL.
    """
    from pymavlink import mavutil
    P_INT = mavutil.mavlink.MAV_PARAM_TYPE_INT32
    P_REAL = mavutil.mavlink.MAV_PARAM_TYPE_REAL32
    for n, v in [("COM_RC_IN_MODE", 4), ("NAV_RCL_ACT", 0),
                 ("NAV_DLL_ACT", 0), ("CBRK_SUPPLY_CHK", 894281),
                 ("COM_ARM_WO_GPS", 1), ("COM_PREARM_MODE", 0)]:
        m.mav.param_set_send(1, 1, n.encode(), float(v), P_INT)
        time.sleep(0.05)
    for n, v in [("MPC_THR_MIN", 0.30), ("MPC_TKO_SPEED", 2.0),
                 ("MPC_LAND_SPEED", 1.0), ("LNDMC_FFALL_THR", 2.0),
                 ("MPC_THR_MAX", 1.0)]:
        m.mav.param_set_send(1, 1, n.encode(), float(v), P_REAL)
        time.sleep(0.05)


def _force_arm(m) -> None:
    from pymavlink import mavutil
    _set_permissive_params(m)
    m.mav.command_long_send(1, 1,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
        1, 21196, 0, 0, 0, 0, 0)
    m.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)


def _set_mission_mode(m) -> None:
    from pymavlink import mavutil
    m.mav.set_mode_send(1,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        4 << 16 | 4 << 24)


def _set_takeoff_mode(m) -> None:
    from pymavlink import mavutil
    m.mav.set_mode_send(1,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        4 << 16 | 2 << 24)


def _upload_mission(m, home_lat: int, home_lon: int,
                    waypoints: list[Waypoint]) -> bool:
    from pymavlink import mavutil
    R = 6378137.0

    def offset_to_ll(north_m: float, east_m: float) -> tuple[int, int]:
        lat_ref = home_lat / 1e7
        dlat = math.degrees(north_m / R)
        dlon = math.degrees(east_m / (R * math.cos(math.radians(lat_ref))))
        return int(home_lat + dlat * 1e7), int(home_lon + dlon * 1e7)

    m.mav.mission_count_send(1, 1, len(waypoints),
                             mavutil.mavlink.MAV_MISSION_TYPE_MISSION)

    for seq, wp in enumerate(waypoints):
        req = m.recv_match(type=["MISSION_REQUEST", "MISSION_REQUEST_INT"],
                           blocking=True, timeout=5)
        if req is None:
            print(f"  upload stalled at seq {seq}")
            return False
        lat, lon = offset_to_ll(wp.north_m, wp.east_m)
        # current=1 on seq 0 forces PX4 to reset the mission pointer to
        # the first waypoint.  Without this, a fresh upload after a
        # completed mission keeps the pointer at the last item (LAND),
        # so the new mission lands immediately.
        m.mav.mission_item_int_send(
            1, 1, seq,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            wp.cmd,
            1 if seq == 0 else 0, 1,
            0, 0, 0, float("nan"),
            lat, lon, wp.alt_m,
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION)

    ack = m.recv_match(type="MISSION_ACK", blocking=True, timeout=5)
    if ack is None or ack.type != 0:
        return False

    # Belt-and-braces: also send a dedicated MISSION_SET_CURRENT message.
    m.mav.mission_set_current_send(1, 1, 0)
    return True


def _wait_home(m) -> tuple[int, int]:
    print("  waiting for home position...")
    for _ in range(30):
        gp = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if gp and gp.lat != 0:
            return gp.lat, gp.lon
    raise RuntimeError("no GPS lock after 30s")


def _watch_until_landed(m, max_secs: float = 120,
                        landed_after: float = 30) -> None:
    """Stream alt + waypoint progress until touchdown."""
    t0 = time.time()
    last_seq = -1
    last_print = 0.0
    while time.time() - t0 < max_secs:
        msg = m.recv_match(blocking=True, timeout=0.5)
        if msg is None:
            continue
        tt = msg.get_type()
        if tt == "GLOBAL_POSITION_INT":
            alt = msg.relative_alt / 1000.0
            if time.time() - last_print > 1.0:
                print(f"    t={time.time()-t0:5.1f}s alt={alt:6.2f} m")
                last_print = time.time()
            if (alt < 0.3 and time.time() - t0 > landed_after):
                print(f"  touchdown at {alt:.2f} m")
                return
        elif tt == "MISSION_CURRENT" and msg.seq != last_seq:
            print(f"    -> waypoint {msg.seq}")
            last_seq = msg.seq
    print("  watchdog timeout")


def _square_waypoints(alt: float, side: float) -> list[Waypoint]:
    from pymavlink import mavutil
    return [
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, alt),
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, side, 0, alt),
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, side, side, alt),
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, side, alt),
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, 0, alt),
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_LAND, 0, 0, 0),
    ]


def _figure8_waypoints(alt: float, radius: float) -> list[Waypoint]:
    from pymavlink import mavutil
    # 16-point figure-8 (lemniscate) — two loops centered at home, on
    # opposite sides
    pts = []
    for i in range(8):
        theta = i * math.pi / 4
        pts.append((radius * math.cos(theta) + radius,
                    radius * math.sin(theta)))
    for i in range(8):
        theta = math.pi + i * math.pi / 4
        pts.append((radius * math.cos(theta) - radius,
                    radius * math.sin(theta)))

    wps: list[Waypoint] = [
        Waypoint(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, alt),
    ]
    for n, e in pts:
        wps.append(Waypoint(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, n, e, alt))
    wps.append(Waypoint(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, 0, alt))
    wps.append(Waypoint(mavutil.mavlink.MAV_CMD_NAV_LAND, 0, 0, 0))
    return wps


def cmd_fly(args: argparse.Namespace) -> None:
    if not _sitl_running():
        print("no SITL running — `flight_lab.py start` first")
        return

    print(f"flight: {args.pattern}, alt={args.alt} m")
    m = _connect_mavlink()
    print(f"  connected sys={m.target_system}")

    # Re-center camera on the drone before flying
    if _gui_running() and not args.no_camera:
        cmd_camera(argparse.Namespace(distance=args.cam_dist,
                                      height=args.cam_height, follow=True))

    if args.pattern == "takeoff":
        from pymavlink import mavutil
        m.mav.param_set_send(1, 1, b"MIS_TAKEOFF_ALT", float(args.alt),
                             mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
        time.sleep(0.5)
        _force_arm(m)
        _set_takeoff_mode(m)
        print("  AUTO_TAKEOFF")
        # Wait for climb
        t0 = time.time()
        while time.time() - t0 < 25:
            gp = m.recv_match(type="GLOBAL_POSITION_INT",
                              blocking=True, timeout=0.5)
            if gp and gp.relative_alt / 1000 > args.alt - 0.5:
                print(f"  reached {gp.relative_alt/1000:.2f} m")
                break
        print(f"  hover {args.hover_secs}s")
        time.sleep(args.hover_secs)
        print("  LAND")
        m.mav.command_long_send(1, 1, mavutil.mavlink.MAV_CMD_NAV_LAND, 0,
                                0, 0, 0, 0, 0, 0, 0)
        _watch_until_landed(m, max_secs=40)
        return

    if args.pattern == "hover":
        # Same as takeoff but no horizontal motion (just a clearer name)
        args.pattern = "takeoff"
        return cmd_fly(args)

    # Mission patterns
    if args.pattern == "square":
        wps = _square_waypoints(alt=args.alt, side=args.side)
    elif args.pattern == "figure8":
        wps = _figure8_waypoints(alt=args.alt, radius=args.side / 2)
    else:
        print(f"unknown pattern: {args.pattern}")
        return

    home_lat, home_lon = _wait_home(m)
    print(f"  home: {home_lat/1e7:.6f}, {home_lon/1e7:.6f}")
    print(f"  uploading {len(wps)} waypoints")
    if not _upload_mission(m, home_lat, home_lon, wps):
        print("  mission upload failed")
        return

    _force_arm(m)
    time.sleep(1)
    _set_mission_mode(m)
    print("  AUTO_MISSION")
    _watch_until_landed(m, max_secs=180,
                        landed_after=15 if args.pattern == "square" else 40)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flight_lab",
                                description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show SITL + Gazebo state")

    p_start = sub.add_parser("start", help="boot SITL + Gazebo GUI")
    p_start.add_argument("--headless", action="store_true",
                         help="no Gazebo GUI")

    sub.add_parser("stop", help="kill SITL + Gazebo")

    p_cam = sub.add_parser("camera", help="reposition viewing camera")
    p_cam.add_argument("--distance", type=float, default=20.0,
                       help="diagonal distance from origin (m)")
    p_cam.add_argument("--height", type=float, default=12.0,
                       help="camera height (m)")
    p_cam.add_argument("--follow", action="store_true",
                       help="center on drone first")

    p_fly = sub.add_parser("fly", help="fly a pattern")
    p_fly.add_argument("pattern",
                       choices=["takeoff", "hover", "square", "figure8"])
    p_fly.add_argument("--alt", type=float, default=8.0,
                       help="cruise altitude (m)")
    p_fly.add_argument("--side", type=float, default=15.0,
                       help="square side / figure-8 size (m)")
    p_fly.add_argument("--hover-secs", type=float, default=10.0,
                       help="hover duration (takeoff/hover only)")
    p_fly.add_argument("--no-camera", action="store_true",
                       help="don't reposition the camera")
    p_fly.add_argument("--cam-dist", type=float, default=20.0)
    p_fly.add_argument("--cam-height", type=float, default=12.0)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = {
        "status": cmd_status,
        "start":  cmd_start,
        "stop":   cmd_stop,
        "camera": cmd_camera,
        "fly":    cmd_fly,
    }[args.command]
    handler(args)


if __name__ == "__main__":
    main()
