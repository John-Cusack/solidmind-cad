"""Pre-merge fixed joints in a URDF so Isaac Sim doesn't have to.

For each fixed joint, merges the child link's visual/collision/inertial
into the parent link, then removes the fixed joint and child link.
Revolute/prismatic joints that referenced the child as parent are
re-parented to the original parent with updated origins.

This eliminates the Isaac Sim 5.1 fabric rendering bug where merged
fixed-joint visual meshes get broken paths (node_STL_BINARY_).

Usage:
    python scripts/merge_fixed_joints_urdf.py input.urdf output.urdf
"""

from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET

import numpy as np


def _parse_xyz(s: str) -> np.ndarray:
    return np.array([float(x) for x in s.split()])


def _parse_rpy(s: str) -> np.ndarray:
    return np.array([float(x) for x in s.split()])


def _format_vec(v: np.ndarray) -> str:
    return " ".join(f"{x:.6g}" for x in v)


def _rpy_to_matrix(rpy: np.ndarray) -> np.ndarray:
    """RPY (XYZ extrinsic) to 3x3 rotation matrix."""
    r, p, y = rpy
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _matrix_to_rpy(R: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix to RPY (XYZ extrinsic)."""
    if abs(R[2, 0]) < 1.0 - 1e-6:
        p = math.asin(-R[2, 0])
        r = math.atan2(R[2, 1], R[2, 2])
        y = math.atan2(R[1, 0], R[0, 0])
    else:
        p = math.copysign(math.pi / 2, -R[2, 0])
        r = math.atan2(-R[1, 2], R[1, 1])
        y = 0.0
    return np.array([r, p, y])


def _build_transform(origin_elem: ET.Element | None) -> tuple[np.ndarray, np.ndarray]:
    """Extract (xyz, R) from an <origin> element."""
    if origin_elem is None:
        return np.zeros(3), np.eye(3)
    xyz = _parse_xyz(origin_elem.get("xyz", "0 0 0"))
    rpy = _parse_rpy(origin_elem.get("rpy", "0 0 0"))
    return xyz, _rpy_to_matrix(rpy)


def _compose_transforms(
    xyz1: np.ndarray,
    R1: np.ndarray,
    xyz2: np.ndarray,
    R2: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Compose two transforms: T1 * T2."""
    return R1 @ xyz2 + xyz1, R1 @ R2


def _set_origin(elem: ET.Element, xyz: np.ndarray, R: np.ndarray) -> None:
    """Set or create <origin> on elem with given xyz and rotation matrix."""
    rpy = _matrix_to_rpy(R)
    origin = elem.find("origin")
    if origin is None:
        origin = ET.SubElement(elem, "origin")
    origin.set("xyz", _format_vec(xyz))
    origin.set("rpy", _format_vec(rpy))


def merge_fixed_joints(urdf_path: str, output_path: str) -> None:
    tree = ET.parse(urdf_path)
    root = tree.getroot()

    # Index links and joints
    links = {link_el.get("name"): link_el for link_el in root.findall("link")}
    joints = list(root.findall("joint"))

    # Find fixed joints
    fixed_joints = [j for j in joints if j.get("type") == "fixed"]

    for fj in fixed_joints:
        parent_name = fj.find("parent").get("link")
        child_name = fj.find("child").get("link")
        parent_link = links.get(parent_name)
        child_link = links.get(child_name)

        if parent_link is None or child_link is None:
            continue

        # Get fixed joint transform
        fj_xyz, fj_R = _build_transform(fj.find("origin"))

        # Move child's visual/collision elements to parent with transformed origins
        for tag in ("visual", "collision"):
            for elem in list(child_link.findall(tag)):
                # Get element's local origin
                elem_xyz, elem_R = _build_transform(elem.find("origin"))
                # Compose: parent_frame -> fixed_joint -> element
                new_xyz, new_R = _compose_transforms(fj_xyz, fj_R, elem_xyz, elem_R)
                _set_origin(elem, new_xyz, new_R)
                # Move to parent
                child_link.remove(elem)
                parent_link.append(elem)

        # Re-parent any joints that had child_name as parent
        for j in root.findall("joint"):
            if j is fj:
                continue
            p = j.find("parent")
            if p is not None and p.get("link") == child_name:
                # Compose this joint's origin with the fixed joint transform
                j_xyz, j_R = _build_transform(j.find("origin"))
                new_xyz, new_R = _compose_transforms(fj_xyz, fj_R, j_xyz, j_R)
                _set_origin(j, new_xyz, new_R)
                p.set("link", parent_name)

        # Remove fixed joint and child link
        root.remove(fj)
        root.remove(child_link)
        del links[child_name]

    # Write output
    ET.indent(tree, space="  ")
    tree.write(output_path, xml_declaration=True, encoding="utf-8")
    print(f"Merged {len(fixed_joints)} fixed joints")
    print(f"Links: {len(root.findall('link'))}, Joints: {len(root.findall('joint'))}")
    print(f"Written to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} input.urdf output.urdf")
        sys.exit(1)
    merge_fixed_joints(sys.argv[1], sys.argv[2])
