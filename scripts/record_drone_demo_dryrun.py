#!/usr/bin/env python3
"""Pre-recording dry run for the drone demo.

Walks the same 5 stages as ``examples/quadrotor_camera_drone/run.py``
and ``RECORDING_PROMPT.md`` but with shrunken parameters (lower
takeoff altitude, shorter hover) so it finishes in ~5 minutes instead
of ~20.  Use it to confirm everything is wired and timed before
recording.

What it checks:

1. Environment — FreeCAD bridge listening, PX4 binary present,
   pymavlink importable, ``gz`` on PATH
2. Each of the 5 pipeline stages, individually timed
3. Final pass/fail with hover-altitude reading from the live PX4

It does NOT check the BEMT optimizer's quality (that's part of the LLM
prompt's job).  It does verify that every tool surface the prompt will
exercise is reachable and that the PX4 flight half works on this
machine right now.

Usage::

    python scripts/record_drone_demo_dryrun.py

    # Skip the slow PX4 rebuild + flight stages (Phases 1-4 only)
    python scripts/record_drone_demo_dryrun.py --no-fly

The script delegates the actual building to ``run.py`` (with
``--takeoff-alt 3 --hover-secs 5``).  Output is the wall-clock time of
each stage plus the total — useful for budgeting the recording session.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SCRIPT = REPO_ROOT / "examples" / "quadrotor_camera_drone" / "run.py"


# ----------------------------------------------------------------------
# Environment checks
# ----------------------------------------------------------------------


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}", flush=True)


def check_environment(px4_install: Path) -> bool:
    print("\n=== Environment checks ===")
    ok = True

    # FreeCAD bridge listening on 9876
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(("127.0.0.1", 9876))
        _ok("FreeCAD bridge listening on :9876")
    except OSError as exc:
        _fail(f"FreeCAD bridge :9876 not reachable: {exc}. Run `freecad &`.")
        ok = False

    # PX4 binary
    px4_bin = px4_install / "build" / "px4_sitl_default" / "bin" / "px4"
    if px4_bin.is_file() and os.access(px4_bin, os.X_OK):
        _ok(f"PX4 binary present at {px4_bin}")
    else:
        _fail(
            f"PX4 binary missing at {px4_bin}. Run "
            f"`cd {px4_install} && make px4_sitl gz_x500` once."
        )
        ok = False

    # gz CLI
    if shutil.which("gz"):
        try:
            out = subprocess.run(
                ["gz", "sim", "--version"],
                capture_output=True, text=True, timeout=4,
            )
            version = out.stdout.strip().splitlines()[-1] if out.stdout else "?"
            _ok(f"gz sim available (version {version})")
        except subprocess.SubprocessError:
            _warn("gz sim version probe timed out (still usable)")
    else:
        _fail("gz not on PATH. See docs/px4_integration.md install steps.")
        ok = False

    # pymavlink importable
    try:
        import pymavlink  # noqa: F401
        _ok("pymavlink importable")
    except ImportError:
        _fail("pymavlink not installed. Run: pip install -e '.[drone]'")
        ok = False

    # xfoil (optional — BEMT falls back to flat-plate without it)
    if shutil.which("xfoil"):
        _ok("xfoil on PATH (BEMT will use real airfoil polars)")
    else:
        _warn("xfoil missing — BEMT will use flat-plate fallback (still works)")

    # SOLIDMIND_PX4_INSTALL env var
    env_install = os.environ.get("SOLIDMIND_PX4_INSTALL")
    if env_install:
        _ok(f"SOLIDMIND_PX4_INSTALL={env_install}")
    else:
        _warn(
            f"SOLIDMIND_PX4_INSTALL not set. Defaulting to {px4_install}. "
            "Export it if your PX4 is elsewhere."
        )

    return ok


# ----------------------------------------------------------------------
# Stage runner
# ----------------------------------------------------------------------


def run_stage(stage: str, args: argparse.Namespace) -> tuple[bool, float]:
    """Invoke run.py with ``--stop-after <stage>`` and time it."""
    cmd = [
        sys.executable, str(RUN_SCRIPT),
        "--output-dir", str(args.output_dir),
        "--takeoff-alt", str(args.takeoff_alt),
        "--hover-secs", str(args.hover_secs),
        "--px4-install", str(args.px4_install),
        "--stop-after", stage,
    ]
    if stage != "build" and args.skip_px4_rebuild:
        cmd.append("--skip-px4-rebuild")

    print(f"\n=== Stage: {stage} ===", flush=True)
    print(f"$ {' '.join(cmd)}", flush=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env={
            **os.environ, "PYTHONPATH": str(REPO_ROOT),
        })
    except KeyboardInterrupt:
        print(f"  interrupted after {time.monotonic() - t0:.1f}s", flush=True)
        return False, time.monotonic() - t0
    elapsed = time.monotonic() - t0

    if proc.returncode == 0:
        _ok(f"stage '{stage}' completed in {elapsed:.1f}s")
        return True, elapsed
    _fail(f"stage '{stage}' exited {proc.returncode} after {elapsed:.1f}s")
    return False, elapsed


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output-dir", type=Path, default=Path("/tmp/camera_drone_dryrun"),
        help="Where run.py drops STL/URDF/SDF artifacts",
    )
    p.add_argument(
        "--takeoff-alt", type=float, default=3.0,
        help="Lower than the demo's 5m so the dry run is faster",
    )
    p.add_argument(
        "--hover-secs", type=float, default=5.0,
        help="Brief hover so the dry run finishes in ~5 min",
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
        help="Reuse the existing PX4 build (skip Stage 3 rebuild). "
             "Use this if you've already built once today.",
    )
    p.add_argument(
        "--no-fly", action="store_true",
        help="Stop after Stage 4 (build the prop). Skips PX4 rebuild + "
             "flight — useful when iterating on geometry quickly.",
    )
    args = p.parse_args()

    print("\n" + "=" * 70)
    print("  Drone demo dry run")
    print("  Walks the 5-stage pipeline used by RECORDING_PROMPT.md")
    print("=" * 70)

    if not check_environment(args.px4_install):
        print("\n\033[31mEnvironment checks failed — fix the above and re-run.\033[0m")
        return 2

    stages = ["build", "export", "rebuild", "launch", "fly"]
    if args.no_fly:
        stages = ["build", "export"]
        print("\n(--no-fly: skipping rebuild/launch/fly stages)")

    timings: list[tuple[str, bool, float]] = []
    overall_t0 = time.monotonic()
    for stage in stages:
        ok, elapsed = run_stage(stage, args)
        timings.append((stage, ok, elapsed))
        if not ok:
            break

    overall = time.monotonic() - overall_t0
    print("\n" + "=" * 70)
    print("  Stage timing summary")
    print("=" * 70)
    for stage, ok, elapsed in timings:
        marker = "✓" if ok else "✗"
        print(f"  [{marker}] {stage:10s}  {elapsed:6.1f}s")
    print(f"  {'-' * 22}")
    print(f"  total      {overall:6.1f}s ({overall / 60:.1f} min)")

    all_ok = all(ok for _, ok, _ in timings)
    if all_ok:
        print(
            f"\n\033[32m✓ Dry run passed.\033[0m "
            f"Recording session should fit in ~{overall * 1.3 / 60:.0f} min "
            f"with retakes."
        )
        return 0
    print("\n\033[31m✗ Dry run failed at some stage. See output above.\033[0m")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
