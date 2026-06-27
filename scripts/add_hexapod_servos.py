#!/usr/bin/env python3
"""Add 18 servo motor bodies to the hexapod FreeCAD model.

Connects via TCP to the FreeCAD addon on localhost:9876 and creates
a 32×24×24mm rectangular block at each of the 18 joint locations
(6 hip-yaw, 6 hip-pitch, 6 knee-pitch).

Usage:
    python3 scripts/add_hexapod_servos.py
"""

from __future__ import annotations

import json
import math
import socket
import sys
import time

# ---------------------------------------------------------------------------
# Hexapod geometry (mm) — must match the standing-pose build script
# ---------------------------------------------------------------------------
COXA_LEN = 52
FEMUR_LEN = 66
TIBIA_LEN = 133

FEMUR_PITCH = math.radians(30)  # 30° below horizontal
TIBIA_PITCH = math.radians(70)  # 70° below horizontal

# Chassis height so feet touch ground (z = 0)
total_drop = FEMUR_LEN * math.sin(FEMUR_PITCH) + TIBIA_LEN * math.sin(TIBIA_PITCH)
CHASSIS_Z = total_drop  # ~158 mm

# Leg attachment points on chassis (x, y) in mm
# R=60mm at 60° intervals — all fit within the 75mm chassis disc.
LEGS: dict[str, tuple[float, float]] = {
    "L1": (52, 30),  # front-left   (30°)
    "L2": (0, 60),  # mid-left     (90°)
    "L3": (-52, 30),  # rear-left    (150°)
    "R1": (52, -30),  # front-right  (330°)
    "R2": (0, -60),  # mid-right    (270°)
    "R3": (-52, -30),  # rear-right   (210°)
}

# Servo block dimensions (mm) — AX-12A proportional
SERVO_W = 32  # along shaft axis
SERVO_H = 24  # width
SERVO_D = 24  # depth

# ---------------------------------------------------------------------------
# TCP helpers
# ---------------------------------------------------------------------------
HOST = "localhost"
PORT = 9876
TIMEOUT = 10.0


def send_cmd(sock: socket.socket, cmd: str, args: dict) -> dict:
    """Send a command and return the response."""
    payload = json.dumps({"cmd": cmd, "args": args}) + "\n"
    sock.sendall(payload.encode())

    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("FreeCAD addon closed connection")
        buf += chunk

    resp = json.loads(buf.split(b"\n", 1)[0])
    if not resp.get("ok"):
        print(f"  ERROR: {resp.get('error', resp)}", file=sys.stderr)
    return resp


def create_servo(
    sock: socket.socket,
    name: str,
    position: list[float],
    rotation_axis: list[float],
    rotation_angle_deg: float,
) -> None:
    """Create one servo body: new_body → sketch → pad → set_placement."""
    print(f"  Creating {name} at ({position[0]:.1f}, {position[1]:.1f}, {position[2]:.1f})")

    # 1. new_body
    resp = send_cmd(sock, "new_body", {"name": name})
    if not resp.get("ok"):
        return
    body_name = resp["result"]["name"]

    # 2. new_sketch on XY plane
    resp = send_cmd(sock, "new_sketch", {"body": body_name, "plane": "XY"})
    if not resp.get("ok"):
        return
    sketch_name = resp["result"]["sketch"]

    # 3. sketch_populate — centered rectangle SERVO_W × SERVO_H
    resp = send_cmd(
        sock,
        "sketch_populate",
        {
            "sketch": sketch_name,
            "elements": [
                {
                    "type": "rect",
                    "x": -SERVO_W / 2,
                    "y": -SERVO_H / 2,
                    "w": SERVO_W,
                    "h": SERVO_H,
                }
            ],
        },
    )
    if not resp.get("ok"):
        return

    # 4. close_sketch
    resp = send_cmd(sock, "close_sketch", {"sketch": sketch_name})
    if not resp.get("ok"):
        return

    # 5. pad — SERVO_D height, symmetric so the block is centered on Z
    resp = send_cmd(
        sock,
        "pad",
        {
            "sketch": sketch_name,
            "length": SERVO_D,
            "symmetric": True,
            "verify": False,
        },
    )
    if not resp.get("ok"):
        return

    # 6. set_placement — move to joint location
    resp = send_cmd(
        sock,
        "set_placement",
        {
            "object_name": body_name,
            "position": [round(p, 2) for p in position],
            "rotation_axis": [round(a, 6) for a in rotation_axis],
            "rotation_angle_deg": round(rotation_angle_deg, 4),
        },
    )
    if not resp.get("ok"):
        return

    print(f"    ✓ {name}")


