# Sim Engine Policy

Simulation engines (Chrono, Gazebo, Isaac) are managed via `sim.*` tools. They run as subprocesses with TCP bridges.

## Lifecycle Rules

- **Start on demand** — only start an engine when a motion/analysis tool needs it.
- **Reuse across runs** — once started, keep the engine running for subsequent validation runs. Don't stop/restart between iterations.
- **Graceful shutdown** — use `sim.stop_engine` when the user is done with simulation, or let engines drain naturally.
- **Health monitoring** — `sim.engine_status` reports health of all backends. Use it to diagnose connection issues before retrying operations.

## Backend Selection

| Robot Type | Backend | Why |
|-----------|---------|-----|
| Drone / multirotor | `gazebo` | PX4 ecosystem, 5-DOF teleop |
| Wheeled vehicle | `gazebo` | ROS ecosystem, lateral velocity |
| Legged robot / hexapod | `isaac` | GPU contact, tripod controller |
| Articulated arm | `isaac` | GPU physics, joint-level control |
| Gear train / linkage | `chrono` | Analytical MBS, batch validation |
| CPU-only fallback | `gazebo` | No GPU required |

## Port Defaults

| Backend | Default Port | Env Override |
|---------|-------------|-------------|
| Chrono | 9877 | `SOLIDMIND_CHRONO_PORT` |
| Isaac | 9878 | `SOLIDMIND_ISAAC_PORT` |
| Gazebo | 9879 | `SOLIDMIND_GAZEBO_PORT` |

Host: `127.0.0.1` (override: `SOLIDMIND_SIM_HOST`)

## Stub vs Real Mode

- **Stub mode** (`runtime='stub'`): In-memory simulation, no real engine needed. Good for testing tool integration.
- **Real mode** (`runtime='real'`): Requires actual engine installation (Gazebo Harmonic, Isaac Sim, Chrono build).

Default to stub mode unless the user has a real engine installed and needs physics fidelity.

## Error Recovery

- If an engine crashes, `sim.engine_status` will report it as `failed` with the error.
- Don't auto-restart — report the failure and let the user decide.
- For repeated crashes, check logs and suggest the user verify the installation.
