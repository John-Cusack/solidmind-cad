# Simulation Backends & RL Training

Advanced simulation and reinforcement learning details for SolidMind CAD.
For basic setup and getting started, see the [README](../README.md).

## Isaac-native RL Quickstart

This flow documents the current default RL path (`Isaac Lab + RSL-RL`) exposed by `rl.*`.

### Pipeline notes

- `rl.configure_environment` generates env config files with `PIPELINE = "isaaclab"`.
- `rl_training.train` reads that field and dispatches to `rl_training.isaaclab_train`.
- `rl.start_training` uses `ISAAC_PYTHON` when set (or auto-detects `../isaacsim/_build/linux-x86_64/release/python.sh` if present).
- Legacy/custom training code paths are retained for backward compatibility with older env configs, but Isaac-native is the default and recommended path.

### MCP sequence (with expected outputs)

1. `cad.export_sim_package` (include `mechanism_id` so URDF is generated)
   - Input example:
     - `{"mechanism_id":"mech_123", "format":"stl"}`
   - Expected output fields:
     - `ok`, `output_dir`, `urdf_path`, `sim_model`
2. `rl.configure_environment`
   - Input example:
     - `{"urdf_path":"<from cad.export_sim_package urdf_path>", "num_envs":4096}`
   - Expected output fields:
     - `ok`, `config_path`, `analysis` (`robot_name`, `morphology`, `actuated_joints`, `joint_limits`, ...)
3. `rl.start_training`
   - Input example:
     - `{"env_config":"<from rl.configure_environment config_path>", "max_iterations":3000}`
   - Expected output fields:
     - `ok`, `training_id`, `pid`, `output_dir`
4. `rl.monitor_training`
   - Input example:
     - `{"training_id":"<from rl.start_training training_id>"}`
   - Expected output fields:
     - `ok`, `process_status`, `elapsed_s`, optional `progress` (`iteration`, `mean_reward`, `status`)
5. `rl.deploy_policy`
   - Input example:
     - `{"training_id":"<from rl.start_training training_id>"}`
   - Expected output fields:
     - `ok`, `policy_path`, `config_path`, `joint_names`
6. `rl.evaluate_policy`
   - Input example:
     - `{"policy_path":"<from rl.deploy_policy policy_path>", "num_episodes":10}`
   - Expected output fields:
     - `ok`, `policy_loaded`, `output_shape`, `action_dim`

### CLI parity path (same training backend, without MCP)

```bash
export ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh

scripts/run_rl_training.sh \
  --env-config training_runs/<robot_env_config.py> \
  --output-dir training_runs/<run_id> \
  --max-iterations 3000
```

### Artifact mapping between CLI files and MCP tools

| Artifact | Produced by | MCP equivalent |
|---|---|---|
| `training_runs/<run_id>/progress.json` | `rl_training.train` / `rl_training.isaaclab_train` | `rl.monitor_training` (`progress`) |
| `training_runs/<run_id>/training_config.json` | `rl_training.train` / `rl_training.isaaclab_train` | in `output_dir` from `rl.start_training` |
| `training_runs/<run_id>/deployed/policy.pt` | `rl.deploy_policy` (or trainer export) | `policy_path` |
| `training_runs/<run_id>/deployed/deployment_config.json` | `rl.deploy_policy` (or trainer export) | `config_path` |

### Isaac RL dependencies

```bash
# Canonical interpreter path for a local Isaac Sim source build
export ISAAC_PYTHON=../isaacsim/_build/linux-x86_64/release/python.sh

# Install RL runtime dependencies into Isaac Sim's Python environment.
# Source paths for Isaac Lab modules vary by your local setup.
$ISAAC_PYTHON -m pip install -e /path/to/isaaclab
$ISAAC_PYTHON -m pip install -e /path/to/isaaclab_rl
$ISAAC_PYTHON -m pip install rsl-rl
```

Verify the required modules are importable:

```bash
$ISAAC_PYTHON - <<'PY'
import importlib

required = ["isaaclab", "isaaclab_rl", "rsl_rl"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit("Missing modules: " + ", ".join(missing))
print("Isaac-native RL dependencies OK:", ", ".join(required))
PY
```

RL-specific tests:

```bash
python3 -m unittest tests.test_rl_tools tests.test_rl_deploy_fixes
```

## Simulation Validation

### Lightweight (no external daemons)

```bash
python3 -m unittest tests.test_tools_motion tests.test_motion_isaac_integration tests.test_simulation_spec_builder tests.test_chrono_client
```

### Isaac bridge (runtime-backed)

```bash
scripts/run_isaac_bridge.sh --host 127.0.0.1 --port 9878
SOLIDMIND_RUN_ISAAC_E2E=1 python3 -m unittest tests.test_isaac_bridge_real_runtime
```

Env overrides:

- `SOLIDMIND_ISAAC_HOST`
- `SOLIDMIND_ISAAC_PORT`
- `SOLIDMIND_ISAAC_CONNECT_TIMEOUT_S`
- `SOLIDMIND_ISAAC_READ_TIMEOUT_S`

### Gazebo bridge (runtime-backed)

```bash
scripts/run_gazebo_bridge.sh --runtime real --world default --host 127.0.0.1 --port 9879
SOLIDMIND_RUN_GAZEBO_E2E=1 python3 -m unittest tests.test_gazebo_bridge_real_runtime
```

Gazebo PX4 lifecycle validation (fake PX4 mode for CI/local):

```bash
SOLIDMIND_GAZEBO_PX4_FAKE=1 scripts/run_gazebo_bridge.sh --runtime stub --enable-px4
SOLIDMIND_RUN_GAZEBO_PX4_E2E=1 python3 -m unittest tests.test_gazebo_px4_e2e
```

Env overrides:

- `SOLIDMIND_GAZEBO_RUNTIME` (`real` or `stub`)
- `SOLIDMIND_GAZEBO_HOST`
- `SOLIDMIND_GAZEBO_PORT`
- `SOLIDMIND_GAZEBO_CONNECT_TIMEOUT_S`
- `SOLIDMIND_GAZEBO_READ_TIMEOUT_S`

### Chrono backend

```bash
chrono_daemon/run.sh
```

Then in your MCP client/host, run a short manual path:

1. Call `motion.define_mechanism` with a small mechanism payload.
2. Call `motion.simulate` with `backend="chrono"` and the returned `mechanism_id`.

### Transcript replay

```bash
python3 scripts/replay_transcript.py tests/transcripts/cnc_L2.yml
```
