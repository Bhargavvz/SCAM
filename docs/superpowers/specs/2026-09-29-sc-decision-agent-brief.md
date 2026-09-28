# Supply Chain Memory & Decision Agent - prototype brief (verbatim from the request, 2026-09-29)

## ROLE
You are a senior AI/full-stack engineer. Build a working PROTOTYPE (demonstrable, not production-hardened) of the Supply Chain Memory & Decision Agent, using the synthetic dataset and Hindsight memory bank already built in the prior project.

## PROJECT CONTEXT
Problem: modern supply chains generate fragmented records (POs, movements, negotiations, decisions) across suppliers, inventory, and disruptions. Traditional systems track events; RAG retrieves similar documents; neither maintains an evolving understanding of what happened, why, what was decided, what was promised, and what the outcome was. This agent must do that: on a new disruption, recall historical context, connect related events and open commitments, evaluate the consequences of candidate actions, and recommend or initiate a response.

## EXISTING ASSETS (read-only inputs to this project)
- Structured DB: original inventory-supply-chain tables + generated tables (raw materials, BOM, RM ops, production_runs, product_demand_weekly, disruption_events, event_impacts, event_links, negotiations, decisions, commitments, supplier_scorecard_monthly) as CSV/Parquet + a SQLite copy.
- memory_corpus.jsonl: narrative docs (doc_id, timestamp, context, content, doc_type, entity_ids, source_record_ids, split memory|holdout).
- A Hindsight bank already populated (via hindsight_loader.py) with split=memory docs, retained in chronological order, with a mission and directives already configured.
- eval_questions.jsonl (fact recall / temporal / causal-why / commitment tracking / precedent-analogy / consequence evaluation / contradiction-supersession / multi-hop), each with as_of_date and gold answers/citations.
- holdout_scenarios.json: 12-15 disruptions (2+ with a misleading "most similar" precedent) with gold best action and reasoning.
- planted_patterns.json: known recurring patterns the agent should be able to surface as precedent.

## GOAL OF THE PROTOTYPE
Given a new disruption description, demonstrate end to end that the agent:
1. Recalls relevant history from Hindsight (not just the single most-similar doc).
2. Connects it to structured ground truth (past event_links, decisions, commitments, scorecards) - not narrative alone.
3. Evaluates 2+ candidate actions with a DETERMINISTIC simulator (expected cost, expected stockout days, service impact) - never numbers invented by the LLM.
4. Recommends an action with a rationale that cites specific doc_ids and record_ids.
5. Optionally "initiates" the response: writes a new decision + commitment row and retains a summary back into Hindsight, so the next disruption benefits from this one.
It must also visibly handle: a trap scenario (rejects the misleading precedent), an open-commitment conflict (catches a directive violation before answering), and a contradiction/supersession (uses the later fact, not the earlier one).

