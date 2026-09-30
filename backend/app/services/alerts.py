"""Alert engine. Each detector is a query over current state; refresh() opens new alerts, updates existing ones and
resolves alerts whose condition no longer holds. It runs at startup and after every write, so alerts are live."""
from __future__ import annotations

import time

from app.db import TODAY, last_complete_month, months_back, now_iso, rows, shift, tx

CAP = 300  # per detector, most urgent first


def _detectors() -> list[tuple[str, str, str, str]]:
    """(kind, module, severity expression or literal, SQL returning entity_type, entity_id, title, detail, metric)."""
    return [
        ("stockout", "inventory", "critical", f"""
            SELECT 'stock' et, s.product_id || '@' || s.warehouse_id eid,
                   'Out of stock: ' || s.product_id || ' at ' || s.warehouse_id title,
                   'On hand ' || s.on_hand || ', allocated ' || s.allocated || ', min ' || p.min_stock detail, s.on_hand metric
            FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id)
            WHERE s.on_hand <= 0 ORDER BY s.on_hand LIMIT {CAP}"""),
        ("low_stock", "inventory", "warning", f"""
            SELECT 'stock', s.product_id || '@' || s.warehouse_id,
                   'Below minimum: ' || s.product_id || ' at ' || s.warehouse_id,
                   'Available ' || (s.on_hand - s.allocated) || ' vs min ' || p.min_stock || ' (on order ' || COALESCE(o.on_order, 0) || ')',
                   s.on_hand - s.allocated
            FROM stock_levels s JOIN stock_policies p USING (product_id, warehouse_id)
            LEFT JOIN stock_on_order o USING (product_id, warehouse_id)
            WHERE s.on_hand > 0 AND s.on_hand - s.allocated < p.min_stock ORDER BY 1.0 * (s.on_hand - s.allocated) / p.min_stock LIMIT {CAP}"""),
        ("po_overdue", "purchasing", "CASE WHEN metric > 14 THEN 'critical' ELSE 'warning' END", f"""
            SELECT 'purchase_order', o.purchase_order_id, 'Overdue PO ' || o.purchase_order_id || ' from ' || s.supplier_name,
                   'Expected ' || o.expected_at || ' at ' || o.warehouse_id || ', status ' || o.status,
                   CAST(julianday('{TODAY}') - julianday(o.expected_at) AS INTEGER)
            FROM purchase_orders o JOIN suppliers s USING (supplier_id)
            WHERE o.status IN ('open', 'partial') AND o.expected_at BETWEEN '{shift(-90)}' AND '{shift(-1)}' ORDER BY o.expected_at LIMIT {CAP}"""),
        ("po_stale", "purchasing", "info", f"""
            SELECT 'supplier', o.supplier_id, 'Stale open POs with ' || s.supplier_name,
                   COUNT(*) || ' orders still open more than 90 days after their expected date (oldest ' || MIN(o.expected_at) || ') - confirm or close',
                   COUNT(*)
            FROM purchase_orders o JOIN suppliers s USING (supplier_id)
            WHERE o.status IN ('open', 'partial') AND o.expected_at < '{shift(-90)}' GROUP BY o.supplier_id ORDER BY 5 DESC LIMIT {CAP}"""),
        ("material_po_slip", "purchasing", "warning", f"""
            SELECT 'purchase_order', o.rm_purchase_order_id, 'Material PO ' || o.rm_purchase_order_id || ' slipped',
                   s.supplier_name || ': promised ' || o.promised_at || ', now expected ' || o.expected_at,
                   CAST(julianday(o.expected_at) - julianday(o.promised_at) AS INTEGER)
            FROM rm_purchase_orders o JOIN suppliers s USING (supplier_id)
            WHERE o.status IN ('open', 'partial') AND o.expected_at > o.promised_at ORDER BY 5 DESC LIMIT {CAP}"""),
        ("material_shortage", "production", "critical", f"""
            SELECT DISTINCT 'material', s.rm_id || '@' || s.plant_id, 'Material short for upcoming run: ' || m.rm_name || ' at ' || s.plant_id,
                   'On hand ' || ROUND(s.on_hand_units, 1) || ' vs safety ' || ROUND(s.safety_stock_units, 1) || ' ' || m.uom ||
                   '; next run ' || r.production_run_id || ' on ' || r.planned_start, ROUND(s.days_of_cover, 1)
            FROM rm_inventory_snapshots_weekly s JOIN raw_materials m USING (rm_id)
            JOIN bill_of_materials b ON b.rm_id = s.rm_id
            JOIN production_runs r ON r.product_id = b.product_id AND r.plant_id = s.plant_id
                 AND r.planned_start BETWEEN '{TODAY}' AND '{shift(14)}' AND r.actual_start IS NULL
            WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= '{TODAY}')
              AND s.on_hand_units < s.safety_stock_units GROUP BY s.rm_id, s.plant_id LIMIT {CAP}"""),
        ("shipment_late", "logistics", "CASE WHEN metric > 5 THEN 'critical' ELSE 'warning' END", f"""
            SELECT 'shipment', s.shipment_id, 'Late ' || s.direction || ' shipment ' || s.shipment_id,
                   COALESCE(s.sales_order_id, s.purchase_order_id) || ' to ' || s.destination_name || ', promised ' || s.promised_date,
                   CAST(julianday('{TODAY}') - julianday(s.promised_date) AS INTEGER)
            FROM shipments s WHERE s.status IN ('in_transit', 'exception') AND s.promised_date < '{TODAY}' ORDER BY s.promised_date LIMIT {CAP}"""),
        ("supplier_otif", "suppliers", "warning", f"""
            SELECT 'supplier', c.supplier_id, 'On-time delivery dropped: ' || s.supplier_name,
                   'OTIF ' || ROUND(c.otif_rate * 100) || '% in ' || substr(c.month, 1, 7) || ' vs ' || ROUND(h.avg12 * 100) ||
                   '% 12-month average; avg delay ' || c.avg_delay_days || ' days', c.otif_rate
            FROM supplier_scorecard_monthly c JOIN suppliers s USING (supplier_id)
            JOIN (SELECT supplier_id, AVG(otif_rate) avg12 FROM supplier_scorecard_monthly
                  WHERE month > '{months_back(12)}' AND month <= '{last_complete_month()}' GROUP BY 1) h USING (supplier_id)
            WHERE c.month = '{last_complete_month()}' AND c.po_count >= 3 AND c.otif_rate < h.avg12 - 0.2
            ORDER BY c.otif_rate - h.avg12 LIMIT {CAP}"""),
        ("disruption", "suppliers", "CASE WHEN metric >= 4 THEN 'critical' ELSE 'warning' END", f"""
            SELECT 'disruption', e.event_id, e.title, 'Detected ' || e.detected_at || ' at ' || e.origin_entity_id || ' (' || e.event_type || ')',
                   e.severity
            FROM disruption_events e WHERE e.end_date IS NULL AND e.detected_at > '{shift(-30)}' AND e.detected_at <= '{TODAY}'
              AND e.severity >= 3 ORDER BY e.severity DESC, e.detected_at DESC LIMIT {CAP}"""),
        ("order_late", "sales", "critical", f"""
            SELECT 'sales_order', o.sales_order_id, 'Order ' || o.sales_order_id || ' past promise date',
                   c.customer_name || ', promised ' || o.promised_date || ', status ' || o.status,
                   CAST(julianday('{TODAY}') - julianday(o.promised_date) AS INTEGER)
            FROM sales_orders o JOIN customers c USING (customer_id)
            WHERE o.status IN ('confirmed', 'allocated', 'picking', 'packed') AND o.promised_date < '{TODAY}' LIMIT {CAP}"""),
        ("backorder", "sales", "warning", f"""
            SELECT 'sales_order', l.sales_order_id || ':' || l.product_id, 'Backorder on ' || l.sales_order_id,
                   l.qty_backordered || ' x ' || l.product_id || ' waiting for stock', l.qty_backordered
            FROM sales_order_lines l WHERE l.backorder_status = 'open' LIMIT {CAP}"""),
        ("return_waiting", "returns", "info", f"""
            SELECT 'return', r.rma_id, 'Return ' || r.rma_id || ' not received after 14 days',
                   r.qty || ' x ' || r.product_id || ' authorized ' || r.requested_at, CAST(julianday('{TODAY}') - julianday(r.requested_at) AS INTEGER)
            FROM returns r WHERE r.status = 'authorized' AND r.requested_at < '{shift(-14)}' LIMIT {CAP}"""),
        ("contract_expiring", "suppliers", "info", f"""
            SELECT 'contract', c.contract_id, 'Contract ' || c.contract_id || ' expires soon', s.supplier_name || ': ' || c.scope || ', ends ' || c.valid_to,
                   CAST(julianday(c.valid_to) - julianday('{TODAY}') AS INTEGER)
            FROM contracts c JOIN suppliers s USING (supplier_id)
            WHERE c.valid_to BETWEEN '{TODAY}' AND '{shift(45)}' LIMIT {CAP}"""),
    ]


