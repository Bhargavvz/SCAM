"""Build the Meridian operational database from the supply chain dataset.

1. Copy the source dataset SQLite byte-for-byte (the source file is never modified) and record its SHA-256.
2. Derive the records the dataset does not have, deterministically (fixed seed), so they reconcile with it:
     customers, sales orders + lines   <- every 'sale' inventory movement (qty shipped) and every unfulfilled
                                          weekly demand row (qty backordered); totals match the dataset exactly
     carriers, shipments               <- one outbound shipment per sales order, one inbound per purchase order
     returns (RMAs)                    <- a small, category-dependent share of delivered sales order lines
     stock_policies                    <- min stock = the dataset's reorder point, max stock from demand
3. Create the operational tables the application writes to (alerts, audit log, warehouse tasks, tracking events).
4. Write data/manifest.json: source checksum, row counts per table, lineage of every derived table.

Run:  python -m app.build_db [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta

from app.config import settings

SEED = 20250905
TODAY = date.fromisoformat(settings.today)

REGION_CITIES = {
    "West": ["Portland", "Seattle", "Sacramento", "Reno", "Boise", "Spokane", "Eugene", "Fresno"],
    "Southwest": ["Phoenix", "Tucson", "Albuquerque", "El Paso", "Las Vegas", "San Antonio", "Austin", "Mesa"],
    "Midwest": ["Chicago", "Columbus", "Indianapolis", "Milwaukee", "Des Moines", "Omaha", "Kansas City", "Detroit"],
    "Northeast": ["Boston", "Hartford", "Albany", "Providence", "Newark", "Buffalo", "Pittsburgh", "Scranton"],
    "Southeast": ["Atlanta", "Charlotte", "Nashville", "Orlando", "Jacksonville", "Raleigh", "Birmingham", "Tampa"],
    "Canada": ["Toronto", "Montreal", "Ottawa", "Calgary", "Winnipeg", "Hamilton", "Quebec City", "London ON"],
}
NAME_A = ["Cedar", "Harbor", "Summit", "Pioneer", "Granite", "Prairie", "Lakeside", "Beacon", "Ironwood", "Northgate",
          "Silverline", "Keystone", "Bluewater", "Redstone", "Oakridge", "Evergreen", "Crescent", "Highland", "Riverside", "Sterling"]
NAME_B = ["Supply", "Distribution", "Industrial", "Retail", "Trading", "Hardware", "Outfitters", "Wholesale", "Mart", "Depot",
          "Components", "Traders", "Goods", "Merchants", "Partners"]
SEGMENTS = [("Distributor", 0.30, 0.90), ("Retail chain", 0.30, 0.95), ("OEM", 0.20, 0.88), ("E-commerce", 0.20, 1.00)]
CHANNELS = {"Distributor": "EDI", "Retail chain": "EDI", "OEM": "Sales rep", "E-commerce": "Web portal"}
CARRIERS = [
    ("CR01", "Summit Freight", "LTL", 0.93, 1.9),
    ("CR02", "BlueLine Parcel", "Parcel", 0.95, 3.4),
    ("CR03", "Northstar Logistics", "FTL", 0.91, 1.2),
    ("CR04", "Maple Express", "LTL", 0.94, 2.1),
    ("CR05", "Pacific Haul", "FTL", 0.92, 1.3),
]
RETURN_REASONS = [("damaged_in_transit", "scrap"), ("defective", "replace"), ("wrong_item", "restock"),
                  ("not_needed", "restock"), ("quality_issue", "refurbish")]
RETURN_RATE = {"finished_goods": 0.028, "components": 0.018, "spares": 0.015, "consumables": 0.010, "packaging": 0.008}

SCHEMA = """
CREATE TABLE customers (customer_id TEXT PRIMARY KEY, customer_name TEXT, segment TEXT, region TEXT, city TEXT,
  home_warehouse_id TEXT, credit_limit REAL, payment_terms TEXT, discount_pct REAL, since DATE, status TEXT);
