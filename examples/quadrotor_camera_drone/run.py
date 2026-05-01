"""End-to-end FreeCAD → PX4 SITL flight pipeline.

Builds a 700 mm wheelbase quadrotor in FreeCAD, generates the SDF (with
canonical multicopter motor model plugins + IMU/GPS/baro/magnetometer
sensors) and a custom PX4 airframe init script, rebuilds PX4 SITL with
the new airframe, launches PX4 + Gazebo, then flies a hover-takeoff-land
sequence under offboard MAVLink control through the SolidMind bridge.

This is the dogfood path the demo recording will use.  Phase 6 (the
recording) waits on this running cleanly for ~1 week.

Prerequisites
-------------
1. **FreeCAD** 1.1+ running with the SolidMind addon (``freecad &``)
2. **PX4-Autopilot** built once: see ``docs/px4_integration.md``
3. **Gazebo Harmonic 10+** installed (auto-confirmed by Phase 1)
4. **pymavlink** installed: ``pip install -e ".[drone]"``

Run from the repo root::

    PYTHONPATH=. python3 examples/quadrotor_camera_drone/run.py \\
        --output-dir /tmp/camera_drone \\
        --takeoff-alt 5.0 \\
        --hover-secs 15

The script is staged so you can stop after any phase by passing
``--stop-after build|export|rebuild|launch|fly``.  Useful when iterating
on geometry without re-running the full PX4 rebuild every time.

Architecture
------------
Phase 1: Build CAD geometry via cad.* MCP tool helpers (FreeCAD addon)
Phase 2: Define rotor mechanism + export sim package + airframe params
Phase 3: Rebuild PX4 SITL with the new airframe registered
Phase 4: Launch PX4 + Gazebo via subprocess
Phase 5: Connect MavlinkController, arm, set OFFBOARD, takeoff, hover, land

Each phase prints a banner so progress is visible during the recording.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("solidmind.examples.camera_drone")


# ---------------------------------------------------------------------------
# Drone parameters
# ---------------------------------------------------------------------------

# Quadrotor in X configuration: motors at the four corners of a square,
# arms emanating diagonally from the chassis centre.
WHEELBASE_MM = 700.0           # corner-to-corner across the X
ARM_OFFSET_MM = WHEELBASE_MM / (2 * math.sqrt(2))   # ≈ 247.5 mm
ROTOR_RADIUS_MM = 100.0
ROTOR_THICKNESS_MM = 5.0

# 3-blade prop geometry (single-sketch, 3 trapezoid blades + hub).
PROP_HUB_RADIUS_MM = 10.0
PROP_BLADE_LEN_MM = 90.0       # blade length from hub edge outward
PROP_BLADE_INNER_W_MM = 18.0   # blade width at hub
PROP_BLADE_OUTER_W_MM = 8.0    # blade width at tip
PROP_BLADE_COUNT = 3

CHASSIS_W_MM = 200.0
CHASSIS_H_MM = 200.0
CHASSIS_T_MM = 30.0

# Estimated masses — small enough that even our default motor constants
# can lift the drone with hover throttle in the safe range.
CHASSIS_MASS_KG = 1.2
ROTOR_MASS_KG = 0.05

ROTORS = [
    # (name, dx_mm, dy_mm, direction) — FLU body frame
    # (+X forward, +Y left, +Z up).  Standard PX4 X-quad layout:
    # motor 0 front-right, 1 rear-left (CCW pair), 2 front-left,
    # 3 rear-right (CW pair).  rotor_FR sits at +X (forward) and
    # -Y (right in FLU); the CA_ROTOR conversion in
    # server.px4_airframe_generator.extract_rotors() negates Y for
    # PX4's FRD frame.
    ("rotor_FR",  ARM_OFFSET_MM, -ARM_OFFSET_MM, "ccw"),  # front-right
    ("rotor_RL", -ARM_OFFSET_MM,  ARM_OFFSET_MM, "ccw"),  # rear-left
    ("rotor_FL",  ARM_OFFSET_MM,  ARM_OFFSET_MM, "cw"),   # front-left
    ("rotor_RR", -ARM_OFFSET_MM, -ARM_OFFSET_MM, "cw"),   # rear-right
]


@dataclass(slots=True)
class FlightArgs:
    output_dir: Path
    takeoff_alt_m: float
    hover_secs: float
    stop_after: str | None
    px4_install: Path
    skip_px4_rebuild: bool
    document_name: str


# ---------------------------------------------------------------------------
# Stage banners
# ---------------------------------------------------------------------------


def _banner(stage: str) -> None:
    border = "=" * 70
    print(f"\n{border}\n  {stage}\n{border}", flush=True)


# ---------------------------------------------------------------------------
# Stage 1: Build CAD geometry
# ---------------------------------------------------------------------------


def _build_3blade_props(document_name: str) -> list[str]:
    """Build a 3-blade propeller body for each rotor location.

    Each prop is a single sketch: a hub circle plus 3 trapezoid blades
    rotated 120° apart, all padded together as one solid.  This avoids
    the polar_pattern-on-PartDesign-Body Tip-update bug.
    """
    from server.tools_cad import (
        cad_new_body, cad_pad, cad_set_placement, cad_sketch,
    )

    rotor_bodies: list[str] = []
    R_HUB = PROP_HUB_RADIUS_MM
    R_TIP = R_HUB + PROP_BLADE_LEN_MM
    HALF_INNER = PROP_BLADE_INNER_W_MM / 2
    HALF_OUTER = PROP_BLADE_OUTER_W_MM / 2

    for name, dx, dy, _direction in ROTORS:
        print(f"Building {name} (3-blade prop) at ({dx:.0f}, {dy:.0f})…")
        cad_new_body(name=name, doc=document_name)

        # 3 trapezoid blades drawn as polylines (4 line segments each)
        # rotated by 0°, 120°, 240°.
        elements: list[dict[str, Any]] = [
            {"type": "circle", "cx": 0.0, "cy": 0.0, "r": R_HUB},
        ]
        for i in range(PROP_BLADE_COUNT):
            theta = math.radians(i * 360 / PROP_BLADE_COUNT)
            ct, st = math.cos(theta), math.sin(theta)

            def rot(x: float, y: float) -> tuple[float, float]:
                return (x * ct - y * st, x * st + y * ct)

            # Blade as 4 lines forming a trapezoid: hub-side wider,
            # tip-side narrower.  Slight overlap into the hub circle
            # so the boolean union closes cleanly.
            p1 = rot(R_HUB - 1.0, +HALF_INNER)
            p2 = rot(R_TIP,       +HALF_OUTER)
            p3 = rot(R_TIP,       -HALF_OUTER)
            p4 = rot(R_HUB - 1.0, -HALF_INNER)
            elements.extend([
                {"type": "line", "x1": p1[0], "y1": p1[1],
                                 "x2": p2[0], "y2": p2[1]},
                {"type": "line", "x1": p2[0], "y1": p2[1],
                                 "x2": p3[0], "y2": p3[1]},
                {"type": "line", "x1": p3[0], "y1": p3[1],
                                 "x2": p4[0], "y2": p4[1]},
                {"type": "line", "x1": p4[0], "y1": p4[1],
                                 "x2": p1[0], "y2": p1[1]},
            ])

        prop_sketch = cad_sketch(
            body=name, plane="XY",
            elements=elements,
            doc=document_name,
        )
        cad_pad(sketch=prop_sketch["sketch"],
                length=ROTOR_THICKNESS_MM, doc=document_name)
        cad_set_placement(
            object_name=name,
            position=[dx, dy, CHASSIS_T_MM + 5.0],
            doc=document_name,
        )
        rotor_bodies.append(name)
    return rotor_bodies


def build_drone_geometry(document_name: str) -> dict[str, Any]:
    """Drive the FreeCAD addon to build chassis + 4 rotor bodies.

    Returns a dict with the body labels in the order they were created,
    so the caller can match them up to mechanism parts.
    """
    from server.tools_cad import (
        cad_new_body,
        cad_new_document,
        cad_pad,
        cad_set_placement,
        cad_sketch,
    )

    _banner("Stage 1: Build CAD geometry")
    print(f"Creating document: {document_name}")
    cad_new_document(name=document_name)

    # ---- Chassis: rectangular pad on XY plane ----
    print("Building chassis (rectangular pad)…")
    cad_new_body(name="Chassis", doc=document_name)
    sketch_result = cad_sketch(
        body="Chassis", plane="XY",
        elements=[{
            "type": "rect",
            "x": -CHASSIS_W_MM / 2, "y": -CHASSIS_H_MM / 2,
            "w": CHASSIS_W_MM, "h": CHASSIS_H_MM,
        }],
        doc=document_name,
    )
    cad_pad(sketch=sketch_result["sketch"], length=CHASSIS_T_MM,
            doc=document_name)

    # ---- Rotors: 3-blade propellers, one per arm tip ----
    # Each prop = central hub circle + 3 trapezoid blades drawn in a
    # single sketch, rotated 120° apart.  Single-sketch construction
    # (rather than polar_pattern) is more robust because polar_pattern
    # has had Tip-update bugs in this addon and silent scope failures
    # that produce a single blade instead of three.
    rotor_bodies = _build_3blade_props(document_name)

    return {
        "chassis_body": "Chassis",
        "rotor_bodies": rotor_bodies,
    }


# ---------------------------------------------------------------------------
# Stage 2: Define mechanism + export sim package + airframe
# ---------------------------------------------------------------------------


def define_rotor_mechanism(geometry: dict[str, Any]) -> str:
    """Register a 4-rotor mechanism with the bridge's mechanism store.

    Returns the mechanism handle that ``cad.export_sim_package`` consumes.
    """
    from server.tools_motion import motion_define_mechanism

    _banner("Stage 2a: Define mechanism")
    parts = [
        {
            "id": "chassis", "body_name": geometry["chassis_body"],
            "is_ground": True, "mass_kg": CHASSIS_MASS_KG,
        },
    ]
    joints = []
    for (rotor_name, dx, dy, _direction), body in zip(
        ROTORS, geometry["rotor_bodies"], strict=True,
    ):
        # Use the rotor's body name as the part id so that link names
        # in the SDF match. mass_kg ensures the airframe generator can
        # compute total mass.
        parts.append({
            "id": rotor_name,
            "body_name": body,
            "mass_kg": ROTOR_MASS_KG,
        })
        joints.append({
            "id": f"{rotor_name}_joint",
            "joint_type": "continuous",
            "parent_part": "chassis",
            "child_part": rotor_name,
            "origin": [dx, dy, CHASSIS_T_MM + 5.0],
            "axis": [0.0, 0.0, 1.0],
        })
    result = motion_define_mechanism({
        "name": "quadrotor_camera_drone",
        "parts": parts,
        "joints": joints,
        "drives": [],
    })
    if not result.get("ok"):
        raise RuntimeError(
            f"motion.define_mechanism failed: {result.get('error')}"
        )
    mech_id = result["mechanism_id"]
    print(f"Mechanism registered: {mech_id} ({len(parts)} parts, {len(joints)} joints)")
    return mech_id


def export_sim_package_with_px4(
    mechanism_id: str, output_dir: Path, px4_install: Path,
) -> dict[str, Any]:
    """Export STL + URDF + SDF + PX4 airframe init script in one call."""
    from server.tools_cad import cad_export_sim_package

    _banner("Stage 2b: Export sim package + PX4 airframe")
    drone_config: dict[str, Any] = {
        "rotors": [
            {
                "index": idx,
                "joint": f"{rotor_name}_joint",
                "direction": direction,
                # Body-frame position in metres for the airframe generator.
                "position_m": (dx / 1000.0, dy / 1000.0, 0.0),
            }
            for idx, (rotor_name, dx, dy, direction) in enumerate(ROTORS)
        ],
        "sensors": True,
        "px4": True,
        "register_airframe": True,
        "px4_install_path": str(px4_install),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    result = cad_export_sim_package(
        mechanism_id=mechanism_id,
        emit_sdf=True,
        ground_clearance_m=0.05,
        drone_config=drone_config,
        output_dir=str(output_dir),
    )
    if not result.get("ok"):
        raise RuntimeError(
            f"cad.export_sim_package failed: {result.get('error')}"
        )

    print(f"  SDF: {result.get('sdf_path')}")
    print(f"  URDF: {result.get('urdf_path')}")
    print(f"  Airframe ID: {result.get('airframe_id')}")
    print(f"  Airframe path: {result.get('airframe_path')}")
    print(f"  Mass: {result.get('airframe_mass_kg', 0):.3f} kg")
    print(f"  Arm length: {result.get('airframe_arm_length_m', 0):.3f} m")
    print(f"  Hover throttle: {result.get('airframe_hover_throttle', 0):.3f}")

    if result.get("airframe_error"):
        raise RuntimeError(
            f"airframe generation failed: {result['airframe_error']}"
        )
    return result


# ---------------------------------------------------------------------------
# Stage 3: Rebuild PX4 with the new airframe
# ---------------------------------------------------------------------------


def _make_target_name(airframe_name: str) -> str:
    """Convert airframe init filename → ``make`` target.

    The airframe filename includes a ``gz_`` prefix
    (e.g. ``50837_gz_quadrotor_camera_drone``).  PX4's CMakeLists
    turns each such file into a ``gz_<model>`` make target.
    """
    if airframe_name.startswith("gz_"):
        return airframe_name
    if "_gz_" in airframe_name:
        return f"gz_{airframe_name.split('_gz_', 1)[1]}"
    return f"gz_{airframe_name}"


def _model_base_name(airframe_name: str) -> str:
    """Strip the ``gz_`` prefix to get the SDF model directory name."""
    target = _make_target_name(airframe_name)
    return target[len("gz_"):]


def deploy_model_to_px4(
    output_dir: Path,
    px4_install: Path,
    airframe_name: str,
) -> Path:
    """Copy the exported SDF + STL meshes into PX4's gz models tree.

    cad.export_sim_package writes everything to ``output_dir``; PX4
    looks for SDFs at ``<px4>/Tools/simulation/gz/models/<name>/``
    with ``model.sdf`` + ``model.config`` + meshes side-by-side.  This
    helper copies the artifacts, rewrites mesh URIs to relative paths,
    and writes a minimal ``model.config``.

    Idempotent — re-running overwrites existing files.
    """
    import shutil

    base = _model_base_name(airframe_name)
    model_dir = px4_install / "Tools" / "simulation" / "gz" / "models" / base
    model_dir.mkdir(parents=True, exist_ok=True)

    # Copy STLs
    for stl in output_dir.glob("*.stl"):
        shutil.copy2(stl, model_dir / stl.name)

    # Copy SDF as model.sdf, rewriting absolute mesh URIs → relative.
    src_sdf = next(output_dir.glob("*.sdf"), None)
    if src_sdf is None:
        raise RuntimeError(f"no SDF found in {output_dir}")
    sdf_text = src_sdf.read_text()
    # Rewrite "<output_dir>/foo.stl" → "foo.stl" inside <uri> tags so
    # the SDF is portable to the model dir.
    sdf_text = sdf_text.replace(f"{output_dir}/", "")
    sdf_text = sdf_text.replace(f"{output_dir.resolve()}/", "")
    (model_dir / "model.sdf").write_text(sdf_text)

    # Minimal model.config — Gazebo requires this to load the model.
    (model_dir / "model.config").write_text(
        f'<?xml version="1.0"?>\n'
        f'<model>\n'
        f'  <name>{base}</name>\n'
        f'  <version>1.0</version>\n'
        f'  <sdf version="1.10">model.sdf</sdf>\n'
        f'  <description>Generated by SolidMind run.py</description>\n'
        f'</model>\n'
    )
    return model_dir


def patch_airframes_cmakelists(px4_install: Path, airframe_filename: str) -> None:
    """Register the airframe init script in ROMFS/.../airframes/CMakeLists.txt.

    PX4's cmake explicitly enumerates airframe files; a new file is
    invisible to the build until added.  Idempotent — does nothing if
    the entry is already present.
    """
    cmake_lists = (
        px4_install / "ROMFS" / "px4fmu_common" / "init.d-posix"
        / "airframes" / "CMakeLists.txt"
    )
    text = cmake_lists.read_text()
    if airframe_filename in text:
        return
    # Find the marker comment that PX4 uses to reserve a range for
    # custom models, and insert just before it.  Match in a way that
    # doesn't care about indentation (real PX4 uses tabs; simpler
    # generated fixtures use spaces).
    import re
    marker = re.search(
        r"^([ \t]*)# \[22000, 22999\] Reserve for custom models",
        text, re.MULTILINE,
    )
    insertion_indent = marker.group(1) if marker else "\t"
    insertion = f"{insertion_indent}{airframe_filename}\n"
    if marker:
        text = text[:marker.start()] + insertion + text[marker.start():]
    else:
        # Fallback: insert before the final ")".
        text = text.rstrip().rstrip(")") + insertion + ")\n"
    cmake_lists.write_text(text)


def rebuild_px4_with_airframe(
    px4_install: Path,
    airframe_name: str,
    output_dir: Path,
    airframe_filename: str | None = None,
) -> None:
    """Deploy artifacts, register the airframe, and rebuild PX4 SITL.

    PX4 discovers airframes via cmake at build time; a new file in
    ROMFS/.../airframes/ requires a rebuild before PX4_SIM_MODEL can
    reference it.  Subsequent rebuilds are incremental (~30s).
    """
    _banner("Stage 3: Rebuild PX4 with new airframe")

    # 1. Copy SDF + STLs into PX4's gz models tree.
    model_dir = deploy_model_to_px4(output_dir, px4_install, airframe_name)
    print(f"  Deployed model to {model_dir}")

    # 2. Register the airframe init in CMakeLists.txt (if not already).
    if airframe_filename:
        patch_airframes_cmakelists(px4_install, airframe_filename)
        print(f"  Registered {airframe_filename} in airframes/CMakeLists.txt")

    sanitized = _make_target_name(airframe_name)
    cmd = ["make", "px4_sitl_default", sanitized]
    print(f"  Running: {' '.join(cmd)} (cwd={px4_install})")
    print("  This is incremental — should be ~30s after the initial build.")

    proc = subprocess.run(
        cmd,
        cwd=str(px4_install),
        # Stream output so the operator sees progress; the recording
        # voiceover can talk over it.
        stdout=sys.stdout, stderr=sys.stderr, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"PX4 rebuild failed (exit {proc.returncode}). "
            f"Run '{' '.join(cmd)}' from {px4_install} manually to debug."
        )
    print("PX4 rebuild successful.")


# ---------------------------------------------------------------------------
# Stage 4: Launch PX4 + Gazebo
# ---------------------------------------------------------------------------


def launch_px4_sim(
    px4_install: Path, airframe_name: str,
) -> subprocess.Popen[bytes]:
    """Launch PX4 + Gazebo SITL in a background subprocess.

    Returns the Popen handle so callers can terminate it on shutdown.
    Waits up to 30 s for the MAVLink endpoint to come up before returning.
    """
    _banner("Stage 4: Launch PX4 SITL + Gazebo")
    env = os.environ.copy()
    target = _make_target_name(airframe_name)
    env["PX4_SIM_MODEL"] = target
    cmd = ["make", "px4_sitl_default", target]
    print(f"  Launching: {' '.join(cmd)} (cwd={px4_install})")
    print(f"  PX4_SIM_MODEL={airframe_name}")

    proc = subprocess.Popen(
        cmd, cwd=str(px4_install), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Wait for UDP 14540 to become reachable.
    import socket
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"PX4 exited during boot (rc={proc.returncode})"
            )
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                sock.bind(("127.0.0.1", 0))
                sock.sendto(b"", ("127.0.0.1", 14540))
                sock.recvfrom(2048)
                print("PX4 MAVLink endpoint reachable on UDP 14540.")
                return proc
        except OSError:
            time.sleep(1.0)

    proc.terminate()
    raise RuntimeError("PX4 failed to come up within 30 s")


# ---------------------------------------------------------------------------
# Stage 5: MAVLink flight
# ---------------------------------------------------------------------------


def fly_takeoff_hover_land(takeoff_alt_m: float, hover_secs: float) -> None:
    """Connect to PX4, arm, takeoff, hover, land.

    Uses ``MavlinkController`` directly — the bridge's offboard wiring
    will invoke the same code path once the recording prompt drives
    flight via ``motion.teleop_command``.
    """
    from server.mavlink_controller import MavlinkController, MavlinkError

    _banner("Stage 5: Flight (arm → takeoff → hover → land)")
    ctrl = MavlinkController(udp_url="udp:127.0.0.1:14540")
    ctrl.connect(timeout_s=10.0)
    try:
        # Start the setpoint stream BEFORE switching to OFFBOARD —
        # PX4 rejects the mode switch otherwise.
        ctrl.set_velocity(0.0, 0.0, 0.0, 0.0)
        ctrl.start_setpoint_stream()
        time.sleep(1.0)

        print("Arming…")
        ctrl.arm(timeout_s=5.0)

        print(f"Taking off to {takeoff_alt_m:.1f} m…")
        ctrl.takeoff(takeoff_alt_m, timeout_s=5.0)

        # Wait for altitude to reach target.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            tel = ctrl.get_telemetry()
            if tel.local_position is not None:
                alt = -tel.local_position[2]  # NED z is down; flip
                if abs(alt - takeoff_alt_m) < 1.5:
                    print(f"  Reached {alt:.2f} m")
                    break
            time.sleep(0.2)

        print(f"Hovering for {hover_secs:.1f} s…")
        time.sleep(hover_secs)

        print("Landing…")
        ctrl.land(timeout_s=5.0)

        # Wait until back on the ground.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            tel = ctrl.get_telemetry()
            if tel.local_position is not None and -tel.local_position[2] < 0.5:
                print("  Landed.")
                break
            time.sleep(0.5)

        print("Disarming…")
        ctrl.disarm(timeout_s=5.0)
    except MavlinkError as exc:
        raise RuntimeError(f"MAVLink flight failed: {exc}") from exc
    finally:
        ctrl.stop_setpoint_stream()
        ctrl.disconnect()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> FlightArgs:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, default=Path("/tmp/camera_drone"))
    p.add_argument("--takeoff-alt", type=float, default=5.0)
    p.add_argument("--hover-secs", type=float, default=15.0)
    p.add_argument(
        "--stop-after",
        choices=["build", "export", "rebuild", "launch", "fly"],
        default=None,
        help="Stop after the named stage (useful when iterating).",
    )
    p.add_argument(
        "--px4-install", type=Path,
        default=Path(os.environ.get(
            "SOLIDMIND_PX4_INSTALL",
            str(Path.home() / "repos" / "PX4-Autopilot"),
        )),
    )
    p.add_argument(
        "--skip-px4-rebuild", action="store_true",
        help="Reuse an existing PX4 build (skip Stage 3).",
    )
    p.add_argument(
        "--document-name", default="QuadrotorCameraDrone",
    )
    args = p.parse_args()
    return FlightArgs(
        output_dir=args.output_dir,
        takeoff_alt_m=args.takeoff_alt,
        hover_secs=args.hover_secs,
        stop_after=args.stop_after,
        px4_install=args.px4_install,
        skip_px4_rebuild=args.skip_px4_rebuild,
        document_name=args.document_name,
    )


def _stop_if_requested(stage: str, current: str) -> bool:
    return stage == current


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = parse_args()

    if not args.px4_install.is_dir():
        print(
            f"PX4 install not found at {args.px4_install}. "
            f"Set SOLIDMIND_PX4_INSTALL or pass --px4-install.",
            file=sys.stderr,
        )
        return 2

    px4_proc: subprocess.Popen[bytes] | None = None
    try:
        # Stage 1: CAD
        geometry = build_drone_geometry(args.document_name)
        if _stop_if_requested(args.stop_after or "", "build"):
            return 0

        # Stage 2: Mechanism + export
        mech_id = define_rotor_mechanism(geometry)
        export_result = export_sim_package_with_px4(
            mech_id, args.output_dir, args.px4_install,
        )
        airframe_name = export_result.get("airframe_name") or args.document_name
        # Filename of the init script on disk (e.g. "50837_gz_<name>"),
        # used to register the airframe in PX4's CMakeLists.
        airframe_path = export_result.get("airframe_path")
        airframe_filename = (
            Path(airframe_path).name if airframe_path else None
        )
        if _stop_if_requested(args.stop_after or "", "export"):
            return 0

        # Stage 3: Rebuild PX4
        if not args.skip_px4_rebuild:
            rebuild_px4_with_airframe(
                args.px4_install, airframe_name, args.output_dir,
                airframe_filename=airframe_filename,
            )
        if _stop_if_requested(args.stop_after or "", "rebuild"):
            return 0

        # Stage 4: Launch
        px4_proc = launch_px4_sim(args.px4_install, airframe_name)
        if _stop_if_requested(args.stop_after or "", "launch"):
            print("Leaving PX4 running. Ctrl+C to terminate.")
            px4_proc.wait()
            return 0

        # Stage 5: Fly
        time.sleep(5.0)  # Give PX4's EKF a moment to converge.
        fly_takeoff_hover_land(args.takeoff_alt_m, args.hover_secs)
    finally:
        if px4_proc is not None and px4_proc.poll() is None:
            print("Terminating PX4 SITL…")
            px4_proc.terminate()
            try:
                px4_proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                px4_proc.kill()

    _banner("✓ Flight pipeline complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
