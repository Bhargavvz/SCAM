"""Read-only SQL for the agent: a guarded free-form `run_sql` plus curated lookups.

All queries run on an as-of connection from agent.db.connect: unqualified table names resolve to the
as-of TEMP views, which is why the LLM may not use schema-qualified names.
"""
from __future__ import annotations

import re
import sqlite3
import time

_LITERAL = re.compile(r"'(?:[^']|'')*'")
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|create|alter|replace|attach|detach|pragma|vacuum|"
                        r"reindex|analyze|begin|commit|rollback|savepoint)\b", re.I)
_QUALIFIED = re.compile(r"\b(main|live|temp)\s*\.", re.I)

_RECORD_TABLES = [  # longest prefix first
    ("DECL", "decisions", "decision_id"), ("CMTL", "commitments", "commitment_id"),
    ("RPOL", "rm_purchase_order_lines", "rm_purchase_order_line_id"),
    ("RPO", "rm_purchase_orders", "rm_purchase_order_id"), ("REV", "rm_po_revisions", "revision_id"),
    ("EVT", "disruption_events", "event_id"), ("DEC", "decisions", "decision_id"),
    ("CMT", "commitments", "commitment_id"), ("NEG", "negotiations", "negotiation_id"),
    ("CTR", "contracts", "contract_id"), ("CAT", "rm_supplier_catalog", "catalog_id"),
    ("POL", "purchase_order_lines", "purchase_order_line_id"), ("PR", "production_runs", "production_run_id"),
    ("PO", "purchase_orders", "purchase_order_id"), ("RT", "rm_inventory_movements", "transfer_id"),
    ("SUP", "suppliers", "supplier_id"), ("RM", "raw_materials", "rm_id"), ("IP", "products", "product_id"),
    ("W", "warehouses", "warehouse_id"), ("P", "plants", "plant_id"),
]


class SQLGuardError(ValueError):
    """The query is not a single read-only statement over the as-of views."""


def check_query(query: str) -> str:
    q = (query or "").strip().rstrip(";").strip()
    if not q:
        raise SQLGuardError("empty query")
    bare = _LITERAL.sub("''", q)
    if ";" in bare:
        raise SQLGuardError("only one statement is allowed")
    if not re.match(r"(?is)^(select|with)\b", bare):
        raise SQLGuardError("only SELECT / WITH queries are allowed")
    if m := _FORBIDDEN.search(bare):
        raise SQLGuardError(f"keyword {m.group(1).upper()} is not allowed (read-only)")
    if _QUALIFIED.search(bare):
        raise SQLGuardError("schema-qualified names (main./live./temp.) are not allowed; use plain table "
                            "names so the as-of filter applies")
    return q


def _asof_only_authorizer(action, arg1, arg2, db_name, inner_view):
    """Deny direct reads of the dataset (`main`) or live tables; reads made by the as-of TEMP views
    (inner_view is set) are allowed. This is the enforcement; the regexes above only give friendlier errors."""
    if action == sqlite3.SQLITE_READ and db_name in ("main", "live") and inner_view is None:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def run_sql(con: sqlite3.Connection, query: str, *, max_rows: int = 200, timeout_s: float = 5.0) -> dict:
    q = check_query(query)
    deadline = time.monotonic() + timeout_s
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    con.set_authorizer(_asof_only_authorizer)
    try:
        try:
            cur = con.execute(q)
        except sqlite3.DatabaseError as ex:
            if "prohibited" in str(ex) or "not authorized" in str(ex):
                raise SQLGuardError("direct access to dataset/live tables is not allowed; use plain table names so "
                                    "the as-of filter applies") from ex
            raise
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    finally:
        con.set_authorizer(None)
        con.set_progress_handler(None, 0)
    return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}