CREATE TABLE carriers (carrier_id TEXT PRIMARY KEY, carrier_name TEXT, mode TEXT, base_on_time REAL, cost_per_km_unit REAL);
CREATE TABLE sales_orders (sales_order_id TEXT PRIMARY KEY, customer_id TEXT, warehouse_id TEXT, channel TEXT,
  order_date DATE, requested_date DATE, promised_date DATE, shipped_at DATE, delivered_at DATE, status TEXT,
  order_value REAL, origin TEXT DEFAULT 'derived', created_at TEXT, notes TEXT);
CREATE TABLE sales_order_lines (sales_order_line_id TEXT PRIMARY KEY, sales_order_id TEXT, product_id TEXT,
  qty_ordered INTEGER, qty_allocated INTEGER, qty_shipped INTEGER, qty_backordered INTEGER, backorder_status TEXT,
  unit_price REAL, movement_id TEXT);
CREATE TABLE shipments (shipment_id TEXT PRIMARY KEY, direction TEXT, sales_order_id TEXT, purchase_order_id TEXT,
  carrier_id TEXT, mode TEXT, origin_id TEXT, origin_name TEXT, destination_id TEXT, destination_name TEXT,
  distance_km REAL, units INTEGER, ship_date DATE, promised_date DATE, delivered_at DATE, status TEXT,
  freight_cost REAL, tracking_no TEXT);
CREATE TABLE shipment_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT, at TEXT, status TEXT,
  location TEXT, note TEXT);
CREATE TABLE returns (rma_id TEXT PRIMARY KEY, sales_order_id TEXT, sales_order_line_id TEXT, customer_id TEXT,
  product_id TEXT, warehouse_id TEXT, qty INTEGER, reason TEXT, requested_at DATE, received_at DATE,
  closed_at DATE, disposition TEXT, status TEXT, refund_value REAL, replacement_order_id TEXT, notes TEXT,
  origin TEXT DEFAULT 'derived');
CREATE TABLE stock_policies (product_id TEXT, warehouse_id TEXT, min_stock INTEGER, max_stock INTEGER,
  avg_weekly_demand REAL, PRIMARY KEY (product_id, warehouse_id));
CREATE TABLE alerts (alert_id INTEGER PRIMARY KEY AUTOINCREMENT, alert_key TEXT UNIQUE, kind TEXT, severity TEXT,
  module TEXT, entity_type TEXT, entity_id TEXT, title TEXT, detail TEXT, metric REAL, created_at TEXT,
  status TEXT DEFAULT 'open', acknowledged_by TEXT, acknowledged_at TEXT, resolved_at TEXT);
CREATE TABLE audit_log (audit_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, business_date DATE, actor TEXT,
  module TEXT, action TEXT, entity_type TEXT, entity_id TEXT, summary TEXT, payload TEXT);
CREATE TABLE warehouse_tasks (task_id TEXT PRIMARY KEY, warehouse_id TEXT, task_type TEXT, ref_type TEXT, ref_id TEXT,
  status TEXT, created_at TEXT, completed_at TEXT, assignee TEXT, units INTEGER, bin TEXT);
