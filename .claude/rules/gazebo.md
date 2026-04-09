---
paths:
  - "gazebo_bridge/**"
  - "scripts/run_gazebo_bridge.sh"
---
# Gazebo Bridge

TCP sidecar on localhost:9879 for Tier 3 `backend=gazebo` simulation and teleop.

## Key Details

- Start with `scripts/run_gazebo_bridge.sh` (optional `--launch-gz` flag)
- Same newline-delimited JSON protocol as Isaac bridge
- Best for: drones (PX4 SITL), wheeled vehicles, CPU-only environments
- Teleop 5-DOF: `vx_mps`, `vy_mps`, `vz_mps`, `yaw_rate_rps`, `body_height_m`
  (vs Isaac's 3-DOF which lacks vy_mps and vz_mps)

## Module Structure

- `bridge_server.py` — TCP server (no main-thread pump needed)
- `runtime_gazebo.py` — Stub command handlers, session tracking
- `models.py` — GazeboConfig (frozen), GazeboSession (mutable, 5-DOF teleop)
