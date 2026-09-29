#!/usr/bin/env bash
# Build the React UI (first run: npm install + build) and serve UI + API on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d web/node_modules ]; then (cd web && npm install); fi
if [ ! -f web/dist/index.html ] || [ -n "$(find web/src web/index.html -newer web/dist/index.html 2>/dev/null)" ]; then
  (cd web && npm run build)
fi
echo "Open http://localhost:8000"
exec .venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port "${PORT:-8000}"
