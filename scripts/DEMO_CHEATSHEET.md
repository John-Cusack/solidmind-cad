# SolidMind CAD — "Holy Smokes" Demo Cheat Sheet

**Goal:** ~2.5 min edited video. Two acts: **planetary gears** (real mechanical CAD depth) then **18-DOF hexapod walking in Isaac Sim** (money shot). All live Claude Code, no fake steps.

## Before Recording

```bash
# Terminal 1: Start FreeCAD, then in its Python console:
import freecad_addon; freecad_addon.start()

# Terminal 2: Start Isaac bridge (~60s to init)
ISAAC_PYTHON=./isaacsim/_build/linux-x86_64/release/python.sh scripts/run_isaac_bridge.sh

# Terminal 3: Preflight
python3 scripts/demo_preflight.py --no-isaac   # FreeCAD-only check
python3 scripts/demo_preflight.py              # with Isaac check
python3 scripts/demo_preflight.py --full       # full dry run (slow)

# Terminal 4: Optional dry run of 18-DOF build
python3 scripts/demo_build_hexapod_18dof.py --fast
```

Screen layout: **Claude Code terminal left 40%, FreeCAD right 60%.** When Isaac comes in (Act 2 Beat 7), swap FreeCAD for Isaac Sim viewport.

### Cleanup between takes

In FreeCAD: `Ctrl+A` → Delete, or close the document.

---

## ACT 1 — Planetary Gear Set (~45s edited)

Opens with:

> **Build a planetary gear set — sun 12 teeth, 3 planets 9 teeth, ring gear. Module 2mm, 8mm thick.**

