#!/usr/bin/env bash
# Copy the project (including .env) from this machine to the Oracle instance.
# Usage: deploy/sync_to_server.sh ubuntu@<PUBLIC_IP> [~/.ssh/oracle_key]
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:?usage: deploy/sync_to_server.sh ubuntu@<PUBLIC_IP> [ssh_key]}"
KEY="${2:-}"
SSH="ssh${KEY:+ -i $KEY}"
rsync -az --delete -e "$SSH" \
  --exclude .git --exclude .venv --exclude .superpowers --exclude __pycache__ --exclude .pytest_cache \
  --include runtime/ --include runtime/retention_log.jsonl --exclude 'runtime/*' \
  --exclude data --exclude web/node_modules --exclude web/dist \
  ./ "$TARGET:~/sc_memory_extension/"
echo "Synced. On the server: cd ~/sc_memory_extension && bash deploy/deploy.sh"
