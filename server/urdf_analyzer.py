"""URDF analysis using stdlib XML parsing.

Extracts joint topology, morphology classification, mass, and standing
height from a URDF file.  No Isaac or FreeCAD dependencies — pure
Python stdlib.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class URDFAnalysis:
    """Parsed URDF analysis result."""

    robot_name: str
    actuated_joints: tuple[str, ...]
    joint_types: dict[str, str]
    joint_limits: dict[str, tuple[float, float]]
    joint_effort: dict[str, float]
    joint_velocity: dict[str, float]
    joint_damping: dict[str, float]
    morphology: str
    base_link: str
    foot_links: tuple[str, ...]
    total_mass_kg: float
    standing_height_m: float
    max_leg_reach_m: float


def _parse_xyz(element: ET.Element | None) -> tuple[float, float, float]:
    """Extract xyz from an element's 'xyz' attribute."""
    if element is None:
        return (0.0, 0.0, 0.0)
    xyz_str = element.get("xyz", "0 0 0")
    parts = xyz_str.split()
    if len(parts) != 3:
        return (0.0, 0.0, 0.0)
    return (float(parts[0]), float(parts[1]), float(parts[2]))


def _classify_morphology(
    actuated_joints: list[str],
    joint_types: dict[str, str],
) -> str:
    """Classify robot morphology from actuated joint count and names."""
    revolute_joints = [
        j for j in actuated_joints if joint_types.get(j) == "revolute"
    ]
    n = len(revolute_joints)

    # Check for hip naming pattern (hexapod convention)
    hip_joints = [j for j in revolute_joints if "hip" in j.lower()]

    if n == 6 and len(hip_joints) == 6:
        return "hexapod_1dof"
    if n == 12:
        return "quadruped"
    if n == 18 and len(hip_joints) >= 6:
        return "hexapod_3dof"
    if n == 2:
        return "biped"
    return "unknown"


def _find_foot_links(
    links: set[str],
    parent_map: dict[str, str],
    child_map: dict[str, list[str]],
    collision_links: set[str],
) -> list[str]:
    """Find leaf links with collision geometry (likely feet)."""
    feet: list[str] = []
    for link_name in sorted(links):
        # Leaf = no children in joint tree
        if link_name not in child_map or not child_map[link_name]:
            if link_name in collision_links:
                feet.append(link_name)
    return feet


def _compute_standing_height(
    root: ET.Element,
    base_link: str,
) -> float:
    """Estimate standing height from base_link fixed joint offsets.

    Walks the fixed joint chain from base_link downward and sums Z
    offsets.  For robots with a base_link → chassis fixed joint, this
    gives the ground clearance set by the URDF author.
    """
    joints = root.findall("joint")
    # Build parent→child fixed joint map with Z offsets
    fixed_z: dict[str, list[tuple[str, float]]] = {}
    for joint_el in joints:
        if joint_el.get("type") != "fixed":
            continue
        parent_el = joint_el.find("parent")
        child_el = joint_el.find("child")
        if parent_el is None or child_el is None:
            continue
        parent = parent_el.get("link", "")
        child = child_el.get("link", "")
        origin = joint_el.find("origin")
        _, _, z = _parse_xyz(origin)
        fixed_z.setdefault(parent, []).append((child, z))

    # Walk from base_link, accumulating Z offset
    max_z = 0.0
    stack = [(base_link, 0.0)]
    while stack:
        link, cumulative_z = stack.pop()
        if abs(cumulative_z) > abs(max_z):
            max_z = cumulative_z
        for child, dz in fixed_z.get(link, []):
            stack.append((child, cumulative_z + dz))

    return abs(max_z)


def _compute_max_leg_reach(
    root: ET.Element,
    foot_links: list[str],
    base_link: str,
) -> float:
    """Compute the maximum leg reach from the first revolute joint to foot tip.

    Walks from each foot link back to the base, summing Euclidean distances
    between consecutive joints.  Only counts segments from the first revolute
    joint outward (the vertical chain from base to the first hip is part of
    the body, not the leg).

    Returns the maximum reach across all legs, or 0.0 if no legs found.
    """
    if not foot_links:
        return 0.0

    # Build child→parent map with origin xyz per joint
    joint_info: dict[str, tuple[str, str, str, tuple[float, float, float]]] = {}
    # key = child_link, value = (joint_name, joint_type, parent_link, origin_xyz)
    for joint_el in root.findall("joint"):
        jname = joint_el.get("name", "")
        jtype = joint_el.get("type", "fixed")
        parent_el = joint_el.find("parent")
        child_el = joint_el.find("child")
        if parent_el is None or child_el is None:
            continue
        parent_link = parent_el.get("link", "")
        child_link = child_el.get("link", "")
        origin = joint_el.find("origin")
        xyz = _parse_xyz(origin)
        joint_info[child_link] = (jname, jtype, parent_link, xyz)

    max_reach = 0.0

    for foot in foot_links:
        # Walk from foot back to base, collecting (joint_type, distance) pairs
        chain: list[tuple[str, float]] = []
        link = foot
        while link and link != base_link and link in joint_info:
            jname, jtype, parent_link, xyz = joint_info[link]
            dist = math.sqrt(xyz[0] ** 2 + xyz[1] ** 2 + xyz[2] ** 2)
            chain.append((jtype, dist))
            link = parent_link

        # chain is foot→base order; reverse to base→foot
        chain.reverse()

        # Sum distances from the first revolute joint onward
        leg_reach = 0.0
        past_first_revolute = False
        for jtype, dist in chain:
            if jtype != "fixed" and not past_first_revolute:
                past_first_revolute = True
            if past_first_revolute:
                leg_reach += dist

        if leg_reach > max_reach:
            max_reach = leg_reach

    return max_reach


