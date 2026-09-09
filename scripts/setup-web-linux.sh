#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools wheel
.venv/bin/pip install -e '.[dev]'
if ! .venv/bin/python -c 'import greenlet, playwright.sync_api' >/dev/null 2>&1; then
  .venv/bin/pip uninstall -y greenlet
  .venv/bin/pip install --no-binary=greenlet greenlet
fi
.venv/bin/python -c 'import fastapi, uvicorn, playwright.sync_api, cryptography; print("TH Media web dependencies: OK")'
ffmpeg -version | head -n 1
