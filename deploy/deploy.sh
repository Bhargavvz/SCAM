#!/usr/bin/env bash
# Build and (re)start the stack on the server. Safe to re-run after every code update.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env - copy it from your machine (see DEPLOY.md)"; exit 1; }
mkdir -p runtime && sudo chown -R 1000:1000 runtime   # the container runs as uid 1000
docker compose up -d --build
docker compose ps
echo "Logs: docker compose logs -f app"
