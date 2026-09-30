"""Supply chain management API.

Operational pages (dashboard, disruptions, purchase orders, suppliers, materials, production, demand, commitments,
decisions, search) read the database through the as-of views, so the whole application can be viewed as of any
business date. Agent work (resolving a disruption, memory briefs, questions) runs as background jobs; accepting a
recommendation is a separate, explicit step that writes the decision back to the database and to Hindsight.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agent import agent_core, sql_tools
from agent.capabilities import ask, brief
from agent.card import DecisionCard
from agent.config import ROOT, load_settings
from agent.db import connect
from agent.log_decision import log_decision
from agent.scenarios import load_holdout
from agent.simulate import SimulationError, build_state, simulate_options
from api.jobs import JOBS, load_cached, store_cached

S = load_settings()
APP_TODAY = os.environ.get("APP_TODAY", "2025-10-14")
DATA_START, DATA_END = "2023-01-01", "2025-12-31"
router = APIRouter(prefix="/api")


# ------------------------------------------------------------------------------------------------ helpers
def _asof(as_of: str | None) -> str:
    return as_of or APP_TODAY


def _con(as_of: str):
    try:
        return connect(S.db_path, S.live_db_path, as_of)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


def _q(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _one(con, sql: str, params=()) -> dict | None:
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def _shift(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


def _sunday(d: str) -> str:
    x = date.fromisoformat(d)
    return (x - timedelta(days=(x.weekday() + 1) % 7)).isoformat()


def _agent():
    return agent_core.build_agent(S, live_db_path=S.live_db_path)


def _like(q: str | None) -> str:
    return f"%{(q or '').strip()}%"


def _page(rows_sql: str, count_sql: str, con, params: tuple, limit: int, offset: int) -> dict:
    total = con.execute(count_sql, params).fetchone()[0]
    rows = _q(con, f"{rows_sql} LIMIT ? OFFSET ?", (*params, limit, offset))
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


# ------------------------------------------------------------------------------------------------ app + dashboard
@router.get("/app")
def app_info():
    return {"today": APP_TODAY, "data_start": DATA_START, "data_end": DATA_END, "bank_id": S.hindsight_bank_id,
            "llm": f"{S.llm_provider}:{S.llm_model}", "compact": S.llm_compact}


def _runs_at_risk(con, as_of: str, days: int = 21, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT r.production_run_id, r.plant_id, r.product_id, r.planned_start, r.planned_qty,
               b.rm_id, m.rm_name, s.on_hand_units, s.safety_stock_units, s.days_of_cover
        FROM production_runs r
        JOIN bill_of_materials b ON b.product_id = r.product_id AND b.effective_from <= r.planned_start
             AND (b.effective_to IS NULL OR b.effective_to = '' OR b.effective_to >= r.planned_start)
        JOIN rm_inventory_snapshots_weekly s ON s.rm_id = b.rm_id AND s.plant_id = r.plant_id AND s.snapshot_date = ?
        JOIN raw_materials m ON m.rm_id = b.rm_id
        WHERE r.planned_start BETWEEN ? AND ? AND r.actual_start IS NULL AND s.on_hand_units < s.safety_stock_units
        ORDER BY r.planned_start, r.production_run_id"""
    rows = _q(con, sql + (f" LIMIT {int(limit)}" if limit else ""), (_sunday(as_of), as_of, _shift(as_of, days)))
    return rows


@router.get("/dashboard")
def dashboard(as_of: str | None = None):
    a = _asof(as_of)
    con = _con(a)
    try:
        c = lambda sql, p=(): con.execute(sql, p).fetchone()[0]  # noqa: E731
        risk = _runs_at_risk(con, a)
        outcomes = {r["outcome_label"]: r["n"] for r in _q(con, """SELECT outcome_label, COUNT(*) n FROM decisions
                                                                   WHERE outcome_label IS NOT NULL GROUP BY 1""")}
        known = sum(outcomes.values()) or 1
        kpis = {
            "open_disruptions": c("SELECT COUNT(*) FROM disruption_events WHERE end_date IS NULL"),
            "new_disruptions_7d": c("SELECT COUNT(*) FROM disruption_events WHERE detected_at > ?", (_shift(a, -7),)),
            "open_pos": c("SELECT COUNT(*) FROM rm_purchase_orders WHERE status = 'open'"),
            "overdue_pos": c("SELECT COUNT(*) FROM rm_purchase_orders WHERE status = 'open' AND expected_at < ?", (a,)),
            "slipped_pos": c("SELECT COUNT(*) FROM rm_purchase_orders WHERE status = 'open' AND expected_at > promised_at"),
            "runs_at_risk": len({r["production_run_id"] for r in risk}),
            "commitments_due_14d": c("""SELECT COUNT(*) FROM commitments WHERE status = 'open'
                                        AND due_date BETWEEN ? AND ?""", (a, _shift(a, 14))),
            "decision_success_rate": round(outcomes.get("success", 0) / known, 3),
            "decisions_logged_live": c("SELECT COUNT(*) FROM decisions WHERE source = 'live'"),
        }
        trend_disruptions = _q(con, """SELECT substr(detected_at, 1, 7) AS month, COUNT(*) AS n,
                                              ROUND(AVG(severity), 2) AS avg_severity
                                       FROM disruption_events WHERE detected_at > ? AND detected_at < ?
                                       GROUP BY 1 ORDER BY 1""", (_shift(a, -365), a[:7] + "-01"))
        trend_otif = _q(con, """SELECT substr(month, 1, 7) AS month, ROUND(AVG(otif_rate), 3) AS otif,
                                       ROUND(AVG(avg_delay_days), 2) AS delay
                                FROM supplier_scorecard_monthly WHERE month >= ? AND month < ?
                                GROUP BY 1 ORDER BY 1""", (_shift(a, -365), a[:7] + "-01"))
        recent = _q(con, """SELECT event_id, event_type, title, detected_at, severity, origin_entity_id, end_date
                            FROM disruption_events ORDER BY detected_at DESC, event_id DESC LIMIT 8""")
        watch = _q(con, """SELECT o.supplier_id, s.supplier_name, COUNT(*) AS open_pos,
                                  SUM(o.expected_at > o.promised_at) AS slipped, SUM(o.expected_at < ?) AS overdue
                           FROM rm_purchase_orders o JOIN suppliers s USING (supplier_id)
                           WHERE o.status = 'open' GROUP BY o.supplier_id
                           HAVING slipped > 0 ORDER BY slipped DESC, overdue DESC LIMIT 8""", (a,))
        due = _q(con, """SELECT commitment_id, counterparty_id, due_date, commitment_text, source FROM commitments
                         WHERE status = 'open' AND due_date >= ? ORDER BY due_date LIMIT 6""", (a,))
        live = _q(con, """SELECT decision_id, event_id, decided_at, decision_type, expected_cost, expected_stockout_days
                          FROM decisions WHERE source = 'live' ORDER BY decided_at DESC, decision_id DESC LIMIT 5""")
        return {"as_of": a, "kpis": kpis, "trend_disruptions": trend_disruptions, "trend_otif": trend_otif,
                "recent_disruptions": recent, "supplier_watchlist": watch, "runs_at_risk": risk[:8],
                "commitments_due": due, "live_decisions": live}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ disruptions
