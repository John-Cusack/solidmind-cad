#!/usr/bin/env bash
set -euo pipefail

# Run the SolidMind Gazebo bridge sidecar.
#
# Examples:
#   scripts/run_gazebo_bridge.sh --runtime real --world default
#   scripts/run_gazebo_bridge.sh --launch-gz --world default --port 9879
#   scripts/run_gazebo_bridge.sh --runtime stub

GZ_PID=""

cleanup() {
    if [[ -n "$GZ_PID" ]]; then
        kill "$GZ_PID" 2>/dev/null || true
        wait "$GZ_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

HOST="127.0.0.1"
PORT="9879"
RUNTIME="${SOLIDMIND_GAZEBO_RUNTIME:-real}"
WORLD="default"
LAUNCH_GZ=false
ENABLE_PX4=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime)
            RUNTIME="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --world)
            WORLD="$2"
            shift 2
            ;;
        --launch-gz)
            LAUNCH_GZ=true
            shift
            ;;
        --enable-px4)
            ENABLE_PX4=true
            shift
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ "$RUNTIME" != "real" && "$RUNTIME" != "stub" ]]; then
    echo "ERROR: --runtime must be 'real' or 'stub' (got '$RUNTIME')." >&2
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required to run gazebo_bridge.bridge_server." >&2
    exit 1
fi

if [[ "$LAUNCH_GZ" == "true" || "$RUNTIME" == "real" ]]; then
    if ! command -v gz &>/dev/null; then
        echo "ERROR: 'gz' CLI not found. Install Gazebo Harmonic or use --runtime stub." >&2
        exit 1
    fi
fi

if [[ "$ENABLE_PX4" == "true" && "${SOLIDMIND_GAZEBO_PX4_FAKE:-}" != "1" ]]; then
    PX4_BIN="${SOLIDMIND_PX4_BIN:-px4}"
    if ! command -v "$PX4_BIN" &>/dev/null; then
        echo "ERROR: PX4 requested but '$PX4_BIN' is unavailable." >&2
        echo "Set SOLIDMIND_PX4_BIN or SOLIDMIND_GAZEBO_PX4_FAKE=1." >&2
        exit 1
    fi
fi

if [[ "$LAUNCH_GZ" == "true" ]]; then
    echo "Starting Gazebo server in background..."
    if [[ -f "$WORLD" ]]; then
        gz sim -s "$WORLD" &
    else
        gz sim -s &
    fi
    GZ_PID=$!
    sleep 2
fi

export SOLIDMIND_GAZEBO_RUNTIME="$RUNTIME"

BRIDGE_ARGS=(
    --host "$HOST"
    --port "$PORT"
    --runtime "$RUNTIME"
    --world "$WORLD"
)
if [[ "$ENABLE_PX4" == "true" ]]; then
    BRIDGE_ARGS+=(--enable-px4)
fi
BRIDGE_ARGS+=("${EXTRA_ARGS[@]}")

exec python3 -m gazebo_bridge.bridge_server "${BRIDGE_ARGS[@]}"