CREATE TABLE app_records (table_name TEXT, record_id TEXT, created_at TEXT, PRIMARY KEY (table_name, record_id));
CREATE TABLE insight_cache (key TEXT PRIMARY KEY, created_at TEXT, payload TEXT);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_im_pw ON inventory_movements(product_id, warehouse_id);
CREATE INDEX IF NOT EXISTS ix_im_at ON inventory_movements(movement_at);
CREATE INDEX IF NOT EXISTS ix_im_type ON inventory_movements(movement_type, movement_at);
CREATE INDEX IF NOT EXISTS ix_ob_pw ON inventory_opening_balances(product_id, warehouse_id);
CREATE INDEX IF NOT EXISTS ix_pol_po ON purchase_order_lines(purchase_order_id);
CREATE INDEX IF NOT EXISTS ix_pol_prod ON purchase_order_lines(product_id);
CREATE INDEX IF NOT EXISTS ix_po_sup ON purchase_orders(supplier_id);
CREATE INDEX IF NOT EXISTS ix_po_status ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS ix_rpo_sup ON rm_purchase_orders(supplier_id);
CREATE INDEX IF NOT EXISTS ix_rpol_po ON rm_purchase_order_lines(rm_purchase_order_id);
CREATE INDEX IF NOT EXISTS ix_pdw_prod ON product_demand_weekly(product_id, week_start);
CREATE INDEX IF NOT EXISTS ix_pdw_week ON product_demand_weekly(week_start);
CREATE INDEX IF NOT EXISTS ix_pr_start ON production_runs(planned_start);
CREATE INDEX IF NOT EXISTS ix_pr_prod ON production_runs(product_id);
CREATE INDEX IF NOT EXISTS ix_rms_date ON rm_inventory_snapshots_weekly(snapshot_date);
CREATE INDEX IF NOT EXISTS ix_bom_prod ON bill_of_materials(product_id);
CREATE INDEX IF NOT EXISTS ix_so_cust ON sales_orders(customer_id);
CREATE INDEX IF NOT EXISTS ix_so_date ON sales_orders(order_date);
CREATE INDEX IF NOT EXISTS ix_so_status ON sales_orders(status);
CREATE INDEX IF NOT EXISTS ix_sol_so ON sales_order_lines(sales_order_id);
CREATE INDEX IF NOT EXISTS ix_sol_prod ON sales_order_lines(product_id);
CREATE INDEX IF NOT EXISTS ix_sh_so ON shipments(sales_order_id);
CREATE INDEX IF NOT EXISTS ix_sh_po ON shipments(purchase_order_id);
CREATE INDEX IF NOT EXISTS ix_sh_ship ON shipments(ship_date);
CREATE INDEX IF NOT EXISTS ix_sh_carrier ON shipments(carrier_id, ship_date);
CREATE INDEX IF NOT EXISTS ix_ret_prod ON returns(product_id);
CREATE INDEX IF NOT EXISTS ix_ret_req ON returns(requested_at);
CREATE INDEX IF NOT EXISTS ix_de_detect ON disruption_events(detected_at);
CREATE INDEX IF NOT EXISTS ix_alert_status ON alerts(status, severity);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS ix_task_wh ON warehouse_tasks(warehouse_id, status);
"""

VIEWS = """
CREATE VIEW IF NOT EXISTS stock_on_hand AS
  SELECT product_id, warehouse_id, SUM(units) AS on_hand FROM (
    SELECT product_id, warehouse_id, opening_units AS units FROM inventory_opening_balances
    UNION ALL SELECT product_id, warehouse_id, quantity_change FROM inventory_movements)
  GROUP BY product_id, warehouse_id;
CREATE VIEW IF NOT EXISTS stock_allocated AS
  SELECT l.product_id, o.warehouse_id, SUM(l.qty_allocated - l.qty_shipped) AS allocated
  FROM sales_order_lines l JOIN sales_orders o USING (sales_order_id)
  WHERE o.status IN ('allocated', 'picking', 'packed') GROUP BY 1, 2;
CREATE VIEW IF NOT EXISTS stock_on_order AS
  SELECT l.product_id, o.warehouse_id, SUM(l.quantity_ordered - COALESCE(l.quantity_received, 0)) AS on_order
  FROM purchase_order_lines l JOIN purchase_orders o USING (purchase_order_id)
  WHERE o.status IN ('open', 'partial') GROUP BY 1, 2;
