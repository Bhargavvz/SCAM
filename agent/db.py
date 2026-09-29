"""SQLite access with the as-of rule built in.

The dataset DB is opened read-only. An attached `live` DB holds the append-only tables the agent writes
at runtime. TEMP views named like the dataset tables shadow them (SQLite resolves unqualified names
temp -> main -> attached), so any unqualified query only returns rows known on `as_of`, with
outcome/receipt/resolution columns masked until the date they became known.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from pathlib import Path

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions_live (
  decision_id TEXT PRIMARY KEY,
  event_ref TEXT,
  supplier_id TEXT, rm_id TEXT, plant_id TEXT, rpo_id TEXT,
  decided_at TEXT NOT NULL,
  decided_by_role TEXT NOT NULL,
  decision_type TEXT NOT NULL,
  options_considered_json TEXT NOT NULL,
  chosen_option TEXT NOT NULL,
  rationale_text TEXT NOT NULL,
  expected_cost REAL,
  expected_stockout_days REAL,
  evidence_doc_ids TEXT NOT NULL,
  evidence_record_ids TEXT NOT NULL,
  run_id TEXT NOT NULL,
  logged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitments_live (
  commitment_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL REFERENCES decisions_live(decision_id),
  counterparty_type TEXT NOT NULL,
  counterparty_id TEXT NOT NULL,
  made_by_role TEXT NOT NULL,
  made_at TEXT NOT NULL,
  commitment_text TEXT NOT NULL,
  quantity REAL,
  due_date TEXT NOT NULL,
  penalty_or_credit TEXT,
  logged_at TEXT NOT NULL
);
""" + "".join(
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
    f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;\n"
    for t in ("decisions_live", "commitments_live") for op in ("UPDATE", "DELETE"))

_DEC_OUTCOME = ("actual_cost", "actual_stockout_days", "outcome_label", "outcome_assessed_at",
                "lesson_text", "footprint_ids", "outcome_attribution")
_RUN_ACTUALS = ("actual_start", "produced_qty", "delay_days", "delay_reason_code", "rm_shortage_rm_id")


