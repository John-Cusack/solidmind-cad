---
paths:
  - "isaac_bridge/**"
  - "scripts/run_isaac_bridge.sh"
  - "scripts/smoke_test_isaac.py"
  - "scripts/isaac_keyboard_teleop.py"
---
# Isaac Sim Bridge

TCP sidecar on localhost:9878 for Tier 3 `backend=isaac` simulation and teleop.

## Key Details

- Start with `scripts/run_isaac_bridge.sh`
- Newline-delimited JSON protocol (ping, simulate, teleop_*)
- Supported joints: revolute, prismatic, fixed
- Teleop: `Controller` protocol, currently `HexapodTripodController` (1-DOF tripod gait)
- Teleop DOF: `vx_mps`, `yaw_rate_rps`, `body_height_m`
- Kit event loop MUST be pumped on main thread — TCP runs in background thread
- Joint counting: use `prim.GetTypeName()`, NOT `prim.HasAPI()`
- Isaac Sim from source: `ISAAC_PYTHON=./isaacsim/_build/linux-x86_64/release/python.sh`

## Module Structure

- `bridge_server.py` — TCP server + main-thread pump loop
- `runtime_isaac.py` — URDF import, simulation, teleop lifecycle, DOF mapping
- `models.py` — TeleopConfig, TeleopState, Controller protocol, SimulationSession
- `controllers.py` — HexapodTripodController, PolicyController, create_controller() registry
- `keyboard_teleop.py` — KeyboardTeleopMapper (no Isaac dependency)
