"""Risk & disruptions: disruption events, the memory-backed decision agent, and accepting its recommendation.

Resolve: the agent (vendored from the decision-agent project) recalls precedents from Hindsight, reads ground truth
through as-of views of this database, simulates every response deterministically, checks open commitments, and
recommends one action with cited evidence.
Accept: the recommendation becomes real operational changes in Meridian (expedite -> revised expected date, switch ->
spot material PO, cancel -> cancelled PO, reallocate -> plant transfer, accept delay -> rescheduled run), plus a decision
row, the commitment it creates, and an audit entry that the memory outbox retains to Hindsight.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import TODAY, audit, created, get_db, like, must, next_id, now_iso, one, page, rows, scalar, shift, tx
from app.jobs import JOBS, load_cached, store_cached
from app.services import alerts as alert_engine

router = APIRouter(prefix="/api", tags=["risk"])


def _agent_settings():
    from agent.config import load_settings
    return load_settings()


def _asof_con():
    from agent.db import connect
    s = _agent_settings()
    return connect(s.db_path, s.live_db_path, TODAY)


def event_context(con, event_id: str) -> dict | None:
    """Slipped raw-material PO, its material and the dependent production runs, derived from the event's evidence."""
    from agent import sql_tools
    row = con.execute("SELECT anchor_ids FROM main.disruption_events WHERE event_id = ?", (event_id,)).fetchone()
    anchors = [a for a in (json.loads(row[0]) if row and row[0] else []) if sql_tools.record_exists(con, a)]
    rpos = [a for a in anchors if a.startswith("RPO")]
    if not rpos:
        return None
    revised = con.execute(f"""SELECT rm_po_id, MAX(revised_at) FROM rm_po_revisions WHERE rm_po_id IN ({','.join('?' * len(rpos))})
                              AND revised_at <= ? GROUP BY rm_po_id ORDER BY 2 DESC, rm_po_id""", (*rpos, TODAY)).fetchall()
    if not revised:
        return None
    rpo = revised[0][0]
    po = con.execute("SELECT plant_id, supplier_id FROM rm_purchase_orders WHERE rm_purchase_order_id = ?", (rpo,)).fetchone()
    rm = con.execute("SELECT rm_id FROM rm_purchase_order_lines WHERE rm_purchase_order_id = ? LIMIT 1", (rpo,)).fetchone()
    if not po or not rm:
        return None
    rm = rm[0]
    runs = [a for a in anchors if a.startswith("PR")]
    if runs:
        runs = [r[0] for r in con.execute(f"""SELECT r.production_run_id FROM production_runs r JOIN bill_of_materials b
                                               ON b.product_id = r.product_id AND b.rm_id = ?
                                               WHERE r.production_run_id IN ({','.join('?' * len(runs))}) ORDER BY r.planned_start""",
                                           (rm, *runs))]
    if not runs:
        runs = [r[0] for r in con.execute("""SELECT DISTINCT r.production_run_id FROM production_runs r
                                             JOIN bill_of_materials b ON b.product_id = r.product_id AND b.rm_id = ?
                                             WHERE r.plant_id = ? AND r.planned_start >= ? ORDER BY r.planned_start LIMIT 3""",
                                          (rm, po[0], shift(-14)))]
    return {"rpo_id": rpo, "run_ids": runs, "rm_id": rm, "supplier_id": po[1], "plant_id": po[0]} if runs else None


@router.get("/risk/summary")
def risk_summary(con=Depends(get_db)):
    return {
        "open": scalar(con, "SELECT COUNT(*) FROM disruption_events WHERE end_date IS NULL AND detected_at <= ?", (TODAY,)),
        "new_30d": scalar(con, "SELECT COUNT(*) FROM disruption_events WHERE detected_at > ? AND detected_at <= ?", (shift(-30), TODAY)),
        "decided_in_meridian": scalar(con, "SELECT COUNT(*) FROM decisions WHERE decided_by_role LIKE '%Meridian%'"),
        "by_type": rows(con, """SELECT event_type, COUNT(*) AS n FROM disruption_events WHERE detected_at > ? AND detected_at <= ?
                                GROUP BY 1 ORDER BY n DESC""", (shift(-365), TODAY)),
        "monthly": rows(con, """SELECT substr(detected_at, 1, 7) AS month, COUNT(*) AS n, ROUND(AVG(severity), 2) AS severity
                                FROM disruption_events WHERE detected_at > ? AND detected_at <= ? GROUP BY 1 ORDER BY 1""", (shift(-730), TODAY)),
        "outcomes": rows(con, """SELECT decision_type, COUNT(*) AS n, ROUND(AVG(outcome_label = 'success'), 3) AS success_rate,
                                        ROUND(SUM(actual_cost) / NULLIF(SUM(expected_cost), 0), 2) AS cost_ratio
                                 FROM decisions WHERE outcome_label IS NOT NULL GROUP BY 1 ORDER BY n DESC"""),
    }


