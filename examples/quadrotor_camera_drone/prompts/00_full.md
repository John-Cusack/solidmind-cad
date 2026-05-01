I want you to design, build, optimize, and fly a long-endurance quadrotor for a DSLR-class camera payload — basically a hand-built DJI Inspire-style aerial camera rig. End to end: design brief → CAD geometry → BEMT optimization → final prop build → PX4 + Gazebo flight verification.

Constraints (fixed):
- Payload: 2.5 kg camera + 3-axis gimbal (model as a 100×80×60 mm block at the drone's centroid)
- Battery: 6S2P Li-ion 18650 pack, 300 Wh, 1.5 kg, 100×80×40 mm
- Frame topology: quadrotor in X configuration, 700 mm wheelbase
- Target: maximize hover time on the fixed battery
- T/W ≥ 1.8 at full throttle for stability margin

Work through five phases in order. After EACH phase, summarize what you did and explicitly ask me "OK to proceed?" before starting the next phase. If I say to proceed, continue without further prompting.

══════════════════════════════════════════════════════════════════════
Phase 1 — Sizing (design brief)
══════════════════════════════════════════════════════════════════════

1. Compute target AUW from a frame/motor/ESC/wiring budget. Show me the line items with masses (~0.8 kg frame, ~0.6 kg motors+ESCs, ~0.6 kg wiring+electronics is reasonable; use your engineering judgement).

2. Pick a motor class. Use a low-Kv (~150-180 Kv) cinema-class motor (T-Motor MN605S 320Kv or similar). Justify the Kv range from the target hover RPM (~4000 RPM for 6S battery → Kv ≈ ω/V).

3. Propose a prop diameter range with the disk-loading rationale. Smaller disk loading = more efficient hover. Constrain by frame wheelbase (max prop diameter ~22" so props don't collide).

Save this as a design brief via design.save_brief, register the parts via design.add_part (frame center, 4 arms, 4 motor mounts, payload block, battery pack, 4 props), then summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 2 — Airframe build in FreeCAD
══════════════════════════════════════════════════════════════════════

1. Add interfaces via design.add_interface — bolt patterns connecting arms to the frame, motor mounts to arm tips, payload mount, battery mount.

2. Update the brief with layout positions: arms emanate diagonally from the frame center at ±247.5 mm (X-pattern 700 mm wheelbase), payload below frame, battery on top.

3. Build the airframe in FreeCAD:
   - cad.new_document("CameraDrone")
   - Frame center body — rectangular pad ~200×200×30 mm via cad.sketch on XY + cad.pad
   - One arm — rectangular pad extending out from the frame; use cad.polar_pattern with occurrences=4 around Z to make all 4 arms
   - Motor mounts — small cylinders at arm tips (radius 25 mm, height 20 mm). Place via cad.set_placement at the four ±247.5 mm corner positions
   - Payload block — 100×80×60 mm box centred BELOW the frame
   - Battery pack — 100×80×40 mm box centred ABOVE the frame

Take a verification screenshot after each major step. Mark each part "built" via design.update_part as you go. Skip props for now — they come after optimization.

Then summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 3 — Rotor optimization (BEMT sweep)
══════════════════════════════════════════════════════════════════════

study.create with:
  solver: "bemt_xfoil"
  fixed_params:
    airfoil: "NACA4412"
    Re: 200000
    rpm: 4000
    rho: 1.225
    hover_thrust_N_per_rotor: 14.7
  param_ranges:
    diameter_mm: [400, 460, 500, 540, 580]
    num_blades: [2, 3]
    chord_root_mm: [25, 30, 35, 40]
    chord_tip_mm: [10, 14, 18]
    twist_root_deg: [20, 24, 28]
    twist_tip_deg: [6, 10, 14]
  primary_metric: "efficiency"

study.run, study.status until done, study.results top_n=5.

Compute hover_time_min for each top-5 variant inline:
  P_total_W      = power_W × 4 / 0.85
  hover_time_min = (300 / P_total_W) × 60

Present a ranked table:
  | Rank | dia_mm | blades | chord_root | chord_tip | twist_root | twist_tip | power_W | FoM | hover_min |

Pick the winner. Summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 4 — Build the winning prop
══════════════════════════════════════════════════════════════════════

Use geometry.propeller_blade with the winner's params (diameter_mm, num_blades, chord_root_mm, chord_tip_mm, num_sections=6). Pitch derived from the winner's twist.

Follow the returned build_hint EXACTLY:
1. For each of the 6 sections: cad.sketch on an offset XZ datum plane at section.plane_offset_mm using the section's geometry_ref
2. cad.loft across all 6 section sketches (creates the blade)
3. cad.polar_pattern with occurrences=num_blades around the prop spin axis

Mount the prop body on each of the 4 motor mounts (cad.linear_pattern across the 4 motor positions, or 4 instances via cad.set_placement).

Take a final beauty shot. Report:
- Final hover time vs. baseline (18" 2-blade with stock NACA 4412 chord/twist)
- Search-space coverage of the optimization

Summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 5 — Verify it flies (PX4 + Gazebo)
══════════════════════════════════════════════════════════════════════

1. Build the mechanism: motion.define_mechanism with 4 continuous joints named rotor_FL_joint, rotor_FR_joint, rotor_RR_joint, rotor_RL_joint. Axis (0, 0, 1) at each motor mount, prop bodies as the rotating links.

2. Export sim package + PX4 airframe params via cad.export_sim_package:
   - mechanism_id = <id from step 1>
   - emit_sdf = true
   - drone_config = {
       "rotors": [
         {"index": 0, "joint": "rotor_FL_joint", "direction": "ccw", "position_m": (0.2475, 0.2475, 0)},
         {"index": 1, "joint": "rotor_FR_joint", "direction": "cw",  "position_m": (0.2475, -0.2475, 0)},
         {"index": 2, "joint": "rotor_RR_joint", "direction": "ccw", "position_m": (-0.2475, -0.2475, 0)},
         {"index": 3, "joint": "rotor_RL_joint", "direction": "cw",  "position_m": (-0.2475, 0.2475, 0)},
       ],
       "sensors": True,
       "px4": True,
       "register_airframe": True,
     }

   Report airframe_id (a SYS_AUTOSTART number in 50000-50999), airframe_path, computed hover_throttle.

3. Rebuild PX4 with the new airframe via shell:
     cd ~/repos/PX4-Autopilot && make px4_sitl <airframe_name>
   ~30 s incremental build to register the airframe.

4. Launch PX4 SITL with the custom airframe in the background:
     cd ~/repos/PX4-Autopilot && make px4_sitl <airframe_name> &
   Wait until "Startup script returned successfully" appears in the PX4 log.

5. Connect MavlinkController and fly:
   - Stream HEARTBEAT at 3 Hz from sys_id=255
   - Set permissive params: COM_RC_IN_MODE=4, NAV_RCL_ACT=0, NAV_DLL_ACT=0
   - Wait for sensors healthy in SYS_STATUS
   - controller.arm() — force-arm magic 21196 by default
   - controller.takeoff_via_mode() — DO_SET_MODE → AUTO_TAKEOFF (main=4 sub=2)
   - Hold for 30 s — drone hovers in Gazebo at MIS_TAKEOFF_ALT
   - controller.land_via_mode() — DO_SET_MODE → AUTO_LAND (main=4 sub=6)
   - Wait for landed/disarmed

Report final hover time alongside the BEMT prediction from Phase 3. The visible flight in Gazebo proves the optimization landed somewhere flyable.

══════════════════════════════════════════════════════════════════════
Done.
══════════════════════════════════════════════════════════════════════

Begin with Phase 1.
