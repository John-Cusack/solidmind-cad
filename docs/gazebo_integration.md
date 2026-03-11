# Gazebo Simulator Integration

Research and implementation plan for adding Gazebo support to SolidMind CAD,
with emphasis on drone/UAV simulation via PX4 SITL.

## 1. Why Gazebo?

SolidMind CAD currently supports NVIDIA Isaac Sim as the primary Tier 3
dynamic simulation backend. Gazebo fills a different niche:

| Aspect | Gazebo (Harmonic/Jetty) | NVIDIA Isaac Sim |
|--------|------------------------|------------------|
| **Hardware** | Any CPU (no GPU required) | NVIDIA GPU required |
| **License** | Open source, free | Open-sourced in 2025, free |
| **Physics** | Pluggable: DART, Bullet, TPE | PhysX (GPU-accelerated) |
| **Rendering** | Ogre2 (functional, not photorealistic) | RTX ray tracing (photorealistic) |
| **Drone ecosystem** | PX4 SITL + ArduPilot native | No native autopilot integration |
| **Sensors** | IMU, GPS, baro, mag, camera, LIDAR | Camera, LIDAR (stronger for vision/ML) |
| **ROS integration** | First-class (the standard ROS sim) | ROS 2 bridge extension |
| **Model format** | SDF native; auto-converts URDF | USD native; imports URDF |
| **Multi-vehicle** | Native (N vehicles, unique namespaces) | Possible but not primary use |
| **Communication** | gz-transport (pub/sub + services) | Kit event loop + USD APIs |
| **Best for** | Drones, ground robots, ROS workflows | AI/ML, photorealistic vision, GPU physics |
| **Vision accuracy** | Overestimates prediction accuracy | Matches reality better for CV tasks |

**Bottom line:** Isaac excels at legged robots, AI/vision, and contact-heavy
scenarios. Gazebo excels at drones (PX4 gives a complete flight stack) and
ROS-centric workflows. They are complementary backends.

## 2. Gazebo Architecture

### 2.1 Core Design

Gazebo Sim (Harmonic LTS, supported 2023-2028) is a dual-process system:

```
┌─────────────────────────────────────────────────────┐
│ Gazebo Server Process                               │
│                                                     │
│  Entity Component Manager (ECS)                     │
│  ┌──────────┐ ┌──────────┐ ┌────────────────────┐  │
│  │ Physics  │ │ Sensors  │ │ Scene Broadcaster  │  │
│  │ System   │ │ System   │ │ (world state→GUI)  │  │
│  │ (plugin) │ │ (plugin) │ │ (plugin)           │  │
│  └──────────┘ └──────────┘ └────────────────────┘  │
│  ┌──────────┐ ┌──────────────────────────────────┐  │
│  │ User     │ │ Custom Systems (plugins)         │  │
│  │ Commands │ │ PX4 bridge, diff-drive, etc.     │  │
│  └──────────┘ └──────────────────────────────────┘  │
│                       │                             │
│              gz-transport (pub/sub + services)      │
│                       │                             │
└───────────────────────┼─────────────────────────────┘
                        │
              ┌─────────┼─────────┐
              │         │         │
┌─────────────▼──┐ ┌────▼────┐ ┌──▼──────────────┐
│ Gazebo Client  │ │ ROS 2   │ │ External        │
│ (GUI process)  │ │ Bridge  │ │ Programs        │
│ Ogre2 renderer │ │         │ │ (PX4, our code) │
└────────────────┘ └─────────┘ └─────────────────┘
```

**Key concepts:**

- **Entity Component System (ECS):** Entities are simulation objects (models,
  links, lights). Components describe properties (pose, geometry, mass).
  Systems act on entities through a simulation loop.
- **gz-transport:** Socket-based message passing for inter-process
  communication. Supports pub/sub topics and request/reply services.
- **Plugins:** All loaded at runtime — no recompilation needed. Server plugins
  (systems) handle physics, sensors, and custom behavior. GUI plugins handle
  visualization.
