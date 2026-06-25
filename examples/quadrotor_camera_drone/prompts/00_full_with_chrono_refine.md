I want you to design, build, optimize, and fly a long-endurance quadrotor for a DSLR-class camera payload — basically a hand-built DJI Inspire-style aerial camera rig. End to end: design brief → CAD geometry → BEMT optimization → Chrono refinement loop (model ↔ sim iteration) → final prop build → PX4 + Gazebo flight verification.

This variant of the prompt adds a **Chrono refinement loop** between the analytical BEMT winner and the final prop build. BEMT is a fast, computation-only screening — it picks the best candidate but doesn't see hub bearing loads, blade root stress, mass imbalance, or off-design dynamics. The Chrono phase takes the BEMT winner, perturbs the geometry by small amounts, *actually rebuilds each candidate in the FreeCAD model*, runs each through Chrono multi-body dynamics, and locks in the best survivor before we commit to flight. Model ↔ sim iteration, not one-shot.

Constraints (fixed):
- Payload: 2.5 kg camera + 3-axis gimbal (model as a 100×80×60 mm block at the drone's centroid)
- Battery: 6S2P Li-ion 18650 pack, 300 Wh, 1.5 kg, 100×80×40 mm
- Frame topology: quadrotor in X configuration, 700 mm wheelbase
- Target: maximize hover time on the fixed battery
- T/W ≥ 1.8 at full throttle for stability margin
- Blade root factor of safety ≥ 1.5 under design-RPM dynamic load
- Hub bearing load within 2× the BEMT mean thrust (no resonance amplification)

Work through seven phases in order. After EACH phase, summarize what you did and explicitly ask me "OK to proceed?" before starting the next phase. If I say to proceed, continue without further prompting.

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
Phase 3 — Rotor optimization (BEMT analytical sweep)
══════════════════════════════════════════════════════════════════════

This is the cheap, computation-only screen. We're picking a seed for the refinement loop, not a final answer.

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

Pick the BEMT winner — call it the **seed**. Tell me the seed params and predicted hover time. Summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 4 — Build the seed prop in FreeCAD
══════════════════════════════════════════════════════════════════════

Build the seed in geometry first so we have something to perturb against.

1. geometry.propeller_blade with the seed's params (diameter_mm, num_blades, chord_root_mm, chord_tip_mm, twist_root_deg, twist_tip_deg, num_sections=6).
2. Follow the returned build_hint EXACTLY:
   - For each of the 6 sections: cad.sketch on an offset XZ datum plane at section.plane_offset_mm using the section's geometry_ref
   - cad.loft across all 6 section sketches → blade body
   - cad.polar_pattern with occurrences=num_blades around the prop spin axis → full prop
3. Mount one prop body on the FL motor mount only for now (we'll instance the rest after refinement). cad.set_placement at (+247.5, +247.5, 0).
4. Take a screenshot. Confirm geometry looks like a prop, not a screw.

Mark the prop part "built_seed" via design.update_part. Summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 5 — Chrono refinement loop (model ↔ sim iteration)
══════════════════════════════════════════════════════════════════════

Now we close the inner loop. BEMT predicted thrust and power; Chrono will tell us what the rotor actually *does* under design-RPM dynamics — hub bearing loads, blade root bending moment, tip deflection, and mass-imbalance signature. We perturb the seed by small amounts, **actually rebuild the blade body in FreeCAD for each candidate**, run Chrono, compare, and either lock in the seed or accept a refined winner. Up to 2 rounds.

### Step 5.0 — Stand up the Chrono backend

- sim.engine_status — confirm Chrono is reachable. If not, sim.start_engine(backend="chrono").
- Do NOT stop the engine between candidates; reuse it across the sweep (per .claude/rules/sim-engine-policy.md).

### Step 5.1 — Define the rotor test article as a mechanism

We isolate one rotor on a fixed hub — the airframe doesn't matter for Chrono's job here.

motion.define_mechanism with:
- 1 fixed link: hub (the FL motor mount cylinder)
- 1 revolute joint: rotor_test_joint, axis (0, 0, 1) at the hub centre
- 1 rotating link: the seed prop body
- driven by torque-controlled actuator at design RPM (4000)
- aerodynamic load: **distributed thrust along blade span** from BEMT's per-section data. The high-level study tools (`study.results`) only surface aggregate scalars, so call `_bemt_solve` from `server.study_solvers` directly with the candidate's params and read its `per_section: tuple[_BEMTStation, ...]` field. Each `_BEMTStation` is one annulus with:
    - `r_m`        — radial station centre on the blade
    - `dr_m`       — annulus width (already integrated into `dT_N` / `dQ_Nm`)
    - `dT_N`       — thrust contribution from this annulus, summed over all blades
    - `dQ_Nm`      — torque contribution about the spin axis, summed over all blades
    - `chord_m`, `twist_deg`, `converged`
  Apply the loads as:
    - `dT_N / num_blades` as a +Z point load on **each blade body** at radius `r_m` (one load per station per blade — typically 15 stations × num_blades loads total)
    - `dQ_Nm` accumulates naturally from the tangential aero component if you instead apply `dT_N / num_blades` as a body-frame force aligned with the BEMT velocity triangle; for the purposes of Phase 5 (where we care about hub bearing load, blade root moment, tip deflection, and imbalance) the simpler thrust-only application is sufficient — apply hub torque from `sum(dQ_Nm)` directly at the actuator
  Sanity check: `sum(s.dT_N) ≈ thrust_N` and `sum(s.dQ_Nm) ≈ torque_Nm` from the same solve (the test suite asserts this to 9 decimals).

Save the mechanism_id — you'll re-use it for every candidate. Only the blade geometry changes; the load station list and per-station magnitudes update from each candidate's fresh BEMT solve, so the distributed load tracks chord-taper and twist-distribution changes (which a single 0.75R lumped load would have missed).

### Step 5.2 — Build the perturbation grid

Small, local perturbations around the seed (NOT a fresh search):
- chord_root_mm: [seed × 0.95, seed, seed × 1.05]
- chord_tip_mm:  [seed × 0.95, seed, seed × 1.05]
- twist_root_deg: [seed − 1.5°, seed, seed + 1.5°]
- twist_tip_deg:  [seed − 1.5°, seed, seed + 1.5°]

Don't run the full 81-cell Cartesian product. Use a **central-composite**-style subset: the seed itself + 4 axial pairs (one parameter at a time) + 4 corner points = **13 candidates** total. Enumerate them in a table before running anything.

### Step 5.3 — Iterate (the actual loop)

For each candidate (including the seed as candidate 0):

a. **Update the model.**
   - design.update_part for the prop with the candidate's params.
   - Delete the existing blade body via cad.delete_objects (keep the polar pattern feature).
   - Re-run geometry.propeller_blade with the candidate params, then re-loft + re-polar-pattern. The body label stays the same so the mechanism reference holds.
   - cad.screenshot — visual sanity check on every candidate so I can see what changed.

b. **Re-sim.**
   - cad.export_sim_package(mechanism_id=..., emit_urdf=true, emit_sdf=false) — fresh meshes for the refined geometry.
   - motion.simulate(backend="chrono", duration_s=2.0, step_s=1e-4, mechanism_id=..., capture=["thrust_mean_N", "thrust_std_N", "hub_bearing_load_N", "blade_root_moment_Nm", "tip_deflection_mm", "imbalance_amplitude_mm"]).

c. **Score.**
   For each candidate compute:
   - thrust_delivered_ratio = thrust_mean_N / 14.7        (target: ≥ 0.95; flag BEMT-optimistic if < 0.95)
   - bearing_load_ratio     = hub_bearing_load_N / thrust_mean_N    (target: ≤ 2.0)
   - FoS_blade_root = sigma_yield_carbon / sigma_root_from_moment   (target: ≥ 1.5)
   - hover_time_min as in Phase 3, but using thrust_delivered_ratio to correct power
   - composite_score = hover_time_min × min(1, FoS_blade_root / 1.5) × min(1, 2.0 / bearing_load_ratio)

d. **Reject** any candidate with FoS_blade_root < 1.5 or bearing_load_ratio > 2.5 outright.

Present a ranked table after the round:
  | # | Δchord_r | Δchord_t | Δtwist_r | Δtwist_t | thrust_ratio | bearing_ratio | FoS_root | tip_defl | hover_min | score |

### Step 5.4 — Decide and (maybe) loop again

- If the **seed** wins → lock the seed geometry. Skip to Phase 6.
- If a different candidate wins by < 1% in composite_score → lock the seed (refinement noise; not worth committing).
- If a refined candidate wins by ≥ 1% → adopt it as the new seed, **rebuild the blade in CAD with its params**, and run **one more refinement round** (smaller perturbation: ±2.5% chord, ±0.75° twist) centred on the new seed. Cap at 2 rounds total.

After convergence, take a final screenshot of the locked blade body. Report what changed from the BEMT seed and why (which Chrono signal forced the change).

Summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 6 — Instance the locked prop across all 4 motors
══════════════════════════════════════════════════════════════════════

The geometry is now frozen. Mount it on the remaining three motor mounts:

- cad.linear_pattern across the 4 motor positions, OR 4 instances via cad.set_placement at the four ±247.5 mm corner positions.
- design.update_part(..., status="built").
- design.verify_build(brief_id) — confirm the brief is closed.
- Final beauty shot.

Report:
- Final hover time vs. (a) the 18" 2-blade NACA 4412 baseline, (b) the BEMT-only seed, (c) the Chrono-refined winner.
- Search-space coverage: BEMT cells × Chrono refinement cells.
- The single Chrono signal that most influenced the final geometry choice.

Summarize and ask permission to proceed.

══════════════════════════════════════════════════════════════════════
Phase 7 — Verify it flies (PX4 + Gazebo)
══════════════════════════════════════════════════════════════════════

1. Build the full-airframe mechanism: motion.define_mechanism with 4 continuous joints named rotor_FL_joint, rotor_FR_joint, rotor_RR_joint, rotor_RL_joint. Axis (0, 0, 1) at each motor mount, prop bodies as the rotating links.

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

Report final hover time alongside (a) the BEMT prediction from Phase 3, (b) the Chrono-refined prediction from Phase 5, and (c) the actual Gazebo behaviour. Three numbers — they should converge. If they don't, that's the interesting finding.

══════════════════════════════════════════════════════════════════════
Done.
══════════════════════════════════════════════════════════════════════

Begin with Phase 1.
