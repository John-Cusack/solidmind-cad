"""Plot 2-DOF hexapod gait controller output for different body shapes.

Shows joint angles over time for rectangular vs circular leg layouts,
so you can compare gait behavior.

Run:  python3 scripts/plot_2dof_gait.py
"""
from __future__ import annotations

import math
import sys

from isaac_bridge.controllers import Hexapod2DOFController
from isaac_bridge.models import TeleopConfig, TeleopState

try:
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib not installed — run: pip install matplotlib")
    sys.exit(1)

# ── Shared params ────────────────────────────────────────────────────
JOINT_NAMES = [
    "hip_yaw_L1", "hip_pitch_L1",
    "hip_yaw_L2", "hip_pitch_L2",
    "hip_yaw_L3", "hip_pitch_L3",
    "hip_yaw_R1", "hip_pitch_R1",
    "hip_yaw_R2", "hip_pitch_R2",
    "hip_yaw_R3", "hip_pitch_R3",
]

SHARED_PROFILE = {
    "controller_type": "hexapod_2dof_tripod",
    "leg_joint_names": JOINT_NAMES,
    "leg_phase_offsets": [0.0, 0.5, 0.0, 0.5, 0.0, 0.5],
    "left_legs": ["hip_yaw_L1", "hip_yaw_L2", "hip_yaw_L3"],
    "right_legs": ["hip_yaw_R1", "hip_yaw_R2", "hip_yaw_R3"],
    "tripod_a": ["hip_yaw_L1", "hip_yaw_L3", "hip_yaw_R2"],
    "tripod_b": ["hip_yaw_L2", "hip_yaw_R1", "hip_yaw_R3"],
    "dofs_per_leg": 2,
    "amplitude_deg": 18.0,
    "lift_deg": 15.0,
    "stride_hz": 2.0,
    "duty_factor": 0.5,
}

# ── Shape configs ────────────────────────────────────────────────────
# The controller itself is shape-agnostic (same sine waves regardless),
# but these represent what body you'd pair it with.
SHAPES = {
    "Rectangle (130×80 mm)\nLegs straight out the sides": {},
    "Circle (R=75 mm)\nLegs radial at 30°/90°/150°": {},
    "Long rectangle (180×50 mm)\nLegs straight out the sides": {
        "amplitude_deg": 22.0,  # longer body, wider swing to compensate
    },
}

# ── Simulate ─────────────────────────────────────────────────────────
DT = 0.02  # 50 Hz
WARMUP_S = 1.0
RUN_S = 2.0
VX = 0.3

LEG_IDS = ["L1", "L2", "L3", "R1", "R2", "R3"]
LEG_LABELS = ["L1 (front)", "L2 (mid)", "L3 (rear)",
              "R1 (front)", "R2 (mid)", "R3 (rear)"]


def run_controller(profile_overrides: dict) -> tuple[list[float], dict[str, list[float]]]:
    profile = {**SHARED_PROFILE, **profile_overrides}
    config = TeleopConfig.from_profile(profile)
    ctrl = Hexapod2DOFController()
    state = TeleopState(vx_mps=VX)
    phase = 0.0

    # Warm up
    for _ in range(int(WARMUP_S / DT)):
        _, phase = ctrl.compute_targets(state, DT, config, phase)

    # Record
    times: list[float] = []
    history: dict[str, list[float]] = {name: [] for name in JOINT_NAMES}
    t = 0.0
    for _ in range(int(RUN_S / DT)):
        targets, phase = ctrl.compute_targets(state, DT, config, phase)
        times.append(t)
        for name in JOINT_NAMES:
            history[name].append(math.degrees(targets[name]))
        t += DT
    return times, history


# ── Plot ─────────────────────────────────────────────────────────────
n_shapes = len(SHAPES)
fig, axes = plt.subplots(6, n_shapes, figsize=(6 * n_shapes, 14),
                         sharex=True, sharey=True)
fig.suptitle(f"2-DOF Hexapod Gait — vx={VX} m/s, stride=2 Hz\n"
             f"Blue = coxa yaw, Red = femur pitch (lift)", fontsize=14)

for col, (shape_name, overrides) in enumerate(SHAPES.items()):
    times, history = run_controller(overrides)

    for row, (leg_id, label) in enumerate(zip(LEG_IDS, LEG_LABELS, strict=False)):
        ax = axes[row, col] if n_shapes > 1 else axes[row]

        coxa_key = f"hip_yaw_{leg_id}"
        femur_key = f"hip_pitch_{leg_id}"

        ax.plot(times, history[coxa_key], color="#2196F3", linewidth=1.5,
                label="coxa" if row == 0 else None)
        ax.plot(times, history[femur_key], color="#FF5722", linewidth=1.5,
                label="femur" if row == 0 else None)
        ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
        ax.grid(True, alpha=0.3)

        if col == 0:
            ax.set_ylabel(f"{label}\n(deg)", fontsize=9)
        if row == 0:
            ax.set_title(shape_name, fontsize=10)
            ax.legend(loc="upper right", fontsize=8)
        if row == 5:
            ax.set_xlabel("time (s)")

plt.tight_layout()
plt.savefig("gait_2dof_plot.png", dpi=150)
print("Saved gait_2dof_plot.png")
plt.show()
