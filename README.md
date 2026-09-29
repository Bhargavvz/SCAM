# Supply Chain Memory & Decision Agent (prototype)

Given a free-text disruption report, the agent:
- recalls history from a Hindsight memory bank;
- connects it to structured ground truth (decisions, commitments, event links, scorecards);
- simulates candidate actions deterministically;
- recommends one with cited evidence;
- can log the decision back, so the next disruption benefits.

It also answers any supply-chain question through seven capabilities: supplier reliability, commitments, decision precedent, exceptions, quality root cause, negotiation, and decision outcome learning.

The dataset (`dataset/`) is a read-only input. See `dataset/README.md`.

## Architecture

```mermaid
flowchart LR
  R[Disruption report] --> P[parse_report]
  P --> S[simulate.py\nport of generator\nconsequence model]
  P --> F{parallel evidence fan-out}
  F --> H1[Hindsight recall: broad]
  F --> H2[Hindsight recall: entity]
  F --> H3[Hindsight recall: 120-day window]
  F --> Q[sql_tools: commitments, scorecard,\nevents, links, prior decisions]
  H1 & H2 & H3 --> AF[as-of filter +\nsupersession via DocIndex]
  AF --> RF[reflect: native if safe,\nelse local synthesis]
  S & AF & RF & Q --> L[Claude tool loop - effort low\nrecall / reflect / doc / sql / simulate]
  L --> G{guardrails:\ncommitment breach, feasibility,\ncitations}
  G -- reject --> L
  G -- accept --> W[Claude rationale - effort high]
  W --> N[number check vs simulator/DB]
  N --> C[Decision card]
  C -- --writeback --> LD[log_decision: decisions_live /\ncommitments_live + Hindsight retain\n+ MOCK external actions]
  A[Any question] --> RT[capabilities.route] --> CH[capability handler:\nSQL ground truth + reflect\nbudget / response_schema] --> CA[Capability answer\n+ verified citations]
  subgraph Data
    DB[(dataset SQLite - read-only\nas-of TEMP views)]
    LV[(live.sqlite - append-only)]
  end
  Q --- DB
  Q --- LV
  S --- DB
  CH --- DB
```

| Layer | Files |
|---|---|
| Config / setup | `agent/config.py`, `agent/setup_data.py`, `agent/check_connectivity.py`, `.env.example` |
| Hindsight bank | `scripts/start_hindsight.sh`, `agent/bank_setup.py` (mission, 8 directives, disposition), `agent/mental_models.py` (12 pattern models) |
| Data | `agent/db.py` (as-of views, append-only live tables), `agent/sql_tools.py` |
| Memory | `agent/corpus.py`, `agent/hindsight_tools.py` |
| Decision | `agent/simulate.py` + `agent/sim_rules.yaml`, `agent/guardrails.py`, `agent/agent_core.py`, `agent/log_decision.py` |
| Capabilities | `agent/capabilities.py` |
| Interface | `interface/decision_console.py` |
| Eval | `eval/run_eval_questions.py`, `eval/run_holdout_scenarios.py`, `eval/run_pattern_probe.py`, `eval/scorecard.py` |
| Demo | `demo/scenarios.yaml`, `demo/run_demo.py`, `demo/EXPECTED.md` |

