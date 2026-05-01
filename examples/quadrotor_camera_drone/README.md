# Quadrotor Camera Drone — FreeCAD → PX4 SITL flight pipeline

End-to-end demonstration of the SolidMind drone development platform:
build a custom quadrotor in FreeCAD, generate the PX4 airframe params
from the geometry, rebuild PX4 SITL with the new airframe, launch it
in Gazebo, and fly a hover-takeoff-land sequence under offboard MAVLink
control through the SolidMind bridge.

This is the "happy path" the demo recording will use, and the dogfood
test that proves Phases 2-4 of the platform work together.

## Two paths to a flying drone

There are now **two** ways to get a drone airframe into PX4 SITL:

### Recommended for new drones: `MulticopterAirframe` spec

Describe the drone as a typed dataclass and let the spec emit the
SimModel + SDF + PX4 airframe params directly.  No FreeCAD doc
required — useful for tests, parameter sweeps, and any drone where
the CAD geometry is purely cosmetic (the rotor visuals don't change
flight behaviour; PX4 reads CA_ROTOR positions and the SDF motor
plugin reads `motorConstant × ω²`).

```python
from server.airframes.presets import cinema_drone
from server.sim_export import write_sdf
from server.px4_airframe_generator import register_airframe

af = cinema_drone()              # 5.7 kg X-quad with payload + battery
sim_model = af.to_sim_model()    # SimModel with chassis + 4 rotor links
write_sdf(sim_model, "/tmp/cinema_drone.sdf", drone_config={
    "rotors": [
        {"index": i, "joint": f"{r.name}_joint",
         "direction": r.direction, "link": r.name}
        for i, r in enumerate(af.rotors)
    ],
    "sensors": True,
})
register_airframe(af.to_px4_airframe_params())
```

The spec collapses what used to be three loose inputs (`mechanism +
drone_config + manifest`) into one typed object.  Inertia, hover
throttle, and rotor positions are all derived from the same source
of truth, so it's no longer possible to forget to aggregate the
battery's mass into the chassis link.

### Legacy: drive everything from a FreeCAD doc via `run.py`

Useful when you actually need to *draw* the drone in FreeCAD (e.g.
for the demo recording where the LLM builds the chassis live).
`run.py` walks through the legacy `motion.define_mechanism` +
`cad.export_sim_package` API; both paths produce equivalent SDFs
through the same `write_sdf` writer, so the underlying physics is
identical.

## Prerequisites

1. **FreeCAD 1.1+** installed and running with the SolidMind addon
   (the bridge addon listens on TCP 9876).
2. **PX4-Autopilot** built once for SITL — see
   [`docs/px4_integration.md`](../../docs/px4_integration.md).
3. **Gazebo Harmonic 10+** installed (the PX4 setup script handles this).
4. **pymavlink** installed: `pip install -e ".[drone]"` from the repo root.

## Run it

From the repo root:

```bash
PYTHONPATH=. python3 examples/quadrotor_camera_drone/run.py \
    --output-dir /tmp/camera_drone \
    --takeoff-alt 5.0 \
    --hover-secs 15
```

You'll see five staged banners scroll by as the script:

1. **Builds CAD** — chassis + 4 rotors in FreeCAD via the `cad.*` tool helpers
2. **Defines mechanism + exports** — 4 continuous rotor joints + STL meshes
   + URDF + SDF (with multicopter motor model plugins, IMU/GPS/baro/mag
   sensors) + a custom PX4 airframe init script (e.g.
   `~/repos/PX4-Autopilot/ROMFS/.../airframes/50734_quadrotorcameradrone`)
3. **Rebuilds PX4** — `make px4_sitl <airframe_name>` to register the new
   airframe (~30 s incremental build after first run)
4. **Launches PX4 SITL + Gazebo** — `make px4_sitl <airframe_name>` again,
   blocking the script until UDP 14540 is reachable
5. **Flies** — `MavlinkController` connects, arms, switches to OFFBOARD,
   takes off to the requested altitude, hovers, lands, disarms

Total wall-clock time on a fresh machine: ~5-10 minutes (most of it the
PX4 incremental rebuild). On a warm machine: ~2-3 minutes.

## Stop after a stage

When iterating on geometry, you don't need to re-run the whole flight
sequence every time. `--stop-after` exits cleanly after the named stage:

```bash
# Just build the geometry — useful while tuning the FreeCAD layout
python3 examples/quadrotor_camera_drone/run.py --stop-after build

# Build + export — confirms the SDF and airframe params look right
python3 examples/quadrotor_camera_drone/run.py --stop-after export

# Through PX4 launch but skip flying — useful for QGroundControl debugging
python3 examples/quadrotor_camera_drone/run.py --stop-after launch
```

## Skip the rebuild

If you've already built PX4 with this airframe and just want to fly:

```bash
python3 examples/quadrotor_camera_drone/run.py --skip-px4-rebuild
```

## Drone parameters

The geometry is intentionally minimum-viable: rectangular chassis +
4 cylindrical rotor discs in an X pattern. Edit constants at the top
of `run.py` to change the layout:

| Constant | Default | Meaning |
|---|---|---|
| `WHEELBASE_MM` | 700 | Corner-to-corner across the X pattern |
| `ROTOR_RADIUS_MM` | 100 | Rotor disc radius |
| `CHASSIS_W/H_MM` | 200×200 | Chassis footprint |
| `CHASSIS_T_MM` | 30 | Chassis thickness |
| `CHASSIS_MASS_KG` | 1.2 | Mass of the central body |
| `ROTOR_MASS_KG` | 0.05 | Mass of each rotor disc |

Total mass: ~1.4 kg. With default motor constants
(`8.55e-6 N·s²` × `1000² rad/s²` × 4 rotors = 34 N max thrust) the
drone has ~2.4× thrust-to-weight at full throttle and a hover throttle
around 0.4 — comfortably in the safe range for PX4's MPC_THR_HOVER.

## How to verify

Watch Gazebo: the drone should lift off, hold the requested altitude,
and land cleanly. The PX4 console will print position + velocity
estimates and arming/disarming events.

The script prints the airframe ID + path so you can inspect the
generated PX4 params:

```bash
cat ~/repos/PX4-Autopilot/ROMFS/px4fmu_common/init.d-posix/airframes/50*_quadrotorcameradrone
```

Look for the `CA_ROTOR{N}_PX/PY/KM` lines, `MPC_THR_HOVER`, and the
`MC_*RATE_P` PID seeds — these are all derived from the geometry, not
hand-tuned.

## See also

- [`RECORDING_PROMPT.md`](RECORDING_PROMPT.md) — the LLM-driven
  counterpart to this script. 5-stage prompt sequence used during the
  demo recording: design brief → CAD build → BEMT optimization →
  prop build → PX4 flight verification, with a human-approval gate
  between each stage.
- [`docs/px4_integration.md`](../../docs/px4_integration.md) — full PX4
  integration architecture, the verified v1.17 takeoff sequence, and
  install runbook
- [`server/px4_airframe_generator.py`](../../server/px4_airframe_generator.py)
  — the airframe params generator
- [`examples/hexapod_robot/`](../hexapod_robot/) — sibling example
  exercising the orchestrator + URDF + Isaac Sim path (no PX4)