@router.get("/risk/events")
def events(status: str = "open", q: str = "", event_type: str = "", limit: int = Query(50, le=200), offset: int = 0, con=Depends(get_db)):
    where, params = ["e.detected_at <= ?", "(e.title LIKE ? OR e.event_id LIKE ? OR e.origin_entity_id LIKE ?)"], [TODAY, *[like(q)] * 3]
    if status == "open":
        where.append("e.end_date IS NULL")
    elif status == "recent":
        where.append("e.detected_at > ?")
        params.append(shift(-60))
    if event_type:
        where.append("e.event_type = ?")
        params.append(event_type)
    res = page(con, """SELECT e.event_id, e.event_type, e.title, e.detected_at, e.start_date, e.end_date, e.severity, e.origin_entity_type,
                              e.origin_entity_id, e.root_cause_code,
                              (SELECT decision_id FROM decisions d WHERE d.event_id = e.event_id ORDER BY decided_at DESC LIMIT 1) AS decision_id,
                              (e.anchor_ids LIKE '%RPO%') AS has_po
                       FROM disruption_events e""", where, params, sort=None, default_sort="detected_at DESC, event_id DESC",
               allowed=set(), limit=limit, offset=offset)
    for r in res["rows"]:
        r["resolution_cached"] = load_cached("resolve", f"{r['event_id']}|{TODAY}") is not None
    return res


@router.get("/risk/events/{event_id}")
def event(event_id: str, con=Depends(get_db)):
    ev = must(one(con, "SELECT * FROM disruption_events WHERE event_id = ? AND detected_at <= ?", (event_id, TODAY)), "disruption")
    ac = _asof_con()
    try:
        ctx = event_context(ac, event_id)
    finally:
        ac.close()
    affected = []
    if ctx:
        affected = rows(con, f"""SELECT r.production_run_id, r.plant_id, r.product_id, p.sku, r.planned_start, r.planned_qty, r.actual_start
                                 FROM production_runs r JOIN products p USING (product_id)
                                 WHERE r.production_run_id IN ({','.join('?' * len(ctx['run_ids']))})""", tuple(ctx["run_ids"]))
    cached = load_cached("resolve", f"{event_id}|{TODAY}")
    return {"event": ev,
            "impacts": rows(con, "SELECT entity_type, entity_id, impact_metric, impact_value, unit FROM event_impacts WHERE event_id = ?", (event_id,)),
            "links": rows(con, """SELECT l.link_type, CASE WHEN l.src_event_id = :e THEN l.dst_event_id ELSE l.src_event_id END AS other_id,
                                         o.title AS other_title, o.detected_at AS other_detected
                                  FROM event_links l JOIN disruption_events o
                                    ON o.event_id = CASE WHEN l.src_event_id = :e THEN l.dst_event_id ELSE l.src_event_id END
                                  WHERE (l.src_event_id = :e OR l.dst_event_id = :e) AND o.detected_at <= :t
                                  ORDER BY o.detected_at DESC LIMIT 15""", {"e": event_id, "t": TODAY}),
            "decisions": rows(con, """SELECT decision_id, decided_at, decided_by_role, decision_type, expected_cost, expected_stockout_days,
                                             actual_cost, outcome_label FROM decisions WHERE event_id = ? ORDER BY decided_at""", (event_id,)),
            "context": ctx, "affected_runs": affected,
            "resolution": cached["result"] if cached else None, "resolution_meta": cached.get("meta") if cached else None}


def _resolve(event_id: str) -> dict:
    from agent import agent_core
    s = _agent_settings()
    ac = _asof_con()
    try:
        ev = ac.execute("SELECT title, detected_at FROM disruption_events WHERE event_id = ?", (event_id,)).fetchone()
        if not ev:
            raise ValueError(f"{event_id} is not known on {TODAY}")
        ctx = event_context(ac, event_id)
    finally:
        ac.close()
    if not ctx:
        raise ValueError(f"{event_id} has no slipped material purchase order with dependent production runs - nothing to simulate")
    report = (f"{TODAY} - {ev[0]} ({event_id}, detected {ev[1]}). Purchase order {ctx['rpo_id']} from {ctx['supplier_id']} "
              f"({ctx['rm_id']}, plant {ctx['plant_id']}) slipped; runs {', '.join(ctx['run_ids'])} depend on it. What should we do?")
    agent = agent_core.build_agent(s, live_db_path=s.live_db_path)
    card = agent.run(report, as_of=TODAY, context={k: ctx[k] for k in ("rpo_id", "run_ids", "rm_id")}, writeback=False)
    return card.to_dict()


