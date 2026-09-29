#!/usr/bin/env bash
# Full live evaluation batch; logs in runtime/live_*.log, results in eval/out/ and demo/out/.
# Order matters: the demo writes back into the bank (D5), so it runs last.
cd "$(dirname "$0")/.."
mkdir -p runtime
F='unclosed|client_session|connector|connections'
echo "== pattern probe $(date +%H:%M)"; .venv/bin/python -m eval.run_pattern_probe --mental-models "${MM:-both}" 2>&1 | grep -viE "$F" > runtime/live_probe.log; tail -1 runtime/live_probe.log
echo "== holdout scenarios $(date +%H:%M)"; .venv/bin/python -m eval.run_holdout_scenarios 2>&1 | grep -viE "$F" > runtime/live_holdout.log; tail -1 runtime/live_holdout.log
echo "== eval questions $(date +%H:%M)"; .venv/bin/python -m eval.run_eval_questions --per-type "${PER_TYPE:-5}" 2>&1 | grep -viE "$F" > runtime/live_questions.log; tail -1 runtime/live_questions.log
echo "== demo $(date +%H:%M)"; .venv/bin/python -m demo.run_demo 2>&1 | grep -viE "$F" > runtime/live_demo.log; tail -1 runtime/live_demo.log
echo "== done $(date +%H:%M)"
