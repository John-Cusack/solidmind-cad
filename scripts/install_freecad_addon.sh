#!/usr/bin/env bash
# Install SolidMind CAD as a FreeCAD addon (symlink into Mod directory).
# After running this, FreeCAD will auto-start the SolidMind socket server
# on every launch — no manual Python console commands needed.

set -euo pipefail

ADDON_DIR="$(cd "$(dirname "$0")/../freecad_addon" && pwd)"
MOD_DIR="${HOME}/.local/share/FreeCAD/Mod"
LINK="${MOD_DIR}/SolidMind"

mkdir -p "${MOD_DIR}"

if [ -L "${LINK}" ]; then
    echo "Symlink already exists: ${LINK} -> $(readlink "${LINK}")"
    echo "Removing old symlink..."
    rm "${LINK}"
elif [ -e "${LINK}" ]; then
    echo "Error: ${LINK} exists and is not a symlink. Remove it manually."
    exit 1
fi

ln -s "${ADDON_DIR}" "${LINK}"
echo "Installed: ${LINK} -> ${ADDON_DIR}"
echo ""
echo "Restart FreeCAD and the SolidMind addon will start automatically."