def init_live(live_path: Path) -> None:
    live_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(live_path)
    try:
        with con:
            con.executescript(LIVE_SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta VALUES ('token', ?)", (secrets.token_hex(4),))
    finally:
        con.close()


def open_live_writer(live_path: Path) -> sqlite3.Connection:
    init_live(live_path)
    con = sqlite3.connect(live_path)
    con.row_factory = sqlite3.Row
    return con


def live_token(live_path: Path) -> str:
    init_live(live_path)
    con = sqlite3.connect(live_path)
    try:
        return con.execute("SELECT value FROM meta WHERE key='token'").fetchone()[0]
    finally:
        con.close()


def live_memory_ids(live_path: Path) -> set[str]:
    """Hindsight document_ids of decisions logged in this live DB ("DECL00001.<token>")."""
    token = live_token(live_path)
    con = sqlite3.connect(live_path)
    try:
        return {f"{r[0]}.{token}" for r in con.execute("SELECT decision_id FROM decisions_live")}
    finally:
        con.close()


def _masked(cols, cond: str) -> str:
    return ", ".join(f"CASE WHEN {cond} THEN {c} END AS {c}" for c in cols)


def asof_views(as_of: str) -> dict[str, str]:
    if not DATE_RE.match(as_of or ""):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    A = f"'{as_of}'"
    resolved = "NULLIF(resolved_at, '')"
    concluded = "NULLIF(concluded_at, '')"
    closed = f"NULLIF(end_date, '') <= {A}"  # events still running on as_of carry whole-life aggregates
    return {
        "disruption_events": f"""
            SELECT event_id, event_type,
                   CASE WHEN {closed} THEN title ELSE replace(event_type, '_', ' ') || ' (ongoing)' END AS title,
                   start_date, CASE WHEN {closed} THEN end_date END AS end_date, detected_at,
                   {_masked(("severity", "root_cause_text", "anchor_ids"), closed)},
                   root_cause_code, origin_entity_type, origin_entity_id, anchor_type
            FROM main.disruption_events WHERE detected_at <= {A}""",
        "event_impacts": f"""SELECT i.* FROM main.event_impacts i JOIN main.disruption_events e USING (event_id)
                              WHERE e.detected_at <= {A} AND NULLIF(e.end_date, '') <= {A}""",
        "contracts": f"SELECT * FROM main.contracts WHERE valid_from <= {A}",
        "rm_supplier_catalog": f"SELECT * FROM main.rm_supplier_catalog WHERE price_valid_from <= {A}",
        "bill_of_materials": f"""
            SELECT bom_id, product_id, rm_id, qty_per_unit, uom, scrap_pct, effective_from,
                   CASE WHEN NULLIF(effective_to, '') <= {A} THEN effective_to END AS effective_to,
                   bom_version, change_reason
            FROM main.bill_of_materials WHERE effective_from <= {A}""",
        "event_links": f"""SELECT l.* FROM main.event_links l
                            JOIN main.disruption_events s ON s.event_id = l.src_event_id
                            JOIN main.disruption_events d ON d.event_id = l.dst_event_id
                            WHERE s.detected_at <= {A} AND d.detected_at <= {A}""",
        "decisions": f"""
            SELECT decision_id, event_id, decided_at, decided_by_role, decision_type, options_considered_json,
                   chosen_option, rationale_text, expected_cost, expected_stockout_days,
                   {_masked(_DEC_OUTCOME, f"NULLIF(outcome_assessed_at, '') <= {A}")}, 'historical' AS source
            FROM main.decisions WHERE decided_at <= {A}
            UNION ALL
            SELECT decision_id, event_ref, decided_at, decided_by_role, decision_type, options_considered_json,
                   chosen_option, rationale_text, expected_cost, expected_stockout_days,
                   NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'live'
            FROM live.decisions_live WHERE decided_at <= {A}""",
        "commitments": f"""
            SELECT commitment_id, decision_id, negotiation_id, counterparty_type, counterparty_id, made_by_role,
                   made_at, commitment_text, quantity, due_date, penalty_or_credit,
                   CASE WHEN {resolved} <= {A} THEN status ELSE 'open' END AS status,
                   CASE WHEN {resolved} <= {A} THEN resolved_at END AS resolved_at,
                   CASE WHEN {resolved} <= {A} THEN evidence_ids END AS evidence_ids,
                   'historical' AS source
            FROM main.commitments WHERE made_at <= {A}
            UNION ALL
            SELECT commitment_id, decision_id, NULL, counterparty_type, counterparty_id, made_by_role, made_at,
                   commitment_text, quantity, due_date, penalty_or_credit, 'open', NULL, NULL, 'live'
            FROM live.commitments_live WHERE made_at <= {A}""",
        "negotiations": f"""
            SELECT negotiation_id, supplier_id, rm_id, started_at, topic, our_ask, their_offer,
                   CASE WHEN {concluded} <= {A} THEN contract_id END AS contract_id,
                   CASE WHEN {concluded} <= {A} THEN concluded_at END AS concluded_at,
                   CASE WHEN {concluded} <= {A} THEN concessions_json END AS concessions_json,
                   CASE WHEN {concluded} <= {A} THEN final_terms END AS final_terms,
                   CASE WHEN {concluded} <= {A} THEN outcome ELSE 'open' END AS outcome
            FROM main.negotiations WHERE started_at <= {A}""",
        "supplier_scorecard_monthly": f"""
            SELECT s.supplier_id, s.month, s.po_count, s.otif_rate, s.avg_delay_days, s.fill_rate, s.quality_ppm,
                   s.commitments_made,
                   (SELECT COUNT(*) FROM main.commitments c
                     WHERE c.counterparty_id = s.supplier_id AND substr(c.made_at, 1, 7) = substr(s.month, 1, 7)
                       AND c.status = 'fulfilled' AND NULLIF(c.resolved_at, '') <= {A}) AS commitments_kept
            FROM main.supplier_scorecard_monthly s WHERE date(s.month, '+1 month') <= {A}""",
        "rm_purchase_orders": f"""
            SELECT o.rm_purchase_order_id, o.supplier_id, o.plant_id, o.order_type, o.ordered_at, o.promised_at,
                   COALESCE((SELECT r.new_expected_at FROM main.rm_po_revisions r
                             WHERE r.rm_po_id = o.rm_purchase_order_id AND r.revised_at <= {A}
                             ORDER BY r.revised_at DESC, r.revision_id DESC LIMIT 1), o.promised_at) AS expected_at,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN o.received_at END AS received_at,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN o.status
                        WHEN o.status = 'cancelled' THEN 'cancelled' ELSE 'open' END AS status
            FROM main.rm_purchase_orders o WHERE o.ordered_at <= {A}""",
        "rm_purchase_order_lines": f"""
            SELECT l.rm_purchase_order_line_id, l.rm_purchase_order_id, l.rm_id, l.quantity_ordered,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN l.quantity_received END AS quantity_received,
                   l.unit_cost
            FROM main.rm_purchase_order_lines l JOIN main.rm_purchase_orders o USING (rm_purchase_order_id)
            WHERE o.ordered_at <= {A}""",
        "rm_po_revisions": f"SELECT * FROM main.rm_po_revisions WHERE revised_at <= {A}",
        "rm_inventory_movements": f"SELECT * FROM main.rm_inventory_movements WHERE movement_at <= {A}",
        "rm_inventory_snapshots_weekly": f"SELECT * FROM main.rm_inventory_snapshots_weekly WHERE snapshot_date <= {A}",
        "production_runs": f"""
            SELECT production_run_id, plant_id, product_id, purchase_order_line_id, planned_start, planned_qty,
                   {_masked(_RUN_ACTUALS, f"NULLIF(actual_start, '') <= {A}")}
            FROM main.production_runs""",
        "product_demand_weekly": f"SELECT * FROM main.product_demand_weekly WHERE week_start <= {A}",
        "purchase_orders": f"""
            SELECT purchase_order_id, supplier_id, warehouse_id, ordered_at, expected_at,
                   CASE WHEN NULLIF(received_at, '') <= {A} THEN received_at END AS received_at,
                   CASE WHEN NULLIF(received_at, '') <= {A} THEN status
                        WHEN status = 'cancelled' THEN 'cancelled' ELSE 'open' END AS status
            FROM main.purchase_orders WHERE ordered_at <= {A}""",
        "purchase_order_lines": f"""
            SELECT l.purchase_order_line_id, l.purchase_order_id, l.product_id, l.quantity_ordered,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN l.quantity_received END AS quantity_received,
                   l.unit_cost
            FROM main.purchase_order_lines l JOIN main.purchase_orders o USING (purchase_order_id)
            WHERE o.ordered_at <= {A}""",
        "inventory_movements": f"SELECT * FROM main.inventory_movements WHERE movement_at <= {A}",
    }


def connect(db_path: Path, live_path: Path, as_of: str) -> sqlite3.Connection:
    views = asof_views(as_of)  # validates as_of before anything is opened
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found - run `python -m agent.setup_data`")
    init_live(live_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS live", (f"file:{live_path}?mode=ro",))
    for name, sql in views.items():
        con.execute(f"CREATE TEMP VIEW {name} AS {sql}")
    con.execute("PRAGMA query_only = ON")
    return con
