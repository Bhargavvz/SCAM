"""Warehouse operations, logistics & transportation, production planning."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import TODAY, audit, created, get_db, like, must, next_id, now_iso, one, page, rows, scalar, shift, tx
from app.services import alerts as alert_engine

router = APIRouter(prefix="/api", tags=["warehouse", "logistics", "production"])


# ================================================================ warehouses
@router.get("/warehouses")
def warehouses(con=Depends(get_db)):
    return rows(con, f"""
      SELECT w.*, st.units, st.value, ROUND(1.0 * st.units / w.capacity_units, 3) AS utilization,
             COALESCE(inb.due_7d, 0) AS inbound_due_7d, COALESCE(outq.open_orders, 0) AS outbound_open,
             COALESCE(t.open_tasks, 0) AS open_tasks, COALESCE(sh.shipped_30d, 0) AS shipped_30d,
             COALESCE(rc.received_30d, 0) AS received_30d
      FROM warehouses w
      LEFT JOIN (SELECT s.warehouse_id, SUM(s.on_hand) units, ROUND(SUM(s.on_hand * p.unit_cost), 0) value
                 FROM stock_levels s JOIN products p USING (product_id) GROUP BY 1) st USING (warehouse_id)
      LEFT JOIN (SELECT warehouse_id, COUNT(*) due_7d FROM purchase_orders
                 WHERE status IN ('open', 'partial') AND expected_at <= '{shift(7)}' GROUP BY 1) inb USING (warehouse_id)
      LEFT JOIN (SELECT warehouse_id, COUNT(*) open_orders FROM sales_orders
                 WHERE status IN ('confirmed', 'allocated', 'picking', 'packed') GROUP BY 1) outq USING (warehouse_id)
      LEFT JOIN (SELECT warehouse_id, COUNT(*) open_tasks FROM warehouse_tasks WHERE status = 'open' GROUP BY 1) t USING (warehouse_id)
      LEFT JOIN (SELECT warehouse_id, -SUM(quantity_change) shipped_30d FROM inventory_movements
                 WHERE movement_type = 'sale' AND movement_at > '{shift(-30)}' GROUP BY 1) sh USING (warehouse_id)
      LEFT JOIN (SELECT warehouse_id, SUM(quantity_change) received_30d FROM inventory_movements
                 WHERE movement_type = 'receipt' AND movement_at > '{shift(-30)}' GROUP BY 1) rc USING (warehouse_id)
      ORDER BY w.warehouse_id""")


@router.get("/warehouses/{warehouse_id}")
def warehouse(warehouse_id: str, con=Depends(get_db)):
    w = must(next((x for x in warehouses(con) if x["warehouse_id"] == warehouse_id), None), "warehouse")
    return {
        "warehouse": w,
        "receiving": rows(con, """SELECT o.purchase_order_id, o.supplier_id, s.supplier_name, o.expected_at, o.status,
                                         (SELECT SUM(quantity_ordered - COALESCE(quantity_received, 0)) FROM purchase_order_lines l
                                          WHERE l.purchase_order_id = o.purchase_order_id) AS units_due
                                  FROM purchase_orders o JOIN suppliers s USING (supplier_id)
                                  WHERE o.warehouse_id = ? AND o.status IN ('open', 'partial') AND o.expected_at <= ?
                                  ORDER BY o.expected_at LIMIT 50""", (warehouse_id, shift(14))),
        "picking": rows(con, """SELECT o.sales_order_id, o.customer_id, c.customer_name, o.status, o.promised_date, o.order_value,
                                       (SELECT SUM(qty_ordered) FROM sales_order_lines l WHERE l.sales_order_id = o.sales_order_id) AS units
                                FROM sales_orders o JOIN customers c USING (customer_id)
                                WHERE o.warehouse_id = ? AND o.status IN ('confirmed', 'allocated', 'picking', 'packed')
                                ORDER BY o.promised_date""", (warehouse_id,)),
        "shipping": rows(con, """SELECT s.shipment_id, s.sales_order_id, s.carrier_id, s.destination_name, s.units, s.ship_date,
                                        s.promised_date, s.status FROM shipments s
                                 WHERE s.origin_id = ? AND s.direction = 'outbound' AND s.ship_date >= ?
                                 ORDER BY s.ship_date DESC LIMIT 30""", (warehouse_id, shift(-3))),
        "tasks": rows(con, "SELECT * FROM warehouse_tasks WHERE warehouse_id = ? ORDER BY (status = 'open') DESC, created_at DESC LIMIT 40",
                      (warehouse_id,)),
        "activity": rows(con, """SELECT movement_at AS day,
                                        SUM(CASE WHEN movement_type = 'receipt' THEN quantity_change ELSE 0 END) AS received,
                                        -SUM(CASE WHEN movement_type = 'sale' THEN quantity_change ELSE 0 END) AS shipped,
                                        SUM(CASE WHEN movement_type = 'transfer' THEN quantity_change ELSE 0 END) AS transfers
                                 FROM inventory_movements WHERE warehouse_id = ? AND movement_at > ?
                                 GROUP BY 1 ORDER BY 1""", (warehouse_id, shift(-60))),
        "by_category": rows(con, """SELECT p.category, SUM(s.on_hand) AS units, ROUND(SUM(s.on_hand * p.unit_cost), 0) AS value
                                    FROM stock_levels s JOIN products p USING (product_id) WHERE s.warehouse_id = ?
                                    GROUP BY 1 ORDER BY value DESC""", (warehouse_id,)),
    }


@router.post("/warehouse-tasks/{task_id}/complete")
def complete_task(task_id: str, con=Depends(get_db)):
    t = must(one(con, "SELECT * FROM warehouse_tasks WHERE task_id = ?", (task_id,)), "task")
    if t["status"] != "open":
        raise HTTPException(status_code=409, detail=f"{task_id} is {t['status']}")
    with tx(con):
        con.execute("UPDATE warehouse_tasks SET status = 'done', completed_at = ?, assignee = COALESCE(assignee, 'floor team') WHERE task_id = ?",
                    (now_iso(), task_id))
        audit(con, module="warehouse", action="complete_task", entity_type="task", entity_id=task_id,
              summary=f"{t['task_type'].title()} task {task_id} for {t['ref_id']} completed at {t['warehouse_id']}")
    return one(con, "SELECT * FROM warehouse_tasks WHERE task_id = ?", (task_id,))


# ================================================================ logistics
SHIP_SELECT = f"""
  SELECT s.*, c.carrier_name,
         CASE WHEN s.delivered_at IS NOT NULL THEN CAST(julianday(s.delivered_at) - julianday(s.promised_date) AS INTEGER)
              WHEN s.status = 'in_transit' AND s.promised_date < '{TODAY}' THEN CAST(julianday('{TODAY}') - julianday(s.promised_date) AS INTEGER)
         END AS days_late
  FROM shipments s LEFT JOIN carriers c USING (carrier_id)"""


@router.get("/shipments")
def list_shipments(direction: str = "", status: str = "", carrier_id: str = "", q: str = "", late: bool = False,
                   sort: Optional[str] = None, limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    where, params = ["(shipment_id LIKE ? OR tracking_no LIKE ? OR sales_order_id LIKE ? OR purchase_order_id LIKE ? OR destination_name LIKE ?)"], [like(q)] * 5
    for col, v in (("direction", direction), ("status", status), ("carrier_id", carrier_id)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    if late:
        where.append("days_late > 0")
    return page(con, f"SELECT * FROM ({SHIP_SELECT})", where, params, sort=sort, default_sort="ship_date DESC, shipment_id DESC",
                allowed={"ship_date", "promised_date", "freight_cost", "units", "days_late", "distance_km"}, limit=limit, offset=offset)


def _timeline(s: dict, events: list[dict]) -> list[dict]:
    """Tracking milestones: stored scan events when the shipment was handled in the app, otherwise derived from its dates."""
    if events:
        return [{"at": e["at"], "status": e["status"], "location": e["location"], "note": e["note"]} for e in events]
    if not s["ship_date"]:
        return [{"at": s["promised_date"], "status": "booked", "location": s["origin_name"], "note": "awaiting pickup"}]
    ship = date.fromisoformat(s["ship_date"])
    end = date.fromisoformat(s["delivered_at"]) if s["delivered_at"] else min(date.fromisoformat(TODAY), ship + timedelta(days=30))
    out = [{"at": s["ship_date"], "status": "picked_up", "location": s["origin_name"], "note": f"{s['units']} units"}]
    span = (end - ship).days
    if span >= 2:
        out.append({"at": (ship + timedelta(days=max(1, span // 2))).isoformat(), "status": "in_transit",
                    "location": "carrier hub", "note": s.get("mode")})
    if s["delivered_at"]:
        if span >= 1:
            out.append({"at": (end - timedelta(days=0)).isoformat(), "status": "out_for_delivery", "location": s["destination_name"], "note": None})
        out.append({"at": s["delivered_at"], "status": "delivered", "location": s["destination_name"],
                    "note": "late" if s["delivered_at"] > (s["promised_date"] or s["delivered_at"]) else "on time"})
    return out


@router.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str, con=Depends(get_db)):
    s = must(one(con, f"SELECT * FROM ({SHIP_SELECT}) WHERE shipment_id = ?", (shipment_id,)), "shipment")
    ev = rows(con, "SELECT * FROM shipment_events WHERE shipment_id = ? ORDER BY event_id", (shipment_id,))
    return {"shipment": s, "timeline": _timeline(s, ev)}


class ScanEvent(BaseModel):
    status: str
    location: str
    note: Optional[str] = None


@router.post("/shipments/{shipment_id}/events")
def add_scan(shipment_id: str, body: ScanEvent, con=Depends(get_db)):
    s = must(one(con, "SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,)), "shipment")
    with tx(con):
        con.execute("INSERT INTO shipment_events (shipment_id, at, status, location, note) VALUES (?, ?, ?, ?, ?)",
                    (shipment_id, now_iso(), body.status, body.location, body.note))
        if body.status == "delivered":
            con.execute("UPDATE shipments SET status = 'delivered', delivered_at = ? WHERE shipment_id = ?", (TODAY, shipment_id))
            if s["sales_order_id"]:
                con.execute("UPDATE sales_orders SET status = 'delivered', delivered_at = ? WHERE sales_order_id = ? AND status = 'shipped'",
                            (TODAY, s["sales_order_id"]))
        elif body.status == "exception":
            con.execute("UPDATE shipments SET status = 'exception' WHERE shipment_id = ?", (shipment_id,))
        audit(con, module="logistics", action="scan", entity_type="shipment", entity_id=shipment_id,
              summary=f"{shipment_id}: {body.status} at {body.location}")
    alert_engine.refresh(con)
    return get_shipment(shipment_id, con)


@router.get("/logistics/overview")
def logistics_overview(con=Depends(get_db)):
    since = shift(-365)
    return {
        "kpis": one(con, f"""SELECT SUM(status = 'in_transit') AS in_transit,
                                    SUM(status = 'in_transit' AND promised_date < '{TODAY}') AS late_in_transit,
                                    SUM(status = 'exception') AS exceptions,
                                    (SELECT ROUND(1.0 * SUM(delivered_at <= promised_date) / COUNT(*), 3) FROM shipments
                                     WHERE direction = 'outbound' AND delivered_at > '{shift(-30)}') AS otd_30d,
                                    (SELECT ROUND(SUM(freight_cost), 0) FROM shipments WHERE ship_date > '{shift(-30)}') AS freight_30d
                             FROM shipments"""),
        "carriers": rows(con, """SELECT c.carrier_id, c.carrier_name, c.mode, COUNT(*) AS shipments,
                                        ROUND(1.0 * SUM(s.delivered_at <= s.promised_date) / NULLIF(SUM(s.delivered_at IS NOT NULL), 0), 3) AS on_time,
                                        ROUND(AVG(julianday(s.delivered_at) - julianday(s.ship_date)), 1) AS avg_transit_days,
                                        ROUND(SUM(s.freight_cost) / NULLIF(SUM(s.units), 0), 2) AS cost_per_unit
                                 FROM shipments s JOIN carriers c USING (carrier_id)
                                 WHERE s.direction = 'outbound' AND s.ship_date > ? GROUP BY 1 ORDER BY 1""", (shift(-90),)),
        "carrier_monthly": rows(con, """SELECT substr(ship_date, 1, 7) AS month, carrier_id,
                                               ROUND(1.0 * SUM(delivered_at <= promised_date) / NULLIF(SUM(delivered_at IS NOT NULL), 0), 3) AS on_time
                                        FROM shipments WHERE direction = 'outbound' AND ship_date > ? AND delivered_at IS NOT NULL
                                        GROUP BY 1, 2 ORDER BY 1""", (since,)),
        "lanes": rows(con, """SELECT s.origin_id, w.region AS origin_region, c.city AS destination, COUNT(*) AS shipments,
                                     ROUND(AVG(s.distance_km), 0) AS avg_km,
                                     ROUND(AVG(julianday(s.delivered_at) - julianday(s.ship_date)), 1) AS avg_transit_days,
                                     ROUND(1.0 * SUM(s.delivered_at <= s.promised_date) / NULLIF(SUM(s.delivered_at IS NOT NULL), 0), 3) AS on_time,
                                     ROUND(SUM(s.freight_cost), 0) AS freight
                              FROM shipments s JOIN warehouses w ON w.warehouse_id = s.origin_id
                              JOIN customers c ON c.customer_id = s.destination_id
                              WHERE s.direction = 'outbound' AND s.ship_date > ? GROUP BY 1, 3
                              ORDER BY shipments DESC LIMIT 40""", (shift(-90),)),
        "freight_monthly": rows(con, """SELECT substr(ship_date, 1, 7) AS month,
                                               ROUND(SUM(CASE WHEN direction = 'outbound' THEN freight_cost END), 0) AS outbound,
                                               ROUND(SUM(CASE WHEN direction = 'inbound' THEN freight_cost END), 0) AS inbound
                                        FROM shipments WHERE ship_date > ? AND ship_date <= ? GROUP BY 1 ORDER BY 1""", (since, TODAY)),
    }


# ================================================================ production
@router.get("/production/overview")
def production_overview(con=Depends(get_db)):
    snap = scalar(con, "SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= ?", (TODAY,))
    return {
        "plants": rows(con, """SELECT p.*,
                                      (SELECT COUNT(*) FROM production_runs r WHERE r.plant_id = p.plant_id AND r.planned_start BETWEEN :a AND :b) AS runs_30d,
                                      (SELECT SUM(planned_qty) FROM production_runs r WHERE r.plant_id = p.plant_id AND r.planned_start BETWEEN :a AND :b) AS planned_units_30d,
                                      (SELECT ROUND(AVG(delay_days > 0), 3) FROM production_runs r WHERE r.plant_id = p.plant_id AND r.actual_start > :c) AS late_share_90d,
                                      (SELECT SUM(on_hand_units < safety_stock_units) FROM rm_inventory_snapshots_weekly s
                                       WHERE s.plant_id = p.plant_id AND s.snapshot_date = :snap) AS materials_below_safety
                               FROM plants p ORDER BY p.plant_id""",
                       {"a": shift(-30), "b": TODAY, "c": shift(-90), "snap": snap}),
        "weekly_load": rows(con, """SELECT date(planned_start, 'weekday 1', '-7 days') AS week, plant_id, SUM(planned_qty) AS planned,
                                           SUM(produced_qty) AS produced
                                    FROM production_runs WHERE planned_start > ? AND planned_start <= ? GROUP BY 1, 2 ORDER BY 1""",
                             (shift(-182), shift(28))),
        "delay_reasons": rows(con, """SELECT COALESCE(delay_reason_code, 'on_time') AS reason, COUNT(*) AS runs, ROUND(AVG(delay_days), 1) AS avg_days
                                      FROM production_runs WHERE actual_start > ? GROUP BY 1 ORDER BY runs DESC""", (shift(-90),)),
        "shortages": rows(con, """SELECT s.plant_id, s.rm_id, m.rm_name, m.criticality, ROUND(s.on_hand_units, 1) AS on_hand,
                                         ROUND(s.safety_stock_units, 1) AS safety_stock, ROUND(s.days_of_cover, 1) AS days_of_cover
                                  FROM rm_inventory_snapshots_weekly s JOIN raw_materials m USING (rm_id)
                                  WHERE s.snapshot_date = ? AND s.on_hand_units < s.safety_stock_units
                                  ORDER BY s.days_of_cover LIMIT 30""", (snap,)),
        "snapshot_date": snap,
    }


@router.get("/production/runs")
def production_runs(plant_id: str = "", status: str = "", q: str = "", start: str = "", end: str = "",
                    limit: int = Query(100, le=500), offset: int = 0, con=Depends(get_db)):
    sel = f"""SELECT r.*, p.sku, CASE WHEN r.produced_qty IS NOT NULL AND r.produced_qty > 0 THEN 'completed'
                                    WHEN r.actual_start IS NOT NULL THEN 'in_progress'
                                    WHEN r.planned_start < '{TODAY}' THEN 'overdue' ELSE 'planned' END AS status
              FROM production_runs r JOIN products p USING (product_id)"""
    where, params = ["(r.production_run_id LIKE ? OR r.product_id LIKE ? OR p.sku LIKE ?)"], [like(q)] * 3
    if plant_id:
        where.append("r.plant_id = ?")
        params.append(plant_id)
    where.append("r.planned_start BETWEEN ? AND ?")
    params += [start or shift(-30), end or shift(30)]
    sub = f"SELECT * FROM ({sel} WHERE {' AND '.join(where)})"
    return page(con, sub, ["status = ?"] if status else [], [*params, status] if status else params, sort=None,
                default_sort="planned_start DESC, production_run_id", allowed=set(), limit=limit, offset=offset)


@router.get("/production/runs/{run_id}")
def production_run(run_id: str, con=Depends(get_db)):
    r = must(one(con, "SELECT r.*, p.sku, p.category FROM production_runs r JOIN products p USING (product_id) WHERE production_run_id = ?",
                 (run_id,)), "production run")
    snap = scalar(con, "SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= ?", (min(TODAY, r["planned_start"]),))
    req = rows(con, """SELECT b.rm_id, m.rm_name, m.uom, b.qty_per_unit, b.scrap_pct,
                              ROUND(b.qty_per_unit * ? * (1 + b.scrap_pct / 100.0), 2) AS required,
                              ROUND(s.on_hand_units, 1) AS on_hand, ROUND(s.safety_stock_units, 1) AS safety_stock
                       FROM bill_of_materials b JOIN raw_materials m USING (rm_id)
                       LEFT JOIN rm_inventory_snapshots_weekly s ON s.rm_id = b.rm_id AND s.plant_id = ? AND s.snapshot_date = ?
                       WHERE b.product_id = ? AND b.effective_from <= ? AND (b.effective_to IS NULL OR b.effective_to >= ?)""",
               (r["planned_qty"], r["plant_id"], snap, r["product_id"], r["planned_start"], r["planned_start"]))
    for x in req:
        x["status"] = "ok" if (x["on_hand"] or 0) >= x["required"] else "short"
    return {"run": r, "materials": req, "snapshot_date": snap,
            "history": rows(con, "SELECT at, actor, action, summary FROM audit_log WHERE entity_id = ? ORDER BY audit_id", (run_id,))}


class RunUpdate(BaseModel):
    action: str  # start | complete | reschedule
    produced_qty: Optional[int] = None
    planned_start: Optional[str] = None


@router.post("/production/runs/{run_id}")
def update_run(run_id: str, body: RunUpdate, con=Depends(get_db)):
    r = must(one(con, "SELECT * FROM production_runs WHERE production_run_id = ?", (run_id,)), "production run")
    with tx(con):
        if body.action == "start":
            if r["actual_start"]:
                raise HTTPException(status_code=409, detail="already started")
            delay = max(0, (date.fromisoformat(TODAY) - date.fromisoformat(r["planned_start"])).days)
            con.execute("UPDATE production_runs SET actual_start = ?, delay_days = ? WHERE production_run_id = ?", (TODAY, delay, run_id))
            msg = f"Started {run_id}" + (f" ({delay} days late)" if delay else "")
        elif body.action == "complete":
            if not r["actual_start"]:
                raise HTTPException(status_code=409, detail="start the run first")
            qty = body.produced_qty or r["planned_qty"]
            con.execute("UPDATE production_runs SET produced_qty = ? WHERE production_run_id = ?", (qty, run_id))
            msg = f"Completed {run_id}: {qty} units"
        elif body.action == "reschedule" and body.planned_start:
            if r["actual_start"]:
                raise HTTPException(status_code=409, detail="already started")
            con.execute("UPDATE production_runs SET planned_start = ? WHERE production_run_id = ?", (body.planned_start, run_id))
            msg = f"Rescheduled {run_id} {r['planned_start']} -> {body.planned_start}"
        else:
            raise HTTPException(status_code=400, detail="unknown action")
        audit(con, module="production", action=body.action, entity_type="production_run", entity_id=run_id, summary=msg, payload=body.model_dump())
    alert_engine.refresh(con)
    return production_run(run_id, con)


class RunCreate(BaseModel):
    product_id: str
    plant_id: str
    planned_start: str
    planned_qty: int


@router.post("/production/runs")
def create_run(body: RunCreate, con=Depends(get_db)):
    if body.planned_qty <= 0:
        raise HTTPException(status_code=400, detail="quantity must be positive")
    if body.planned_start < TODAY:
        raise HTTPException(status_code=400, detail="planned start is in the past")
    must(one(con, "SELECT 1 FROM plants WHERE plant_id = ?", (body.plant_id,)), "plant")
    if not one(con, """SELECT 1 FROM bill_of_materials WHERE product_id = ? AND effective_from <= ?
                       AND (effective_to IS NULL OR effective_to >= ?)""", (body.product_id, body.planned_start, body.planned_start)):
        raise HTTPException(status_code=400, detail=f"{body.product_id} has no bill of materials in effect - it cannot be manufactured")
    with tx(con):
        rid = next_id(con, "production_runs", "production_run_id", "PR", 7)
        con.execute("""INSERT INTO production_runs (production_run_id, plant_id, product_id, purchase_order_line_id, planned_start,
                       actual_start, planned_qty, produced_qty, delay_days, delay_reason_code, rm_shortage_rm_id)
                       VALUES (?, ?, ?, NULL, ?, NULL, ?, NULL, 0, NULL, NULL)""",
                    (rid, body.plant_id, body.product_id, body.planned_start, body.planned_qty))
        created(con, "production_runs", rid)
        audit(con, module="production", action="schedule", entity_type="production_run", entity_id=rid,
              summary=f"Scheduled {rid}: {body.planned_qty} x {body.product_id} at {body.plant_id} on {body.planned_start}",
              payload=body.model_dump())
    alert_engine.refresh(con)
    return production_run(rid, con)
