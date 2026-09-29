"""Allowlisted, parameterised, read-only DB queries.

Hindsight recall chooses WHICH of these to run (by name, via playbook memories); it never supplies SQL.
Each query is filtered to rows dated on/before :as_of and returns at most LIMIT rows.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

LIMIT = 5

QUERIES = {
    "po_status": {
        "params": ["po_id"],
        "desc": "the delayed RM purchase order: supplier, plant, dates, status and latest ETA revision",
        "sql": """SELECT p.rm_po_id, p.supplier_id, p.plant_id, l.rm_id, l.quantity_ordered, p.promised_at, p.status,
                         (SELECT new_expected_at FROM rm_po_revisions r WHERE r.rm_po_id=p.rm_po_id AND r.revised_at<=:as_of
                          ORDER BY revised_at DESC LIMIT 1) AS latest_eta
                  FROM rm_purchase_orders p JOIN rm_purchase_order_lines l ON l.rm_po_id=p.rm_po_id
                  WHERE p.rm_po_id=:po_id""",
    },
    "open_commitments": {
        "params": ["supplier_id", "po_id"],
        "desc": "commitments with the supplier that are still open on the decision date (ones naming this PO first)",
        "sql": """SELECT commitment_id, commitment_text, made_at, due_date, penalty_or_credit FROM commitments
                  WHERE counterparty_id=:supplier_id AND made_at<=:as_of AND (resolved_at IS NULL OR resolved_at>:as_of)
                  ORDER BY instr(commitment_text, :po_id) > 0 DESC, due_date""",
    },
    "supplier_scorecard": {
        "params": ["supplier_id"],
        "desc": "the supplier's most recent monthly scorecards (OTIF, delay, fill, quality, commitments kept)",
        "sql": """SELECT supplier_id, month, po_count, otif_rate, avg_delay_days, fill_rate, quality_ppm, commitments_made, commitments_kept
                  FROM supplier_scorecard_monthly WHERE supplier_id=:supplier_id AND month<substr(:as_of,1,7)
                  ORDER BY month DESC""",
    },
    "stock_cover": {
        "params": ["rm_id", "plant_id"],
        "desc": "latest weekly RM snapshots at the plant: on hand, safety stock, days of cover, stockout flag",
        "sql": """SELECT rm_id, plant_id, snapshot_date, on_hand_units, safety_stock_units, days_of_cover, stockout_flag
                  FROM rm_inventory_snapshots_weekly WHERE rm_id=:rm_id AND plant_id=:plant_id AND snapshot_date<=:as_of
                  ORDER BY snapshot_date DESC""",
    },
    "supplier_recent_slips": {
        "params": ["supplier_id"],
        "desc": "the supplier's most recent ETA revisions (how late, and why)",
        "sql": """SELECT r.rm_po_revision_id, r.rm_po_id, r.revised_at, r.old_expected_at, r.new_expected_at, r.reason_code
                  FROM rm_po_revisions r JOIN rm_purchase_orders p ON p.rm_po_id=r.rm_po_id
                  WHERE p.supplier_id=:supplier_id AND r.revised_at<=:as_of ORDER BY r.revised_at DESC""",
    },
    "past_decisions_supplier": {
        "params": ["supplier_id"],
        "desc": "past decisions on events originating at this supplier, with outcome and lesson",
        "sql": """SELECT d.decision_id, d.decided_at, d.decision_type, d.outcome_label, d.expected_cost, d.actual_cost, d.lesson_text
                  FROM decisions d JOIN disruption_events e ON e.event_id=d.event_id
                  WHERE e.origin_entity_id=:supplier_id AND d.outcome_assessed_at<=:as_of ORDER BY d.decided_at DESC""",
    },
    "alternate_sources": {
        "params": ["rm_id", "supplier_id"],
        "desc": "other catalog sources for the raw material valid on the decision date",
        "sql": """SELECT catalog_line_id, supplier_id, sourcing_rank, lead_time_days, unit_price, qualified_flag FROM rm_supplier_catalog
                  WHERE rm_id=:rm_id AND supplier_id<>:supplier_id AND price_valid_from<=:as_of
                        AND (price_valid_to IS NULL OR price_valid_to>=:as_of) ORDER BY qualified_flag DESC, lead_time_days""",
    },
}

DEFAULT = ["po_status", "open_commitments", "stock_cover"]


def run_query(con: sqlite3.Connection, name: str, params: dict) -> pd.DataFrame:
    q = QUERIES[name]  # KeyError for anything not allowlisted
    missing = [p for p in q["params"] + ["as_of"] if not params.get(p)]
    if missing:
        raise ValueError(f"{name}: missing {missing}")
    return pd.read_sql(f"SELECT * FROM ({q['sql']}) LIMIT {LIMIT}", con, params={k: params[k] for k in q["params"] + ["as_of"]})