def _anchors(con, event_id: str) -> list[str]:
    """Record ids that evidence an event, restricted to records that exist on the as-of date (the view masks
    anchor_ids of events still running, so read the base column and filter each id)."""
    row = con.execute("SELECT anchor_ids FROM main.disruption_events WHERE event_id = ?", (event_id,)).fetchone()
    ids = json.loads(row[0]) if row and row[0] else []
    return [i for i in ids if sql_tools.record_exists(con, i)]


def event_context(con, event_id: str, as_of: str) -> dict | None:
    """Derive the agent's inputs (slipped RPO, dependent runs, material) from an event's evidence."""
    anchors = _anchors(con, event_id)
    rpos = [a for a in anchors if a.startswith("RPO")]
    if not rpos:
        return None
    revised = _q(con, f"""SELECT rm_po_id, MAX(revised_at) AS last_rev FROM rm_po_revisions
                          WHERE rm_po_id IN ({','.join('?' * len(rpos))}) AND revised_at <= ?
                          GROUP BY rm_po_id ORDER BY last_rev DESC, rm_po_id""", (*rpos, as_of))
    if not revised:
        return None
    rpo = revised[0]["rm_po_id"]
    po = _one(con, "SELECT plant_id, supplier_id FROM rm_purchase_orders WHERE rm_purchase_order_id = ?", (rpo,))
    rms = [r["rm_id"] for r in _q(con, "SELECT DISTINCT rm_id FROM rm_purchase_order_lines WHERE rm_purchase_order_id = ?",
                                  (rpo,))]
    if not po or not rms:
        return None
    rm = rms[0]
    runs = [a for a in anchors if a.startswith("PR")]
    runs = [r["production_run_id"] for r in _q(con, f"""
        SELECT r.production_run_id FROM production_runs r
        JOIN bill_of_materials b ON b.product_id = r.product_id AND b.rm_id = ?
        WHERE r.production_run_id IN ({','.join('?' * len(runs)) or "''"}) ORDER BY r.planned_start""",
                                                    (rm, *runs))] if runs else []
    if not runs:  # fall back to the next runs at that plant that consume the material
        runs = [r["production_run_id"] for r in _q(con, """
            SELECT DISTINCT r.production_run_id, r.planned_start FROM production_runs r
            JOIN bill_of_materials b ON b.product_id = r.product_id AND b.rm_id = ?
            WHERE r.plant_id = ? AND r.planned_start >= ? ORDER BY r.planned_start LIMIT 3""", (rm, po["plant_id"], as_of))]
    return {"rpo_id": rpo, "run_ids": runs, "rm_id": rm, "supplier_id": po["supplier_id"],
            "plant_id": po["plant_id"]} if runs else None


def _decision_for_event(con, event_id: str) -> dict | None:
    return _one(con, """SELECT decision_id, decision_type, decided_at, source, outcome_label FROM decisions
                        WHERE event_id = ? ORDER BY (source = 'live') DESC, decided_at DESC LIMIT 1""", (event_id,))


@router.get("/disruptions")
def disruptions(as_of: str | None = None, status: str = "open", q: str | None = None, severity: int | None = None,
                limit: int = Query(50, le=200), offset: int = 0):
    a = _asof(as_of)
    con = _con(a)
    try:
        where = ["(e.title LIKE ? OR e.event_id LIKE ? OR e.origin_entity_id LIKE ?)"]
        params: list = [_like(q)] * 3
        if status == "open":
            where.append("e.end_date IS NULL")
        elif status == "recent":
            where.append("e.detected_at > ?")
            params.append(_shift(a, -30))
        if severity:
            where.append("e.severity >= ?")
            params.append(severity)
        w = " AND ".join(where)
        page = _page(f"""SELECT e.event_id, e.event_type, e.title, e.detected_at, e.start_date, e.end_date, e.severity,
                                 e.origin_entity_type, e.origin_entity_id, e.root_cause_code,
                                 (SELECT decision_id FROM decisions d WHERE d.event_id = e.event_id
                                  ORDER BY (d.source = 'live') DESC LIMIT 1) AS decision_id
                          FROM disruption_events e WHERE {w} ORDER BY e.detected_at DESC, e.event_id DESC""",
                     f"SELECT COUNT(*) FROM disruption_events e WHERE {w}", con, tuple(params), limit, offset)
        return page
    finally:
        con.close()


