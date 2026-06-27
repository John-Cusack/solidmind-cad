"""Merge multiple visual STL meshes per URDF link into single STL files.

For each link with >1 visual, reads all referenced STL files, applies
the visual origin transforms, and writes a combined STL.  Then updates
the URDF to reference the single merged mesh per link.

Usage::
    python scripts/merge_stl_per_link.py \
        hexapod_18dof_fresh_pkg/Hexapod18DOF_merged.urdf \
        hexapod_18dof_fresh_pkg/Hexapod18DOF_final.urdf
"""

from __future__ import annotations

import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np


def _parse_vec(s: str) -> np.ndarray:
    return np.array([float(x) for x in s.split()])


def _rpy_to_matrix(rpy: np.ndarray) -> np.ndarray:
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


def read_stl_binary(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Read binary STL, return (normals [N,3], vertices [N,3,3])."""
    with open(path, "rb") as f:
        f.read(80)
        n_tri = struct.unpack("<I", f.read(4))[0]
        normals = np.zeros((n_tri, 3), dtype=np.float32)
        vertices = np.zeros((n_tri, 3, 3), dtype=np.float32)
        for i in range(n_tri):
            data = struct.unpack("<12fH", f.read(50))
            normals[i] = data[0:3]
            vertices[i, 0] = data[3:6]
            vertices[i, 1] = data[6:9]
            vertices[i, 2] = data[9:12]
    return normals, vertices


def read_stl_ascii(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Read ASCII STL, return (normals [N,3], vertices [N,3,3])."""
    normals_list = []
    vertices_list = []
    with open(path) as f:
        tri_verts = []
        for line in f:
            line = line.strip()
            if line.startswith("facet normal"):
                parts = line.split()
                normals_list.append([float(parts[2]), float(parts[3]), float(parts[4])])
                tri_verts = []
            elif line.startswith("vertex"):
                parts = line.split()
                tri_verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif line.startswith("endfacet"):
                if len(tri_verts) == 3:
                    vertices_list.append(tri_verts)
    return np.array(normals_list, dtype=np.float32), np.array(vertices_list, dtype=np.float32)


def read_stl(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Auto-detect ASCII vs binary STL."""
    with open(path, "rb") as f:
        head = f.read(80)
    if head[:5] == b"solid" and b"\x00" not in head:
        try:
            return read_stl_ascii(path)
        except Exception:
            pass
    return read_stl_binary(path)


def write_stl_binary(path: str, normals: np.ndarray, vertices: np.ndarray) -> None:
    """Write binary STL."""
    n_tri = len(normals)
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", n_tri))
        for i in range(n_tri):
            f.write(struct.pack("<3f", *normals[i]))
            for j in range(3):
                f.write(struct.pack("<3f", *vertices[i, j]))
            f.write(struct.pack("<H", 0))  # attribute byte count


def transform_mesh(
    normals: np.ndarray,
    vertices: np.ndarray,
    xyz: np.ndarray,
    R: np.ndarray,
    scale: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply scale, rotation, translation to mesh."""
    # Scale vertices
    scaled_verts = vertices * scale.reshape(1, 1, 3)
    # Rotate and translate vertices
    n_tri = len(scaled_verts)
    flat = scaled_verts.reshape(-1, 3)
    transformed = (R @ flat.T).T + xyz
    new_verts = transformed.reshape(n_tri, 3, 3)
    # Rotate normals (no translation)
    new_normals = (R @ normals.T).T
    # Renormalize
    norms = np.linalg.norm(new_normals, axis=1, keepdims=True)
    norms[norms < 1e-10] = 1.0
    new_normals = new_normals / norms
    return new_normals.astype(np.float32), new_verts.astype(np.float32)


def merge_link_meshes(urdf_path: str, output_path: str) -> None:
    tree = ET.parse(urdf_path)
    root = tree.getroot()
    pkg_dir = Path(urdf_path).parent

    for link in root.findall("link"):
        link_name = link.get("name")

        for tag in ("visual", "collision"):
            elements = link.findall(tag)
            if len(elements) <= 1:
                continue

            # Merge all meshes for this tag
            all_normals = []
            all_vertices = []

            for elem in elements:
                mesh = elem.find(".//mesh")
                if mesh is None:
                    continue

                stl_path = mesh.get("filename")
                scale_str = mesh.get("scale", "1 1 1")
                scale = _parse_vec(scale_str)

                origin = elem.find("origin")
                if origin is not None:
                    xyz = _parse_vec(origin.get("xyz", "0 0 0"))
                    rpy = _parse_vec(origin.get("rpy", "0 0 0"))
                else:
                    xyz = np.zeros(3)
                    rpy = np.zeros(3)

                R = _rpy_to_matrix(rpy)

                normals, vertices = read_stl(stl_path)
                t_normals, t_vertices = transform_mesh(normals, vertices, xyz, R, scale)
                all_normals.append(t_normals)
                all_vertices.append(t_vertices)

            if not all_normals:
                continue

            # Concatenate
            merged_normals = np.concatenate(all_normals, axis=0)
            merged_vertices = np.concatenate(all_vertices, axis=0)

            # Write merged STL
            merged_name = f"{link_name}_merged.stl"
            merged_path = pkg_dir / merged_name
            write_stl_binary(str(merged_path), merged_normals, merged_vertices)

            # Remove all existing elements of this tag
            for elem in elements:
                link.remove(elem)

            # Add single element with merged mesh (scale already applied)
            new_elem = ET.SubElement(link, tag)
            new_origin = ET.SubElement(new_elem, "origin")
            new_origin.set("xyz", "0 0 0")
            new_origin.set("rpy", "0 0 0")
            new_geom = ET.SubElement(new_elem, "geometry")
            new_mesh = ET.SubElement(new_geom, "mesh")
            new_mesh.set("filename", str(merged_path.resolve()))
            # No scale — already baked in

            print(
                f"  {link_name}/{tag}: merged {len(elements)} meshes "
                f"({sum(len(n) for n in all_normals)} triangles) → {merged_name}"
            )

    ET.indent(tree, space="  ")
    tree.write(output_path, xml_declaration=True, encoding="utf-8")
    print(f"\nWritten to {output_path}")
    print(f"Links: {len(root.findall('link'))}, Joints: {len(root.findall('joint'))}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} input.urdf output.urdf")
        sys.exit(1)
    merge_link_meshes(sys.argv[1], sys.argv[2])
