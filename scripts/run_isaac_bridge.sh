#!/usr/bin/env bash
set -euo pipefail

# Run the SolidMind Isaac bridge sidecar.
# In an Isaac Sim environment, set ISAAC_PYTHON to Isaac's Python executable.
# Example:
#   ISAAC_PYTHON=/path/to/isaac-sim/python.sh scripts/run_isaac_bridge.sh --port 9878

ISAAC_PYTHON="${ISAAC_PYTHON:-python3}"

# Ensure the entire process tree is killed on exit.  When this script
# is backgrounded (`&`) and later killed, the trap propagates SIGTERM
# to the Isaac python child so it doesn't become an orphan.
cleanup() {
    # Kill all processes in our process group.
    kill -- -$$ 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Default: non-headless with full_warehouse.usd environment.
# Override with --headless or --environment '' as needed.
exec "${ISAAC_PYTHON}" -m isaac_bridge.bridge_server "$@"
