# Engines

Simulation engines are **applications**, not libraries. Each lives in its own
repository, runs on its own interpreter, and talks to core over one published
contract: [`docs/engine-contract.md`](engine-contract.md).

Core knows an engine exists because a descriptor file says so — not because
anyone edited core.

## Available engines

| Engine | Repository | Good for |
|---|---|---|
| **reference** | ships with core (`reference_engine/`) | Always available, no install. Analytic gear trains, pendulums, free fall — check a pipeline end to end before reaching for a real engine. |
| **chrono** | `solidmind-engine-chrono` | Gear trains, linkages, springs. Analytic multibody dynamics, batch only, no GPU. |
| **gazebo** | `solidmind-engine-gazebo` | Drones (PX4 SITL), wheeled vehicles, CPU-only hosts. 5-DOF teleop. |
| **isaac** | `solidmind-engine-isaac` | Legged robots, articulated arms. GPU contact physics, sessions and teleop. |

Related: **`solidmind-rl`** — the Isaac Lab + RSL-RL training pipeline. Not an
engine (it speaks no contract); core drives it as a subprocess.

`sim.engine_status` reports which are installed and running, with an install
hint for each that isn't.

## Adding your own

No fork, no pull request, no permission:

1. **Implement four verbs** — `hello`, `ping`, `simulate`, `shutdown` — over
   newline-delimited JSON on a TCP port. Any language.
   [`reference_engine/`](../reference_engine/) is a complete worked example;
   clone it and swap the physics.
2. **Read two file formats** — the sim package (`manifest.json` + meshes) and,
   if you want it, the courtesy URDF. Both are documented in the contract §6.
3. **Prove it** — `python3 -m tck --port <yours>`. Exit 0 means conformant.
   See [`tck/README.md`](../tck/README.md).
4. **Drop a descriptor** into `~/.solidmind/engines.d/`:

   ```toml
   name = "mujoco"
   port = 9899
   launch = ["python3", "-m", "mj_bridge.server", "--port", "${PORT}"]
   cwd = "~/repos/solidmind-engine-mujoco"
   install_hint = "pip install mujoco, then: sim.start_engine('mujoco')"
   when_to_use = "Contact-rich manipulation; fast MJCF scenes."
   ```

That last field matters more than it looks: `when_to_use` flows into the
prompts the copilot reads, so your engine becomes *recommendable*, not just
callable. An engine nobody knows when to pick is only half integrated.

**Partial engines are first-class.** A batch-only engine with no teleop, no
sessions and no package ingest is a valid engine — the TCK reports skips, not
failures, and core gates every optional verb on what your handshake advertises.

## What core promises you

Within contract v1.x: additive changes only, the four-verb floor never grows,
deprecations get a minor version of warning, and the TCK never breaks. If an
engine passes v1.0 it passes v1.x (contract §10).

## What core will not do

- Emit your dialect. Core writes the canonical package, meshes and a URDF;
  SDF, MJCF, USD and autopilot parameters are compiled on your side, at load
  time.
- Sit inside your control loop. Anything that needs per-timestep decisions runs
  in your process — core orchestrates at batch cadence only.
- Introduce your engine to another one. Engines conform to the contract, never
  to each other; composition is core's job, expressed as data.
