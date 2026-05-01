# Recording prompt — long-endurance camera drone

This is the operator's guide for recording the drone demo. The actual
prompts live in [`prompts/`](prompts/) — one file per stage. Don't
paste this guide into Claude Code; paste each numbered file as you go.

## How to use

1. Have FreeCAD running with the SolidMind addon (port 9876).
2. Have PX4 SITL built once (`cd ~/repos/PX4-Autopilot && make px4_sitl gz_x500`).
3. Open a Claude Code session in this repo.
4. **Paste the contents of `prompts/01_sizing.md`** into the prompt box. Send.
5. Wait for the LLM to finish + present its design brief gate.
   Approve, push back, or course-correct.
6. **Paste `prompts/02_airframe.md`**. Watch FreeCAD build the bodies.
7. Approve the visual gate.
8. **Paste `prompts/03_optimize.md`**. Watch the BEMT sweep run.
9. Approve the winning rotor.
10. **Paste `prompts/04_build_prop.md`**. Watch the prop loft into FreeCAD.
11. Approve the beauty shot.
12. **Paste `prompts/05_fly.md`**. Watch PX4 launch + the drone hover in Gazebo.

Each file is a single self-contained directive. The LLM treats it as
the user's request and executes immediately — no scaffolding text to
confuse the parser.

## The five prompts

| File | What it does | Visible artifact |
|---|---|---|
| [`prompts/01_sizing.md`](prompts/01_sizing.md) | Design brief: AUW budget, motor pick, prop diameter range | Part list with masses |
| [`prompts/02_airframe.md`](prompts/02_airframe.md) | Build chassis + 4 arms + motor mounts + payload + battery in FreeCAD | Drone airframe in FreeCAD viewport |
| [`prompts/03_optimize.md`](prompts/03_optimize.md) | BEMT sweep over (diameter, blades, chord, twist) for hover time | Ranked table of top-5 prop variants |
| [`prompts/04_build_prop.md`](prompts/04_build_prop.md) | Build the winning prop via `geometry.propeller_blade` + loft + polar pattern | Drone with real cinema-style props |
| [`prompts/05_fly.md`](prompts/05_fly.md) | Export sim package + PX4 airframe, launch PX4 + Gazebo, takeoff + hover + land | Drone visibly flying in Gazebo |

## Pre-recording dry run

Before the real take, verify the pipeline works on this machine:

```bash
python scripts/record_drone_demo_dryrun.py --skip-px4-rebuild
```

This walks the same five stages programmatically with shrunken
parameters (3 m takeoff, 5 s hover) and times each one. If it
finishes green in ~5 minutes, the recording session is unblocked.

## What success looks like

| Metric | Target |
|---|---|
| Total wall-clock | 15-25 min (sped 8× in the cut → ~2-3 min) |
| AUW | 5.5-6.5 kg |
| Winning prop | ~20-22" diameter, 3 blades, optimized twist/chord |
| Hover time (BEMT prediction) | 25-35 min |
| FoM (figure of merit) for the winning rotor | 0.70-0.80 |
| Hover-time improvement vs. baseline 18" 2-blade | 15-25% |
| Flight in Gazebo | Lifts to 5 m, holds 30 s, lands cleanly |

## Recording-day notes

- Save `.FCStd` checkpoints between stages so post can re-render
  beauty shots without rerunning the build.
- Keep `study.results` JSON output — it drives the on-screen
  optimization plot in post.
- Each stage is a separate Claude Code prompt. **Don't paste the next
  stage until the previous gate is visually confirmed in FreeCAD or
  Gazebo.**

## See also

- [`run.py`](run.py) — same pipeline as a standalone script (no LLM)
- [`README.md`](README.md) — operator-facing run instructions for `run.py`
- [`docs/px4_integration.md`](../../docs/px4_integration.md) — the
  verified PX4 v1.17 takeoff sequence the bridge uses
- [`server/px4_airframe_generator.py`](../../server/px4_airframe_generator.py)
  — the airframe-from-geometry generator
