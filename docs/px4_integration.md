# PX4 SITL Integration

This document covers SolidMind's drone development platform: PX4 SITL +
Gazebo Harmonic + the SolidMind bridge as a MAVLink client. Phases 1-3
are landed; Phases 4-5 are in progress.

## Architecture

```
[ User / LLM ]
      │
      │ MCP tool calls (motion.simulate, motion.teleop_command, …)
      ▼
[ SolidMind bridge ]
      │
      ├─► gz transport :spawn ────────► [ Gazebo Harmonic ]
      │                                       │
      │                                       │ runs the SDF written by
      │                                       │ server/sim_export.py
      │                                       │ (multicopter motor model
      │                                       │ plugin + IMU/GPS/baro/mag
      │                                       │ sensors on root link)
      │                                       ▼
      │                                 [ Drone in sim ]
      │                                       ▲
      │                                       │ rotor velocity via
      │                                       │ /<model>/command/motor_speed
      │                                       │
      └─► MAVLink UDP :14540 ─► [ PX4 SITL ] ─┘
                                       ▲
                                       │ sensor data from gz topics
                                       │ (gz<->PX4 bridge inside PX4)
                                       ▼
                                 EKF2 + autopilot loop
```

The bridge is **a MAVLink client of PX4** — it sends offboard setpoints
and reads telemetry. The autopilot loop lives in PX4. Sensor data flows
from Gazebo into PX4 via PX4's internal gz<->mavlink bridges; SolidMind
doesn't touch that path.

## Install

### 1. PX4-Autopilot (source build)

```bash
git clone https://github.com/PX4/PX4-Autopilot.git --recursive ~/repos/PX4-Autopilot
bash ~/repos/PX4-Autopilot/Tools/setup/ubuntu.sh
```

The setup script installs ninja, ccache, kconfig-frontends, gz-harmonic,
gstreamer, libeigen3-dev, and other build deps. **Requires sudo** —
you'll be prompted once. Supports Ubuntu 22.04 and 24.04.

If `gz-harmonic` is already installed (this repo's earlier work depends
on it), apt will treat the install as a no-op. The script also adds the
OSRF Gazebo apt repo if it isn't already present.

After setup, you may need to log out and back in (or `source` your
shell rc) so the new PATH entries take effect.

### 2. Build PX4 SITL with gz_x500

```bash
cd ~/repos/PX4-Autopilot
make px4_sitl gz_x500
```

That command does two things in one shot:

1. Builds PX4 SITL binary at `build/px4_sitl_default/bin/px4`
2. Launches a Gazebo session with the X500 quadrotor model

Initial cold build is ~30 minutes on a 16-core machine. Subsequent
incremental builds are <30 seconds.

When the build finishes you'll see the PX4 console prompt (`pxh>`)
and Gazebo opening with an X500 quadrotor on the ground plane.
**Leave both running** — they communicate over local sockets.

### 3. Install pymavlink

In the SolidMind venv:

```bash
cd ~/repos/solidmind-cad
pip install -e ".[drone]"
```

That pulls `pymavlink>=2.4.40` from `pyproject.toml`'s `[drone]` optional
group.

### PX4 v1.17 takeoff sequence (verified working)

PX4 v1.17 main has stricter SITL arming behavior than older releases.
The smoke test (`scripts/px4_smoke_test_takeoff.py`) and the bridge's
`MavlinkController.arm()` / `takeoff_via_mode()` use this exact dance:

1. Connect via MAVLink (`udp:127.0.0.1:14540`)
2. **Stream HEARTBEAT at ≥2 Hz** so PX4 considers a GCS connected
3. Set permissive params:
   - `COM_RC_IN_MODE=4` (no RC stick required — SITL has no transmitter)
   - `NAV_RCL_ACT=0` (no RC-loss action)
   - `NAV_DLL_ACT=0` (no data-link-loss action)
   - `MIS_TAKEOFF_ALT=<alt>` (default takeoff altitude)