def analyze_urdf(urdf_path: str | Path) -> URDFAnalysis:
    """Parse a URDF file and return structural analysis.

    Args:
        urdf_path: Path to the URDF XML file.

    Returns:
        URDFAnalysis with joint topology, morphology, mass, etc.

    Raises:
        FileNotFoundError: If the URDF file doesn't exist.
        ET.ParseError: If the URDF is not valid XML.
    """
    path = Path(urdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"URDF file not found: {path}")

    tree = ET.parse(path)
    root = tree.getroot()

    robot_name = root.get("name", "unknown")

    # Parse all links
    links: set[str] = set()
    collision_links: set[str] = set()
    link_masses: dict[str, float] = {}
    for link_el in root.findall("link"):
        name = link_el.get("name", "")
        if not name:
            continue
        links.add(name)
        if link_el.find("collision") is not None:
            collision_links.add(name)
        inertial = link_el.find("inertial")
        if inertial is not None:
            mass_el = inertial.find("mass")
            if mass_el is not None:
                try:
                    link_masses[name] = float(mass_el.get("value", "0"))
                except ValueError:
                    pass

    # Parse all joints
    joint_types: dict[str, str] = {}
    joint_limits: dict[str, tuple[float, float]] = {}
    joint_effort: dict[str, float] = {}
    joint_velocity: dict[str, float] = {}
    joint_damping: dict[str, float] = {}
    actuated_joints: list[str] = []
    parent_map: dict[str, str] = {}  # child_link → parent_link
    child_map: dict[str, list[str]] = {}  # parent_link → [child_links]

    for joint_el in root.findall("joint"):
        jname = joint_el.get("name", "")
        jtype = joint_el.get("type", "fixed")
        if not jname:
            continue

        joint_types[jname] = jtype

        parent_el = joint_el.find("parent")
        child_el = joint_el.find("child")
        if parent_el is not None and child_el is not None:
            parent_link = parent_el.get("link", "")
            child_link = child_el.get("link", "")
            if parent_link and child_link:
                parent_map[child_link] = parent_link
                child_map.setdefault(parent_link, []).append(child_link)

        # Non-fixed joints are actuated
        if jtype != "fixed":
            actuated_joints.append(jname)

            # Parse limits
            limit_el = joint_el.find("limit")
            if limit_el is not None:
                try:
                    lo = float(limit_el.get("lower", "0"))
                    hi = float(limit_el.get("upper", "0"))
                    joint_limits[jname] = (lo, hi)
                except ValueError:
                    pass
                try:
                    joint_effort[jname] = float(limit_el.get("effort", "0"))
                except ValueError:
                    pass
                try:
                    joint_velocity[jname] = float(limit_el.get("velocity", "0"))
                except ValueError:
                    pass

            # Parse dynamics
            dynamics_el = joint_el.find("dynamics")
            if dynamics_el is not None:
                try:
                    joint_damping[jname] = float(dynamics_el.get("damping", "0"))
                except ValueError:
                    pass

    # Determine base link (root of the tree — no parent)
    child_links = set(parent_map.keys())
    root_links = links - child_links
    base_link = sorted(root_links)[0] if root_links else ""

    # Find foot links
    foot_links = _find_foot_links(links, parent_map, child_map, collision_links)

    # Total mass
    total_mass = sum(link_masses.values())

    # Standing height
    standing_height = _compute_standing_height(root, base_link)

    # Max leg reach (first revolute joint to foot tip)
    max_leg_reach = _compute_max_leg_reach(root, foot_links, base_link)

    # Classify morphology
    morphology = _classify_morphology(actuated_joints, joint_types)

    return URDFAnalysis(
        robot_name=robot_name,
        actuated_joints=tuple(actuated_joints),
        joint_types=joint_types,
        joint_limits=joint_limits,
        joint_effort=joint_effort,
        joint_velocity=joint_velocity,
        joint_damping=joint_damping,
        morphology=morphology,
        base_link=base_link,
        foot_links=tuple(foot_links),
        total_mass_kg=round(total_mass, 6),
        standing_height_m=round(standing_height, 6),
        max_leg_reach_m=round(max_leg_reach, 6),
    )
