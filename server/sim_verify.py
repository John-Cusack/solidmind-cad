"""Verification checks for the FreeCAD → URDF → Isaac Sim pipeline.

Compares data at three stages:
1. Mechanism definition vs FreeCAD model tree (pre-export)
2. Mechanism vs generated URDF file (post-export)
3. URDF vs Isaac USD scene (post-import)

All functions are pure — they take data dicts and return findings.
No network calls or FreeCAD dependencies.
"""

from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from typing import Any

from server.models import Finding, Severity
from server.motion_models import JointType, Mechanism

# ── Stage 1: Mechanism vs FreeCAD model tree ──────────────────────


def verify_mechanism_vs_tree(
    mechanism: Mechanism,
    model_tree_bodies: list[dict[str, Any]],
) -> list[Finding]:
    """Check that every mechanism part has a matching body in FreeCAD.

    ``model_tree_bodies`` is the ``bodies`` list from ``cad_get_model_tree``.
    """
    findings: list[Finding] = []

    # Build case-insensitive lookup
    body_labels: set[str] = set()
    body_labels_lower: set[str] = set()
    for b in model_tree_bodies:
        label = b.get("label", b.get("name", ""))
        body_labels.add(label)
        body_labels_lower.add(label.lower())

    for part in mechanism.parts:
        if part.is_ground:
            continue  # Ground link has no body

        name = part.body_name or part.id
        if name in body_labels:
            continue
        if name.lower() in body_labels_lower:
            continue

        findings.append(
            Finding(
                rule_id="mech_part_missing_body",
                severity=Severity.BLOCK,
                message=(
                    f"Mechanism part '{part.id}' (body_name='{name}') "
                    f"not found in FreeCAD model tree. "
                    f"Available bodies: {sorted(body_labels)}"
                ),
                field=f"parts.{part.id}",
            )
        )

    return findings


# ── Stage 2: Mechanism vs URDF file ──────────────────────────────