4. Wait for sensor health bits in `SYS_STATUS` (gyro+accel+mag+baro)
5. **Force-arm** via `MAV_CMD_COMPONENT_ARM_DISARM` with `param2=21196.0`
   (the magic force-arm number; PX4's RC sensor bit lingers as
   "enabled but not present" in SITL and only force-arm bypasses that
   specific check)
6. **Switch mode to AUTO_TAKEOFF** via `MAV_CMD_DO_SET_MODE` with
   `param2=4` (AUTO main mode), `param3=2` (AUTO_TAKEOFF sub mode).
   PX4 lifts the (now-armed) vehicle to `MIS_TAKEOFF_ALT`.
7. Wait for altitude target (poll `LOCAL_POSITION_NED.z`)
8. Hover, then switch mode to AUTO_LAND (`param2=4`, `param3=6`)
9. Wait for landed/disarmed state

Without step 2 (continuous heartbeat) PX4 reports `Preflight Fail: No
connection to the GCS` and rejects every arm attempt.  Without step 5
(force flag) PX4 reports the generic `Resolve system health failures
first` even though every other check passes.  Without step 6 (mode
switch) the older `MAV_CMD_NAV_TAKEOFF` command alone gets ACKed but
the vehicle never lifts because it's still in POSCTL/HOLD mode.

This sequence has been verified flying the X500 to >3.5 m on PX4
v1.17.0-alpha1 with Gazebo Harmonic 10.1.1 on Ubuntu 24.04.

### 4. Run the smoke test

With `make px4_sitl gz_x500` still running in another terminal:

```bash
cd ~/repos/solidmind-cad
python scripts/px4_smoke_test_takeoff.py
```

Expected output:

```
[px4-smoke] connecting to udp:127.0.0.1:14540 ...
[px4-smoke] heartbeat from sys=1 comp=1 type=2 autopilot=12
[px4-smoke] armed
[px4-smoke] takeoff to 5.0 m commanded
[px4-smoke] altitude reached: 5.04 m (target 5.0 m)
[px4-smoke] holding for 10.0 s
[px4-smoke] land commanded
[px4-smoke] disarmed
[px4-smoke] PASS
```

Watch the Gazebo window — you'll see the X500 lift off, hover at 5 m,
descend, and disarm.

If this passes, **PX4 + Gazebo + MAVLink are healthy on this machine**
independent of any SolidMind code. Phase 2 of the integration plan is
unblocked.

