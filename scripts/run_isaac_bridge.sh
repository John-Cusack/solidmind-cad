#!/usr/bin/env bash
set -euo pipefail

# Run the SolidMind Isaac bridge sidecar.
# In an Isaac Sim environment, set ISAAC_PYTHON to Isaac's Python executable.
# Example:
#   ISAAC_PYTHON=/path/to/isaac-sim/python.sh scripts/run_isaac_bridge.sh --port 9878

ISAAC_PYTHON="${ISAAC_PYTHON:-python3}"

exec "${ISAAC_PYTHON}" -m isaac_bridge.bridge_server "$@"
