#!/usr/bin/env python3
"""Transform hexapod STL meshes from world coordinates to body-local coordinates.

The FreeCAD export produced STLs in world coords. URDF expects them in
body-local coords (the mesh is placed at the link frame which is already
positioned by joint origins). This script applies the inverse of each body's
Placement (from the manifest) to transform vertices into body-local space.

Usage:
    python3 scripts/fix_stl_coords.py
"""
from __future__ import annotations

import re
from pathlib import Path

# Manifest: body name -> (position_mm, rotation_quat [w, x, y, z])
MANIFEST = {
    "Body_Chassis": ([0.0, 0.0, 157.98], [1.0, 0.0, 0.0, 0.0]),
    "Coxa_L1": ([70.0, 75.0, 157.98], [0.9171473939110514, 0.0, 0.0, 0.39854818760366567]),
    "Coxa_L2": ([0.0, 75.0, 157.98], [0.7071067811865476, 0.0, 0.0, 0.7071067811865475]),
    "Coxa_L3": ([-70.0, 75.0, 157.98], [0.3985481876036656, 0.0, 0.0, 0.9171473939110515]),
    "Coxa_R1": ([70.0, -75.0, 157.98], [-0.9171473939110514, 0.0, 0.0, 0.3985481876036659]),
    "Coxa_R2": ([0.0, -75.0, 157.98], [-0.7071067811865475, 0.0, 0.0, 0.7071067811865476]),
    "Coxa_R3": ([-70.0, -75.0, 157.98], [-0.3985481876036655, 0.0, 0.0, 0.9171473939110515]),
    "Femur_L1": ([105.48, 113.01, 157.98], [0.8858961147811252, -0.10315199541276454, 0.23737539474853156, 0.38496839042452735]),
    "Femur_L2": ([0.0, 127.0, 157.98], [0.6830124833676485, -0.18301289796097248, 0.18301289796097248, 0.6830128153437613]),
    "Femur_L3": ([-105.48, 113.01, 157.98], [0.38496794074669644, -0.23737480950244239, 0.10315218668510899, 0.8858964447343762]),
    "Femur_R1": ([105.48, -113.01, 157.98], [0.8858961147811252, 0.10315199541276454, 0.23737539474853156, -0.38496839042452735]),
    "Femur_R2": ([0.0, -127.0, 157.98], [0.6830124833676485, 0.18301289796097248, 0.18301289796097248, -0.6830128153437613]),
    "Femur_R3": ([-105.48, -113.01, 157.98], [0.38496794074669644, 0.23737480950244239, 0.10315218668510899, -0.8858964447343762]),
    "Tibia_L1": ([144.48, 154.8, 124.98], [0.751283139875577, -0.22859789377779857, 0.5260541291875838, 0.32647159120471936]),
    "Tibia_L2": ([0.0, 184.16, 124.98], [0.5792278211125137, -0.40557985126140306, 0.40557985126140306, 0.579228020515933]),
    "Tibia_L3": ([-144.48, 154.8, 124.98], [0.32647151275243197, -0.5260542180192672, 0.22859840306596504, 0.7512829568018742]),
    "Tibia_R1": ([144.48, -154.8, 124.98], [0.751283139875577, 0.22859789377779857, 0.5260541291875838, -0.32647159120471936]),
    "Tibia_R2": ([0.0, -184.16, 124.98], [0.5792278211125137, 0.40557985126140306, 0.40557985126140306, -0.579228020515933]),
    "Tibia_R3": ([-144.48, -154.8, 124.98], [0.32647151275243197, 0.5260542180192672, 0.22859840306596504, -0.7512829568018742]),
    "Servo_hip_yaw_L1": ([70.0, 75.0, 157.98], [0.9171473939110514, 0.0, 0.0, 0.39854818760366567]),
    "Servo_hip_pitch_L1": ([105.48, 113.01, 157.98], [0.36670501549791396, 0.0, 0.0, 0.9303372676662344]),
    "Servo_knee_L1": ([144.48, 154.8, 124.98], [0.36670501549791396, 0.0, 0.0, 0.9303372676662344]),
    "Servo_hip_yaw_L2": ([0.0, 75.0, 157.98], [0.7071067811865476, 0.0, 0.0, 0.7071067811865475]),
    "Servo_hip_pitch_L2": ([0.0, 127.0, 157.98], [6.12e-17, 0.0, 0.0, 1.0]),
    "Servo_knee_L2": ([0.0, 184.16, 124.98], [6.12e-17, 0.0, 0.0, 1.0]),
    "Servo_hip_yaw_L3": ([-70.0, 75.0, 157.98], [0.3985481876036656, 0.0, 0.0, 0.9171473939110515]),
    "Servo_hip_pitch_L3": ([-105.48, 113.01, 157.98], [-0.36670501549791407, 0.0, 0.0, 0.9303372676662344]),
    "Servo_knee_L3": ([-144.48, 154.8, 124.98], [-0.36670501549791407, 0.0, 0.0, 0.9303372676662344]),
    "Servo_hip_yaw_R1": ([70.0, -75.0, 157.98], [-0.9171473939110514, 0.0, 0.0, 0.3985481876036659]),
    "Servo_hip_pitch_R1": ([105.48, -113.01, 157.98], [0.9303372676662345, 0.0, 0.0, 0.36670501549791384]),
    "Servo_knee_R1": ([144.48, -154.8, 124.98], [0.9303372676662345, 0.0, 0.0, 0.36670501549791384]),
    "Servo_hip_yaw_R2": ([0.0, -75.0, 157.98], [-0.7071067811865475, 0.0, 0.0, 0.7071067811865476]),
    "Servo_hip_pitch_R2": ([0.0, -127.0, 157.98], [1.0, 0.0, 0.0, 0.0]),
    "Servo_knee_R2": ([0.0, -184.16, 124.98], [1.0, 0.0, 0.0, 0.0]),
    "Servo_hip_yaw_R3": ([-70.0, -75.0, 157.98], [-0.3985481876036655, 0.0, 0.0, 0.9171473939110515]),
    "Servo_hip_pitch_R3": ([-105.48, -113.01, 157.98], [-0.9303372676662345, 0.0, 0.0, 0.36670501549791384]),
    "Servo_knee_R3": ([-144.48, -154.8, 124.98], [-0.9303372676662345, 0.0, 0.0, 0.36670501549791384]),
}


