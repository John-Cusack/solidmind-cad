#!/usr/bin/env bash
# Launch RL training for SolidMind CAD.
#
# Usage:
#   scripts/run_rl_training.sh --env-config <path> [--output-dir <path>] [--max-iterations N]
#
# If ISAAC_PYTHON is set, uses that Python interpreter (for Isaac Lab).
# Otherwise falls back to the system Python.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Prefer Isaac Lab Python if available
if [[ -n "${ISAAC_PYTHON:-}" ]] && [[ -x "$ISAAC_PYTHON" ]]; then
    PYTHON="$ISAAC_PYTHON"
    echo "Using Isaac Lab Python: $PYTHON"
else
    PYTHON="${PYTHON:-python3}"
    echo "Using system Python: $PYTHON (set ISAAC_PYTHON for Isaac Lab)"
fi

cd "$PROJECT_ROOT"

exec "$PYTHON" -m rl_training.train "$@"
