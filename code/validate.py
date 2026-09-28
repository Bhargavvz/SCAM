"""validate.py - fail-loud validation of the extended dataset.

Checks are grouped by stage; `run(ctx, stages)` runs every check whose inputs
exist, prints PASS/FAIL lines with the measured values and raises
SystemExit(1) if any check fails. A JSON report is written to
<output>/validation_report.json.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

from schema import SCHEMA
from sc_common import SOURCE_PK, SOURCE_TABLES, Ctx, banner, dump_json, load_source_raw, log, source_fingerprint, table_content_hash

TOL = 0.0015


class Report:
    def __init__(self):
        self.rows: list[dict] = []

    def check(self, stage: str, name: str, ok: bool, detail: str = "", metrics: dict | None = None):
        self.rows.append({"stage": stage, "check": name, "ok": bool(ok), "detail": detail, "metrics": metrics or {}})
        print(f"  [{'PASS' if ok else 'FAIL'}] {stage:6s} {name:58s} {detail}", flush=True)

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]


def _load(ctx: Ctx, name: str) -> pd.DataFrame | None:
    p = ctx.out / "tables" / f"{name}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, keep_default_na=True, low_memory=False)
    for col, typ, *_ in SCHEMA[name]["columns"]:
        if typ == "date" and col in df:
            df[col] = pd.to_datetime(df[col])
    return df


# =============================================================================
# generic
# =============================================================================
def check_keys(ctx: Ctx, rep: Report, T: dict):
    src = ctx.src
    for name, spec in SCHEMA.items():
        df = T.get(name)
        if df is None:
            continue
        pk = spec["pk"]
        dup = int(df.duplicated(pk).sum())
        nulls = int(df[pk].isna().any(axis=1).sum())
        rep.check("keys", f"PK unique/non-null: {name}", dup == 0 and nulls == 0, f"rows={len(df):,} dup={dup} null={nulls}")
        for col, ref_t, ref_c in spec["fks"]:
            ref = T.get(ref_t) if ref_t in SCHEMA else src.get(ref_t)
            if ref is None:
                continue
            vals = df[col].dropna()
            if vals.empty:
                continue
            miss = ~vals.isin(set(ref[ref_c].dropna()))
            rep.check("keys", f"FK {name}.{col} -> {ref_t}.{ref_c}", int(miss.sum()) == 0, f"missing={int(miss.sum())}")
        for col, typ, desc, nullable, key in spec["columns"]:
            if not nullable and col in df and df[col].isna().any():
                rep.check("keys", f"NOT NULL {name}.{col}", False, f"nulls={int(df[col].isna().sum())}")


def check_source_unchanged(ctx: Ctx, rep: Report):
    raw_now, prov_now = load_source_raw(ctx.input)
    fp_now = source_fingerprint(raw_now)
    for t in SOURCE_TABLES:
        a, b = ctx.fingerprint[t], fp_now[t]
        rep.check("source", f"input table unchanged: {t}", a == b, f"rows={b['rows']:,} sha={b['sha256'][:12]}")
    for t, f in ctx.prov.get("files", {}).items():
        rep.check("source", f"input file bytes unchanged: {t}", f["sha256"] == prov_now["files"][t]["sha256"], f["sha256"][:12])
    db = ctx.out / ctx.cfg["output"]["sqlite_name"]
    if db.exists():
        with sqlite3.connect(db) as con:
            for t in SOURCE_TABLES:
                df = pd.read_sql_query(f"SELECT * FROM {t}", con).astype(str).replace({"None": ""})
                h = table_content_hash(df, SOURCE_PK[t])
                rep.check("source", f"SQLite copy identical: {t}", h == ctx.fingerprint[t]["sha256"] and len(df) == ctx.fingerprint[t]["rows"], f"rows={len(df):,}")


# =============================================================================
# stage 1
# =============================================================================
def daily_rm_stock(T: dict) -> tuple[pd.DataFrame, pd.Timestamp]:
    ob, mv = T["rm_inventory_opening_balances"], T["rm_inventory_movements"]
    t0 = ob.balance_date.min()
    return ob, t0


def check_stage1(ctx: Ctx, rep: Report, T: dict):
    src = ctx.src
    need = ["bill_of_materials", "raw_materials", "products_ext", "production_runs", "rm_inventory_movements", "rm_inventory_snapshots_weekly", "rm_inventory_opening_balances"]
    if any(T.get(n) is None for n in need):
        return
    bom, rms, pe, runs = T["bill_of_materials"], T["raw_materials"], T["products_ext"], T["production_runs"]
    mv, snap, ob = T["rm_inventory_movements"], T["rm_inventory_snapshots_weekly"], T["rm_inventory_opening_balances"]
    make = pe[pe.sourcing_mode == "make"]

    # --- BOM structure
    b1 = bom[bom.bom_version == 1]
    size = b1.groupby("product_id").size()
    cov = b1.groupby("rm_id").product_id.nunique() / len(make)
    k = max(1, int(round(0.10 * len(rms))))
    topmin = float(cov.sort_values(ascending=False).head(k).min())
    rep.check("S1", "BOM: 4-12 RMs per make product", size.between(4, 12).all() and set(size.index) == set(make.product_id), f"min={size.min()} mean={size.mean():.2f} max={size.max()}")
    rep.check("S1", "BOM commonality: top 10% RMs each in >30% of make products", topmin > 0.30, f"top{k} min coverage={topmin:.1%}")
    chg = bom[bom.bom_version > 1].product_id.nunique() / len(make)
    rep.check("S1", "BOM versioned changes ~5% of make products", 0.03 <= chg <= 0.08, f"{chg:.1%}")
    ov = 0
    for pid, g in bom.groupby("product_id"):
        v = g.groupby("bom_version").agg(f=("effective_from", "min"), t=("effective_to", "max")).sort_index()
        if len(v) > 1 and (v.f.values[1:] <= v.t.values[:-1]).any():
            ov += 1
    rep.check("S1", "BOM versions do not overlap", ov == 0, f"overlapping products={ov}")
    ss_share = float(rms.is_single_source.mean())
    rep.check("S1", "single-source RMs ~15%", 0.10 <= ss_share <= 0.20, f"{ss_share:.1%}")

    # --- production runs vs FG lines
    pol = src["purchase_order_lines"].set_index("purchase_order_line_id")
    po = src["purchase_orders"].set_index("purchase_order_id")
    r = runs.join(pol[["purchase_order_id", "quantity_ordered", "quantity_received"]], on="purchase_order_line_id")
    r = r.join(po[["expected_at", "received_at", "status"]], on="purchase_order_id")
    ok_q = (r.produced_qty == r.quantity_received).all() and (r.planned_qty == r.quantity_ordered).all()
    rep.check("S1", "production_runs: produced=qty_received, planned=qty_ordered", ok_q, f"runs={len(r):,}")
    ex = r[r.actual_start.notna()]
    rep.check("S1", "production_runs: actual_start precedes FG received_at", (ex.actual_start < ex.received_at).all(), f"executed={len(ex):,}")
    rep.check("S1", "one run per make-product FG line (non-cancelled)", runs.purchase_order_line_id.is_unique, "")

    # --- consumption = produced x BOM x (1+scrap)
    cons = mv[mv.movement_type == "consumption"]
    b = bom[["product_id", "rm_id", "qty_per_unit", "scrap_pct", "effective_from", "effective_to"]]
    m = ex[["production_run_id", "product_id", "actual_start", "produced_qty"]].merge(b, on="product_id")
    m = m[(m.actual_start >= m.effective_from) & (m.actual_start <= m.effective_to.fillna(pd.Timestamp("2262-01-01")))]
    m["exp"] = m.produced_qty * m.qty_per_unit * (1 + m.scrap_pct / 100)
    j = m.merge(cons[["production_run_id", "rm_id", "quantity_change", "movement_at"]], on=["production_run_id", "rm_id"], how="outer", indicator=True)
    both = j[j._merge == "both"]
    err = (both.exp + both.quantity_change).abs().max()
    rep.check("S1", "RM consumption = produced x BOM x (1+scrap%)", (j._merge == "both").all() and err <= TOL and (both.movement_at == both.actual_start).all(),
              f"pairs={len(both):,} unmatched={int((j._merge != 'both').sum())} max_abs_err={err:.4f}")

    # --- stock identity + non-negativity (daily and weekly)
    t0 = ob.balance_date.min()
    end = pd.Timestamp(ctx.calib["window_end"])
    ndays = (end - t0).days + 1
    mv = mv.assign(day=(mv.movement_at - t0).dt.days)
    opening = ob.set_index(["rm_id", "plant_id"]).opening_units
    daily = mv.groupby(["rm_id", "plant_id", "day"]).quantity_change.sum()
    snap = snap.assign(day=(snap.snapshot_date - t0).dt.days)
    max_err, neg_unflagged, min_daily = 0.0, 0, np.inf
    stock = {}
    for key, op in opening.items():
        arr = np.zeros(ndays)
        try:
            d = daily.loc[key]
            arr[d.index.values.astype(int)] = d.values
        except KeyError:
            pass
        S = op + np.cumsum(arr)
        stock[key] = S
        min_daily = min(min_daily, float(S.min()))
    sk = snap.set_index(["rm_id", "plant_id"])
    for key, g in snap.groupby(["rm_id", "plant_id"]):
        S = stock[key]
        e = np.abs(S[g.day.values] - g.on_hand_units.values).max()
        max_err = max(max_err, float(e))
        neg_unflagged += int(((g.on_hand_units < -TOL) & (g.stockout_flag == 0)).sum())
    rep.check("S1", "RM identity: opening + cumulative movements = weekly on-hand", max_err <= 0.01, f"snapshots={len(snap):,} max_abs_err={max_err:.4f}")
    rep.check("S1", "no negative RM stock unless stockout_flag=1", neg_unflagged == 0, f"negative unflagged={neg_unflagged}; min daily stock={min_daily:.3f}")

    # --- cascades: strict causal order + stock conditions
    try:
        rmo = ctx.load_internal("rm_ops")
    except FileNotFoundError:
        return
    cas = rmo["cascades"]
    rev = T["rm_po_revisions"]
    rpo = T["rm_purchase_orders"].set_index("rm_purchase_order_id")
    rr = runs.set_index("production_run_id")
    ss_snap = snap.set_index(["rm_id", "plant_id", "snapshot_date"]).safety_stock_units
    bad = defaultdict(int)
    for c in cas:
        run = rr.loc[c["production_run_id"]]
        fg = r.set_index("production_run_id").loc[c["production_run_id"]]
        revs = rev[rev.rm_po_id.isin({s["rm_purchase_order_id"] for s in c["slipped"]})]
        t1 = revs.revised_at.min()
        b_ = c["t_below_ss_date"]
        P, A = run.planned_start, run.actual_start
        if pd.isna(t1) or not (t1 < b_ <= P < A < fg.received_at):
            bad["order"] += 1
        if not (fg.received_at > fg.expected_at or fg.status == "partial"):
            bad["fg_not_late_or_short"] += 1
        if run.delay_reason_code != "rm_shortage" or run.rm_shortage_rm_id != c["rm_id"]:
            bad["reason"] += 1
        S = stock[(c["rm_id"], c["plant_id"])]
        pd_, xd = int((P - t0).days), int((c["X_date"] - t0).days)
        if (S[pd_:xd] >= c["req"] - TOL).any():
            bad["could_start_earlier"] += 1
        # below safety stock on b (daily stock vs the SS level in force that week)
        wk_sun = b_ + pd.Timedelta(days=(6 - b_.dayofweek))
        ssv = ss_snap.get((c["rm_id"], c["plant_id"], wk_sun), np.nan)
        if not (S[int((b_ - t0).days)] < ssv + TOL):
            bad["not_below_ss"] += 1
        for s_ in c["slipped"]:
            if rpo.loc[s_["rm_purchase_order_id"]].status != "cancelled" and s_["rm_purchase_order_id"] not in set(revs.rm_po_id):
                bad["missing_revision"] += 1
    rep.check("S1", "cascade chain strictly ordered: revision < below-SS < planned start < actual start < FG receipt", sum(bad.values()) == 0, f"cascades={len(cas):,} problems={dict(bad)}")
    elig = ex[((ex.delay_days >= ctx.cfg["rm_ops"]["cascade_min_prod_delay"]) | ((ex.status == "partial") & (ex.delay_days >= 1)))]
    share = len(cas) / max(1, len(elig))
    rep.check("S1", "cascade back-fill covers 10-15% of eligible late/short make lines", 0.10 <= share <= 0.15, f"{len(cas):,}/{len(elig):,} = {share:.1%}")

    # --- RM PO consistency
    rpol = T["rm_purchase_order_lines"]
    h = T["rm_purchase_orders"]
    agg = rpol.groupby("rm_purchase_order_id").agg(o=("quantity_ordered", "sum"), rcv=("quantity_received", "sum"),
                                                   short=("quantity_received", lambda s: 0))
    short_lines = (rpol.quantity_received < rpol.quantity_ordered - TOL).groupby(rpol.rm_purchase_order_id).any()
    hh = h.set_index("rm_purchase_order_id").join(short_lines.rename("has_short"))
    probs = {
        "received_without_date": int(((hh.status.isin(["received", "partial"])) & hh.received_at.isna()).sum()),
        "received_but_short": int(((hh.status == "received") & hh.has_short).sum()),
        "partial_not_short": int(((hh.status == "partial") & ~hh.has_short).sum()),
        "open_or_cancelled_with_date": int(((hh.status.isin(["open", "cancelled"])) & hh.received_at.notna()).sum()),
    }
    rep.check("S1", "RM PO status consistent with lines/dates", sum(probs.values()) == 0, str(probs))
    # revision chains end at expected_at
    last = rev.sort_values(["rm_po_id", "revised_at", "revision_id"]).groupby("rm_po_id").new_expected_at.last()
    mism = int((last != hh.loc[last.index, "expected_at"]).sum())
    rep.check("S1", "last revision new_expected_at = header expected_at", mism == 0, f"mismatch={mism}")
    # catalog: allocation of qualified suppliers sums to 100 on sample dates
    cat = T["rm_supplier_catalog"]
    badalloc = 0
    for d in pd.date_range(ctx.calib["window_start"], ctx.calib["window_end"], freq="90D"):
        a = cat[(cat.price_valid_from <= d) & (cat.price_valid_to >= d) & (cat.qualified_flag == 1)].groupby("rm_id").allocation_pct.sum()
        badalloc += int((a - 100).abs().gt(0.5).sum())
    rep.check("S1", "catalog allocations of qualified suppliers sum to 100%", badalloc == 0, f"violations={badalloc}")
    sup = src["suppliers"].set_index("supplier_id").lead_time_days
    rmh = rms.set_index("rm_id").is_hazmat
    exp_lt = cat.supplier_id.map(sup) + cat.rm_id.map(rmh) * ctx.cfg["raw_materials"]["hazmat_extra_lead_days"]
    rep.check("S1", "catalog lead times = supplier lead_time_days (+hazmat)", (exp_lt == cat.lead_time_days).all(), "")
    ctx.internal["_stock"] = (stock, t0)


# =============================================================================
def run(ctx: Ctx, stages: list[str] | None = None) -> Report:
    banner("VALIDATION")
    rep = Report()
    T = {n: _load(ctx, n) for n in SCHEMA}
    check_keys(ctx, rep, T)
    check_source_unchanged(ctx, rep)
    check_stage1(ctx, rep, T)
    for fn in EXTRA_CHECKS:
        fn(ctx, rep, T)
    # summary
    print("\nROW COUNTS (new tables)")
    for n, df in T.items():
        if df is not None:
            print(f"  {n:34s} {len(df):>10,d}")
    dump_json({"checks": rep.rows, "failed": len(rep.failed)}, ctx.out / "validation_report.json")
    if rep.failed:
        print(f"\nVALIDATION FAILED: {len(rep.failed)} check(s)")
        for f in rep.failed:
            print(f"   - {f['stage']} {f['check']}: {f['detail']}")
        raise SystemExit(1)
    print(f"\nVALIDATION PASSED ({len(rep.rows)} checks)")
    return rep


EXTRA_CHECKS: list = []


# =============================================================================
# stage 2
# =============================================================================
def check_stage2(ctx: Ctx, rep: Report, T: dict):
    d = T.get("product_demand_weekly")
    if d is None:
        return
    from sc_common import week_start
    mv = ctx.src["inventory_movements"]
    s = mv[mv.movement_type == "sale"].copy()
    s["week_start"] = week_start(s.movement_at).values
    f = s.groupby(["product_id", "warehouse_id", "week_start"]).quantity_change.sum().mul(-1)
    dd = d.set_index(["product_id", "warehouse_id", "week_start"])
    j = pd.concat([f.rename("src"), dd.fulfilled_units], axis=1).fillna(0)
    bad = int((j.src != j.fulfilled_units).sum())
    rep.check("S2", "fulfilled_units == -SUM(existing sales) per product/warehouse/week", bad == 0 and int(d.fulfilled_units.sum()) == int(-s.quantity_change.sum()),
              f"mismatched keys={bad}; total fulfilled={int(d.fulfilled_units.sum()):,}")
    rep.check("S2", "actual = fulfilled + unfulfilled and actual >= fulfilled", ((d.actual_demand_units == d.fulfilled_units + d.unfulfilled_units) & (d.unfulfilled_units >= 0)).all(), "")
    led = ctx.load_internal("fg_ledger")
    led = led.assign(week_start=week_start(led.movement_at).values)
    out = set(map(tuple, led[led.balance <= 0][["product_id", "warehouse_id", "week_start"]].drop_duplicates().values))
    u = d[d.unfulfilled_units > 0]
    expl = u.demand_shock_event_id.notna() | pd.Series([tuple(x) in out for x in u[["product_id", "warehouse_id", "week_start"]].values], index=u.index)
    rep.check("S2", "unfulfilled > 0 only where stock <= 0 or a shock event explains it", bool(expl.all()), f"unfulfilled rows={len(u):,}, unexplained={int((~expl).sum())}")
    a = d[d.actual_demand_units > 0]
    ape = (a.forecast_units - a.actual_demand_units).abs() / a.actual_demand_units
    normal = ape[(a.promo_flag == 0) & a.demand_shock_event_id.isna()].mean()
    rep.check("S2", "SKU-location-week MAPE within 20-40%", 0.20 <= ape.mean() <= 0.40, f"MAPE={ape.mean():.1%}")
    rep.check("S2", "forecast error worse in promo and shock weeks", ape[a.promo_flag == 1].mean() > normal and ape[a.demand_shock_event_id.notna()].mean() > normal,
              f"normal={normal:.1%} promo={ape[a.promo_flag == 1].mean():.1%} shock={ape[a.demand_shock_event_id.notna()].mean():.1%}")
    rep.check("S2", "forecast frozen before the week starts", (d.forecast_made_at < d.week_start).all(), "")
    ev = T.get("disruption_events")
    if ev is None:
        prov = d.demand_shock_event_id.dropna().str.startswith("SHK").all()
        rep.check("S2", "shock ids provisional until Stage 3", bool(prov), "")


EXTRA_CHECKS.append(check_stage2)


# =============================================================================
# stage 3
# =============================================================================
def id_universe(ctx: Ctx, T: dict) -> set:
    s = ctx.src
    ids = set()
    for t, cols in {"purchase_orders": ["purchase_order_id"], "purchase_order_lines": ["purchase_order_line_id"], "inventory_movements": ["movement_id", "transfer_id"],
                    "products": ["product_id"], "warehouses": ["warehouse_id"], "suppliers": ["supplier_id"]}.items():
        for c in cols:
            ids |= set(s[t][c].dropna().astype(str))
    for t, cols in {"rm_purchase_orders": ["rm_purchase_order_id"], "rm_purchase_order_lines": ["rm_purchase_order_line_id"], "rm_po_revisions": ["revision_id"],
                    "rm_inventory_movements": ["rm_movement_id", "transfer_id"], "production_runs": ["production_run_id"], "rm_supplier_catalog": ["catalog_id"],
                    "bill_of_materials": ["bom_id"], "negotiations": ["negotiation_id"], "contracts": ["contract_id"], "raw_materials": ["rm_id"], "plants": ["plant_id"]}.items():
        if T.get(t) is not None:
            for c in cols:
                ids |= set(T[t][c].dropna().astype(str))
    sn = T.get("rm_inventory_snapshots_weekly")
    if sn is not None:
        ids |= set(sn.rm_id + "|" + sn.plant_id + "|" + sn.snapshot_date.dt.strftime("%Y-%m-%d"))
    return ids


def check_stage3(ctx: Ctx, rep: Report, T: dict):
    ev, dc, cm = T.get("disruption_events"), T.get("decisions"), T.get("commitments")
    if ev is None or dc is None or cm is None:
        return
    U = id_universe(ctx, T)
    src_ids = set()
    for t, c in (("purchase_orders", "purchase_order_id"), ("purchase_order_lines", "purchase_order_line_id"), ("inventory_movements", "movement_id")):
        src_ids |= set(ctx.src[t][c].astype(str))
    bad_fp = [(r.decision_id, x) for r in dc.itertuples() for x in json.loads(r.footprint_ids) if x not in U]
    rep.check("S3", "every decision footprint id resolves to a real row", not bad_fp and dc.footprint_ids.map(lambda s: len(json.loads(s)) > 0).all(), f"decisions={len(dc)} unresolved={bad_fp[:3]}")
    bad_an = [(r.event_id, x) for r in ev.itertuples() for x in json.loads(r.anchor_ids) if x not in U]
    ex = ev[ev.anchor_type == "existing_data"]
    ex_ok = ex.anchor_ids.map(lambda s: any(x in src_ids for x in json.loads(s)))
    rep.check("S3", "event anchor ids resolve; existing_data events anchor on v1.0.0 rows", not bad_an and bool(ex_ok.all()),
              f"events={len(ev):,} ({ev.anchor_type.value_counts().to_dict()}) unresolved={bad_an[:3]} existing-without-source-anchor={int((~ex_ok).sum())}")
    fp_src = dc.footprint_ids.map(lambda s: any(x in src_ids for x in json.loads(s)))
    rep.check("S3", "FG-side decisions leave footprints in existing rows", int(fp_src.sum()) > 0, f"decisions with v1.0.0 footprint={int(fp_src.sum())}")
    rep.check("S3", "event dates ordered (start <= end) and severity 1-5", bool(((ev.start_date <= ev.end_date) | ev.end_date.isna()).all() and ev.severity.between(1, 5).all()), "")
    j = dc.merge(ev[["event_id", "detected_at"]], on="event_id")
    rep.check("S3", "decisions taken on/after their event was detected", bool((j.decided_at >= j.detected_at).all()), f"violations={int((j.decided_at < j.detected_at).sum())}")
    assessed = dc[dc.outcome_label.notna()]
    nulls_ok = dc[dc.outcome_label.isna()][["actual_cost", "actual_stockout_days", "outcome_assessed_at"]].isna().all().all()
    rep.check("S3", "actual outcome fields present iff the outcome was assessable by the cutoff", bool(nulls_ok and assessed[["actual_cost", "actual_stockout_days", "outcome_assessed_at"]].notna().all().all()), "")
    cdep = float(assessed.outcome_attribution.isin(["context_shift", "bad_luck"]).mean())
    rep.check("S3", ">=25% of decisions context-dependent (context shift or bad luck)", cdep >= 0.25,
              f"{cdep:.1%} of {len(assessed)} assessed; {assessed.outcome_attribution.value_counts().to_dict()}; outcomes {assessed.outcome_label.value_counts().to_dict()}")
    stt = cm.status.value_counts(normalize=True)
    rep.check("S3", "commitments: >=15% breached, ~10% renegotiated, some open at 2025-12-31",
              stt.get("breached", 0) >= 0.15 and 0.07 <= stt.get("renegotiated", 0) <= 0.15 and stt.get("open", 0) > 0, str(stt.round(3).to_dict()))
    rep.check("S3", "open commitments unresolved; resolved ones have resolved_at", bool(cm[cm.status == "open"].resolved_at.isna().all() and cm[cm.status != "open"].resolved_at.notna().all()), "")
    lk = T["event_links"]
    rep.check("S3", "event links: no self-links, all 5 link types used", bool((lk.src_event_id != lk.dst_event_id).all()) and set(lk.link_type) == {"caused", "similar_to", "recurrence_of", "mitigated_by", "superseded_by"},
              str(lk.link_type.value_counts().to_dict()))
    # scorecard recomputation
    from gen_events import compute_scorecard
    sc = T["supplier_scorecard_monthly"]
    re_ = compute_scorecard(ctx.src, T["rm_purchase_orders"], T["rm_purchase_order_lines"], T["rm_inventory_movements"], cm, pd.Timestamp(ctx.calib["window_end"]))
    a = sc.set_index(["supplier_id", "month"]).sort_index()
    b = re_.set_index(["supplier_id", "month"]).sort_index()
    same_keys = a.index.equals(b.index)
    diffs = {}
    if same_keys:
        for col in ["po_count", "otif_rate", "avg_delay_days", "fill_rate", "quality_ppm", "commitments_made", "commitments_kept"]:
            x, y = a[col].astype(float), b[col].astype(float)
            diffs[col] = int((~(np.isclose(x, y, atol=1e-3, rtol=1e-6) | (x.isna() & y.isna()))).sum())
    rep.check("S3", "supplier scorecards match recomputation from raw PO / receipt / commitment rows", same_keys and sum(diffs.values()) == 0, f"rows={len(sc):,} diffs={diffs}")
    # planted patterns
    import patterns as pt
    pp = json.loads((ctx.out / "planted_patterns.json").read_text())
    T2 = dict(T)
    meas = pt.measure(pp["plan"], pp["extra"], T2, ctx.src, ctx.calib)
    ok = [m for m in meas if m["recoverable"]]
    rep.check("S3", "planted patterns statistically recoverable from tables (>=8)", len(ok) >= 8, f"{len(ok)}/{len(meas)} recoverable")
    for m in meas:
        print(f"         {m['pattern_id']} {'recoverable' if m['recoverable'] else 'weak       '} p={m['p_value']} effect={m['effect']}")


EXTRA_CHECKS.append(check_stage3)


if __name__ == "__main__":
    import argparse
    from sc_common import build_ctx, load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--scale", default="default"); ap.add_argument("--seed", type=int); ap.add_argument("--config")
    a = ap.parse_args()
    run(build_ctx(a.input, a.output, load_config(a.config, a.scale, a.seed)))