@router.get("/disruptions/{event_id}")
def disruption(event_id: str, as_of: str | None = None):
    a = _asof(as_of)
    con = _con(a)
    try:
        ev = _one(con, "SELECT * FROM disruption_events WHERE event_id = ?", (event_id,))
        if not ev:
            raise HTTPException(status_code=404, detail=f"{event_id} is not known as of {a}")
        impacts = _q(con, "SELECT entity_type, entity_id, impact_metric, impact_value, unit FROM event_impacts WHERE event_id = ?",
                     (event_id,))
        links = _q(con, """SELECT l.link_type, l.src_event_id, l.dst_event_id,
                                  CASE WHEN l.src_event_id = ? THEN l.dst_event_id ELSE l.src_event_id END AS other_id,
                                  e.title AS other_title, e.detected_at AS other_detected
                           FROM event_links l JOIN disruption_events e
                             ON e.event_id = CASE WHEN l.src_event_id = ? THEN l.dst_event_id ELSE l.src_event_id END
                           WHERE l.src_event_id = ? OR l.dst_event_id = ? ORDER BY e.detected_at DESC LIMIT 20""",
                   (event_id, event_id, event_id, event_id))
        decisions = _q(con, """SELECT decision_id, decided_at, decision_type, expected_cost, expected_stockout_days,
                                      actual_cost, actual_stockout_days, outcome_label, source
                               FROM decisions WHERE event_id = ? ORDER BY decided_at""", (event_id,))
        ctx = event_context(con, event_id, a)
        affected = []
        if ctx:
            affected = _q(con, f"""SELECT r.production_run_id, r.plant_id, r.product_id, r.planned_start, r.planned_qty,
                                          p.sku, x.unit_price
                                   FROM production_runs r JOIN products p USING (product_id)
                                   JOIN products_ext x USING (product_id)
                                   WHERE r.production_run_id IN ({','.join('?' * len(ctx['run_ids']))})""",
                              tuple(ctx["run_ids"]))
        cached = load_cached("resolve", f"{event_id}|{a}")
        return {"as_of": a, "event": ev, "impacts": impacts, "links": links, "decisions": decisions,
                "context": ctx, "affected_runs": affected, "resolution": cached and {**cached, "result": cached["result"]}}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ purchase orders
PO_SELECT = """SELECT o.rm_purchase_order_id, o.supplier_id, s.supplier_name, o.plant_id, o.order_type, o.ordered_at,
                      o.promised_at, o.expected_at, o.received_at, o.status,
                      CAST(julianday(o.expected_at) - julianday(o.promised_at) AS INTEGER) AS slip_days,
                      (SELECT l.rm_id FROM rm_purchase_order_lines l WHERE l.rm_purchase_order_id = o.rm_purchase_order_id
                       LIMIT 1) AS rm_id,
                      (SELECT ROUND(SUM(l.quantity_ordered * l.unit_cost), 2) FROM rm_purchase_order_lines l
                       WHERE l.rm_purchase_order_id = o.rm_purchase_order_id) AS value
               FROM rm_purchase_orders o JOIN suppliers s USING (supplier_id)"""


@router.get("/pos")
def pos(as_of: str | None = None, status: str = "open", supplier_id: str | None = None, plant_id: str | None = None,
        q: str | None = None, limit: int = Query(50, le=200), offset: int = 0):
    a = _asof(as_of)
    con = _con(a)
    try:
        where, params = ["(o.rm_purchase_order_id LIKE ? OR s.supplier_name LIKE ? OR o.supplier_id LIKE ?)"], [_like(q)] * 3
        if status == "open":
            where.append("o.status = 'open'")
        elif status == "overdue":
            where.append("o.status = 'open' AND o.expected_at < ?")
            params.append(a)
        elif status == "slipped":
            where.append("o.status = 'open' AND o.expected_at > o.promised_at")
        if supplier_id:
            where.append("o.supplier_id = ?")
            params.append(supplier_id)
        if plant_id:
            where.append("o.plant_id = ?")
            params.append(plant_id)
        w = " AND ".join(where)
        order = "slip_days DESC, o.expected_at" if status in ("slipped", "overdue") else "o.ordered_at DESC"
        return _page(f"{PO_SELECT} WHERE {w} ORDER BY {order}",
                     f"SELECT COUNT(*) FROM rm_purchase_orders o JOIN suppliers s USING (supplier_id) WHERE {w}",
                     con, tuple(params), limit, offset)
    finally:
        con.close()


