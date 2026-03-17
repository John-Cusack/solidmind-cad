#!/usr/bin/env bash
set -euo pipefail

# Launch the Isaac bridge and start the hexapod teleop demo.
#
# Usage:
#   scripts/run_hexapod_teleop.sh              # start bridge + teleop
#   scripts/run_hexapod_teleop.sh --keyboard   # also launch keyboard control
#
# Prerequisites:
#   - Isaac Sim built from source in ../isaacsim/
#   - URDF + STLs in hexapod_sim_pkg/
#
# To stop: Ctrl-C (kills bridge), or send teleop_stop via TCP.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$REPO_DIR/hexapod_sim_pkg/teleop_config.json"
ISAAC_PYTHON="${ISAAC_PYTHON:-$REPO_DIR/../isaacsim/_build/linux-x86_64/release/python.sh}"
BRIDGE_PORT=9878
KEYBOARD="${1:-}"

cd "$REPO_DIR"

echo "=== Hexapod Teleop Demo ==="
echo "Config: $CONFIG"
echo ""

# 1. Start Isaac bridge in background
echo "[1/3] Starting Isaac bridge (this takes ~60s for Kit init)..."
"$ISAAC_PYTHON" -m isaac_bridge.bridge_server --no-headless --log-level INFO &
BRIDGE_PID=$!
trap "kill $BRIDGE_PID 2>/dev/null; wait $BRIDGE_PID 2>/dev/null" EXIT

# Wait for bridge to be ready
echo "      Waiting for bridge on port $BRIDGE_PORT..."
for i in $(seq 1 120); do
    if python3 -c "
import socket, json, sys
try:
    with socket.create_connection(('127.0.0.1', $BRIDGE_PORT), timeout=2) as s:
        s.settimeout(5)
        s.sendall(b'{\"cmd\":\"ping\",\"args\":{}}\n')
        buf = b''
        while b'\n' not in buf:
            buf += s.recv(4096)
        r = json.loads(buf.split(b'\n',1)[0])
        sys.exit(0 if r.get('ok') else 1)
except: sys.exit(1)
" 2>/dev/null; then
        echo "      Bridge ready!"
        break
    fi
    sleep 1
done

# 2. Start teleop session
echo "[2/3] Starting teleop session..."
SESSION_ID=$(python3 -c "
import json, socket, sys, os
config = json.loads(open('$CONFIG').read())
urdf = os.path.abspath(config['urdf_path'])
payload = json.dumps({'cmd': 'teleop_start', 'args': {
    'mechanism': config['mechanism'],
    'urdf_path': urdf,
    'import_config': config['import_config'],
    'profile': config['profile']
}}) + '\n'
with socket.create_connection(('127.0.0.1', $BRIDGE_PORT), timeout=10) as s:
    s.settimeout(180)
    s.sendall(payload.encode())
    buf = b''
    while b'\n' not in buf:
        chunk = s.recv(65536)
        if not chunk: break
        buf += chunk
    r = json.loads(buf.split(b'\n',1)[0])
    if r.get('ok'):
        print(r['result']['session_id'])
    else:
        print('ERROR: ' + json.dumps(r.get('error')), file=sys.stderr)
        sys.exit(1)
")

echo "      Session: $SESSION_ID"

# 3. Send initial walk command
echo "[3/3] Sending walk command..."
python3 -c "
import json, socket
config = json.loads(open('$CONFIG').read())
cmd = config.get('walk_command', {'vx_mps': 0.3, 'yaw_rate_rps': 0, 'body_height_m': 0})
cmd['session_id'] = '$SESSION_ID'
payload = json.dumps({'cmd': 'teleop_command', 'args': cmd}) + '\n'
with socket.create_connection(('127.0.0.1', $BRIDGE_PORT), timeout=10) as s:
    s.settimeout(30)
    s.sendall(payload.encode())
    buf = b''
    while b'\n' not in buf:
        buf += s.recv(65536)
    print('Walk command sent!')
"

echo ""
echo "=== Hexapod is walking! ==="
echo "Session: $SESSION_ID"
echo ""
echo "Controls:"
echo "  - Use scripts/isaac_keyboard_teleop.py for W/A/S/D control"
echo "  - Or send commands via: motion.teleop_command"
echo "  - Press Ctrl-C to stop"
echo ""

if [ "$KEYBOARD" = "--keyboard" ]; then
    echo "Starting keyboard control..."
    python3 scripts/isaac_keyboard_teleop.py
else
    echo "Bridge running (PID $BRIDGE_PID). Press Ctrl-C to stop."
    wait $BRIDGE_PID
fi
