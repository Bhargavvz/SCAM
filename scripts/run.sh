#!/usr/bin/env bash
# Build the database (first run), build the UI when sources changed, and serve Meridian on http://localhost:8100
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || { python3.12 -m venv .venv && .venv/bin/pip install -q -r backend/requirements.txt; }
[ -f data/meridian.sqlite ] || (cd backend && ../.venv/bin/python -m app.build_db)
if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src frontend/index.html -newer frontend/dist/index.html 2>/dev/null)" ]; then
  (cd frontend && { [ -d node_modules ] || npm install; } && npm run build)
fi
cd backend && exec ../.venv/bin/python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8100}"
