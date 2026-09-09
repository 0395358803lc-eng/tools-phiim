#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="/home/runner/workspace/tools-phiim"
ENV_FILE="/home/runner/workspace/thmedia-data/thmedia.env"
cd "$PROJECT_ROOT"
set -a
source "$ENV_FILE"
set +a
exec "$PROJECT_ROOT/.venv/bin/python" -m flow_story_studio.main
