#!/bin/bash
# Optional: install systemd user service for Chrono daemon.
# Skip if systemd is not available or user doesn't want it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="${HOME}/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

# Generate service file with correct paths
sed "s|%h|${HOME}|g" "${SCRIPT_DIR}/chrono-daemon.service" > "${SERVICE_DIR}/chrono-daemon.service"

systemctl --user daemon-reload
echo "Installed. Use:"
echo "  systemctl --user start chrono-daemon    # start now"
echo "  systemctl --user enable chrono-daemon   # auto-start on login"
echo "  journalctl --user -u chrono-daemon -f   # view logs"
