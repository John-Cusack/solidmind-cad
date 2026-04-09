#!/usr/bin/env bash
# Build the solidmind-cad worker Docker image.
#
# Usage: ./docker/build.sh
#
# Copies the FreeCAD AppImage into the build context (Docker COPY
# can't follow symlinks outside the context), then builds the image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Find the FreeCAD AppImage
APPIMAGE="${FREECAD_APPIMAGE:-$REPO_ROOT/FreeCAD_1.0.2-conda-Linux-x86_64-py311.AppImage}"

if [ -L "$APPIMAGE" ]; then
    APPIMAGE="$(readlink -f "$APPIMAGE")"
fi

if [ ! -f "$APPIMAGE" ]; then
    echo "ERROR: FreeCAD AppImage not found at $APPIMAGE"
    echo "Set FREECAD_APPIMAGE to the correct path."
    exit 1
fi

echo "Using FreeCAD AppImage: $APPIMAGE ($(du -h "$APPIMAGE" | cut -f1))"

# Copy into docker/ build context (if not already there)
DEST="$SCRIPT_DIR/FreeCAD.AppImage"
if [ ! -f "$DEST" ] || [ "$APPIMAGE" -nt "$DEST" ]; then
    echo "Copying AppImage to docker/ ..."
    cp "$APPIMAGE" "$DEST"
fi

echo "Building solidmind-worker image..."
docker build \
    -t solidmind-worker \
    -f "$SCRIPT_DIR/Dockerfile.worker" \
    "$REPO_ROOT"

echo ""
echo "Build complete. Run with:"
echo "  docker run -p 8080:8080 solidmind-worker"
echo ""
echo "Or use docker compose:"
echo "  docker compose -f docker/docker-compose.yml up -d"