def quat_to_rotation_matrix(q: list[float]) -> list[list[float]]:
    """Convert quaternion [w, x, y, z] to 3x3 rotation matrix."""
    w, x, y, z = q
    return [
        [1 - 2*(y*y + z*z), 2*(x*y - w*z),     2*(x*z + w*y)],
        [2*(x*y + w*z),     1 - 2*(x*x + z*z), 2*(y*z - w*x)],
        [2*(x*z - w*y),     2*(y*z + w*x),     1 - 2*(x*x + y*y)],
    ]


def transpose_3x3(m: list[list[float]]) -> list[list[float]]:
    """Transpose 3x3 matrix."""
    return [[m[j][i] for j in range(3)] for i in range(3)]


def mat_vec_mul(m: list[list[float]], v: list[float]) -> list[float]:
    """Multiply 3x3 matrix by 3-vector."""
    return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]


def transform_stl(stl_path: Path, position: list[float], quat: list[float]) -> None:
    """Transform ASCII STL vertices from world to body-local coordinates.

    V_local = R_inv * (V_world - T)
    """
    r_mat = quat_to_rotation_matrix(quat)
    r_inv = transpose_3x3(r_mat)  # R^T = R^-1 for rotation matrices
    tx, ty, tz = position

    text = stl_path.read_text()
    vertex_re = re.compile(
        r"(\s+vertex\s+)([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)"
    )

    def replace_vertex(match: re.Match) -> str:
        prefix = match.group(1)
        vx = float(match.group(2))
        vy = float(match.group(3))
        vz = float(match.group(4))
        # Apply inverse transform: V_local = R_inv * (V_world - T)
        dx, dy, dz = vx - tx, vy - ty, vz - tz
        lx, ly, lz = mat_vec_mul(r_inv, [dx, dy, dz])
        return f"{prefix}{lx:.6e} {ly:.6e} {lz:.6e}"

    # Also transform normals (rotation only, no translation)
    normal_re = re.compile(
        r"(\s+facet normal\s+)([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)"
    )

    def replace_normal(match: re.Match) -> str:
        prefix = match.group(1)
        nx = float(match.group(2))
        ny = float(match.group(3))
        nz = float(match.group(4))
        lnx, lny, lnz = mat_vec_mul(r_inv, [nx, ny, nz])
        return f"{prefix}{lnx:.6e} {lny:.6e} {lnz:.6e}"

    text = vertex_re.sub(replace_vertex, text)
    text = normal_re.sub(replace_normal, text)
    stl_path.write_text(text)


def main() -> int:
    pkg_dir = Path(__file__).parent.parent / "hexapod_full_pkg"

    transformed = 0
    for body_name, (position, quat) in MANIFEST.items():
        stl_path = pkg_dir / f"{body_name}.stl"
        if not stl_path.exists():
            print(f"  SKIP {body_name} — file not found")
            continue
        transform_stl(stl_path, position, quat)
        transformed += 1
        print(f"  OK   {body_name}")

    print(f"\nTransformed {transformed} STL files to body-local coordinates.")

    # Verify a few
    print("\nVerification (mesh centers should be near origin):")
    for name in ["Body_Chassis", "Coxa_L1", "Femur_L1", "Tibia_L1"]:
        stl_path = pkg_dir / f"{name}.stl"
        xs, ys, zs = [], [], []
        for line in stl_path.read_text().splitlines():
            m = re.match(r"\s+vertex\s+([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)", line)
            if m:
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
                zs.append(float(m.group(3)))
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        cz = (min(zs) + max(zs)) / 2
        print(f"  {name:20s}  center=({cx:7.2f}, {cy:7.2f}, {cz:7.2f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
