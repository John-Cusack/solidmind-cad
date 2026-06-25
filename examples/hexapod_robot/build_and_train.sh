#!/usr/bin/env bash
# End-to-end hexapod build → URDF export → RL training pipeline.
#
# What this does in ~20 minutes:
#  1. Builds 37 hexapod bodies live in FreeCAD (chassis + 6 × (3 segments + 3 servos))
#  2. Defines a 37-part / 36-joint mechanism via motion.define_mechanism
#  3. Exports STLs + URDF via cad.export_sim_package
#  4. Patches the URDF's auto-generated joint limits (knee was asymmetric;
#     widen to ±2.094 rad to match the v3 reference)
#  5. Patches the env config to point at the fresh URDF + chassis name
#  6. Trains a 500-iter PPO walking policy on the fresh URDF (~14 min on a 3090)
#  7. Final checkpoint: training_runs/hex18_freshbuild_500/model_499.pt
#
# Run watch_walking.sh after this to see the trained robot in Isaac GUI.
#
# Requirements:
#  - FreeCAD running with addon on :9876
#  - Isaac Sim source build at ~/repos/isaacsim/
#  - $ISAAC_PYTHON (defaults to ~/repos/isaacsim/_build/.../python.sh)

set -euo pipefail

ISAAC_PY="${ISAAC_PYTHON:-$HOME/repos/isaacsim/_build/linux-x86_64/release/python.sh}"
URDF_OUT="${URDF_OUT:-/tmp/v3_orch_pkg}"
ENV_CFG_OUT="${ENV_CFG_OUT:-/tmp/v3_fresh_env_config.py}"
TRAIN_DIR="${TRAIN_DIR:-training_runs/hex18_freshbuild_500}"
NUM_ITERS="${NUM_ITERS:-500}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -x "$ISAAC_PY" ]; then
    echo "ERROR: Isaac Python not found at $ISAAC_PY" >&2
    exit 1
fi

# 0. Probe FreeCAD addon
python3 -c "
from orchestrator.worker_builds import common
import sys
if not common.freecad_ready():
    print('ERROR: FreeCAD addon not reachable on :9876', file=sys.stderr)
    sys.exit(1)
"

# 1+2+3. Build hexapod live + export URDF
echo "[1/4] Building hexapod (37 bodies) + exporting URDF..."
python3 scripts/demo_build_hexapod_18dof.py --fast --export

# Define mechanism + export URDF (the deterministic build script writes
# STLs but not URDF; this step uses the same mechanism dict and routes
# through cad.export_sim_package which writes URDF).
PYTHONPATH="$REPO_ROOT" python3 - << PYEOF
import importlib.util, sys
spec = importlib.util.spec_from_file_location("_demo", "scripts/demo_build_hexapod_18dof.py")
demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
mech = demo.build_mechanism_dict()

from server.tools_motion import motion_define_mechanism
r = motion_define_mechanism(mech)
if not r.get("ok"):
    print(f"motion_define_mechanism failed: {r}", file=sys.stderr); sys.exit(1)
mech_id = r["mechanism_id"]
print(f"  mechanism_id: {mech_id}")

from server.tools_cad import cad_export_sim_package
r = cad_export_sim_package(mechanism_id=mech_id, output_dir="${URDF_OUT}", format="stl")
if not r.get("ok"):
    print(f"cad_export_sim_package failed: {r}", file=sys.stderr); sys.exit(1)
print(f"  urdf_path: {r['urdf_path']}")
PYEOF

# 4. Patch URDF joint limits (auto-gen produces asymmetric knee)
echo "[2/4] Patching URDF joint limits..."
python3 - << PYEOF
path = "${URDF_OUT}/Hexapod_18DOF.urdf"
text = open(path).read()
text = text.replace(
    'lower="-2.0944" upper="0" effort="1.5"',
    'lower="-2.0944" upper="2.0944" effort="10.0"',
)
text = text.replace(
    'lower="-1.5708" upper="0.785398" effort="1.5"',
    'lower="-1.5708" upper="1.5708" effort="10.0"',
)
text = text.replace(
    'lower="-0.785398" upper="0.785398" effort="1.5"',
    'lower="-1.0472" upper="1.0472" effort="10.0"',
)
open(path, "w").write(text)
print(f"  patched {path}")
PYEOF

# 5. Patch env config — point at fresh URDF + Body_Chassis base link
echo "[3/4] Patching env config..."
cp training_runs/hex18_perf_100/env_config_patched.py "$ENV_CFG_OUT"
sed -i "s|URDF_PATH = .*|URDF_PATH = '${URDF_OUT}/Hexapod_18DOF.urdf'|" "$ENV_CFG_OUT"
sed -i "s|BASE_LINK = 'chassis'|BASE_LINK = 'Body_Chassis'|" "$ENV_CFG_OUT"
sed -i "s|FOOT_LINKS = \['tibia_L1', 'tibia_L2', 'tibia_L3', 'tibia_R1', 'tibia_R2', 'tibia_R3'\]|FOOT_LINKS = ['Tibia_L1', 'Tibia_L2', 'Tibia_L3', 'Tibia_R1', 'Tibia_R2', 'Tibia_R3']|" "$ENV_CFG_OUT"
echo "  env config: $ENV_CFG_OUT"

# 6. Train
mkdir -p "$TRAIN_DIR"
echo "[4/4] Training ${NUM_ITERS}-iter walking policy on the fresh URDF..."
"$ISAAC_PY" -m rl_training.isaaclab_train \
    --env-config "$ENV_CFG_OUT" \
    --output-dir "$TRAIN_DIR" \
    --max-iterations "$NUM_ITERS" \
    --num-envs 4096

echo
echo "Done. Checkpoint: $TRAIN_DIR/model_$((NUM_ITERS - 1)).pt"
echo "To watch the trained robot walk in Isaac GUI:"
echo "  DISPLAY=:0 \"$ISAAC_PY\" scripts/eval_policy_isaac.py \\"
echo "      --env-config $ENV_CFG_OUT \\"
echo "      --checkpoint $TRAIN_DIR/model_$((NUM_ITERS - 1)).pt \\"
echo "      --num-steps 5000 --num-envs 1 --forward-vel 0.3 --no-reset"
