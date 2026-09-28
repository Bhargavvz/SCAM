"""Stage 2 - weekly demand, forecasts, promos, price index and demand shocks.

Hard constraints
* fulfilled_units == -SUM(sale quantity_change) per product/warehouse/week (exact)
* actual_demand_units = fulfilled_units + unfulfilled_units
* unfulfilled_units > 0 only in weeks where existing stock hit <= 0 or where a
  logged shock (allocation during a supply cascade, DC disruption, demand
  spike) explains it. Shock ids are provisional (SHKxxxx) and are mapped to
  disruption_events ids in Stage 3.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sc_common import Ctx, banner, log, week_start, write_table


def run(ctx: Ctx):
    banner("STAGE 2 - product demand (weekly), forecasts, promos, shocks")
    cfg = ctx.cfg["demand"]
    rng = ctx.rng("demand")
    src = ctx.src
    prod = ctx.load_internal("structure")["prod"].set_index("product_id")
    mv = src["inventory_movements"]
    sales = mv[mv.movement_type == "sale"].copy()
    sales["week_start"] = week_start(sales.movement_at).values
    f = sales.groupby(["product_id", "warehouse_id", "week_start"]).quantity_change.sum().mul(-1).rename("fulfilled_units").reset_index()
    f["fulfilled_units"] = f.fulfilled_units.astype(int)
    ws, we = pd.Timestamp(ctx.calib["window_start"]), pd.Timestamp(ctx.calib["window_end"])
    week0, weekN = week_start(pd.Series([ws])).iloc[0], week_start(pd.Series([we])).iloc[0]

    # ------------------------------------------------ stock <= 0 weeks (natural lost sales)
    led = ctx.load_internal("fg_ledger")
    led["week_start"] = week_start(led.movement_at).values
    out_wk = led[led.balance <= 0].groupby(["product_id", "warehouse_id", "week_start"]).size().rename("days_out").reset_index()
    rop_wk = led[led.balance <= led.reorder_point].groupby(["product_id", "warehouse_id", "week_start"]).size().rename("days_le_rop").reset_index()

    d = f.merge(out_wk, on=["product_id", "warehouse_id", "week_start"], how="outer")
    d["fulfilled_units"] = d.fulfilled_units.fillna(0).astype(int)
    d["days_out"] = d.days_out.fillna(0).astype(int)
    d["unfulfilled_units"] = 0
    d["demand_shock_event_id"] = None
    so = d.days_out > 0
    d.loc[so, "unfulfilled_units"] = rng.integers(3, 21, int(so.sum()))
    shocks: list[dict] = []

    # ------------------------------------------------ (a) allocation cuts during supply cascades
    rmo = ctx.load_internal("rm_ops")
    po = src["purchase_orders"].set_index("purchase_order_id")
    pol = src["purchase_order_lines"].set_index("purchase_order_line_id")
    rop_idx = rop_wk.set_index(["product_id", "warehouse_id", "week_start"]).days_le_rop
    key_idx = d.set_index(["product_id", "warehouse_id", "week_start"]).index
    pos_of = pd.Series(np.arange(len(d)), index=key_idx)
    n_alloc = 0
    for c in rmo["cascades"]:
        line = pol.loc[c["purchase_order_line_id"]]
        h = po.loc[line.purchase_order_id]
        wks = pd.date_range(week_start(pd.Series([h.expected_at])).iloc[0], week_start(pd.Series([h.received_at])).iloc[0], freq="7D")
        hit = []
        for wk in wks:
            k = (line.product_id, h.warehouse_id, wk)
            if k in pos_of.index and k in rop_idx.index:
                i = pos_of[k]
                if d.at[i, "fulfilled_units"] > 0 and d.at[i, "demand_shock_event_id"] is None:
                    hit.append(i)
        if not hit:
            continue
        sid = f"SHK{len(shocks) + 1:04d}"
        cut = float(rng.uniform(*cfg["allocation_cut"]))
        units = 0
        for i in hit:
            u = max(1, int(round(d.at[i, "fulfilled_units"] * cut / (1 - cut))))
            d.at[i, "unfulfilled_units"] += u
            d.at[i, "demand_shock_event_id"] = sid
            units += u
        shocks.append({"shock_id": sid, "type": "allocation_cut", "cascade_id": c["cascade_id"], "product_ids": [line.product_id],
                       "warehouse_ids": [h.warehouse_id], "start": d.at[hit[0], "week_start"], "end": d.at[hit[-1], "week_start"] + pd.Timedelta(days=6),
                       "units_cut": units, "cut_pct": round(cut * 100, 1), "purchase_order_line_id": c["purchase_order_line_id"]})
        n_alloc += 1

    # ------------------------------------------------ (b) DC service disruptions (anchored on weekly sales dips)
    wk_tot = f.groupby(["warehouse_id", "week_start"]).fulfilled_units.sum().unstack(fill_value=0)
    z = (wk_tot.sub(wk_tot.median(axis=1), axis=0)).div(wk_tot.std(axis=1), axis=0)
    zz = z.stack().rename("z").reset_index()
    zz = zz[(zz.week_start > week0) & (zz.week_start < weekN)].sort_values("z")
    picked, used_wh = [], {}
    for r in zz.itertuples():
        if len(picked) >= cfg["dc_disruptions"]:
            break
        if any(abs((r.week_start - x).days) < 120 for x in used_wh.get(r.warehouse_id, [])):
            continue
        picked.append(r)
        used_wh.setdefault(r.warehouse_id, []).append(r.week_start)
    causes = ["wms_outage", "labor_shortage", "severe_weather_closure", "dock_equipment_failure", "carrier_capacity_shortfall"]
    for r in picked:
        sid = f"SHK{len(shocks) + 1:04d}"
        near = f[(f.warehouse_id == r.warehouse_id) & (f.week_start.between(r.week_start - pd.Timedelta(days=35), r.week_start + pd.Timedelta(days=35)))]
        top = near.groupby("product_id").fulfilled_units.sum().sort_values(ascending=False).head(int(rng.integers(12, 30)))
        units = 0
        for pid, tot in top.items():
            u = max(1, int(round(tot / 10 * rng.uniform(0.6, 1.4))))
            k = (pid, r.warehouse_id, r.week_start)
            if k in pos_of.index:
                i = pos_of[k]
                if d.at[i, "demand_shock_event_id"] is not None:
                    continue
                d.at[i, "unfulfilled_units"] += u
                d.at[i, "demand_shock_event_id"] = sid
            else:
                d.loc[len(d)] = {"product_id": pid, "warehouse_id": r.warehouse_id, "week_start": r.week_start, "fulfilled_units": 0,
                                 "days_out": 0, "unfulfilled_units": u, "demand_shock_event_id": sid}
            units += u
        shocks.append({"shock_id": sid, "type": "dc_disruption", "cause": str(rng.choice(causes)), "warehouse_ids": [r.warehouse_id],
                       "product_ids": top.index.tolist(), "start": r.week_start, "end": r.week_start + pd.Timedelta(days=6), "units_cut": units,
                       "sales_z": round(float(r.z), 2), "week_units": int(wk_tot.at[r.warehouse_id, r.week_start]), "median_units": float(wk_tot.loc[r.warehouse_id].median())})
    pos_of = pd.Series(np.arange(len(d)), index=d.set_index(["product_id", "warehouse_id", "week_start"]).index)

    # ------------------------------------------------ (c) demand spikes (anchored on product-week sales peaks)
    pw = f.groupby(["product_id", "week_start"]).fulfilled_units.sum()
    stats = pw.groupby(level=0).agg(["mean", "std"])
    zs = ((pw - pw.index.get_level_values(0).map(stats["mean"]).values) / pw.index.get_level_values(0).map(stats["std"]).values).rename("z").reset_index()
    zs = zs[np.isfinite(zs.z)].sort_values("z", ascending=False)
    n_sp, used_p = 0, set()
    spike_causes = ["competitor_stockout", "retailer_feature_unplanned", "weather_driven_surge", "social_media_mention", "tender_win_early_start"]
    for r in zs.itertuples():
        if n_sp >= cfg["demand_spikes"]:
            break
        if r.product_id in used_p:
            continue
        rows = d[(d.product_id == r.product_id) & (d.week_start == r.week_start) & (d.fulfilled_units > 0) & d.demand_shock_event_id.isna()]
        if len(rows) < 2:
            continue
        sid = f"SHK{len(shocks) + 1:04d}"
        units = 0
        for i in rows.index:
            u = max(1, int(round(d.at[i, "fulfilled_units"] * rng.uniform(0.15, 0.6))))
            d.at[i, "unfulfilled_units"] += u
            d.at[i, "demand_shock_event_id"] = sid
            units += u
        shocks.append({"shock_id": sid, "type": "demand_spike", "cause": str(rng.choice(spike_causes)), "product_ids": [r.product_id],
                       "warehouse_ids": rows.warehouse_id.tolist(), "start": r.week_start, "end": r.week_start + pd.Timedelta(days=6),
                       "units_cut": units, "sales_z": round(float(r.z), 2), "week_units": int(pw.loc[(r.product_id, r.week_start)])})
        used_p.add(r.product_id)
        n_sp += 1

    # ------------------------------------------------ promos (planned; anchored on high-sales weeks)
    q = d.loc[d.fulfilled_units > 0, "fulfilled_units"].quantile(cfg["promo_quantile"])
    cand = (d.fulfilled_units >= q) & d.demand_shock_event_id.isna()
    d["promo_flag"] = (cand & (rng.random(len(d)) < cfg["promo_prob"])).astype(int)

    # ------------------------------------------------ price index
    arch = d.product_id.map(prod.archetype)
    cats = sorted(arch.unique())
    drift = {c: float(rng.uniform(*cfg["price_drift_per_year"])) for c in cats}
    years = (d.week_start.dt.year - ws.year).values
    list_idx = np.array([(1 + drift[a]) ** y for a, y in zip(arch.values, years)])
    list_idx *= 1 + rng.normal(0, 0.006, len(d))
    disc = np.where(d.promo_flag == 1, rng.uniform(*cfg["promo_discount"], len(d)), 1.0)
    d["price_index"] = np.round(list_idx * disc, 3)

    # ------------------------------------------------ forecasts
    d["actual_demand_units"] = (d.fulfilled_units + d.unfulfilled_units).astype(int)
    bias = arch.map(cfg["category_bias"]).fillna(0).values
    shock = d.demand_shock_event_id.notna().values
    mu = bias + cfg["promo_bias"] * d.promo_flag.values + cfg["shock_bias"] * shock
    sig = cfg["mape_sigma"] * np.where(d.promo_flag.values == 1, cfg["promo_sigma_mult"], 1.0) * np.where(shock, cfg["shock_sigma_mult"], 1.0)
    eps = rng.normal(mu - sig ** 2 / 2, sig)  # E[exp(eps)] = exp(mu): configured bias is the true bias
    fc = np.round(d.actual_demand_units.values * np.exp(eps)).astype(int)
    d["forecast_units"] = np.where(d.actual_demand_units > 0, np.maximum(fc, 0), 0)

    # false-positive forecast rows: forecast >= 1, no demand (seasonality x trend fitted from sales)
    season = ctx.calib["sales"]["seasonality_index_by_month"]
    trend = ctx.calib["sales"]["annual_trend_by_category"]
    pair_stats = f.groupby(["product_id", "warehouse_id"]).fulfilled_units.agg(["mean", "size"]).reset_index()
    n_fp = int(cfg["false_positive_rows_share"] * int((d.actual_demand_units > 0).sum()))
    pick = pair_stats.sample(n=n_fp, replace=True, weights=pair_stats["size"], random_state=int(rng.integers(1 << 30)))
    all_weeks = pd.date_range(week0, weekN, freq="7D")
    wk_pick = all_weeks[rng.integers(0, len(all_weeks), n_fp)]
    fp = pick[["product_id", "warehouse_id", "mean"]].assign(week_start=wk_pick).drop_duplicates(["product_id", "warehouse_id", "week_start"])
    fp = fp.merge(d[["product_id", "warehouse_id", "week_start"]], how="left", indicator=True)
    fp = fp[fp._merge == "left_only"].drop(columns="_merge")
    cat_of = fp.product_id.map(prod.category)
    sidx = np.array([season.get(c, {}).get(str(m), season.get(c, {}).get(m, 1.0)) for c, m in zip(cat_of, fp.week_start.dt.month)])
    tr = np.array([(1 + trend.get(c, 0.0)) ** (y - ws.year) for c, y in zip(cat_of, fp.week_start.dt.year)])
    fpf = np.maximum(1, np.round(fp["mean"].values * sidx * tr * rng.uniform(0.25, 0.8, len(fp)))).astype(int)
    fp = fp.assign(forecast_units=fpf, fulfilled_units=0, unfulfilled_units=0, actual_demand_units=0, promo_flag=0, demand_shock_event_id=None, days_out=0)
    fp["price_index"] = np.round(np.array([(1 + drift[a]) ** (y - ws.year) for a, y in zip(fp.product_id.map(prod.archetype), fp.week_start.dt.year)]), 3)
    d = pd.concat([d, fp[d.columns.intersection(fp.columns)]], ignore_index=True)
    d["forecast_made_at"] = d.week_start - pd.Timedelta(weeks=cfg["forecast_lag_weeks"]) - pd.Timedelta(days=3)
    d = d[(d.actual_demand_units > 0) | (d.forecast_units > 0)]
    d = d.sort_values(["week_start", "product_id", "warehouse_id"]).reset_index(drop=True)
    for c in ("forecast_units", "actual_demand_units", "fulfilled_units", "unfulfilled_units", "promo_flag"):
        d[c] = d[c].astype(int)
    write_table(ctx, "product_demand_weekly", d)
    ctx.save_internal("demand", {"shocks": shocks, "price_drift": drift, "table": d.copy()})

    # ------------------------------------------------ summary
    a = d[d.actual_demand_units > 0]
    ape = (a.forecast_units - a.actual_demand_units).abs() / a.actual_demand_units
    sku = a.groupby(["product_id", "week_start"])[["forecast_units", "actual_demand_units"]].sum()
    sku_mape = ((sku.forecast_units - sku.actual_demand_units).abs() / sku.actual_demand_units).mean()
    wape = (d.forecast_units - d.actual_demand_units).abs().sum() / d.actual_demand_units.sum()
    log(f"rows {len(d):,}: demand rows {len(a):,}, forecast-only rows {int((d.actual_demand_units == 0).sum()):,}")
    log(f"fulfilled {int(d.fulfilled_units.sum()):,} units (source sales {int(-sales.quantity_change.sum()):,}); unfulfilled {int(d.unfulfilled_units.sum()):,}")
    log(f"unfulfilled rows: stock<=0 {int(((d.unfulfilled_units > 0) & (d.days_out > 0)).sum())}, shock-linked {int(((d.unfulfilled_units > 0) & d.demand_shock_event_id.notna()).sum())}")
    log(f"shocks: {pd.Series([s['type'] for s in shocks]).value_counts().to_dict()}")
    log(f"MAPE row-level {ape.mean():.1%} (normal {ape[(a.promo_flag == 0) & a.demand_shock_event_id.isna()].mean():.1%}, promo {ape[a.promo_flag == 1].mean():.1%}, shock {ape[a.demand_shock_event_id.notna()].mean():.1%}); SKU-week {sku_mape:.1%}; WAPE all rows {wape:.1%}")
    bias_c = a.assign(arch=a.product_id.map(prod.archetype)).groupby("arch").apply(lambda g: g.forecast_units.sum() / g.actual_demand_units.sum() - 1)
    log(f"forecast bias by archetype: {bias_c.round(3).to_dict()}; promo rows {int(d.promo_flag.sum()):,}")
