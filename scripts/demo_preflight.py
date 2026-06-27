#!/usr/bin/env python3
"""Demo preflight — verify everything works before hitting record.

Checks each subsystem in sequence, reports pass/fail/skip, and optionally
does a dry run of the full hexapod pipeline (build → export → import → walk).

Usage::

    # Quick check (connections only, ~10s)
    python3 scripts/demo_preflight.py

    # Full dry run including Isaac (slow, ~3min)
    python3 scripts/demo_preflight.py --full

    # Skip Isaac checks (FreeCAD-only demo)
    python3 scripts/demo_preflight.py --no-isaac
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEXAPOD_PKG = REPO / "hexapod_sim_pkg"
URDF_PATH = HEXAPOD_PKG / "Hexapod_v2_1DOF.urdf"
TELEOP_CONFIG = HEXAPOD_PKG / "teleop_config.json"
HEXAPOD_18DOF_PKG = REPO / "hexapod_18dof_pkg"

FREECAD_HOST = "127.0.0.1"
FREECAD_PORT = 9876
ISAAC_HOST = "127.0.0.1"
ISAAC_PORT = 9878

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

passed = 0
failed = 0
skipped = 0


def _status(label: str, ok: bool | None, detail: str = "") -> None:
    global passed, failed, skipped
    if ok is True:
        tag = f"{GREEN}PASS{RESET}"
        passed += 1
    elif ok is False:
        tag = f"{RED}FAIL{RESET}"
        failed += 1
    else:
        tag = f"{YELLOW}SKIP{RESET}"
        skipped += 1
    suffix = f"  {detail}" if detail else ""
    print(f"  [{tag}] {label}{suffix}")


def _tcp_ping(host: str, port: int, timeout: float = 3.0) -> dict | None:
    """Send a JSON ping and return the parsed response, or None on failure."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"cmd":"ping","args":{}}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0])
    except (ConnectionRefusedError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _tcp_command(
    host: str, port: int, cmd: str, args: dict | None = None, timeout: float = 10.0,
) -> dict | None:
    """Send a command and return parsed response."""
    try:
        payload = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload.encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0])
    except Exception:
        return None


# ── Check functions ──────────────────────────────────────────────────


def check_python() -> None:
    v = sys.version_info
    ok = v >= (3, 12)
    _status(f"Python {v.major}.{v.minor}.{v.micro}", ok, "(need >= 3.12)")


def check_package_importable() -> None:
    try:
        import server.main  # noqa: F401
        _status("server package importable", True)
    except Exception as e:
        _status("server package importable", False, str(e))


def check_tests() -> None:
    """Run the fast unit tests (skip anything needing live connections)."""
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_sketch_elements", "-v"],
        capture_output=True, text=True, cwd=str(REPO), timeout=30,
    )
    _status("unit tests (sketch_elements)", r.returncode == 0,
            f"{r.stdout.strip().splitlines()[-1]}" if r.stdout.strip() else r.stderr.strip()[:80])


def check_freecad_connection() -> bool:
    resp = _tcp_ping(FREECAD_HOST, FREECAD_PORT)
    ok = resp is not None and resp.get("ok", False)
    if ok:
        _status("FreeCAD addon (localhost:9876)", True)
    else:
        _status("FreeCAD addon (localhost:9876)", False,
                "Start FreeCAD and run: import freecad_addon; freecad_addon.start()")
    return ok


def check_freecad_new_doc() -> bool:
    """Create and close a test document to verify FreeCAD is responsive."""
    resp = _tcp_command(FREECAD_HOST, FREECAD_PORT, "new_document",
                        {"name": "_preflight_test"})
    if resp and resp.get("ok"):
        # Clean up
        _tcp_command(FREECAD_HOST, FREECAD_PORT, "close_document",
                     {"name": "_preflight_test"}, timeout=5)
        _status("FreeCAD create document", True)
        return True
    _status("FreeCAD create document", False, str(resp))
    return False


def check_hexapod_assets() -> bool:
    missing = []
    for f in [URDF_PATH, TELEOP_CONFIG]:
        if not f.exists():
            missing.append(f.name)
    stls = list(HEXAPOD_PKG.glob("*.stl"))
    if len(stls) < 12:
        missing.append(f"STLs ({len(stls)}/12)")
    ok = len(missing) == 0
    _status("Hexapod assets (URDF + STLs)", ok,
            f"missing: {', '.join(missing)}" if missing else f"{len(stls)} STLs")
    return ok


