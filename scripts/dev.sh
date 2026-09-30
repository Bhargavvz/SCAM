#!/usr/bin/env bash
# Development: API with auto-reload on :8100 and Vite hot reload on :5173 (proxying /api)
set -euo pipefail
cd "$(dirname "$0")/.."
(cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload --port 8100) &
cd frontend && npm run dev
