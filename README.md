# SCAM

## Supply Chain Memory & Decision Agent - prototype

Minimal agent for one disruption type (**RM supplier delay**) and three actions (**expedite, switch_supplier, accept_delay**).
On a new delay it recalls history from Hindsight Cloud, checks the DB for open commitments and supplier performance,
simulates each action with deterministic formulas on historical rates, and prints a decision card with cited evidence.

### 1. Hindsight Cloud key
1. Sign up at https://ui.hindsight.vectorize.io/signup and create an API key.
2. `cp .env.example .env` and set `HINDSIGHT_API_KEY` (and optionally `HINDSIGHT_BANK_ID`). Never commit `.env`.

### 2. Setup (one time)
```
uv pip install --python .venv/Scripts/python.exe hindsight-client python-dotenv pandas pyyaml
python run_all.py --scale full            # builds output/ (DB + corpus) if not present
python hindsight_setup.py --dry-run       # ~290 docs, ~27k tokens - check before paying
python hindsight_setup.py                 # creates bank (mission + 2 directives), retains docs in time order, smoke-tests recall()
```

### 3. Run
```
python console.py --as-of 2025-11-19 "Supplier 118 (SUP0118) now quotes 2025-11-30 for RMPO037977. Should we move it to another supplier?"
python console.py --offline ...           # DB + simulator only, no Hindsight calls
python test_scenarios.py [--offline] [--only T3]
```
The report must name the delayed RM PO (`RMPO…`); supplier, RM, plant, qty and ETA are completed from the DB.
All DB reads are read-only and filtered to records dated on/before `--as-of`.

### How numbers are made (never by the LLM)
- accept_delay: arrival = current ETA + the supplier's historical slip after a first revised ETA (same season if n>=3) [`rm_po_revisions`]
- expedite: arrival pulled in by historical pull days x historical expedite success (supplier, else network); fee = qty x price x median fee rate of past expedite decisions (plant, else network) [`decisions`]
- switch_supplier: alternate catalog source lead time + its recent avg delay [`rm_supplier_catalog`, `supplier_scorecard_monthly`]; cost = price difference x qty + 1,200 admin
- stockout days = arrival - days of cover (on-hand / 56-day avg consumption); one stockout day = 10% of the daily FG contribution made with that RM at that plant [`production_runs`, `products_ext`]
- switch is BLOCKED (and flagged) if it would breach an open commitment: an in-flight arrangement on the PO being cancelled, or a contract min-volume that would fall short.

Recall results dated after `--as-of` are dropped. Reflect text is shown as narrative only, with the memory ids it was based on.
Token usage for every retain/recall/reflect call is appended to `logs/hindsight_usage.jsonl` (recall returns no usage, so its size is estimated).
External actions are mocked; `--retain` optionally stores the decision summary back in the bank.

### Memory design (what goes into Hindsight)
- The bank (`scm-memory-conv`) holds only **conversations and discussions** (supplier calls, handovers, escalations,
  decision memos, post-mortems, QBR and forecast-review notes) plus 4 **team playbooks**. It never holds DB records;
  ETAs, prices, commitments and stock always come from SQLite.
- Per query: `recall()` on the playbooks picks **which allowlisted named queries** to run (`queries.py`, read-only,
  parameterised, max 5 rows each; recall never supplies SQL). The agent runs them, then `retain()`s the planner's query plus
  **at most 5 DB rows** and the recommendation, so the next similar question recalls it. Use `--no-retain` to skip.
- Cost seen so far: bank load ~494k tokens (184 items); per query ~4k recall + ~4k retain + ~120k reflect.
