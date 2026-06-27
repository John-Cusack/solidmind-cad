"""Diagnose 3-DOF controller: trace what each leg does over a gait cycle.

Run:  python3 scripts/diagnose_3dof_controller.py
"""

from __future__ import annotations

import math

from isaac_bridge.controllers import Hexapod3DOFController
from isaac_bridge.hexapod_ik import (
    LegGeometry,
    body_to_hip_frame,
    forward_kinematics,
    inverse_kinematics,
)
from isaac_bridge.models import TeleopConfig, TeleopState

DEG = math.degrees
RAD = math.radians

# ── Use default config (matching the 18-DOF hexapod) ────────────────
config = TeleopConfig.from_profile(
    {
        "controller_type": "hexapod_3dof_tripod",
    }
)

geom = LegGeometry(l_coxa=config.l_coxa, l_femur=config.l_femur, l_tibia=config.l_tibia)
ctrl = Hexapod3DOFController()

LEG_NAMES = ["LF", "LM", "LR", "RF", "RM", "RR"]

# ── 1. Check geometry ────────────────────────────────────────────────
print("=" * 70)
print("GEOMETRY CHECK")
print(
    f"  l_coxa={geom.l_coxa * 1000:.1f}mm  l_femur={geom.l_femur * 1000:.1f}mm  l_tibia={geom.l_tibia * 1000:.1f}mm"
)
print(
    f"  body_length={config.body_length * 1000:.1f}mm  body_width={config.body_width * 1000:.1f}mm"
)
print(f"  stance_height={config.stance_height * 1000:.1f}mm")
print(f"  step_height={config.step_height * 1000:.1f}mm")
print(f"  stride_length={config.stride_length * 1000:.1f}mm")
print(f"  duty_factor={config.duty_factor}")
print(f"  stride_hz={config.stride_hz}")
print()

# ── 2. Check hip mounts and default feet ─────────────────────────────
print("=" * 70)
print("HIP MOUNTS & DEFAULT FOOT POSITIONS")

# Force init
state_zero = TeleopState()
ctrl.compute_targets(state_zero, 0.02, config, 0.0)

for i, name in enumerate(LEG_NAMES):
    mount = ctrl._mounts[i]
    foot = ctrl._default_feet[i]
    bias = ctrl._standing_bias[i]
    print(f"\n  {name}:")
    print(
        f"    mount: ({mount.x * 1000:.1f}, {mount.y * 1000:.1f}) mm, angle={DEG(mount.angle):.1f}°"
    )
    print(
        f"    default foot (body frame): ({foot[0] * 1000:.1f}, {foot[1] * 1000:.1f}, {foot[2] * 1000:.1f}) mm"
    )
    print(
        f"    standing bias: coxa={DEG(bias[0]):.2f}°  femur={DEG(bias[1]):.2f}°  tibia={DEG(bias[2]):.2f}°"
    )

    # Verify IK roundtrip
    hip_pt = body_to_hip_frame(foot, mount)
    angles = inverse_kinematics(hip_pt[0], hip_pt[1], hip_pt[2], geom)
    fk_pt = forward_kinematics(angles, geom)
    dist = math.sqrt(
        (hip_pt[0] - fk_pt[0]) ** 2 + (hip_pt[1] - fk_pt[1]) ** 2 + (hip_pt[2] - fk_pt[2]) ** 2
    )
    print(
        f"    hip frame target: ({hip_pt[0] * 1000:.1f}, {hip_pt[1] * 1000:.1f}, {hip_pt[2] * 1000:.1f}) mm"
    )
    print(
        f"    IK solution: coxa={DEG(angles.coxa):.2f}°  femur={DEG(angles.femur):.2f}°  tibia={DEG(angles.tibia):.2f}°"
    )
    print(f"    FK roundtrip error: {dist * 1000:.3f} mm")

# ── 3. Run controller for a few seconds and check outputs ────────────
print("\n" + "=" * 70)
print("CONTROLLER OUTPUT DURING WALK (vx=0.3 m/s)")

DT = 0.02
VX = 0.3
phase = 0.0

# Warm up slew filter
state = TeleopState(vx_mps=VX)
for _ in range(50):
    _, phase = ctrl.compute_targets(state, DT, config, phase)

# Sample 3 ticks
for tick in range(3):
    targets, phase = ctrl.compute_targets(state, DT, config, phase)
    print(f"\n  tick {tick}, phase={DEG(phase):.1f}°:")
    for i, name in enumerate(LEG_NAMES):
        base = i * 3
        jnames = config.leg_joint_names[base : base + 3]
        vals = [targets[j] for j in jnames]
        print(
            f"    {name}: coxa={DEG(vals[0]):+7.2f}°  femur={DEG(vals[1]):+7.2f}°  tibia={DEG(vals[2]):+7.2f}°"
        )

# ── 4. Check range of motion over full gait cycle ────────────────────
print("\n" + "=" * 70)
print("JOINT RANGE OVER 2 FULL GAIT CYCLES")

# Reset controller
ctrl2 = Hexapod3DOFController()
phase = 0.0
for _ in range(50):
    _, phase = ctrl2.compute_targets(state, DT, config, phase)

# Track min/max per joint
mins = {j: float("inf") for j in config.leg_joint_names}
maxs = {j: float("-inf") for j in config.leg_joint_names}

for _ in range(200):  # 4 seconds at 50Hz
    targets, phase = ctrl2.compute_targets(state, DT, config, phase)
    for j, v in targets.items():
        mins[j] = min(mins[j], v)
        maxs[j] = max(maxs[j], v)

print()
for i, name in enumerate(LEG_NAMES):
    base = i * 3
    jnames = config.leg_joint_names[base : base + 3]
    print(f"  {name}:")
    for j, label in zip(jnames, ["coxa", "femur", "tibia"], strict=False):
        range_deg = DEG(maxs[j]) - DEG(mins[j])
        print(
            f"    {label:6s}: {DEG(mins[j]):+7.2f}° to {DEG(maxs[j]):+7.2f}°  (range: {range_deg:.2f}°)"
        )

# ── 5. Check if dead reckoning diverges ──────────────────────────────
print("\n" + "=" * 70)
print("DEAD RECKONING AFTER 4 SECONDS OF WALKING")
print(f"  body_x={ctrl2._body_x * 1000:.1f} mm")
print(f"  body_y={ctrl2._body_y * 1000:.1f} mm")
print(f"  body_yaw={DEG(ctrl2._body_yaw):.1f}°")
print(f"  Expected: ~{VX * 4 * 1000:.0f} mm forward at vx={VX} m/s")
print()

# ── 6. Sanity: what happens at vx=0? ────────────────────────────────
print("=" * 70)
print("STANDING STILL (vx=0) — should all be near zero")
ctrl3 = Hexapod3DOFController()
state_still = TeleopState(vx_mps=0.0)
targets_still, _ = ctrl3.compute_targets(state_still, DT, config, 0.0)
max_dev = 0.0
for _, v in targets_still.items():
    max_dev = max(max_dev, abs(v))
print(f"  Max deviation from zero: {DEG(max_dev):.4f}°")
if max_dev < 0.001:
    print("  ✓ Standing still works correctly")
else:
    print("  ✗ PROBLEM: joints not at zero when standing still!")
    for j, v in targets_still.items():
        if abs(v) > 0.001:
            print(f"    {j}: {DEG(v):.4f}°")
