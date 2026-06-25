# Hexapod Robot — 7-Worker Orchestrator Demo

The biggest end-to-end build the v0.2.0 outer loop has run. Seven worker dispatches in a single orchestrator pass produce a complete six-legged robot's CAD: 1 chassis + 6 multi-segment legs, **18 revolute-joint pivot bores** when assembled.

```
[1/4] G0 → G3 gate walk           ✓ 7-subsystem spec frozen
[2/4] Dispatch 7 worker builds    ✓ chassis + leg × 6
[3/4] G4 (artifact check)         ✓ 7 STEPs, 7 STLs, 7 metadata.jsons
[4/4] G5 (verify-mode validation) ✓ 20 checkpoints across 7 parts
                                    all measured by orchestrator-side
                                    re-import (not by the workers)

Result: ALL PARTS PASS
Total leg DOF when assembled: 18
Built in ~30s.
```

## What this proves over the planetary gearbox demo

- **Two distinct build paths in one run**: chassis routes through `_build_envelope` (rectangular plate + 7 ThroughAll pockets), legs route through the new `_build_leg` (3 fused rect pads + 3 distinct pivot bores). The orchestrator coordinates both without caring which path each subsystem takes.
- **Heterogeneous measurement strategies**: chassis verifies a `motor_mount_pcd` (measured 110.0000032 mm vs spec 110.0); each leg verifies 3 distinct bore diameters (4 / 5 / 6 mm) picked out via `expected_mm` hints.
- **Larger interface graph**: 7 subsystems × ~3 interfaces each = 20 dimension checkpoints, all independently re-measured. The two earlier demos (single-class verify-mode tests, the 5-part planetary gearbox) topped out at 6 parts / ~6 checkpoints.
- **Same builder used 6 times for 6 legs**: each leg is its own subsystem with its own `worker_count=1`, dispatched independently. The `assembly_constraints={"datum": "body_origin"}` in every subsystem satisfies G2 skeleton attachment for all 7.

## How to run

Prerequisites identical to `examples/planetary_gearbox/`:

```bash
scripts/install_freecad_addon.sh && open -a FreeCAD
maturin develop --manifest-path geometry/Cargo.toml
```

Run from the repo root:

```bash
PYTHONPATH=. python3 examples/hexapod_robot/run.py --out /tmp/hexapod
```

Output (in `/tmp/hexapod`):

```
run/
  spec.yaml                       # 7-subsystem MasterSpec
  state.json                      # G0 → G5 transition history
  hexapod_chassis_0/output/       # 150×150×5 plate, central bore + 6 mount holes
  leg_1_0/output/                 # leg at chassis position +55, 0 (angle=0°)
  leg_2_0/output/                 # +27.5, +47.6 (angle=60°)
  leg_3_0/output/                 # -27.5, +47.6 (angle=120°)
  leg_4_0/output/                 # -55.0, 0 (angle=180°)
  leg_5_0/output/                 # -27.5, -47.6 (angle=240°)
  leg_6_0/output/                 # +27.5, -47.6 (angle=300°)
orchestrator_report.txt           # per-part pass/fail summary
```

Each `*/output/` directory contains a STEP file + STL + metadata.json.

## Why each leg is built at origin (not in place)

Workers produce STEPs in their own coordinate frames; the **assembly step** is what places parts in world space. This is the same convention `examples/planetary_gearbox/` uses for its 3 planet gears, and it's what FreeCAD's Assembly workbench expects — link bodies into an Assembly container, place each link, add joint constraints between them. None of that happens in this example; the seven STEPs are the substrate for whatever assembly path you pick downstream.

The leg mount positions (`+55, 0`; `+27.5, +47.6`; ...) are still printed during the build because they're the chassis-side positions that the eventual leg-to-chassis fixed joints will reference. The hexapod_leg builder produces all 6 legs as identical geometry (the chassis-side asymmetry is in the assembly, not the leg STEP).

## Where this fits in the v0.2.0 release

Two flagship orchestrator examples in the repo now:

| | `planetary_gearbox/` | `hexapod_robot/` (here) |
|---|---|---|
| Parts built | 6 (sun + planets + carrier + ring) | 7 (chassis + 6 legs) |
| Distinct build types | 3 (`gear`, `carrier`, `ring_gear`) | 2 (`envelope`, `leg`) |
| Measurement strategies exercised | bore + PCD | bore + PCD |
| Closed-form verifiable spec | speed ratio = 5:1 | DOF = 18 |
| Demo angle | "Watch the LLM design a gearbox" | "Watch the LLM design a 6-legged robot" |

## What's still open (v0.3.0+ follow-ups)

The CAD pipeline is closed. The assembly + simulation + RL pipeline beyond it is in the repo but not yet wired into a fresh orchestrator-built robot:

- **Assembly**: `motion.create_assembly` + `motion.add_joint` would turn the 7 STEPs into one `Assembly` doc with `App::Link`s placed at the leg angles and 18 revolute joints connecting the segments. The recent diagnostics improvement (commit `7e8fea1`) means it'll now fail with a helpful "build the bodies first" message if you try to call it before the orchestrator finishes.
- **URDF export**: `cad.export_sim_package` walks the Assembly to produce a URDF + STL meshes. Tested and working on the existing v3 hexapod (per `hexapod_18dof_v3_pkg/Hexapod_18DOF.urdf`).
- **Isaac Sim integration**: `motion.simulate(backend="isaac")` drops the URDF into Isaac Sim. The bridge is already running per the existing `scripts/run_isaac_bridge.sh`.
- **RL training**: `rl_training/isaaclab_train.py` trains a walking policy with PPO. ~4 minutes for a 50-iteration smoke test on an RTX 3090.

Each of those is its own ~half-day of integration work because the orchestrator-built 7-body robot has different link names / joint origins than the existing v3 19-body model. The proven build pipeline this example demonstrates is the foundation for all of it.

## Sample output

See `expected_outputs/orchestrator_report.sample.txt` for a captured run.
