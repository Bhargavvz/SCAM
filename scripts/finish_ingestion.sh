#!/usr/bin/env bash
# Resume memory ingestion with live progress; if Hindsight accepts writes, finish the bank setup:
# re-apply mission/directives, create the 12 pattern Mental Models, run the Stage 0 checks. Safe to re-run.
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
F='unclosed|client_session|connector|connections'

.venv/bin/python -m agent.ingest "$@" 2>&1 | grep --line-buffered -viE "$F"
status=${PIPESTATUS[0]}
if [ "$status" -eq 2 ]; then
  echo "Stopped: Hindsight writes are failing (see message above). Nothing else was changed."
  exit 2
fi

.venv/bin/python -m agent.bank_setup 2>&1 | grep --line-buffered -viE "$F"
.venv/bin/python -m agent.mental_models create 2>&1 | grep --line-buffered -viE "$F"
.venv/bin/python -m agent.check_connectivity 2>&1 | grep --line-buffered -viE "$F"