def evaluate(con) -> list[dict]:
    out = []
    for kind, module, sev, sql in _detectors():
        for r in con.execute(sql).fetchall():
            et, eid, title, detail, metric = r
            severity = sev
            if sev.startswith("CASE"):
                cond = sev.split("WHEN ")[1].split(" THEN")[0]  # "metric > 14"
                op, val = cond.split()[1], float(cond.split()[2])
                hit = (metric or 0) > val if op == ">" else (metric or 0) >= val
                severity = "critical" if hit else "warning"
            out.append({"alert_key": f"{kind}:{eid}", "kind": kind, "severity": severity, "module": module,
                        "entity_type": et, "entity_id": eid, "title": title, "detail": detail, "metric": metric})
    return out


def refresh(con) -> dict:
    t0 = time.time()
    current = evaluate(con)
    keys = {a["alert_key"] for a in current}
    with tx(con):
        open_keys = {r[0] for r in con.execute("SELECT alert_key FROM alerts WHERE status IN ('open', 'acknowledged')")}
        new = 0
        for a in current:
            cur = con.execute("""UPDATE alerts SET severity = ?, title = ?, detail = ?, metric = ?,
                                   status = CASE WHEN status = 'resolved' THEN 'open' ELSE status END,
                                   resolved_at = CASE WHEN status = 'resolved' THEN NULL ELSE resolved_at END,
                                   created_at = CASE WHEN status = 'resolved' THEN ? ELSE created_at END
                                 WHERE alert_key = ?""",
                              (a["severity"], a["title"], a["detail"], a["metric"], now_iso(), a["alert_key"]))
            if cur.rowcount == 0:
                con.execute("""INSERT INTO alerts (alert_key, kind, severity, module, entity_type, entity_id, title, detail, metric, created_at)
                               VALUES (:alert_key, :kind, :severity, :module, :entity_type, :entity_id, :title, :detail, :metric, :created_at)""",
                            {**a, "created_at": now_iso()})
                new += 1
        gone = open_keys - keys
        for k in gone:
            con.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE alert_key = ?", (now_iso(), k))
    return {"active": len(current), "new": new, "resolved": len(gone), "ms": round((time.time() - t0) * 1000)}


def summary(con) -> dict:
    return {
        "by_severity": {r["severity"]: r["n"] for r in rows(con, "SELECT severity, COUNT(*) n FROM alerts WHERE status = 'open' GROUP BY 1")},
        "by_module": rows(con, """SELECT module, COUNT(*) AS n, SUM(severity = 'critical') AS critical FROM alerts
                                  WHERE status = 'open' GROUP BY 1 ORDER BY critical DESC, n DESC"""),
    }