- **SDF (Simulation Description Format):** The native scene description. Richer
  than URDF — describes entire worlds (ground planes, lighting, physics config,
  multiple robots, plugins). URDF auto-converted via libsdformat.

### 2.2 Physics Engines

Gazebo's physics is pluggable via the Physics system plugin:

- **DART** (default) — stable, well-tested, good for most robotics
- **Bullet** — wide compatibility, game-engine heritage
- **TPE (Trivial Physics Engine)** — lightweight, kinematic-only

This contrasts with Isaac which is locked to PhysX (but GPU-accelerated).

### 2.3 Sensor Simulation

Gazebo provides rich sensor simulation through the Sensors system plugin:

| Sensor | Use for Drones | Notes |
|--------|---------------|-------|
| IMU | Attitude estimation | Accel + gyro, configurable noise |
| GPS | Position (outdoor) | Lat/lon/alt with noise model |
| Barometer | Altitude estimation | Pressure-based |
| Magnetometer | Heading reference | 3-axis, declination model |
| Camera (RGB) | FPV, object detection | Configurable resolution/FPS |
| Depth Camera | Obstacle avoidance | Range images |
| LIDAR (2D/3D) | Mapping, avoidance | Configurable scan pattern |

All sensor data is published on gz-transport topics.

### 2.4 External Communication

Programs interact with Gazebo through:

1. **gz-transport topics** — pub/sub for streaming data (sensor readings,
   joint states, world state)
2. **gz-transport services** — request/reply for commands (spawn model,
   set pose, apply force)
3. **gz CLI** — `gz service -s /world/<name>/create --reqtype gz.msgs.EntityFactory`
4. **ROS 2 bridge** — bidirectional topic/service mapping
5. **Custom plugins** — C++ or Python code loaded into the server process

### 2.5 URDF Spawning

Gazebo auto-converts URDF to SDF via libsdformat:

```bash
# Spawn a URDF model into a running world
gz service -s /world/default/create \
  --reqtype gz.msgs.EntityFactory \
  --reptype gz.msgs.Boolean \
  --timeout 1000 \
  --req 'sdf_filename: "/path/to/robot.urdf", name: "my_robot"'
```

Limitations of URDF vs SDF:
- URDF describes one robot only; SDF can describe full worlds
- URDF lacks plugin elements (needed for PX4 bridge, sensors)
- Mimic joints may need manual SDF plugin equivalents
- No world-level config (physics step size, gravity, environment)

## 3. PX4 SITL — The Drone Killer Feature

### 3.1 What PX4 SITL Provides

PX4 is a professional open-source autopilot. In SITL (Software-In-The-Loop)
mode, PX4 runs as a separate process and communicates with Gazebo:

```
┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│ Ground       │◀─MAVLink─│ PX4 SITL     │◀─GZ──── │ Gazebo       │
│ Control      │          │ (autopilot)  │  Bridge  │ (physics)    │
│ (MAVSDK,     │          │              │          │              │
│  QGC, ROS)   │          │ Attitude     │          │ Sensor data  │
│              │─MAVLink─▶│ Position     │─Motor──▶ │ Motor forces │
│              │          │ Failsafe     │  cmds    │ Collisions   │
│              │          │ Missions     │          │              │
└──────────────┘          └──────────────┘          └──────────────┘
```