## Configuration

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SOLIDMIND_PX4_BIN` | `px4` (from PATH) | Override path to PX4 binary |
| `SOLIDMIND_PX4_INSTALL` | `~/repos/PX4-Autopilot` | Where Phase 4's airframe generator drops new airframe init files |
| `SOLIDMIND_GAZEBO_PX4_FAKE` | unset | When `=1`, Px4Manager runs in stub mode (no real PX4 process) — useful for unit tests |
| `SOLIDMIND_RUN_PX4_E2E` | unset | When `=1`, Phase 2's real-runtime tests are unskipped |

### Common MAVLink endpoints

PX4 SITL listens on several UDP endpoints by default:

| Port | Purpose |
|---|---|
| 14540 | Offboard external API (used by MAVSDK/pymavlink/SolidMind bridge) |
| 14550 | QGroundControl |
| 14580 | gz<->PX4 internal bridge |

The smoke test and bridge connect to **14540**.

## Drone SDF generation (Phase 3 — landed)

`server.sim_export.write_sdf` accepts a `drone_config` dict that switches
the SDF into flight-ready mode:

```python
drone_config = {
    "rotors": [
        {"index": 0, "joint": "rotor_FL_joint", "direction": "ccw"},
        {"index": 1, "joint": "rotor_FR_joint", "direction": "cw"},
        {"index": 2, "joint": "rotor_RR_joint", "direction": "ccw"},
        {"index": 3, "joint": "rotor_RL_joint", "direction": "cw"},
    ],
    # sensors default to True when drone_config is present
    "sensors": True,
    # or fine-grained:
    # "sensors": {"link": "base_link", "imu": True, "gps": True,
    #             "barometer": True, "magnetometer": True},
}
```

When emitted, the SDF contains:

- One canonical `<plugin name="gz::sim::systems::MulticopterMotorModel">`
  per rotor with `jointName`, `linkName`, `commandSubTopic`, and
  `actuator_number` correctly bound. PX4 publishes a single
  `gz.msgs.Actuators` message and each plugin reads its actuator slot.
- IMU / GPS (navsat) / barometer / magnetometer sensor declarations on
  the kinematic root link. PX4's EKF2 reads these via the gz<->PX4
  bridge.

Per-rotor motor parameters can be overridden in each rotor entry
(`motor_constant`, `moment_constant`, `max_rot_velocity`); defaults
match the stock X500. Phase 4's airframe generator will compute these
from BEMT thrust curves so each FreeCAD-built drone gets calibrated
motor params.

## Bridge MAVLink client (Phase 2 — in progress)

The bridge currently has scaffolding (`gazebo_bridge/px4_integration.py`,
`gazebo_bridge/controllers.py:Px4OffboardController`) but no real
MAVLink wiring. Phase 2 adds:

- `server/mavlink_controller.py` — async `MavlinkController` class
- `Px4OffboardController` rewritten to dispatch via the controller
- `Px4Manager` lifecycle tracks heartbeat instead of just `is_alive()`
- Real-runtime test suite at `tests/test_gazebo_px4_real_runtime.py`

Until Phase 2 lands, drive PX4 directly from `pymavlink` scripts
(`scripts/px4_smoke_test_takeoff.py` is the template). Phase 2 will
make `motion.teleop_command` send these commands through the normal
SolidMind tool surface.

## Parameterized airframe generator (Phase 4 — planned)

Phase 4 closes the FreeCAD → flying drone loop. The generator at
`server/px4_airframe_generator.py` will:

1. Read `SimModel` (mass, inertia, joint origins) from
   `cad.export_sim_package`
2. Read `drone_config["rotors"]` (per-rotor X/Y/Z + direction)
3. Read optional BEMT thrust curve from `study.results`
4. Compute the motor allocation matrix from the rotor geometry
5. Seed PID gains from mass + arm length + thrust-to-weight
6. Emit a PX4 airframe init file with a stable hash-based
   `SYS_AUTOSTART` ID (e.g. 50000-50999 range)
7. Drop the file into
   `<SOLIDMIND_PX4_INSTALL>/ROMFS/px4fmu_common/init.d-posix/airframes/`
8. Trigger an incremental `make px4_sitl` to rebuild with the new
   airframe registered

After Phase 4, every FreeCAD-built drone — racing quad, cinema hex,
fixed-pitch coaxial — gets its own PX4 airframe with sensible defaults.
No manual airframe tuning per drone.

## Troubleshooting

### `[libprotobuf ERROR] File already exists in database: gz/msgs/...` and PX4 sensor preflight fails

If `make px4_sitl gz_x500` floods the console with hundreds of lines like:

```
[libprotobuf ERROR] File already exists in database: gz/msgs/imu_sensor.proto
[libprotobuf ERROR] Invalid proto descriptor for file "gz/msgs/sensor.proto"
DynamicFactory(). Unable to place descriptors from [/usr/share/gz/protos/gz-msgs12.gz_desc] in the descriptor pool
```

…and PX4 reports preflight failures for every sensor (Accel/Gyro/Baro/Mag/EKF
all "missing"), the cause is **two gz-msgs versions installed at once**. The
older `gz-sim8` Harmonic transitional package pulls in `libgz-msgs10`, which
conflicts with `libgz-msgs12` that PX4 + `gz-sim10` use. Protobuf rejects
the duplicate registrations, breaking the sensor message decode pipeline.

Diagnose:

```bash
dpkg -l | grep -E "(gz-sim|gz-msgs)" | grep -v "^rc"
# If gz-sim8-cli + libgz-msgs10 appear alongside gz-sim10 + gz-msgs12,
# you have the conflict.
```

Confirm what PX4 actually links against:

```bash
strings ~/repos/PX4-Autopilot/build/px4_sitl_default/bin/px4 | grep libgz-msgs | sort -u
# Expect: libgz-msgs.so.12  (PX4 wants gz-msgs12 — keep that, drop gz-msgs10)
```

Fix: kill any running `gz-sim` processes, purge the gz-sim8 stack,
then re-test.

```bash
pkill -f gz-sim
sudo apt-get remove --purge -y gz-sim8-cli libgz-msgs10 libgz-msgs10-dev \
                                libgz-transport13 libgz-transport13-dev