def verify_mechanism_vs_urdf(
    mechanism: Mechanism,
    urdf_path: str,
) -> list[Finding]:
    """Parse the URDF and compare against the mechanism definition.

    Checks:
    - Link count matches non-ground parts (+ base_link if present)
    - Joint count matches mechanism joints
    - Joint types match
    - Mesh files exist and are non-empty
    - Mass/inertia are present and plausible
    - Joint limits aren't silent defaults
    """
    findings: list[Finding] = []

    if not os.path.isfile(urdf_path):
        findings.append(
            Finding(
                rule_id="urdf_file_missing",
                severity=Severity.BLOCK,
                message=f"URDF file not found: {urdf_path}",
                field="urdf_path",
            )
        )
        return findings

    try:
        tree = ET.parse(urdf_path)
    except ET.ParseError as exc:
        findings.append(
            Finding(
                rule_id="urdf_parse_error",
                severity=Severity.BLOCK,
                message=f"URDF parse error: {exc}",
                field="urdf_path",
            )
        )
        return findings

    root = tree.getroot()

    # Collect URDF links and joints
    urdf_links: dict[str, ET.Element] = {}
    for link_el in root.findall("link"):
        name = link_el.get("name", "")
        if name:
            urdf_links[name] = link_el

    urdf_joints: dict[str, ET.Element] = {}
    for joint_el in root.findall("joint"):
        name = joint_el.get("name", "")
        if name:
            urdf_joints[name] = joint_el

    # --- Link count check ---
    mech_moving_parts = [p for p in mechanism.parts if not p.is_ground]
    # URDF may have base_link (ground clearance) which isn't in mechanism
    extra_links = {"base_link"}
    expected_link_names = {p.id for p in mech_moving_parts}
    urdf_link_names = set(urdf_links.keys())
    content_links = urdf_link_names - extra_links

    missing_links = expected_link_names - content_links
    for name in sorted(missing_links):
        findings.append(
            Finding(
                rule_id="urdf_missing_link",
                severity=Severity.WARN,
                message=f"Mechanism part '{name}' has no corresponding URDF link.",
                field=f"link.{name}",
            )
        )

    # --- Joint count check ---
    # Mechanism joints that map to URDF (exclude gear_mesh, belt_chain which become mimic)
    sim_joint_types = {
        JointType.REVOLUTE,
        JointType.PRISMATIC,
        JointType.FIXED,
        JointType.CONTINUOUS,
        JointType.PLANAR,
        JointType.GEAR_MESH,
        JointType.BELT_CHAIN,
        JointType.CAM,
    }
    mech_sim_joints = [j for j in mechanism.joints if j.joint_type in sim_joint_types]
    # URDF may also have a base_link_to_root fixed joint
    urdf_joint_names = set(urdf_joints.keys())

    # Don't count exact names — just compare counts with tolerance for base_link joint
    expected_min = len(mech_sim_joints)
    actual = len(urdf_joint_names)
    # Allow +1 for base_link joint, +N for any extra fixed joints
    if actual < expected_min:
        findings.append(
            Finding(
                rule_id="urdf_joint_count_low",
                severity=Severity.WARN,
                message=(
                    f"URDF has {actual} joints but mechanism defines "
                    f"{expected_min} joints. Some may not have been exported."
                ),
                field="joints",
            )
        )

    # --- Joint type check ---
    _TYPE_MAP = {
        "revolute": {"revolute"},
        "prismatic": {"prismatic"},
        "fixed": {"fixed"},
        "continuous": {"continuous"},
        "planar": {"planar"},
        "gear_mesh": {"revolute"},
        "belt_chain": {"revolute"},
        "cam": {"revolute"},
    }
    for mj in mechanism.joints:
        # Try to find matching URDF joint
        urdf_j = urdf_joints.get(mj.id)
        if urdf_j is None:
            continue
        urdf_type = urdf_j.get("type", "")
        expected_types = _TYPE_MAP.get(mj.joint_type.value, set())
        if urdf_type and expected_types and urdf_type not in expected_types:
            findings.append(
                Finding(
                    rule_id="urdf_joint_type_mismatch",
                    severity=Severity.WARN,
                    message=(
                        f"Joint '{mj.id}': mechanism type '{mj.joint_type.value}' "
                        f"expected URDF type in {sorted(expected_types)}, "
                        f"got '{urdf_type}'."
                    ),
                    field=f"joint.{mj.id}",
                )
            )

    # --- Mesh file checks ---
    urdf_dir = os.path.dirname(os.path.abspath(urdf_path))
    for link_name, link_el in urdf_links.items():
        if link_name in extra_links:
            continue
        visual = link_el.find("visual")
        if visual is None:
            findings.append(
                Finding(
                    rule_id="urdf_link_no_visual",
                    severity=Severity.NOTE,
                    message=f"Link '{link_name}' has no <visual> element.",
                    field=f"link.{link_name}",
                )
            )
            continue
        mesh_el = visual.find(".//mesh")
        if mesh_el is None:
            continue
        filename = mesh_el.get("filename", "")
        if not filename:
            continue
        # Resolve relative path
        if not os.path.isabs(filename):
            mesh_path = os.path.join(urdf_dir, filename)
        else:
            mesh_path = filename
        if not os.path.isfile(mesh_path):
            findings.append(
                Finding(
                    rule_id="urdf_mesh_missing",
                    severity=Severity.BLOCK,
                    message=f"Link '{link_name}': mesh file not found: {filename}",
                    field=f"link.{link_name}.visual.mesh",
                )
            )
        elif os.path.getsize(mesh_path) == 0:
            findings.append(
                Finding(
                    rule_id="urdf_mesh_empty",
                    severity=Severity.BLOCK,
                    message=f"Link '{link_name}': mesh file is empty: {filename}",
                    field=f"link.{link_name}.visual.mesh",
                )
            )
        else:
            # Check mesh has 3D extent (not flat/degenerate)
            try:
                from server.sim_export import validate_stl_extent

                mesh_extent_findings = validate_stl_extent(mesh_path, link_name)
                findings.extend(mesh_extent_findings)
            except ImportError:
                pass

    # --- Mass / inertia checks ---
    for link_name, link_el in urdf_links.items():
        if link_name in extra_links:
            continue
        inertial = link_el.find("inertial")
        if inertial is None:
            findings.append(
                Finding(
                    rule_id="urdf_link_no_inertial",
                    severity=Severity.NOTE,
                    message=f"Link '{link_name}' has no <inertial> element.",
                    field=f"link.{link_name}",
                )
            )
            continue

        mass_el = inertial.find("mass")
        if mass_el is not None:
            mass_val = float(mass_el.get("value", "0"))
            if mass_val <= 0:
                findings.append(
                    Finding(
                        rule_id="urdf_link_zero_mass",
                        severity=Severity.WARN,
                        message=f"Link '{link_name}' has zero or negative mass ({mass_val}).",
                        field=f"link.{link_name}.mass",
                    )
                )

        inertia_el = inertial.find("inertia")
        if inertia_el is not None:
            ixx = float(inertia_el.get("ixx", "0"))
            iyy = float(inertia_el.get("iyy", "0"))
            izz = float(inertia_el.get("izz", "0"))
            if ixx <= 0 or iyy <= 0 or izz <= 0:
                findings.append(
                    Finding(
                        rule_id="urdf_link_bad_inertia",
                        severity=Severity.WARN,
                        message=(
                            f"Link '{link_name}' has non-positive diagonal inertia: "
                            f"ixx={ixx}, iyy={iyy}, izz={izz}."
                        ),
                        field=f"link.{link_name}.inertia",
                    )
                )

    # --- Joint limit checks ---
    _DEFAULT_LIMIT_RAD = math.radians(60)
    for joint_name, joint_el in urdf_joints.items():
        jtype = joint_el.get("type", "")
        if jtype not in ("revolute", "prismatic"):
            continue
        limit_el = joint_el.find("limit")
        if limit_el is None:
            findings.append(
                Finding(
                    rule_id="urdf_joint_no_limits",
                    severity=Severity.WARN,
                    message=f"Joint '{joint_name}' ({jtype}) has no <limit> element.",
                    field=f"joint.{joint_name}",
                )
            )
            continue
        lower = float(limit_el.get("lower", "0"))
        upper = float(limit_el.get("upper", "0"))
        # Check for silent ±60° default
        if math.isclose(abs(lower), _DEFAULT_LIMIT_RAD, rel_tol=0.01) and math.isclose(
            abs(upper), _DEFAULT_LIMIT_RAD, rel_tol=0.01
        ):
            # Check if the mechanism explicitly set these limits
            mj = mechanism.get_joint(joint_name)
            if mj and mj.min_angle_deg is None and mj.max_angle_deg is None:
                findings.append(
                    Finding(
                        rule_id="urdf_joint_default_limits",
                        severity=Severity.NOTE,
                        message=(
                            f"Joint '{joint_name}' uses default ±60° limits. "
                            f"Mechanism did not specify limits — these are auto-generated."
                        ),
                        field=f"joint.{joint_name}.limits",
                    )
                )

    return findings


