"""Customers, sales orders (confirm -> allocate -> pick -> pack -> ship -> deliver) and returns (RMA)."""
from __future__ import annotations

import random
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import (TODAY, audit, available, change_allocation, created, get_db, like, must, next_id, now_iso, one,
                    page, post_movement, rows, scalar, shift, tx)
from app.services import alerts as alert_engine

router = APIRouter(prefix="/api", tags=["sales", "returns"])

FLOW = ["confirmed", "allocated", "picking", "packed", "shipped", "delivered"]


# ================================================================ customers
CUSTOMER_SELECT = f"""
  SELECT c.*, COALESCE(o.orders_12m, 0) AS orders_12m, COALESCE(o.revenue_12m, 0) AS revenue_12m,
         o.last_order, COALESCE(r.returns_12m, 0) AS returns_12m,
         COALESCE(op.open_orders, 0) AS open_orders
  FROM customers c
  LEFT JOIN (SELECT customer_id, COUNT(*) orders_12m, ROUND(SUM(order_value), 0) revenue_12m, MAX(order_date) last_order
             FROM sales_orders WHERE order_date > '{shift(-365)}' AND status != 'cancelled' GROUP BY 1) o USING (customer_id)
  LEFT JOIN (SELECT customer_id, COUNT(*) returns_12m FROM returns WHERE requested_at > '{shift(-365)}' GROUP BY 1) r USING (customer_id)
  LEFT JOIN (SELECT customer_id, COUNT(*) open_orders FROM sales_orders
             WHERE status IN ('confirmed', 'allocated', 'picking', 'packed', 'shipped') GROUP BY 1) op USING (customer_id)"""


