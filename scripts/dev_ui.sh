#!/usr/bin/env bash
# Development mode: API with auto-reload on :8000, Vite dev server with hot reload on http://localhost:5173
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d web/node_modules ]; then (cd web && npm install); fi
.venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload &
API_PID=$!
trap 'kill $API_PID 2>/dev/null' EXIT
cd web && npm run dev