@router.get("/pos/{po_id}")
def po(po_id: str, as_of: str | None = None):
    a = _asof(as_of)
    con = _con(a)
    try:
        head = _one(con, f"{PO_SELECT} WHERE o.rm_purchase_order_id = ?", (po_id,))
        if not head:
            raise HTTPException(status_code=404, detail=f"{po_id} is not known as of {a}")
        lines = _q(con, """SELECT l.rm_purchase_order_line_id, l.rm_id, m.rm_name, m.uom, l.quantity_ordered,
                                  l.quantity_received, l.unit_cost FROM rm_purchase_order_lines l
                           JOIN raw_materials m USING (rm_id) WHERE l.rm_purchase_order_id = ?""", (po_id,))
        revisions = _q(con, """SELECT revision_id, revised_at, old_expected_at, new_expected_at, reason_code
                               FROM rm_po_revisions WHERE rm_po_id = ? ORDER BY revised_at, revision_id""", (po_id,))
        receipts = _q(con, """SELECT m.rm_movement_id, m.movement_at, m.movement_type, m.quantity_change
                              FROM rm_inventory_movements m JOIN rm_purchase_order_lines l
                                ON l.rm_purchase_order_line_id = m.rm_purchase_order_line_id
                              WHERE l.rm_purchase_order_id = ? ORDER BY m.movement_at""", (po_id,))
        events = _q(con, """SELECT event_id, event_type, title, detected_at, severity FROM disruption_events
                            WHERE event_id IN (SELECT event_id FROM main.disruption_events WHERE anchor_ids LIKE ?)
                            ORDER BY detected_at DESC""", (f'%"{po_id}"%',))
        commitments = sql_tools.open_commitments(con, [head["supplier_id"], head["plant_id"]], a)
        dependent = []
        if head["rm_id"]:
            dependent = _q(con, """SELECT DISTINCT r.production_run_id, r.product_id, r.planned_start, r.planned_qty
                                   FROM production_runs r JOIN bill_of_materials b ON b.product_id = r.product_id
                                   WHERE b.rm_id = ? AND r.plant_id = ? AND r.planned_start BETWEEN ? AND ?
                                   AND r.actual_start IS NULL ORDER BY r.planned_start LIMIT 10""",
                               (head["rm_id"], head["plant_id"], a, _shift(head["expected_at"] or a, 14)))
        return {"as_of": a, "po": head, "lines": lines, "revisions": revisions, "receipts": receipts,
                "events": events, "open_commitments": commitments, "dependent_runs": dependent}
    finally:
        con.close()


class ActionCheck(BaseModel):
    action: str
    as_of: str | None = None
    run_ids: list[str] | None = None


@router.post("/pos/{po_id}/check-action")
def check_action(po_id: str, req: ActionCheck):
    """Guardrail before a planner changes a PO: simulate it when possible and always check open commitments."""
    a = _asof(req.as_of)
    con = _con(a)
    try:
        head = _one(con, "SELECT supplier_id, plant_id, status FROM rm_purchase_orders WHERE rm_purchase_order_id = ?",
                    (po_id,))
        if not head:
            raise HTTPException(status_code=404, detail=f"{po_id} is not known as of {a}")
        commitments = sql_tools.open_commitments(con, [head["supplier_id"], head["plant_id"]], a)
        breaches = [c for c in commitments if req.action == "cancel_po" and c["is_volume_commitment"]
                    and c["counterparty_id"] == head["supplier_id"]]
        simulated = None
        runs = req.run_ids or [r["production_run_id"] for r in _q(con, """
            SELECT DISTINCT r.production_run_id, r.planned_start FROM production_runs r
            JOIN bill_of_materials b ON b.product_id = r.product_id
            JOIN rm_purchase_order_lines l ON l.rm_id = b.rm_id AND l.rm_purchase_order_id = ?
            WHERE r.plant_id = ? AND r.planned_start >= ? AND r.actual_start IS NULL
            ORDER BY r.planned_start LIMIT 1""", (po_id, head["plant_id"], a))]
        try:
            st = build_state(con, rpo_id=po_id, run_ids=runs, day0=a)
            simulated = next((o for o in simulate_options(con, st) if o["action"] == req.action), None)
        except SimulationError as ex:
            simulated = {"note": f"not simulated: {ex}"}
        blocked = bool(breaches) or bool(simulated and simulated.get("breaches_commitments"))
        return {"as_of": a, "po_id": po_id, "action": req.action, "blocked": blocked,
                "breached_commitments": breaches, "open_commitments": commitments, "simulation": simulated,
                "external_actions": [f"[MOCK - no external call made] ERP: would {req.action.replace('_', ' ')} {po_id}"]
                if not blocked else []}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ suppliers
@router.get("/suppliers")
def suppliers(as_of: str | None = None, q: str | None = None, limit: int = Query(50, le=400), offset: int = 0,
              sort: str = "slipped"):
    a = _asof(as_of)
    con = _con(a)
    try:
        order = {"slipped": "slipped DESC, open_pos DESC", "otif": "otif ASC", "name": "s.supplier_name"}.get(sort, "slipped DESC")
        rows_sql = f"""
            SELECT s.supplier_id, s.supplier_name, s.country_code, s.lead_time_days, s.reliability_score,
                   (SELECT COUNT(*) FROM rm_purchase_orders o WHERE o.supplier_id = s.supplier_id AND o.status = 'open') AS open_pos,
                   (SELECT COUNT(*) FROM rm_purchase_orders o WHERE o.supplier_id = s.supplier_id AND o.status = 'open'
                      AND o.expected_at > o.promised_at) AS slipped,
                   (SELECT ROUND(AVG(otif_rate), 3) FROM supplier_scorecard_monthly c WHERE c.supplier_id = s.supplier_id
                      AND c.month >= ?) AS otif,
                   (SELECT COUNT(*) FROM commitments m WHERE m.counterparty_id = s.supplier_id AND m.status = 'open') AS open_commitments
            FROM suppliers s WHERE (s.supplier_id LIKE ? OR s.supplier_name LIKE ?) ORDER BY {order}"""
        total = con.execute("SELECT COUNT(*) FROM suppliers s WHERE (s.supplier_id LIKE ? OR s.supplier_name LIKE ?)",
                            (_like(q), _like(q))).fetchone()[0]
        rows = _q(con, f"{rows_sql} LIMIT ? OFFSET ?", (_shift(a, -180), _like(q), _like(q), limit, offset))
        return {"total": total, "rows": rows, "limit": limit, "offset": offset}
    finally:
        con.close()


