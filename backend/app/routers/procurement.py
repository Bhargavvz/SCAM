"""Supplier management and purchasing (finished-goods POs to warehouses, raw-material POs to plants)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import (TODAY, audit, created, get_db, last_complete_month, like, months_back, must, next_id, one, page,
                    post_movement, rows, scalar, shift, tx)
from app.services import alerts as alert_engine

router = APIRouter(prefix="/api", tags=["suppliers", "purchasing"])


# ================================================================ suppliers
SUPPLIER_SELECT = f"""
  SELECT s.supplier_id, s.supplier_name, s.country_code, s.lead_time_days, s.reliability_score,
         sc.otif_6m, sc.delay_6m, sc.quality_ppm_6m,
         COALESCE(sp.spend_12m, 0) AS spend_12m, COALESCE(op.open_pos, 0) AS open_pos,
         COALESCE(ct.active_contracts, 0) AS active_contracts,
         CASE WHEN sc.otif_6m IS NULL THEN 'unrated' WHEN sc.otif_6m >= 0.5 THEN 'preferred'
              WHEN sc.otif_6m >= 0.3 THEN 'approved' ELSE 'at_risk' END AS tier
  FROM suppliers s
  LEFT JOIN (SELECT supplier_id, ROUND(AVG(otif_rate), 3) otif_6m, ROUND(AVG(avg_delay_days), 1) delay_6m,
                    ROUND(AVG(quality_ppm), 0) quality_ppm_6m
             FROM supplier_scorecard_monthly WHERE month > '{months_back(6)}' AND month <= '{last_complete_month()}' GROUP BY 1) sc USING (supplier_id)
  LEFT JOIN (SELECT supplier_id, ROUND(SUM(spend), 0) spend_12m FROM (
               SELECT o.supplier_id, l.quantity_ordered * l.unit_cost spend FROM purchase_orders o
               JOIN purchase_order_lines l USING (purchase_order_id) WHERE o.ordered_at > '{shift(-365)}' AND o.status != 'cancelled'
               UNION ALL
               SELECT o.supplier_id, l.quantity_ordered * l.unit_cost FROM rm_purchase_orders o
               JOIN rm_purchase_order_lines l USING (rm_purchase_order_id) WHERE o.ordered_at > '{shift(-365)}' AND o.status != 'cancelled')
             GROUP BY 1) sp USING (supplier_id)
  LEFT JOIN (SELECT supplier_id, COUNT(*) open_pos FROM (
               SELECT supplier_id FROM purchase_orders WHERE status IN ('open', 'partial', 'draft')
               UNION ALL SELECT supplier_id FROM rm_purchase_orders WHERE status IN ('open', 'partial')) GROUP BY 1) op USING (supplier_id)
  LEFT JOIN (SELECT supplier_id, COUNT(*) active_contracts FROM contracts
             WHERE valid_from <= '{TODAY}' AND valid_to >= '{TODAY}' GROUP BY 1) ct USING (supplier_id)"""


@router.get("/suppliers")
def list_suppliers(q: str = "", country: str = "", tier: str = "", sort: Optional[str] = None,
                   limit: int = Query(50, le=300), offset: int = 0, con=Depends(get_db)):
    where, params = ["(supplier_id LIKE ? OR supplier_name LIKE ?)"], [like(q)] * 2
    if country:
        where.append("country_code = ?")
        params.append(country)
    if tier:
        where.append("tier = ?")
        params.append(tier)
    return page(con, f"SELECT * FROM ({SUPPLIER_SELECT})", where, params, sort=sort, default_sort="spend_12m DESC",
                allowed={"supplier_id", "supplier_name", "otif_6m", "spend_12m", "open_pos", "lead_time_days", "delay_6m", "quality_ppm_6m"},
                limit=limit, offset=offset)


@router.get("/suppliers/{supplier_id}")
def supplier(supplier_id: str, con=Depends(get_db)):
    s = must(one(con, f"SELECT * FROM ({SUPPLIER_SELECT}) WHERE supplier_id = ?", (supplier_id,)), "supplier")
    return {
        "supplier": s,
        "scorecard": rows(con, """SELECT month, po_count, otif_rate, avg_delay_days, fill_rate, quality_ppm,
                                         commitments_made, commitments_kept FROM supplier_scorecard_monthly
                                  WHERE supplier_id = ? AND month <= ? ORDER BY month DESC LIMIT 24""", (supplier_id, last_complete_month()))[::-1],
        "lead_time": rows(con, """SELECT substr(ordered_at, 1, 7) AS month, COUNT(*) AS orders,
                                         ROUND(AVG(julianday(received_at) - julianday(ordered_at)), 1) AS actual_lead_days,
                                         ROUND(AVG(MAX(0, julianday(received_at) - julianday(expected_at))), 1) AS avg_late_days
                                  FROM (SELECT ordered_at, received_at, expected_at FROM purchase_orders WHERE supplier_id = :s
                                        UNION ALL SELECT ordered_at, received_at, promised_at FROM rm_purchase_orders WHERE supplier_id = :s)
                                  WHERE received_at IS NOT NULL AND ordered_at > :since GROUP BY 1 ORDER BY 1""",
                          {"s": supplier_id, "since": shift(-730)}),
        "contracts": rows(con, """SELECT *, CASE WHEN valid_to < ? THEN 'expired' WHEN valid_from > ? THEN 'future' ELSE 'active' END AS state
                                  FROM contracts WHERE supplier_id = ? ORDER BY valid_from DESC""", (TODAY, TODAY, supplier_id)),
        "negotiations": rows(con, """SELECT negotiation_id, rm_id, started_at, concluded_at, topic, our_ask, their_offer,
                                            final_terms, outcome FROM negotiations WHERE supplier_id = ? ORDER BY started_at DESC""",
                             (supplier_id,)),
        "products": rows(con, """SELECT p.product_id, p.sku, p.category, p.unit_cost FROM products p WHERE p.supplier_id = ?
                                 ORDER BY p.product_id LIMIT 50""", (supplier_id,)),
        "materials": rows(con, """SELECT c.rm_id, m.rm_name, c.sourcing_rank, c.allocation_pct, c.lead_time_days, c.moq,
                                         c.unit_price, c.price_valid_to, c.qualified_flag
                                  FROM rm_supplier_catalog c JOIN raw_materials m USING (rm_id)
                                  WHERE c.supplier_id = ? AND c.price_valid_to >= ? ORDER BY c.rm_id""", (supplier_id, TODAY)),
        "purchase_orders": rows(con, """SELECT * FROM (
              SELECT purchase_order_id AS po_id, 'goods' AS type, warehouse_id AS ship_to, ordered_at, expected_at, received_at, status
              FROM purchase_orders WHERE supplier_id = :s
              UNION ALL SELECT rm_purchase_order_id, 'material', plant_id, ordered_at, expected_at, received_at, status
              FROM rm_purchase_orders WHERE supplier_id = :s) ORDER BY ordered_at DESC LIMIT 25""", {"s": supplier_id}),
        "commitments": rows(con, """SELECT commitment_id, made_at, due_date, commitment_text, status FROM commitments
                                    WHERE counterparty_id = ? ORDER BY made_at DESC LIMIT 15""", (supplier_id,)),
        "disruptions": rows(con, """SELECT event_id, event_type, title, detected_at, severity, end_date FROM disruption_events
                                    WHERE origin_entity_id = ? ORDER BY detected_at DESC LIMIT 15""", (supplier_id,)),
    }


# ================================================================ purchase orders
GOODS_PO = """
  SELECT o.purchase_order_id AS po_id, 'goods' AS type, o.supplier_id, s.supplier_name, o.warehouse_id AS ship_to,
         o.ordered_at, o.expected_at, o.received_at, o.status,
         CAST(julianday(COALESCE(o.received_at, :today)) - julianday(o.expected_at) AS INTEGER) AS days_late,
         (SELECT ROUND(SUM(quantity_ordered * unit_cost), 2) FROM purchase_order_lines l WHERE l.purchase_order_id = o.purchase_order_id) AS value,
         (SELECT COUNT(*) FROM purchase_order_lines l WHERE l.purchase_order_id = o.purchase_order_id) AS lines
  FROM purchase_orders o JOIN suppliers s USING (supplier_id)"""
MATERIAL_PO = """
  SELECT o.rm_purchase_order_id AS po_id, 'material' AS type, o.supplier_id, s.supplier_name, o.plant_id AS ship_to,
         o.ordered_at, o.expected_at, o.received_at, o.status,
         CAST(julianday(COALESCE(o.received_at, :today)) - julianday(o.promised_at) AS INTEGER) AS days_late,
         (SELECT ROUND(SUM(quantity_ordered * unit_cost), 2) FROM rm_purchase_order_lines l WHERE l.rm_purchase_order_id = o.rm_purchase_order_id) AS value,
         (SELECT COUNT(*) FROM rm_purchase_order_lines l WHERE l.rm_purchase_order_id = o.rm_purchase_order_id) AS lines
  FROM rm_purchase_orders o JOIN suppliers s USING (supplier_id)"""


@router.get("/purchase-orders")
def list_pos(type: str = "goods", status: str = "", q: str = "", supplier_id: str = "", ship_to: str = "",
             late: bool = False, sort: Optional[str] = None, limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    base = GOODS_PO if type == "goods" else MATERIAL_PO
    where, params = ["(po_id LIKE ? OR supplier_id LIKE ? OR supplier_name LIKE ?)"], [like(q)] * 3
    if status == "active":
        where.append("status IN ('draft', 'open', 'partial')")
    elif status:
        where.append("status = ?")
        params.append(status)
    for col, v in (("supplier_id", supplier_id), ("ship_to", ship_to)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    if late:
        where.append("status IN ('open', 'partial') AND expected_at < ?")
        params.append(TODAY)
    sel = f"SELECT * FROM ({base.replace(':today', repr(TODAY))})"
    return page(con, sel, where, params, sort=sort, default_sort="ordered_at DESC, po_id DESC",
                allowed={"po_id", "ordered_at", "expected_at", "value", "days_late", "supplier_name"}, limit=limit, offset=offset)


@router.get("/purchase-orders/summary")
def po_summary(con=Depends(get_db)):
    return {
        "goods": one(con, f"""SELECT SUM(status IN ('open', 'partial')) AS open, SUM(status = 'draft') AS draft,
                                     SUM(status IN ('open', 'partial') AND expected_at < '{TODAY}') AS late,
                                     SUM(ordered_at > '{shift(-30)}') AS last_30d FROM purchase_orders"""),
        "material": one(con, f"""SELECT SUM(status IN ('open', 'partial')) AS open,
                                        SUM(status IN ('open', 'partial') AND expected_at > promised_at) AS slipped,
                                        SUM(ordered_at > '{shift(-30)}') AS last_30d FROM rm_purchase_orders"""),
        "spend_monthly": rows(con, """SELECT month, ROUND(SUM(goods), 0) AS goods, ROUND(SUM(material), 0) AS material FROM (
               SELECT substr(o.ordered_at, 1, 7) month, l.quantity_ordered * l.unit_cost goods, 0 material
               FROM purchase_orders o JOIN purchase_order_lines l USING (purchase_order_id) WHERE o.ordered_at > :s AND o.status != 'cancelled'
               UNION ALL SELECT substr(o.ordered_at, 1, 7), 0, l.quantity_ordered * l.unit_cost
               FROM rm_purchase_orders o JOIN rm_purchase_order_lines l USING (rm_purchase_order_id) WHERE o.ordered_at > :s AND o.status != 'cancelled')
               GROUP BY 1 ORDER BY 1""", {"s": shift(-365)}),
    }


@router.get("/purchase-orders/{po_id}")
def get_po(po_id: str, con=Depends(get_db)):
    if po_id.startswith("RPO"):
        head = must(one(con, f"SELECT * FROM ({MATERIAL_PO.replace(':today', repr(TODAY))}) WHERE po_id = ?", (po_id,)), "purchase order")
        extra = one(con, "SELECT order_type, promised_at FROM rm_purchase_orders WHERE rm_purchase_order_id = ?", (po_id,))
        lines = rows(con, """SELECT l.rm_purchase_order_line_id AS line_id, l.rm_id AS item_id, m.rm_name AS item_name, m.uom,
                                    l.quantity_ordered, l.quantity_received, l.unit_cost
                             FROM rm_purchase_order_lines l JOIN raw_materials m USING (rm_id) WHERE l.rm_purchase_order_id = ?""", (po_id,))
        revisions = rows(con, "SELECT * FROM rm_po_revisions WHERE rm_po_id = ? ORDER BY revised_at", (po_id,))
        receipts = rows(con, """SELECT m.rm_movement_id AS movement_id, m.movement_at, m.quantity_change, l.rm_id AS item_id
                                FROM rm_inventory_movements m JOIN rm_purchase_order_lines l
                                ON l.rm_purchase_order_line_id = m.rm_purchase_order_line_id
                                WHERE l.rm_purchase_order_id = ? ORDER BY m.movement_at""", (po_id,))
        return {"po": {**head, **(extra or {})}, "lines": lines, "revisions": revisions, "receipts": receipts,
                "shipment": None,
                "disruptions": rows(con, """SELECT event_id, title, detected_at, severity FROM disruption_events
                                            WHERE anchor_ids LIKE ? ORDER BY detected_at""", (f'%"{po_id}"%',))}
    head = must(one(con, f"SELECT * FROM ({GOODS_PO.replace(':today', repr(TODAY))}) WHERE po_id = ?", (po_id,)), "purchase order")
    lines = rows(con, """SELECT l.purchase_order_line_id AS line_id, l.product_id AS item_id, p.sku AS item_name, 'units' AS uom,
                                l.quantity_ordered, COALESCE(l.quantity_received, 0) AS quantity_received, l.unit_cost
                         FROM purchase_order_lines l JOIN products p USING (product_id) WHERE l.purchase_order_id = ?""", (po_id,))
    receipts = rows(con, """SELECT m.movement_id, m.movement_at, m.quantity_change, m.product_id AS item_id
                            FROM inventory_movements m JOIN purchase_order_lines l ON l.purchase_order_line_id = m.purchase_order_line_id
                            WHERE l.purchase_order_id = ? ORDER BY m.movement_at""", (po_id,))
    return {"po": head, "lines": lines, "revisions": [], "receipts": receipts,
            "shipment": one(con, "SELECT * FROM shipments WHERE purchase_order_id = ?", (po_id,)), "disruptions": []}


class POLine(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    unit_cost: Optional[float] = Field(None, gt=0)


class POCreate(BaseModel):
    supplier_id: str
    warehouse_id: str
    expected_at: Optional[str] = None
    lines: list[POLine] = Field(..., min_length=1)
    submit: bool = True


@router.post("/purchase-orders")
def create_po(body: POCreate, con=Depends(get_db)):
    sup = must(one(con, "SELECT * FROM suppliers WHERE supplier_id = ?", (body.supplier_id,)), "supplier")
    must(one(con, "SELECT 1 FROM warehouses WHERE warehouse_id = ?", (body.warehouse_id,)), "warehouse")
    expected = body.expected_at or shift(int(sup["lead_time_days"]))
    if expected < TODAY:
        raise HTTPException(status_code=400, detail="expected date is in the past")
    with tx(con):
        po_id = next_id(con, "purchase_orders", "purchase_order_id", "PO", 6)
        con.execute("INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (po_id, body.supplier_id, body.warehouse_id, TODAY, expected, "open" if body.submit else "draft"))
        created(con, "purchase_orders", po_id)
        total = 0.0
        for ln in body.lines:
            p = must(one(con, "SELECT unit_cost FROM products WHERE product_id = ?", (ln.product_id,)), f"product {ln.product_id}")
            cost = ln.unit_cost or p["unit_cost"]
            lid = next_id(con, "purchase_order_lines", "purchase_order_line_id", "POL", 7)
            con.execute("INSERT INTO purchase_order_lines VALUES (?, ?, ?, ?, 0, ?)", (lid, po_id, ln.product_id, ln.quantity, cost))
            created(con, "purchase_order_lines", lid)
            total += cost * ln.quantity
        units = sum(ln.quantity for ln in body.lines)
        sid = next_id(con, "shipments", "shipment_id", "SH", 7)
        wh = one(con, "SELECT warehouse_name FROM warehouses WHERE warehouse_id = ?", (body.warehouse_id,))
        con.execute("""INSERT INTO shipments VALUES (?, 'inbound', NULL, ?, 'CR01', 'LTL', ?, ?, ?, ?, NULL, ?, NULL, ?, NULL,
                       'booked', NULL, NULL)""", (sid, po_id, body.supplier_id, sup["supplier_name"], body.warehouse_id,
                                                 wh["warehouse_name"], units, expected))
        created(con, "shipments", sid)
        audit(con, module="purchasing", action="create", entity_type="purchase_order", entity_id=po_id,
              summary=f"{'Issued' if body.submit else 'Drafted'} {po_id} to {sup['supplier_name']} for {body.warehouse_id}: "
                      f"{len(body.lines)} lines, ${total:,.0f}, expected {expected}", payload=body.model_dump())
    alert_engine.refresh(con)
    return get_po(po_id, con)


class ReceiveLine(BaseModel):
    line_id: str
    quantity: int = Field(..., gt=0)


class Receive(BaseModel):
    lines: list[ReceiveLine] = Field(..., min_length=1)


@router.post("/purchase-orders/{po_id}/receive")
def receive_po(po_id: str, body: Receive, con=Depends(get_db)):
    po = must(one(con, "SELECT * FROM purchase_orders WHERE purchase_order_id = ?", (po_id,)), "purchase order")
    if po["status"] not in ("open", "partial"):
        raise HTTPException(status_code=409, detail=f"{po_id} is {po['status']}; only open orders can be received")
    with tx(con):
        received = []
        for r in body.lines:
            ln = must(one(con, "SELECT * FROM purchase_order_lines WHERE purchase_order_line_id = ? AND purchase_order_id = ?",
                          (r.line_id, po_id)), f"line {r.line_id}")
            outstanding = ln["quantity_ordered"] - (ln["quantity_received"] or 0)
            if r.quantity > outstanding:
                raise HTTPException(status_code=400, detail=f"{r.line_id}: only {outstanding} outstanding")
            mid = post_movement(con, product_id=ln["product_id"], warehouse_id=po["warehouse_id"], qty=r.quantity,
                                movement_type="receipt", po_line_id=r.line_id)
            con.execute("UPDATE purchase_order_lines SET quantity_received = COALESCE(quantity_received, 0) + ? WHERE purchase_order_line_id = ?",
                        (r.quantity, r.line_id))
            received.append({"line_id": r.line_id, "product_id": ln["product_id"], "quantity": r.quantity, "movement_id": mid})
        left = scalar(con, "SELECT SUM(quantity_ordered - COALESCE(quantity_received, 0)) FROM purchase_order_lines WHERE purchase_order_id = ?",
                      (po_id,))
        status = "received" if not left else "partial"
        con.execute("UPDATE purchase_orders SET status = ?, received_at = ? WHERE purchase_order_id = ?", (status, TODAY, po_id))
        con.execute("UPDATE shipments SET status = 'delivered', delivered_at = ? WHERE purchase_order_id = ? AND direction = 'inbound'",
                    (TODAY, po_id))
        tid = next_id(con, "warehouse_tasks", "task_id", "TSK", 6)
        con.execute("INSERT INTO warehouse_tasks VALUES (?, ?, 'putaway', 'purchase_order', ?, 'open', ?, NULL, NULL, ?, NULL)",
                    (tid, po["warehouse_id"], po_id, TODAY, sum(x["quantity"] for x in received)))
        audit(con, module="purchasing", action="receive", entity_type="purchase_order", entity_id=po_id,
              summary=f"Received {sum(x['quantity'] for x in received)} units on {po_id} at {po['warehouse_id']} ({status})",
              payload=received)
    alert_engine.refresh(con)
    return get_po(po_id, con)


@router.post("/purchase-orders/{po_id}/submit")
def submit_po(po_id: str, con=Depends(get_db)):
    po = must(one(con, "SELECT status FROM purchase_orders WHERE purchase_order_id = ?", (po_id,)), "purchase order")
    if po["status"] != "draft":
        raise HTTPException(status_code=409, detail="only drafts can be issued")
    with tx(con):
        con.execute("UPDATE purchase_orders SET status = 'open', ordered_at = ? WHERE purchase_order_id = ?", (TODAY, po_id))
        audit(con, module="purchasing", action="submit", entity_type="purchase_order", entity_id=po_id, summary=f"Issued {po_id} to supplier")
    return get_po(po_id, con)


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_po(po_id: str, con=Depends(get_db)):
    po = must(one(con, "SELECT * FROM purchase_orders WHERE purchase_order_id = ?", (po_id,)), "purchase order")
    if po["status"] not in ("draft", "open"):
        raise HTTPException(status_code=409, detail=f"{po_id} is {po['status']} and cannot be cancelled")
    blocking = rows(con, """SELECT commitment_id, commitment_text, due_date FROM commitments
                            WHERE counterparty_id = ? AND status = 'open' AND commitment_text LIKE 'We order at least%'""",
                    (po["supplier_id"],))
    if blocking:
        raise HTTPException(status_code=409, detail=f"cancelling would breach volume commitment {blocking[0]['commitment_id']}: "
                                                    f"{blocking[0]['commitment_text']}")
    with tx(con):
        con.execute("UPDATE purchase_orders SET status = 'cancelled' WHERE purchase_order_id = ?", (po_id,))
        con.execute("UPDATE shipments SET status = 'cancelled' WHERE purchase_order_id = ?", (po_id,))
        audit(con, module="purchasing", action="cancel", entity_type="purchase_order", entity_id=po_id, summary=f"Cancelled {po_id}")
    alert_engine.refresh(con)
    return get_po(po_id, con)


@router.get("/purchasing/suggestions")
def reorder_suggestions(warehouse_id: str = "", limit: int = Query(100, le=500), con=Depends(get_db)):
    """Positions where on hand + on order is below min stock: order up to max stock from the product's supplier."""
    where = "WHERE s.on_hand - s.allocated + COALESCE(o.on_order, 0) < p.min_stock"
    params: list = []
    if warehouse_id:
        where += " AND s.warehouse_id = ?"
        params.append(warehouse_id)
    return rows(con, f"""SELECT s.product_id, pr.sku, s.warehouse_id, pr.supplier_id, su.supplier_name, su.lead_time_days,
                                s.on_hand, s.allocated, COALESCE(o.on_order, 0) AS on_order, p.min_stock, p.max_stock,
                                p.max_stock - (s.on_hand - s.allocated + COALESCE(o.on_order, 0)) AS suggested_qty,
                                pr.unit_cost,
                                ROUND((p.max_stock - (s.on_hand - s.allocated + COALESCE(o.on_order, 0))) * pr.unit_cost, 2) AS est_value
                         FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id)
                         JOIN products pr USING (product_id) JOIN suppliers su ON su.supplier_id = pr.supplier_id
                         LEFT JOIN stock_on_order o USING (product_id, warehouse_id)
                         {where} ORDER BY (s.on_hand - p.min_stock) LIMIT ?""", (*params, limit))
