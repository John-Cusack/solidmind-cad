#!/usr/bin/env python3
"""Demo: auto-profile generation for hexapod teleop.

Defines an 18-DOF hexapod mechanism, calls teleop_start with NO explicit
profile, and prints the auto-generated profile alongside the old manual
config for comparison.

Usage::

    # Dry run (no Isaac bridge needed — just prints the auto-profile)
    python3 scripts/demo_auto_profile.py

    # Live run (Isaac bridge must be running on :9878)
    python3 scripts/demo_auto_profile.py --live

    # Compare against existing manual config
    python3 scripts/demo_auto_profile.py --compare hexapod_18dof_v2_pkg/teleop_config.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

# ── Geometry constants (from demo_build_hexapod_18dof.py) ─────────
COXA_LEN = 52.0    # mm
FEMUR_LEN = 66.0   # mm
TIBIA_LEN = 133.0  # mm

LEGS: dict[str, tuple[float, float]] = {
    "L1": (52.0, 30.0),
    "L2": (0.0, 60.0),
    "L3": (-52.0, 30.0),
    "R1": (52.0, -30.0),
    "R2": (0.0, -60.0),
    "R3": (-52.0, -30.0),
}


def _make_mechanism_dict() -> dict[str, Any]:
    """Build a minimal 18-DOF hexapod mechanism dict."""
    parts: list[dict[str, Any]] = [
        {"id": "chassis", "is_ground": True},
    ]
    joints: list[dict[str, Any]] = []

    for leg_id, (ax, ay) in LEGS.items():
        angle = math.atan2(ay, ax)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        coxa_id = f"coxa_{leg_id}"
        femur_id = f"femur_{leg_id}"
        tibia_id = f"tibia_{leg_id}"

        parts.extend([
            {"id": coxa_id},
            {"id": femur_id},
            {"id": tibia_id},
        ])

        # Coxa joint at chassis attachment point
        joints.append({
            "id": f"hip_yaw_{leg_id}",
            "joint_type": "revolute",
            "parent_part": "chassis",
            "child_part": coxa_id,
            "axis": [0.0, 0.0, 1.0],
            "origin": [ax, ay, 0.0],
            "min_angle_deg": -45.0,
            "max_angle_deg": 45.0,
        })

        # Femur joint at end of coxa
        femur_x = ax + COXA_LEN * cos_a
        femur_y = ay + COXA_LEN * sin_a
        joints.append({
            "id": f"hip_pitch_{leg_id}",
            "joint_type": "revolute",
            "parent_part": coxa_id,
            "child_part": femur_id,
            "axis": [0.0, 1.0, 0.0],
            "origin": [femur_x, femur_y, 0.0],
            "min_angle_deg": -90.0,
            "max_angle_deg": 90.0,
        })

        # Tibia joint at end of femur
        tibia_x = femur_x + FEMUR_LEN * cos_a
        tibia_y = femur_y + FEMUR_LEN * sin_a
        joints.append({
            "id": f"knee_{leg_id}",
            "joint_type": "revolute",
            "parent_part": femur_id,
            "child_part": tibia_id,
            "axis": [0.0, 1.0, 0.0],
            "origin": [tibia_x, tibia_y, 0.0],
            "min_angle_deg": -120.0,
            "max_angle_deg": 0.0,
        })

    return {
        "name": "Hexapod_18DOF_auto",
        "parts": parts,
        "joints": joints,
        "drives": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Send teleop_start to Isaac bridge")
    parser.add_argument("--compare", type=str, help="Path to manual teleop_config.json for comparison")
    parser.add_argument("--urdf", type=str, default="hexapod_18dof_v2_pkg/Hexapod_18DOF.urdf",
                        help="URDF path for live mode")
    args = parser.parse_args(argv)

    # ── Step 1: Define mechanism ──────────────────────────────────
    from server.motion_models import Mechanism
    from server.tools_motion import _build_profile_from_mechanism

    mech_dict = _make_mechanism_dict()
    mech = Mechanism.from_dict(mech_dict)

    print("=" * 60)
    print("Auto-Profile Generation Demo")
    print("=" * 60)
    print(f"\nMechanism: {mech.name}")
    print(f"  Parts:  {len(mech.parts)}")
    print(f"  Joints: {len(mech.joints)}")

    # ── Step 2: Generate auto-profile ─────────────────────────────
    auto_profile = _build_profile_from_mechanism(mech)

    print(f"\n--- Auto-generated profile ---")
    print(json.dumps(auto_profile, indent=2))

    print(f"\nSummary:")
    print(f"  Controller type: {auto_profile.get('controller_type')}")
    print(f"  DOFs per leg:    {auto_profile.get('dofs_per_leg')}")
    print(f"  Legs found:      {len(auto_profile.get('hip_mounts', []))}")
    print(f"  l_coxa:          {auto_profile.get('l_coxa', 0):.4f} m")
    print(f"  l_femur:         {auto_profile.get('l_femur', 0):.4f} m")
    print(f"  body_length:     {auto_profile.get('body_length', 0):.4f} m")
    print(f"  body_width:      {auto_profile.get('body_width', 0):.4f} m")

    # ── Step 3: Compare with manual config ────────────────────────
    if args.compare:
        try:
            with open(args.compare) as f:
                manual = json.load(f)
            print(f"\n--- Manual config ({args.compare}) ---")
            # Compare key fields
            _COMPARE_KEYS = [
                "controller_type", "l_coxa", "l_femur", "l_tibia",
                "body_length", "body_width", "leg_joint_names",
            ]
            print(f"  {'Field':<20} {'Auto':>12} {'Manual':>12} {'Match':>6}")
            print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*6}")
            for key in _COMPARE_KEYS:
                auto_val = auto_profile.get(key)
                manual_val = manual.get(key)
                if isinstance(auto_val, float) and isinstance(manual_val, float):
                    match = "~" if abs(auto_val - manual_val) < 0.01 else "X"
                    print(f"  {key:<20} {auto_val:>12.4f} {manual_val:>12.4f} {match:>6}")
                elif isinstance(auto_val, list) and isinstance(manual_val, list):
                    match = "✓" if set(auto_val) == set(manual_val) else "X"
                    print(f"  {key:<20} {'['+str(len(auto_val))+']':>12} {'['+str(len(manual_val))+']':>12} {match:>6}")
                else:
                    match = "✓" if auto_val == manual_val else "X"
                    print(f"  {key:<20} {str(auto_val):>12} {str(manual_val):>12} {match:>6}")
        except FileNotFoundError:
            print(f"\nWarning: manual config not found: {args.compare}")

    # ── Step 4: Live teleop (optional) ────────────────────────────
    if args.live:
        import os
        if not os.path.isfile(args.urdf):
            print(f"\nError: URDF not found: {args.urdf}", file=sys.stderr)
            return 1

        from server import motion_store
        from server.tools_motion import motion_define_mechanism, motion_teleop_start

        print(f"\n--- Live teleop (Isaac bridge) ---")
        print(f"  URDF: {args.urdf}")

        # Define mechanism
        result = motion_define_mechanism(mech_dict)
        if not result.get("ok"):
            print(f"  Define failed: {result.get('error')}", file=sys.stderr)
            return 1
        mid = result["mechanism_id"]
        print(f"  Mechanism ID: {mid}")

        # Start teleop with NO profile — auto-profile should fill everything
        result = motion_teleop_start(
            mechanism_id=mid,
            backend="isaac",
            urdf_path=os.path.abspath(args.urdf),
            import_config={"robot_type": "mobile"},
        )

        if result.get("ok"):
            print(f"  Session ID: {result.get('session_id')}")
            print(f"  Controller: {result.get('controller_type')}")
            if "profile_used" in result:
                print(f"  Profile used: {json.dumps(result['profile_used'], indent=4)}")
            print(f"\n  Teleop started! Use keyboard_teleop.py or motion.teleop_command.")
        else:
            err = result.get("error", {})
            print(f"  Teleop failed: [{err.get('code')}] {err.get('message')}", file=sys.stderr)
            return 1

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
