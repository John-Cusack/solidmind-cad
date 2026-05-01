I want to design a long-endurance quadrotor for a DSLR-class camera payload — basically a hand-built DJI Inspire-style aerial camera rig. Use the design.* pipeline. Present each gate and wait for my approval.

Constraints (fixed):
- Payload: 2.5 kg camera + 3-axis gimbal (model later as a 100×80×60 mm block at the drone's centroid)
- Battery: 6S2P Li-ion 18650 pack, 300 Wh, 1.5 kg, 100×80×40 mm
- Frame topology: quadrotor in X configuration, 700 mm wheelbase
- Target: maximize hover time on the fixed battery
- T/W ≥ 1.8 at full throttle for stability margin

Phase 1 — Intent + Sizing:

1. Compute target AUW from a frame/motor/ESC/wiring budget. Show me the line items with masses (~0.8 kg frame, ~0.6 kg motors+ESCs, ~0.6 kg wiring+electronics is reasonable; use your engineering judgement).

2. Pick a motor class. Use a low-Kv (~150-180 Kv) cinema-class motor. T-Motor MN605S 320Kv or similar. Justify the Kv range from the target hover RPM (4000 RPM for 6S battery → Kv ≈ ω/V).

3. Propose a prop diameter range with the disk-loading rationale. Smaller disk loading = more efficient hover. Constrain by frame wheelbase (max prop diameter ~22" so props don't collide).

Save this as a design brief via design.save_brief, register the parts via design.add_part (frame center, 4 arms, 4 motor mounts, payload block, battery pack, 4 props), and present the part list with masses and dimensions before I approve.
