"""Stage 5 - validation. Fails loudly (exit 1) on any broken invariant; prints a summary.

Checks run only for tables that exist, so this can be run after every stage."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from common import SOURCE_TABLES, Ctx, banner, cli, load_source, table_fingerprint

TOL = 1e-4


class V:
    def __init__(self):
        self.fail, self.ok, self.info = [], [], []

    def check(self, cond: bool, name: str, detail: str = ""):
        (self.ok if cond else self.fail).append(f"{name}{(' - ' + detail) if detail else ''}")
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        return cond

    def note(self, msg):
        self.info.append(msg)
        print(f"  [INFO] {msg}")


def exists(ctx: Ctx, name: str) -> bool:
    return (ctx.output / "tables" / "parquet" / f"{name}.parquet").exists()


def fk(v: V, child: pd.DataFrame, col: str, parent: pd.Series, name: str, nullable=True):
    c = child[col]
    if nullable:
        c = c.dropna()
    bad = ~c.isin(set(parent.dropna()))
    v.check(not bad.any(), f"FK {name}.{col}", f"{int(bad.sum())} orphans" if bad.any() else f"{len(c):,} refs")


# ------------------------------------------------------------------ checks
def check_source_unchanged(ctx: Ctx, v: V):
    banner("original tables unchanged")
    fp = ctx.load_json("source_fingerprint.json")
    src = load_source(ctx.input)
    for t in SOURCE_TABLES:
        now = table_fingerprint(src[t])
        v.check(now["sha256"] == fp[t]["sha256"] and now["rows"] == fp[t]["rows"], f"source {t} unchanged",
                f"{now['rows']:,} rows sha256={now['sha256'][:12]}")
    db = ctx.output / "inventory-supply-chain-v1.0.0-extended.sqlite"
    if db.exists():
        copy = load_source(db)
        for t in SOURCE_TABLES:
            now = table_fingerprint(copy[t])
            v.check(now["sha256"] == fp[t]["sha256"], f"SQLite copy {t} identical to source", f"{now['rows']:,} rows")


def check_fks(ctx: Ctx, v: V):
    banner("foreign keys")
    src = ctx.src()
    P, S, Wh = src["products"], src["suppliers"], src["warehouses"]
    POL, PO = src["purchase_order_lines"], src["purchase_orders"]
    r = ctx.read
    if not exists(ctx, "plants"):
        return
    pl, pe, rm, bom, cat, ct = r("plants"), r("products_ext"), r("raw_materials"), r("bill_of_materials"), r("rm_supplier_catalog"), r("contracts")
    fk(v, pe, "product_id", P.product_id, "products_ext", nullable=False)
    fk(v, pe, "plant_id", pl.plant_id, "products_ext")
    v.check(pe.product_id.is_unique and len(pe) == len(P), "products_ext covers every product once")
    v.check(((pe.sourcing_mode == "make") == pe.plant_id.notna()).all(), "make <=> plant_id present")
    fk(v, bom, "product_id", pe[pe.sourcing_mode == "make"].product_id, "bill_of_materials", False)
    fk(v, bom, "rm_id", rm.rm_id, "bill_of_materials", False)
    fk(v, cat, "rm_id", rm.rm_id, "rm_supplier_catalog", False)
    fk(v, cat, "supplier_id", S.supplier_id, "rm_supplier_catalog", False)
    fk(v, cat, "contract_id", ct.contract_id, "rm_supplier_catalog")
    fk(v, ct, "supplier_id", S.supplier_id, "contracts", False)
    v.check(rm.substitute_group_id.dropna().map(rm.substitute_group_id.value_counts()).ge(2).all(), "substitute groups have >=2 RMs")
    if not exists(ctx, "production_runs"):
        return
    runs, ob, po, pol, rev, mv, sn = (r("production_runs"), r("rm_inventory_opening_balances"), r("rm_purchase_orders"),
                                      r("rm_purchase_order_lines"), r("rm_po_revisions"), r("rm_inventory_movements"),
                                      r("rm_inventory_snapshots_weekly"))
    fk(v, runs, "plant_id", pl.plant_id, "production_runs", False)
    fk(v, runs, "product_id", P.product_id, "production_runs", False)
    fk(v, runs, "purchase_order_line_id", POL.purchase_order_line_id, "production_runs", False)
    fk(v, runs, "rm_shortage_rm_id", rm.rm_id, "production_runs")
    fk(v, ob, "rm_id", rm.rm_id, "rm_inventory_opening_balances", False)
    fk(v, ob, "plant_id", pl.plant_id, "rm_inventory_opening_balances", False)
    fk(v, po, "supplier_id", S.supplier_id, "rm_purchase_orders", False)
    fk(v, po, "plant_id", pl.plant_id, "rm_purchase_orders", False)
    fk(v, pol, "rm_po_id", po.rm_po_id, "rm_purchase_order_lines", False)
    fk(v, pol, "rm_id", rm.rm_id, "rm_purchase_order_lines", False)
    fk(v, rev, "rm_po_id", po.rm_po_id, "rm_po_revisions", False)
    fk(v, mv, "rm_purchase_order_line_id", pol.rm_purchase_order_line_id, "rm_inventory_movements")
    fk(v, mv, "production_run_id", runs.production_run_id, "rm_inventory_movements")
    fk(v, mv, "rm_id", rm.rm_id, "rm_inventory_movements", False)
    fk(v, sn, "rm_id", rm.rm_id, "rm_inventory_snapshots_weekly", False)
    pairs_ob = set(zip(ob.rm_id, ob.plant_id))
    v.check(set(zip(mv.rm_id, mv.plant_id)) <= pairs_ob, "every RM movement pair has an opening balance")
    tr = mv[mv.movement_type == "transfer"].groupby("transfer_id").quantity_change.agg(["sum", "size"])
    v.check(((tr["size"] == 2) & (tr["sum"].abs() < TOL)).all(), "RM transfers are equal/opposite pairs", f"{len(tr)} transfers")


def daily_onhand(ob, mv):
    d = mv.groupby(["rm_id", "plant_id", "movement_at"]).quantity_change.sum().reset_index().sort_values(["rm_id", "plant_id", "movement_at"])
    d["cum"] = d.groupby(["rm_id", "plant_id"]).quantity_change.cumsum()
    d = d.merge(ob[["rm_id", "plant_id", "opening_units"]], on=["rm_id", "plant_id"])
    d["on_hand"] = (d.opening_units + d.cum).round(4)
    return d


def check_rm(ctx: Ctx, v: V):
    if not exists(ctx, "production_runs"):
        return
    banner("RM operations identities")
    src, r = ctx.src(), ctx.read
    runs, ob, mv, sn, bom = r("production_runs"), r("rm_inventory_opening_balances"), r("rm_inventory_movements"), \
        r("rm_inventory_snapshots_weekly"), r("bill_of_materials")
    POL, PO = src["purchase_order_lines"], src["purchase_orders"]
    # snapshot identity
    d = daily_onhand(ob, mv)
    s = sn.merge(d[["rm_id", "plant_id", "movement_at", "on_hand"]].rename(columns={"movement_at": "snapshot_date"}),
                 on=["rm_id", "plant_id", "snapshot_date"], how="left")
    s = s.sort_values(["rm_id", "plant_id", "snapshot_date"])
    # carry last movement on-hand forward to snapshot dates without movements
    allm = pd.concat([d[["rm_id", "plant_id", "movement_at", "on_hand"]].rename(columns={"movement_at": "dt"}),
                      sn[["rm_id", "plant_id", "snapshot_date"]].rename(columns={"snapshot_date": "dt"}).assign(on_hand=np.nan)])
    allm = allm.sort_values(["rm_id", "plant_id", "dt", "on_hand"], na_position="last")
    allm["ff"] = allm.groupby(["rm_id", "plant_id"]).on_hand.ffill()
    allm = allm.merge(ob[["rm_id", "plant_id", "opening_units"]], on=["rm_id", "plant_id"])
    allm["ff"] = allm.ff.fillna(allm.opening_units)
    exp = allm[allm.on_hand.isna()].drop_duplicates(["rm_id", "plant_id", "dt"])
    s = sn.merge(exp[["rm_id", "plant_id", "dt", "ff"]].rename(columns={"dt": "snapshot_date"}), on=["rm_id", "plant_id", "snapshot_date"])
    diff = (s.on_hand_units - s.ff).abs()
    v.check(len(s) == len(sn) and diff.max() < 1e-3, "RM identity: opening + movements = weekly snapshot on_hand",
            f"{len(s):,} snapshots, max |diff|={diff.max():.2e}")
    # negative stock only in flagged weeks
    neg = d[d.on_hand < -1e-6]
    if len(neg):
        wk = neg.movement_at + pd.to_timedelta((6 - neg.movement_at.dt.weekday) % 7, unit="D")
        flag = sn.set_index(["rm_id", "plant_id", "snapshot_date"]).stockout_flag
        f = flag.reindex(pd.MultiIndex.from_arrays([neg.rm_id, neg.plant_id, wk])).fillna(0)
        v.check((f == 1).all(), "negative RM stock only where stockout_flag=1", f"{len(neg)} negative days")
    else:
        v.check(True, "no negative RM stock", f"min on-hand {d.on_hand.min():.4f}")
    # consumption identity
    cons = mv[mv.movement_type == "consumption"]
    rr = runs.dropna(subset=["actual_start"])
    m = rr[["production_run_id", "product_id", "actual_start", "produced_qty"]].merge(bom, on="product_id")
    m = m[(m.effective_from <= m.actual_start) & (m.effective_to.isna() | (m.effective_to >= m.actual_start))]
    m["expq"] = m.produced_qty.astype(float) * m.qty_per_unit * (1 + m.scrap_pct)
    j = m.merge(cons[["production_run_id", "rm_id", "quantity_change", "movement_at", "plant_id"]], on=["production_run_id", "rm_id"], how="outer", indicator=True)
    both = j[j._merge == "both"]
    err = (both.expq + both.quantity_change).abs()
    v.check((j._merge == "both").all() and err.max() < 1e-3, "RM consumption = produced_qty x BOM x (1+scrap)",
            f"{len(both):,} lines, unmatched={int((j._merge != 'both').sum())}, max err={err.max():.2e}")
    v.check((both.movement_at == both.actual_start).all(), "consumption dated at run actual_start")
    # production runs vs existing FG
    L = POL.merge(PO, on="purchase_order_id")
    x = runs.merge(L, on="purchase_order_line_id", how="left")
    rec = x.actual_start.notna()
    v.check((x.loc[rec, "produced_qty"] == x.loc[rec, "quantity_received"]).all(), "produced_qty = existing quantity_received")
    v.check((x.loc[rec, "actual_start"] < x.loc[rec, "received_at"]).all() and (x.planned_start < x.expected_at).all(),
            "runs precede FG receipt / expected date")
    v.check((x.loc[rec, "delay_days"] == (x.loc[rec, "received_at"] - x.loc[rec, "expected_at"]).dt.days).all(),
            "run delay_days = FG receipt delay (production lead preserved)")
    pe = r("products_ext")
    mk = L[L.product_id.isin(pe[pe.sourcing_mode == "make"].product_id) & (L.quantity_received > 0)]
    v.check(set(mk.purchase_order_line_id) == set(runs[rec].purchase_order_line_id) and runs.purchase_order_line_id.is_unique,
            "every received make-product FG line has exactly one production run", f"{len(mk):,} lines")

    # cascade causal order
    plan = ctx.load_json("rm_ops_plan.json")
    eps = plan["episodes"]
    rev = r("rm_po_revisions")
    runs_i = runs.set_index("production_run_id")
    fgl = L.set_index("purchase_order_line_id")
    sim0 = pd.Timestamp(plan["sim_start"])
    dd = d.set_index(["rm_id", "plant_id", "movement_at"]).on_hand
    ssw = sn.set_index(["rm_id", "plant_id", "snapshot_date"]).safety_stock_units
    bad, checked = [], 0
    dgrp = {k: g for k, g in d.groupby(["rm_id", "plant_id"])}
    creq = cons.set_index(["production_run_id", "rm_id"]).quantity_change
    for ep in eps:
        t_first = sim0 + pd.Timedelta(days=ep["t_first"])
        R = sim0 + pd.Timedelta(days=ep["R"])
        rv = rev[rev.rm_po_id.isin(ep["slipped_po_ids"])]
        if rv.empty:
            bad.append((ep["id"], "no revision"))
            continue
        first_notice = rv.revised_at.min()
        g = dgrp[(ep["rm_id"], ep["plant_id"])]
        g = g[g.movement_at < t_first]
        oh_first = g.on_hand.iat[-1] if len(g) else np.nan
        wk = t_first + pd.Timedelta(days=(6 - t_first.weekday()) % 7)
        ss = ssw.get((ep["rm_id"], ep["plant_id"], wk), np.nan)
        # first day on-hand fell below safety after the notice
        gb = g[(g.movement_at >= first_notice)]
        below = gb[gb.on_hand < ss].movement_at.min() if len(gb) else pd.NaT
        for run_id in ep["blocked_runs"]:
            rn = runs_i.loc[run_id]
            fg = fgl.loc[rn.purchase_order_line_id]
            ok = (first_notice < t_first) and (pd.notna(below) and first_notice <= below <= t_first) and \
                 (rn.planned_start < R <= rn.actual_start) and (rn.actual_start < fg.received_at) and (fg.received_at > fg.expected_at)
            checked += 1
            if not ok:
                bad.append((ep["id"], run_id, str(first_notice.date()), str(below), str(t_first.date()), str(rn.actual_start.date())))
        if not (oh_first < ss):
            bad.append((ep["id"], "on-hand at stockout start not below safety", oh_first, ss))
    v.check(not bad, "cascade chains in strict causal order (revision < below-safety <= stockout < run start < FG receipt)",
            f"{checked:,} blocked lines in {len(eps)} episodes" + (f"; {len(bad)} bad e.g. {bad[:3]}" if bad else ""))
    lop = plan["late_or_partial_make_lines"]
    share = checked / lop
    v.check(0.10 <= share <= 0.15, "cascade back-fill covers 10-15% of late/partial make-product lines", f"{share:.1%}")
    rs = runs[runs.delay_reason_code == "rm_shortage"]
    v.check(set(rs.production_run_id) == {x for ep in eps for x in ep["blocked_runs"]}, "rm_shortage runs == cascade blocked runs")


def check_demand(ctx: Ctx, v: V):
    if not exists(ctx, "product_demand_weekly"):
        return
    banner("demand")
    src, dm = ctx.src(), ctx.read("product_demand_weekly")
    M = src["inventory_movements"]
    s = M[M.movement_type == "sale"].copy()
    s["week_start"] = (s.movement_at - pd.to_timedelta(s.movement_at.dt.weekday, unit="D")).dt.normalize()
    g = (-s.groupby(["product_id", "warehouse_id", "week_start"]).quantity_change.sum()).rename("sales").reset_index()
    j = dm.merge(g, on=["product_id", "warehouse_id", "week_start"], how="outer")
    j["sales"] = j.sales.fillna(0)
    v.check(j.fulfilled_units.notna().all() and (j.fulfilled_units == j.sales).all(),
            "fulfilled_units == -SUM(existing sales) per product/warehouse/week (exact)", f"{len(g):,} sale weeks, {len(dm):,} rows")
    v.check((dm.actual_demand_units >= dm.fulfilled_units).all(), "actual_demand >= fulfilled")
    v.check((dm.unfulfilled_units == dm.actual_demand_units - dm.fulfilled_units).all(), "unfulfilled = actual - fulfilled")
    u = dm[dm.unfulfilled_units > 0]
    expl = u.demand_shock_event_id.notna() | u.get("_stockout_week", pd.Series(False, index=u.index))
    if "_stockout_week" not in dm.columns:
        wk_oh = ctx.read("fg_stockout_weeks") if (ctx.work / "fg_stockout_weeks.parquet").exists() else pd.DataFrame(columns=["product_id", "warehouse_id", "week_start"])
        key = set(zip(wk_oh.product_id, wk_oh.warehouse_id, wk_oh.week_start))
        expl = u.demand_shock_event_id.notna() | pd.Series([k in key for k in zip(u.product_id, u.warehouse_id, u.week_start)], index=u.index)
    v.check(bool(expl.all()), "unfulfilled > 0 only at existing stock <= 0 or a logged shock", f"{len(u)} rows with unfulfilled")
    a = dm[dm.actual_demand_units > 0]
    ape = (a.forecast_units - a.actual_demand_units).abs() / a.actual_demand_units
    mape = ape.mean()
    v.check(0.20 <= mape <= 0.40, "SKU-week MAPE in 20-40% (weeks with demand)", f"{mape:.1%}")
    v.note(f"MAPE promo {ape[a.promo_flag == 1].mean():.1%} / shock {ape[a.demand_shock_event_id.notna()].mean():.1%} / normal "
           f"{ape[(a.promo_flag == 0) & a.demand_shock_event_id.isna()].mean():.1%}")
    v.check((dm.forecast_made_at < dm.week_start).all(), "forecasts made before the week")


def check_events(ctx: Ctx, v: V):
    if not exists(ctx, "disruption_events"):
        return
    banner("institutional memory")
    r, src = ctx.read, ctx.src()
    ev, imp, lk, neg, dec, cm, sc = (r("disruption_events"), r("event_impacts"), r("event_links"), r("negotiations"),
                                     r("decisions"), r("commitments"), r("supplier_scorecard_monthly"))
    fk(v, imp, "event_id", ev.event_id, "event_impacts", False)
    fk(v, lk, "src_event_id", ev.event_id, "event_links", False)
    fk(v, lk, "dst_event_id", ev.event_id, "event_links", False)
    fk(v, dec, "event_id", ev.event_id, "decisions", False)
    fk(v, cm, "decision_id", dec.decision_id, "commitments")
    fk(v, cm, "negotiation_id", neg.negotiation_id, "commitments")
    fk(v, neg, "supplier_id", src["suppliers"].supplier_id, "negotiations", False)
    fk(v, neg, "contract_id", r("contracts").contract_id, "negotiations")
    fk(v, sc, "supplier_id", src["suppliers"].supplier_id, "supplier_scorecard_monthly", False)
    v.check((ev.detected_at >= ev.start_date).all() and (ev.end_date >= ev.start_date).all(), "event dates ordered")
    v.check((dec.decided_at >= dec.event_id.map(ev.set_index("event_id").detected_at)).all(), "decisions made on/after detection")
    # footprints resolve
    universe = footprint_universe(ctx)
    fp = dec.footprint_ids.apply(json.loads)
    missing = [(d, x) for d, xs in zip(dec.decision_id, fp) for x in xs if x not in universe]
    v.check(all(len(x) > 0 for x in fp) and not missing, "every decision has a resolvable footprint",
            f"{sum(map(len, fp)):,} footprint refs" + (f"; missing {missing[:3]}" if missing else ""))
    cd = dec.context_dependent.mean() if "context_dependent" in dec else 0
    v.check(cd >= ctx.cfg["events"]["context_dependent_min"], "context-dependent decisions >= 25%", f"{cd:.1%}")
    st = cm.status.value_counts(normalize=True)
    v.check(st.get("breached", 0) >= 0.15, "commitments breached >= 15%", f"{st.get('breached', 0):.1%}")
    v.check(0.06 <= st.get("renegotiated", 0) <= 0.16, "commitments renegotiated ~10%", f"{st.get('renegotiated', 0):.1%}")
    v.check(st.get("open", 0) > 0 and (cm[cm.status == "open"].due_date.isna() | (cm[cm.status == "open"].resolved_at.isna())).all(),
            "some commitments open on 2025-12-31", f"{int((cm.status == 'open').sum())}")
    v.note(f"decision types {dec.decision_type.value_counts().to_dict()}; outcomes {dec.outcome_label.value_counts().to_dict()}")
    # scorecard recomputation
    from gen_events import compute_scorecard
    re_sc = compute_scorecard(ctx)
    j = sc.merge(re_sc, on=["supplier_id", "month"], suffixes=("", "_re"))
    cols = ["po_count", "otif_rate", "avg_delay_days", "fill_rate", "quality_ppm", "commitments_made", "commitments_kept"]
    ok = len(j) == len(sc) == len(re_sc) and all(np.allclose(j[c].astype(float).fillna(-1), j[c + "_re"].astype(float).fillna(-1), atol=1e-6) for c in cols)
    v.check(ok, "supplier scorecards match recomputation from raw PO tables", f"{len(sc):,} supplier-months")
    check_patterns(ctx, v)


def footprint_universe(ctx: Ctx) -> set:
    src, r = ctx.src(), ctx.read
    u = set(src["purchase_orders"].purchase_order_id) | set(src["purchase_order_lines"].purchase_order_line_id)
    u |= set(src["inventory_movements"].transfer_id.dropna()) | set(src["inventory_movements"].movement_id)
    for t, c in [("rm_purchase_orders", "rm_po_id"), ("rm_purchase_order_lines", "rm_purchase_order_line_id"),
                 ("rm_po_revisions", "rm_po_revision_id"), ("rm_inventory_movements", "transfer_id"),
                 ("rm_inventory_movements", "rm_movement_id"), ("production_runs", "production_run_id"),
                 ("bill_of_materials", "bom_id"), ("rm_supplier_catalog", "catalog_line_id"),
                 ("negotiations", "negotiation_id"), ("contracts", "contract_id")]:
        if exists(ctx, t):
            u |= set(r(t)[c].dropna())
    return u


def check_patterns(ctx: Ctx, v: V):
    banner("planted patterns - statistical recoverability")
    p = ctx.output / "planted_patterns.json"
    if not p.exists():
        return
    pats = json.loads(p.read_text(encoding="utf-8"))
    from gen_events import pattern_tests
    res = pattern_tests(ctx, pats)
    for name, r_ in res.items():
        v.check(r_["recovered"], f"pattern {name}", f"effect={r_['effect']} p={r_['p_value']:.2g} ({r_['metric']})")
    v.check(8 <= len(res) <= 12, "8-12 planted patterns", str(len(res)))


def check_narratives(ctx: Ctx, v: V):
    p = ctx.output / "memory_corpus.jsonl"
    if not p.exists():
        return
    banner("narrative corpus")
    docs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    df = pd.DataFrame(docs)
    universe = footprint_universe(ctx)
    for t, c in [("disruption_events", "event_id"), ("decisions", "decision_id"), ("commitments", "commitment_id"),
                 ("negotiations", "negotiation_id"), ("contracts", "contract_id"), ("production_runs", "production_run_id")]:
        if exists(ctx, t):
            universe |= set(ctx.read(t)[c])
    src = ctx.src()
    universe |= set(src["suppliers"].supplier_id) | set(src["products"].product_id) | set(src["warehouses"].warehouse_id)
    universe |= set(ctx.read("raw_materials").rm_id) | set(ctx.read("plants").plant_id)
    miss = [(d, x) for d, xs in zip(df.doc_id, df.source_record_ids) for x in xs if x not in universe]
    v.check(not miss, "every narrative source_record_id exists", f"{sum(map(len, df.source_record_ids)):,} refs" + (f"; missing {miss[:5]}" if miss else ""))
    wc = df.content.str.split().str.len()
    v.check(wc.between(*ctx.cfg["narratives"]["words"]).all(), "docs are 60-250 words", f"min {wc.min()} max {wc.max()} mean {wc.mean():.0f}")
    v.check(df.doc_id.is_unique, "doc_id unique")
    hold = pd.Timestamp(ctx.cfg["holdout_start"])
    ts = pd.to_datetime(df.timestamp.str[:19])
    v.check(((ts >= hold) == (df.split == "holdout")).all(), "holdout split == docs dated >= 2025-10-01")
    hrs = ts.dt.hour
    v.check(hrs.between(*ctx.cfg["narratives"]["business_hours"]).all() and (ts.dt.weekday < 5).all(), "timestamps in business hours on weekdays")
    nf = df.is_noise.mean()
    v.check(0.25 <= nf <= 0.30, "25-30% low-signal noise docs", f"{nf:.1%}")
    sup = df.supersedes_doc_ids.apply(len).gt(0).mean()
    v.check(sup >= 0.10, ">=10% of docs update/supersede earlier facts", f"{sup:.1%}")
    dts = df.set_index("doc_id").timestamp
    bad = [(d, s) for d, ss in zip(df.doc_id, df.supersedes_doc_ids) for s in ss if not dts[s] < dts[d]]
    v.check(not bad, "superseded docs are older than their superseding doc", f"{len(bad)} bad")
    v.note(f"docs {len(df):,} ({df.split.value_counts().to_dict()}); types {df.doc_type.value_counts().to_dict()}")
    chains = ctx.load_json("chains.json") if (ctx.work / "chains.json").exists() else []
    ok_chains = [c for c in chains if len(c["doc_ids"]) >= 3 and len(set(c["roles"])) >= 2 and c["span_days"] >= 3]
    v.check(len(ok_chains) >= (10 if ctx.scale == "full" else 3), "key causal chains span >=3 docs, >=2 roles, written days/weeks apart", f"{len(ok_chains)} chains")
    fac = ctx.work / "doc_facts.parquet"
    if fac.exists():
        f = pd.read_parquet(fac)
        from gen_narratives import check_facts
        f["consistent"] = check_facts(ctx, f)  # recomputed independently of the generator's flag
        v.check(f.consistent.all(), "no doc contradicts the structured tables (except explicitly superseded docs)",
                f"{len(f):,} rendered facts checked against tables; {int((~f.consistent).sum())} conflicts")
    tv = df.groupby("doc_type").attrs if False else None
    tmpl = ctx.load_json("template_stats.json") if (ctx.work / "template_stats.json").exists() else {}
    if tmpl:
        v.check(min(tmpl.values()) >= 15, ">=15 template variants per doc type", f"min {min(tmpl.values())}")


def check_eval(ctx: Ctx, v: V):
    p = ctx.output / "eval_questions.jsonl"
    if not p.exists():
        return
    banner("evaluation set")
    qs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    docs = {d["doc_id"]: d for d in map(json.loads, (ctx.output / "memory_corpus.jsonl").read_text(encoding="utf-8").splitlines())}
    universe = footprint_universe(ctx)
    for t, c in [("disruption_events", "event_id"), ("decisions", "decision_id"), ("commitments", "commitment_id"),
                 ("negotiations", "negotiation_id")]:
        universe |= set(ctx.read(t)[c])
    universe |= set(ctx.src()["suppliers"].supplier_id) | set(ctx.read("raw_materials").rm_id) | set(ctx.read("plants").plant_id)
    leak, missing, split = [], [], []
    for q in qs:
        for d in q["gold_doc_ids"]:
            if d not in docs:
                missing.append((q["question_id"], d))
            elif docs[d]["timestamp"][:10] > q["as_of_date"]:
                leak.append((q["question_id"], d))
            elif docs[d]["split"] != "memory":
                split.append((q["question_id"], d))
        missing += [(q["question_id"], r_) for r_ in q["gold_record_ids"] if r_ not in universe]
    v.check(len(qs) >= (200 if ctx.scale == "full" else 60), "eval question count (>=200 at full scale)", str(len(qs)))
    v.check(not leak, "no eval question leaks post-as_of_date docs", f"{len(leak)} leaks")
    v.check(not split, "gold docs are all in the ingested memory split", f"{len(split)} holdout refs")
    v.check(not missing, "gold doc/record ids exist", f"{len(missing)} missing {missing[:3]}")
    t = pd.Series([q["type"] for q in qs]).value_counts()
    v.check(len(t) == 8, "all 8 question types present", t.to_dict().__repr__())
    v.check(all(q["hop_count"] >= 3 for q in qs if q["type"] == "multi-hop"), "multi-hop questions have hop_count >= 3")
    hs = json.loads((ctx.output / "holdout_scenarios.json").read_text(encoding="utf-8"))
    v.check(12 <= len(hs) <= 15, "12-15 holdout scenarios", str(len(hs)))
    v.check(sum(1 for h in hs if h.get("trap")) >= 2, ">=2 trap scenarios with misleading nearest precedent")
    ev = ctx.read("disruption_events").set_index("event_id")
    okp = all(ev.loc[p_, "start_date"] < pd.Timestamp(ctx.cfg["holdout_start"]) for h in hs for p_ in h["gold_precedent_event_ids"])
    v.check(okp, "holdout gold precedents predate the holdout quarter")


def summary(ctx: Ctx):
    banner("summary - new tables")
    d = ctx.output / "tables" / "parquet"
    for p in sorted(d.glob("*.parquet")):
        df = pd.read_parquet(p)
        print(f"  {p.stem:<34} {len(df):>9,} rows  {len(df.columns):>2} cols")
    for f in ("memory_corpus.jsonl", "eval_questions.jsonl"):
        if (ctx.output / f).exists():
            print(f"  {f:<34} {sum(1 for _ in open(ctx.output / f, encoding='utf-8')):>9,} lines")


def run(ctx: Ctx) -> bool:
    v = V()
    check_source_unchanged(ctx, v)
    check_fks(ctx, v)
    check_rm(ctx, v)
    check_demand(ctx, v)
    check_events(ctx, v)
    check_narratives(ctx, v)
    check_eval(ctx, v)
    summary(ctx)
    ctx.save_json({"passed": v.ok, "failed": v.fail, "info": v.info}, "validation_report.json", work=False)
    banner(f"VALIDATION: {len(v.ok)} passed, {len(v.fail)} failed")
    for f in v.fail:
        print(f"  FAIL: {f}")
    return not v.fail


if __name__ == "__main__":
    ok = run(cli(__doc__))
    sys.exit(0 if ok else 1)