@router.post("/risk/events/{event_id}/resolve")
def resolve(event_id: str, force: bool = False):
    return JOBS.submit("resolve", f"{event_id}|{TODAY}", lambda: _resolve(event_id), force=force, meta={"event_id": event_id}).to_dict()


def _apply(con, action: str, opt: dict, st, dec_id: str) -> list[str]:
    """Operational effect of the accepted action. Returns human-readable change lines."""
    arr = opt.get("arrival")
    changes = []
    if action == "expedite" and arr:
        for rpo in st.lot_rpo_ids:
            old = scalar(con, "SELECT expected_at FROM rm_purchase_orders WHERE rm_purchase_order_id = ?", (rpo,))
            rev = next_id(con, "rm_po_revisions", "revision_id", "REV", 6)
            con.execute("INSERT INTO rm_po_revisions VALUES (?, ?, ?, ?, ?, 'expedited')", (rev, rpo, TODAY, old, arr))
            created(con, "rm_po_revisions", rev)
            con.execute("UPDATE rm_purchase_orders SET expected_at = ? WHERE rm_purchase_order_id = ?", (arr, rpo))
            changes.append(f"{rpo} expedited: expected {old} -> {arr} ({rev})")
    elif action == "switch_supplier":
        alt = opt.get("alt_supplier_id")
        price = scalar(con, "SELECT unit_price FROM rm_supplier_catalog WHERE rm_id = ? AND supplier_id = ? ORDER BY price_valid_to DESC LIMIT 1",
                       (st.rm_id, alt)) or scalar(con, "SELECT std_unit_cost FROM raw_materials WHERE rm_id = ?", (st.rm_id,))
        rpo = next_id(con, "rm_purchase_orders", "rm_purchase_order_id", "RPO", 6)
        con.execute("INSERT INTO rm_purchase_orders VALUES (?, ?, ?, 'spot', ?, ?, ?, NULL, 'open')", (rpo, alt, st.plant_id, TODAY, arr, arr))
        line = next_id(con, "rm_purchase_order_lines", "rm_purchase_order_line_id", "RPOL", 7)
        con.execute("INSERT INTO rm_purchase_order_lines VALUES (?, ?, ?, ?, 0, ?)", (line, rpo, st.rm_id, round(st.req, 2), price))
        created(con, "rm_purchase_orders", rpo)
        created(con, "rm_purchase_order_lines", line)
        changes.append(f"Spot order {rpo}: {round(st.req, 1)} {st.uom} of {st.rm_id} from {alt} to {st.plant_id}, due {arr}")
    elif action == "cancel_po":
        if opt.get("breaches_commitments"):
            raise HTTPException(status_code=409, detail=f"blocked: cancelling breaches {', '.join(opt['breaches_commitments'])}")
        con.execute("UPDATE rm_purchase_orders SET status = 'cancelled' WHERE rm_purchase_order_id = ?", (st.rpo_id,))
        changes.append(f"{st.rpo_id} cancelled")
    elif action == "reallocate_stock":
        donor = opt.get("donor_plant")
        n = scalar(con, "SELECT MAX(CAST(SUBSTR(transfer_id, 3) AS INTEGER)) FROM rm_inventory_movements WHERE transfer_id LIKE 'RT%'") or 0
        tid = f"RT{n + 1:07d}"
        for plant, q in ((donor, -st.req), (st.plant_id, st.req)):
            mid = next_id(con, "rm_inventory_movements", "rm_movement_id", "RMM", 8)
            con.execute("INSERT INTO rm_inventory_movements VALUES (?, ?, ?, ?, 'transfer', ?, NULL, ?, NULL)",
                        (mid, st.rm_id, plant, TODAY, round(q, 2), tid))
            created(con, "rm_inventory_movements", mid)
        changes.append(f"Transfer {tid}: {round(st.req, 1)} {st.uom} of {st.rm_id} {donor} -> {st.plant_id}")
    elif action == "accept_delay":
        eta = st.eta.isoformat()
        run = one(con, "SELECT planned_start, actual_start FROM production_runs WHERE production_run_id = ?", (st.primary_run_id,))
        if run and run["actual_start"]:
            changes.append(f"{st.primary_run_id} already started on {run['actual_start']}; no reschedule needed")
        elif run and run["planned_start"] < eta:
            con.execute("UPDATE production_runs SET planned_start = ? WHERE production_run_id = ?", (eta, st.primary_run_id))
            changes.append(f"{st.primary_run_id} rescheduled {run['planned_start']} -> {eta}")
    elif action == "substitute_rm":
        changes.append(f"QA approval requested for {opt.get('substitute')} as substitute for {st.rm_id} at {st.plant_id}")
    return changes


