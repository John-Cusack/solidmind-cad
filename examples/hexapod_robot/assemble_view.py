"""Visual assembly of the orchestrator-built hexapod parts.

Imports the 7 STEP files produced by ``run.py`` into a SINGLE FreeCAD
document, places each leg at its mount position and angle around the
chassis, and leaves the document open so you can rotate / inspect the
assembled robot live.

This is the "where is the hexapod?" answer — ``run.py`` produces 7
disconnected STEPs at origin (correct, because each is a workshop-
shippable part); this script composes them into the robot for visual
verification. No Assembly-workbench joints are added; this is purely
positional. For URDF / Isaac integration use ``cad.export_sim_package``
on a real Assembly doc instead.

Run from repo root with FreeCAD addon listening on :9876:

    PYTHONPATH=. python3 examples/hexapod_robot/assemble_view.py \\
        --run-dir /tmp/hexapod_robot_run2

Open FreeCAD before running so you can watch the parts appear live.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

# Match run.py — chassis sits at z=0..thickness, legs sit on top.
CHASSIS_THICKNESS_MM = 5.0
LEG_MOUNT_PCD_MM = 110.0
LEG_ANGLES_DEG = [0, 60, 120, 180, 240, 300]


def _leg_mount_xy(angle_deg: float) -> tuple[float, float]:
    r = LEG_MOUNT_PCD_MM / 2.0
    return (
        r * math.cos(math.radians(angle_deg)),
        r * math.sin(math.radians(angle_deg)),
    )


def _expected_steps(run_dir: Path) -> dict[str, Path]:
    """Map subsystem name -> STEP path inside a run.py output directory."""
    base = run_dir / "run"
    expected = {
        "hexapod_chassis": base / "hexapod_chassis_0" / "output" / "hexapod_chassis.step",
    }
    for i in range(1, 7):
        expected[f"leg_{i}"] = base / f"leg_{i}_0" / "output" / f"leg_{i}.step"
    return expected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--run-dir",
        type=Path,
        default=Path("/tmp/hexapod_robot_run2"),
        help="Directory passed to run.py --out (contains run/<subsystem>_0/output/*.step)",
    )
    ap.add_argument(
        "--doc-name",
        default="HexapodAssembly",
        help="Name of the FreeCAD document to create / reuse",
    )
    args = ap.parse_args()

    steps = _expected_steps(args.run_dir)
    missing = [name for name, p in steps.items() if not p.exists()]
    if missing:
        print(f"ERROR: missing STEP files: {missing}", file=sys.stderr)
        print(f"Run examples/hexapod_robot/run.py --out {args.run_dir} first.", file=sys.stderr)
        return 1

    from orchestrator.worker_builds import common
    from server import tools_cad as cad

    if not common.freecad_ready():
        print(
            f"ERROR: FreeCAD addon not reachable at {common.fc_host()}:{common.fc_port()}.",
            file=sys.stderr,
        )
        return 1

    print(f"Composing 7-part hexapod from {args.run_dir}\n")

    # 1. Fresh document. cad_import_step into doc=None creates a new
    # "step_import*" doc; we want one named doc so the second + later
    # imports land in the same window.
    new_doc = cad.cad_new_document(name=args.doc_name)
    doc_name = new_doc.get("name", args.doc_name)
    print(f"  doc: {doc_name}")

    # 2. Chassis at origin, no rotation.
    chassis_step = steps["hexapod_chassis"]
    print(f"  importing chassis: {chassis_step.name}")
    r = cad.cad_import_step(
        path=str(chassis_step),
        object_name="Chassis",
        doc=doc_name,
    )
    print(f"    object: {r['object']}")

    # 3. Each leg: rotate around Z by leg angle, translate to mount xy,
    # lift by chassis thickness so leg sits on top of the plate.
    for i, angle_deg in enumerate(LEG_ANGLES_DEG, start=1):
        leg_name = f"leg_{i}"
        leg_step = steps[leg_name]
        cx, cy = _leg_mount_xy(angle_deg)
        print(f"  importing {leg_name}: angle={angle_deg}°, mount=({cx:+.1f},{cy:+.1f})")
        r = cad.cad_import_step(
            path=str(leg_step),
            object_name=f"Leg_{i}",
            doc=doc_name,
        )
        # Place after import: rotate first (around Z, leg extends in +X),
        # then translate so leg's hip_yaw (at object origin) lands at the
        # chassis mount position.
        cad.cad_set_placement(
            object_name=r["object"],
            position=[cx, cy, CHASSIS_THICKNESS_MM],
            rotation_axis=[0.0, 0.0, 1.0],
            rotation_angle_deg=float(angle_deg),
            doc=doc_name,
        )
        print(f"    placed at z={CHASSIS_THICKNESS_MM} mm, rotated {angle_deg}° about Z")

    # 4. Capture iso + top-down screenshots for the demo.
    for label, target in (("iso", "iso"), ("top", [0.0, 0.0, 0.0])):
        try:
            kwargs: dict = {
                "target": target,
                "distance": 4.0,
                "width": 1280,
                "height": 720,
                "doc": doc_name,
            }
            if label == "top":
                kwargs["direction"] = [0.0, 0.0, -1.0]
                kwargs["up"] = [0.0, 1.0, 0.0]
            screenshot = cad.cad_screenshot(**kwargs)
            path = screenshot.get("path") or screenshot.get("file_path")
            if path:
                print(f"  {label} screenshot: {path}")
        except Exception as exc:  # pragma: no cover
            print(f"  ({label} screenshot skipped: {exc})")

    print("\nHexapod assembled. Switch to FreeCAD to inspect.")
    print("  - 1 chassis at origin (square 150×150×5 plate, central bore + 6 mount holes)")
    print(
        f"  - 6 legs at z={CHASSIS_THICKNESS_MM} mm, evenly spaced at "
        f"{LEG_ANGLES_DEG} degrees on PCD={LEG_MOUNT_PCD_MM} mm"
    )
    print("  - each leg: 251 mm long, 3 distinct pivot bores per leg")
    print(f"  - total: {1 + 6} bodies, 18 revolute pivot points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
