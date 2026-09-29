#!/usr/bin/env bash
# On the server: pull the latest code from git and redeploy.  Usage: bash deploy/update.sh [branch]
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
bash deploy/deploy.sh