# ── Stage 3: URDF vs Isaac USD scene ────────────────────────────


def verify_urdf_vs_diagnose(
    urdf_path: str,
    diagnose: dict[str, Any],
) -> list[Finding]:
    """Compare a URDF against an engine's normalized ``diagnose`` report.

    One check for every engine: ``diagnose`` results are normalized by the
    contract (generic joint-type counts, DOF, per-joint connectivity), so
    nothing here knows which engine produced them
    (``docs/engine-contract.md`` §3.2).

    Findings are advisory — an engine legitimately merges fixed joints, so
    only *missing* actuated joints and disconnected/undriven ones are flagged.
    """
    findings: list[Finding] = []

    if diagnose.get("error"):
        findings.append(
            Finding(
                rule_id="diagnose_error",
                severity=Severity.BLOCK,
                message=f"Engine diagnose failed: {diagnose['error']}",
                field="diagnose",
            )
        )
        return findings

    if not os.path.isfile(urdf_path):
        return findings
    try:
        tree = ET.parse(urdf_path)
    except ET.ParseError:
        return findings

    root = tree.getroot()
    urdf_joints_el = root.findall("joint")
    urdf_revolute = sum(1 for j in urdf_joints_el if j.get("type") == "revolute")
    urdf_continuous = sum(1 for j in urdf_joints_el if j.get("type") == "continuous")
    urdf_prismatic = sum(1 for j in urdf_joints_el if j.get("type") == "prismatic")
    urdf_total_joints = len(urdf_joints_el)

    joint_counts = diagnose.get("joint_counts", {})
    engine_total = int(diagnose.get("joint_total", sum(joint_counts.values())))
    engine_revolute = int(joint_counts.get("revolute", 0))
    engine_prismatic = int(joint_counts.get("prismatic", 0))

    if engine_total < urdf_total_joints:
        findings.append(
            Finding(
                rule_id="diagnose_joint_count_low",
                severity=Severity.WARN,
                message=(
                    f"Engine reports {engine_total} joints but the URDF defines "
                    f"{urdf_total_joints}. Joints may have been silently dropped."
                ),
                field="diagnose.joints",
            )
        )

    # URDF's continuous joints are revolute-without-limits everywhere else.
    urdf_revolute_like = urdf_revolute + urdf_continuous
    if engine_revolute < urdf_revolute_like:
        findings.append(
            Finding(
                rule_id="diagnose_revolute_count_low",
                severity=Severity.WARN,
                message=(
                    f"Engine reports {engine_revolute} revolute joints but the URDF "
                    f"defines {urdf_revolute_like} (revolute={urdf_revolute}, "
                    f"continuous={urdf_continuous}). "
                    "Some revolute joints may not have imported correctly."
                ),
                field="diagnose.joints.revolute",
            )
        )

    if engine_prismatic < urdf_prismatic:
        findings.append(
            Finding(
                rule_id="diagnose_prismatic_count_low",
                severity=Severity.WARN,
                message=(
                    f"Engine reports {engine_prismatic} prismatic joints but the URDF "
                    f"defines {urdf_prismatic}."
                ),
                field="diagnose.joints.prismatic",
            )
        )

    urdf_actuated = urdf_revolute + urdf_continuous + urdf_prismatic
    if "dof_count" in diagnose:
        dof_count = int(diagnose["dof_count"])
        if dof_count < urdf_actuated:
            findings.append(
                Finding(
                    rule_id="diagnose_dof_count_low",
                    severity=Severity.WARN,
                    message=(
                        f"Engine reports {dof_count} DOFs but the URDF defines "
                        f"{urdf_actuated} actuated joints. "
                        f"DOF names: {diagnose.get('dof_names', [])}"
                    ),
                    field="diagnose.dof",
                )
            )
    else:
        findings.append(
            Finding(
                rule_id="diagnose_no_dof_report",
                severity=Severity.NOTE,
                message="Engine reported no articulation DOF information.",
                field="diagnose.dof",
            )
        )

    for joint in diagnose.get("joints", []):
        name = joint.get("name", "?")
        if joint.get("connected") is False:
            findings.append(
                Finding(
                    rule_id="diagnose_joint_disconnected",
                    severity=Severity.WARN,
                    message=(
                        f"Joint '{name}' is missing a body target — it will have "
                        "no effect in the simulation."
                    ),
                    field=f"diagnose.joint.{name}",
                )
            )
        if joint.get("type") in ("revolute", "prismatic") and joint.get("has_drive") is False:
            findings.append(
                Finding(
                    rule_id="diagnose_joint_no_drive",
                    severity=Severity.WARN,
                    message=(
                        f"Joint '{name}' has no drive (stiffness and damping are "
                        "both zero) — it has no actuation force."
                    ),
                    field=f"diagnose.joint.{name}.drive",
                )
            )

    return findings