@router.post("/risk/events/{event_id}/accept")
def accept(event_id: str, con=Depends(get_db)):
    from agent.log_decision import commitment_for
    from agent.simulate import build_state
    hit = load_cached("resolve", f"{event_id}|{TODAY}")
    if not hit:
        raise HTTPException(status_code=404, detail="resolve the disruption first")
    card = hit["result"]
    if card.get("accepted"):
        raise HTTPException(status_code=409, detail=f"already accepted as {card['accepted']['decision_id']}")
    action = card.get("recommended_action")
    if not action or not card.get("scenario_state"):
        raise HTTPException(status_code=400, detail="this analysis has no recommendation to accept")
    opt = next(o for o in card["options"] if o["action"] == action)
    ss = card["scenario_state"]
    ac = _asof_con()
    try:
        st = build_state(ac, rpo_id=ss["rpo_id"], run_ids=ss["run_ids"], day0=ss["day0"], rm_id=ss["rm_id"])
    finally:
        ac.close()
    c = commitment_for(opt, st)
    with tx(con):
        dec = next_id(con, "decisions", "decision_id", "DEC", 5)
        con.execute("""INSERT INTO decisions (decision_id, event_id, decided_at, decided_by_role, decision_type, options_considered_json,
                       chosen_option, rationale_text, expected_cost, expected_stockout_days, footprint_ids)
                       VALUES (?, ?, ?, 'Planner (Meridian decision agent)', ?, ?, ?, ?, ?, ?, ?)""",
                    (dec, event_id, TODAY, action,
                     json.dumps([{"option": o["action"], "est_cost": o.get("cost"), "est_stockout_days": o.get("stockout_days"),
                                  "risk": o.get("risk"), "note": o.get("note")} for o in card["options"] if o.get("feasible")]),
                     action, card.get("rationale"), opt.get("cost"), opt.get("stockout_days"),
                     json.dumps(card.get("cited_record_ids", [])[:40])))
        created(con, "decisions", dec)
        cmt = next_id(con, "commitments", "commitment_id", "CMT", 5)
        con.execute("""INSERT INTO commitments (commitment_id, decision_id, negotiation_id, counterparty_type, counterparty_id, made_by_role,
                       made_at, commitment_text, quantity, due_date, penalty_or_credit, status, resolved_at, evidence_ids)
                       VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, 'open', NULL, ?)""",
                    (cmt, dec, c["counterparty_type"], c["counterparty_id"], c["made_by_role"], TODAY, c["commitment_text"],
                     c.get("quantity"), c.get("due_date"), json.dumps([event_id, st.rpo_id])))
        created(con, "commitments", cmt)
        changes = _apply(con, action, opt, st, dec)
        audit(con, module="risk", action="decide", entity_type="decision", entity_id=dec,
              summary=(f"Decision {dec} on disruption {event_id}: {action.replace('_', ' ')} for {st.rpo_id} "
                       f"({st.supplier_id}, {st.rm_id} at {st.plant_id}); expected cost ${opt.get('cost') or 0:,.0f}, "
                       f"{opt.get('stockout_days')} stockout days; confidence {card.get('confidence')}. Commitment {cmt}: "
                       f"{c['commitment_text']}. Changes: {'; '.join(changes) or 'none'}. Reasoning: {(card.get('rationale') or '')[:600]}"),
              payload={"event_id": event_id, "decision_id": dec, "commitment_id": cmt, "action": action, "changes": changes,
                       "alternatives": [o["action"] for o in card["options"] if o.get("feasible") and o["action"] != action]})
    accepted = {"decision_id": dec, "commitment_id": cmt, "changes": changes, "at": now_iso()}
    store_cached("resolve", f"{event_id}|{TODAY}", {**card, "accepted": accepted}, hit.get("meta"))
    alert_engine.refresh(con)
    return {"accepted": accepted, "card": {**card, "accepted": accepted}}
