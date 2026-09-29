#!/usr/bin/env bash
# Start (or reuse) a local Hindsight server: API on :8888, Control Plane UI on :9999, data in volume hindsight-data.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${HINDSIGHT_API_LLM_API_KEY:?set HINDSIGHT_API_LLM_API_KEY in .env (the Hindsight server's own LLM key)}"
if docker ps -a --format '{{.Names}}' | grep -qx hindsight; then
  docker start hindsight >/dev/null
else
  docker run -d --name hindsight --restart unless-stopped --shm-size=1g -p 8888:8888 -p 9999:9999 \
    -e HINDSIGHT_API_LLM_PROVIDER="${HINDSIGHT_API_LLM_PROVIDER:-openai}" \
    -e HINDSIGHT_API_LLM_API_KEY="$HINDSIGHT_API_LLM_API_KEY" \
    -e HINDSIGHT_API_WORKER_ID="${HINDSIGHT_API_WORKER_ID:-sc-memory-worker-1}" \
    -v hindsight-data:/home/hindsight/.pg0 \
    ghcr.io/vectorize-io/hindsight:latest >/dev/null
fi
for _ in $(seq 1 90); do
  if curl -s -o /dev/null http://localhost:8888/; then echo "Hindsight API up on :8888, UI on :9999"; exit 0; fi
  sleep 2
done
echo "Hindsight did not answer on :8888 within 3 minutes - see: docker logs hindsight" >&2
exit 1