def check_isaac_python() -> str | None:
    isaac_py = os.environ.get("ISAAC_PYTHON",
                              str(REPO.parent / "isaacsim/_build/linux-x86_64/release/python.sh"))
    exists = Path(isaac_py).exists()
    _status("ISAAC_PYTHON", exists, isaac_py if exists else f"not found: {isaac_py}")
    return isaac_py if exists else None


def check_isaac_bridge() -> bool:
    resp = _tcp_ping(ISAAC_HOST, ISAAC_PORT, timeout=5)
    ok = resp is not None and resp.get("ok", False)
    if ok:
        caps = resp.get("result", {}).get("capabilities", {})
        _status("Isaac bridge (localhost:9878)", True,
                f"isaac_available={caps.get('isaac_available')}")
    else:
        _status("Isaac bridge (localhost:9878)", False,
                "Start with: scripts/run_isaac_bridge.sh")
    return ok


def check_isaac_urdf_import() -> bool:
    """Import the hexapod URDF into Isaac (slow ~60-180s)."""
    print("    Importing URDF (this takes ~60-180s)...")
    resp = _tcp_command(ISAAC_HOST, ISAAC_PORT, "import_urdf",
                        {"urdf_path": str(URDF_PATH.resolve())}, timeout=300)
    if resp and resp.get("ok"):
        r = resp["result"]
        _status("Isaac URDF import", True,
                f"joints={r.get('joint_count')} links={r.get('link_count')}")
        return True
    err = resp.get("error", resp) if resp else "timeout/connection error"
    _status("Isaac URDF import", False, str(err)[:100])
    return False


def check_isaac_screenshot() -> bool:
    resp = _tcp_command(ISAAC_HOST, ISAAC_PORT, "screenshot",
                        {"width": 640, "height": 480}, timeout=30)
    if resp and resp.get("ok") and resp.get("result", {}).get("image_base64"):
        import base64
        png = base64.b64decode(resp["result"]["image_base64"])
        out = REPO / "preflight_screenshot.png"
        out.write_bytes(png)
        _status("Isaac screenshot", True, f"saved {len(png)} bytes → {out.name}")
        return True
    _status("Isaac screenshot", False)
    return False


def check_gear_engine() -> None:
    """Check that the Rust gear geometry engine is importable."""
    try:
        import solidmind_geometry  # noqa: F401
        _status("Gear engine (solidmind_geometry)", True)
    except ImportError:
        _status("Gear engine (solidmind_geometry)", False,
                "pip install -e solidmind_geometry/ or cargo build")
    except Exception as e:
        _status("Gear engine (solidmind_geometry)", False, str(e)[:80])


def check_hexapod_3dof_controller() -> None:
    """Check that Hexapod3DOFController and IK module are importable."""
    try:
        # Quick IK smoke test: solve for a point and verify angles are finite
        import math

        from isaac_bridge.controllers import Hexapod3DOFController  # noqa: F401
        from isaac_bridge.hexapod_ik import LegGeometry, inverse_kinematics  # noqa: F401
        geom = LegGeometry(l_coxa=0.052, l_femur=0.066, l_tibia=0.133)
        angles = inverse_kinematics(0.15, 0.0, -0.10, geom)
        ok = all(math.isfinite(a) for a in (angles.coxa, angles.femur, angles.tibia))
        _status("Hexapod3DOFController + IK", ok,
                f"coxa={math.degrees(angles.coxa):.1f}° femur={math.degrees(angles.femur):.1f}° "
                f"tibia={math.degrees(angles.tibia):.1f}°")
    except Exception as e:
        _status("Hexapod3DOFController + IK", False, str(e)[:80])


