#!/bin/bash
# Wrapper script for chrono_daemon with LD_LIBRARY_PATH setup.
# Usage: chrono_daemon/run.sh [--port 9878]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHRONO_LIB_DIR="${CHRONO_LIB_DIR:-/usr/local/lib}"
export LD_LIBRARY_PATH="${CHRONO_LIB_DIR}:${LD_LIBRARY_PATH:-}"
exec "${SCRIPT_DIR}/build/chrono_daemon" "$@"