"""


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def d(s: str) -> date:
    return date.fromisoformat(s[:10])


def build(force: bool = False) -> dict:
    src, dst = settings.source_db, settings.db_path
    if not src.exists():
        raise SystemExit(f"source dataset not found: {src}")
    if dst.exists() and not force:
        raise SystemExit(f"{dst} exists - pass --force to rebuild (this discards records created in the app)")
    t0 = time.time()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    src_hash = sha256(src)
    shutil.copyfile(src, tmp)
    source_tables = [r[0] for r in sqlite3.connect(f"file:{src}?mode=ro", uri=True).execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    rng = random.Random(SEED)

    warehouses = {r["warehouse_id"]: dict(r) for r in con.execute("SELECT * FROM warehouses")}
    products = {r["product_id"]: dict(r) for r in con.execute(
        "SELECT p.*, x.unit_price, x.brand_family, x.abc_class FROM products p JOIN products_ext x USING (product_id)")}

    # ---------------------------------------------------------------- customers
    customers, by_wh = [], defaultdict(list)
    used = set()
    n = 0
    for wid, w in sorted(warehouses.items()):
        for _ in range(22):
            n += 1
            while True:
                name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B)}"
                if name not in used:
                    used.add(name)
                    break
            r = rng.random()
            acc = 0.0
            for seg, share, disc in SEGMENTS:
                acc += share
                if r <= acc:
                    break
            cid = f"CUS{n:04d}"
            city = rng.choice(REGION_CITIES[w["region"]])
            customers.append((cid, name, seg, w["region"], city, wid, round(rng.choice([25, 50, 100, 250, 500]) * 1000.0),
                              rng.choice(["Net 30", "Net 45", "Net 60", "Net 15"]), round((1 - disc) * 100, 1),
                              (date(2019, 1, 1) + timedelta(days=rng.randint(0, 1400))).isoformat(), "active"))
            by_wh[wid].append((cid, seg, round((1 - disc), 3), city, rng.uniform(25, 950)))
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?)", customers)
    con.executemany("INSERT INTO carriers VALUES (?,?,?,?,?)", CARRIERS)

    # ---------------------------------------------------------------- sales orders from sale movements
    sales = con.execute("""SELECT movement_id, product_id, warehouse_id, movement_at, -quantity_change AS qty
                           FROM inventory_movements WHERE movement_type = 'sale'
                           ORDER BY warehouse_id, movement_at, movement_id""").fetchall()
    groups = defaultdict(list)
    for m in sales:
        groups[(m["warehouse_id"], m["movement_at"][:10])].append(m)

    so_rows, line_rows, ship_rows = [], [], []
    so_n = line_n = ship_n = 0
    week_orders = defaultdict(list)  # (wh, week_start) -> [so_id] for attaching backorders
    so_index = {}
    for (wid, day), ms in sorted(groups.items()):
        rng.shuffle(ms)
        i = 0
        while i < len(ms):
            k = rng.choice([1, 1, 2, 2, 3, 4])
            chunk = ms[i:i + k]
            i += k
            so_n += 1
            so_id = f"SO{so_n:06d}"
            cid, seg, price_factor, city, dist = rng.choice(by_wh[wid])
            ship_d = d(day)
            order_d = ship_d - timedelta(days=rng.choice([0, 1, 1, 2, 2, 3, 4]))
            req_d = order_d + timedelta(days=rng.randint(3, 8))
            carrier = CARRIERS[rng.randrange(len(CARRIERS))] if warehouses[wid]["region"] != "Canada" else CARRIERS[3]
            planned_transit = max(1, math.ceil(dist / 550))
            # Northstar Logistics (CR03) degrades from October 2025 - a real pattern for the insight engine to find.
            on_time_p = carrier[3] - (0.22 if carrier[0] == "CR03" and ship_d >= date(2025, 10, 1) else 0) \
                - (0.05 if ship_d.month in (11, 12) else 0)
            late = 0 if rng.random() < on_time_p else rng.choice([1, 1, 2, 3, 4])
            deliv = ship_d + timedelta(days=planned_transit + late)
            promised = ship_d + timedelta(days=planned_transit)
            value = 0.0
            units = 0
            for m in chunk:
                line_n += 1
                price = round(products[m["product_id"]]["unit_price"] * price_factor, 2)
                value += price * m["qty"]
                units += m["qty"]
                line_rows.append((f"SOL{line_n:07d}", so_id, m["product_id"], m["qty"], m["qty"], m["qty"], 0, None,
                                  price, m["movement_id"]))
            delivered = deliv <= TODAY
            status = "delivered" if delivered else "shipped"
            so_rows.append((so_id, cid, wid, CHANNELS[seg], order_d.isoformat(), req_d.isoformat(), promised.isoformat(),
                            ship_d.isoformat(), deliv.isoformat() if delivered else None, status, round(value, 2), "derived",
                            None, None))
            so_index[so_id] = (cid, wid, deliv if delivered else None, city)
            week_orders[(wid, (ship_d - timedelta(days=ship_d.weekday())).isoformat())].append(so_id)
            ship_n += 1
            mode = carrier[2] if units >= 40 or carrier[2] == "Parcel" else "LTL"
            freight = round(dist * units * carrier[4] / 100 + 45, 2)
            ship_rows.append((f"SH{ship_n:07d}", "outbound", so_id, None, carrier[0], mode, wid, warehouses[wid]["warehouse_name"],
                              cid, city, round(dist, 1), units, ship_d.isoformat(), promised.isoformat(),
                              deliv.isoformat() if delivered else None, "delivered" if delivered else "in_transit",
                              freight, f"{carrier[0]}-{rng.randint(10**9, 10**10 - 1)}"))

    # backorders: unfulfilled weekly demand becomes a backordered line on an order of the same warehouse and week
    backorders = con.execute("""SELECT product_id, warehouse_id, week_start, unfulfilled_units FROM product_demand_weekly
                                WHERE unfulfilled_units > 0 ORDER BY week_start, warehouse_id, product_id""").fetchall()
    bo_attached = 0
    for b in backorders:
        cands = week_orders.get((b["warehouse_id"], b["week_start"]))
        if not cands:
            continue
        so_id = cands[rng.randrange(len(cands))]
        line_n += 1
        price = round(products[b["product_id"]]["unit_price"], 2)
        line_rows.append((f"SOL{line_n:07d}", so_id, b["product_id"], b["unfulfilled_units"], 0, 0, b["unfulfilled_units"],
                          "cancelled" if d(b["week_start"]) < TODAY - timedelta(days=28) else "open", price, None))
        bo_attached += b["unfulfilled_units"]

    con.executemany("INSERT INTO sales_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", so_rows)
    con.executemany("INSERT INTO sales_order_lines VALUES (?,?,?,?,?,?,?,?,?,?)", line_rows)

    # ---------------------------------------------------------------- inbound shipments from purchase orders
    suppliers = {r["supplier_id"]: dict(r) for r in con.execute("SELECT * FROM suppliers")}
    pos = con.execute("""SELECT o.*, (SELECT SUM(quantity_ordered) FROM purchase_order_lines l
                                     WHERE l.purchase_order_id = o.purchase_order_id) AS units
                         FROM purchase_orders o ORDER BY purchase_order_id""").fetchall()
    for po in pos:
        if po["status"] == "cancelled":
            continue
        sup = suppliers[po["supplier_id"]]
        domestic = sup["country_code"] in ("US", "CA", "MX")
        transit = rng.randint(3, 7) if domestic else rng.randint(18, 35)
        dist = rng.uniform(300, 2500) if domestic else rng.uniform(6000, 12000)
        exp = d(po["expected_at"])
        if po["received_at"]:
            arrived = d(po["received_at"])
            ship_d = arrived - timedelta(days=transit)
            status, deliv = "delivered", arrived.isoformat()
        else:
            ship_d = exp - timedelta(days=transit)
            if ship_d > TODAY:
                status = "booked"
            else:
                status = "in_transit"
            deliv = None
        carrier = CARRIERS[4] if not domestic else CARRIERS[rng.choice([0, 2])]
        ship_n += 1
        ship_rows_in = (f"SH{ship_n:07d}", "inbound", None, po["purchase_order_id"], carrier[0],
                        "Ocean + FTL" if not domestic else carrier[2], po["supplier_id"], sup["supplier_name"],
                        po["warehouse_id"], warehouses[po["warehouse_id"]]["warehouse_name"], round(dist, 1),
                        int(po["units"] or 0), ship_d.isoformat(), exp.isoformat(), deliv, status,
                        round(dist * (po["units"] or 0) * 0.004 + (1800 if not domestic else 250), 2),
                        f"{carrier[0]}-{rng.randint(10**9, 10**10 - 1)}")
        ship_rows.append(ship_rows_in)
    con.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ship_rows)

    # ---------------------------------------------------------------- returns
    ret_rows = []
    ret_n = 0
    risky_brand = "Servo Max"  # returns cluster on one brand family in H2 2025 - a pattern for the insight engine
    for (sol_id, so_id, pid, qo, qa, qs, qb, bs, price, mid) in line_rows:
        if qs <= 0:
            continue
        cid, wid, deliv, _city = so_index[so_id]
        if deliv is None:
            continue
        p = products[pid]
        rate = RETURN_RATE.get(p["category"], 0.012)
        if p["brand_family"] == risky_brand and deliv >= date(2025, 7, 1):
            rate *= 4
        if rng.random() >= rate:
            continue
        req = deliv + timedelta(days=rng.randint(2, 25))
        if req > TODAY:
            continue
        reason, disposition = RETURN_REASONS[rng.choices(range(5), weights=[3, 4, 2, 2, 2] if p["brand_family"] != risky_brand else [1, 8, 1, 1, 3])[0]]
        qty = min(qs, rng.choice([1, 1, 1, 2, 3]))
        received = req + timedelta(days=rng.randint(3, 9))
        closed = received + timedelta(days=rng.randint(1, 6))
        if closed <= TODAY:
            status = "closed"
        elif received <= TODAY:
            status, closed = "received", None
        else:
            status, received, closed = "authorized", None, None
        ret_n += 1
        ret_rows.append((f"RMA{ret_n:06d}", so_id, sol_id, cid, pid, wid, qty, reason, req.isoformat(),
                         received.isoformat() if received else None, closed.isoformat() if closed else None,
                         disposition, status, round(qty * price, 2), None, None, "derived"))
    con.executemany("INSERT INTO returns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ret_rows)

    # ---------------------------------------------------------------- stock policies
    demand = defaultdict(float)
    horizon = (TODAY - timedelta(days=182)).isoformat()
    for r in con.execute("""SELECT product_id, warehouse_id, SUM(actual_demand_units) AS u FROM product_demand_weekly
                            WHERE week_start >= ? GROUP BY 1, 2""", (horizon,)):
        demand[(r["product_id"], r["warehouse_id"])] = r["u"] / 26.0
    pol = []
    for r in con.execute("SELECT DISTINCT product_id, warehouse_id FROM inventory_opening_balances"):
        p = products[r["product_id"]]
        wk = demand.get((r["product_id"], r["warehouse_id"]), 0.0)
        mn = int(p["reorder_point"])
        mx = int(max(mn * 3, mn + wk * 26))
        pol.append((r["product_id"], r["warehouse_id"], mn, mx, round(wk, 3)))
    con.executemany("INSERT INTO stock_policies VALUES (?,?,?,?,?)", pol)

    con.executescript(INDEXES)
    con.executescript(VIEWS)
    # materialised stock, kept current by every movement the application posts (app.db.post_movement)
    con.execute("""CREATE TABLE stock_levels AS SELECT product_id, warehouse_id, on_hand, 0 AS allocated FROM stock_on_hand""")
    con.execute("CREATE UNIQUE INDEX ix_sl ON stock_levels(product_id, warehouse_id)")
    con.commit()

    # ---------------------------------------------------------------- manifest
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in
              con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}
    sold = con.execute("SELECT SUM(qty_shipped), SUM(qty_backordered) FROM sales_order_lines").fetchone()
    fulfilled = con.execute("SELECT SUM(fulfilled_units), SUM(unfulfilled_units) FROM product_demand_weekly").fetchone()
    con.close()
    tmp.replace(dst)
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "business_today": settings.today,
        "seed": SEED,
        "source": {"path": str(src), "sha256": src_hash, "tables": source_tables},
        "counts": counts,
        "reconciliation": {
            "units_shipped_on_sales_orders": sold[0], "dataset_fulfilled_units": fulfilled[0],
            "units_backordered_on_sales_orders": sold[1], "dataset_unfulfilled_units": fulfilled[1],
            "backorder_units_attached": bo_attached,
        },
        "lineage": {
            "customers": "generated, 22 per warehouse, fixed seed",
            "sales_orders / sales_order_lines": "one line per 'sale' inventory movement (movement_id kept); unfulfilled weekly demand as backordered lines",
            "shipments": "outbound: one per sales order; inbound: one per non-cancelled purchase order (received_at = delivery)",
            "returns": "category-dependent share of delivered lines",
            "stock_policies": "min = products.reorder_point; max = max(3 x min, min + 26 weeks of recent demand)",
            "carriers": "5 carriers",
            "all other tables": "byte-identical copy of the source dataset",
        },
        "seconds": round(time.time() - t0, 1),
    }
    settings.manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    m = build(ap.parse_args().force)
    print(json.dumps({k: m[k] for k in ("business_today", "reconciliation", "seconds")}, indent=2))
    print({k: v for k, v in m["counts"].items() if k in ("customers", "sales_orders", "sales_order_lines", "shipments", "returns")})
