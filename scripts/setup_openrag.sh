#!/usr/bin/env bash
# Bootstrap script for the OpenRAG knowledge backend.
# Usage: bash scripts/setup_openrag.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="$PROJECT_DIR/docker"

echo "=== SolidMind CAD — OpenRAG Setup ==="

# 1. Check Docker is available
if ! command -v docker &>/dev/null; then
    echo "ERROR: docker is not installed or not in PATH."
    exit 1
fi
if ! docker compose version &>/dev/null; then
    echo "ERROR: docker compose plugin is not available."
    exit 1
fi

# 2. Copy .env.example -> .env if missing
if [ ! -f "$DOCKER_DIR/.env" ]; then
    cp "$DOCKER_DIR/.env.example" "$DOCKER_DIR/.env"
    echo "Created docker/.env from .env.example — edit it to add API keys."
fi

# 3. Start services
echo "Starting OpenRAG stack..."
docker compose -f "$DOCKER_DIR/docker-compose.yml" up -d

# 4. Wait for health
echo "Waiting for OpenRAG to become healthy..."
MAX_WAIT=120
WAITED=0
until curl -sf http://localhost:8080/health &>/dev/null; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "WARNING: OpenRAG did not become healthy within ${MAX_WAIT}s."
        echo "Check logs: docker compose -f docker/docker-compose.yml logs"
        exit 1
    fi
done
echo "OpenRAG is healthy."

# 5. Ingest existing knowledge notes
NOTES_DIR="$PROJECT_DIR/me_knowledge/notes"
if [ -d "$NOTES_DIR" ] && [ "$(ls -A "$NOTES_DIR"/*.md 2>/dev/null)" ]; then
    echo "Ingesting existing research notes from me_knowledge/notes/..."
    python3 "$SCRIPT_DIR/ingest_knowledge.py" "$NOTES_DIR"
else
    echo "No existing research notes found in me_knowledge/notes/ — skipping initial ingestion."
fi

echo "=== Setup complete ==="