## ARCHITECTURE
- Data layer: DuckDB or SQLite over the existing tables, read-only, PLUS append-only `decisions_live` / `commitments_live` tables the agent writes to at runtime (so demo runs never corrupt the eval ground truth).
- Memory layer: the existing Hindsight bank. Use recall for retrieval, reflect for synthesis (guided by the bank's mission/directives), retain for anything the agent logs at runtime.
- Orchestrator ("agent core"): Claude via the Anthropic API, tool-use loop, low temperature for structured steps (option enumeration, tool calls), normal temperature for the final written rationale. Tools:
  - hindsight_recall(query, filters?) - semantic + keyword + graph + temporal
  - hindsight_reflect(question) - synthesis over retrieved memory
  - sql_query(query) - read-only: open commitments, recent scorecard, prior event_links, current stock/lead time
  - simulate_action(action_type, params) - deterministic what-if; reuse the exact consequence formulas from the dataset generator's gen_events.py so a given input always produces the same output
  - log_decision(...) - writes decision_live + commitment_live rows, retains an experience-fact summary with a real timestamp
- Interface: pick ONE for the prototype - a CLI/chat console is enough. Input: a free-text disruption report (optionally with as_of_date for eval mode). Output: a structured "decision card" - situation summary, cited precedents (with doc/record links), a candidate-options table (simulated cost/stockout/risk per option), recommended option + rationale, any open commitments flagged, confidence.
- Guardrail enforcement, not just a stated directive: include a runnable test case where the naive action would breach an open commitment, and show the agent's commitments lookup catching it before it recommends.

## CORE AGENT LOOP
1. Ingest disruption report.
2. Recall: parallel Hindsight recalls (broad semantic + entity/graph-anchored + temporal window) plus SQL lookups on the same entities (open commitments, recent scorecard, related event_links).
3. Reflect: synthesize what happened before, what was tried, what worked or failed and why, what's still open.
4. Enumerate candidate actions from the existing decision_type taxonomy (expedite, switch_supplier, substitute_rm, reallocate_stock, cancel_po, build_safety_stock, renegotiate, accept_delay, reduce_allocation).
5. Simulate each candidate's consequences deterministically.
6. Recommend, citing memory (doc_ids) and structured evidence (record_ids); explicitly surface any relevant open commitment or contract clause.
7. (Optional) Write back decision + commitment rows and retain a summary, closing the loop.

## EVALUATION HARNESS
- run_eval_questions.py: for each question, respect as_of_date (only memory retained on/before that date is visible - filter by timestamp or use a snapshot bank per cutoff). Score fact-recall/temporal/causal/commitment/precedent/consequence/contradiction/multi-hop against gold_answer, plus citation precision/recall vs gold_doc_ids/gold_record_ids.
- run_holdout_scenarios.py: run the full agent loop on all 12-15 scenarios, compare chosen option to gold best action, report accuracy overall and specifically on the trap scenarios.
- Output a scorecard: accuracy by question type, trap pass/fail, citation precision/recall, latency and token cost per query.

## HARD RULES
- Mock any external system call (ERP, email, supplier portal) as a clearly labeled no-op - never claim a real action happened.
- The LLM never invents cost/stockout/consequence numbers - those come only from simulate_action.
- Every recommendation must cite evidence it can actually trace back to; if it can't ground a claim, it says so rather than asserting it.
- Never let eval or the live agent read narrative docs dated after the query's as_of_date.
- No hardcoded secrets - bank_id, model name, DB path all in config/.env.

## DELIVERABLES
1. agent/ - agent_core.py, hindsight_tools.py, sql_tools.py, simulate.py, config.
2. interface/ - decision_console.py (CLI or minimal Streamlit) rendering the decision card.
3. eval/ - run_eval_questions.py, run_holdout_scenarios.py, scorecard output.
4. demo/ - a scripted 3-5 scenario walkthrough (including one trap, one commitment-conflict) with expected vs. actual output.
5. README - architecture diagram, setup, how to run the demo and the eval, known limitations.

## WORKFLOW (validate before moving to the next stage)
Stage 0: wire up config; confirm Hindsight connectivity (recall a known fact) and DB connectivity (query a known table).
Stage 1: build and unit-test the tools, especially simulate.py against known historical outcomes.
Stage 2: build agent_core on one hardcoded scenario end to end, printing the full trace.
Stage 3: build the interface around agent_core.
Stage 4: build and run the eval harness; produce the scorecard.
Stage 5: package the demo script and README.

---

## Findings recorded while planning (2026-09-29) - these refine the brief

- The consequence formulas the brief attributes to `gen_events.py` actually live in `dataset/code/consequences.py` (cost functions) and `dataset/code/gen_eval.py::holdout_scenarios` (option construction). A port over the public SQLite tables reproduced every `candidate_actions` entry in `holdout_scenarios.json` (0 mismatches, cost within the generator's rounding of +/-1 USD) and the gold best action in 14/14 scenarios.
- The "slipped lot" whose value drives expedite/transfer/cancel costs is every RPO for the same RM and plant revised on the same day as the named RPO, not only the named RPO.
- None of the 14 holdout scenarios contains a volume commitment that `cancel_po` would breach. A real historical case does: EVT01987 / RPO016183 / PR0016522 as of 2025-01-13, where cancelling would breach open volume commitment CMT00560 with SUP0169. It is the commitment-conflict demo.
- Current Claude models (Opus 5, Sonnet 5, Opus 4.7+) reject `temperature` with HTTP 400. "Low temperature for structured steps / normal for rationale" is implemented as `effort` low vs high plus deterministic code paths; a temperature is only sent when explicitly configured for a model that accepts it.
- Hindsight `recall` accepts `query_timestamp` / `temporal_window` and returns `document_id` and `mentioned_at`; `reflect` has no date filter. The as-of rule is therefore enforced client-side (results resolved to corpus doc_ids and dropped if dated after as_of), and native reflect runs only when nothing in the bank can be newer than as_of.
- No Hindsight server answered on localhost:8888 during planning; the bank location is configuration and Stage 0 must confirm it.
