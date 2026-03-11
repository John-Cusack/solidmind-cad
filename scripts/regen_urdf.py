#!/usr/bin/env python3
"""Regenerate hexapod URDF with correct joint origins from manifest placements.

Uses the updated build_sim_model (manifest fallback for zero joint origins).
Run standalone — does NOT need the MCP server running.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from server.motion_models import JointType, Mechanism, PartNode, JointEdge
from server.sim_export import build_sim_model, write_urdf

# ── Geometry constants (must match add_hexapod_servos.py) ────────────
COXA_LEN = 52        # mm
FEMUR_LEN = 66        # mm
TIBIA_LEN = 133       # mm

FEMUR_PITCH = math.radians(30)   # 30° below horizontal
TIBIA_PITCH = math.radians(70)   # 70° below horizontal

total_drop = FEMUR_LEN * math.sin(FEMUR_PITCH) + TIBIA_LEN * math.sin(TIBIA_PITCH)
CHASSIS_Z = total_drop  # ~157.98 mm

# Leg attachment points: R=60mm at 60° intervals — all within 75mm chassis.
LEGS: dict[str, tuple[float, float]] = {
    "L1": (52, 30),    # front-left   (30°)
    "L2": (0, 60),     # mid-left     (90°)
    "L3": (-52, 30),   # rear-left    (150°)
    "R1": (52, -30),   # front-right  (330°)
    "R2": (0, -60),    # mid-right    (270°)
    "R3": (-52, -30),  # rear-right   (210°)
}

PKG_DIR = str(Path(__file__).resolve().parent.parent / "hexapod_full_pkg")


# ── Quaternion helpers ───────────────────────────────────────────────

def _quat_z(angle_rad: float) -> list[float]:
    """Quaternion [w, x, y, z] for rotation about Z."""
    return [math.cos(angle_rad / 2), 0.0, 0.0, math.sin(angle_rad / 2)]


def _quat_mul(q1: list[float], q2: list[float]) -> list[float]:
    """Hamilton product of two [w,x,y,z] quaternions."""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return [
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
    ]


def _quat_y(angle_rad: float) -> list[float]:
    """Quaternion [w, x, y, z] for rotation about Y."""
    return [math.cos(angle_rad / 2), 0.0, math.sin(angle_rad / 2), 0.0]


# ── Build MANIFEST from geometry constants ───────────────────────────

def _build_manifest() -> list[dict]:
    """Compute body positions/rotations from leg geometry constants."""
    manifest: list[dict] = []

    # Chassis
    manifest.append({
        "name": "Body_Chassis",
        "mesh_path": f"{PKG_DIR}/Body_Chassis.stl",
        "placement": {"position": [0.0, 0.0, round(CHASSIS_Z, 2)],
                      "rotation_quat": [1.0, 0.0, 0.0, 0.0]},
        "bbox_mm": [190.0, 190.0, 8.0],
        "volume_mm3": 228000.0,
    })

    for leg_id, (ax, ay) in LEGS.items():
        angle = math.atan2(ay, ax)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        # Coxa: at attachment point
        coxa_pos = [round(ax, 2), round(ay, 2), round(CHASSIS_Z, 2)]
        coxa_quat = _quat_z(angle)

        # Femur: at coxa tip (coxa_len along radial direction)
        hp_x = ax + cos_a * COXA_LEN
        hp_y = ay + sin_a * COXA_LEN
        femur_pos = [round(hp_x, 2), round(hp_y, 2), round(CHASSIS_Z, 2)]
        femur_quat = _quat_mul(_quat_z(angle), _quat_y(-FEMUR_PITCH))

        # Tibia: at femur tip (femur_len along pitched direction)
        femur_horiz = FEMUR_LEN * math.cos(FEMUR_PITCH)
        femur_vert = FEMUR_LEN * math.sin(FEMUR_PITCH)
        kp_x = hp_x + cos_a * femur_horiz
        kp_y = hp_y + sin_a * femur_horiz
        kp_z = CHASSIS_Z - femur_vert
        tibia_pos = [round(kp_x, 2), round(kp_y, 2), round(kp_z, 2)]
        tibia_quat = _quat_mul(_quat_z(angle), _quat_y(-TIBIA_PITCH))

        # Servo quaternions (yaw-only for hip-yaw, yaw+90 for pitch servos)
        servo_yaw_quat = _quat_z(angle)
        servo_pitch_quat = _quat_z(angle + math.pi / 2)

        # --- Structural bodies ---
        manifest.append({
            "name": f"Coxa_{leg_id}",
            "mesh_path": f"{PKG_DIR}/Coxa_{leg_id}.stl",
            "placement": {"position": coxa_pos,
                          "rotation_quat": coxa_quat},
            "bbox_mm": [48.64, 50.30, 10.0],
            "volume_mm3": 9360.0,
        })
        manifest.append({
            "name": f"Femur_{leg_id}",
            "mesh_path": f"{PKG_DIR}/Femur_{leg_id}.stl",
            "placement": {"position": femur_pos,
                          "rotation_quat": femur_quat},
            "bbox_mm": [57.03, 59.09, 41.66],
            "volume_mm3": 13200.0,
        })
        manifest.append({
            "name": f"Tibia_{leg_id}",
            "mesh_path": f"{PKG_DIR}/Tibia_{leg_id}.stl",
            "placement": {"position": tibia_pos,
                          "rotation_quat": tibia_quat},
            "bbox_mm": [47.13, 48.99, 127.72],
            "volume_mm3": 15960.0,
        })

        # --- Servo bodies (co-located with their joint) ---
        manifest.append({
            "name": f"Servo_hip_yaw_{leg_id}",
            "mesh_path": f"{PKG_DIR}/Servo_hip_yaw_{leg_id}.stl",
            "placement": {"position": coxa_pos,
                          "rotation_quat": servo_yaw_quat},
            "bbox_mm": [39.38, 39.77, 24.0],
            "volume_mm3": 18432.0,
        })
        manifest.append({
            "name": f"Servo_hip_pitch_{leg_id}",
            "mesh_path": f"{PKG_DIR}/Servo_hip_pitch_{leg_id}.stl",
            "placement": {"position": femur_pos,
                          "rotation_quat": servo_pitch_quat},
            "bbox_mm": [39.77, 39.38, 24.0],
            "volume_mm3": 18432.0,
        })
        manifest.append({
            "name": f"Servo_knee_{leg_id}",
            "mesh_path": f"{PKG_DIR}/Servo_knee_{leg_id}.stl",
            "placement": {"position": tibia_pos,
                          "rotation_quat": servo_pitch_quat},
            "bbox_mm": [39.77, 39.38, 24.0],
            "volume_mm3": 18432.0,
        })

    return manifest


MANIFEST = _build_manifest()

# Reconstruct the mechanism (matching the hexapod_18dof_full definition)
LEG_IDS = ["L1", "L2", "L3", "R1", "R2", "R3"]
LEG_SUFFIXES = ["lf", "lm", "lr", "rf", "rm", "rr"]

parts = [PartNode(id="chassis", body_name="Body_Chassis", is_ground=True)]
joints = []

for leg_id, suffix in zip(LEG_IDS, LEG_SUFFIXES):
    parts.append(PartNode(id=f"coxa_{suffix}", body_name=f"Coxa_{leg_id}"))
    parts.append(PartNode(id=f"femur_{suffix}", body_name=f"Femur_{leg_id}"))
    parts.append(PartNode(id=f"tibia_{suffix}", body_name=f"Tibia_{leg_id}"))

    # Revolute joints: chassis→coxa, coxa→femur, femur→tibia
    joints.append(JointEdge(
        id=f"j_coxa_{leg_id}", joint_type=JointType.REVOLUTE,
        parent_part="chassis", child_part=f"coxa_{suffix}",
        axis=(0, 0, 1), origin=(0, 0, 0),
    ))
    joints.append(JointEdge(
        id=f"j_femur_{leg_id}", joint_type=JointType.REVOLUTE,
        parent_part=f"coxa_{suffix}", child_part=f"femur_{suffix}",
        axis=(0, 1, 0), origin=(0, 0, 0),  # pitch around local Y
    ))
    joints.append(JointEdge(
        id=f"j_tibia_{leg_id}", joint_type=JointType.REVOLUTE,
        parent_part=f"femur_{suffix}", child_part=f"tibia_{suffix}",
        axis=(0, 1, 0), origin=(0, 0, 0),  # pitch around local Y
    ))

mechanism = Mechanism(
    name="hexapod_18dof_full",
    parts=tuple(parts),
    joints=tuple(joints),
    drives=(),
)

print(f"Mechanism: {len(mechanism.parts)} parts, {len(mechanism.joints)} joints")
print(f"Manifest: {len(MANIFEST)} bodies")

# Build sim model with updated code (manifest fallback)
sim_model = build_sim_model(mechanism, MANIFEST)

# Check joint origins
print("\nJoint origins (should be non-zero):")
for j in sim_model.joints:
    if "coxa" in j.name or "femur" in j.name or "tibia" in j.name:
        print(f"  {j.name}: xyz=({j.origin_xyz[0]:.4f}, {j.origin_xyz[1]:.4f}, {j.origin_xyz[2]:.4f})"
              f"  rpy=({j.origin_rpy[0]:.4f}, {j.origin_rpy[1]:.4f}, {j.origin_rpy[2]:.4f})")

# Write URDF
output_path = str(Path(__file__).resolve().parent.parent / "hexapod_full_pkg" / "hexapod_18dof_full.urdf")
write_urdf(sim_model, output_path)
print(f"\nURDF written to {output_path}")
