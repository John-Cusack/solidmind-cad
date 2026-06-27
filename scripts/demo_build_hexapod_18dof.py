#!/usr/bin/env python3
"""Deterministic 18-DOF hexapod builder — drives FreeCAD addon directly via TCP.

Builds a full 18-DOF hexapod (chassis with PartDesign features + 6 articulated
legs with coxa/femur/tibia segments + 18 servo bodies) in ~30s.  No MCP server
or LLM needed — just FreeCAD with the addon running on localhost:9876.

The geometry matches the live demo prompts so this script doubles as a pre-demo
verification tool and a dry-run for the 18-DOF build pipeline.

Usage::

    # FreeCAD must be running with addon started first:
    #   import freecad_addon; freecad_addon.start()

    python3 scripts/demo_build_hexapod_18dof.py

    # Skip verification screenshots (faster)
    python3 scripts/demo_build_hexapod_18dof.py --fast

    # Export URDF sim package after building
    python3 scripts/demo_build_hexapod_18dof.py --export
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from typing import Any

HOST = "127.0.0.1"
PORT = 9876

# ── Geometry constants ───────────────────────────────────────────────

CHASSIS_RADIUS = 75.0  # mm
CHASSIS_THICKNESS = 5.0  # mm
SERVO_POCKET_W = 24.0  # mm (along radial)
SERVO_POCKET_H = 22.0  # mm (along tangential)
SERVO_POCKET_DEPTH = 3.0  # mm
FILLET_RADIUS = 1.5  # mm

# Segment lengths (mm) — from add_hexapod_servos.py
COXA_LEN = 52.0
FEMUR_LEN = 66.0
TIBIA_LEN = 133.0

# Standing pose angles
FEMUR_PITCH = math.radians(30)  # 30° below horizontal
TIBIA_PITCH = math.radians(70)  # 70° below horizontal

# Chassis height so feet touch ground (z = 0)
TOTAL_DROP = FEMUR_LEN * math.sin(FEMUR_PITCH) + TIBIA_LEN * math.sin(TIBIA_PITCH)
CHASSIS_Z = TOTAL_DROP  # ~158 mm

# Servo block dimensions (AX-12A proportional)
SERVO_W = 32.0  # mm along shaft axis
SERVO_H = 24.0  # mm width
SERVO_D = 24.0  # mm depth

# Link cross-sections
COXA_W = 20.0  # mm
COXA_D = 12.0  # mm
FEMUR_W = 16.0  # mm
FEMUR_D = 10.0  # mm
TIBIA_W = 12.0  # mm
TIBIA_D = 8.0  # mm

# 6 leg attachment positions on chassis (x, y) at R=60mm, 60° intervals
LEGS: dict[str, tuple[float, float]] = {
    "L1": (52.0, 30.0),  # front-left   (30°)
    "L2": (0.0, 60.0),  # mid-left     (90°)
    "L3": (-52.0, 30.0),  # rear-left    (150°)
    "R1": (52.0, -30.0),  # front-right  (330°)
    "R2": (0.0, -60.0),  # mid-right    (270°)
    "R3": (-52.0, -30.0),  # rear-right   (210°)
}

# ── ANSI colors ──────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# ── TCP helper ───────────────────────────────────────────────────────

TOTAL_STEPS = 14


def _send(cmd: str, timeout: float = 30.0, **args: Any) -> dict[str, Any]:
    """Send a command to the FreeCAD addon and return the parsed response."""
    payload = json.dumps({"cmd": cmd, "args": args}) + "\n"
    with socket.create_connection((HOST, PORT), timeout=10) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf.split(b"\n", 1)[0])
    if not resp.get("ok", False):
        err = resp.get("error", resp)
        raise RuntimeError(f"Command {cmd!r} failed: {err}")
    return resp.get("result", resp)


def _step(num: int, label: str) -> float:
    """Print step header, return start time."""
    print(f"  [{num}/{TOTAL_STEPS}] {label}...", end="", flush=True)
    return time.monotonic()


def _done(t0: float, detail: str = "") -> None:
    """Print step completion with elapsed time."""
    elapsed = time.monotonic() - t0
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {GREEN}✓{RESET}  {DIM}{elapsed:.1f}s{RESET}{suffix}")


# ── FK geometry computation ──────────────────────────────────────────


def _compose_yaw_pitch(yaw_rad: float, pitch_rad: float) -> tuple[list[float], float]:
    """Compose yaw (Z) then pitch (local Y) into a single axis-angle.

    Returns (axis, angle_deg) suitable for set_placement.  The composition
    is: R = Rz(yaw) · Ry(pitch).
    """
    # Quaternion for Rz(yaw): (cos(y/2), 0, 0, sin(y/2))
    cy2 = math.cos(yaw_rad / 2)
    sy2 = math.sin(yaw_rad / 2)
    qz = (cy2, 0.0, 0.0, sy2)  # (w, x, y, z)

    # Quaternion for Ry(pitch): (cos(p/2), 0, sin(p/2), 0)
    cp2 = math.cos(pitch_rad / 2)
    sp2 = math.sin(pitch_rad / 2)
    qy = (cp2, 0.0, sp2, 0.0)

    # Multiply: q = qz * qy
    w = qz[0] * qy[0] - qz[1] * qy[1] - qz[2] * qy[2] - qz[3] * qy[3]
    x = qz[0] * qy[1] + qz[1] * qy[0] + qz[2] * qy[3] - qz[3] * qy[2]
    y = qz[0] * qy[2] - qz[1] * qy[3] + qz[2] * qy[0] + qz[3] * qy[1]
    z = qz[0] * qy[3] + qz[1] * qy[2] - qz[2] * qy[1] + qz[3] * qy[0]

    # Convert quaternion to axis-angle
    angle = 2 * math.acos(max(-1.0, min(1.0, w)))
    s = math.sin(angle / 2)
    if s < 1e-12:
        return [0.0, 0.0, 1.0], 0.0
    axis = [x / s, y / s, z / s]
    return axis, math.degrees(angle)


def compute_leg_geometry(
    leg_id: str,
    ax: float,
    ay: float,
) -> dict[str, Any]:
    """Compute all positions/orientations for one leg.

    Returns a dict with keys: coxa, femur, tibia, servo_hip_yaw,
    servo_hip_pitch, servo_knee — each a dict with position, rotation_axis,
    rotation_angle_deg.  Femur and tibia include compound yaw+pitch rotation
    so the bodies tilt downward in the standing pose.
    """
    angle = math.atan2(ay, ax)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    angle_deg = math.degrees(angle)

    result: dict[str, Any] = {}

    # ── Coxa link: from chassis attachment → hip-pitch joint ──
    coxa_start_x = ax
    coxa_start_y = ay
    coxa_end_x = ax + cos_a * COXA_LEN
    coxa_end_y = ay + sin_a * COXA_LEN
    coxa_mid_x = (coxa_start_x + coxa_end_x) / 2
    coxa_mid_y = (coxa_start_y + coxa_end_y) / 2
    coxa_mid_z = CHASSIS_Z

    result["coxa"] = {
        "name": f"Coxa_{leg_id}",
        "shape": "box",
        "dimensions": {"length": COXA_LEN, "width": COXA_W, "height": COXA_D},
        "position": [coxa_mid_x, coxa_mid_y, coxa_mid_z],
        "rotation_axis": [0.0, 0.0, 1.0],
        "rotation_angle_deg": angle_deg,
    }

    # ── Femur link: from hip-pitch → knee joint ──
    hp_x = coxa_end_x
    hp_y = coxa_end_y
    hp_z = CHASSIS_Z

    femur_horiz = FEMUR_LEN * math.cos(FEMUR_PITCH)
    femur_vert = FEMUR_LEN * math.sin(FEMUR_PITCH)

    femur_end_x = hp_x + cos_a * femur_horiz
    femur_end_y = hp_y + sin_a * femur_horiz
    femur_end_z = hp_z - femur_vert

    femur_mid_x = (hp_x + femur_end_x) / 2
    femur_mid_y = (hp_y + femur_end_y) / 2
    femur_mid_z = (hp_z + femur_end_z) / 2

    # Compound rotation: yaw around Z, then pitch downward around local Y.
    # Positive pitch tilts +X end downward (toward knee).
    femur_axis, femur_angle_deg = _compose_yaw_pitch(angle, FEMUR_PITCH)

    result["femur"] = {
        "name": f"Femur_{leg_id}",
        "shape": "box",
        "dimensions": {"length": FEMUR_LEN, "width": FEMUR_W, "height": FEMUR_D},
        "position": [femur_mid_x, femur_mid_y, femur_mid_z],
        "rotation_axis": femur_axis,
        "rotation_angle_deg": femur_angle_deg,
    }

    # ── Tibia link: from knee → foot ──
    kp_x = femur_end_x
    kp_y = femur_end_y
    kp_z = femur_end_z

    tibia_horiz = TIBIA_LEN * math.cos(TIBIA_PITCH)
    tibia_vert = TIBIA_LEN * math.sin(TIBIA_PITCH)

    tibia_end_x = kp_x + cos_a * tibia_horiz
    tibia_end_y = kp_y + sin_a * tibia_horiz
    tibia_end_z = kp_z - tibia_vert

    tibia_mid_x = (kp_x + tibia_end_x) / 2
    tibia_mid_y = (kp_y + tibia_end_y) / 2
    tibia_mid_z = (kp_z + tibia_end_z) / 2

    # Compound rotation: yaw around Z, then steeper pitch downward.
    # Positive pitch tilts +X end downward (toward foot).
    tibia_axis, tibia_angle_deg = _compose_yaw_pitch(angle, TIBIA_PITCH)

    result["tibia"] = {
        "name": f"Tibia_{leg_id}",
        "shape": "box",
        "dimensions": {"length": TIBIA_LEN, "width": TIBIA_W, "height": TIBIA_D},
        "position": [tibia_mid_x, tibia_mid_y, tibia_mid_z],
        "rotation_axis": tibia_axis,
        "rotation_angle_deg": tibia_angle_deg,
    }

    # ── Servo positions (same as add_hexapod_servos.py) ──
    result["servo_hip_yaw"] = {
        "name": f"Servo_hip_yaw_{leg_id}",
        "shape": "box",
        "dimensions": {"length": SERVO_W, "width": SERVO_H, "height": SERVO_D},
        "position": [ax, ay, CHASSIS_Z],
        "rotation_axis": [0.0, 0.0, 1.0],
        "rotation_angle_deg": angle_deg,
    }

    result["servo_hip_pitch"] = {
        "name": f"Servo_hip_pitch_{leg_id}",
        "shape": "box",
        "dimensions": {"length": SERVO_W, "width": SERVO_H, "height": SERVO_D},
        "position": [hp_x, hp_y, hp_z],
        "rotation_axis": [0.0, 0.0, 1.0],
        "rotation_angle_deg": angle_deg + 90,
    }

    result["servo_knee"] = {
        "name": f"Servo_knee_{leg_id}",
        "shape": "box",
        "dimensions": {"length": SERVO_W, "width": SERVO_H, "height": SERVO_D},
        "position": [kp_x, kp_y, kp_z],
        "rotation_axis": [0.0, 0.0, 1.0],
        "rotation_angle_deg": angle_deg + 90,
    }

    return result


# ── Build functions ──────────────────────────────────────────────────


def build_document() -> None:
    """Step 1: Create document and body."""
    t0 = _step(1, "Creating document + chassis body")
    _send("new_document", name="Hexapod")
    _send("new_body", name="Body_Chassis")
    _done(t0)


def build_chassis_disc(verify: bool = True) -> str:
    """Step 2: Sketch circle + pad -> solid disc."""
    t0 = _step(2, f"Building chassis disc (r={CHASSIS_RADIUS}mm, h={CHASSIS_THICKNESS}mm)")

    result = _send("new_sketch", body="Body_Chassis", plane="XY")
    sketch_name = result["sketch"]

    _send(
        "sketch_populate",
        sketch=sketch_name,
        elements=[{"type": "circle", "cx": 0, "cy": 0, "r": CHASSIS_RADIUS}],
        constraints=[],
    )
    _send("close_sketch", sketch=sketch_name)

    _send("pad", sketch=sketch_name, length=CHASSIS_THICKNESS, verify=verify)
    _done(t0)
    return sketch_name


def build_servo_pocket(verify: bool = True) -> str:
    """Step 3: Single rectangular pocket at radius ~52mm."""
    t0 = _step(3, "Adding servo pocket cutout")

    pocket_cx = 0.0
    pocket_cy = 52.0
    pocket_x = pocket_cx - SERVO_POCKET_W / 2
    pocket_y = pocket_cy - SERVO_POCKET_H / 2

    result = _send("new_sketch", body="Body_Chassis", plane="XY")
    sketch_name = result["sketch"]

    _send(
        "sketch_populate",
        sketch=sketch_name,
        elements=[
            {"type": "rect", "x": pocket_x, "y": pocket_y, "w": SERVO_POCKET_W, "h": SERVO_POCKET_H}
        ],
        constraints=[],
    )
    _send("close_sketch", sketch=sketch_name)

    result = _send(
        "pocket",
        sketch=sketch_name,
        length=SERVO_POCKET_DEPTH,
        pocket_type="Dimension",
        reversed="auto",
        verify=verify,
    )
    _done(t0)
    return result.get("pocket", result.get("name", "Pocket"))


def build_polar_pattern(pocket_name: str, verify: bool = True) -> None:
    """Step 4: Polar pattern — 6 copies of the pocket around Z axis."""
    t0 = _step(4, "Polar pattern (6x servo pockets)")
    _send(
        "polar_pattern",
        features=[pocket_name],
        axis="Base_Z",
        occurrences=6,
        angle=360.0,
        verify=verify,
    )
    _done(t0)


def build_fillets(verify: bool = True) -> None:
    """Step 5: Fillet the top circular edge for a polished look."""
    t0 = _step(5, "Filleting top edges")

    result = _send("find_edges", body="Body_Chassis", curve_type="Circle", convexity="convex")
    edges = result.get("edges", [])
    edge_names = [e["edge"] for e in edges]

    if edge_names:
        _send(
            "fillet", edges=edge_names[:2], radius=FILLET_RADIUS, body="Body_Chassis", verify=verify
        )

    # Move chassis up to standing height so it physically connects to the legs.
    # Legs are placed at z=CHASSIS_Z; chassis must be there too.
    _send("set_placement", object_name="Body_Chassis", position=[0.0, 0.0, CHASSIS_Z])
    _done(t0)


def build_coxa_links(verify: bool = True) -> None:
    """Step 6: Create 6 coxa link bodies."""
    t0 = _step(6, "Creating coxa links (6x)")
    items = []
    for leg_id, (ax, ay) in LEGS.items():
        geom = compute_leg_geometry(leg_id, ax, ay)
        items.append(geom["coxa"])
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0, f"{len(items)} coxa links")


def build_femur_links(verify: bool = True) -> None:
    """Step 7: Create 6 femur link bodies."""
    t0 = _step(7, "Creating femur links (6x)")
    items = []
    for leg_id, (ax, ay) in LEGS.items():
        geom = compute_leg_geometry(leg_id, ax, ay)
        items.append(geom["femur"])
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0, f"{len(items)} femur links")


def build_tibia_links(verify: bool = True) -> None:
    """Step 8: Create 6 tibia link bodies."""
    t0 = _step(8, "Creating tibia links (6x)")
    items = []
    for leg_id, (ax, ay) in LEGS.items():
        geom = compute_leg_geometry(leg_id, ax, ay)
        items.append(geom["tibia"])
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0, f"{len(items)} tibia links")


def build_servos_hip_yaw(verify: bool = True) -> None:
    """Step 9: Create 6 hip-yaw servo bodies."""
    t0 = _step(9, "Creating hip-yaw servos (6x)")
    items = []
    for leg_id, (ax, ay) in LEGS.items():
        geom = compute_leg_geometry(leg_id, ax, ay)
        items.append(geom["servo_hip_yaw"])
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0)


def build_servos_hip_pitch(verify: bool = True) -> None:
    """Step 10: Create 6 hip-pitch servo bodies."""
    t0 = _step(10, "Creating hip-pitch servos (6x)")
    items = []
    for leg_id, (ax, ay) in LEGS.items():
        geom = compute_leg_geometry(leg_id, ax, ay)
        items.append(geom["servo_hip_pitch"])
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0)


def build_servos_knee(verify: bool = True) -> None:
    """Step 11: Create 6 knee servo bodies."""
    t0 = _step(11, "Creating knee servos (6x)")
    items = []
    for leg_id, (ax, ay) in LEGS.items():
        geom = compute_leg_geometry(leg_id, ax, ay)
        items.append(geom["servo_knee"])
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0)


def take_screenshot() -> None:
    """Step 12: Capture iso screenshot."""
    t0 = _step(12, "Capturing screenshot")
    _send("screenshot", target="iso", width=1024, height=1024)
    _done(t0)


def export_sim_package() -> str | None:
    """Step 13: Export URDF sim package."""
    t0 = _step(13, "Exporting sim package (URDF + STLs)")
    try:
        result = _send("export_sim_package", format="stl", timeout=120)
        path = result.get("output_dir", result.get("urdf_path", "?"))
        _done(t0, path)
        return path
    except RuntimeError as e:
        print(f"  {RED}✗{RESET}  {DIM}{e}{RESET}")
        return None


def verify_model() -> int:
    """Step 14: Print model tree summary. Returns body count."""
    _step(14, "Verifying model tree")
    result = _send("get_model_tree", detail="bodies")
    bodies = result.get("bodies", [])
    count = len(bodies)
    print(f"  {GREEN}✓{RESET}  {DIM}{count} bodies{RESET}")
    return count


# ── Mechanism definition helper ──────────────────────────────────────


def build_mechanism_dict() -> dict[str, Any]:
    """Build the full 37-part, 18-joint mechanism dict for motion.define_mechanism.

    This is the JSON that the LLM would construct during the demo.
    The dict can be passed to the MCP server's motion.define_mechanism tool.
    """
    parts: list[dict[str, Any]] = []
    joints: list[dict[str, Any]] = []

    # Chassis (ground part)
    parts.append(
        {
            "id": "Body_Chassis",
            "body_name": "Body_Chassis",
            "is_ground": True,
        }
    )

    leg_order = ["L1", "L2", "L3", "R1", "R2", "R3"]

    for leg_id in leg_order:
        ax, ay = LEGS[leg_id]
        angle = math.atan2(ay, ax)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        # Compute key positions (same math as compute_leg_geometry)
        hp_x = ax + cos_a * COXA_LEN
        hp_y = ay + sin_a * COXA_LEN

        femur_horiz = FEMUR_LEN * math.cos(FEMUR_PITCH)
        femur_vert = FEMUR_LEN * math.sin(FEMUR_PITCH)
        kp_x = hp_x + cos_a * femur_horiz
        kp_y = hp_y + sin_a * femur_horiz
        kp_z = CHASSIS_Z - femur_vert

        # ── Parts: coxa, femur, tibia + 3 servos ──
        for segment in ("Coxa", "Femur", "Tibia"):
            parts.append(
                {
                    "id": f"{segment}_{leg_id}",
                    "body_name": f"{segment}_{leg_id}",
                    "is_ground": False,
                }
            )
        for servo in ("Servo_hip_yaw", "Servo_hip_pitch", "Servo_knee"):
            parts.append(
                {
                    "id": f"{servo}_{leg_id}",
                    "body_name": f"{servo}_{leg_id}",
                    "is_ground": False,
                }
            )

        # ── Joints ──
        # Hip yaw: chassis → coxa (revolute around Z)
        joints.append(
            {
                "id": f"hip_yaw_{leg_id}",
                "joint_type": "revolute",
                "parent_part": "Body_Chassis",
                "child_part": f"Coxa_{leg_id}",
                "axis": [0.0, 0.0, 1.0],
                "origin": [ax, ay, CHASSIS_Z],
                "min_angle_deg": -45,
                "max_angle_deg": 45,
            }
        )

        # Hip yaw servo fixed to chassis
        joints.append(
            {
                "id": f"servo_hip_yaw_fixed_{leg_id}",
                "joint_type": "fixed",
                "parent_part": "Body_Chassis",
                "child_part": f"Servo_hip_yaw_{leg_id}",
                "origin": [ax, ay, CHASSIS_Z],
            }
        )

        # Hip pitch: coxa → femur (revolute around local Y)
        joints.append(
            {
                "id": f"hip_pitch_{leg_id}",
                "joint_type": "revolute",
                "parent_part": f"Coxa_{leg_id}",
                "child_part": f"Femur_{leg_id}",
                "axis": [0.0, 1.0, 0.0],
                "origin": [hp_x, hp_y, CHASSIS_Z],
                "min_angle_deg": -90,
                "max_angle_deg": 45,
            }
        )

        # Hip pitch servo fixed to coxa
        joints.append(
            {
                "id": f"servo_hip_pitch_fixed_{leg_id}",
                "joint_type": "fixed",
                "parent_part": f"Coxa_{leg_id}",
                "child_part": f"Servo_hip_pitch_{leg_id}",
                "origin": [hp_x, hp_y, CHASSIS_Z],
            }
        )

        # Knee: femur → tibia (revolute around local Y)
        joints.append(
            {
                "id": f"knee_{leg_id}",
                "joint_type": "revolute",
                "parent_part": f"Femur_{leg_id}",
                "child_part": f"Tibia_{leg_id}",
                "axis": [0.0, 1.0, 0.0],
                "origin": [kp_x, kp_y, kp_z],
                "min_angle_deg": -120,
                "max_angle_deg": 0,
            }
        )

        # Knee servo fixed to femur
        joints.append(
            {
                "id": f"servo_knee_fixed_{leg_id}",
                "joint_type": "fixed",
                "parent_part": f"Femur_{leg_id}",
                "child_part": f"Servo_knee_{leg_id}",
                "origin": [kp_x, kp_y, kp_z],
            }
        )

    return {
        "name": "Hexapod_18DOF",
        "parts": parts,
        "joints": joints,
        "drives": [
            {"joint_id": f"hip_yaw_{lid}", "speed_rpm": 60, "torque_nm": 1.5} for lid in leg_order
        ]
        + [{"joint_id": f"hip_pitch_{lid}", "speed_rpm": 60, "torque_nm": 1.5} for lid in leg_order]
        + [{"joint_id": f"knee_{lid}", "speed_rpm": 60, "torque_nm": 1.5} for lid in leg_order],
        "expected_outputs": {
            "dof": 18,
        },
    }


def build_teleop_profile() -> dict[str, Any]:
    """Build the teleop profile dict for motion.teleop_start with the 3-DOF IK controller.

    Joint names must match what the URDF export produces from the mechanism definition.
    """
    leg_order = ["L1", "L2", "L3", "R1", "R2", "R3"]

    # leg_joint_names: flat list of 18 names, groups of 3 (coxa, femur, tibia)
    # Order: LF, LM, LR, RF, RM, RR — matching the Hexapod3DOFController
    leg_joint_names = []
    for lid in leg_order:
        leg_joint_names.append(f"hip_yaw_{lid}")
        leg_joint_names.append(f"hip_pitch_{lid}")
        leg_joint_names.append(f"knee_{lid}")

    return {
        "controller_type": "hexapod_3dof_tripod",
        "leg_joint_names": leg_joint_names,
        # Tripod phase offsets: LF=0, LM=0.5, LR=0, RF=0.5, RM=0, RR=0.5
        "leg_phase_offsets": [0.0, 0.5, 0.0, 0.5, 0.0, 0.5],
        # IK geometry (meters)
        "l_coxa": COXA_LEN / 1000.0,
        "l_femur": FEMUR_LEN / 1000.0,
        "l_tibia": TIBIA_LEN / 1000.0,
        # Body dimensions (meters) — bounding box of hip mount positions
        "body_length": 0.104,  # ~2 * 52mm
        "body_width": 0.060,  # ~2 * 30mm (min Y offset to edge)
        # Gait parameters
        "stride_hz": 1.0,
        "stride_length": 0.06,
        "step_height": 0.04,
        "stance_height": -0.10,
        "duty_factor": 0.5,
        # Command limits
        "vx_max_mps": 0.5,
        "yaw_max_rps": 1.0,
        "height_max_m": 0.05,
        # Slew rates
        "slew_vx_mps2": 1.0,
        "slew_yaw_rps2": 2.0,
        "slew_height_mps2": 0.1,
    }


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Build 18-DOF hexapod in FreeCAD")
    parser.add_argument("--fast", action="store_true", help="Skip verification screenshots")
    parser.add_argument(
        "--export", action="store_true", help="Export URDF sim package after building"
    )
    parser.add_argument(
        "--print-mechanism", action="store_true", help="Print mechanism JSON to stdout and exit"
    )
    parser.add_argument(
        "--print-profile", action="store_true", help="Print teleop profile JSON to stdout and exit"
    )
    args = parser.parse_args()

    if args.print_mechanism:
        print(json.dumps(build_mechanism_dict(), indent=2))
        return

    if args.print_profile:
        print(json.dumps(build_teleop_profile(), indent=2))
        return

    verify = not args.fast

    print(f"\n{BOLD}{CYAN}═══ SolidMind CAD — 18-DOF Hexapod Demo Build ═══{RESET}\n")
    print(f"  Chassis: r={CHASSIS_RADIUS}mm, h={CHASSIS_THICKNESS}mm at z={CHASSIS_Z:.0f}mm")
    print(f"  Segments: coxa={COXA_LEN}mm, femur={FEMUR_LEN}mm, tibia={TIBIA_LEN}mm")
    print(
        f"  Pose: femur {math.degrees(FEMUR_PITCH):.0f}° down, tibia {math.degrees(TIBIA_PITCH):.0f}° down"
    )
    print("  Expected bodies: 1 chassis + 6×(3 links + 3 servos) = 37")
    print()

    t_start = time.monotonic()

    # Check connection
    try:
        _send("ping")
    except (ConnectionRefusedError, OSError):
        print(f"  {RED}Cannot connect to FreeCAD addon on {HOST}:{PORT}{RESET}")
        print("  Start FreeCAD and run: import freecad_addon; freecad_addon.start()")
        sys.exit(1)

    # Phase 1: Chassis plate (rich PartDesign features)
    build_document()
    build_chassis_disc(verify=verify)
    pocket_name = build_servo_pocket(verify=verify)
    build_polar_pattern(pocket_name, verify=verify)
    build_fillets(verify=verify)

    # Phase 2: Articulated legs
    build_coxa_links(verify=verify)
    build_femur_links(verify=verify)
    build_tibia_links(verify=verify)

    # Phase 3: Servos
    build_servos_hip_yaw(verify=verify)
    build_servos_hip_pitch(verify=verify)
    build_servos_knee(verify=verify)

    # Phase 4: Screenshot
    take_screenshot()

    # Phase 5: Export (optional)
    export_path = None
    if args.export:
        export_path = export_sim_package()

    # Phase 6: Verify
    body_count = verify_model()

    t_total = time.monotonic() - t_start
    print(f"\n{BOLD}{'━' * 60}{RESET}")
    print(f"  {GREEN}{BOLD}Total: {t_total:.1f}s  |  {body_count} bodies  |  18 DOF{RESET}")
    if export_path:
        print(f"  {DIM}URDF: {export_path}{RESET}")
    print(f"{BOLD}{'━' * 60}{RESET}\n")

    if body_count < 37:
        print(f"  {RED}WARNING: Expected 37 bodies, got {body_count}{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