# ---------------------------------------------------------------------------
# Compute servo positions for all 18 joints
# ---------------------------------------------------------------------------


def compute_servo_placements() -> list[dict]:
    """Return a list of 18 servo placement specs."""
    servos: list[dict] = []

    for leg_id, (ax, ay) in LEGS.items():
        # Outward angle from chassis center to attachment point
        angle = math.atan2(ay, ax)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        # --- Hip yaw servo: at chassis attachment, shaft along Z ---
        servos.append(
            {
                "name": f"Servo_hip_yaw_{leg_id}",
                "position": [ax, ay, CHASSIS_Z],
                # Rotate so the servo's long axis (local X) points outward
                "rotation_axis": [0.0, 0.0, 1.0],
                "rotation_angle_deg": math.degrees(angle),
            }
        )

        # --- Hip pitch servo: at coxa-femur junction ---
        # Coxa extends outward from attachment point
        hp_x = ax + cos_a * COXA_LEN
        hp_y = ay + sin_a * COXA_LEN
        hp_z = CHASSIS_Z  # same Z as chassis

        # Pitch axis is perpendicular to the outward direction, horizontal
        # pitch_axis = (-sin_a, cos_a, 0)
        # We orient the servo so its long axis aligns with pitch axis
        servos.append(
            {
                "name": f"Servo_hip_pitch_{leg_id}",
                "position": [hp_x, hp_y, hp_z],
                "rotation_axis": [0.0, 0.0, 1.0],
                "rotation_angle_deg": math.degrees(angle) + 90,
            }
        )

        # --- Knee pitch servo: at femur-tibia junction ---
        # Femur extends outward and downward from hip pitch position
        femur_horiz = FEMUR_LEN * math.cos(FEMUR_PITCH)
        femur_vert = FEMUR_LEN * math.sin(FEMUR_PITCH)
        kp_x = hp_x + cos_a * femur_horiz
        kp_y = hp_y + sin_a * femur_horiz
        kp_z = hp_z - femur_vert

        servos.append(
            {
                "name": f"Servo_knee_{leg_id}",
                "position": [kp_x, kp_y, kp_z],
                "rotation_axis": [0.0, 0.0, 1.0],
                "rotation_angle_deg": math.degrees(angle) + 90,
            }
        )

    return servos


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    placements = compute_servo_placements()

    print(f"Connecting to FreeCAD addon at {HOST}:{PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect((HOST, PORT))
    except ConnectionRefusedError:
        print("ERROR: Cannot connect to FreeCAD addon. Is it running?", file=sys.stderr)
        sys.exit(1)

    print(f"Creating {len(placements)} servo bodies...\n")

    for spec in placements:
        create_servo(
            sock,
            name=spec["name"],
            position=spec["position"],
            rotation_axis=spec["rotation_axis"],
            rotation_angle_deg=spec["rotation_angle_deg"],
        )
        # Small delay to let FreeCAD process
        time.sleep(0.1)

    # Take a screenshot at the end
    print("\nTaking screenshot...")
    send_cmd(sock, "screenshot", {"views": ["default"]})

    sock.close()
    print(f"\nDone! {len(placements)} servos placed.")


if __name__ == "__main__":
    main()
