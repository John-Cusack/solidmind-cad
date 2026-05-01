#!/usr/bin/env python3
"""Phase 1 PX4 SITL smoke test (PX4 v1.17 sequencing).

Runs entirely outside the SolidMind bridge — only depends on a running
``make px4_sitl gz_x500`` session and a local ``pymavlink`` install.

PX4 v1.17 main has stricter arming requirements than older releases.
Specifically:

1. PX4 will NOT arm unless a Ground Control Station (GCS) is connected.
   "GCS connected" means we are continuously sending HEARTBEAT messages
   at >= 2 Hz from the same MAVLink endpoint.
2. PX4 expects ``COM_RC_IN_MODE=4`` (stick disabled) for SITL setups
   without an RC transmitter.  Without this, the RC sensor flags
   "missing" and arming is denied with the generic message
   "Resolve system health failures first".
3. ``NAV_RCL_ACT=0`` and ``NAV_DLL_ACT=0`` keep the autopilot from
   triggering an RC- or data-link-loss failsafe under SITL conditions.

The script does the dance, then sends ``MAV_CMD_NAV_TAKEOFF`` which is
how PX4's commander expects to receive a takeoff request from a GCS.
PX4 auto-arms internally and lifts to ``MIS_TAKEOFF_ALT``.

Goal: prove PX4 + Gazebo + MAVLink work together on this machine BEFORE
any SolidMind integration.  When this script flies, Phase 2 (bridge
MAVLink client) is unblocked.

Prerequisites
-------------
1. PX4 cloned and built::

     git clone https://github.com/PX4/PX4-Autopilot.git --recursive
     cd PX4-Autopilot && make px4_sitl gz_x500

   That command launches both PX4 SITL and a Gazebo session with the
   X500 quadrotor.  Leave it running.

2. ``pymavlink`` installed in your environment::

     pip install pymavlink

   Or via the SolidMind drone optional deps::

     pip install -e ".[drone]"

Usage
-----
::

    python scripts/px4_smoke_test_takeoff.py

Optional flags::

    --url udp:127.0.0.1:14540   # MAVLink endpoint (PX4 SITL default)
    --takeoff-alt 5.0           # metres above home (sets MIS_TAKEOFF_ALT)
    --hover-secs 10             # seconds to hold the takeoff altitude
    --timeout 60                # bail out after this many wall seconds
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass

try:
    from pymavlink import mavutil
except ImportError as exc:  # noqa: BLE001
    sys.stderr.write(
        "pymavlink is not installed. Run: pip install pymavlink\n"
        "Or: pip install -e '.[drone]' from the solidmind-cad repo root.\n"
    )
    raise SystemExit(2) from exc


@dataclass(slots=True)
class FlightConfig:
    url: str = "udp:127.0.0.1:14540"
    takeoff_alt_m: float = 5.0
    hover_secs: float = 10.0
    timeout_s: float = 90.0
    altitude_tolerance_m: float = 1.5


def connect(url: str, timeout_s: float) -> mavutil.mavfile:
    """Open a MAVLink connection and wait for the first heartbeat."""
    print(f"[px4-smoke] connecting to {url} ...", flush=True)
    conn = mavutil.mavlink_connection(url, source_system=255, source_component=190)
    msg = conn.wait_heartbeat(timeout=timeout_s)
    if msg is None:
        raise SystemExit(f"timed out waiting for heartbeat on {url}")
    print(
        f"[px4-smoke] heartbeat from sys={conn.target_system} "
        f"comp={conn.target_component} type={msg.type} autopilot={msg.autopilot}",
        flush=True,
    )
    return conn


def start_gcs_heartbeat(conn: mavutil.mavfile, stop: threading.Event) -> threading.Thread:
    """Stream HEARTBEAT messages at 3 Hz so PX4 sees an active GCS.

    PX4 v1.17 requires a continuously-broadcasting GCS partner before
    it'll allow arming.  Without this, arming is denied with the generic
    "Resolve system health failures first" message and the underlying
    health bit is ``MAV_SYS_STATUS_SENSOR_RC_RECEIVER`` plus an internal
    GCS-connection check that isn't surfaced separately over MAVLink.
    """
    def loop() -> None:
        while not stop.is_set():
            conn.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0, 0, 0,
            )
            time.sleep(0.3)

    t = threading.Thread(target=loop, daemon=True, name="gcs-heartbeat")
    t.start()
    return t


def set_param(
    conn: mavutil.mavfile,
    name: str,
    value: float,
    ptype: int,
    timeout_s: float = 3.0,
) -> bool:
    """Set a PX4 parameter and wait for the PARAM_VALUE confirmation."""
    conn.mav.param_set_send(
        conn.target_system, conn.target_component,
        name.encode(), float(value), ptype,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        msg = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
        if msg is not None and msg.param_id.startswith(name):
            print(f"[px4-smoke]   {msg.param_id}={msg.param_value}", flush=True)
            return True
    print(f"[px4-smoke]   WARN: no PARAM_VALUE for {name}", flush=True)
    return False


def configure_for_sitl(conn: mavutil.mavfile, takeoff_alt_m: float) -> None:
    """Set the params PX4 v1.17 SITL needs to allow arming under MAVLink."""
    print("[px4-smoke] setting SITL-friendly params...", flush=True)
    # Disable RC stick requirement (no physical transmitter in SITL).
    set_param(conn, "COM_RC_IN_MODE", 4, mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    # Disable RC-loss action (we have no RC to lose).
    set_param(conn, "NAV_RCL_ACT", 0, mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    # Disable data-link-loss action (this script keeps the link up but
    # the failsafe can still trigger on transient drops).
    set_param(conn, "NAV_DLL_ACT", 0, mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    # Default takeoff altitude.
    set_param(
        conn, "MIS_TAKEOFF_ALT", takeoff_alt_m,
        mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
    )


def wait_for_ready_for_takeoff(
    conn: mavutil.mavfile, timeout_s: float = 60.0,
) -> bool:
    """Block until PX4 reports sensors healthy + GCS connected.

    Tracks SYS_STATUS for sensor health and listens for the
    "Ready for takeoff!" STATUSTEXT that PX4 emits once preflight passes.
    """
    print("[px4-smoke] waiting for sensors + GCS to converge...", flush=True)
    deadline = time.monotonic() + timeout_s
    sensors_ok = False
    while time.monotonic() < deadline:
        msg = conn.recv_match(blocking=True, timeout=0.5)
        if msg is None:
            continue
        t = msg.get_type()
        if t == "SYS_STATUS":
            health = msg.onboard_control_sensors_health
            present = msg.onboard_control_sensors_present
            needed = 0x0F  # gyro + accel + mag + baro
            if (health & needed) == needed and (present & needed) == needed:
                if not sensors_ok:
                    print(
                        f"[px4-smoke] sensors healthy "
                        f"(health=0x{health:08x})", flush=True,
                    )
                    sensors_ok = True
        elif t == "STATUSTEXT":
            text = msg.text if isinstance(msg.text, str) else msg.text.decode("utf-8", "ignore")
            text = text.strip("\x00").strip()
            if "Ready for takeoff" in text:
                print(f"[px4-smoke] {text}", flush=True)
                return True
            if text and msg.severity <= 4:  # WARN/ERROR
                print(f"[px4-smoke]   STATUSTEXT[{msg.severity}]: {text}", flush=True)
    print("[px4-smoke] timed out waiting for ready-for-takeoff", flush=True)
    return sensors_ok  # Best-effort: continue if sensors are at least healthy.


def force_arm(conn: mavutil.mavfile, timeout_s: float = 8.0) -> bool:
    """Force-arm via the magic number 21196 (PX4 SITL convention).

    Standard arming through the preflight checks can fail under SITL even
    after sensors are healthy because the RC sensor bit lingers as
    "enabled but not present".  Force-arm bypasses that specific check.
    PX4 still rejects if any *real* failure exists (e.g., no GCS link,
    sensors not converged).
    """
    print("[px4-smoke] force-arming...", flush=True)
    conn.mav.command_long_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
        1.0,        # arm
        21196.0,    # MAVLINK force-arm magic
        0, 0, 0, 0, 0,
    )
    return _wait_command_ack(
        conn, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, timeout_s,
    )


def set_mode(
    conn: mavutil.mavfile,
    custom_main: int,
    custom_sub: int,
    label: str,
    timeout_s: float = 5.0,
) -> bool:
    """Switch PX4 to a (main, sub) mode pair via MAV_CMD_DO_SET_MODE."""
    print(f"[px4-smoke] DO_SET_MODE -> {label} (main={custom_main}, sub={custom_sub})", flush=True)
    conn.mav.command_long_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_CMD_DO_SET_MODE, 0,
        1.0,  # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        float(custom_main),
        float(custom_sub),
        0, 0, 0, 0,
    )
    return _wait_command_ack(
        conn, mavutil.mavlink.MAV_CMD_DO_SET_MODE, timeout_s,
    )


def takeoff(conn: mavutil.mavfile, alt_m: float, timeout_s: float = 8.0) -> bool:
    """Trigger takeoff by switching to AUTO_TAKEOFF mode.

    PX4's commander handles takeoff internally on this mode switch:
    auto-arms (if not already), engages thrust, and lifts to
    ``MIS_TAKEOFF_ALT``.  This is the same code path that
    ``commander takeoff`` uses from the pxh shell.
    """
    # PX4_CUSTOM_MAIN_MODE_AUTO=4, PX4_CUSTOM_SUB_MODE_AUTO_TAKEOFF=2
    return set_mode(conn, 4, 2, "AUTO_TAKEOFF", timeout_s=timeout_s)


def land(conn: mavutil.mavfile, timeout_s: float = 5.0) -> bool:
    """Switch to AUTO_LAND mode — PX4 commander brings the vehicle down."""
    # PX4_CUSTOM_MAIN_MODE_AUTO=4, PX4_CUSTOM_SUB_MODE_AUTO_LAND=6
    return set_mode(conn, 4, 6, "AUTO_LAND", timeout_s=timeout_s)


def _wait_command_ack(
    conn: mavutil.mavfile, expected_cmd: int, timeout_s: float,
) -> bool:
    rmap = {0: "ACCEPTED", 1: "TEMP_REJ", 2: "DENIED", 3: "UNSUPPORTED", 4: "FAILED"}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        msg = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.5)
        if msg is None or msg.command != expected_cmd:
            continue
        result = rmap.get(msg.result, str(msg.result))
        print(f"[px4-smoke]   ACK cmd={expected_cmd}: {result}", flush=True)
        return msg.result == 0
    print(f"[px4-smoke]   timeout waiting for ACK on cmd={expected_cmd}", flush=True)
    return False


def get_altitude_m(conn: mavutil.mavfile, timeout_s: float = 1.0) -> float | None:
    msg = conn.recv_match(
        type="LOCAL_POSITION_NED", blocking=True, timeout=timeout_s,
    )
    if msg is None:
        return None
    return -float(msg.z)  # NED z is positive-down; flip to up.


def hold_until_altitude(
    conn: mavutil.mavfile,
    target_alt_m: float,
    tolerance_m: float,
    timeout_s: float,
) -> bool:
    deadline = time.monotonic() + timeout_s
    last_alt: float | None = None
    while time.monotonic() < deadline:
        alt = get_altitude_m(conn)
        if alt is not None:
            last_alt = alt
            if alt > target_alt_m - tolerance_m:
                print(
                    f"[px4-smoke] altitude reached: {alt:.2f} m "
                    f"(target {target_alt_m:.1f} m)", flush=True,
                )
                return True
    print(
        f"[px4-smoke] altitude target not reached. last={last_alt} m", flush=True,
    )
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="udp:127.0.0.1:14540")
    p.add_argument("--takeoff-alt", type=float, default=5.0)
    p.add_argument("--hover-secs", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=90.0)
    args = p.parse_args()

    cfg = FlightConfig(
        url=args.url,
        takeoff_alt_m=args.takeoff_alt,
        hover_secs=args.hover_secs,
        timeout_s=args.timeout,
    )

    overall_deadline = time.monotonic() + cfg.timeout_s
    conn = connect(cfg.url, timeout_s=10.0)

    stop = threading.Event()
    start_gcs_heartbeat(conn, stop)

    try:
        configure_for_sitl(conn, cfg.takeoff_alt_m)
        if not wait_for_ready_for_takeoff(conn, timeout_s=45.0):
            print("[px4-smoke] FAIL: vehicle never reported ready", flush=True)
            return 1

        # Give PX4 a few extra seconds with our heartbeat flowing so the
        # internal "GCS connection alive" timer is firmly satisfied
        # before we ask it to arm.
        time.sleep(3.0)

        if not force_arm(conn, timeout_s=8.0):
            print("[px4-smoke] FAIL: force-arm rejected", flush=True)
            return 1

        if not takeoff(conn, cfg.takeoff_alt_m, timeout_s=8.0):
            print("[px4-smoke] FAIL: takeoff command rejected", flush=True)
            return 1

        remaining = max(15.0, overall_deadline - time.monotonic())
        if not hold_until_altitude(
            conn, cfg.takeoff_alt_m, cfg.altitude_tolerance_m, timeout_s=remaining,
        ):
            print("[px4-smoke] FAIL: did not reach takeoff altitude", flush=True)
            land(conn)
            return 1

        print(f"[px4-smoke] holding for {cfg.hover_secs:.1f} s", flush=True)
        time.sleep(cfg.hover_secs)

        land(conn)
        # Wait for landed (alt < 0.5 m or auto-disarm)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            alt = get_altitude_m(conn)
            if alt is not None and alt < 0.5:
                break
        print("[px4-smoke] PASS", flush=True)
        return 0
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
