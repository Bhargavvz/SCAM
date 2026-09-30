# Meridian — supply chain management system

A working supply chain management system on the synthetic supply chain dataset. It covers the whole flow:
**Supplier → Procurement → Inventory/Warehouse → Production → Shipping → Customer**. Every screen reads live data, every
action writes to the database with an audit trail, and AI insights explain what changed and what to do.

```bash
scripts/run.sh          # first run: venv, database build (~30 s), UI build; then http://localhost:8100
scripts/dev.sh          # API with reload on :8100 + Vite hot reload on :5173
```

Configuration lives in `.env`: `GROQ_API_KEY` (AI features), `APP_TODAY` (business date, default 2025-12-31, the last day of the
dataset), and optionally `UI_USER` / `UI_PASSWORD` / `UI_SESSION_SECRET` to require sign-in.

## Modules

| # | Module | What you can do |
|---|---|---|
| 1 | **Products / SKUs** | 2,500 SKUs by category, brand and ABC class. Stock by warehouse, 52-week demand with forecast, BOM, POs, orders, return rate. Edit price and reorder point. |
| 2 | **Inventory** | Live stock positions (on hand, allocated, available, min/max, on order, weeks of cover, status), the full movement ledger, raw materials by plant. Post adjustments and inter-warehouse transfers. |
| 3 | **Suppliers** | 250 suppliers with tier, OTIF, delay, quality, spend, lead-time trend, contracts, catalog, negotiations, commitments, disruptions. |
| 4 | **Purchasing** | Finished-goods POs to warehouses and raw-material POs to plants. Create (draft or issue), receive in full or partially, cancel (blocked when it would breach a volume commitment). Reorder suggestions → pre-filled PO. |
| 5 | **Sales orders** | Customers and orders. Create an order with live availability, then **confirm → allocate → pick → pack → ship → deliver**. Allocation backorders what is short, and shipping posts stock and creates a tracked shipment. |
| 6 | **Warehouses** | 12 DCs: utilization, receiving queue, pick & pack queue, shipping, floor tasks (put-away, pick, inspect) you can complete. |
| 7 | **Demand forecasting** | Holt-Winters for regular series and Syntetos-Boylan for intermittent SKUs, at total/category/warehouse/product level, with an 80% band. Each is backtested over 13 weeks against the planner forecast. |
| 8 | **Logistics** | 48k inbound/outbound shipments, tracking timelines, carrier scans, carrier on-time trends, lanes and freight cost. |
| 9 | **Production** | Plant load, material shortages, delay causes. Run detail checks BOM × quantity against plant stock. Schedule, start, complete and reschedule runs. |
| 10 | **Returns** | RMAs by reason and disposition. Authorize from an order line, receive (creates an inspection task), then restock (stock returns), replace (no-charge order), refurbish or scrap. |
| 11 | **Alerts** | 13 live detectors: stock-outs, low stock, overdue/stale POs, slipped material POs, material shortages for upcoming runs, late shipments, supplier drops, disruptions, late orders, backorders, returns, expiring contracts. They re-evaluate after every write and resolve themselves when the condition clears. Acknowledge, resolve or reopen. |
| 12 | **Reports & dashboards** | Operations dashboard with the flow; sales, inventory, costs and supplier reports, each table exportable as CSV. |
| + | **AI Insights** | Detectors find what changed (carrier decline, return spikes, supplier OTIF drops, replenishment vs demand, forecast bias, demand shifts, overdue POs, material risk, customers slipping). Groq writes the daily briefing and per-finding action plans from those numbers only; any figure not in the evidence is flagged. |
| + | **Ask Meridian** | Natural-language questions answered by read-only SQL on the live database (write statements are blocked by an authorizer). Every query it ran is shown. |
| + | **Risk & disruptions** | Disruption events. The memory-backed decision agent recommends a response, and accepting it executes the change. |
| + | **Data & sync** | Proves the database is the dataset plus audited app changes (see below), and how the memory corpus maps onto it. |

## Memory (Hindsight)

