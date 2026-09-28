"""Stage 2 - product_demand_weekly.

Grain: one row per product x warehouse x week (Monday week_start) where there was demand, a logged
shock, or a non-trivial forecast (sparse - source sales are intermittent, ~2 sale rows per
product-warehouse over three years). fulfilled_units == -SUM(existing sale quantity_change) exactly.
Unfulfilled demand appears only where existing stock hit <= 0 or where a logged demand shock explains it.
Forecast = demand plan made 2-4 weeks ahead (order book + fitted seasonality/trend) with lognormal
error, category bias, and larger errors in promo and shock weeks.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import Ctx, banner, cli, week_start
from profile import stock_trajectory


def fg_stockout_weeks(src) -> pd.DataFrame:
    tr = stock_trajectory(src)
    z = tr[tr.on_hand <= 0].copy()
    z["week_start"] = week_start(z.movement_at)
    return z.groupby(["product_id", "warehouse_id", "week_start"]).on_hand.min().rename("min_on_hand").reset_index()


def run(ctx: Ctx):
    banner("STAGE 2 - demand")
    rng = ctx.rng("demand")
    dc = ctx.cfg["demand"]
    src, cal = ctx.src(), ctx.load_json("calibration.json")
    P = src["products"].merge(ctx.read("products_ext"), on="product_id")
    M = src["inventory_movements"]
    s = M[M.movement_type == "sale"].copy()
    s["week_start"] = week_start(s.movement_at)
    wk = (-s.groupby(["product_id", "warehouse_id", "week_start"]).quantity_change.sum()).rename("fulfilled_units").reset_index()

    # FG stockout weeks from the existing trajectory
    so = fg_stockout_weeks(src)
    ctx.write(so, "fg_stockout_weeks", work=True)

    # demand shocks anchored on the largest existing product-week sales spikes (distinct products, spread in time)
    pw = wk.groupby(["product_id", "week_start"]).fulfilled_units.sum().reset_index()
    mu = pw.groupby("product_id").fulfilled_units.transform("mean")
    sd = pw.groupby("product_id").fulfilled_units.transform("std").fillna(1)
    pw["z"] = (pw.fulfilled_units - mu) / sd.replace(0, 1)
    pw = pw.sort_values(["z", "fulfilled_units"], ascending=False)
    shocks, used_p, used_m = [], set(), {}
    for r in pw.itertuples():
        m = r.week_start.to_period("Q")
        if r.product_id in used_p or used_m.get(m, 0) >= 2:
            continue
        used_p.add(r.product_id)
        used_m[m] = used_m.get(m, 0) + 1
        shocks.append({"shock_key": f"DS{len(shocks) + 1:02d}", "product_id": r.product_id, "week_start": r.week_start,
                       "z": round(float(r.z), 2), "extra": float(rng.uniform(*dc["shock_extra_demand"]))})
        if len(shocks) >= dc["shock_count"]:
            break
    shk = pd.DataFrame(shocks)

    df = wk.merge(so, on=["product_id", "warehouse_id", "week_start"], how="outer")
    df["fulfilled_units"] = df.fulfilled_units.fillna(0).astype(int)
    df = df.merge(shk[["shock_key", "product_id", "week_start", "extra"]], on=["product_id", "week_start"], how="left")
    shortage = np.where(df.min_on_hand.notna(), np.maximum(1, np.ceil(-df.min_on_hand.fillna(0))), 0)
    shock_extra = np.where(df.shock_key.notna() & (df.fulfilled_units > 0), np.ceil(df.fulfilled_units * df.extra.fillna(0)), 0)
    df["unfulfilled_units"] = (shortage + shock_extra).astype(int)
    df["actual_demand_units"] = df.fulfilled_units + df.unfulfilled_units
    df = df[df.actual_demand_units > 0].copy()

    # promotions: per brand family, the family-weeks with the highest sales ratio (anchored in existing sales)
    fam = P.set_index("product_id").brand_family
    df["brand_family"] = df.product_id.map(fam)
    fw = df.groupby(["brand_family", "week_start"]).fulfilled_units.sum().reset_index()
    fw["ratio"] = fw.fulfilled_units / fw.groupby("brand_family").fulfilled_units.transform("mean")
    fw["year"] = fw.week_start.dt.year
    promos = fw.sort_values("ratio", ascending=False).groupby(["brand_family", "year"]).head(dc["promo_weeks_per_family_year"])
    promos = promos.assign(price_index=rng.uniform(*dc["promo_price_index"], len(promos)).round(3))
    df = df.merge(promos[["brand_family", "week_start", "price_index"]], on=["brand_family", "week_start"], how="left")
    df["promo_flag"] = df.price_index.notna().astype(int)
    df["price_index"] = df.price_index.fillna(1.0)

    # false-positive forecast rows (forecast but no demand) - small share
    n_fp = int(dc["false_positive_frac"] * len(df))
    cand = df[["product_id", "warehouse_id"]].drop_duplicates().sample(n=n_fp, replace=True, random_state=int(rng.integers(1 << 30)))
    weeks = pd.date_range(df.week_start.min(), df.week_start.max(), freq="W-MON")
    cand["week_start"] = weeks[rng.integers(0, len(weeks), n_fp)]
    cand = cand.merge(df[["product_id", "warehouse_id", "week_start"]], how="left", indicator=True)
    cand = cand[cand._merge == "left_only"].drop(columns="_merge").drop_duplicates()
    cand = cand.assign(fulfilled_units=0, unfulfilled_units=0, actual_demand_units=0, promo_flag=0, price_index=1.0,
                       brand_family=cand.product_id.map(fam))
    df = pd.concat([df, cand], ignore_index=True)

    # forecast: fitted seasonality x trend x (order book) with category bias and lognormal error
    seas = {int(k): v for k, v in cal["sales_seasonality_month_index"].items()}
    trend = cal["sales_trend_per_month_rel"]
    months = (df.week_start.dt.year - 2023) * 12 + df.week_start.dt.month - 1
    base_mult = df.week_start.dt.month.map(seas).to_numpy() * (1 + trend * (months.to_numpy() - 18))
    cat = df.product_id.map(P.set_index("product_id").category)
    bias = cat.map(lambda c: dc["category_bias"].get(c, dc["category_bias"]["_default"])).to_numpy()
    is_shock = df.shock_key.notna().to_numpy()
    is_promo = df.promo_flag.to_numpy() == 1
    sigma = np.where(is_shock, dc["shock_noise_sigma"], np.where(is_promo, dc["promo_noise_sigma"], dc["noise_sigma"]))
    mu_err = np.where(is_shock, -0.45, 0.0)
    pf = ctx.load_json("pattern_plan.json")["promo_underforecast_family"]
    promo_bias = np.where(is_promo & (df.brand_family.to_numpy() == pf), ctx.cfg["patterns"]["promo_underforecast_family"]["bias"], 0.0)
    err = np.exp(rng.normal(mu_err, sigma))
    basis = np.where(df.actual_demand_units > 0, df.actual_demand_units, rng.uniform(1, 6, len(df)))
    df["forecast_units"] = np.maximum(0.1, basis * base_mult * (1 + bias + promo_bias) * err).round(1)
    lag = rng.integers(dc["forecast_lag_days"][0], dc["forecast_lag_days"][1] + 1, len(df))
    df["forecast_made_at"] = df.week_start - pd.to_timedelta(lag, unit="D")
    df["demand_shock_event_id"] = df.shock_key.astype("string")  # remapped to EVT ids in Stage 3

    out = df[["product_id", "warehouse_id", "week_start", "forecast_units", "forecast_made_at", "actual_demand_units",
              "fulfilled_units", "unfulfilled_units", "promo_flag", "price_index", "demand_shock_event_id"]] \
        .sort_values(["week_start", "product_id", "warehouse_id"]).reset_index(drop=True)
    for c in ("actual_demand_units", "fulfilled_units", "unfulfilled_units", "promo_flag"):
        out[c] = out[c].astype(int)
    ctx.write(out, "product_demand_weekly")
    ctx.write(out, "demand_raw", work=True)  # shock keys unmapped - Stage 3 maps them to event ids idempotently
    ctx.write(shk, "demand_shocks", work=True)
    ctx.write(promos, "promos", work=True)

    a = out[out.actual_demand_units > 0]
    ape = (a.forecast_units - a.actual_demand_units).abs() / a.actual_demand_units
    rev = a.merge(P[["product_id", "unit_price"]], on="product_id").pipe(lambda x: x.fulfilled_units * x.unit_price)
    top20 = rev.groupby(a.product_id.values).sum().sort_values(ascending=False)
    print(f"rows {len(out):,} (demand weeks {len(a):,}, forecast-only {int((out.actual_demand_units == 0).sum()):,})")
    print(f"fulfilled total {out.fulfilled_units.sum():,} | unfulfilled rows {int((out.unfulfilled_units > 0).sum())} "
          f"(stockout weeks {len(so)}, shocks {len(shk)})")
    print(f"MAPE all {ape.mean():.1%} | normal {ape[(a.promo_flag == 0) & a.demand_shock_event_id.isna()].mean():.1%} | "
          f"promo {ape[a.promo_flag == 1].mean():.1%} | shock {ape[a.demand_shock_event_id.notna()].mean():.1%}")
    print(f"category bias (mean signed error): {((a.forecast_units - a.actual_demand_units) / a.actual_demand_units).groupby(a.product_id.map(P.set_index('product_id').category)).mean().round(3).to_dict()}")
    print(f"revenue share of top 20% SKUs: {top20.head(int(len(top20) * 0.2)).sum() / top20.sum():.1%} (limited by existing sales skew)")


if __name__ == "__main__":
    run(cli(__doc__))