def check_18dof_build_script() -> None:
    """Check that the 18-DOF build script is importable and produces valid mechanism JSON."""
    try:
        # Import the build script's mechanism builder
        build_script = REPO / "scripts" / "demo_build_hexapod_18dof.py"
        if not build_script.exists():
            _status("18-DOF build script", False, "scripts/demo_build_hexapod_18dof.py not found")
            return

        r = subprocess.run(
            [sys.executable, str(build_script), "--print-mechanism"],
            capture_output=True, text=True, cwd=str(REPO), timeout=10,
        )
        if r.returncode != 0:
            _status("18-DOF build script", False, r.stderr.strip()[:80])
            return

        mechanism = json.loads(r.stdout)
        parts = mechanism.get("parts", [])
        joints = mechanism.get("joints", [])
        revolute_joints = [j for j in joints if j.get("joint_type") == "revolute"]
        _status("18-DOF build script", len(parts) == 37 and len(revolute_joints) == 18,
                f"{len(parts)} parts, {len(revolute_joints)} revolute joints, "
                f"{len(joints)} total joints")
    except Exception as e:
        _status("18-DOF build script", False, str(e)[:80])


def check_18dof_hexapod_assets() -> bool:
    """Check for 18-DOF hexapod URDF package (if it exists)."""
    if not HEXAPOD_18DOF_PKG.exists():
        _status("18-DOF hexapod assets", None, "hexapod_18dof_pkg/ not found (will be generated)")
        return False
    urdfs = list(HEXAPOD_18DOF_PKG.glob("*.urdf"))
    stls = list(HEXAPOD_18DOF_PKG.glob("*.stl"))
    ok = len(urdfs) >= 1 and len(stls) >= 37
    _status("18-DOF hexapod assets", ok,
            f"{len(urdfs)} URDFs, {len(stls)} STLs" if ok else
            f"incomplete: {len(urdfs)} URDFs, {len(stls)}/37 STLs")
    return ok


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    global passed, failed, skipped

    parser = argparse.ArgumentParser(description="Demo preflight checks")
    parser.add_argument("--full", action="store_true",
                        help="Full dry run (imports URDF into Isaac, slow)")
    parser.add_argument("--no-isaac", action="store_true",
                        help="Skip all Isaac Sim checks")
    args = parser.parse_args()

    print(f"\n{BOLD}{CYAN}=== SolidMind CAD — Demo Preflight ==={RESET}\n")

    # ── Python & packages ──
    print(f"{BOLD}Environment{RESET}")
    check_python()
    check_package_importable()
    check_gear_engine()
    check_hexapod_3dof_controller()
    check_tests()
    print()

    # ── FreeCAD ──
    print(f"{BOLD}FreeCAD{RESET}")
    fc_ok = check_freecad_connection()
    if fc_ok:
        check_freecad_new_doc()
    else:
        _status("FreeCAD create document", None, "skipped (no connection)")
    print()

    # ── 18-DOF build script ──
    print(f"{BOLD}18-DOF Hexapod{RESET}")
    check_18dof_build_script()
    check_18dof_hexapod_assets()
    print()

    # ── Hexapod assets (1-DOF legacy) ──
    print(f"{BOLD}Hexapod Assets (1-DOF){RESET}")
    assets_ok = check_hexapod_assets()
    print()

    # ── Isaac Sim ──
    if args.no_isaac:
        print(f"{BOLD}Isaac Sim{RESET}  (skipped with --no-isaac)")
        _status("Isaac checks", None, "skipped")
        print()
    else:
        print(f"{BOLD}Isaac Sim{RESET}")
        check_isaac_python()
        bridge_ok = check_isaac_bridge()

        if args.full and bridge_ok and assets_ok:
            check_isaac_urdf_import()
            check_isaac_screenshot()
        elif args.full and not bridge_ok:
            _status("Isaac URDF import", None, "skipped (no bridge)")
            _status("Isaac screenshot", None, "skipped (no bridge)")
        elif not args.full:
            _status("Isaac URDF import", None, "skipped (use --full)")
            _status("Isaac screenshot", None, "skipped (use --full)")
        print()

    # ── Summary ──
    passed + failed + skipped
    print(f"{BOLD}{'=' * 50}{RESET}")
    color = GREEN if failed == 0 else RED
    print(f"{color}{BOLD}{passed} passed, {failed} failed, {skipped} skipped{RESET}")

    if failed == 0:
        print(f"\n{GREEN}{BOLD}Ready to record!{RESET}\n")
    else:
        print(f"\n{RED}{BOLD}Fix the failures above before recording.{RESET}\n")
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