Meridian's long-term memory is a Hindsight bank (`HINDSIGHT_BASE_URL`, `HINDSIGHT_API_KEY`, `HINDSIGHT_BANK_ID` or
`MERIDIAN_BANK_ID` in `.env`). It holds the 2,500-document supply chain corpus plus every change made in Meridian.

**Keeping memory in sync with the database.** `app.db.audit()` writes each change and its memory document to `memory_outbox`
in the same SQLite transaction (a transactional outbox), so no committed change can be missing from the queue. A background
worker (`app/memory/outbox.py`) sends queued documents with **async `retain_batch`**, including tags (`source:meridian`,
`module:*`, `entity:*`), entities, metadata and the business-date timestamp. It then follows each Hindsight **operation** until it
completes and retries failures with backoff (up to 6 attempts). Synchronous retain is not used, because extraction takes longer than the gateway's 60 s
limit. **Backfill** sends dataset memories that never reached the bank through the same pipeline, keeping their original dates.

| Where | Hindsight capability |
|---|---|
| Every record page (supplier, product, customer, order, PO, warehouse, shipment, return, run) | **reflect** with `response_schema` → structured brief (summary, patterns, risks, past decisions, commitments, recommendations, confidence, evidence); **recall** with entities, as-of filtering; planner **notes** → retain |
| Creating a PO, changing or cancelling a PO, choosing a carrier, accepting an agent decision | **advice**: structured reflect → proceed / caution / stop, with reasons, precedents, commitments at risk and an alternative |
| Risk & disruptions | the decision agent (vendored in `backend/agent`) **recalls** precedents as of the business date, simulates every response and checks commitments. Accepting executes the action (revises the date, places a spot PO, cancels, transfers or reschedules), records the decision and commitment, and retains it |
| AI Insights "Explain" | recall of past episodes → "Has this happened before?" |
| Ask Meridian | tool calling with `run_sql` **and** `search_memory` (recall) |
| Memory hub | health, bank stats (units by fact type, links by type), sync pipeline, outbox, **operations** (list/retry/cancel), corpus coverage via **documents**, **timeseries** |
| Explorer | recall playground (types, budget, as-of, temporal window, tags, prefer observations), reflect playground (structured presets, directives, mental models), **list_memories**, **documents** with chunks and extracted units, **entity graph** |
| Mental models | 12 Meridian models (supplier reliability, seasonal slippage, disruption playbook, carrier performance, …): create, edit, refresh, dry-run, history, delete |
| Knowledge base | playbook pages written by Hindsight and refreshed daily: folders, pages, search, delete |
| Directives & settings | directives (create, toggle, edit, delete), bank config (missions, observations, retrieval switches, dispositions), **export** the bank |

The directives, mental models and playbooks are created idempotently at startup (`MEMORY_BOOTSTRAP=1`), or from Memory → Run setup.
Set `MEMORY_SYNC=0` to pause the outbox worker.

## Data and sync

`backend/app/build_db.py` copies the source dataset byte-for-byte (the source file is never written) and records its SHA-256 in
`data/manifest.json`. It then derives what the dataset lacks, deterministically (fixed seed) and reconciled to it:

- **Sales orders**: one line per `sale` inventory movement, keeping the `movement_id`. Shipped units total **720,115**, equal to the dataset's fulfilled demand. Unfulfilled weekly demand (**1,206** units) becomes backordered lines.
- **Shipments**: one outbound per sales order and one inbound per purchase order, with `received_at` as the delivery date.
- **Customers** (264), **carriers** (5) and **returns** (a category-dependent share of delivered lines).
- **Stock policies**: min = the dataset's reorder point; `stock_levels` is materialized from the ledger and kept current by every movement.

**Data & sync** compares every source table with the live database row by row. A table is **identical**, **in sync (audited
changes)** when every added or changed row is explained by the audit log, or **DIVERGED**. It also reports how many memory-corpus
record references resolve (7,310 of 7,487), plus the Hindsight retention ledger. Phase 2 retains each audited application event to
Hindsight so memory stays in step with the database.

## Stack

FastAPI + SQLite (WAL, serialized writes) · React 18 + TypeScript + Vite · Recharts · Groq (`openai/gpt-oss-120b`).
Docker: `docker compose up -d --build` (mounts the source dataset read-only; the operational database lives in a volume).