def _rows(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def open_commitments(con, counterparty_ids, as_of: str) -> list[dict]:
    ids = [i for i in dict.fromkeys(counterparty_ids) if i]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return _rows(con, f"""
        SELECT commitment_id, counterparty_type, counterparty_id, made_at, due_date, commitment_text,
               penalty_or_credit, source, commitment_text LIKE 'We order at least%' AS is_volume_commitment
        FROM commitments
        WHERE status = 'open' AND made_at <= ? AND counterparty_id IN ({ph})
        ORDER BY due_date, commitment_id""", (as_of, *ids))


def supplier_scorecard(con, supplier_id: str, months: int = 6) -> list[dict]:
    return _rows(con, """SELECT * FROM supplier_scorecard_monthly WHERE supplier_id = ?
                         ORDER BY month DESC LIMIT ?""", (supplier_id, months))


def prior_events(con, supplier_id, rm_id, plant_id, limit: int = 12) -> list[dict]:
    return _rows(con, """
        SELECT e.event_id, e.event_type, e.title, e.detected_at, e.start_date, e.end_date, e.severity,
               e.root_cause_code, e.origin_entity_type, e.origin_entity_id,
               (e.origin_entity_id = :rm OR EXISTS (SELECT 1 FROM event_impacts i
                                                    WHERE i.event_id = e.event_id AND i.entity_id = :rm)) AS same_rm
        FROM disruption_events e
        WHERE e.origin_entity_id IN (:sup, :rm, :plant)
           OR EXISTS (SELECT 1 FROM event_impacts i WHERE i.event_id = e.event_id AND i.entity_id = :rm)
        ORDER BY same_rm DESC, e.detected_at DESC LIMIT :lim""",
                 {"sup": supplier_id, "rm": rm_id, "plant": plant_id, "lim": limit})


def event_links_for(con, event_ids: list[str]) -> list[dict]:
    if not event_ids:
        return []
    ph = ",".join("?" * len(event_ids))
    return _rows(con, f"""SELECT src_event_id, dst_event_id, link_type FROM event_links
                          WHERE src_event_id IN ({ph}) OR dst_event_id IN ({ph})
                          ORDER BY src_event_id, dst_event_id""", (*event_ids, *event_ids))


def prior_decisions(con, supplier_id, rm_id, as_of: str, limit: int = 8) -> list[dict]:
    """This deployment's own live decisions first, then historical decisions whose outcome is already known
    on as_of (they carry lessons), then decisions still pending an outcome; newest first within each group.
    Pure recency would hide the informative precedents: e.g. on 2025-10-14 the only SUP0247 accept_delay
    with a known (successful) outcome is DEC00353, the 9th most recent decision."""
    return _rows(con, """
        SELECT * FROM (
            SELECT d.decision_id, d.event_id, d.decided_at, d.decision_type, d.chosen_option, d.expected_cost,
                   d.expected_stockout_days, d.actual_cost, d.actual_stockout_days, d.outcome_label, d.lesson_text,
                   d.outcome_attribution, d.source
            FROM decisions d JOIN disruption_events e ON e.event_id = d.event_id
            WHERE d.source = 'historical'
              AND (e.origin_entity_id IN (:sup, :rm)
                   OR EXISTS (SELECT 1 FROM event_impacts i WHERE i.event_id = e.event_id AND i.entity_id = :rm))
            UNION ALL
            SELECT decision_id, event_ref, decided_at, decision_type, chosen_option, expected_cost,
                   expected_stockout_days, NULL, NULL, NULL, NULL, NULL, 'live'
            FROM live.decisions_live
            WHERE decided_at <= :as_of AND (supplier_id = :sup OR rm_id = :rm))
        ORDER BY (source = 'live') DESC, (outcome_label IS NULL), decided_at DESC LIMIT :lim""",
                 {"sup": supplier_id, "rm": rm_id, "as_of": as_of, "lim": limit})


def record_exists(con, record_id: str) -> bool | None:
    rid = (record_id or "").strip()
    if not re.fullmatch(r"[A-Z]+\d+", rid):
        return None
    for prefix, table, col in _RECORD_TABLES:
        if rid.startswith(prefix) and rid[len(prefix):].isdigit():
            return con.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (rid,)).fetchone() is not None
    return None


def schema_summary(con, columns: bool = True) -> str:
    names = [r[0] for r in con.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'table' AND name <> 'dataset_info' ORDER BY name")]
    if not columns:
        return ("Tables (query `SELECT * FROM <table> LIMIT 1` to see columns): " + ", ".join(names)
                + ". decisions / commitments also contain live rows (source = 'live').")
    lines = []
    for n in names:
        cols = [r[1] for r in con.execute(f"PRAGMA main.table_info({n})")]
        lines.append(f"{n}({', '.join(cols)})")
    lines.append("decisions / commitments also contain this deployment's live rows (column source = 'live').")
    return "\n".join(lines)
