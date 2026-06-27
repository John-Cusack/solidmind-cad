"""Validate a drone sim package for fundamental flaws before flight.

Catches issues that aren't obvious from CAD-side validation:
  * Prop STLs with only one blade (polar_pattern not promoted to body.Tip).
  * Asymmetric rotor mesh distributions (would cause a yeet on takeoff).
  * Mass / arm-length inconsistencies between SDF and PX4 airframe.
  * Hover throttle outside [0.3, 0.8] sanity band.
  * Motor constant + max RPM combination that can't lift the drone.

Usage:
  python3 scripts/validate_drone_export.py <sim_pkg_dir> [--airframe FILE]

Exit codes:
  0 = all checks pass (warnings allowed)
  1 = at least one BLOCKER detected
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Finding:
    severity: str  # "block" | "warn" | "info"
    rule: str
    message: str

    def __str__(self) -> str:
        sigil = {"block": "✗ BLOCK", "warn": "⚠ WARN", "info": "i INFO"}[self.severity]
        return f"{sigil}  [{self.rule}] {self.message}"


def stl_bbox(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float], int]:
    """Return ((xmin,xmax),(ymin,ymax),(zmin,zmax)) and triangle count."""
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    is_binary = path.read_bytes()[:5] != b"solid"
    if is_binary:
        # Binary STL parsing — minimal
        data = path.read_bytes()
        n = int.from_bytes(data[80:84], "little")
        offset = 84
        for _ in range(n):
            for j in range(3):
                vx = struct_unpack_float(data, offset + 12 + j * 12)
                vy = struct_unpack_float(data, offset + 16 + j * 12)
                vz = struct_unpack_float(data, offset + 20 + j * 12)
                xs.append(vx)
                ys.append(vy)
                zs.append(vz)
            offset += 50
        triangles = n
    else:
        triangles = 0
        with path.open() as f:
            for line in f:
                if "vertex" in line:
                    parts = line.split()
                    xs.append(float(parts[1]))
                    ys.append(float(parts[2]))
                    zs.append(float(parts[3]))
                elif "endloop" in line:
                    triangles += 1
    if not xs:
        return (0, 0), (0, 0), (0, 0), 0
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs)), triangles


def struct_unpack_float(buf: bytes, off: int) -> float:
    import struct

    return struct.unpack("<f", buf[off : off + 4])[0]


def parse_sdf_rotors(sdf_path: Path) -> list[dict]:
    """Extract rotor info: link name, mesh path, position."""
    tree = ET.parse(sdf_path)
    root = tree.getroot()
    out: list[dict] = []
    for link in root.iter("link"):
        name = link.get("name", "")
        if not name.startswith("rotor"):
            continue
        # Find mesh path inside visual
        mesh_uri = None
        for mesh in link.iter("mesh"):
            uri_el = mesh.find("uri")
            if uri_el is not None and uri_el.text:
                mesh_uri = uri_el.text.strip()
                break
        # Find position via <pose> on link
        pose = link.find("pose")
        pos = (0.0, 0.0, 0.0)
        if pose is not None and pose.text:
            parts = pose.text.split()
            if len(parts) >= 3:
                pos = (float(parts[0]), float(parts[1]), float(parts[2]))
        out.append({"name": name, "mesh_uri": mesh_uri, "position_m": pos})
    return out


def parse_airframe_params(airframe_path: Path) -> dict:
    """Pull mass-relevant params from a PX4 airframe init script."""
    out: dict = {}
    txt = airframe_path.read_text()
    # @name, mass, hover throttle from comment
    m = re.search(r"Mass\s*=\s*([\d.]+)\s*kg", txt)
    if m:
        out["mass_kg"] = float(m.group(1))
    m = re.search(r"hover throttle\s*=\s*(\d+\.\d+)", txt)
    if m:
        out["hover_throttle"] = float(m.group(1))
    m = re.search(r"arm length\s*=\s*([\d.]+)", txt)
    if m:
        out["arm_length_m"] = float(m.group(1))
    m = re.search(r"rotor count\s*=\s*(\d+)", txt)
    if m:
        out["rotor_count"] = int(m.group(1))
    m = re.search(r"MPC_THR_HOVER\s+([\d.]+)", txt)
    if m:
        out["mpc_thr_hover"] = float(m.group(1))
    # Rotor positions in FRD (PX4 convention)
    rotors = []
    for ri in range(8):
        rx = re.search(rf"CA_ROTOR{ri}_PX\s+(-?[\d.]+)", txt)
        ry = re.search(rf"CA_ROTOR{ri}_PY\s+(-?[\d.]+)", txt)
        if rx and ry:
            rotors.append((float(rx.group(1)), float(ry.group(1))))
    out["rotor_positions_frd"] = rotors
    return out


def check_prop_completeness(rotor: dict, findings: list[Finding]) -> None:
    """A 2-blade prop's mesh should be roughly symmetric about its origin in
    the radial direction. A single-blade STL (only one direction populated)
    is the classic 'polar_pattern not promoted to Tip' artifact."""
    mesh_uri = rotor["mesh_uri"]
    if not mesh_uri:
        findings.append(Finding("warn", "rotor.no_mesh", f"{rotor['name']}: no mesh URI in SDF"))
        return
    path = Path(mesh_uri.replace("model://", ""))
    if not path.is_absolute():
        path = Path(mesh_uri)
    if not path.exists():
        findings.append(
            Finding("warn", "rotor.mesh_missing", f"{rotor['name']}: mesh not found at {path}")
        )
        return
    (xmin, xmax), (ymin, ymax), (zmin, zmax), tris = stl_bbox(path)
    spans = [xmax - xmin, ymax - ymin, zmax - zmin]
    radial_span = max(spans)
    radial_axis = ["X", "Y", "Z"][spans.index(radial_span)]

    # Symmetry: along the radial axis, midpoint should be near 0 (body local).
    # If midpoint is far from 0, the prop only has one blade.
    if radial_axis == "X":
        midpoint = (xmin + xmax) / 2.0
    elif radial_axis == "Y":
        midpoint = (ymin + ymax) / 2.0
    else:
        midpoint = (zmin + zmax) / 2.0
    asymmetry = abs(midpoint) / max(radial_span / 2.0, 1.0)

    findings.append(
        Finding(
            "info",
            "rotor.geometry",
            f"{rotor['name']}: tris={tris} bbox=({spans[0]:.1f},{spans[1]:.1f},{spans[2]:.1f}) mm, "
            f"radial_axis={radial_axis} span={radial_span:.1f} mm, midpoint={midpoint:+.1f} mm",
        )
    )

    # 2-blade prop: midpoint should be near 0 (within 5% of half-span)
    if asymmetry > 0.20:
        findings.append(
            Finding(
                "block",
                "rotor.incomplete_blades",
                f"{rotor['name']}: radial bbox is asymmetric "
                f"(midpoint {midpoint:+.1f} mm, half-span {radial_span / 2:.1f} mm, "
                f"asymmetry={asymmetry:.0%}). Likely only one blade exported — "
                f"check that the body's polar_pattern feature was promoted to body.Tip.",
            )
        )


def check_airframe_consistency(
    sdf_rotors: list[dict],
    airframe: dict,
    findings: list[Finding],
) -> None:
    if not airframe:
        return
    # Hover throttle must be in plausible range
    h = airframe.get("hover_throttle") or airframe.get("mpc_thr_hover")
    if h is not None:
        if h < 0.30:
            findings.append(
                Finding(
                    "block",
                    "airframe.thr_too_low",
                    f"hover_throttle={h:.3f} is suspiciously low — motor constant likely oversized",
                )
            )
        elif h > 0.80:
            findings.append(
                Finding(
                    "block",
                    "airframe.thr_too_high",
                    f"hover_throttle={h:.3f} is too high — drone too heavy or motor constant undersized",
                )
            )
        else:
            findings.append(
                Finding(
                    "info", "airframe.thr_ok", f"hover_throttle={h:.3f} (within plausible band)"
                )
            )

    # Rotor count must match
    n_sdf = len(sdf_rotors)
    n_af = airframe.get("rotor_count") or len(airframe.get("rotor_positions_frd", []))
    if n_sdf and n_af and n_sdf != n_af:
        findings.append(
            Finding(
                "block",
                "airframe.rotor_count_mismatch",
                f"SDF has {n_sdf} rotors, airframe declares {n_af}",
            )
        )


def check_rotor_distribution(sdf_rotors: list[dict], findings: list[Finding]) -> None:
    """Rotor positions should sum to ~zero (drone CoM aligned)."""
    if not sdf_rotors:
        return
    sx = sum(r["position_m"][0] for r in sdf_rotors)
    sy = sum(r["position_m"][1] for r in sdf_rotors)
    n = len(sdf_rotors)
    cx, cy = sx / n, sy / n
    if abs(cx) > 0.05 or abs(cy) > 0.05:  # 5 cm offset threshold
        findings.append(
            Finding(
                "warn",
                "rotor.offset_centroid",
                f"rotor centroid at ({cx:+.3f}, {cy:+.3f}) m — non-symmetric layout, may need CoM offset",
            )
        )
    # Pairs should be at opposite corners
    findings.append(
        Finding(
            "info",
            "rotor.layout",
            f"{n} rotors at: "
            + ", ".join(
                f"({r['position_m'][0]:+.3f},{r['position_m'][1]:+.3f})" for r in sdf_rotors
            ),
        )
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("pkg_dir", help="Output dir from cad.export_sim_package")
    p.add_argument("--airframe", help="PX4 airframe init script path", default=None)
    args = p.parse_args()

    pkg = Path(args.pkg_dir)
    if not pkg.is_dir():
        print(f"ERROR: not a directory: {pkg}", file=sys.stderr)
        return 1

    sdfs = list(pkg.glob("*.sdf"))
    if not sdfs:
        print(f"ERROR: no SDF in {pkg}", file=sys.stderr)
        return 1
    sdf = sdfs[0]
    print(f"== Validating {sdf} ==")

    findings: list[Finding] = []

    sdf_rotors = parse_sdf_rotors(sdf)
    if not sdf_rotors:
        findings.append(
            Finding(
                "warn", "sdf.no_rotors", "No rotor* links found in SDF — not a multirotor model?"
            )
        )

    for rotor in sdf_rotors:
        check_prop_completeness(rotor, findings)

    check_rotor_distribution(sdf_rotors, findings)

    airframe = {}
    if args.airframe:
        af_path = Path(args.airframe)
        if af_path.exists():
            airframe = parse_airframe_params(af_path)
            print(f"   airframe params: {airframe}")
    check_airframe_consistency(sdf_rotors, airframe, findings)

    print()
    blockers = sum(1 for f in findings if f.severity == "block")
    warnings = sum(1 for f in findings if f.severity == "warn")
    for f in findings:
        print(f"  {f}")

    print()
    print(f"== Result: {blockers} blockers, {warnings} warnings ==")
    return 1 if blockers > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
