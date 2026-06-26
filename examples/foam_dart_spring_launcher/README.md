# Foam-Dart Spring Launcher — sim-to-real validation rig

A 3D-printable, fixed-angle, single-shot spring-plunger foam-dart launcher,
built as a **rigor demo**: the number the analytical model predicts, the number
the Chrono dynamic sim produces, and the number you measure off the printed rig
must agree within a stated tolerance. When they don't, the report says so — the
whole point is to surface disagreement, not hide it.

It is also the first example that walks the full **nine-step inner loop**
end to end and *closes the autonomous iteration test* for a real part class.

```
PYTHONPATH=. python3 examples/foam_dart_spring_launcher/run.py \
    --out /tmp/foam_dart_spring_launcher_run
```

## The nine-step loop, on one latch

The launcher's printed latch tooth is deliberately under-dimensioned in **V1**
(thin root, sharp corner). The loop catches it and fixes it — with no human in
between the diagnosis and the re-check:

| Step | What happens here |
| --- | --- |
| **Specify** | Load the committed `design_brief.json` (parts, interfaces, layout). |
| **Synthesize** | Drive FreeCAD → real STEP per part (skipped if the addon isn't running). |
| **Reflect** | File a `ReflectExpectations` per part class from `failure_modes.yaml`. |
| **Screen** | `analysis.screen_stress` — beam theory + stress-concentration factor. V1 latch **FAILs** on `stress_concentration`, no FEA needed. |
| **Simulate** | CalculiX FEA on any *marginal* part; **Chrono** for the spring-plunger dynamics. |
| **Interpret** | Compare to expectations, classify with a typed `FailureMode`. |
| **Decide** | `decide.from_failure` → "add a root fillet". |
| **Act** | Thicken the root + add the fillet, **re-screen → PASS**. |
| **Learn** | Record the finding (`knowledge.*` if available, else a note file). |

The V1→V2 improvement is real: peak latch stress drops from ~68 MPa (FAIL) to
~6 MPa (PASS).

## The sim-to-real chain

```
(1) analytical plunger velocity  ── physics_model, lossless: sqrt(k·x²/m_plunger)
(2) Chrono plunger exit velocity ── real MBS run (spring on a prismatic)
(3) measured range               ── you fire one shot and fill it in
```

- **(1) ↔ (2)** is a *toolchain* check: the lossless head-to-head validates the
  energy core of `physics_model.py` against the physics engine. On a real run
  these agree to ~0%.
- **(3)** is the *reality* check. The **dart** muzzle velocity folds in a single
  lumped `efficiency` that absorbs spring mass, plunger friction, the air column,
  and barrel losses — so a predicted-vs-measured *range* gap is a **calibration**
  result, not a model failure.

### Calibration-first (the headline)

Don't trust the absolute range out of the box — trust the *relationship*. With
efficiency fixed, `v ∝ x` and (no-drag limit) `range ∝ x²`. Fire one shot,
then:

```
... run.py --calibrate-from-shot 20 4.5     # measured 4.5 m at the 20 mm notch
```

The model fits `efficiency` from that one shot and predicts the other two
pullbacks, reporting **relative** error. If the implied efficiency lands outside
`(0, 1]`, the run errors out and tells you the spring constant or dart mass
disagrees with reality — exactly the kind of surprise this rig exists to catch.

## Physics model

```
E_spring = 1/2 k x^2
E_dart   = efficiency * E_spring
v        = sqrt(2 E_dart / m_dart)
range    = projectile_range(v, angle, launch_height)     # no-drag, from a height
```

`physics_model.py` is pure and independently tested
(`tests/test_foam_dart_physics.py`).

## What's real vs what's skipped

The **real path is the default**. Each backend that isn't installed is reported
as `SKIPPED` — never faked.

| Step | Backend | Runs when |
| --- | --- | --- |
| Synthesize → STEP | FreeCAD addon on :9876 | addon is launched |
| Screen | none (analytical) | always |
| Simulate (structural) | CalculiX + gmsh | installed and a part is marginal |
| Simulate (dynamic) | Chrono daemon | `chrono_daemon` is built |
| Learn | LanceDB | knowledge store available |

`--smoke` runs a **no-solver CI path** and prints a loud
`PHYSICS NOT VALIDATED — smoke mode` banner; it never emits predicted-vs-actual
numbers as if they were real.

### Installing the real backends

- **FreeCAD**: launch with the addon (`scripts/install_freecad_addon.sh`), then
  the Synthesize step builds real STEP.
- **CalculiX + gmsh**: `apt install calculix-ccx` and `pip install gmsh`.
- **Chrono daemon**: build `chrono_daemon/` (see its README); the dynamic step
  then runs for real.

## Flags

```
--out PATH                       output dir (default /tmp/foam_dart_spring_launcher_run)
--spring-k-n-m FLOAT             spring constant (MEASURE yours)
--dart-mass-g FLOAT              dart mass (default 1.0 g placeholder)
--angle-deg FLOAT                launch angle (default 12)
--efficiency FLOAT               starting efficiency before calibration
--material pla|petg              print material
--calibrate-from-shot MM M       fit efficiency from one measured shot
--smoke                          CI-only no-solver path
```

## Outputs

```
launcher_v1/  launcher_v2/       per-version artifacts (incl. the V2 finding)
step/  stl/                      generated geometry (real path only; not committed)
range_prediction.csv             per-pullback velocity + predicted/actual range
motion_trace.csv                 plunger position/speed vs time (Chrono or analytical)
validation_report.md             the human-readable, video-friendly report
bom.json                         bill of materials with hardware assumptions
```

Committed text fixtures live in `expected_outputs/` (`*.sample.*`). No binary
artifacts are committed — STL/STEP are generated to the output dir only.

## Print / test

1. Print all custom parts in PLA (or PETG), no supports, flat-on-bed faces, M3
   screws or printed pins.
2. Fit an off-the-shelf compression spring and **measure its constant**.
3. Weigh the dart.
4. Fire one shot at a known pullback, measure the range, and feed it to
   `--calibrate-from-shot`.
