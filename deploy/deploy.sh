#!/usr/bin/env bash
# Build and (re)start the stack on the server. Safe to re-run after every code update.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env - copy it from your machine: scp .env ubuntu@<IP>:$(pwd)/.env"; exit 1; }
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER compose version >/dev/null 2>&1 || { echo "docker compose plugin missing: sudo apt-get install -y docker-compose-plugin"; exit 1; }
mkdir -p runtime
# seed the ingestion log (doc ids + statuses only) so the Overview page can count stored memory documents
[ -f runtime/retention_log.jsonl ] || cp deploy/seed/retention_log.jsonl runtime/retention_log.jsonl
sudo chown -R 1000:1000 runtime   # the container runs as uid 1000
$DOCKER compose up -d --build
$DOCKER compose ps
echo "Logs: $DOCKER compose logs -f app"
