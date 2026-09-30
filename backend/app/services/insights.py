"""AI insights.

Layer 1 - analytics (exact, deterministic): detectors scan the data for changes that matter and return findings with
their evidence numbers, the records involved and a recommended action.
Layer 2 - language (Groq): explains a finding, writes the daily briefing, or answers a free question by querying the
database read-only. The model is told to use only numbers present in the evidence, and every narrative is checked for
figures that do not appear in it.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import timedelta

from app.config import settings
from app.db import TODAY, last_complete_month, months_back, now_iso, one, rows, scalar, shift
from app.services import forecast as fc
from app.services import llm

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _pct(x) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


# ================================================================ detectors
def carrier_performance(con) -> list[dict]:
    out = []
    for c in rows(con, f"""SELECT c.carrier_id, c.carrier_name,
                                  SUM(CASE WHEN s.ship_date > '{shift(-60)}' THEN s.delivered_at <= s.promised_date END) * 1.0 /
                                  NULLIF(SUM(CASE WHEN s.ship_date > '{shift(-60)}' THEN 1 END), 0) AS recent,
                                  SUM(CASE WHEN s.ship_date BETWEEN '{shift(-240)}' AND '{shift(-61)}' THEN s.delivered_at <= s.promised_date END) * 1.0 /
                                  NULLIF(SUM(CASE WHEN s.ship_date BETWEEN '{shift(-240)}' AND '{shift(-61)}' THEN 1 END), 0) AS before,
                                  SUM(CASE WHEN s.ship_date > '{shift(-60)}' THEN 1 END) AS n_recent
                           FROM shipments s JOIN carriers c USING (carrier_id)
                           WHERE s.direction = 'outbound' AND s.delivered_at IS NOT NULL GROUP BY 1"""):
        if c["recent"] is None or c["before"] is None:
            continue
        drop = c["before"] - c["recent"]
        if drop >= 0.08:
            out.append({
                "id": f"carrier-{c['carrier_id']}", "module": "logistics", "severity": "high" if drop >= 0.15 else "medium",
                "title": f"{c['carrier_name']} on-time delivery fell {drop * 100:.0f} points",
                "summary": f"{c['carrier_name']} delivered {_pct(c['recent'])} of {c['n_recent']} shipments on time in the last 60 days, "
                           f"down from {_pct(c['before'])} in the six months before.",
                "metrics": {"on_time_last_60d": round(c["recent"], 3), "on_time_prior": round(c["before"], 3), "shipments_last_60d": c["n_recent"]},
                "entities": [{"type": "carrier", "id": c["carrier_id"], "label": c["carrier_name"]}],
                "recommendation": f"Shift time-critical lanes from {c['carrier_name']} to the best-performing carrier and raise a service review.",
                "link": "/logistics",
            })
    return out


def return_spikes(con) -> list[dict]:
    out = []
    data = rows(con, f"""
        WITH sold AS (SELECT x.brand_family, SUM(CASE WHEN o.delivered_at > '{shift(-180)}' THEN l.qty_shipped END) AS recent,
                             SUM(CASE WHEN o.delivered_at BETWEEN '{shift(-545)}' AND '{shift(-181)}' THEN l.qty_shipped END) AS before
                      FROM sales_order_lines l JOIN sales_orders o USING (sales_order_id) JOIN products_ext x USING (product_id)
                      WHERE o.delivered_at IS NOT NULL GROUP BY 1),
             ret AS (SELECT x.brand_family, SUM(CASE WHEN r.requested_at > '{shift(-180)}' THEN r.qty END) AS recent,
                            SUM(CASE WHEN r.requested_at BETWEEN '{shift(-545)}' AND '{shift(-181)}' THEN r.qty END) AS before,
                            COUNT(CASE WHEN r.requested_at > '{shift(-180)}' THEN 1 END) AS rmas
                     FROM returns r JOIN products_ext x USING (product_id) GROUP BY 1)
        SELECT s.brand_family, 1.0 * r.recent / s.recent AS rate_recent, 1.0 * r.before / s.before AS rate_before, r.rmas,
               (SELECT reason FROM returns rr JOIN products_ext xx USING (product_id) WHERE xx.brand_family = s.brand_family
                AND rr.requested_at > '{shift(-180)}' GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 1) AS top_reason
        FROM sold s JOIN ret r USING (brand_family) WHERE s.recent > 0 AND s.before > 0 AND r.before > 0""")
    for d in data:
        if d["rate_recent"] and d["rate_before"] and d["rate_recent"] / d["rate_before"] >= 2 and d["rmas"] >= 8:
            out.append({
                "id": f"returns-{d['brand_family'].replace(' ', '_')}", "module": "returns", "severity": "high",
                "title": f"Returns on {d['brand_family']} are {d['rate_recent'] / d['rate_before']:.1f}x their usual rate",
                "summary": f"{_pct(d['rate_recent'])} of {d['brand_family']} units delivered in the last 6 months came back "
                           f"({d['rmas']} RMAs), against {_pct(d['rate_before'])} the year before. Most common reason: {d['top_reason']}.",
                "metrics": {"return_rate_6m": round(d["rate_recent"], 4), "return_rate_prior": round(d["rate_before"], 4),
                            "rmas_6m": d["rmas"], "top_reason": d["top_reason"]},
                "entities": [{"type": "brand", "id": d["brand_family"], "label": d["brand_family"]}],
                "recommendation": f"Quarantine incoming {d['brand_family']} stock for inspection and open a quality claim with the supplier.",
                "link": f"/returns?q={d['brand_family']}",
            })
    return out


def supplier_decline(con) -> list[dict]:
    data = rows(con, f"""
        SELECT s.supplier_id, s.supplier_name,
               AVG(CASE WHEN c.month > '{months_back(3)}' THEN c.otif_rate END) AS recent,
               AVG(CASE WHEN c.month > '{months_back(9)}' AND c.month <= '{months_back(3)}' THEN c.otif_rate END) AS before,
               (SELECT COUNT(*) FROM rm_purchase_orders o WHERE o.supplier_id = s.supplier_id AND o.status IN ('open', 'partial')) +
               (SELECT COUNT(*) FROM purchase_orders o WHERE o.supplier_id = s.supplier_id AND o.status IN ('open', 'partial')) AS open_pos
        FROM supplier_scorecard_monthly c JOIN suppliers s USING (supplier_id)
        WHERE c.month <= '{last_complete_month()}' GROUP BY 1
        HAVING recent IS NOT NULL AND before IS NOT NULL AND before - recent >= 0.15 AND before >= 0.3
        ORDER BY before - recent DESC LIMIT 5""")
    if not data:
        return []
    worst = data[0]
    return [{
        "id": "supplier-decline", "module": "suppliers", "severity": "high" if len(data) >= 3 else "medium",
        "title": f"{len(data)} suppliers' on-time delivery dropped sharply in the last 3 months",
        "summary": f"Worst: {worst['supplier_name']} fell from {_pct(worst['before'])} to {_pct(worst['recent'])} OTIF. "
                   + "; ".join(f"{d['supplier_id']} {_pct(d['before'])}→{_pct(d['recent'])}" for d in data[1:]),
        "metrics": {d["supplier_id"]: {"otif_prior": round(d["before"], 3), "otif_last_3m": round(d["recent"], 3), "open_pos": d["open_pos"]} for d in data},
        "entities": [{"type": "supplier", "id": d["supplier_id"], "label": d["supplier_name"]} for d in data],
        "recommendation": "Add buffer lead time on open orders with these suppliers and qualify a second source for their critical items.",
        "link": "/suppliers?sort=otif_6m:asc",
    }]


def forecast_bias(con) -> list[dict]:
    out = []
    for cat in [r["category"] for r in rows(con, "SELECT DISTINCT category FROM products ORDER BY 1")]:
        f = fc.forecast(con, "category", cat, horizon=4, history=0)
        a = f.get("accuracy")
        if not a or a["planner_bias"] is None:
            continue
        if abs(a["planner_bias"]) >= 0.1:
            direction = "over" if a["planner_bias"] > 0 else "under"
            better = a["model_wape"] is not None and a["planner_wape"] is not None and a["model_wape"] < a["planner_wape"]
            out.append({
                "id": f"bias-{cat}", "module": "forecasting", "severity": "medium",
                "title": f"Planner forecast for {cat.replace('_', ' ')} is {abs(a['planner_bias']) * 100:.0f}% {direction}-forecasting",
                "summary": f"Over the last {a['weeks']} weeks the planner forecast for {cat} was {_pct(abs(a['planner_bias']))} "
                           f"{'above' if direction == 'over' else 'below'} actual demand (error {_pct(a['planner_wape'])}); "
                           f"the statistical model's error was {_pct(a['model_wape'])}.",
                "metrics": a,
                "entities": [{"type": "category", "id": cat, "label": cat}],
                "recommendation": (f"Use the statistical forecast for {cat} replenishment" if better else
                                   f"Recalibrate the {cat} planner forecast") + f" to avoid {'excess stock' if direction == 'over' else 'stockouts'}.",
                "link": f"/forecasting?level=category&key={cat}",
            })
    return out


def excess_inventory(con) -> list[dict]:
    flow = one(con, """SELECT SUM(CASE WHEN movement_type = 'receipt' THEN quantity_change END) AS received,
                              -SUM(CASE WHEN movement_type = 'sale' THEN quantity_change END) AS shipped
                       FROM inventory_movements WHERE movement_at > ?""", (shift(-365),))
    val = one(con, """SELECT ROUND(SUM(s.on_hand * p.unit_cost), 0) AS total,
                             ROUND(SUM(CASE WHEN s.on_hand > pol.max_stock THEN (s.on_hand - pol.max_stock) * p.unit_cost END), 0) AS above_max,
                             SUM(s.on_hand > pol.max_stock) AS positions
                      FROM stock_levels s JOIN products p USING (product_id) JOIN stock_policies pol USING (product_id, warehouse_id)
                      WHERE s.on_hand > 0""")
    if not flow or not flow["shipped"] or not val["total"]:
        return []
    ratio = flow["received"] / flow["shipped"]
    top = rows(con, """SELECT pr.supplier_id, su.supplier_name, SUM(m.quantity_change) AS units
                       FROM inventory_movements m JOIN products pr USING (product_id) JOIN suppliers su ON su.supplier_id = pr.supplier_id
                       WHERE m.movement_type = 'receipt' AND m.movement_at > ? GROUP BY 1 ORDER BY units DESC LIMIT 3""", (shift(-365),))
    if ratio < 1.5:
        return []
    return [{
        "id": "replenishment-ahead", "module": "inventory", "severity": "high",
        "title": f"Replenishment is running {ratio:.0f}x ahead of demand",
        "summary": f"{flow['received']:,.0f} units received in the last 12 months against {flow['shipped']:,.0f} shipped to customers. "
                   f"{val['positions']:,} stock positions are above max stock, holding ${val['above_max']:,.0f} of the "
                   f"${val['total']:,.0f} inventory value.",
        "metrics": {"received_12m": flow["received"], "shipped_12m": flow["shipped"], "ratio": round(ratio, 1),
                    "positions_above_max": val["positions"], "value_above_max": val["above_max"], "inventory_value": val["total"]},
        "entities": [{"type": "supplier", "id": t["supplier_id"], "label": t["supplier_name"]} for t in top],
        "recommendation": "Freeze automatic reorders for positions above max stock, cut open PO quantities with the largest inbound suppliers, and rebalance surplus to warehouses with demand.",
        "link": "/inventory?status=excess",
    }]


def material_risk(con) -> list[dict]:
    snap = scalar(con, "SELECT MAX(snapshot_date) FROM rm_inventory_snapshots_weekly WHERE snapshot_date <= ?", (TODAY,))
    data = rows(con, f"""SELECT s.rm_id, m.rm_name, s.plant_id, ROUND(s.on_hand_units, 1) AS on_hand, ROUND(s.safety_stock_units, 1) AS safety,
                                COUNT(DISTINCT r.production_run_id) AS runs
                         FROM rm_inventory_snapshots_weekly s JOIN raw_materials m USING (rm_id)
                         JOIN bill_of_materials b ON b.rm_id = s.rm_id
                         JOIN production_runs r ON r.product_id = b.product_id AND r.plant_id = s.plant_id
                              AND r.planned_start BETWEEN '{shift(-7)}' AND '{shift(21)}'
                         WHERE s.snapshot_date = ? AND s.on_hand_units < s.safety_stock_units
                         GROUP BY 1, 3 ORDER BY runs DESC LIMIT 8""", (snap,))
    if not data:
        return []
    runs = sum(d["runs"] for d in data)
    return [{
        "id": "material-risk", "module": "production", "severity": "critical" if runs >= 10 else "high",
        "title": f"{len(data)} raw materials below safety stock feed {runs} production runs in the next 3 weeks",
        "summary": "; ".join(f"{d['rm_name']} at {d['plant_id']}: {d['on_hand']} vs safety {d['safety']} ({d['runs']} runs)" for d in data[:4]) + ".",
        "metrics": {"materials": len(data), "runs_at_risk": runs, "snapshot": snap},
        "entities": [{"type": "material", "id": d["rm_id"], "label": f"{d['rm_name']} @ {d['plant_id']}"} for d in data],
        "recommendation": "Expedite open material POs for these items or resequence the affected runs behind materials that are in stock.",
        "link": "/production",
    }]


def demand_shift(con) -> list[dict]:
    out = []
    lw = fc.last_complete_week()
    for d in rows(con, """SELECT p.category,
                                 SUM(CASE WHEN week_start > :r THEN actual_demand_units END) AS recent,
                                 SUM(CASE WHEN week_start > :ly0 AND week_start <= :ly1 THEN actual_demand_units END) AS last_year
                          FROM product_demand_weekly JOIN products p USING (product_id) GROUP BY 1""",
                      {"r": (lw - timedelta(weeks=8)).isoformat(), "ly0": (lw - timedelta(weeks=60)).isoformat(),
                       "ly1": (lw - timedelta(weeks=52)).isoformat()}):
        if d["recent"] and d["last_year"]:
            ch = d["recent"] / d["last_year"] - 1
            if abs(ch) >= 0.15:
                out.append({
                    "id": f"demand-{d['category']}", "module": "forecasting", "severity": "medium",
                    "title": f"{d['category'].replace('_', ' ').title()} demand is {'up' if ch > 0 else 'down'} {abs(ch) * 100:.0f}% year on year",
                    "summary": f"{d['recent']:,.0f} units in the last 8 weeks against {d['last_year']:,.0f} in the same weeks last year.",
                    "metrics": {"units_last_8w": d["recent"], "units_same_8w_last_year": d["last_year"], "change": round(ch, 3)},
                    "entities": [{"type": "category", "id": d["category"], "label": d["category"]}],
                    "recommendation": "Adjust min/max stock for this category to the new run-rate before the next replenishment cycle.",
                    "link": f"/forecasting?level=category&key={d['category']}",
                })
    return out


def overdue_purchasing(con) -> list[dict]:
    recent = rows(con, f"""SELECT o.supplier_id, s.supplier_name, COUNT(*) AS n,
                                  MAX(CAST(julianday('{TODAY}') - julianday(o.expected_at) AS INTEGER)) AS worst_days
                           FROM purchase_orders o JOIN suppliers s USING (supplier_id)
                           WHERE o.status IN ('open', 'partial') AND o.expected_at BETWEEN '{shift(-90)}' AND '{shift(-1)}'
                           GROUP BY 1 ORDER BY n DESC LIMIT 5""")
    n_recent = scalar(con, f"SELECT COUNT(*) FROM purchase_orders WHERE status IN ('open', 'partial') AND expected_at BETWEEN '{shift(-90)}' AND '{shift(-1)}'")
    stale = one(con, f"""SELECT COUNT(*) AS n, MIN(expected_at) AS oldest,
                                ROUND(SUM((SELECT SUM((quantity_ordered - COALESCE(quantity_received, 0)) * unit_cost) FROM purchase_order_lines l
                                           WHERE l.purchase_order_id = o.purchase_order_id)), 0) AS value
                         FROM purchase_orders o WHERE status IN ('open', 'partial') AND expected_at < '{shift(-90)}'""")
    out = []
    if n_recent:
        out.append({
            "id": "overdue-pos", "module": "purchasing", "severity": "high" if n_recent > 25 else "medium",
            "title": f"{n_recent} purchase orders are overdue",
            "summary": "Expected in the last 90 days and not yet received. Most by supplier: "
                       + ", ".join(f"{d['supplier_name']} ({d['n']}, up to {d['worst_days']} days)" for d in recent) + ".",
            "metrics": {"overdue_pos": n_recent, "by_supplier": {d["supplier_id"]: d["n"] for d in recent}},
            "entities": [{"type": "supplier", "id": d["supplier_id"], "label": d["supplier_name"]} for d in recent],
            "recommendation": "Chase confirmed ship dates for these orders and re-plan allocations that depend on them.",
            "link": "/purchasing?late=1",
        })
    if stale and stale["n"]:
        out.append({
            "id": "stale-pos", "module": "purchasing", "severity": "medium",
            "title": f"{stale['n']:,} open purchase orders are more than 90 days past due",
            "summary": f"The oldest was expected on {stale['oldest']}. They hold ${stale['value']:,.0f} of unreceived value and inflate "
                       "on-order quantities used by replenishment planning.",
            "metrics": {"stale_pos": stale["n"], "oldest_expected": stale["oldest"], "unreceived_value": stale["value"]},
            "entities": [],
            "recommendation": "Confirm each with the supplier and close or cancel the ones that will not ship, so reorder suggestions use real pipeline stock.",
            "link": "/purchasing?late=1",
        })
    return out


def customer_drop(con) -> list[dict]:
    data = rows(con, f"""SELECT c.customer_id, c.customer_name,
                                SUM(CASE WHEN o.order_date > '{shift(-90)}' THEN o.order_value ELSE 0 END) AS recent,
                                SUM(CASE WHEN o.order_date BETWEEN '{shift(-180)}' AND '{shift(-91)}' THEN o.order_value ELSE 0 END) AS before
                         FROM sales_orders o JOIN customers c USING (customer_id) WHERE o.status != 'cancelled'
                         GROUP BY 1 HAVING before > 20000 AND recent < before * 0.5 ORDER BY before - recent DESC LIMIT 5""")
    if not data:
        return []
    lost = sum(d["before"] - d["recent"] for d in data)
    return [{
        "id": "customer-drop", "module": "sales", "severity": "medium",
        "title": f"{len(data)} key customers halved their orders this quarter",
        "summary": ", ".join(f"{d['customer_name']} ${d['before']:,.0f}→${d['recent']:,.0f}" for d in data) + f" (${lost:,.0f} less revenue).",
        "metrics": {d["customer_id"]: {"revenue_prior_90d": round(d["before"]), "revenue_last_90d": round(d["recent"])} for d in data},
        "entities": [{"type": "customer", "id": d["customer_id"], "label": d["customer_name"]} for d in data],
        "recommendation": "Have account managers contact these customers; check for service failures or backorders on their recent orders.",
        "link": "/sales/customers",
    }]


DETECTORS = [material_risk, carrier_performance, return_spikes, supplier_decline, overdue_purchasing, excess_inventory,
             forecast_bias, demand_shift, customer_drop]


def compute(con, use_cache: bool = True) -> dict:
    """All current findings. Cached until the next write (the cache key includes the audit log length)."""
    version = f"{scalar(con, 'SELECT COUNT(*) FROM audit_log')}:{TODAY}"
    if use_cache:
        hit = one(con, "SELECT payload FROM insight_cache WHERE key = ?", (f"insights:{version}",))
        if hit:
            return json.loads(hit["payload"])
    t0 = time.time()
    found = []
    for det in DETECTORS:
        try:
            found += det(con)
        except Exception as ex:  # one broken detector must not hide the others
            found.append({"id": f"error-{det.__name__}", "module": "system", "severity": "low", "title": f"{det.__name__} failed",
                          "summary": str(ex), "metrics": {}, "entities": [], "recommendation": "", "link": "/"})
    found.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 9), f["module"]))
    out = {"generated_at": now_iso(), "business_date": TODAY, "insights": found, "ms": round((time.time() - t0) * 1000)}
    con.execute("INSERT OR REPLACE INTO insight_cache VALUES (?, ?, ?)", (f"insights:{version}", now_iso(), json.dumps(out, default=str)))
    con.commit()
    return out


# ================================================================ language layer
NUM = re.compile(r"(?<![A-Za-z0-9])\$?\d[\d,]*(?:\.\d+)?%?")


def ungrounded(text: str, evidence: str) -> list[str]:
    """Numbers in the narrative that do not occur in the evidence (ignoring small counts and years)."""
    ev = {n.replace(",", "").replace("$", "").rstrip("%") for n in NUM.findall(evidence)}
    bad = []
    for n in NUM.findall(text):
        v = n.replace(",", "").replace("$", "").rstrip("%")
        try:
            f = float(v)
        except ValueError:
            continue
        if f <= 12 or 2000 <= f <= 2100 or v in ev:
            continue
        if any(abs(float(e) - f) < 0.6 or abs(float(e) * 100 - f) < 0.6 for e in ev if re.fullmatch(r"\d+(\.\d+)?", e)):
            continue
        bad.append(n)
    return sorted(set(bad))


SYSTEM = ("You are the supply chain analyst inside Meridian, an operations system. Write for a busy operations manager: "
          "short, specific, no filler. Use ONLY facts and numbers present in the evidence you are given - never invent or "
          "extrapolate figures, and never assume costs, penalties, rates or prices that are not in the evidence; when an impact "
          "cannot be quantified from the evidence, describe it in words. Refer to records by their ids. Business date: {today}.")


def _cached_llm(con, key: str, messages: list[dict], evidence: str, max_tokens: int = 900) -> dict:
    hit = one(con, "SELECT payload FROM insight_cache WHERE key = ?", (key,))
    if hit:
        return {**json.loads(hit["payload"]), "cached": True}
    t0 = time.time()
    msg = llm.chat(messages, max_tokens=max_tokens)
    text = (msg.get("content") or "").strip()
    out = {"text": text, "ungrounded_numbers": ungrounded(text, evidence), "model": settings.groq_model,
           "latency_s": round(time.time() - t0, 1), "usage": msg.get("_usage", {}), "generated_at": now_iso()}
    con.execute("INSERT OR REPLACE INTO insight_cache VALUES (?, ?, ?)", (key, now_iso(), json.dumps(out)))
    con.commit()
    return {**out, "cached": False}


def explain(con, insight_id: str) -> dict:
    data = compute(con)
    f = next((i for i in data["insights"] if i["id"] == insight_id), None)
    if not f:
        raise KeyError(insight_id)
    memories = _memory_context(f)
    evidence = json.dumps({"finding": f, "institutional_memory": memories}, default=str)
    msgs = [{"role": "system", "content": SYSTEM.format(today=TODAY)},
            {"role": "user", "content": f"Evidence (JSON: the finding plus what the organisation's memory recalls about it):\n{evidence}\n\n"
                                         "Explain in markdown with four short sections: **What is happening**, **Has this happened before** "
                                         "(use institutional_memory; cite dates and document ids, say so if nothing relevant), "
                                         "**Why it matters** (operational and financial impact), **What to do now** (3 concrete numbered "
                                         "steps naming the records involved, informed by what worked before)."}]
    out = _cached_llm(con, f"explain:{insight_id}:{data['generated_at'][:13]}:{hash(evidence)}", msgs, evidence)
    return {**out, "memories": memories}


def _memory_context(finding: dict, k: int = 8) -> list[dict]:
    """What the memory bank recalls about a finding (fast recall, dated on or before the business date)."""
    from app.memory import intel
    from app.memory import service as mem
    if not mem.enabled():
        return []
    q = f"{finding['title']}. " + " ".join(e.get("id", "") for e in finding.get("entities", [])[:6])
    try:
        r = mem.recall(q, as_of=TODAY, budget="mid", max_tokens=1500, include_entities=False)
    except mem.MemoryError_:
        return []
    return [{k2: v for k2, v in intel.resolve(h).items() if k2 in ("text", "type", "when", "document_id", "records")}
            for h in r["results"][:k]]


def briefing(con, kpis: dict) -> dict:
    data = compute(con)
    top = [{k: i[k] for k in ("id", "severity", "title", "summary", "recommendation")} for i in data["insights"][:8]]
    evidence = json.dumps({"kpis": kpis, "findings": top}, default=str)
    msgs = [{"role": "system", "content": SYSTEM.format(today=TODAY)},
            {"role": "user", "content": f"Evidence:\n{evidence}\n\nWrite today's operations briefing in markdown: one sentence "
                                         "headline, then **Top risks** (max 4 bullets, most urgent first), **Watch** (max 3 bullets), "
                                         "**Actions for today** (max 4 numbered, each naming an owner function: procurement, "
                                         "warehouse, logistics, planning, sales). Under 220 words."}]
    return _cached_llm(con, f"briefing:{data['generated_at'][:13]}:{hash(evidence)}", msgs, evidence, max_tokens=1100)


# ---------------------------------------------------------------- ask the data (read-only SQL tool)
_DENY = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE,
         sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
         sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX}


def _readonly_sql(sql: str) -> dict:
    from app.db import connect
    con = connect()
    con.set_authorizer(lambda action, *a: sqlite3.SQLITE_DENY if action in _DENY else sqlite3.SQLITE_OK)
    try:
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description or []]
        data = [dict(zip(cols, r)) for r in cur.fetchmany(60)]
        return {"columns": cols, "rows": data, "truncated": len(data) == 60}
    except sqlite3.Error as ex:
        return {"error": str(ex)}
    finally:
        con.close()


SCHEMA_TABLES = ["products", "products_ext", "suppliers", "warehouses", "plants", "customers", "carriers", "stock_levels",
                 "stock_policies", "inventory_movements", "purchase_orders", "purchase_order_lines", "rm_purchase_orders",
                 "rm_purchase_order_lines", "raw_materials", "rm_inventory_snapshots_weekly", "bill_of_materials",
                 "production_runs", "sales_orders", "sales_order_lines", "shipments", "returns", "product_demand_weekly",
                 "supplier_scorecard_monthly", "contracts", "disruption_events", "alerts"]


def schema(con) -> str:
    parts = []
    for t in SCHEMA_TABLES:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        parts.append(f"{t}({', '.join(cols)})")
    return "\n".join(parts)


TOOLS = [{"type": "function", "function": {
    "name": "run_sql", "description": "Run one read-only SQLite SELECT against the Meridian database. Returns up to 60 rows.",
    "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
    {"type": "function", "function": {
    "name": "search_memory", "description": "Search the organisation's long-term memory (Hindsight): buyer notes, supplier emails, "
    "meeting notes, past decisions and their outcomes, lessons, consolidated observations, and every action recorded in Meridian. "
    "Use it for why/how/history/lessons questions that the tables cannot answer. Returns dated memories with document ids.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                   "types": {"type": "array", "items": {"type": "string", "enum": ["world", "experience", "observation"]}}},
                   "required": ["query"]}}}]


def _search_memory(query: str, types: list[str] | None = None) -> dict:
    from app.memory import intel
    from app.memory import service as mem
    try:
        r = mem.recall(query, types=types or None, as_of=TODAY, budget="mid", max_tokens=2000, include_entities=False)
    except mem.MemoryError_ as ex:
        return {"error": str(ex)}
    return {"memories": [{k: v for k, v in intel.resolve(h).items() if k in ("text", "type", "when", "document_id", "records")}
                         for h in r["results"][:15]]}


def ask(con, question: str) -> dict:
    t0 = time.time()
    sys = (SYSTEM.format(today=TODAY) + " Answer questions by querying the database with run_sql (SQLite dialect; aggregate "
           "in SQL, never select huge row sets). Dates are ISO strings. Domain rules: stock on hand is stock_levels.on_hand, available = "
           "on_hand - allocated; an open purchase order has status IN ('open','partial') (cancelled/received/draft are not open); "
           "overdue = open and expected_at < business date; sales_orders.order_value is revenue (exclude status 'cancelled'); "
           "shipments are on time when delivered_at <= promised_date; supplier_scorecard_monthly.month is the first day of the month. "
           "For history, reasons, lessons or anything qualitative, also call search_memory and cite memory document ids. "
           "Then answer in concise markdown with a small table when useful, and state which tables you used."
           f"\n\nSchema:\n{schema(con)}")
    msgs = [{"role": "system", "content": sys}, {"role": "user", "content": question}]
    queries, evidence = [], []
    for _ in range(6):
        m = llm.chat(msgs, tools=TOOLS, max_tokens=1500)
        calls = m.get("tool_calls") or []
        msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls")})
        if not calls:
            text = (m.get("content") or "").strip()
            return {"answer": text, "queries": queries, "ungrounded_numbers": ungrounded(text, json.dumps(evidence, default=str) + question),
                    "latency_s": round(time.time() - t0, 1), "model": settings.groq_model}
        for c in calls:
            try:
                args = json.loads(c["function"]["arguments"])
            except (ValueError, KeyError):
                args = {}
            if c["function"].get("name") == "search_memory":
                res = _search_memory(args.get("query", ""), args.get("types"))
                queries.append({"tool": "memory", "sql": f"recall: {args.get('query', '')}", "rows": len(res.get("memories", [])),
                                "error": res.get("error")})
            else:
                sql = args.get("sql", "")
                res = _readonly_sql(sql) if sql else {"error": "missing sql"}
                queries.append({"tool": "sql", "sql": sql, "rows": len(res.get("rows", [])), "error": res.get("error")})
            evidence.append(res)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(res, default=str)[:6000]})
    return {"answer": "I could not finish within the query budget. Try a narrower question.", "queries": queries,
            "ungrounded_numbers": [], "latency_s": round(time.time() - t0, 1), "model": settings.groq_model}
