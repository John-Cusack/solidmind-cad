#!/usr/bin/env bash
# Watch the trained hexapod walk in Isaac Sim GUI.
#
# Usage: examples/hexapod_robot/watch_walking.sh [run_id] [num_steps]
#
# Defaults:
#   run_id    = hex18_pub_demo_500 (then hex18_pub_demo as fallback)
#   num_steps = 3000 (~60 simulated seconds)
#
# Requirements:
#   - FreeCAD addon NOT required for this script (Isaac-only)
#   - Isaac Sim source build at ~/repos/isaacsim/
#   - Trained checkpoint at training_runs/<run_id>/model_*.pt
#   - X display ($DISPLAY) reachable
#
# Run from repo root.

set -euo pipefail

ISAAC_PY="${ISAAC_PYTHON:-$HOME/repos/isaacsim/_build/linux-x86_64/release/python.sh}"
RUN_ID="${1:-}"
NUM_STEPS="${2:-3000}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -x "$ISAAC_PY" ]; then
    echo "ERROR: Isaac Python not found at $ISAAC_PY" >&2
    echo "  Set ISAAC_PYTHON env var to override." >&2
    exit 1
fi

# Auto-pick a run_id if not given. Prefer the 500-iter run; fall back to 100.
if [ -z "$RUN_ID" ]; then
    if [ -f "training_runs/hex18_pub_demo_500/model_499.pt" ]; then
        RUN_ID="hex18_pub_demo_500"
    elif [ -f "training_runs/hex18_pub_demo/model_99.pt" ]; then
        RUN_ID="hex18_pub_demo"
    else
        echo "ERROR: no trained checkpoint found in training_runs/hex18_pub_demo_500/ or hex18_pub_demo/" >&2
        echo "  Train first with:" >&2
        echo "    \$ISAAC_PYTHON -m rl_training.isaaclab_train \\" >&2
        echo "      --env-config training_runs/hex18_perf_100/env_config_patched.py \\" >&2
        echo "      --output-dir training_runs/hex18_pub_demo_500 \\" >&2
        echo "      --max-iterations 500 --num-envs 4096" >&2
        exit 1
    fi
fi

# Pick the highest-iter checkpoint in the chosen run.
CKPT=$(ls "training_runs/$RUN_ID"/model_*.pt 2>/dev/null \
       | grep -v "model_0.pt$" \
       | sort -V \
       | tail -1)
if [ -z "$CKPT" ]; then
    echo "ERROR: no model_*.pt checkpoint in training_runs/$RUN_ID/" >&2
    exit 1
fi

echo "Run id:       $RUN_ID"
echo "Checkpoint:   $CKPT"
echo "Step count:   $NUM_STEPS"
echo "Display:      ${DISPLAY:-(not set — Isaac will fail to open a window)}"
echo
echo "Launching Isaac Sim GUI (~30 s startup, then a window opens)..."
echo

exec "$ISAAC_PY" scripts/eval_policy_isaac.py \
    --env-config training_runs/hex18_perf_100/env_config_patched.py \
    --checkpoint "$CKPT" \
    --num-steps "$NUM_STEPS" \
    --num-envs 1 \
    --forward-vel 0.3 \
    --no-reset