sudo apt-get autoremove -y

# Verify only gz-sim10 + gz-msgs12 remain
dpkg -l | grep -E "(gz-sim|gz-msgs)" | grep -v "^rc"

# Sanity: gz alone should now load with no protobuf errors
gz sim --version

# Re-test PX4
cd ~/repos/PX4-Autopilot && make px4_sitl gz_x500
```

If protobuf errors persist, find any remaining libgz-msgs10:

```bash
ldconfig -p | grep libgz-msgs
# Should be one line per .so version. If libgz-msgs.so.10 still appears:
dpkg -S /usr/lib/x86_64-linux-gnu/libgz-msgs.so.10
# Identifies the owning package — purge that next.
```

### `make px4_sitl gz_x500` fails to find gz

Verify Gazebo Harmonic 10+ is installed: `gz sim --version`. If you see
"command not found", the setup script's apt repo step probably failed.
Re-run:

```bash
sudo wget https://packages.osrfoundation.org/gazebo.gpg \
    -O /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/gazebo-stable.list
sudo apt-get update && sudo apt-get install gz-harmonic
```

### Smoke test times out waiting for heartbeat

Make sure `make px4_sitl gz_x500` is actively running in another
terminal. The PX4 console (`pxh>`) prompt should be visible. If PX4
exited (e.g. due to a build error), the UDP port 14540 won't have a
listener.

Check: `ss -ulnp | grep 14540` — should show PX4 bound.

### Drone won't lift off

If the smoke test sends takeoff but the X500 stays on the ground, check
PX4 console for "preflight check" failures. Usually means the EKF
hasn't converged — wait 5-10 seconds for sensors to settle, then retry.
GPS lock is required; SITL provides it instantly but if it isn't,
check `commander preflight_check` in the PX4 console.

### Multiple gz sim worlds running

If you have lingering gz sim processes from other work, PX4 may attach
to the wrong world. List with `ps -ef | grep gz-sim-main`, kill the
ones you don't need, and restart `make px4_sitl gz_x500`.

## Verification staircase

Each phase has a single command that proves it works end-to-end. Run
them in order:

| Phase | Verification |
|---|---|
| 1 | `python scripts/px4_smoke_test_takeoff.py` (with `make px4_sitl gz_x500` running) |
| 2 | `SOLIDMIND_RUN_PX4_E2E=1 python -m unittest tests.test_gazebo_px4_real_runtime` |
| 3 | Already verified by `python -m unittest tests.test_tools_cad.TestCadExportSimPackage` |
| 4 | `tests.test_px4_solidmind_drone_e2e` — non-X500 drone flies under custom airframe |
| 5 | `examples/quadrotor_camera_drone/run.py` — full pipeline from design brief to hover |

## See also

- `docs/gazebo_integration.md` — broader Gazebo bridge architecture
- `.claude/rules/gazebo.md` — bridge protocol notes
- `tests/test_gazebo_multicopter_hover.py` — Tier 3 hover test (validates
  the canonical multicopter plugin format works end-to-end)
- `scripts/px4_smoke_test_takeoff.py` — the standalone PX4 takeoff
  reference script
