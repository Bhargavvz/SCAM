#!/usr/bin/env bash
# Resume memory ingestion (skips docs already logged ok), re-apply bank config, create the 12 pattern Mental Models,
# then run the Stage 0 checks. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
F='unclosed|client_session|connector|connections'
.venv/bin/python dataset/code/hindsight_loader.py --corpus dataset/memory_corpus.jsonl --bank "$HINDSIGHT_BANK_ID" \
  --base-url "$HINDSIGHT_BASE_URL" ${HINDSIGHT_API_KEY:+--api-key "$HINDSIGHT_API_KEY"} \
  --batch 25 --concurrency 5 --log runtime/retention_log.jsonl 2>&1 | grep -viE "$F"
echo "failed retains still in log: $(grep -vc '"status": "ok"' runtime/retention_log.jsonl || true)"
.venv/bin/python -m agent.bank_setup 2>&1 | grep -viE "$F"
.venv/bin/python -m agent.mental_models create 2>&1 | grep -viE "$F"
.venv/bin/python -m agent.check_connectivity 2>&1 | grep -viE "$F"