**Expected tool sequence:**
1. `cad.new_document` + `cad.new_body("Sun")`
2. `geometry.planetary_layout(module=2, sun_teeth=12, planet_teeth=9, num_planets=3)` → 3 geometry_ref handles
3. `cad.sketch(body="Sun", geometry_ref=sun_ref)` → **involute tooth profiles appear** (wow moment #1)
4. `cad.pad(length=8)` → solid sun gear
5. Repeat for 3 planet bodies (new_body + sketch + pad + set_placement at computed mesh positions)
6. Ring gear body with internal involute profile + pad
7. `cad.screenshot(target="iso")` → hero shot

**Why this works:** Mathematically correct involute profiles from the Rust geometry engine. Instantly reads as "real engineering" — not toy CAD.

**Key visual moments (keep at 1x):**
- Step 3: Involute tooth profile sketch
- Step 7: Complete gear assembly hero shot

**Fallback:** Two meshing spur gears (`geometry.spur_gear` x2), or a wine glass via `cad.revolution` + `cad.thickness`.

---

## ACT 2 — 18-DOF Hexapod → Isaac Sim Walking (~2 min edited)

### Beat 1 — Research & Brief (cut to ~10s)

> **Build a full 18-DOF hexapod — 3 joints per leg: coxa, femur, tibia. Research specs and show me your plan.**

**What the LLM should do:**
1. Research AX-12A servo dimensions, hexapod proportions
2. Present design brief:
   - Chassis: rectangular plate ~120×90mm, 5mm thick (length along X, width along Y)
   - Segments: coxa 52mm, femur 66mm, tibia 133mm
   - Servos: AX-12A (32×24×24mm)
   - Standing pose: femur 30° below horizontal, tibia 70°
   - Ground clearance: ~158mm (computed via FK)
3. Wait for user approval

**Editing tip:** Speed through web searches 8x. Keep brief summary at 1x.

---

### Beat 2 — Chassis Build (cut to ~15s)

> **Looks good, build it.**

**Expected tool calls:**
1. `cad.new_document("Hexapod")` + `cad.new_body("Body_Chassis")`
2. `cad.sketch(rect 120×90)` + `cad.pad(length=5)` → **plate appears**
3. `cad.sketch(rect 24×22)` + `cad.pocket(length=3)` → servo cutout
4. `cad.linear_pattern` or manual placement → **6 pockets appear** (wow moment)
5. `cad.fillet(radius=2)` → polished edges

**Key visual moments (keep at 1x):**
- Plate appears (pad)
- 6 servo pockets cut
- Edges round off (fillet)

---

### Beat 3 — Articulated Legs (cut to ~20s)

> **Now add the articulated legs — coxa, femur, tibia segments.**

**Expected tool calls:**
1. `cad.create_primitives([6 coxa links])` — short segments at hip positions
2. `cad.create_primitives([6 femur links])` — angled outward+down at 30°
3. `cad.create_primitives([6 tibia links])` — long segments reaching ground

Positions from FK math (same as `scripts/add_hexapod_servos.py`).

**Key moment at 1x:** Step 3 completing — hexapod leg structure suddenly visible.

---

### Beat 4 — Servo Bodies (cut to ~10s)

> **Add the servo motors at each joint.**

**Expected:**
1. `cad.create_primitives([6 hip-yaw servos])`
2. `cad.create_primitives([6 hip-pitch servos])`
3. `cad.create_primitives([6 knee servos])`
4. `cad.screenshot("iso")` → **hero shot: full 37-body hexapod**

---

### Beat 5 — Mechanism + Validation (cut to ~8s)

> **Define the mechanism and validate it.**

**Expected:**
1. `motion.define_mechanism(...)` — 37 parts, 18 revolute joints + 18 fixed joints
2. `motion.validate(...)` — "18 DOF confirmed"
3. `motion.propagate_motion(...)` — speed/torque at every joint

---

### Beat 6 — URDF Export (cut to ~5s)

> **Export a URDF sim package with 158mm ground clearance.**

**Expected:** `cad.export_sim_package(mechanism_id=..., format="stl")` → 37 STLs + URDF

---

### Beat 7 — Isaac Sim: Import + Walk (cut to ~30s) ← THE MONEY SHOT

> **Import into Isaac Sim and make it walk.**

**Expected:**
1. `motion.teleop_start(mechanism_id=..., urdf_path=...)` — **no profile needed!**
   Auto-profile extracts everything from the mechanism: controller type, 18 joint names,
   leg geometry (l_coxa/l_femur/l_tibia), hip mounts, body dims, tripod phases, left/right.
2. Isaac imports URDF (~60-120s, speed up in edit)
3. `cad.screenshot()` — **hexapod standing in Isaac Sim**
4. `motion.teleop_command(vx_mps=0.3)` — **hexapod walks forward with IK tripod gait**
5. `motion.teleop_command(yaw_rate_rps=0.5)` — **hexapod turns**
6. `cad.screenshot()` from multiple angles

**Keep walking at real speed for 10-15 seconds.** This is analytical IK computing 18 joint angles per tick at 20Hz, with stance/swing foot trajectories, in rigid-body physics. Not animation.

**Optional:** Switch to keyboard teleop (`scripts/isaac_keyboard_teleop.py`) for live WASD control.

---

## Timing Summary

| Segment | Live | Edited | Notes |
|---------|------|--------|-------|
| Act 1: Gears | 3-4 min | 45s | Keep involute sketch + final assembly at 1x |
| Beat 1: Research | 2 min | 10s | Speed 8x, brief at 1x |
| Beat 2: Chassis | 2 min | 15s | Disc, pattern, fillet at 1x |
| Beat 3: Legs | 3 min | 20s | Final pose at 1x |
| Beat 4: Servos | 2 min | 10s | Hero shot at 1x |
| Beat 5-6: Validate+Export | 1.5 min | 13s | Speed through |
| **Beat 7: Isaac Sim** | **3 min** | **30s** | **Speed import, WALKING AT 1x** |
| **Total** | ~17 min | **~2.5 min** | |

---

## What if the LLM picks bad specs?

| LLM proposes... | You say... |
|-----------------|------------|
| Wrong leg lengths | *"Use coxa 52mm, femur 66mm, tibia 133mm"* |
| Wrong servo model | *"Use AX-12A dimensions: 32×24×24mm"* |
| Circular chassis | *"Use a rectangular plate ~120×90mm — cleaner body_length/body_width for the controller"* |
| Skips research | *"Stop. Research the specs first and tell me your plan."* |
| 1-DOF instead of 18 | *"I want 3 joints per leg: hip yaw, hip pitch, knee"* |

---

## Fallback

| Problem | Recovery |
|---------|----------|
| Gears fail to generate | Skip Act 1 — go straight to hexapod |
| LLM picks wrong geometry | Steer: *"That's wrong, here's what I want..."* |
| FreeCAD disconnected | *"Reconnect to FreeCAD"* (LLM retries TCP) |
| Isaac import hangs | Cut, restart bridge, re-record beat 7 |
| Hexapod doesn't walk | *"Send a walk command: vx 0.3 m/s"* |
| Need a clean restart | Close doc in FreeCAD, start from beat 1 |

---

## Post-Production Notes

- **Speed up:** Research/web searches, LLM thinking, import waits. Speed 4-8x.
- **Real speed:** Spec summary (beat 1), geometry appearing (beats 2-4), **walking (beat 7)**.
- **Key visual moments at 1x:**
  - Act 1: Involute sketch, gear assembly hero shot
  - Beat 2: Servo pockets cut into chassis
  - Beat 3: Leg structure appears
  - Beat 4: Full 37-body hexapod hero shot
  - Beat 7: Walking starts
- **Text overlays:**
  - Act 1: *"Mathematically correct involute profiles"*
  - Beat 2: *"PartDesign features, not just boxes"*
  - Beat 5: *"18-DOF kinematic validation"*
  - Beat 7: *"IK-based tripod gait in rigid-body physics"*
- **Thumbnail:** Split screen — typed prompt on left, walking hexapod on right.
- **Music:** Optional. Lo-fi or synth. Quiet — visuals carry it.

---

## Quick Reference

```bash
# FreeCAD addon
import freecad_addon; freecad_addon.start()

# Isaac bridge
ISAAC_PYTHON=./isaacsim/_build/linux-x86_64/release/python.sh scripts/run_isaac_bridge.sh

# Pre-demo verification
python3 scripts/demo_preflight.py --full
python3 scripts/demo_build_hexapod_18dof.py --fast

# Print mechanism/profile JSON (for debugging)
python3 scripts/demo_build_hexapod_18dof.py --print-mechanism
python3 scripts/demo_build_hexapod_18dof.py --print-profile

# Keyboard teleop (after teleop session starts)
python3 scripts/isaac_keyboard_teleop.py
```

---

## Pre-Recording Checklist

```bash
# 1. Gear engine
python3 -c "import solidmind_geometry; print('Gear engine OK')"

# 2. Full preflight
python3 scripts/demo_preflight.py --full

# 3. Dry run 18-DOF build (optional)
python3 scripts/demo_build_hexapod_18dof.py --fast

# 4. Verify mechanism JSON
python3 scripts/demo_build_hexapod_18dof.py --print-mechanism | python3 -c "import json,sys; m=json.load(sys.stdin); print(f'{len(m[\"parts\"])} parts, {len(m[\"joints\"])} joints')"
```