@router.get("/suppliers/{supplier_id}")
def supplier(supplier_id: str, as_of: str | None = None):
    from agent.capabilities import _delay_by_promised_month, _lead_time_by_month

    a = _asof(as_of)
    con = _con(a)
    try:
        prof = _one(con, "SELECT * FROM suppliers WHERE supplier_id = ?", (supplier_id,))
        if not prof:
            raise HTTPException(status_code=404, detail=f"unknown supplier {supplier_id}")
        return {
            "as_of": a, "supplier": prof,
            "scorecard": _q(con, """SELECT month, po_count, otif_rate, avg_delay_days, fill_rate, quality_ppm,
                                           commitments_made, commitments_kept FROM supplier_scorecard_monthly
                                    WHERE supplier_id = ? ORDER BY month DESC LIMIT 18""", (supplier_id,))[::-1],
            "delay_by_month": _delay_by_promised_month(con, supplier_id),
            "lead_time_trend": _lead_time_by_month(con, supplier_id)[::-1],
            "contracts": _q(con, """SELECT contract_id, scope, valid_from, valid_to, price_terms, min_volume, penalty_clause,
                                           force_majeure_flag FROM contracts WHERE supplier_id = ? ORDER BY valid_from DESC""",
                            (supplier_id,)),
            "negotiations": _q(con, """SELECT negotiation_id, rm_id, started_at, concluded_at, topic, our_ask, their_offer,
                                              final_terms, outcome FROM negotiations WHERE supplier_id = ?
                                       ORDER BY started_at DESC""", (supplier_id,)),
            "catalog": _q(con, """SELECT c.rm_id, m.rm_name, c.sourcing_rank, c.allocation_pct, c.lead_time_days, c.unit_price,
                                         c.price_valid_from, c.price_valid_to, c.qualified_flag
                                  FROM rm_supplier_catalog c JOIN raw_materials m USING (rm_id)
                                  WHERE c.supplier_id = ? AND c.price_valid_to >= ? ORDER BY c.rm_id""", (supplier_id, a)),
            "open_pos": _q(con, f"{PO_SELECT} WHERE o.supplier_id = ? AND o.status = 'open' ORDER BY o.expected_at LIMIT 25",
                           (supplier_id,)),
            "open_commitments": sql_tools.open_commitments(con, [supplier_id], a),
            "events": sql_tools.prior_events(con, supplier_id, None, None, limit=10),
            "decisions": sql_tools.prior_decisions(con, supplier_id, None, a, limit=10),
        }
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ materials
@router.get("/materials")
def materials(as_of: str | None = None, q: str | None = None, limit: int = Query(120, le=200), offset: int = 0):
    a = _asof(as_of)
    con = _con(a)
    try:
        rows = _q(con, """
            SELECT m.rm_id, m.rm_name, m.rm_category, m.uom, m.criticality, m.is_single_source, m.substitute_group_id,
                   ROUND(SUM(s.on_hand_units), 1) AS on_hand, ROUND(SUM(s.safety_stock_units), 1) AS safety_stock,
                   SUM(s.on_hand_units < s.safety_stock_units) AS plants_below_safety,
                   ROUND(MIN(s.days_of_cover), 1) AS min_days_of_cover
            FROM raw_materials m LEFT JOIN rm_inventory_snapshots_weekly s ON s.rm_id = m.rm_id AND s.snapshot_date = ?
            WHERE (m.rm_id LIKE ? OR m.rm_name LIKE ?)
            GROUP BY m.rm_id ORDER BY plants_below_safety DESC, m.criticality, m.rm_id LIMIT ? OFFSET ?""",
                  (_sunday(a), _like(q), _like(q), limit, offset))
        total = con.execute("SELECT COUNT(*) FROM raw_materials m WHERE (m.rm_id LIKE ? OR m.rm_name LIKE ?)",
                            (_like(q), _like(q))).fetchone()[0]
        return {"total": total, "rows": rows, "limit": limit, "offset": offset}
    finally:
        con.close()


