"""Measure planted patterns directly from the output tables (effect sizes + tests).

Used by Stage 3 (to write planted_patterns.json) and by validate.py (to prove
the patterns are statistically recoverable from structured data alone).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats


def _cohen(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    s = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
    return float((a.mean() - b.mean()) / s) if s > 0 else float("inf")


def _mw(a, b):
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    return float(stats.mannwhitneyu(a, b, alternative="greater").pvalue)


def _prop(k1, n1, k2, n2):
    """one-sided two-proportion z-test p-value (p1 > p2)."""
    if min(n1, n2) == 0:
        return float("nan")
    p = (k1 + k2) / (n1 + n2)
    se = np.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0 if k1 / n1 > k2 / n2 else 1.0
    return float(stats.norm.sf((k1 / n1 - k2 / n2) / se))


def rm_po_frame(T: dict, suppliers: pd.DataFrame) -> pd.DataFrame:
    h = T["rm_purchase_orders"]
    l = T["rm_purchase_order_lines"]
    x = l.merge(h, on="rm_purchase_order_id")
    x = x[x.received_at.notna() & (x.order_type == "regular")].copy()
    x["delay"] = (x.received_at - x.promised_at).dt.days
    x["country"] = x.supplier_id.map(suppliers.set_index("supplier_id").country_code)
    return x


def measure(plan: dict, extra: dict, T: dict, src: dict, calib: dict) -> list[dict]:
    out = []
    sup = src["suppliers"]
    x = rm_po_frame(T, sup)

    def add(pid, name, desc, entities, detection, metric, effect, p, ok, extra_info=None):
        out.append({"pattern_id": pid, "name": name, "description": desc, "entities": entities, "detection": detection,
                    "metric": metric, "effect": effect, "p_value": None if p is None or (isinstance(p, float) and np.isnan(p)) else round(float(p), 6),
                    "recoverable": bool(ok), **(extra_info or {})})

    if "P01" in plan:
        s = plan["P01"]["supplier_id"]
        g = x[x.supplier_id == s]
        a, b = g[g.promised_at.dt.month.isin([11, 12])].delay, g[~g.promised_at.dt.month.isin([11, 12])].delay
        diff = float(a.mean() - b.mean())
        p = _mw(a, b)
        add("P01", "Supplier slips 7+ days every Nov-Dec", f"RM deliveries from {s} promised in November-December arrive 7-14 days late every year.",
            {"supplier_id": s}, "rm_purchase_orders x lines, delay = received_at - promised_at, grouped by promised month",
            "mean delay Nov-Dec minus other months (days)", {"nov_dec_mean": round(float(a.mean()), 2), "other_mean": round(float(b.mean()), 2), "diff_days": round(diff, 2), "cohens_d": round(_cohen(a, b), 2), "n": [int(len(a)), int(len(b))]},
            p, diff >= 5 and p < 0.01)
    if "P02" in plan:
        c = plan["P02"]["country_code"]
        g = x[x.country == c]
        a, b = g[g.promised_at.dt.month == 2].delay, g[g.promised_at.dt.month != 2].delay
        diff = float(a.mean() - b.mean())
        p = _mw(a, b)
        add("P02", "February port congestion for one origin country", f"RM lots from {c}-based suppliers promised in February arrive 5-12 days late.",
            {"country_code": c}, "RM deliveries from suppliers with country_code, delay by promised month",
            "mean delay Feb minus other months (days)", {"feb_mean": round(float(a.mean()), 2), "other_mean": round(float(b.mean()), 2), "diff_days": round(diff, 2), "cohens_d": round(_cohen(a, b), 2), "n": [int(len(a)), int(len(b))]},
            p, diff >= 4 and p < 0.01)
    if "P03" in plan:
        rm = plan["P03"]["rm_id"]
        runs = T["production_runs"]
        sh = runs[(runs.rm_shortage_rm_id == rm) & runs.actual_start.notna()]
        wins = [(pd.Timestamp(d) + pd.Timedelta(days=21), pd.Timestamp(d) + pd.Timedelta(days=80)) for d in plan["P03"]["spike_dates"]]
        inw = sh.planned_start.apply(lambda t: any(a <= t <= b for a, b in wins))
        days_in = sum((b - a).days for a, b in wins)
        total = (pd.Timestamp(calib["window_end"]) - pd.Timestamp(calib["window_start"])).days
        k_in, k_out = int(inw.sum()), int((~inw).sum())
        r_in, r_out = k_in / days_in, k_out / max(1, total - days_in)
        p = float(stats.poisson.sf(k_in - 1, max(1e-9, r_out * days_in))) if k_in else 1.0
        spikes_hit = sum(any(a <= t <= b for t in sh.planned_start) for a, b in wins)
        add("P03", "Single-source RM shortage recurs after price spikes", f"{rm} (single source {plan['P03']['supplier_id']}) runs short 3-11 weeks after each price increase.",
            {"rm_id": rm, "supplier_id": plan["P03"]["supplier_id"], "spike_dates": plan["P03"]["spike_dates"]},
            "rm_supplier_catalog price steps vs production_runs with rm_shortage_rm_id", "RM-shortage runs per day inside post-spike windows vs outside",
            {"rate_in_window": round(r_in, 4), "rate_outside": round(r_out, 4), "rate_ratio": round(r_in / r_out, 2) if r_out else None, "runs_in": k_in, "runs_out": k_out, "spikes_followed_by_shortage": spikes_hit},
            p, spikes_hit >= 2 and (r_out == 0 or r_in / r_out >= 3) and p < 0.01)
    if "P04" in plan:
        s = plan["P04"]["supplier_id"]
        g = x[x.supplier_id == s].copy()
        g["value"] = g.quantity_ordered * g.unit_cost
        med = g.value.median()
        g["on_time"] = (g.delay <= 0) & (g.quantity_received >= g.quantity_ordered - 1e-6)
        sm, lg = g[g.value <= med], g[g.value > med]
        p = _prop(int(sm.on_time.sum()), len(sm), int(lg.on_time.sum()), len(lg))
        cm = T.get("commitments")
        kept = None
        if cm is not None:
            c = cm[(cm.counterparty_id == s) & cm.status.isin(["fulfilled", "breached"])]
            kept = round(float((c.status == "fulfilled").mean()), 3) if len(c) else None
        add("P04", "Supplier keeps promises only on small POs", f"{s} delivers small RM orders on time but misses most large ones.",
            {"supplier_id": s}, "RM lines of the supplier split at the median line value; on-time = received_at <= promised_at and in full",
            "on-time rate small minus large", {"small_on_time": round(float(sm.on_time.mean()), 3), "large_on_time": round(float(lg.on_time.mean()), 3), "median_line_value": round(float(med), 2),
                                               "n": [int(len(sm)), int(len(lg))], "commitments_kept_rate": kept},
            p, float(sm.on_time.mean() - lg.on_time.mean()) >= 0.3 and p < 0.01)
    dec = T.get("decisions")
    if "P05" in extra and dec is not None:
        w = extra["P05"]["warehouse_id"]
        mv = src["inventory_movements"].set_index("movement_id")
        pol = src["purchase_order_lines"].set_index("purchase_order_line_id")
        po = src["purchase_orders"].set_index("purchase_order_id")
        rows = []
        for r in dec[dec.decision_type == "expedite"].itertuples():
            fp = json.loads(r.footprint_ids)
            if len(fp) >= 2 and fp[0] in po.index and fp[1] in mv.index:
                line = pol.loc[mv.at[fp[1], "purchase_order_line_id"]]
                rows.append((po.at[fp[0], "warehouse_id"], r.actual_cost / (line.quantity_ordered * line.unit_cost)))
        f = pd.DataFrame(rows, columns=["wh", "rate"])
        a, b = f[f.wh == w].rate, f[f.wh != w].rate
        ratio = float(a.mean() / b.mean()) if len(a) and len(b) else float("nan")
        add("P05", "Expedites into one warehouse cost ~2x", f"FG expedites delivered into {w} cost about twice the usual premium.",
            {"warehouse_id": w}, "decisions (expedite) -> footprint receipt movement -> PO line value; cost / value by warehouse",
            "mean cost rate at warehouse / elsewhere", {"rate_at_warehouse": round(float(a.mean()), 4) if len(a) else None, "rate_elsewhere": round(float(b.mean()), 4) if len(b) else None, "ratio": round(ratio, 2), "n": [int(len(a)), int(len(b))]},
            _mw(a, b), len(a) >= 3 and ratio >= 1.8)
    if "P06" in plan:
        pl = plan["P06"]["plant_id"]
        r = T["production_runs"]
        r = r[(r.delay_days > 0) & r.actual_start.notna()].copy()
        r["woq"] = (r.planned_start - r.planned_start.dt.to_period("Q").dt.start_time).dt.days // 7 + 1
        r["m"] = r.delay_reason_code == "maintenance_overrun"
        a = r[(r.plant_id == pl) & r.woq.isin(plan["P06"]["weeks_of_quarter"])]
        b = r[~((r.plant_id == pl) & r.woq.isin(plan["P06"]["weeks_of_quarter"]))]
        p = _prop(int(a.m.sum()), len(a), int(b.m.sum()), len(b))
        add("P06", "Plant maintenance overruns at quarter start", f"Runs at {pl} planned in weeks 1-2 of each quarter are delayed by maintenance overruns.",
            {"plant_id": pl}, "production_runs delay_reason_code by plant and week-of-quarter", "share of delayed runs with maintenance_overrun",
            {"share_in_segment": round(float(a.m.mean()), 3), "share_elsewhere": round(float(b.m.mean()), 3), "n": [int(len(a)), int(len(b))]}, p, float(a.m.mean()) >= 0.5 and p < 0.01)
    if "P07" in plan:
        p7 = plan["P07"]
        mvr = T["rm_inventory_movements"]
        pe = T["products_ext"].set_index("product_id")
        pl = pe.at[p7["product_id"], "plant_id"]
        rc = mvr[mvr.movement_type == "receipt"].groupby(["rm_id", "plant_id"]).quantity_change.sum()
        sc = -mvr[mvr.movement_type == "scrap"].groupby(["rm_id", "plant_id"]).quantity_change.sum()
        after = mvr[(mvr.rm_id == p7["new_rm"]) & (mvr.plant_id == pl) & (mvr.movement_at >= pd.Timestamp(p7["date"]))]
        rate_a = float(-after[after.movement_type == "scrap"].quantity_change.sum() / max(1e-9, after[after.movement_type == "receipt"].quantity_change.sum()))
        base = (sc / rc).fillna(0).drop((p7["new_rm"], pl), errors="ignore")
        p = float(stats.percentileofscore(base.values, rate_a) / 100)
        add("P07", "Substitute RM raises incoming rejects", f"After {p7['product_id']} switched {p7['old_rm']} -> {p7['new_rm']} on {p7['date']}, incoming QA rejects of {p7['new_rm']} at {pl} jumped.",
            {"product_id": p7["product_id"], "old_rm": p7["old_rm"], "new_rm": p7["new_rm"], "plant_id": pl}, "rm_inventory_movements scrap / receipt by rm and plant; bill_of_materials version change",
            "reject rate of substitute after change vs all other rm-plant combos", {"reject_rate_after": round(rate_a, 4), "median_other": round(float(base.median()), 4), "p95_other": round(float(base.quantile(0.95)), 4)},
            1 - p, rate_a > float(base.quantile(0.95)))
    if "P08" in plan and T.get("rm_inventory_snapshots_weekly") is not None:
        pl8 = plan["P08"]["plant_id"]
        mvr = T["rm_inventory_movements"]
        sn = T["rm_inventory_snapshots_weekly"]
        out_tr = mvr[(mvr.movement_type == "transfer") & (mvr.quantity_change < 0)]
        rows = []
        snk = {k: g.sort_values("snapshot_date") for k, g in sn.groupby(["rm_id", "plant_id"])}
        for r in out_tr.itertuples():
            g = snk.get((r.rm_id, r.plant_id))
            if g is None:
                continue
            w_ = g[(g.snapshot_date >= r.movement_at) & (g.snapshot_date <= r.movement_at + pd.Timedelta(days=21))]
            rows.append((r.plant_id, bool((w_.on_hand_units < w_.safety_stock_units).any())))
        f = pd.DataFrame(rows, columns=["donor", "dip"])
        a, b = f[f.donor == pl8].dip, f[f.donor != pl8].dip
        p = _prop(int(a.sum()), len(a), int(b.sum()), len(b))
        add("P08", "Stock transfers out of one plant leave it short", f"Inter-plant RM transfers sourced from {pl8} push {pl8} below safety stock within 3 weeks; other donor plants stay covered.",
            {"plant_id": pl8}, "rm_inventory_movements transfer (outbound) + rm_inventory_snapshots_weekly on_hand vs safety_stock in the next 21 days",
            "share of outbound transfers followed by donor on-hand < safety stock", {"share_donor": round(float(a.mean()), 3) if len(a) else None, "share_other_donors": round(float(b.mean()), 3) if len(b) else None, "n": [int(len(a)), int(len(b))]},
            p, len(a) >= 3 and float(a.mean()) - (float(b.mean()) if len(b) else 0) >= 0.4 and p < 0.01)
    if "P09" in plan and T.get("rm_po_revisions") is not None:
        s = plan["P09"]["supplier_id"]
        rv = T["rm_po_revisions"]
        rv = rv[rv.reason_code != "expedite_air_freight"].merge(T["rm_purchase_orders"][["rm_purchase_order_id", "supplier_id", "received_at", "status"]], left_on="rm_po_id", right_on="rm_purchase_order_id")
        nxt = rv.sort_values("revised_at").groupby("rm_po_id").revised_at.shift(-1).reindex(rv.index)
        broken = (nxt.notna() & (nxt < rv.new_expected_at)) | (rv.received_at > rv.new_expected_at)
        a, b = broken[rv.supplier_id == s], broken[rv.supplier_id != s]
        p = _prop(int(a.sum()), len(a), int(b.sum()), len(b))
        add("P09", "Supplier misses its own recovery dates", f"When {s} re-promises a late RM lot it usually misses the new date too.",
            {"supplier_id": s}, "rm_po_revisions followed by another revision or a later receipt", "share of re-promised dates broken",
            {"broken_share": round(float(a.mean()), 3), "others": round(float(b.mean()), 3), "n": [int(len(a)), int(len(b))]}, p, float(a.mean()) - float(b.mean()) >= 0.3 and p < 0.01)
    d = T.get("product_demand_weekly")
    if d is not None:
        pe = T["products_ext"].set_index("product_id")
        cat = src["products"].set_index("product_id").category
        dd = d[d.actual_demand_units > 0].copy()
        dd["cat"] = dd.product_id.map(cat)
        dd = dd[(dd.promo_flag == 0) & dd.demand_shock_event_id.isna()]
        bias = dd.groupby("cat").apply(lambda g: g.forecast_units.sum() / g.actual_demand_units.sum() - 1)
        top = bias.idxmax()
        ratio = np.log((dd.forecast_units + 0.5) / (dd.actual_demand_units + 0.5))
        p = _mw(ratio[dd.cat == top], ratio[dd.cat != top])
        add("P10", "Systematic forecast over-bias in one category", f"Forecasts for category '{top}' run materially above actual demand.",
            {"category": top}, "product_demand_weekly: sum(forecast)/sum(actual)-1 by category (non-promo, non-shock weeks)", "bias by category",
            {k: round(float(v), 3) for k, v in bias.items()}, p, float(bias[top] - bias.drop(top).max()) >= 0.05 and p < 0.01)
    if "P11" in plan:
        s = plan["P11"]["supplier_id"]
        g = x[x.supplier_id == s].copy()
        g["mi"] = (g.promised_at.dt.year - 2025) * 12 + g.promised_at.dt.month
        a = g[g.promised_at >= pd.Timestamp(plan["P11"]["from"])]
        b = g[g.promised_at < pd.Timestamp(plan["P11"]["from"])]
        sl = stats.linregress(a.mi, a.delay) if len(a) > 3 else None
        add("P11", "Lead-time creep at one supplier", f"Actual lead times from {s} lengthen month after month from {plan['P11']['from']}.",
            {"supplier_id": s}, "RM deliveries: delay vs promised month in 2025", "slope of delay (days/month) in 2025 and mean delay 2025 vs before",
            {"slope_days_per_month": round(float(sl.slope), 2) if sl else None, "mean_2025": round(float(a.delay.mean()), 2), "mean_before": round(float(b.delay.mean()), 2)},
            float(sl.pvalue) if sl else None, bool(sl) and sl.slope >= 0.6 and sl.pvalue < 0.01)
    if "P12" in extra:
        e = extra["P12"]
        po = src["purchase_orders"]
        g = po[(po.supplier_id == e["supplier_id"]) & po.received_at.notna()].copy()
        g["late"] = (g.received_at - g.expected_at).dt.days >= calib["late_threshold_days"]
        a, b = g[g.expected_at.dt.month == e["month"]].late, g[g.expected_at.dt.month != e["month"]].late
        p = _prop(int(a.sum()), len(a), int(b.sum()), len(b))
        add("P12", "Recurring same-month FG lateness (mined)", f"{e['supplier_id']} FG POs due in month {e['month']} were late in every year of the window.",
            {"supplier_id": e["supplier_id"], "month": e["month"]}, "existing purchase_orders delay by expected month", "late rate in month vs other months",
            {"late_rate_month": round(float(a.mean()), 3), "late_rate_other": round(float(b.mean()), 3), "by_year": e["yearly_late_rate"]}, p, float(a.mean() - b.mean()) >= 0.25,
            {"note": "anchored in v1.0.0 rows (not injected)"})
    return out