**What PX4 handles (so we don't have to):**
- Attitude stabilization (PID cascades)
- Position hold, velocity control, altitude hold
- Mission waypoint following
- Failsafes (low battery, GPS loss, geofence)
- Sensor fusion (EKF2)
- Motor mixing (quad-X, hex, octo, etc.)

**What we provide:**
- The vehicle model (SDF/URDF with physics + sensors)
- High-level commands via MAVLink (takeoff, goto, land, velocity)
- The simulation environment (terrain, obstacles, wind)

### 3.2 Lockstep Simulation

Gazebo and PX4 run in lockstep — Gazebo sets PX4's clock on every sim step
via the GZ Bridge. This means:
- Simulation can run faster or slower than real-time
- Pause simulation to step through code
- 6-10x real-time on powerful desktops, 3-4x on laptops
- Deterministic (same inputs produce same outputs)

### 3.3 Supported Vehicle Types

PX4 + Gazebo supports:
- **Quadrotors** (X, +, H configurations)
- **Hexacopters** and octocopters
- **Fixed-wing** aircraft
- **VTOL** (tilt-rotor, tail-sitter, standard)
- **Rovers** (differential, Ackermann)

Vehicle models are SDF files in the PX4-gazebo-models repository.

### 3.4 Control Interface (MAVLink)

Drones are commanded via MAVLink protocol:

```python
# Using mavsdk-python
from mavsdk import System

drone = System()
await drone.connect(system_address="udp://:14540")

await drone.action.arm()
await drone.action.takeoff()

# Velocity control (what our teleop maps to)
await drone.offboard.set_velocity_body(
    VelocityBodyYawspeed(vx, vy, vz, yaw_rate))
await drone.offboard.start()

await drone.action.land()
```

This maps naturally to the existing teleop command interface:
- `vx_mps` → forward velocity
- `yaw_rate_rps` → yaw rate
- `body_height_m` → altitude (reinterpreted as target altitude)
- New: `vy_mps` → lateral velocity (strafe)
- New: `vz_mps` → vertical velocity (climb/descend)

## 4. Integration Architecture

### 4.1 Sidecar Bridge Pattern (Same as Isaac)

```
Claude Code CLI ──stdio──▶ MCP Bridge Server ──TCP :9879──▶ Gazebo Bridge
                           (server/main.py)                  (gazebo_bridge/)
                               │                                  │
                               │                            gz-transport
                               │                                  │
                               │                            ┌─────▼──────┐
                               │                            │ Gazebo Sim │
                               │                            │ (gz sim)   │
                               │                            └────────────┘
                               │                                  │
                               │                            ┌─────▼──────┐
                               │                            │ PX4 SITL   │
                               │                            │ (optional) │
                               │                            └────────────┘
                               │
                          TCP :9878 ──▶ Isaac Bridge (existing, port 9878)
                          TCP :9877 ──▶ Chrono Daemon (existing, port 9877)
```

**Why sidecar (not in-process gz-transport)?**
- Process isolation — Gazebo has its own event loop and lifecycle
- Consistent pattern with Isaac — same protocol, same client structure
- MCP server stays dependency-free (no `import gz.transport` in server/)
- Independent startup (Gazebo + PX4 require specific environment setup)
- Gazebo Python bindings are less mature than C++ — sidecar can fallback
  to `gz service` CLI via subprocess

### 4.2 Protocol

Same newline-delimited JSON protocol as Isaac and FreeCAD:

```json
{"cmd": "ping", "args": {}}
→ {"ok": true, "result": {"version": "harmonic", "worlds": ["default"]}}

{"cmd": "spawn_model", "args": {"path": "/tmp/sim_pkg/robot.urdf", "name": "quad1", "format": "urdf"}}
→ {"ok": true, "result": {"entity_id": 42, "joints": ["rotor_0", "rotor_1", "rotor_2", "rotor_3"]}}

{"cmd": "simulate", "args": {"duration_s": 5.0, "dt_s": 0.001, "output_interval": 0.1}}
→ {"ok": true, "result": {"samples": [...], "summary": {...}}}

{"cmd": "teleop_start", "args": {"profile": {"controller_type": "px4_mavlink"}}}
→ {"ok": true, "result": {"session_id": "abc123", "mode": "offboard"}}

{"cmd": "teleop_command", "args": {"session_id": "abc123", "vx_mps": 1.0, "vy_mps": 0, "vz_mps": 0, "yaw_rate_rps": 0}}
→ {"ok": true, "result": {"state": "flying", "altitude_m": 10.2, "speed_mps": 0.95}}

{"cmd": "screenshot", "args": {"width": 1280, "height": 720}}
→ {"ok": true, "result": {"image_base64": "..."}}
```

### 4.3 Bridge Commands

| Command | Phase | Description |
|---------|-------|-------------|
| `ping` | MVP | Health check, return Gazebo version + active worlds |
| `spawn_model` | MVP | Load URDF/SDF into Gazebo world via EntityFactory service |
| `simulate` | MVP | Step physics for duration, collect joint state samples |
| `simulate_start` | MVP | Non-blocking batch simulation |
| `simulate_status` | MVP | Poll running simulation |
| `simulate_stop` | MVP | Cancel simulation |
| `screenshot` | MVP | Capture viewport (base64 PNG) |
| `diagnose` | MVP | Introspect scene (models, joints, links) |
| `set_world_config` | Phase 2 | Physics engine, step size, gravity |
| `add_sensor` | Phase 2 | Attach sensor to a link |
| `get_sensor_data` | Phase 2 | Read latest sensor values |
| `px4_start` | Phase 3 | Launch PX4 SITL process |
| `px4_status` | Phase 3 | Check PX4 connection state |
| `px4_stop` | Phase 3 | Terminate PX4 SITL |
| `teleop_start` | Phase 3 | Start teleop session (MAVLink or direct) |
| `teleop_command` | Phase 3 | Send velocity/yaw/altitude commands |
| `teleop_state` | Phase 3 | Read telemetry (position, attitude, battery) |
| `teleop_stop` | Phase 3 | End teleop session, land and disarm |
| `spawn_multi` | Phase 4 | Spawn multiple vehicles with unique namespaces |
| `set_wind` | Phase 4 | Configure wind model |
| `set_terrain` | Phase 4 | Load heightmap or mesh terrain |

## 5. Sim Export Pipeline Changes

### 5.1 Existing Pipeline (Format-Agnostic by Design)

The existing `sim_export.py` was explicitly designed for multiple output
formats. The `SimModel` intermediate representation is format-agnostic:

```python
# sim_export.py — existing structure
@dataclass(frozen=True, slots=True)
class SimLink:
    name: str
    mesh_path: str | None
    position: tuple[float, float, float]     # meters, world frame
    rotation_quat: tuple[float, float, float, float]
    mass_kg: float
    inertia: tuple[float, ...]               # 6-element symmetric tensor
    is_root: bool

@dataclass(frozen=True, slots=True)
class SimJoint:
    name: str
    joint_type: str
    parent: str
    child: str
    axis: tuple[float, float, float]
    origin: tuple[float, float, float, float, float, float]  # x,y,z,r,p,y
    limit_lower: float
    limit_upper: float
    effort: float
    velocity: float
    damping: float
    friction: float
    mimic: dict | None

# build_sim_model(mechanism, manifest) → SimModel
# write_urdf(model, output_path) → str       ← exists
# write_sdf(model, output_path) → str        ← to add
```

### 5.2 New: write_sdf()

```python
def write_sdf(
    model: SimModel,
    output_path: str,
    *,
    base_dir: str | None = None,
    world_name: str = "default",
    model_name: str = "robot",
    plugins: list[dict[str, Any]] | None = None,
    include_world: bool = True,
    include_ground_plane: bool = True,
    physics_engine: str = "dart",
    physics_step_size: float = 0.001,
    gravity: tuple[float, float, float] = (0, 0, -9.81),
) -> str:
    """Write SimModel as SDF file.

    If include_world=True, wraps the model in a full <world> with ground
    plane, lighting, and physics config. If False, writes just the <model>
    element (for spawning into an existing world).
    """
```

**SDF structure (with world):**

```xml
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="default">
    <physics name="default_physics" type="dart">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1.0</real_time_factor>
    </physics>

    <light type="directional" name="sun">
      <pose>0 0 10 0 0 0</pose>
      <diffuse>0.8 0.8 0.8 1</diffuse>
    </light>

    <model name="ground_plane">
      <static>true</static>
      <link name="link">
        <collision name="collision">
          <geometry><plane><normal>0 0 1</normal></plane></geometry>
        </collision>
        <visual name="visual">
          <geometry><plane><normal>0 0 1</normal><size>100 100</size></plane></geometry>
        </visual>
      </link>
    </model>

    <model name="robot">
      <pose>0 0 0.5 0 0 0</pose>

      <link name="base_link">
        <pose>0 0 0 0 0 0</pose>
        <inertial>
          <mass>1.5</mass>
          <inertia>
            <ixx>0.01</ixx><iyy>0.01</iyy><izz>0.02</izz>
          </inertia>
        </inertial>
        <collision name="collision">
          <geometry><mesh><uri>meshes/base_link.stl</uri></mesh></geometry>
        </collision>
        <visual name="visual">
          <geometry><mesh><uri>meshes/base_link.stl</uri></mesh></geometry>
        </visual>
      </link>

      <joint name="rotor_0_joint" type="revolute">
        <parent>base_link</parent>
        <child>rotor_0</child>
        <axis>
          <xyz>0 0 1</xyz>
          <limit>
            <lower>-1e16</lower>
            <upper>1e16</upper>
            <effort>10</effort>
            <velocity>1000</velocity>
          </limit>
        </axis>
      </joint>

      <!-- PX4 bridge plugin -->
      <plugin filename="gz-sim-multicopter-motor-model-system"
              name="gz::sim::systems::MulticopterMotorModel">
        <!-- motor parameters -->
      </plugin>
    </model>
  </world>
</sdf>
```

**Key SDF differences from URDF that write_sdf() must handle:**

| Feature | URDF | SDF |
|---------|------|-----|
| Root element | `<robot>` | `<sdf><world><model>` |
| Link pose | Implicit (from joint chain) | Explicit `<pose>` element |
| Joint parent/child | Attributes | Text content |
| Mesh reference | `<filename>` | `<uri>` |
| Plugins | Not supported | `<plugin>` elements |
| World config | Not supported | `<physics>`, `<light>`, `<scene>` |
| Multiple robots | Not supported | Multiple `<model>` in one `<world>` |
| Mimic joints | `<mimic>` element | Plugin-based or `<mimic>` (SDF 1.9+) |

## 6. New Files

### 6.1 gazebo_bridge/bridge_server.py

TCP server on port 9879. Unlike Isaac, Gazebo has its own server process, so
the bridge doesn't need to pump a main-thread event loop. Instead:

```
┌─────────────────────────────────┐
│ gazebo_bridge process           │
│                                 │
│  Thread 1: TCP server           │
│    Accept connections            │
│    Parse JSON commands           │
│    Dispatch to runtime           │
│                                 │
│  Thread 2: gz-transport client  │
│    Subscribe to sensor topics    │
│    Publish commands              │
│    Service calls                 │
│                                 │
│  Thread 3 (Phase 3): MAVLink    │
│    mavsdk-python async loop     │
│    PX4 command forwarding        │
└─────────────────────────────────┘
```

**Contrast with Isaac bridge:** Isaac requires main-thread pumping because
Kit/USD operations deadlock otherwise. Gazebo runs as a separate process, so
our bridge just needs to talk to it via gz-transport (or CLI). No main-thread
constraint.

### 6.2 gazebo_bridge/runtime_gazebo.py

Core runtime implementing Gazebo interactions:

```python
class GazeboRuntime:
    """Interface to a running Gazebo simulation."""

    def spawn_model(self, path: str, name: str, format: str = "urdf") -> dict:
        """Spawn a model into the Gazebo world.

        Uses gz-transport EntityFactory service. Accepts URDF (auto-converted
        to SDF by libsdformat) or native SDF.
        """

    def simulate(self, duration_s: float, dt_s: float, output_interval: float) -> dict:
        """Run batch simulation, collecting joint state samples."""

    def get_joint_states(self) -> dict[str, float]:
        """Read current joint positions from gz-transport topic."""

    def screenshot(self, width: int, height: int) -> str:
        """Capture viewport as base64 PNG."""

    def diagnose(self) -> dict:
        """Introspect scene: list models, joints, links."""
```

**Implementation options (ranked by preference):**

1. **gz-transport Python bindings** (`from gz.transport import Node`) — cleanest
   but requires Gazebo Python packages installed
2. **gz CLI subprocess** — `gz service -s /world/.../create ...` — always works
   if `gz` is on PATH, but slower per call
3. **ROS 2 bridge** — if ROS 2 is already in use, can piggyback on ros_gz_bridge

The runtime should try option 1, fall back to option 2.

### 6.3 gazebo_bridge/px4_integration.py (Phase 3)

```python
class PX4SITLManager:
    """Manage PX4 SITL process lifecycle."""

    def start(self, vehicle: str = "x500", instance: int = 0) -> None:
        """Launch PX4 SITL as subprocess.

        Sets PX4_SIM_MODEL, PX4_GZ_WORLD, PX4_GZ_MODEL_NAME env vars.
        PX4 connects to Gazebo via the GZ Bridge automatically.
        """

    def stop(self) -> None:
        """Terminate PX4 SITL process."""

    def is_ready(self) -> bool:
        """Check if PX4 is connected and ready (heartbeat received)."""


class MAVLinkController:
    """High-level drone control via MAVLink (mavsdk-python).

    This replaces the role of compute_targets() controllers for drones.
    PX4 handles all low-level motor mixing — we just send commands.
    """

    async def connect(self, address: str = "udp://:14540") -> None: ...
    async def arm(self) -> None: ...
    async def takeoff(self, altitude_m: float = 10.0) -> None: ...
    async def land(self) -> None: ...

    async def set_velocity(
        self,
        vx_mps: float,
        vy_mps: float,
        vz_mps: float,
        yaw_rate_rps: float,
    ) -> None:
        """Set velocity in body frame (offboard mode)."""

    async def get_telemetry(self) -> dict:
        """Read position, attitude, velocity, battery, GPS."""
```

### 6.4 gazebo_bridge/controllers.py

For non-PX4 scenarios (direct physics sim without autopilot):

```python
class QuadcopterMixerController:
    """Direct motor RPM control via quadrotor mixing matrix.

    Maps velocity commands to individual motor speeds. Used when PX4
    is NOT running and we need direct physics control.

    Mixing matrix for X-configuration quadcopter:
        motor_0 (front-right, CW):  +throttle -roll +pitch -yaw
        motor_1 (rear-left, CW):    +throttle +roll -pitch -yaw
        motor_2 (front-left, CCW):  +throttle +roll +pitch +yaw
        motor_3 (rear-right, CCW):  +throttle -roll -pitch +yaw
    """

    def compute_targets(
        self,
        state: TeleopState,
        dt_s: float,
        config: GazeboTeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        """Return motor RPM targets."""
```

**Key architectural difference from Isaac controllers:** For drone teleop with
PX4, the controller lives in PX4, not in our bridge. The bridge's role shifts
from "compute joint targets every tick" to "forward high-level commands via
MAVLink." The `PX4TeleopController` is essentially a passthrough that calls
`MAVLinkController.set_velocity()`.

### 6.5 server/gazebo_client.py

TCP client (mirrors `server/isaac_client.py`):

```python
class GazeboClient:
    """TCP client connecting to Gazebo bridge on localhost:9879."""

    def __init__(self, host: str = "localhost", port: int = 9879): ...
    def send_command(self, cmd: str, args: dict) -> dict: ...

    # Convenience methods
    def spawn_model(self, path: str, name: str, **kw) -> dict: ...
    def simulate(self, duration_s: float, **kw) -> dict: ...
    def teleop_start(self, profile: dict) -> dict: ...
    def teleop_command(self, session_id: str, **kw) -> dict: ...
    def screenshot(self, **kw) -> dict: ...


def get_client() -> GazeboClient | None:
    """Module-level singleton, gracefully returns None if unavailable."""
```

Environment variables:
- `SOLIDMIND_GAZEBO_HOST` (default: `localhost`)
- `SOLIDMIND_GAZEBO_PORT` (default: `9879`)
- `SOLIDMIND_GAZEBO_CONNECT_TIMEOUT_S` (default: `5.0`)
- `SOLIDMIND_GAZEBO_READ_TIMEOUT_S` (default: `30.0`)

### 6.6 server/gazebo_adapter.py

Adapter shim (mirrors `server/isaac_adapter.py`):

```python
def simulate(mechanism: dict, **kwargs) -> dict:
    """Run batch simulation via Gazebo bridge.

    Returns {"ok": true, "result": {"samples": [...], "summary": {...}}}
    or {"ok": false, "error": {"code": "...", "message": "..."}}.
    """

def teleop_start(mechanism: dict, profile: dict, **kwargs) -> dict: ...
def teleop_command(session_id: str, **kwargs) -> dict: ...
def teleop_state(session_id: str) -> dict: ...
def teleop_stop(session_id: str) -> dict: ...
def screenshot(**kwargs) -> dict: ...
```

### 6.7 server/tools_motion.py Changes

```python
# Add gazebo to backends
_SIM_BACKENDS = {"chrono", "isaac", "gazebo"}

# In motion_simulate():
if selected_backend == "chrono":
    response = _simulate_with_chrono(...)
elif selected_backend == "gazebo":
    response = _simulate_with_gazebo(...)
elif selected_mode == "teleop":
    response = motion_teleop_start(...)
else:
    response = _simulate_with_isaac(...)

# In motion_teleop_start():
if selected_backend == "gazebo":
    from server import gazebo_adapter
    result = gazebo_adapter.teleop_start(...)
elif selected_backend == "isaac":
    from server import isaac_adapter
    result = isaac_adapter.teleop_start(...)

# New teleop command fields for drones:
# vy_mps (lateral), vz_mps (vertical) — optional, default 0
```

### 6.8 scripts/run_gazebo_bridge.sh

```bash
#!/usr/bin/env bash
# Launch Gazebo bridge sidecar.
# Prerequisites:
#   - Gazebo Harmonic+ installed (gz sim --version)
#   - Optional: PX4-Autopilot built for SITL
#   - Optional: mavsdk-python installed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source Gazebo environment if needed
if command -v gz &>/dev/null; then
    echo "Gazebo found: $(gz sim --version 2>/dev/null || echo 'unknown')"
else
    echo "ERROR: Gazebo not found. Install with: sudo apt install gz-harmonic"
    exit 1
fi

# Start Gazebo sim in background (headless or GUI)
GZ_HEADLESS="${GZ_HEADLESS:-false}"
GZ_WORLD="${GZ_WORLD:-empty.sdf}"

if [ "$GZ_HEADLESS" = "true" ]; then
    gz sim -s "$GZ_WORLD" &
else
    gz sim "$GZ_WORLD" &
fi
GZ_PID=$!

# Start bridge server
cd "$PROJECT_DIR"
python3 -m gazebo_bridge.bridge_server --port "${SOLIDMIND_GAZEBO_PORT:-9879}"

# Cleanup
kill $GZ_PID 2>/dev/null || true
```

## 7. Implementation Phases

### Phase 1: MVP — Spawn + Batch Simulate

**Goal:** Spawn a URDF model in Gazebo, step physics, return joint states.

**Files to create:**
- `gazebo_bridge/__init__.py`
- `gazebo_bridge/bridge_server.py` (commands: ping, spawn_model, simulate,
  simulate_start/status/stop, screenshot, diagnose)
- `gazebo_bridge/runtime_gazebo.py` (gz-transport or CLI subprocess)
- `gazebo_bridge/models.py` (GazeboSession, GazeboConfig)
- `server/gazebo_client.py`
- `server/gazebo_adapter.py`
- `scripts/run_gazebo_bridge.sh`

**Files to modify:**
- `server/tools_motion.py` — add `"gazebo"` to `_SIM_BACKENDS`,
  add `_simulate_with_gazebo()`
- `server/main.py` — register gazebo in tool descriptions

**Not needed:** PX4, MAVLink, SDF export, sensors, teleop

**Test:** Export hexapod URDF → spawn in Gazebo → batch simulate → verify
joint state samples returned.

### Phase 2: SDF Export + World Configuration

**Goal:** Native SDF output with world config and plugin injection.

**Files to create/modify:**
- `server/sim_export.py` — add `write_sdf()` function
- `gazebo_bridge/runtime_gazebo.py` — add world config commands

**Test:** Generate SDF from SimModel → spawn in Gazebo → verify physics
config and ground plane.

### Phase 3: PX4 SITL + Drone Teleop

**Goal:** Full drone simulation with PX4 autopilot.

**Files to create:**
- `gazebo_bridge/px4_integration.py` (PX4SITLManager, MAVLinkController)
- `gazebo_bridge/controllers.py` (QuadcopterMixerController, PX4TeleopController)
- `scripts/gazebo_keyboard_teleop.py` (W/A/S/D/Q/E/arrows)

**Files to modify:**
- `gazebo_bridge/bridge_server.py` — add px4_start/stop, teleop commands
- `gazebo_bridge/runtime_gazebo.py` — teleop lifecycle
- `gazebo_bridge/models.py` — DroneConfig, DroneTeleopState
- `server/tools_motion.py` — update teleop routing for `backend='gazebo'`
- `server/models.py` or `isaac_bridge/models.py` — extend TeleopCommand
  with `vy_mps`, `vz_mps`

**Dependencies:** `mavsdk` (pip), PX4-Autopilot (built from source for SITL)

**Test:** Spawn quadcopter SDF → start PX4 SITL → arm → takeoff → send
velocity commands → land → verify telemetry stream.

### Phase 4: Full Vision

- Multi-vehicle support (spawn N drones, independent PX4 instances)
- Sensor data streaming (camera images, LIDAR point clouds via MCP)
- Environment configuration (wind models, terrain heightmaps, obstacles)
- ROS 2 bridge for external tool integration
- SDF world templates (warehouse, outdoor field, urban canyon)

## 8. Dependencies

| Dependency | Phase | Required? | Install |
|-----------|-------|-----------|---------|
| Gazebo Harmonic+ | 1 | Yes | `sudo apt install gz-harmonic` |
| gz-transport Python | 1 | Preferred (CLI fallback) | Comes with Gazebo |
| PX4-Autopilot | 3 | For drone SITL | Build from source |
| mavsdk-python | 3 | For MAVLink control | `pip install mavsdk` |
| ROS 2 | 4 | Optional | Full ROS 2 install |

## 9. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| gz-transport Python bindings immature | Medium | Fallback to `gz service` CLI subprocess |
| PX4 lockstep timing complexity | High | Start with non-lockstep (Phase 1), add lockstep in Phase 3 |
| Gazebo version fragmentation | Medium | Target Harmonic LTS (2023-2028), test on Jetty |
| URDF→SDF conversion loses mimic joints | Low | Phase 2 adds native SDF export |
| mavsdk-python async complexity | Medium | Run in dedicated thread with own event loop |

## 10. References

- [Gazebo Sim Architecture](https://gazebosim.org/docs/latest/architecture/)
- [PX4 Gazebo Simulation](https://docs.px4.io/main/en/sim_gazebo_gz/)
- [Gazebo URDF Spawning](https://gazebosim.org/docs/harmonic/spawn_urdf/)
- [PX4-gazebo-models (vehicle SDF files)](https://github.com/PX4/PX4-gazebo-models)
- [MAVSDK-Python](https://github.com/mavlink/MAVSDK-Python)
- [Gazebo Harmonic docs](https://gazebosim.org/docs/harmonic/)
- [SDF specification](http://sdformat.org/spec)