@router.get("/materials/{rm_id}")
def material(rm_id: str, as_of: str | None = None):
    a = _asof(as_of)
    con = _con(a)
    try:
        m = _one(con, "SELECT * FROM raw_materials WHERE rm_id = ?", (rm_id,))
        if not m:
            raise HTTPException(status_code=404, detail=f"unknown material {rm_id}")
        stock = _q(con, """SELECT s.plant_id, p.plant_name, s.on_hand_units, s.safety_stock_units, s.days_of_cover
                           FROM rm_inventory_snapshots_weekly s JOIN plants p USING (plant_id)
                           WHERE s.rm_id = ? AND s.snapshot_date = ? ORDER BY s.plant_id""", (rm_id, _sunday(a)))
        trend = _q(con, """SELECT snapshot_date, plant_id, on_hand_units, safety_stock_units FROM rm_inventory_snapshots_weekly
                           WHERE rm_id = ? AND snapshot_date > ? ORDER BY snapshot_date""", (rm_id, _shift(a, -26 * 7)))
        subs = _q(con, """SELECT rm_id, rm_name, criticality FROM raw_materials WHERE substitute_group_id = ?
                          AND rm_id <> ?""", (m["substitute_group_id"], rm_id)) if m["substitute_group_id"] else []
        suppliers = _q(con, """SELECT c.supplier_id, s.supplier_name, c.sourcing_rank, c.allocation_pct, c.lead_time_days,
                                      c.unit_price, c.qualified_flag FROM rm_supplier_catalog c JOIN suppliers s USING (supplier_id)
                               WHERE c.rm_id = ? AND c.price_valid_from <= ? AND c.price_valid_to >= ?
                               ORDER BY c.allocation_pct DESC""", (rm_id, a, a))
        open_pos = _q(con, f"""{PO_SELECT} WHERE o.status = 'open' AND o.rm_purchase_order_id IN
                               (SELECT rm_purchase_order_id FROM rm_purchase_order_lines WHERE rm_id = ?)
                               ORDER BY o.expected_at LIMIT 20""", (rm_id,))
        runs = _q(con, """SELECT DISTINCT r.production_run_id, r.plant_id, r.product_id, r.planned_start, r.planned_qty
                          FROM production_runs r JOIN bill_of_materials b ON b.product_id = r.product_id AND b.rm_id = ?
                          WHERE r.planned_start BETWEEN ? AND ? AND r.actual_start IS NULL
                          ORDER BY r.planned_start LIMIT 20""", (rm_id, a, _shift(a, 30)))
        return {"as_of": a, "material": m, "stock": stock, "trend": trend, "substitutes": subs, "suppliers": suppliers,
                "open_pos": open_pos, "upcoming_runs": runs,
                "events": sql_tools.prior_events(con, None, rm_id, None, limit=10),
                "decisions": sql_tools.prior_decisions(con, None, rm_id, a, limit=10)}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ production / demand
@router.get("/production")
def production(as_of: str | None = None, days: int = Query(30, le=90), plant_id: str | None = None):
    a = _asof(as_of)
    con = _con(a)
    try:
        where, params = ["r.planned_start BETWEEN ? AND ?"], [_shift(a, -7), _shift(a, days)]
        if plant_id:
            where.append("r.plant_id = ?")
            params.append(plant_id)
        runs = _q(con, f"""SELECT r.production_run_id, r.plant_id, r.product_id, p.sku, r.planned_start, r.planned_qty,
                                  r.actual_start, r.delay_days, r.delay_reason_code, r.rm_shortage_rm_id
                           FROM production_runs r JOIN products p USING (product_id)
                           WHERE {' AND '.join(where)} ORDER BY r.planned_start, r.production_run_id LIMIT 400""",
                  tuple(params))
        risk = _runs_at_risk(con, a, days)
        by_plant = _q(con, """SELECT p.plant_id, p.plant_name, p.region, p.capacity_units_per_week,
                                     (SELECT COUNT(*) FROM production_runs r WHERE r.plant_id = p.plant_id
                                        AND r.planned_start BETWEEN ? AND ?) AS runs_planned,
                                     (SELECT COUNT(*) FROM production_runs r WHERE r.plant_id = p.plant_id
                                        AND r.delay_reason_code = 'rm_shortage' AND r.actual_start BETWEEN ? AND ?) AS rm_delays_90d
                              FROM plants p ORDER BY p.plant_id""", (a, _shift(a, days), _shift(a, -90), a))
        return {"as_of": a, "runs": runs, "at_risk": risk, "plants": by_plant,
                "delay_reasons": _q(con, """SELECT delay_reason_code, COUNT(*) n FROM production_runs
                                            WHERE actual_start BETWEEN ? AND ? AND delay_reason_code IS NOT NULL
                                            GROUP BY 1 ORDER BY n DESC""", (_shift(a, -90), a))}
    finally:
        con.close()


@router.get("/demand")
def demand(as_of: str | None = None, weeks: int = Query(26, le=104)):
    a = _asof(as_of)
    con = _con(a)
    try:
        weekly = _q(con, """SELECT p.category, d.week_start, SUM(d.forecast_units) AS forecast, SUM(d.actual_demand_units) AS actual,
                                   SUM(d.unfulfilled_units) AS unfulfilled
                            FROM product_demand_weekly d JOIN products p USING (product_id)
                            WHERE d.week_start > ? GROUP BY 1, 2 ORDER BY 2""", (_shift(a, -7 * weeks),))
        bias = _q(con, """SELECT p.category, SUM(d.forecast_units) AS forecast, SUM(d.actual_demand_units) AS actual,
                                 ROUND(1.0 * SUM(d.forecast_units) / NULLIF(SUM(d.actual_demand_units), 0) - 1, 3) AS bias,
                                 SUM(d.unfulfilled_units) AS unfulfilled
                          FROM product_demand_weekly d JOIN products p USING (product_id)
                          WHERE d.week_start > ? GROUP BY 1 ORDER BY bias DESC""", (_shift(a, -365),))
        return {"as_of": a, "weekly": weekly, "bias_by_category": bias}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ commitments / decisions
