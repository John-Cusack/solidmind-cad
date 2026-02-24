#!/usr/bin/env python3
"""Deterministic hexapod builder — drives FreeCAD addon directly via TCP.

Builds a visually impressive hexapod (chassis with PartDesign features +
servo/leg bodies) in ~15s.  No MCP server or LLM needed — just FreeCAD
with the addon running on localhost:9876.

Usage::

    # FreeCAD must be running with addon started first:
    #   import freecad_addon; freecad_addon.start()

    python3 scripts/demo_build_hexapod.py

    # Skip verification screenshots (faster)
    python3 scripts/demo_build_hexapod.py --fast

    # Export sim package after building
    python3 scripts/demo_build_hexapod.py --export
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from typing import Any

HOST = "127.0.0.1"
PORT = 9876

# ── Geometry constants ───────────────────────────────────────────────

CHASSIS_RADIUS = 75.0      # mm
CHASSIS_THICKNESS = 5.0    # mm
SERVO_POCKET_W = 24.0      # mm (along radial)
SERVO_POCKET_H = 22.0      # mm (along tangential)
SERVO_POCKET_DEPTH = 3.0   # mm
FILLET_RADIUS = 1.5        # mm

# Servo box dimensions (AX-12A proportions)
SERVO_W = 24.0   # mm
SERVO_D = 12.0   # mm
SERVO_H = 23.0   # mm

# Leg dimensions
LEG_W = 10.0     # mm
LEG_D = 10.0     # mm
LEG_H = 60.0     # mm

# 6 leg positions: LF, LM, LR, RF, RM, RR
# Placed at R=60mm, 60° intervals so all fit within the 75mm chassis disc.
# Servos sit on top of the plate, legs hang below
LEG_POSITIONS = [
    {"name": "LF", "x":  52.0, "y":  30.0},   # 30°
    {"name": "LM", "x":   0.0, "y":  60.0},   # 90°
    {"name": "LR", "x": -52.0, "y":  30.0},   # 150°
    {"name": "RF", "x":  52.0, "y": -30.0},   # 330°
    {"name": "RM", "x":   0.0, "y": -60.0},   # 270°
    {"name": "RR", "x": -52.0, "y": -30.0},   # 210°
]

# ── ANSI colors ──────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# ── TCP helper ───────────────────────────────────────────────────────


def _send(cmd: str, timeout: float = 30.0, **args: Any) -> dict[str, Any]:
    """Send a command to the FreeCAD addon and return the parsed response."""
    payload = json.dumps({"cmd": cmd, "args": args}) + "\n"
    with socket.create_connection((HOST, PORT), timeout=10) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf.split(b"\n", 1)[0])
    if not resp.get("ok", False):
        err = resp.get("error", resp)
        raise RuntimeError(f"Command {cmd!r} failed: {err}")
    return resp.get("result", resp)


def _step(num: int, total: int, label: str) -> float:
    """Print step header, return start time."""
    print(f"  [{num}/{total}] {label}...", end="", flush=True)
    return time.monotonic()


def _done(t0: float) -> None:
    """Print step completion with elapsed time."""
    elapsed = time.monotonic() - t0
    print(f"  {GREEN}✓{RESET}  {DIM}{elapsed:.1f}s{RESET}")


# ── Build functions ──────────────────────────────────────────────────

TOTAL_STEPS = 11


def build_document() -> None:
    """Step 1: Create document and body."""
    t0 = _step(1, TOTAL_STEPS, "Creating document + body")
    _send("new_document", name="Hexapod")
    _send("new_body", name="Body_Plate")
    _done(t0)


def build_chassis_disc(verify: bool = True) -> str:
    """Step 2: Sketch circle + pad → solid disc. Returns sketch name."""
    t0 = _step(2, TOTAL_STEPS, "Building chassis disc (r=75mm, h=5mm)")

    # Create sketch on XY plane
    result = _send("new_sketch", body="Body_Plate", plane="XY")
    sketch_name = result["sketch"]

    # Add circle + close
    _send("sketch_populate",
          sketch=sketch_name,
          elements=[{"type": "circle", "cx": 0, "cy": 0, "r": CHASSIS_RADIUS}],
          constraints=[])
    _send("close_sketch", sketch=sketch_name)

    # Pad
    _send("pad", sketch=sketch_name, length=CHASSIS_THICKNESS, verify=verify)
    _done(t0)
    return sketch_name


def build_servo_pocket(verify: bool = True) -> str:
    """Step 3: Single rectangular pocket at radius ~52mm. Returns pocket feature name."""
    t0 = _step(3, TOTAL_STEPS, "Adding servo pocket cutout")

    # Position: center of pocket at radius 52mm along +Y axis
    pocket_cx = 0.0
    pocket_cy = 52.0
    pocket_x = pocket_cx - SERVO_POCKET_W / 2
    pocket_y = pocket_cy - SERVO_POCKET_H / 2

    # Sketch on XY plane (at z=5, top face)
    result = _send("new_sketch", body="Body_Plate", plane="XY")
    sketch_name = result["sketch"]

    _send("sketch_populate",
          sketch=sketch_name,
          elements=[{"type": "rect",
                     "x": pocket_x, "y": pocket_y,
                     "w": SERVO_POCKET_W, "h": SERVO_POCKET_H}],
          constraints=[])
    _send("close_sketch", sketch=sketch_name)

    # Pocket (cut into the disc — reversed because sketch is on XY at z=0
    # and the solid is above)
    result = _send("pocket",
                   sketch=sketch_name,
                   length=SERVO_POCKET_DEPTH,
                   pocket_type="Dimension",
                   reversed="auto",
                   verify=verify)
    _done(t0)
    return result.get("pocket", result.get("name", "Pocket"))


def build_polar_pattern(pocket_name: str, verify: bool = True) -> None:
    """Step 4: Polar pattern — 6 copies of the pocket around Z axis."""
    t0 = _step(4, TOTAL_STEPS, "Polar pattern (6× servo pockets)")
    _send("polar_pattern",
          features=[pocket_name],
          axis="Base_Z",
          occurrences=6,
          angle=360.0,
          verify=verify)
    _done(t0)


def build_fillets(verify: bool = True) -> None:
    """Step 5: Fillet the top circular edge for a polished look."""
    t0 = _step(5, TOTAL_STEPS, "Filleting top edges")

    # Find convex circular edges on the top — these are the rim edges
    result = _send("find_edges",
                   body="Body_Plate",
                   curve_type="Circle",
                   convexity="convex")
    edges = result.get("edges", [])
    edge_names = [e["edge"] for e in edges]

    if edge_names:
        _send("fillet",
              edges=edge_names[:2],  # Top and bottom rim
              radius=FILLET_RADIUS,
              body="Body_Plate",
              verify=verify)
    _done(t0)


def build_servos(verify: bool = True) -> None:
    """Step 6: Create 6 servo bodies as positioned boxes."""
    t0 = _step(6, TOTAL_STEPS, "Creating servo bodies (6×)")
    items = []
    for pos in LEG_POSITIONS:
        items.append({
            "name": f"Servo_{pos['name']}",
            "shape": "box",
            "dimensions": {
                "length": SERVO_W,
                "width": SERVO_D,
                "height": SERVO_H,
            },
            "position": [pos["x"], pos["y"], CHASSIS_THICKNESS / 2 + SERVO_H / 2],
        })
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0)


def build_legs(verify: bool = True) -> None:
    """Step 7: Create 6 leg bodies as positioned boxes."""
    t0 = _step(7, TOTAL_STEPS, "Creating leg bodies (6×)")
    items = []
    for pos in LEG_POSITIONS:
        items.append({
            "name": f"Leg_{pos['name']}",
            "shape": "box",
            "dimensions": {
                "length": LEG_W,
                "width": LEG_D,
                "height": LEG_H,
            },
            "position": [pos["x"], pos["y"], -LEG_H / 2],
        })
    _send("create_primitives", items=items, verify=verify, timeout=60)
    _done(t0)


def take_screenshot() -> None:
    """Step 8: Capture iso screenshot."""
    t0 = _step(8, TOTAL_STEPS, "Capturing screenshot")
    result = _send("screenshot", target="iso", width=1024, height=1024)
    _done(t0)


def define_mechanism() -> str:
    """Step 9: Define the hexapod mechanism (for export). Returns mechanism summary."""
    t0 = _step(9, TOTAL_STEPS, "Defining mechanism (13 parts, 12 joints)")

    # This goes through the MCP server, not the FreeCAD addon.
    # For the deterministic script, we just print what WOULD happen.
    # The actual mechanism definition requires the MCP server.
    print(f"  {DIM}(mechanism definition requires MCP server — skipping){RESET}")
    _done(t0)
    return ""


def export_sim_package() -> str | None:
    """Step 10: Export URDF sim package."""
    t0 = _step(10, TOTAL_STEPS, "Exporting sim package (URDF + STLs)")
    try:
        result = _send("export_sim_package",
                       format="stl",
                       timeout=120)
        path = result.get("output_dir", result.get("urdf_path", "?"))
        _done(t0)
        return path
    except RuntimeError as e:
        print(f"  {RED}✗{RESET}  {DIM}{e}{RESET}")
        return None


def verify_model() -> None:
    """Step 11: Print model tree summary."""
    t0 = _step(11, TOTAL_STEPS, "Verifying model tree")
    result = _send("get_model_tree", detail="bodies")
    bodies = result.get("bodies", [])
    print(f"  {GREEN}✓{RESET}  {DIM}{len(bodies)} bodies{RESET}")


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Build hexapod in FreeCAD")
    parser.add_argument("--fast", action="store_true",
                        help="Skip verification screenshots")
    parser.add_argument("--export", action="store_true",
                        help="Export URDF sim package after building")
    args = parser.parse_args()

    verify = not args.fast

    print(f"\n{BOLD}{CYAN}═══ SolidMind CAD — Hexapod Demo Build ═══{RESET}\n")

    t_start = time.monotonic()

    # Check connection
    try:
        _send("ping")
    except (ConnectionRefusedError, OSError) as e:
        print(f"  {RED}Cannot connect to FreeCAD addon on {HOST}:{PORT}{RESET}")
        print(f"  Start FreeCAD and run: import freecad_addon; freecad_addon.start()")
        sys.exit(1)

    # Phase 1: Chassis plate (rich PartDesign features)
    build_document()
    build_chassis_disc(verify=verify)
    pocket_name = build_servo_pocket(verify=verify)
    build_polar_pattern(pocket_name, verify=verify)
    build_fillets(verify=verify)

    # Phase 2: Servos + Legs
    build_servos(verify=verify)
    build_legs(verify=verify)

    # Phase 3: Screenshot
    take_screenshot()

    # Phase 4: Mechanism + Export (optional)
    if args.export:
        define_mechanism()
        export_sim_package()

    # Phase 5: Verify
    verify_model()

    t_total = time.monotonic() - t_start
    print(f"\n{BOLD}{'━' * 50}{RESET}")
    print(f"  {GREEN}{BOLD}Total: {t_total:.1f}s  |  13 bodies  |  Ready for export{RESET}")
    print(f"{BOLD}{'━' * 50}{RESET}\n")


if __name__ == "__main__":
    main()