# ── Combined verification ────────────────────────────────────────


def verify_sim_package(
    mechanism: Mechanism,
    model_tree_bodies: list[dict[str, Any]] | None = None,
    urdf_path: str | None = None,
    diagnose: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run all available verification stages and return a combined report.

    Each stage is optional — runs only if data is provided.
    """
    all_findings: list[Finding] = []
    stages_run: list[str] = []

    # Stage 1: Mechanism vs FreeCAD
    if model_tree_bodies is not None:
        stage1 = verify_mechanism_vs_tree(mechanism, model_tree_bodies)
        all_findings.extend(stage1)
        stages_run.append("mechanism_vs_freecad")

    # Stage 2: Mechanism vs URDF
    if urdf_path is not None:
        stage2 = verify_mechanism_vs_urdf(mechanism, urdf_path)
        all_findings.extend(stage2)
        stages_run.append("mechanism_vs_urdf")

    # Stage 3: URDF vs the engine's normalized diagnose report
    if urdf_path is not None and diagnose is not None:
        stage3 = verify_urdf_vs_diagnose(urdf_path, diagnose)
        all_findings.extend(stage3)
        stages_run.append("urdf_vs_diagnose")

    blockers = [f for f in all_findings if f.severity == Severity.BLOCK]
    warnings = [f for f in all_findings if f.severity == Severity.WARN]
    notes = [f for f in all_findings if f.severity == Severity.NOTE]

    return {
        "stages_run": stages_run,
        "total_findings": len(all_findings),
        "blockers": len(blockers),
        "warnings": len(warnings),
        "notes": len(notes),
        "findings": [f.to_dict() for f in all_findings],
        "passed": len(blockers) == 0,
    }
