"""Products / SKUs and inventory (finished goods by warehouse, raw materials by plant)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import (TODAY, audit, get_db, like, must, next_id, one, page, post_movement, rows, scalar, shift, tx)
from app.services import alerts as alert_engine

router = APIRouter(prefix="/api", tags=["products", "inventory"])

STOCK_STATUS = """CASE WHEN s.on_hand < 0 THEN 'negative'
                       WHEN s.on_hand = 0 THEN 'out'
                       WHEN s.on_hand < p.min_stock THEN 'low'
                       WHEN s.on_hand > p.max_stock THEN 'excess'
                       ELSE 'ok' END"""


# ================================================================ products
@router.get("/products")
def list_products(q: str = "", category: str = "", abc: str = "", sourcing: str = "", sort: Optional[str] = None,
                  limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    sel = f"""
      SELECT p.product_id, p.sku, p.category, x.brand_family, x.abc_class, x.sourcing_mode, p.supplier_id,
             s.supplier_name, p.unit_cost, x.unit_price, x.gross_margin_pct, p.reorder_point,
             COALESCE(st.on_hand, 0) AS on_hand, COALESCE(st.allocated, 0) AS allocated,
             ROUND(COALESCE(st.on_hand, 0) * p.unit_cost, 2) AS stock_value,
             COALESCE(d.units_13w, 0) AS units_13w
      FROM products p JOIN products_ext x USING (product_id) JOIN suppliers s ON s.supplier_id = p.supplier_id
      LEFT JOIN (SELECT product_id, SUM(on_hand) on_hand, SUM(allocated) allocated FROM stock_levels GROUP BY 1) st USING (product_id)
      LEFT JOIN (SELECT product_id, SUM(actual_demand_units) units_13w FROM product_demand_weekly
                 WHERE week_start > '{shift(-91)}' GROUP BY 1) d USING (product_id)"""
    where, params = ["(p.product_id LIKE ? OR p.sku LIKE ? OR x.brand_family LIKE ?)"], [like(q)] * 3
    for col, v in (("p.category", category), ("x.abc_class", abc), ("x.sourcing_mode", sourcing)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    return page(con, sel, where, params, sort=sort, default_sort="product_id",
                allowed={"product_id", "sku", "category", "on_hand", "stock_value", "units_13w", "unit_price", "brand_family"},
                limit=limit, offset=offset)


@router.get("/products/{product_id}")
def product(product_id: str, con=Depends(get_db)):
    p = must(one(con, """SELECT p.*, x.*, s.supplier_name, s.lead_time_days, s.country_code
                         FROM products p JOIN products_ext x USING (product_id)
                         JOIN suppliers s ON s.supplier_id = p.supplier_id WHERE p.product_id = ?""", (product_id,)),
             "product")
    stock = rows(con, f"""SELECT s.warehouse_id, w.warehouse_name, s.on_hand, s.allocated, s.on_hand - s.allocated AS available,
                                 p.min_stock, p.max_stock, p.avg_weekly_demand, {STOCK_STATUS} AS status,
                                 COALESCE(o.on_order, 0) AS on_order
                          FROM stock_levels s JOIN warehouses w USING (warehouse_id)
                          JOIN stock_policies p USING (product_id, warehouse_id)
                          LEFT JOIN stock_on_order o USING (product_id, warehouse_id)
                          WHERE s.product_id = ? ORDER BY s.warehouse_id""", (product_id,))
    weekly = rows(con, """SELECT week_start, SUM(forecast_units) AS forecast, SUM(actual_demand_units) AS actual,
                                 SUM(fulfilled_units) AS fulfilled, SUM(unfulfilled_units) AS unfulfilled
                          FROM product_demand_weekly WHERE product_id = ? AND week_start > ? GROUP BY 1 ORDER BY 1""",
                  (product_id, shift(-365)))
    bom = rows(con, """SELECT b.rm_id, m.rm_name, b.qty_per_unit, b.uom, b.scrap_pct, b.bom_version, m.criticality
                       FROM bill_of_materials b JOIN raw_materials m USING (rm_id)
                       WHERE b.product_id = ? AND b.effective_from <= ? AND (b.effective_to IS NULL OR b.effective_to >= ?)""",
               (product_id, TODAY, TODAY))
    pos = rows(con, """SELECT o.purchase_order_id, o.supplier_id, o.warehouse_id, o.ordered_at, o.expected_at, o.received_at,
                              o.status, l.quantity_ordered, l.quantity_received, l.unit_cost
                       FROM purchase_order_lines l JOIN purchase_orders o USING (purchase_order_id)
                       WHERE l.product_id = ? ORDER BY o.ordered_at DESC LIMIT 15""", (product_id,))
    orders = rows(con, """SELECT o.sales_order_id, o.customer_id, c.customer_name, o.order_date, o.status, l.qty_ordered,
                                 l.qty_shipped, l.unit_price
                          FROM sales_order_lines l JOIN sales_orders o USING (sales_order_id)
                          JOIN customers c USING (customer_id)
                          WHERE l.product_id = ? ORDER BY o.order_date DESC LIMIT 15""", (product_id,))
    runs = rows(con, """SELECT production_run_id, plant_id, planned_start, actual_start, planned_qty, produced_qty, delay_days
                        FROM production_runs WHERE product_id = ? ORDER BY planned_start DESC LIMIT 10""", (product_id,))
    ret = one(con, """SELECT COUNT(*) AS rmas, COALESCE(SUM(qty), 0) AS units FROM returns WHERE product_id = ?""", (product_id,))
    sold = scalar(con, "SELECT COALESCE(SUM(qty_shipped), 0) FROM sales_order_lines WHERE product_id = ?", (product_id,))
    return {"product": p, "stock": stock, "weekly": weekly, "bom": bom, "purchase_orders": pos, "sales_orders": orders,
            "production_runs": runs, "returns": {**ret, "units_sold": sold,
                                                 "rate": round(ret["units"] / sold, 4) if sold else None}}


class ProductUpdate(BaseModel):
    unit_price: Optional[float] = Field(None, gt=0)
    reorder_point: Optional[int] = Field(None, ge=0)


@router.patch("/products/{product_id}")
def update_product(product_id: str, body: ProductUpdate, con=Depends(get_db)):
    must(one(con, "SELECT product_id FROM products WHERE product_id = ?", (product_id,)), "product")
    with tx(con):
        if body.unit_price is not None:
            con.execute("UPDATE products_ext SET unit_price = ? WHERE product_id = ?", (body.unit_price, product_id))
        if body.reorder_point is not None:
            con.execute("UPDATE products SET reorder_point = ? WHERE product_id = ?", (body.reorder_point, product_id))
            con.execute("UPDATE stock_policies SET min_stock = ?, max_stock = MAX(max_stock, ? * 3) WHERE product_id = ?",
                        (body.reorder_point, body.reorder_point, product_id))
        audit(con, module="products", action="update", entity_type="product", entity_id=product_id,
              summary=f"Updated {product_id}: " + ", ".join(f"{k}={v}" for k, v in body.model_dump(exclude_none=True).items()),
              payload=body.model_dump(exclude_none=True))
    alert_engine.refresh(con)
    return product(product_id, con)


@router.get("/categories")
def categories(con=Depends(get_db)):
    return rows(con, """SELECT p.category, COUNT(*) AS products, ROUND(SUM(st.on_hand * p.unit_cost), 0) AS stock_value,
                               SUM(st.on_hand) AS units
                        FROM products p LEFT JOIN (SELECT product_id, SUM(on_hand) on_hand FROM stock_levels GROUP BY 1) st
                        USING (product_id) GROUP BY 1 ORDER BY 1""")


# ================================================================ inventory
@router.get("/inventory/summary")
def inventory_summary(con=Depends(get_db)):
    k = one(con, f"""SELECT SUM(s.on_hand) AS units, ROUND(SUM(s.on_hand * pr.unit_cost), 0) AS value,
                            SUM(s.allocated) AS allocated,
                            SUM({STOCK_STATUS} = 'low') AS low, SUM({STOCK_STATUS} IN ('out', 'negative')) AS out_of_stock,
                            SUM({STOCK_STATUS} = 'excess') AS excess, COUNT(*) AS positions
                     FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id)
                     JOIN products pr USING (product_id)""")
    by_wh = rows(con, f"""SELECT w.warehouse_id, w.warehouse_name, w.region, w.capacity_units, SUM(s.on_hand) AS units,
                                 ROUND(SUM(s.on_hand * pr.unit_cost), 0) AS value,
                                 ROUND(1.0 * SUM(s.on_hand) / w.capacity_units, 3) AS utilization,
                                 SUM({STOCK_STATUS} = 'low') AS low
                          FROM stock_levels s JOIN warehouses w USING (warehouse_id)
                          JOIN stock_policies p USING (product_id, warehouse_id) JOIN products pr USING (product_id)
                          GROUP BY w.warehouse_id ORDER BY w.warehouse_id""")
    by_cat = rows(con, """SELECT pr.category, SUM(s.on_hand) AS units, ROUND(SUM(s.on_hand * pr.unit_cost), 0) AS value
                          FROM stock_levels s JOIN products pr USING (product_id) GROUP BY 1 ORDER BY value DESC""")
    flow = rows(con, """SELECT substr(movement_at, 1, 7) AS month,
                               SUM(CASE WHEN movement_type = 'receipt' THEN quantity_change ELSE 0 END) AS received,
                               -SUM(CASE WHEN movement_type = 'sale' THEN quantity_change ELSE 0 END) AS shipped,
                               SUM(CASE WHEN movement_type IN ('adjustment', 'return') THEN quantity_change ELSE 0 END) AS adjusted
                        FROM inventory_movements WHERE movement_at > ? GROUP BY 1 ORDER BY 1""", (shift(-365),))
    rm = one(con, """SELECT COUNT(*) AS positions, SUM(on_hand_units < safety_stock_units) AS below_safety,
                            SUM(stockout_flag) AS stocked_out
                     FROM rm_inventory_snapshots_weekly
                     WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= ?)""",
             (TODAY,))
    return {"kpis": k, "by_warehouse": by_wh, "by_category": by_cat, "flow": flow, "raw_materials": rm}


@router.get("/inventory/stock")
def stock(q: str = "", warehouse_id: str = "", category: str = "", status: str = "", sort: Optional[str] = None,
          limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    sel = f"""SELECT s.product_id, pr.sku, pr.category, s.warehouse_id, s.on_hand, s.allocated,
                     s.on_hand - s.allocated AS available, p.min_stock, p.max_stock, COALESCE(o.on_order, 0) AS on_order,
                     ROUND(s.on_hand * pr.unit_cost, 2) AS value,
                     CASE WHEN p.avg_weekly_demand > 0 THEN ROUND(s.on_hand / p.avg_weekly_demand, 1) END AS weeks_of_cover,
                     {STOCK_STATUS} AS status
              FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id)
              JOIN products pr USING (product_id) LEFT JOIN stock_on_order o USING (product_id, warehouse_id)"""
    where, params = ["(s.product_id LIKE ? OR pr.sku LIKE ?)"], [like(q)] * 2
    if warehouse_id:
        where.append("s.warehouse_id = ?")
        params.append(warehouse_id)
    if category:
        where.append("pr.category = ?")
        params.append(category)
    if status:
        where.append(f"{STOCK_STATUS} = ?" if status != "attention" else f"{STOCK_STATUS} IN ('low', 'out', 'negative')")
        if status != "attention":
            params.append(status)
    return page(con, sel, where, params, sort=sort, default_sort="product_id, warehouse_id",
                allowed={"product_id", "warehouse_id", "on_hand", "available", "value", "weeks_of_cover", "min_stock", "on_order"},
                limit=limit, offset=offset)


@router.get("/inventory/movements")
def movements(q: str = "", warehouse_id: str = "", movement_type: str = "", since: str = "",
              limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    sel = """SELECT m.movement_id, m.movement_at, m.movement_type, m.product_id, pr.sku, m.warehouse_id, m.quantity_change,
                    m.purchase_order_line_id, m.transfer_id, (a.record_id IS NOT NULL) AS app_created
             FROM inventory_movements m JOIN products pr USING (product_id)
             LEFT JOIN app_records a ON a.table_name = 'inventory_movements' AND a.record_id = m.movement_id"""
    where, params = ["(m.product_id LIKE ? OR m.movement_id LIKE ? OR pr.sku LIKE ?)"], [like(q)] * 3
    for col, v in (("m.warehouse_id", warehouse_id), ("m.movement_type", movement_type)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    if since:
        where.append("m.movement_at >= ?")
        params.append(since)
    return page(con, sel, where, params, sort=None, default_sort="movement_at DESC, movement_id DESC",
                allowed=set(), limit=limit, offset=offset)


class Adjustment(BaseModel):
    product_id: str
    warehouse_id: str
    quantity_change: int
    reason: str = Field(..., min_length=3)


@router.post("/inventory/adjust")
def adjust(body: Adjustment, con=Depends(get_db)):
    must(one(con, "SELECT 1 FROM stock_policies WHERE product_id = ? AND warehouse_id = ?",
             (body.product_id, body.warehouse_id)), "stock position")
    with tx(con):
        mid = post_movement(con, product_id=body.product_id, warehouse_id=body.warehouse_id,
                            qty=body.quantity_change, movement_type="adjustment")
        audit(con, module="inventory", action="adjust", entity_type="movement", entity_id=mid,
              summary=f"Adjusted {body.product_id} at {body.warehouse_id} by {body.quantity_change:+d}: {body.reason}",
              payload=body.model_dump())
    alert_engine.refresh(con)
    return {"movement_id": mid, "stock": one(con, "SELECT * FROM stock_levels WHERE product_id = ? AND warehouse_id = ?",
                                              (body.product_id, body.warehouse_id))}


class Transfer(BaseModel):
    product_id: str
    from_warehouse_id: str
    to_warehouse_id: str
    quantity: int = Field(..., gt=0)


@router.post("/inventory/transfer")
def transfer(body: Transfer, con=Depends(get_db)):
    if body.from_warehouse_id == body.to_warehouse_id:
        raise HTTPException(status_code=400, detail="source and destination must differ")
    with tx(con):
        n = scalar(con, "SELECT MAX(CAST(SUBSTR(transfer_id, 2) AS INTEGER)) FROM inventory_movements WHERE transfer_id LIKE 'T%'") or 0
        tid = f"T{n + 1:07d}"
        out_id = post_movement(con, product_id=body.product_id, warehouse_id=body.from_warehouse_id, qty=-body.quantity,
                               movement_type="transfer", transfer_id=tid)
        in_id = post_movement(con, product_id=body.product_id, warehouse_id=body.to_warehouse_id, qty=body.quantity,
                              movement_type="transfer", transfer_id=tid)
        audit(con, module="inventory", action="transfer", entity_type="transfer", entity_id=tid,
              summary=f"Moved {body.quantity} x {body.product_id} {body.from_warehouse_id} -> {body.to_warehouse_id}",
              payload=body.model_dump())
    alert_engine.refresh(con)
    return {"transfer_id": tid, "movements": [out_id, in_id]}


@router.get("/inventory/materials")
def raw_materials(q: str = "", plant_id: str = "", status: str = "", con=Depends(get_db)):
    snap = scalar(con, "SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= ?", (TODAY,))
    where, params = ["s.snapshot_date = ?", "(m.rm_id LIKE ? OR m.rm_name LIKE ?)"], [snap, like(q), like(q)]
    if plant_id:
        where.append("s.plant_id = ?")
        params.append(plant_id)
    if status == "short":
        where.append("s.on_hand_units < s.safety_stock_units")
    data = rows(con, f"""SELECT m.rm_id, m.rm_name, m.rm_category, m.uom, m.criticality, m.is_single_source, s.plant_id,
                                ROUND(s.on_hand_units, 1) AS on_hand, ROUND(s.safety_stock_units, 1) AS safety_stock,
                                ROUND(s.days_of_cover, 1) AS days_of_cover, s.stockout_flag,
                                CASE WHEN s.on_hand_units <= 0 THEN 'out' WHEN s.on_hand_units < s.safety_stock_units THEN 'low' ELSE 'ok' END AS status
                         FROM rm_inventory_snapshots_weekly s JOIN raw_materials m USING (rm_id)
                         WHERE {' AND '.join(where)} ORDER BY (s.on_hand_units < s.safety_stock_units) DESC, s.days_of_cover""",
                params)
    return {"snapshot_date": snap, "rows": data}


@router.get("/warehouses-list")
def warehouses_list(con=Depends(get_db)):
    return rows(con, "SELECT warehouse_id, warehouse_name, region FROM warehouses ORDER BY warehouse_id")


@router.get("/lookup")
def lookup(con=Depends(get_db)):
    """Small reference lists for forms."""
    return {
        "warehouses": rows(con, "SELECT warehouse_id, warehouse_name, region FROM warehouses ORDER BY warehouse_id"),
        "plants": rows(con, "SELECT plant_id, plant_name, region FROM plants ORDER BY plant_id"),
        "carriers": rows(con, "SELECT carrier_id, carrier_name, mode FROM carriers ORDER BY carrier_id"),
        "categories": [r["category"] for r in rows(con, "SELECT DISTINCT category FROM products ORDER BY 1")],
        "today": TODAY,
    }


@router.get("/search")
def search(q: str, con=Depends(get_db)):
    q = q.strip()
    if len(q) < 2:
        return []
    L = like(q)
    out = []
    specs = [
        ("product", "SELECT product_id id, sku || ' · ' || category label FROM products WHERE product_id LIKE ? OR sku LIKE ? LIMIT 5", 2),
        ("supplier", "SELECT supplier_id id, supplier_name label FROM suppliers WHERE supplier_id LIKE ? OR supplier_name LIKE ? LIMIT 5", 2),
        ("customer", "SELECT customer_id id, customer_name label FROM customers WHERE customer_id LIKE ? OR customer_name LIKE ? LIMIT 5", 2),
        ("sales_order", "SELECT sales_order_id id, customer_id || ' · ' || status label FROM sales_orders WHERE sales_order_id LIKE ? LIMIT 5", 1),
        ("purchase_order", "SELECT purchase_order_id id, supplier_id || ' · ' || status label FROM purchase_orders WHERE purchase_order_id LIKE ? LIMIT 5", 1),
        ("rm_purchase_order", "SELECT rm_purchase_order_id id, supplier_id || ' · ' || status label FROM rm_purchase_orders WHERE rm_purchase_order_id LIKE ? LIMIT 5", 1),
        ("shipment", "SELECT shipment_id id, direction || ' · ' || status label FROM shipments WHERE shipment_id LIKE ? OR tracking_no LIKE ? LIMIT 5", 2),
        ("return", "SELECT rma_id id, product_id || ' · ' || status label FROM returns WHERE rma_id LIKE ? LIMIT 5", 1),
        ("material", "SELECT rm_id id, rm_name label FROM raw_materials WHERE rm_id LIKE ? OR rm_name LIKE ? LIMIT 5", 2),
        ("warehouse", "SELECT warehouse_id id, warehouse_name label FROM warehouses WHERE warehouse_id LIKE ? OR warehouse_name LIKE ? LIMIT 3", 2),
        ("production_run", "SELECT production_run_id id, product_id || ' · ' || plant_id label FROM production_runs WHERE production_run_id LIKE ? LIMIT 3", 1),
    ]
    for kind, sql, n in specs:
        out += [{"type": kind, **r} for r in rows(con, sql, [L] * n)]
    return out[:25]
