#!/usr/bin/env python3
"""Generate deterministic binary STL test fixtures (stdlib only).

Creates minimal box meshes for the simple_2body test fixture:
- Chassis.stl — 60×40×10mm box centered at origin
- Arm.stl     — 80×20×10mm box centered at origin

Run once, commit outputs.  The STLs are ~684 bytes each (12 triangles).
"""

from __future__ import annotations

import struct
from pathlib import Path


def _write_binary_stl(
    path: Path,
    triangles: list[
        tuple[
            tuple[float, float, float],  # normal
            tuple[float, float, float],  # v1
            tuple[float, float, float],  # v2
            tuple[float, float, float],  # v3
        ]
    ],
) -> None:
    """Write a binary STL file from a list of triangles."""
    with open(path, "wb") as f:
        # 80-byte header (zeroed)
        f.write(b"\x00" * 80)
        # Triangle count (uint32)
        f.write(struct.pack("<I", len(triangles)))
        for normal, v1, v2, v3 in triangles:
            # Normal vector (3 × float32)
            f.write(struct.pack("<3f", *normal))
            # Vertices (3 × 3 × float32)
            f.write(struct.pack("<3f", *v1))
            f.write(struct.pack("<3f", *v2))
            f.write(struct.pack("<3f", *v3))
            # Attribute byte count (uint16, unused)
            f.write(struct.pack("<H", 0))


def _box_triangles(
    dx: float,
    dy: float,
    dz: float,
) -> list[
    tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
]:
    """Generate 12 triangles for an axis-aligned box centered at origin.

    Box spans [-dx/2, dx/2] × [-dy/2, dy/2] × [-dz/2, dz/2].
    """
    hx, hy, hz = dx / 2.0, dy / 2.0, dz / 2.0
    # 8 corners
    c = [
        (-hx, -hy, -hz),  # 0
        (hx, -hy, -hz),  # 1
        (hx, hy, -hz),  # 2
        (-hx, hy, -hz),  # 3
        (-hx, -hy, hz),  # 4
        (hx, -hy, hz),  # 5
        (hx, hy, hz),  # 6
        (-hx, hy, hz),  # 7
    ]
    # 6 faces, 2 triangles each (outward normals)
    faces = [
        # -Z face (bottom)
        ((0.0, 0.0, -1.0), c[0], c[2], c[1]),
        ((0.0, 0.0, -1.0), c[0], c[3], c[2]),
        # +Z face (top)
        ((0.0, 0.0, 1.0), c[4], c[5], c[6]),
        ((0.0, 0.0, 1.0), c[4], c[6], c[7]),
        # -Y face (front)
        ((0.0, -1.0, 0.0), c[0], c[1], c[5]),
        ((0.0, -1.0, 0.0), c[0], c[5], c[4]),
        # +Y face (back)
        ((0.0, 1.0, 0.0), c[2], c[3], c[7]),
        ((0.0, 1.0, 0.0), c[2], c[7], c[6]),
        # -X face (left)
        ((-1.0, 0.0, 0.0), c[0], c[4], c[7]),
        ((-1.0, 0.0, 0.0), c[0], c[7], c[3]),
        # +X face (right)
        ((1.0, 0.0, 0.0), c[1], c[2], c[6]),
        ((1.0, 0.0, 0.0), c[1], c[6], c[5]),
    ]
    return faces


def main() -> None:
    fixture_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "simple_2body"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    # Chassis: 60×40×10mm box centered at origin
    chassis_tris = _box_triangles(60.0, 40.0, 10.0)
    chassis_path = fixture_dir / "Chassis.stl"
    _write_binary_stl(chassis_path, chassis_tris)
    print(
        f"Wrote {chassis_path} ({chassis_path.stat().st_size} bytes, {len(chassis_tris)} triangles)"
    )

    # Arm: 80×20×10mm box centered at origin
    arm_tris = _box_triangles(80.0, 20.0, 10.0)
    arm_path = fixture_dir / "Arm.stl"
    _write_binary_stl(arm_path, arm_tris)
    print(f"Wrote {arm_path} ({arm_path.stat().st_size} bytes, {len(arm_tris)} triangles)")


if __name__ == "__main__":
    main()
