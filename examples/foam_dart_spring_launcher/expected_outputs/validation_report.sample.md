# Foam-Dart Spring Launcher — Validation Report

> **PHYSICS NOT VALIDATED — smoke mode.** No solvers were run; the numbers below are analytical placeholders for CI plumbing only.

## Project summary

A 3D-printable, fixed-angle, single-shot spring-plunger foam-dart launcher used as a sim-to-real validation rig. The analytical model, the Chrono dynamic sim, and the measured range must agree within tolerance after one-shot calibration.

## Assumptions

- Material: **pla** (yield used in screening).
- Spring constant: **300 N/m** (PLACEHOLDER — measure your spring)
- Dart mass: **1.00 g** (default placeholder)
- Launch angle: **12°**, launch height **0.15 m**.
- Efficiency: **0.782** (CALIBRATED from your measured shot)
- Full-cock spring hold force: **9.0 N** (k × 30 mm).

## Sim-to-real chain

**Spring → plunger energy delivery** (Chrono validates physics_model):

| Quantity | Value | Notes |
| --- | ---: | --- |
| (1) Analytical plunger velocity (lossless) | 4.743 m/s | sqrt(k·x²/m_plunger), efficiency=1 |
| (2) Chrono plunger exit velocity | SKIPPED | daemon not built |

The lossless head-to-head validates the energy core of physics_model against the MBS engine. The **dart** muzzle velocity (below) then folds in the lumped efficiency — which absorbs spring mass, plunger friction, the air column, and barrel losses — so a predicted-vs-measured *range* gap is a calibration result, not a model failure. Calibrate efficiency from one measured shot (`--calibrate-from-shot`) and the relationship holds: with efficiency fixed, v∝x and (no-drag) range∝x².

## Predicted ranges (fill in your measurements)

| Pullback | Muzzle v (real) | Predicted range | Actual range | Error |
| ---: | ---: | ---: | ---: | ---: |
| 10 mm | 4.84 m/s | 1.45 m (4.7 ft) | user fills | user fills |
| 20 mm | 9.68 m/s | 4.50 m (14.8 ft) | 4.5 | 0.0% |
| 30 mm | 14.53 m/s | 9.41 m (30.9 ft) | user fills | user fills |

## Inner-loop trace (nine steps)

- `[Specify] loaded committed brief 'Foam-Dart Spring Launcher' (11 parts, 6 interfaces)`
- `[Synthesize] SKIPPED (smoke mode — no geometry)`
- `[Reflect] filed expectations for 3 part classes (latch hotspot=tooth_root)`
- `[Screen] V1 latch=fail spring_seat=pass plunger_rod=pass`
- `[Simulate] latch screen FAIL is definitive — no FEA needed to reject V1`
- `[Simulate] Chrono SKIPPED (smoke)`
- `[Interpret] stress_concentration; hotspot as expected; peak 67.5 MPa outside band 15-60; mode stress_concentration was in the checklist`
- `[Decide] add_fillet → add or enlarge the fillet/round at the hotspot to lower Kt`
- `[Act] V2 latch re-screen → pass (peak 68 → 6 MPa)`
- `[Learn] finding written to <out>/launcher_v2/finding.md (knowledge store unavailable)`

## Structural checks (V1 → V2)

| Check | Target | V1 | V2 |
| --- | ---: | ---: | ---: |
| Latch tooth (FoS basis) | > 2.0 | FAIL (peak 68 MPa) | PASS (peak 6 MPa) |
| Spring seat | > 2.0 | PASS | PASS |
| Plunger rod (buckling) | > 2.0 | PASS | PASS |

## V1 failure → V2 fix

- **V1 failure:** latch screen → `fail` / `stress_concentration` — sigma_nom=22.5 MPa, Kt=3.00, peak=67.5 MPa, FoS=0.89 vs target 2.0
- **Interpret:** hotspot as expected; peak 67.5 MPa outside band 15-60; mode stress_concentration was in the checklist
- **Decide:** add_fillet at `latch tooth_root` (radius_mm += 0.5) — add or enlarge the fillet/round at the hotspot to lower Kt
- **Act → V2:** re-screen → `pass` (peak 68 → 6 MPa).

## Print / test instructions

1. Print all custom parts in PLA (or PETG), no supports, flat-on-bed faces.
2. Fit the off-the-shelf compression spring; **measure its constant** and re-run with `--spring-k-n-m <value>`.
3. Weigh the dart; re-run with `--dart-mass-g <value>`.
4. Fire one shot at a known pullback, measure the range, then run `--calibrate-from-shot <pullback_mm> <range_m>` to fit efficiency and predict the rest.