@router.get("/commitments")
def commitments(as_of: str | None = None, status: str = "open", q: str | None = None,
                limit: int = Query(50, le=200), offset: int = 0):
    a = _asof(as_of)
    con = _con(a)
    try:
        where, params = ["(commitment_id LIKE ? OR counterparty_id LIKE ? OR commitment_text LIKE ?)"], [_like(q)] * 3
        if status == "due":
            where.append("status = 'open' AND due_date BETWEEN ? AND ?")
            params += [a, _shift(a, 14)]
        elif status == "overdue":
            where.append("status = 'open' AND due_date < ?")
            params.append(a)
        elif status != "all":
            where.append("status = ?")
            params.append(status)
        w = " AND ".join(where)
        page = _page(f"""SELECT commitment_id, counterparty_type, counterparty_id, made_by_role, made_at, due_date,
                                 commitment_text, quantity, penalty_or_credit, status, resolved_at, decision_id, source,
                                 commitment_text LIKE 'We order at least%' AS is_volume_commitment
                          FROM commitments WHERE {w} ORDER BY (source = 'live') DESC, due_date""",
                     f"SELECT COUNT(*) FROM commitments WHERE {w}", con, tuple(params), limit, offset)
        page["summary"] = _q(con, "SELECT status, COUNT(*) n FROM commitments GROUP BY 1")
        return page
    finally:
        con.close()


@router.get("/decisions")
def decisions(as_of: str | None = None, source: str | None = None, decision_type: str | None = None,
              limit: int = Query(50, le=200), offset: int = 0):
    a = _asof(as_of)
    con = _con(a)
    try:
        where, params = ["1 = 1"], []
        if source:
            where.append("source = ?")
            params.append(source)
        if decision_type:
            where.append("decision_type = ?")
            params.append(decision_type)
        w = " AND ".join(where)
        page = _page(f"""SELECT decision_id, event_id, decided_at, decided_by_role, decision_type, expected_cost,
                                 expected_stockout_days, actual_cost, actual_stockout_days, outcome_label,
                                 outcome_attribution, lesson_text, source
                          FROM decisions WHERE {w} ORDER BY decided_at DESC, decision_id DESC""",
                     f"SELECT COUNT(*) FROM decisions WHERE {w}", con, tuple(params), limit, offset)
        page["accuracy"] = _q(con, """SELECT decision_type, COUNT(*) AS n,
                                             ROUND(SUM(actual_cost) / NULLIF(SUM(expected_cost), 0), 2) AS cost_ratio,
                                             ROUND(AVG(actual_stockout_days - expected_stockout_days), 2) AS extra_stockout_days,
                                             ROUND(AVG(outcome_label = 'success'), 3) AS success_rate
                                      FROM decisions WHERE outcome_label IS NOT NULL GROUP BY 1 ORDER BY n DESC""")
        return page
    finally:
        con.close()


