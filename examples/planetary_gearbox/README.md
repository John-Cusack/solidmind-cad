# Planetary Gearbox 5:1 — End-to-End Orchestrator Demo

The flagship v0.2.0 demo. Drives the outer orchestrator loop end-to-end on a real **multi-part assembly** — five gear-family workers in one run, each producing real FreeCAD geometry, all independently re-measured by the orchestrator after build.

```
[1/4] G0 → G3 gate walk           ✓ specification + skeleton + ICDs frozen
[2/4] Dispatch 6 worker builds    ✓ sun_gear, planet × 3, planet_carrier, ring_gear
[3/4] G4 (artifact check)         ✓ 6 STEP files, 6 STLs, 6 metadata.jsons present
[4/4] G5 (verify-mode validation) ✓ orchestrator re-imports every STEP, re-measures
                                    every checkpoint, all 6 parts pass

Result: ALL PARTS PASS
Speed ratio (closed-form, ring fixed): 5.00:1
Built in ~30s.
```

## What this proves

Each of the four chunks-5–9 builders ([`sun_gear`](../../orchestrator/worker_builds/sun_gear.py), [`planet_carrier`](../../orchestrator/worker_builds/planet_carrier.py), [`ring_gear`](../../orchestrator/worker_builds/ring_gear.py), the [hexapod / quadrotor / chassis builders](../../orchestrator/worker_builds/)) was already exercised in isolation by [`tests/test_orchestrator_real_worker_e2e.py`](../../tests/test_orchestrator_real_worker_e2e.py). This example is what they were _for_: the same orchestrator coordinating multiple workers to produce a coherent assembly that satisfies an interface contract.

The cross-cutting validation:

- **Sun + planets share a module** (1.0 mm) and **gear-mesh** at the right pitch radii. The Rust geometry kernel (`solidmind_geometry.planetary_layout`) enforces the assembly condition `(sun_teeth + ring_teeth) mod num_planets == 0`.
- **Carrier pin-circle diameter** = `2 × orbit_radius` = `sun_pitch + planet_pitch` = 30.000 mm. The orchestrator's `_measure_pin_circle_diameter` strategy verifies this from the planet positions in the produced STEP — measured 30.000022 mm.
- **Ring root circle** = pitch + 2·dedendum = 48 + 2·1.25 = 50.5 mm (internal teeth sit outward of the pitch). Verified to ±0.5 mm.
- **Speed ratio** = `1 + ring_teeth / sun_teeth` = `1 + 48/12` = **5:1** (ring fixed, sun input, carrier output).

## How to run

Prerequisites:

```bash
# 1. FreeCAD addon installed and running on :9876
scripts/install_freecad_addon.sh && open -a FreeCAD

# 2. Rust geometry kernel built
maturin develop --manifest-path geometry/Cargo.toml
```

Run from the repo root:

```bash
PYTHONPATH=. python3 examples/planetary_gearbox/run.py --out /tmp/gearbox
```

Output (in `/tmp/gearbox`):

```
run/
  spec.yaml                  # frozen MasterSpec — 6 subsystems, 6 interfaces
  state.json                 # G0 → G5 transition history
  sun_gear_0/output/         # sun_gear.step, sun_gear.stl, metadata.json
  planet_1_0/output/         # planet 1 (at orbit position +15, 0)
  planet_2_0/output/         # planet 2 (at +120°)
  planet_3_0/output/         # planet 3 (at +240°)
  planet_carrier_0/output/   # 36-mm disc, central bore, 3 pin bosses on PCD=30
  ring_gear_0/output/        # 48-tooth internal gear, polar pattern
orchestrator_report.txt      # per-part pass/fail summary
```

The five STEP files (≈5.6 MB total) can be imported into FreeCAD, OpenSCAD, KiCad, or any other CAD tool — they're plain ISO-10303 with no engine-specific encoding.

## What the orchestrator does for you

Without this scaffolding, an LLM driving FreeCAD has no way to know whether the part it just built actually meets the spec. It saw the screenshot; it didn't measure. The pattern in this demo:

1. **Spec freeze** — `MasterSpec` is the contract. Six subsystems, six interfaces, dimensional `ValidationCheckPoint`s on each. Once gates G0–G3 pass, no part of the spec moves until release.
2. **Worker dispatch** — each subsystem gets its own builder run. Workers are isolated; their only outputs are STEP + STL + metadata.json. No shared state.
3. **Self-verifying re-measurement** — `orchestrator/measure.py` reimports every STEP independently of the addon's live session. It re-runs `find_holes`, `cad_get_dimensions`, etc. The numbers in the validator come from this fresh re-measurement, not from anything the worker claimed.
4. **Drift detection** — if a worker lies (or a subtle build bug shifts a dimension), the orchestrator catches it. See [`tests/test_orchestrator_drift_e2e.py`](../../tests/test_orchestrator_drift_e2e.py) for the deliberate-drift case.

## Hyperparameters

The numbers in `run.py` were picked so the assembly condition holds and the ratio is clean:

| Parameter | Value | Constraint |
|---|---|---|
| Module | 1.0 mm | All gears must share |
| Sun teeth | 12 | Input |
| Planet teeth | 18 | `(ring + sun) ÷ num_planets` must be integer |
| Ring teeth | 48 | `= sun + 2 × planet` |
| Num planets | 3 | `(48 + 12) ÷ 3 = 20` ✓ |
| Speed ratio | 5:1 | `1 + ring/sun` (ring fixed) |
| Orbit radius | 15 mm | `(sun_pitch + planet_pitch) / 2` |
| Pin-circle diameter (PCD) | 30 mm | `2 × orbit_radius` |

To produce a different ratio, vary `SUN_TEETH` and `PLANET_TEETH` in `run.py` — the planetary_layout call will reject combinations that violate the assembly condition with a clear error.

## Where this fits in the v0.2.0 release

This example is the **multi-part** counterpart to the per-class verify-mode tests in `tests/test_orchestrator_real_worker_e2e.py`. Those prove individual builders work; this one proves they compose.

The natural follow-on is a 60-second screen recording of:

1. `python3 examples/planetary_gearbox/run.py` running in a terminal
2. FreeCAD on screen with the parts appearing live
3. The validator output ticking through "✓" lines
4. A final shot of the assembly (parts placed at the right positions in a Part::Compound)

That recording becomes the README hero, the GitHub Release attachment, and the HN top-of-post asset.