## Setup

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # then fill the Hindsight endpoint/key and the agent LLM (Claude, or LLM_PROVIDER=groq + GROQ_API_KEY)
.venv/bin/python -m agent.setup_data
```

Hindsight (local Docker: API :8888, UI :9999). The first start pulls the image and ingestion is billed by the server's LLM provider:

```bash
scripts/start_hindsight.sh
.venv/bin/python -m agent.bank_setup
.venv/bin/python dataset/code/hindsight_loader.py --corpus dataset/memory_corpus.jsonl --bank supply-chain-memory --base-url http://localhost:8888 --batch 25 --concurrency 5 --log runtime/retention_log.jsonl
.venv/bin/python -m agent.bank_setup
.venv/bin/python -m agent.mental_models create
.venv/bin/python -m agent.check_connectivity
```

## Run

```bash
.venv/bin/python -m interface.decision_console --scenario HS05 --trace
.venv/bin/python -m interface.decision_console --report "..." --as-of 2025-01-13 --runs PR0016522 [--writeback]
.venv/bin/python -m interface.decision_console --question "What is the latest ETA for RPO003179?" --as-of 2023-06-11
.venv/bin/python -m interface.decision_console --ask "What open commitments do we have with SUP0091?"
.venv/bin/python -m demo.run_demo                      # results: demo/out/demo_report.md
.venv/bin/python -m eval.run_holdout_scenarios
.venv/bin/python -m eval.run_eval_questions --per-type 5 [--mode hindsight] [--all] [--judge]
.venv/bin/python -m eval.run_pattern_probe            # with and without Mental Models
.venv/bin/python -m pytest                             # offline suite
RUN_LIVE=1 .venv/bin/python -m pytest -m live          # needs Hindsight + Claude
```

All eval scripts write into `eval/out/` and regenerate `eval/out/scorecard.md`.

## How the hard rules are enforced

- **Numbers come only from the simulator.**
  - `simulate.py` ports `dataset/code/gen_eval.py::holdout_scenarios` and calls the generator's own `consequences.py`. It reproduces all 14 holdout scenarios' candidate costs (see `tests/test_simulate.py`).
  - The rationale's `$` amounts and "N stockout days" are checked against simulator and DB values. Mismatches become warnings on the card.
- **Citations are verified.**
  - Doc ids must be memory docs the agent actually saw, dated on or before `as_of`.
  - Record ids must exist in the as-of DB.
  - Anything else is listed as "unverifiable", and ungrounded claims are listed explicitly.
- **Commitments are checked before acting.**
  - The simulator flags options that would breach an open volume commitment.
  - The guardrail rejects such a recommendation, and overrides it after two rejections.
  - A proposed action named in the report is pre-checked.
  - Capability questions about switching or cancelling get the open commitments attached, or a warning when no supplier is named.
- **The as-of rule has four layers:**
  - SQL runs on TEMP views that hide or mask rows after `as_of`;
  - Hindsight hits are resolved to corpus docs and dropped if they are future-dated or from the holdout split;
  - native reflect (and the Mental Models) runs only when nothing in the bank can post-date `as_of`, otherwise synthesis is local over filtered recall;
  - the eval counts any future-dated citation.
- **External calls are mocked.** ERP, portal, email, MES and QMS actions are strings prefixed `[MOCK - no external call made]`.
- **Config lives in `.env`.** Bank id, model, paths and effort are all set there; nothing is hard-coded.

## Results

Fill this in from `eval/out/scorecard.md` and `demo/out/demo_report.md` after the live runs (date, model, numbers).

## Known limitations

- The simulator reproduces the generator's consequence model, including four risk rules copied from planted patterns P01/P02/P07/P08 (`agent/sim_rules.yaml`). That is deliberate, for parity, but it means the simulator "knows" those patterns rather than learning them from memory.
- One quirk is kept on purpose for parity. When choosing a donor plant, the generator compares risk labels as strings, so a "high"-risk donor can win over a "medium" one (`_best_transfer`).
- Only 6 of the 9 decision types are simulated. build_safety_stock, renegotiate and reduce_allocation show "not modelled", and the guardrail refuses to recommend them.
- The holdout harness passes each scenario's dependent run ids as MRP context. For free-text reports the agent uses the run ids in the report (or `--runs`); if only the first run is known, the service impact is understated.
- As-of views mask receipts, outcomes, resolutions and negotiation results. Some aggregates still use end-of-window knowledge: otif and fill for completed scorecard months, and cancelled-PO status.
- Native Hindsight reflect (and the Mental Models) runs only when nothing in the bank can post-date `as_of`. Otherwise reflect is recall plus a local synthesis using the bank's mission and directives; `reflect_mode` on each answer says which ran. If runtime retains exist in the bank from another live DB, native reflect is disabled for runs using a different live DB.
- The Mental Models are standing questions built from the answer key's pattern list. The probe reports detection both with and without them.
- Eval answer scoring is a deterministic key-fact match (ids, dates, numbers, status and action words, threshold 0.75). Use `--judge` for a semantic LLM check.
- Current Claude models reject `temperature`, so "low vs normal temperature" is implemented as effort low vs high (Groq gpt-oss: `reasoning_effort` low/high).
- The agent LLM is pluggable (`LLM_PROVIDER=anthropic|groq`). The brief names Claude; this deployment runs Groq `openai/gpt-oss-120b` at the user's request. Groq does not enforce strict tool schemas, so malformed tool calls come back to the model as tool errors. Groq per-token prices are not built in: set `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` for cost reporting, otherwise cost shows as unknown.
- Hindsight runs on Hindsight Cloud (`HINDSIGHT_BASE_URL=https://api.hindsight.vectorize.io`); `scripts/start_hindsight.sh` is only for a local server.
- The corpus has no records for corrosion, humidity, partial-shipment exceptions, customer churn or "cheaper supplier" switches. The corresponding capability demos can only answer "no record".