@router.get("/decisions/{decision_id}")
def decision(decision_id: str, as_of: str | None = None):
    a = _asof(as_of)
    con = _con(a)
    try:
        d = _one(con, "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,))
        if not d:
            raise HTTPException(status_code=404, detail=f"{decision_id} is not known as of {a}")
        try:
            d["options"] = json.loads(d.get("options_considered_json") or "[]")
        except json.JSONDecodeError:
            d["options"] = []
        d["commitments"] = _q(con, "SELECT * FROM commitments WHERE decision_id = ?", (decision_id,))
        d["event"] = _one(con, "SELECT event_id, title, detected_at, severity FROM disruption_events WHERE event_id = ?",
                          (d.get("event_id"),))
        return d
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ search
@router.get("/search")
def search(q: str, as_of: str | None = None):
    a = _asof(as_of)
    term = (q or "").strip()
    if len(term) < 2:
        return {"results": []}
    con = _con(a)
    try:
        out: list[dict] = []
        idq = term.upper()
        if re.fullmatch(r"[A-Z]+\d+", idq) and sql_tools.record_exists(con, idq):
            out.append({"id": idq, "type": "record", "label": idq})
        like = _like(term)
        out += [{"id": r["supplier_id"], "type": "supplier", "label": r["supplier_name"]} for r in
                _q(con, "SELECT supplier_id, supplier_name FROM suppliers WHERE supplier_id LIKE ? OR supplier_name LIKE ? LIMIT 5",
                   (like, like))]
        out += [{"id": r["rm_id"], "type": "material", "label": r["rm_name"]} for r in
                _q(con, "SELECT rm_id, rm_name FROM raw_materials WHERE rm_id LIKE ? OR rm_name LIKE ? LIMIT 5", (like, like))]
        out += [{"id": r["event_id"], "type": "disruption", "label": r["title"]} for r in
                _q(con, "SELECT event_id, title FROM disruption_events WHERE event_id LIKE ? OR title LIKE ? "
                        "ORDER BY detected_at DESC LIMIT 5", (like, like))]
        out += [{"id": r["rm_purchase_order_id"], "type": "po", "label": f"{r['supplier_id']} · {r['status']}"} for r in
                _q(con, "SELECT rm_purchase_order_id, supplier_id, status FROM rm_purchase_orders WHERE rm_purchase_order_id LIKE ? "
                        "LIMIT 5", (like,))]
        seen, uniq = set(), []
        for r in out:
            if r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        return {"results": uniq[:15]}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ agent (background jobs)
class ResolveRequest(BaseModel):
    event_id: str
    as_of: str | None = None
    force: bool = False


class AcceptRequest(BaseModel):
    event_id: str
    as_of: str | None = None


class BriefRequest(BaseModel):
    entity_id: str
    as_of: str | None = None
    force: bool = False


class AskRequest(BaseModel):
    question: str
    as_of: str | None = None
    mode: str = "ask"  # ask (7 capabilities) | history (as-of QA)


def _holdout_card(event_id: str, as_of: str) -> dict | None:
    """Decision cards computed by the evaluation run for the same event and date are real results: reuse them."""
    for s in load_holdout(S).values():
        if s["event_id"] == event_id and s["day0"] == as_of:
            p = ROOT / "eval" / "out" / "cards" / f"{s['scenario_id']}.json"
            if p.exists():
                return json.loads(p.read_text())
    return None


def _resolve(event_id: str, as_of: str) -> dict:
    from api.server import RUN_SLOTS, RUN_QUEUE_TIMEOUT_S

    con = _con(as_of)
    try:
        ev = _one(con, "SELECT title, detected_at, origin_entity_id FROM disruption_events WHERE event_id = ?", (event_id,))
        if not ev:
            raise ValueError(f"{event_id} is not known as of {as_of}")
        ctx = event_context(con, event_id, as_of)
    finally:
        con.close()
    if not ctx:
        raise ValueError(f"{event_id} has no slipped purchase order with dependent production runs as of {as_of}; "
                         f"the decision agent needs one to simulate responses")
    report = (f"{as_of} - {ev['title']} ({event_id}, detected {ev['detected_at']}). Purchase order {ctx['rpo_id']} "
              f"({ctx['rm_id']}) slipped; runs {', '.join(ctx['run_ids'])} depend on it. What should we do?")
    if not RUN_SLOTS.acquire(timeout=RUN_QUEUE_TIMEOUT_S):
        raise RuntimeError("the agent is busy with other requests - try again in a minute")
    try:
        return _agent().run(report, as_of=as_of, context={k: ctx[k] for k in ("rpo_id", "run_ids", "rm_id")},
                            writeback=False).to_dict()
    finally:
        RUN_SLOTS.release()


@router.post("/agent/resolve")
def agent_resolve(req: ResolveRequest):
    a = _asof(req.as_of)
    key = f"{req.event_id}|{a}"
    if not req.force and load_cached("resolve", key) is None:
        seeded = _holdout_card(req.event_id, a)
        if seeded:
            store_cached("resolve", key, seeded, {"source": "evaluation run"})
    return JOBS.submit("resolve", key, lambda: _resolve(req.event_id, a), force=req.force,
                       meta={"event_id": req.event_id, "as_of": a}).to_dict()


@router.post("/agent/accept")
def agent_accept(req: AcceptRequest):
    """Close the loop: write the accepted recommendation as append-only rows and retain it in Hindsight."""
    a = _asof(req.as_of)
    key = f"{req.event_id}|{a}"
    hit = load_cached("resolve", key)
    if not hit:
        raise HTTPException(status_code=404, detail="resolve the disruption first")
    card_d = hit["result"]
    if card_d.get("writeback"):
        raise HTTPException(status_code=409, detail=f"already accepted as {card_d['writeback'].get('decision_id')}")
    if not card_d.get("recommended_action") or not card_d.get("scenario_state"):
        raise HTTPException(status_code=400, detail="this card has no recommendation to accept")
    ss = card_d["scenario_state"]
    con = _con(a)
    try:
        state = build_state(con, rpo_id=ss["rpo_id"], run_ids=ss["run_ids"], day0=ss["day0"], rm_id=ss["rm_id"])
    finally:
        con.close()
    card = DecisionCard(**card_d)
    try:
        writeback, mocks = log_decision(S, S.live_db_path, _agent().memory, card, state, event_ref=req.event_id)
    except Exception as ex:  # surface Hindsight/DB failures as a readable error
        raise HTTPException(status_code=502, detail=f"{type(ex).__name__}: {ex}") from ex
    card_d["writeback"], card_d["mock_actions"] = writeback, mocks
    store_cached("resolve", key, card_d, {**hit.get("meta", {}), "accepted": True})
    return {"writeback": writeback, "mock_actions": mocks, "card": card_d}


@router.post("/agent/brief")
def agent_brief(req: BriefRequest):
    a = _asof(req.as_of)

    def run():
        from api.server import RUN_SLOTS, RUN_QUEUE_TIMEOUT_S
        if not RUN_SLOTS.acquire(timeout=RUN_QUEUE_TIMEOUT_S):
            raise RuntimeError("the agent is busy with other requests - try again in a minute")
        try:
            return brief(_agent(), req.entity_id, a).to_dict()
        finally:
            RUN_SLOTS.release()

    return JOBS.submit("brief", f"{req.entity_id}|{a}", run, force=req.force,
                       meta={"entity_id": req.entity_id, "as_of": a}).to_dict()


@router.post("/agent/ask")
def agent_ask(req: AskRequest):
    a = _asof(req.as_of)

    def run():
        from api.server import RUN_SLOTS, RUN_QUEUE_TIMEOUT_S
        if not RUN_SLOTS.acquire(timeout=RUN_QUEUE_TIMEOUT_S):
            raise RuntimeError("the agent is busy with other requests - try again in a minute")
        try:
            agent = _agent()
            if req.mode == "history":
                return {"mode": "history", **agent.answer_question(req.question, a).to_dict()}
            return {"mode": "ask", **ask(agent, req.question, a).to_dict()}
        finally:
            RUN_SLOTS.release()

    return JOBS.submit(f"ask-{req.mode}", f"{req.question.strip()}|{a}", run,
                       meta={"question": req.question, "as_of": a}).to_dict()


@router.get("/jobs/{job_id}")
def job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="unknown job (the server may have restarted)")
    return j.to_dict()
