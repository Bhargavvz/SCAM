"""Dashboard, alerts, reports, AI insights and data/memory sync status."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.db import TODAY, get_db, last_complete_month, now_iso, one, page, rows, scalar, shift, to_csv, tx
from app.services import alerts as alert_engine
from app.services import forecast as fc
from app.services import insights as ai
from app.services.llm import LLMError

router = APIRouter(prefix="/api", tags=["dashboard", "alerts", "reports", "insights", "system"])


# ================================================================ dashboard
def kpis(con) -> dict:
    return one(con, f"""SELECT
        (SELECT ROUND(SUM(order_value), 0) FROM sales_orders WHERE order_date > '{shift(-30)}' AND status != 'cancelled') AS revenue_30d,
        (SELECT ROUND(SUM(order_value), 0) FROM sales_orders WHERE order_date BETWEEN '{shift(-60)}' AND '{shift(-31)}' AND status != 'cancelled') AS revenue_prev_30d,
        (SELECT COUNT(*) FROM sales_orders WHERE order_date > '{shift(-30)}' AND status != 'cancelled') AS orders_30d,
        (SELECT ROUND(1.0 * SUM(delivered_at <= promised_date) / NULLIF(COUNT(*), 0), 3) FROM sales_orders
          WHERE delivered_at > '{shift(-30)}') AS otd_30d,
        (SELECT ROUND(SUM(s.on_hand * p.unit_cost), 0) FROM stock_levels s JOIN products p USING (product_id) WHERE s.on_hand > 0) AS inventory_value,
        (SELECT COUNT(*) FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id) WHERE s.on_hand - s.allocated < p.min_stock) AS below_min,
        (SELECT COUNT(*) FROM purchase_orders WHERE status IN ('open', 'partial')) + (SELECT COUNT(*) FROM rm_purchase_orders WHERE status IN ('open', 'partial')) AS open_pos,
        (SELECT COUNT(*) FROM purchase_orders WHERE status IN ('open', 'partial') AND expected_at BETWEEN '{shift(-90)}' AND '{shift(-1)}') AS late_pos,
        (SELECT COUNT(*) FROM purchase_orders WHERE status IN ('open', 'partial') AND expected_at < '{shift(-90)}') AS stale_pos,
        (SELECT COUNT(*) FROM shipments WHERE status = 'in_transit') AS in_transit,
        (SELECT COUNT(*) FROM shipments WHERE status = 'in_transit' AND promised_date < '{TODAY}') AS late_shipments,
        (SELECT COUNT(*) FROM returns WHERE status IN ('authorized', 'received')) AS open_returns,
        (SELECT COUNT(*) FROM alerts WHERE status = 'open' AND severity = 'critical') AS critical_alerts,
        (SELECT ROUND(AVG(otif_rate), 3) FROM supplier_scorecard_monthly WHERE month = '{last_complete_month()}') AS supplier_otif,
        '{last_complete_month()}' AS scorecard_month""")


@router.get("/dashboard")
def dashboard(con=Depends(get_db)):
    snap = scalar(con, "SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= ?", (TODAY,))
    flow = [
        {"stage": "Suppliers", "primary": scalar(con, "SELECT COUNT(DISTINCT supplier_id) FROM purchase_orders WHERE ordered_at > ?", (shift(-90),)),
         "label": "active (90d)", "issue": scalar(con, "SELECT COUNT(*) FROM alerts WHERE kind = 'supplier_otif' AND status = 'open'"),
         "issue_label": "performance drops", "link": "/suppliers"},
        {"stage": "Procurement", "primary": scalar(con, "SELECT COUNT(*) FROM purchase_orders WHERE status IN ('open','partial')") +
         scalar(con, "SELECT COUNT(*) FROM rm_purchase_orders WHERE status IN ('open','partial')"), "label": "open POs",
         "issue": scalar(con, "SELECT COUNT(*) FROM purchase_orders WHERE status IN ('open','partial') AND expected_at BETWEEN ? AND ?", (shift(-90), shift(-1))),
         "issue_label": "overdue (90d)", "link": "/purchasing"},
        {"stage": "Inventory", "primary": scalar(con, "SELECT SUM(on_hand) FROM stock_levels WHERE on_hand > 0"), "label": "units on hand",
         "issue": scalar(con, "SELECT COUNT(*) FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id) WHERE s.on_hand - s.allocated < p.min_stock"),
         "issue_label": "below min", "link": "/inventory"},
        {"stage": "Production", "primary": scalar(con, "SELECT COUNT(*) FROM production_runs WHERE planned_start BETWEEN ? AND ?", (shift(-7), shift(7))),
         "label": "runs ±7 days", "issue": scalar(con, "SELECT COUNT(*) FROM rm_inventory_snapshots_weekly WHERE snapshot_date = ? AND on_hand_units < safety_stock_units", (snap,)),
         "issue_label": "materials below safety", "link": "/production"},
        {"stage": "Warehouse", "primary": scalar(con, "SELECT COUNT(*) FROM sales_orders WHERE status IN ('confirmed','allocated','picking','packed')"),
         "label": "orders to fulfil", "issue": scalar(con, "SELECT COUNT(*) FROM warehouse_tasks WHERE status = 'open'"), "issue_label": "open tasks",
         "link": "/warehouses"},
        {"stage": "Shipping", "primary": scalar(con, "SELECT COUNT(*) FROM shipments WHERE status = 'in_transit'"), "label": "in transit",
         "issue": scalar(con, "SELECT COUNT(*) FROM shipments WHERE status = 'in_transit' AND promised_date < ?", (TODAY,)), "issue_label": "late",
         "link": "/logistics"},
        {"stage": "Customers", "primary": scalar(con, "SELECT COUNT(*) FROM sales_orders WHERE order_date > ?", (shift(-30),)),
         "label": "orders (30d)", "issue": scalar(con, "SELECT COUNT(*) FROM returns WHERE status IN ('authorized','received')"),
         "issue_label": "open returns", "link": "/sales"},
    ]
    return {
        "today": TODAY, "kpis": kpis(con), "flow": flow,
        "revenue_monthly": rows(con, """SELECT substr(order_date, 1, 7) AS month, ROUND(SUM(order_value), 0) AS revenue, COUNT(*) AS orders
                                        FROM sales_orders WHERE order_date > ? AND order_date <= ? AND status != 'cancelled'
                                        GROUP BY 1 ORDER BY 1""", (shift(-395), TODAY)),
        "inventory_by_warehouse": rows(con, """SELECT s.warehouse_id, ROUND(SUM(s.on_hand * p.unit_cost), 0) AS value,
                                                      ROUND(1.0 * SUM(s.on_hand) / w.capacity_units, 3) AS utilization
                                               FROM stock_levels s JOIN products p USING (product_id) JOIN warehouses w USING (warehouse_id)
                                               WHERE s.on_hand > 0 GROUP BY 1 ORDER BY 1"""),
        "alerts": rows(con, """SELECT alert_id, severity, module, title, detail, entity_type, entity_id, created_at FROM alerts
                               WHERE status = 'open' ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, alert_id DESC LIMIT 8"""),
        "alert_summary": alert_engine.summary(con),
        "activity": rows(con, "SELECT at, actor, module, action, entity_type, entity_id, summary FROM audit_log ORDER BY audit_id DESC LIMIT 12"),
        "insights": ai.compute(con)["insights"][:5],
    }


# ================================================================ alerts
@router.get("/alerts")
def list_alerts(status: str = "open", severity: str = "", module: str = "", q: str = "",
                limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    where, params = ["(title LIKE ? OR detail LIKE ? OR entity_id LIKE ?)"], [f"%{q}%"] * 3
    for col, v in (("status", status), ("severity", severity), ("module", module)):
        if v and v != "all":
            where.append(f"{col} = ?")
            params.append(v)
    res = page(con, "SELECT * FROM alerts", where, params, sort=None,
               default_sort="CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, alert_id DESC",
               allowed=set(), limit=limit, offset=offset)
    res["summary"] = alert_engine.summary(con)
    return res


class AlertAction(BaseModel):
    action: str = Field(..., pattern="^(acknowledge|resolve|reopen)$")
    note: Optional[str] = None


@router.post("/alerts/{alert_id}")
def alert_action(alert_id: int, body: AlertAction, con=Depends(get_db)):
    a = one(con, "SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
    if not a:
        raise HTTPException(status_code=404, detail="alert not found")
    status = {"acknowledge": "acknowledged", "resolve": "resolved", "reopen": "open"}[body.action]
    with tx(con):
        con.execute("""UPDATE alerts SET status = ?, acknowledged_by = CASE WHEN ? = 'acknowledged' THEN 'planner' ELSE acknowledged_by END,
                       acknowledged_at = CASE WHEN ? = 'acknowledged' THEN ? ELSE acknowledged_at END,
                       resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE NULL END WHERE alert_id = ?""",
                    (status, status, status, now_iso(), status, now_iso(), alert_id))
        from app.db import audit
        audit(con, module="alerts", action=body.action, entity_type="alert", entity_id=str(alert_id),
              summary=f"{body.action.title()}d alert: {a['title']}" + (f" - {body.note}" if body.note else ""))
    return one(con, "SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))


@router.post("/alerts-refresh")
def alerts_refresh(con=Depends(get_db)):
    return alert_engine.refresh(con)


# ================================================================ reports
def _report(name: str, con) -> dict:
    y = shift(-365)
    if name == "inventory":
        return {
            "by_warehouse": rows(con, """SELECT s.warehouse_id, w.warehouse_name, SUM(s.on_hand) AS units, ROUND(SUM(s.on_hand * p.unit_cost), 0) AS value,
                                                ROUND(1.0 * SUM(s.on_hand) / w.capacity_units, 3) AS utilization
                                         FROM stock_levels s JOIN products p USING (product_id) JOIN warehouses w USING (warehouse_id)
                                         WHERE s.on_hand > 0 GROUP BY 1 ORDER BY value DESC"""),
            "by_category": rows(con, """SELECT p.category, SUM(s.on_hand) AS units, ROUND(SUM(s.on_hand * p.unit_cost), 0) AS value,
                                               ROUND(SUM(s.on_hand * p.unit_cost) / NULLIF(SUM(pol.avg_weekly_demand * p.unit_cost) * 52, 0), 2) AS years_of_cover
                                        FROM stock_levels s JOIN products p USING (product_id) JOIN stock_policies pol USING (product_id, warehouse_id)
                                        WHERE s.on_hand > 0 GROUP BY 1 ORDER BY value DESC"""),
            "by_abc": rows(con, """SELECT x.abc_class, COUNT(DISTINCT s.product_id) AS products, ROUND(SUM(s.on_hand * p.unit_cost), 0) AS value
                                   FROM stock_levels s JOIN products p USING (product_id) JOIN products_ext x USING (product_id)
                                   WHERE s.on_hand > 0 GROUP BY 1 ORDER BY 1"""),
            "cover_bands": rows(con, """SELECT band, COUNT(*) AS positions, ROUND(SUM(value), 0) AS value FROM (
                                          SELECT s.on_hand * p.unit_cost AS value,
                                                 CASE WHEN pol.avg_weekly_demand = 0 THEN '5 no demand'
                                                      WHEN s.on_hand / pol.avg_weekly_demand <= 13 THEN '1 under 13 weeks'
                                                      WHEN s.on_hand / pol.avg_weekly_demand <= 52 THEN '2 13-52 weeks'
                                                      WHEN s.on_hand / pol.avg_weekly_demand <= 104 THEN '3 1-2 years'
                                                      ELSE '4 over 2 years' END AS band
                                          FROM stock_levels s JOIN products p USING (product_id) JOIN stock_policies pol USING (product_id, warehouse_id)
                                          WHERE s.on_hand > 0) GROUP BY 1 ORDER BY 1"""),
        }
    if name == "sales":
        return {
            "monthly": rows(con, """SELECT substr(order_date, 1, 7) AS month, COUNT(*) AS orders, ROUND(SUM(order_value), 0) AS revenue,
                                           ROUND(AVG(order_value), 0) AS avg_order
                                    FROM sales_orders WHERE order_date > ? AND order_date <= ? AND status != 'cancelled' GROUP BY 1 ORDER BY 1""", (y, TODAY)),
            "by_category": rows(con, """SELECT p.category, SUM(l.qty_shipped) AS units, ROUND(SUM(l.qty_shipped * l.unit_price), 0) AS revenue,
                                               ROUND(SUM(l.qty_shipped * (l.unit_price - p.unit_cost)), 0) AS gross_margin
                                        FROM sales_order_lines l JOIN sales_orders o USING (sales_order_id) JOIN products p USING (product_id)
                                        WHERE o.order_date > ? GROUP BY 1 ORDER BY revenue DESC""", (y,)),
            "by_segment": rows(con, """SELECT c.segment, COUNT(*) AS orders, ROUND(SUM(o.order_value), 0) AS revenue FROM sales_orders o
                                       JOIN customers c USING (customer_id) WHERE o.order_date > ? AND o.status != 'cancelled' GROUP BY 1 ORDER BY revenue DESC""", (y,)),
            "top_customers": rows(con, """SELECT c.customer_id, c.customer_name, c.segment, COUNT(*) AS orders, ROUND(SUM(o.order_value), 0) AS revenue
                                          FROM sales_orders o JOIN customers c USING (customer_id) WHERE o.order_date > ? AND o.status != 'cancelled'
                                          GROUP BY 1 ORDER BY revenue DESC LIMIT 15""", (y,)),
            "top_products": rows(con, """SELECT l.product_id, p.sku, p.category, SUM(l.qty_shipped) AS units, ROUND(SUM(l.qty_shipped * l.unit_price), 0) AS revenue
                                         FROM sales_order_lines l JOIN sales_orders o USING (sales_order_id) JOIN products p USING (product_id)
                                         WHERE o.order_date > ? GROUP BY 1 ORDER BY revenue DESC LIMIT 15""", (y,)),
        }
    if name == "costs":
        return {
            "monthly": rows(con, """SELECT m.month, COALESCE(p.goods, 0) AS purchasing_goods, COALESCE(r.material, 0) AS purchasing_materials,
                                           COALESCE(f.freight, 0) AS freight, COALESCE(rt.refunds, 0) AS return_refunds
                                    FROM (SELECT DISTINCT substr(order_date, 1, 7) AS month FROM sales_orders WHERE order_date > :y AND order_date <= :t) m
                                    LEFT JOIN (SELECT substr(o.ordered_at, 1, 7) month, ROUND(SUM(l.quantity_ordered * l.unit_cost), 0) goods
                                               FROM purchase_orders o JOIN purchase_order_lines l USING (purchase_order_id) WHERE o.status != 'cancelled' GROUP BY 1) p USING (month)
                                    LEFT JOIN (SELECT substr(o.ordered_at, 1, 7) month, ROUND(SUM(l.quantity_ordered * l.unit_cost), 0) material
                                               FROM rm_purchase_orders o JOIN rm_purchase_order_lines l USING (rm_purchase_order_id) WHERE o.status != 'cancelled' GROUP BY 1) r USING (month)
                                    LEFT JOIN (SELECT substr(ship_date, 1, 7) month, ROUND(SUM(freight_cost), 0) freight FROM shipments GROUP BY 1) f USING (month)
                                    LEFT JOIN (SELECT substr(requested_at, 1, 7) month, ROUND(SUM(refund_value), 0) refunds FROM returns GROUP BY 1) rt USING (month)
                                    ORDER BY 1""", {"y": y, "t": TODAY}),
            "decisions": rows(con, """SELECT decision_type, COUNT(*) AS decisions, ROUND(SUM(expected_cost), 0) AS expected_cost,
                                             ROUND(SUM(actual_cost), 0) AS actual_cost FROM decisions
                                      WHERE decided_at > ? AND actual_cost IS NOT NULL GROUP BY 1 ORDER BY actual_cost DESC""", (y,)),
        }
    if name == "suppliers":
        return {
            "ranking": rows(con, f"""SELECT s.supplier_id, s.supplier_name, s.country_code, ROUND(AVG(c.otif_rate), 3) AS otif,
                                            ROUND(AVG(c.avg_delay_days), 1) AS avg_delay_days, ROUND(AVG(c.fill_rate), 3) AS fill_rate,
                                            ROUND(AVG(c.quality_ppm), 0) AS quality_ppm, SUM(c.po_count) AS pos,
                                            SUM(c.commitments_kept) AS commitments_kept, SUM(c.commitments_made) AS commitments_made
                                     FROM supplier_scorecard_monthly c JOIN suppliers s USING (supplier_id)
                                     WHERE c.month > '{y}' AND c.month <= '{last_complete_month()}' GROUP BY 1 ORDER BY otif DESC"""),
            "by_country": rows(con, f"""SELECT s.country_code, COUNT(DISTINCT s.supplier_id) AS suppliers, ROUND(AVG(c.otif_rate), 3) AS otif,
                                               ROUND(AVG(c.avg_delay_days), 1) AS avg_delay_days
                                        FROM supplier_scorecard_monthly c JOIN suppliers s USING (supplier_id)
                                        WHERE c.month > '{y}' AND c.month <= '{last_complete_month()}' GROUP BY 1 ORDER BY otif DESC"""),
            "monthly": rows(con, f"""SELECT substr(month, 1, 7) AS month, ROUND(AVG(otif_rate), 3) AS otif, ROUND(AVG(avg_delay_days), 2) AS delay
                                     FROM supplier_scorecard_monthly WHERE month > '{shift(-730)}' AND month <= '{last_complete_month()}' GROUP BY 1 ORDER BY 1"""),
        }
    raise HTTPException(status_code=404, detail="unknown report")


@router.get("/reports/{name}")
def report(name: str, con=Depends(get_db)):
    return _report(name, con)


@router.get("/reports/{name}/{section}.csv", response_class=PlainTextResponse)
def report_csv(name: str, section: str, con=Depends(get_db)):
    data = _report(name, con).get(section)
    if data is None:
        raise HTTPException(status_code=404, detail="unknown section")
    return PlainTextResponse(to_csv(data), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="meridian_{name}_{section}_{TODAY}.csv"'})


# ================================================================ forecasting
@router.get("/forecast")
def forecast(level: str = "total", key: Optional[str] = None, horizon: int = Query(12, ge=1, le=52), con=Depends(get_db)):
    if level not in ("total", "category", "product", "warehouse"):
        raise HTTPException(status_code=400, detail="level must be total, category, product or warehouse")
    if level != "total" and not key:
        raise HTTPException(status_code=400, detail="key is required")
    return fc.forecast(con, level, key, horizon)


@router.get("/forecast/overview")
def forecast_overview(con=Depends(get_db)):
    cats = []
    for c in [r["category"] for r in rows(con, "SELECT DISTINCT category FROM products ORDER BY 1")]:
        f = fc.forecast(con, "category", c, horizon=12, history=0)
        cats.append({"category": c, "next_4w": f["next_4w"], "last_4w": f["last_4w"], "same_4w_last_year": f["same_4w_last_year"],
                     "next_12w": round(sum(x["forecast"] for x in f["forecast"]), 1), "model": f["model"]["method"],
                     **{k: v for k, v in (f["accuracy"] or {}).items() if k != "weeks"}})
    top = rows(con, """SELECT d.product_id, p.sku, p.category, SUM(d.actual_demand_units) AS units_13w
                       FROM product_demand_weekly d JOIN products p USING (product_id) WHERE d.week_start > ?
                       GROUP BY 1 ORDER BY units_13w DESC LIMIT 20""", (shift(-91),))
    return {"categories": cats, "top_products": top}


# ================================================================ AI insights
@router.get("/insights")
def insights(refresh: bool = False, con=Depends(get_db)):
    return ai.compute(con, use_cache=not refresh)


@router.post("/insights/{insight_id}/explain")
def explain(insight_id: str, con=Depends(get_db)):
    try:
        return ai.explain(con, insight_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="insight no longer applies - refresh the list")
    except LLMError as ex:
        raise HTTPException(status_code=503, detail=str(ex))


@router.post("/insights/briefing")
def briefing(con=Depends(get_db)):
    try:
        return ai.briefing(con, kpis(con))
    except LLMError as ex:
        raise HTTPException(status_code=503, detail=str(ex))


class Question(BaseModel):
    question: str = Field(..., min_length=4, max_length=500)


@router.post("/insights/ask")
def ask(body: Question, con=Depends(get_db)):
    try:
        return ai.ask(con, body.question)
    except LLMError as ex:
        raise HTTPException(status_code=503, detail=str(ex))


# ================================================================ system: data + memory sync
@router.get("/system/sync")
def sync_status(con=Depends(get_db)):
    """Is the operational database still the dataset plus audited application changes? And what does memory cover?"""
    manifest = json.loads(settings.manifest_path.read_text()) if settings.manifest_path.exists() else {}
    src_ok = settings.source_db.exists()
    tables = []
    if src_ok:
        con.execute("ATTACH DATABASE ? AS src", (f"file:{settings.source_db}?mode=ro",))
        try:
            audited = {r[0] for r in con.execute("SELECT DISTINCT entity_id FROM audit_log")}
            created = {r[0] for r in con.execute("SELECT record_id FROM app_records")}
            for (t,) in con.execute("SELECT name FROM src.sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
                src_n = scalar(con, f"SELECT COUNT(*) FROM src.{t}")
                cur_n = scalar(con, f"SELECT COUNT(*) FROM main.{t}")
                changed = con.execute(f"SELECT * FROM src.{t} EXCEPT SELECT * FROM main.{t}").fetchall()
                unexplained = [r[0] for r in changed
                               if not any(str(v) in audited or str(v) in created for v in list(r)[:3])]
                tables.append({"table": t, "source_rows": src_n, "current_rows": cur_n, "changed_by_app": len(changed),
                               "unexplained_changes": len(unexplained), "sample_unexplained": unexplained[:5],
                               "added_by_app": max(0, cur_n - src_n),
                               "status": "identical" if not changed and cur_n == src_n else
                                         ("in sync (audited changes)" if not unexplained else "DIVERGED")})
        finally:
            con.execute("DETACH DATABASE src")
    corpus = settings.dataset_dir / "memory_corpus.jsonl"
    mem = {"corpus_docs": 0, "record_refs": 0, "record_refs_resolved": 0}
    if corpus.exists():
        ids = []
        for line in corpus.open():
            d = json.loads(line)
            mem["corpus_docs"] += 1
            ids += d.get("source_record_ids") or []
        mem["record_refs"] = len(ids)
        check = {"EVT": ("disruption_events", "event_id"), "RPO": ("rm_purchase_orders", "rm_purchase_order_id"),
                 "PO": ("purchase_orders", "purchase_order_id"), "DEC": ("decisions", "decision_id"),
                 "CMT": ("commitments", "commitment_id"), "NEG": ("negotiations", "negotiation_id"),
                 "SUP": ("suppliers", "supplier_id"), "RM": ("raw_materials", "rm_id"), "PR": ("production_runs", "production_run_id"),
                 "CON": ("contracts", "contract_id"),
                 "REV": ("rm_po_revisions", "revision_id"), "POL": ("purchase_order_lines", "purchase_order_line_id"),
                 "RPOL": ("rm_purchase_order_lines", "rm_purchase_order_line_id"), "RT": ("rm_inventory_movements", "transfer_id")}
        ok = 0
        for rid in ids:
            pref = next((p for p in sorted(check, key=len, reverse=True) if rid.startswith(p)), None)
            if pref and scalar(con, f"SELECT 1 FROM {check[pref][0]} WHERE {check[pref][1]} = ?", (rid,)):
                ok += 1
        mem["record_refs_resolved"] = ok
    ledger = settings.db_path.parent / "retention_log.jsonl"
    hs = {"ledger_found": ledger.exists(), "retained": 0, "failed": 0}
    if ledger.exists():
        seen = {}
        for line in ledger.open():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            seen[e.get("doc_id") or e.get("document_id")] = e.get("status")
        hs["retained"] = sum(1 for s in seen.values() if s in ("ok", "stored", "success"))
        hs["failed"] = sum(1 for s in seen.values() if s not in ("ok", "stored", "success"))
    return {
        "business_date": TODAY,
        "source": {"path": str(settings.source_db), "exists": src_ok,
                   "sha256_at_build": manifest.get("source", {}).get("sha256"), "built_at": manifest.get("built_at")},
        "reconciliation": manifest.get("reconciliation"), "lineage": manifest.get("lineage"),
        "tables": tables,
        "app_activity": {"audit_entries": scalar(con, "SELECT COUNT(*) FROM audit_log"),
                         "records_created": scalar(con, "SELECT COUNT(*) FROM app_records")},
        "memory": {**mem, "hindsight": hs,
                   "pending_for_memory": scalar(con, "SELECT COUNT(*) FROM audit_log"),
                   "note": "Phase 2 retains every audited application event to Hindsight so memory stays in step with the database."},
    }


@router.get("/system/source-check")
def source_check():
    from app.build_db import sha256
    manifest = json.loads(settings.manifest_path.read_text()) if settings.manifest_path.exists() else {}
    now = sha256(settings.source_db) if settings.source_db.exists() else None
    return {"sha256_now": now, "sha256_at_build": manifest.get("source", {}).get("sha256"),
            "unchanged": now is not None and now == manifest.get("source", {}).get("sha256")}


@router.get("/activity")
def activity(module: str = "", limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    where, params = [], []
    if module:
        where.append("module = ?")
        params.append(module)
    return page(con, "SELECT * FROM audit_log", where, params, sort=None, default_sort="audit_id DESC", allowed=set(),
                limit=limit, offset=offset)