@router.get("/customers")
def list_customers(q: str = "", segment: str = "", region: str = "", sort: Optional[str] = None,
                   limit: int = Query(50, le=300), offset: int = 0, con=Depends(get_db)):
    where, params = ["(customer_id LIKE ? OR customer_name LIKE ? OR city LIKE ?)"], [like(q)] * 3
    for col, v in (("segment", segment), ("region", region)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    return page(con, f"SELECT * FROM ({CUSTOMER_SELECT})", where, params, sort=sort, default_sort="revenue_12m DESC",
                allowed={"customer_id", "customer_name", "revenue_12m", "orders_12m", "last_order", "returns_12m"},
                limit=limit, offset=offset)


@router.get("/customers/{customer_id}")
def customer(customer_id: str, con=Depends(get_db)):
    c = must(one(con, f"SELECT * FROM ({CUSTOMER_SELECT}) WHERE customer_id = ?", (customer_id,)), "customer")
    return {
        "customer": c,
        "monthly": rows(con, """SELECT substr(order_date, 1, 7) AS month, COUNT(*) AS orders, ROUND(SUM(order_value), 0) AS revenue
                                FROM sales_orders WHERE customer_id = ? AND order_date > ? AND status != 'cancelled'
                                GROUP BY 1 ORDER BY 1""", (customer_id, shift(-365))),
        "orders": rows(con, """SELECT sales_order_id, order_date, requested_date, shipped_at, delivered_at, status, order_value
                               FROM sales_orders WHERE customer_id = ? ORDER BY order_date DESC LIMIT 25""", (customer_id,)),
        "top_products": rows(con, """SELECT l.product_id, p.sku, SUM(l.qty_shipped) AS units, ROUND(SUM(l.qty_shipped * l.unit_price), 0) AS revenue
                                     FROM sales_order_lines l JOIN sales_orders o USING (sales_order_id) JOIN products p USING (product_id)
                                     WHERE o.customer_id = ? GROUP BY 1 ORDER BY revenue DESC LIMIT 10""", (customer_id,)),
        "returns": rows(con, "SELECT rma_id, product_id, qty, reason, requested_at, status FROM returns WHERE customer_id = ? ORDER BY requested_at DESC LIMIT 10",
                        (customer_id,)),
        "service": one(con, """SELECT COUNT(*) AS delivered, SUM(delivered_at <= promised_date) AS on_time,
                                      ROUND(AVG(julianday(delivered_at) - julianday(order_date)), 1) AS avg_cycle_days
                               FROM sales_orders WHERE customer_id = ? AND delivered_at IS NOT NULL AND order_date > ?""",
                       (customer_id, shift(-365))),
    }


# ================================================================ sales orders
SO_SELECT = """
  SELECT o.sales_order_id, o.customer_id, c.customer_name, c.segment, o.warehouse_id, o.channel, o.order_date,
         o.requested_date, o.promised_date, o.shipped_at, o.delivered_at, o.status, o.order_value, o.origin,
         (SELECT COUNT(*) FROM sales_order_lines l WHERE l.sales_order_id = o.sales_order_id) AS lines,
         (SELECT SUM(qty_backordered) FROM sales_order_lines l WHERE l.sales_order_id = o.sales_order_id) AS backordered,
         CASE WHEN o.delivered_at IS NOT NULL THEN (o.delivered_at <= o.promised_date) END AS on_time
  FROM sales_orders o JOIN customers c USING (customer_id)"""


@router.get("/sales-orders")
def list_orders(q: str = "", status: str = "", warehouse_id: str = "", customer_id: str = "", channel: str = "",
                since: str = "", sort: Optional[str] = None, limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    where, params = ["(sales_order_id LIKE ? OR customer_id LIKE ? OR customer_name LIKE ?)"], [like(q)] * 3
    if status == "open":
        where.append("status IN ('confirmed', 'allocated', 'picking', 'packed')")
    elif status == "backordered":
        where.append("backordered > 0")
    elif status:
        where.append("status = ?")
        params.append(status)
    for col, v in (("warehouse_id", warehouse_id), ("customer_id", customer_id), ("channel", channel)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    if since:
        where.append("order_date >= ?")
        params.append(since)
    return page(con, f"SELECT * FROM ({SO_SELECT})", where, params, sort=sort,
                default_sort="order_date DESC, sales_order_id DESC",
                allowed={"sales_order_id", "order_date", "order_value", "status", "customer_name", "promised_date"},
                limit=limit, offset=offset)


@router.get("/sales-orders/summary")
def so_summary(con=Depends(get_db)):
    return {
        "status": rows(con, "SELECT status, COUNT(*) AS n FROM sales_orders WHERE order_date > ? GROUP BY 1", (shift(-30),)),
        "open": scalar(con, "SELECT COUNT(*) FROM sales_orders WHERE status IN ('confirmed', 'allocated', 'picking', 'packed')"),
        "in_transit": scalar(con, "SELECT COUNT(*) FROM sales_orders WHERE status = 'shipped'"),
        "kpis": one(con, """SELECT COUNT(*) AS orders_30d, ROUND(SUM(order_value), 0) AS revenue_30d,
                                   ROUND(AVG(order_value), 0) AS avg_order_value,
                                   ROUND(1.0 * SUM(delivered_at <= promised_date) / NULLIF(SUM(delivered_at IS NOT NULL), 0), 3) AS otd
                            FROM sales_orders WHERE order_date > ? AND status != 'cancelled'""", (shift(-30),)),
        "daily": rows(con, """SELECT order_date AS day, COUNT(*) AS orders, ROUND(SUM(order_value), 0) AS revenue
                              FROM sales_orders WHERE order_date > ? AND status != 'cancelled' GROUP BY 1 ORDER BY 1""", (shift(-90),)),
    }


@router.get("/sales-orders/{so_id}")
def get_order(so_id: str, con=Depends(get_db)):
    o = must(one(con, f"SELECT * FROM ({SO_SELECT}) WHERE sales_order_id = ?", (so_id,)), "sales order")
    lines = rows(con, """SELECT l.*, p.sku, p.category, COALESCE(s.on_hand, 0) - COALESCE(s.allocated, 0) AS available_now
                         FROM sales_order_lines l JOIN products p USING (product_id)
                         LEFT JOIN stock_levels s ON s.product_id = l.product_id AND s.warehouse_id = ?
                         WHERE l.sales_order_id = ? ORDER BY l.sales_order_line_id""", (o["warehouse_id"], so_id))
    ship = one(con, "SELECT s.*, c.carrier_name FROM shipments s LEFT JOIN carriers c USING (carrier_id) WHERE sales_order_id = ?", (so_id,))
    return {"order": o, "lines": lines, "shipment": ship,
            "returns": rows(con, "SELECT * FROM returns WHERE sales_order_id = ?", (so_id,)),
            "tasks": rows(con, "SELECT * FROM warehouse_tasks WHERE ref_id = ? ORDER BY created_at", (so_id,)),
            "history": rows(con, "SELECT at, actor, action, summary FROM audit_log WHERE entity_id = ? ORDER BY audit_id", (so_id,)),
            "next": FLOW[FLOW.index(o["status"]) + 1] if o["status"] in FLOW[:-1] else None}


class SOLine(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    unit_price: Optional[float] = Field(None, gt=0)


class SOCreate(BaseModel):
    customer_id: str
    warehouse_id: Optional[str] = None
    requested_date: Optional[str] = None
    lines: list[SOLine] = Field(..., min_length=1)
    notes: Optional[str] = None


@router.post("/sales-orders")
def create_order(body: SOCreate, con=Depends(get_db)):
    c = must(one(con, "SELECT * FROM customers WHERE customer_id = ?", (body.customer_id,)), "customer")
    wid = body.warehouse_id or c["home_warehouse_id"]
    with tx(con):
        so_id = next_id(con, "sales_orders", "sales_order_id", "SO", 6)
        total = 0.0
        disc = 1 - (c["discount_pct"] or 0) / 100
        line_rows = []
        for ln in body.lines:
            p = must(one(con, "SELECT unit_price FROM products_ext WHERE product_id = ?", (ln.product_id,)), f"product {ln.product_id}")
            price = ln.unit_price or round(p["unit_price"] * disc, 2)
            lid = next_id(con, "sales_order_lines", "sales_order_line_id", "SOL", 7)
            con.execute("INSERT INTO sales_order_lines VALUES (?, ?, ?, ?, 0, 0, 0, NULL, ?, NULL)", (lid, so_id, ln.product_id, ln.quantity, price))
            created(con, "sales_order_lines", lid)
            total += price * ln.quantity
            line_rows.append(lid)
        req = body.requested_date or shift(5)
        con.execute("INSERT INTO sales_orders VALUES (?, ?, ?, 'Sales rep', ?, ?, ?, NULL, NULL, 'confirmed', ?, 'app', ?, ?)",
                    (so_id, body.customer_id, wid, TODAY, req, req, round(total, 2), now_iso(), body.notes))
        created(con, "sales_orders", so_id)
        audit(con, module="sales", action="create", entity_type="sales_order", entity_id=so_id,
              summary=f"Order {so_id} from {c['customer_name']}: {len(body.lines)} lines, ${total:,.0f}, ship from {wid}",
              payload=body.model_dump())
    alert_engine.refresh(con)
    return get_order(so_id, con)


def _lines(con, so_id):
    return rows(con, "SELECT * FROM sales_order_lines WHERE sales_order_id = ? AND qty_ordered > 0", (so_id,))


@router.post("/sales-orders/{so_id}/advance")
def advance(so_id: str, carrier_id: Optional[str] = None, con=Depends(get_db)):
    """Move an order to its next fulfilment step, with the inventory and warehouse effects of that step."""
    o = must(one(con, "SELECT * FROM sales_orders WHERE sales_order_id = ?", (so_id,)), "sales order")
    if o["status"] not in FLOW[:-1]:
        raise HTTPException(status_code=409, detail=f"{so_id} is {o['status']}")
    nxt = FLOW[FLOW.index(o["status"]) + 1]
    wid = o["warehouse_id"]
    with tx(con):
        if nxt == "allocated":
            short = []
            for ln in _lines(con, so_id):
                avail = available(con, ln["product_id"], wid)
                alloc = max(0, min(ln["qty_ordered"], avail))
                back = ln["qty_ordered"] - alloc
                con.execute("UPDATE sales_order_lines SET qty_allocated = ?, qty_backordered = ?, backorder_status = ? WHERE sales_order_line_id = ?",
                            (alloc, back, "open" if back else None, ln["sales_order_line_id"]))
                change_allocation(con, ln["product_id"], wid, alloc)
                if back:
                    short.append(f"{ln['product_id']} short {back}")
            if not scalar(con, "SELECT SUM(qty_allocated) FROM sales_order_lines WHERE sales_order_id = ?", (so_id,)):
                raise HTTPException(status_code=409, detail="nothing available to allocate at " + wid)
            summary = f"Allocated {so_id} at {wid}" + (f"; backordered: {', '.join(short)}" if short else "")
        elif nxt == "picking":
            tid = next_id(con, "warehouse_tasks", "task_id", "TSK", 6)
            units = scalar(con, "SELECT SUM(qty_allocated) FROM sales_order_lines WHERE sales_order_id = ?", (so_id,))
            con.execute("INSERT INTO warehouse_tasks VALUES (?, ?, 'pick', 'sales_order', ?, 'open', ?, NULL, NULL, ?, ?)",
                        (tid, wid, so_id, now_iso(), units, f"A-{random.randint(1, 40):02d}-{random.randint(1, 6)}"))
            summary = f"Pick task {tid} released for {so_id} ({units} units)"
        elif nxt == "packed":
            con.execute("UPDATE warehouse_tasks SET status = 'done', completed_at = ? WHERE ref_id = ? AND task_type = 'pick'", (now_iso(), so_id))
            tid = next_id(con, "warehouse_tasks", "task_id", "TSK", 6)
            con.execute("INSERT INTO warehouse_tasks VALUES (?, ?, 'pack', 'sales_order', ?, 'done', ?, ?, NULL, NULL, NULL)",
                        (tid, wid, so_id, now_iso(), now_iso()))
            summary = f"{so_id} picked and packed"
        elif nxt == "shipped":
            carrier = must(one(con, "SELECT * FROM carriers WHERE carrier_id = ?", (carrier_id or "CR01",)), "carrier")
            units = 0
            for ln in _lines(con, so_id):
                q = ln["qty_allocated"] - ln["qty_shipped"]
                if q <= 0:
                    continue
                mid = post_movement(con, product_id=ln["product_id"], warehouse_id=wid, qty=-q, movement_type="sale")
                change_allocation(con, ln["product_id"], wid, -q)
                con.execute("UPDATE sales_order_lines SET qty_shipped = qty_shipped + ?, movement_id = ? WHERE sales_order_line_id = ?",
                            (q, mid, ln["sales_order_line_id"]))
                units += q
            cust = one(con, "SELECT customer_name, city FROM customers WHERE customer_id = ?", (o["customer_id"],))
            wh = one(con, "SELECT warehouse_name FROM warehouses WHERE warehouse_id = ?", (wid,))
            sid = next_id(con, "shipments", "shipment_id", "SH", 7)
            track = f"{carrier['carrier_id']}-{random.randint(10**9, 10**10 - 1)}"
            con.execute("""INSERT INTO shipments VALUES (?, 'outbound', ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, 'in_transit', ?, ?)""",
                        (sid, so_id, carrier["carrier_id"], carrier["mode"], wid, wh["warehouse_name"], o["customer_id"], cust["city"],
                         units, TODAY, o["promised_date"], round(units * carrier["cost_per_km_unit"] * 3 + 45, 2), track))
            created(con, "shipments", sid)
            for st, loc in (("label_created", wh["warehouse_name"]), ("picked_up", wh["warehouse_name"])):
                con.execute("INSERT INTO shipment_events (shipment_id, at, status, location, note) VALUES (?, ?, ?, ?, NULL)",
                            (sid, now_iso(), st, loc))
            con.execute("UPDATE sales_orders SET shipped_at = ? WHERE sales_order_id = ?", (TODAY, so_id))
            summary = f"Shipped {units} units on {so_id} with {carrier['carrier_name']} ({track})"
        else:  # delivered
            con.execute("UPDATE sales_orders SET delivered_at = ? WHERE sales_order_id = ?", (TODAY, so_id))
            sh = one(con, "SELECT shipment_id, destination_name FROM shipments WHERE sales_order_id = ?", (so_id,))
            if sh:
                con.execute("UPDATE shipments SET status = 'delivered', delivered_at = ? WHERE shipment_id = ?", (TODAY, sh["shipment_id"]))
                con.execute("INSERT INTO shipment_events (shipment_id, at, status, location, note) VALUES (?, ?, 'delivered', ?, 'proof of delivery captured')",
                            (sh["shipment_id"], now_iso(), sh["destination_name"]))
            summary = f"{so_id} delivered"
        con.execute("UPDATE sales_orders SET status = ? WHERE sales_order_id = ?", (nxt, so_id))
        audit(con, module="sales", action=nxt, entity_type="sales_order", entity_id=so_id, summary=summary)
    alert_engine.refresh(con)
    return get_order(so_id, con)


@router.post("/sales-orders/{so_id}/cancel")
def cancel_order(so_id: str, con=Depends(get_db)):
    o = must(one(con, "SELECT * FROM sales_orders WHERE sales_order_id = ?", (so_id,)), "sales order")
    if o["status"] not in ("confirmed", "allocated", "picking", "packed"):
        raise HTTPException(status_code=409, detail=f"{so_id} is {o['status']} and cannot be cancelled")
    with tx(con):
        for ln in _lines(con, so_id):
            if ln["qty_allocated"] > ln["qty_shipped"]:
                change_allocation(con, ln["product_id"], o["warehouse_id"], -(ln["qty_allocated"] - ln["qty_shipped"]))
        con.execute("UPDATE sales_order_lines SET qty_allocated = qty_shipped, backorder_status = CASE WHEN qty_backordered > 0 THEN 'cancelled' END WHERE sales_order_id = ?", (so_id,))
        con.execute("UPDATE warehouse_tasks SET status = 'cancelled' WHERE ref_id = ? AND status = 'open'", (so_id,))
        con.execute("UPDATE sales_orders SET status = 'cancelled' WHERE sales_order_id = ?", (so_id,))
        audit(con, module="sales", action="cancel", entity_type="sales_order", entity_id=so_id, summary=f"Cancelled {so_id}; allocations released")
    alert_engine.refresh(con)
    return get_order(so_id, con)


@router.post("/sales-orders/{so_id}/check-availability")
def check_availability(so_id: str, con=Depends(get_db)):
    o = must(one(con, "SELECT warehouse_id FROM sales_orders WHERE sales_order_id = ?", (so_id,)), "sales order")
    out = []
    for ln in _lines(con, so_id):
        alts = rows(con, """SELECT warehouse_id, on_hand - allocated AS available FROM stock_levels
                            WHERE product_id = ? AND warehouse_id != ? AND on_hand - allocated >= ? ORDER BY available DESC LIMIT 3""",
                    (ln["product_id"], o["warehouse_id"], ln["qty_ordered"]))
        out.append({"line_id": ln["sales_order_line_id"], "product_id": ln["product_id"], "ordered": ln["qty_ordered"],
                    "available_here": available(con, ln["product_id"], o["warehouse_id"]), "alternatives": alts})
    return out


# ================================================================ returns
RMA_SELECT = """SELECT r.*, p.sku, p.category, x.brand_family, c.customer_name FROM returns r JOIN products p USING (product_id)
                JOIN products_ext x USING (product_id) JOIN customers c USING (customer_id)"""


@router.get("/returns")
def list_returns(q: str = "", status: str = "", reason: str = "", sort: Optional[str] = None,
                 limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    where, params = ["(rma_id LIKE ? OR product_id LIKE ? OR customer_name LIKE ? OR sales_order_id LIKE ?)"], [like(q)] * 4
    if status == "open":
        where.append("status IN ('authorized', 'received', 'inspected')")
    elif status:
        where.append("status = ?")
        params.append(status)
    if reason:
        where.append("reason = ?")
        params.append(reason)
    return page(con, f"SELECT * FROM ({RMA_SELECT})", where, params, sort=sort, default_sort="requested_at DESC, rma_id DESC",
                allowed={"rma_id", "requested_at", "refund_value", "qty"}, limit=limit, offset=offset)


@router.get("/returns/summary")
def returns_summary(con=Depends(get_db)):
    return {
        "kpis": one(con, """SELECT COUNT(*) AS rmas_90d, SUM(qty) AS units_90d, ROUND(SUM(refund_value), 0) AS refunds_90d,
                                   (SELECT COUNT(*) FROM returns WHERE status IN ('authorized', 'received', 'inspected')) AS open
                            FROM returns WHERE requested_at > ?""", (shift(-90),)),
        "by_reason": rows(con, "SELECT reason, COUNT(*) AS n, SUM(qty) AS units FROM returns WHERE requested_at > ? GROUP BY 1 ORDER BY n DESC", (shift(-365),)),
        "monthly": rows(con, """SELECT substr(requested_at, 1, 7) AS month, COUNT(*) AS rmas, ROUND(SUM(refund_value), 0) AS refunds
                                FROM returns WHERE requested_at > ? GROUP BY 1 ORDER BY 1""", (shift(-365),)),
        "by_disposition": rows(con, "SELECT disposition, COUNT(*) AS n FROM returns WHERE requested_at > ? GROUP BY 1", (shift(-365),)),
    }


@router.get("/returns/{rma_id}")
def get_return(rma_id: str, con=Depends(get_db)):
    r = must(one(con, f"SELECT * FROM ({RMA_SELECT}) WHERE rma_id = ?", (rma_id,)), "return")
    return {"rma": r, "line": one(con, "SELECT * FROM sales_order_lines WHERE sales_order_line_id = ?", (r["sales_order_line_id"],)),
            "history": rows(con, "SELECT at, actor, action, summary FROM audit_log WHERE entity_id = ? ORDER BY audit_id", (rma_id,))}


class RMACreate(BaseModel):
    sales_order_line_id: str
    qty: int = Field(..., gt=0)
    reason: str
    notes: Optional[str] = None


@router.post("/returns")
def create_return(body: RMACreate, con=Depends(get_db)):
    ln = must(one(con, """SELECT l.*, o.customer_id, o.warehouse_id, o.status FROM sales_order_lines l
                          JOIN sales_orders o USING (sales_order_id) WHERE l.sales_order_line_id = ?""", (body.sales_order_line_id,)), "order line")
    if ln["status"] not in ("delivered", "shipped"):
        raise HTTPException(status_code=409, detail="only shipped or delivered orders can be returned")
    already = scalar(con, "SELECT COALESCE(SUM(qty), 0) FROM returns WHERE sales_order_line_id = ?", (body.sales_order_line_id,))
    if body.qty > ln["qty_shipped"] - already:
        raise HTTPException(status_code=400, detail=f"only {ln['qty_shipped'] - already} units can still be returned")
    with tx(con):
        rid = next_id(con, "returns", "rma_id", "RMA", 6)
        con.execute("""INSERT INTO returns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'authorized', ?, NULL, ?, 'app')""",
                    (rid, ln["sales_order_id"], body.sales_order_line_id, ln["customer_id"], ln["product_id"], ln["warehouse_id"],
                     body.qty, body.reason, TODAY, round(body.qty * ln["unit_price"], 2), body.notes))
        created(con, "returns", rid)
        audit(con, module="returns", action="authorize", entity_type="return", entity_id=rid,
              summary=f"RMA {rid}: {body.qty} x {ln['product_id']} from {ln['sales_order_id']} ({body.reason})", payload=body.model_dump())
    alert_engine.refresh(con)
    return get_return(rid, con)


@router.post("/returns/{rma_id}/receive")
def receive_return(rma_id: str, con=Depends(get_db)):
    r = must(one(con, "SELECT * FROM returns WHERE rma_id = ?", (rma_id,)), "return")
    if r["status"] != "authorized":
        raise HTTPException(status_code=409, detail=f"{rma_id} is {r['status']}")
    with tx(con):
        con.execute("UPDATE returns SET status = 'received', received_at = ? WHERE rma_id = ?", (TODAY, rma_id))
        tid = next_id(con, "warehouse_tasks", "task_id", "TSK", 6)
        con.execute("INSERT INTO warehouse_tasks VALUES (?, ?, 'inspect', 'return', ?, 'open', ?, NULL, NULL, ?, 'RTN-01')",
                    (tid, r["warehouse_id"], rma_id, now_iso(), r["qty"]))
        audit(con, module="returns", action="receive", entity_type="return", entity_id=rma_id, summary=f"Received {r['qty']} units for {rma_id}; inspection task {tid}")
    return get_return(rma_id, con)


class Disposition(BaseModel):
    disposition: str = Field(..., pattern="^(restock|refurbish|scrap|replace)$")


@router.post("/returns/{rma_id}/disposition")
def dispose_return(rma_id: str, body: Disposition, con=Depends(get_db)):
    r = must(one(con, "SELECT * FROM returns WHERE rma_id = ?", (rma_id,)), "return")
    if r["status"] != "received":
        raise HTTPException(status_code=409, detail="receive and inspect the return first")
    with tx(con):
        note = ""
        if body.disposition == "restock":
            mid = post_movement(con, product_id=r["product_id"], warehouse_id=r["warehouse_id"], qty=r["qty"], movement_type="return")
            note = f"; restocked ({mid})"
        repl = None
        if body.disposition == "replace":
            repl = next_id(con, "sales_orders", "sales_order_id", "SO", 6)
            price = scalar(con, "SELECT unit_price FROM sales_order_lines WHERE sales_order_line_id = ?", (r["sales_order_line_id"],))
            lid = next_id(con, "sales_order_lines", "sales_order_line_id", "SOL", 7)
            con.execute("INSERT INTO sales_order_lines VALUES (?, ?, ?, ?, 0, 0, 0, NULL, 0, NULL)", (lid, repl, r["product_id"], r["qty"]))
            con.execute("INSERT INTO sales_orders VALUES (?, ?, ?, 'Replacement', ?, ?, ?, NULL, NULL, 'confirmed', 0, 'app', ?, ?)",
                        (repl, r["customer_id"], r["warehouse_id"], TODAY, shift(3), shift(3), now_iso(), f"Replacement for {rma_id} (was ${price})"))
            created(con, "sales_orders", repl)
            created(con, "sales_order_lines", lid)
            note = f"; replacement order {repl}"
        con.execute("UPDATE returns SET disposition = ?, status = 'closed', closed_at = ?, replacement_order_id = ? WHERE rma_id = ?",
                    (body.disposition, TODAY, repl, rma_id))
        con.execute("UPDATE warehouse_tasks SET status = 'done', completed_at = ? WHERE ref_id = ? AND status = 'open'", (now_iso(), rma_id))
        audit(con, module="returns", action=body.disposition, entity_type="return", entity_id=rma_id,
              summary=f"{rma_id} closed as {body.disposition}{note}")
    alert_engine.refresh(con)
    return get_return(rma_id, con)
