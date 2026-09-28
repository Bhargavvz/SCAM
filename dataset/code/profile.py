"""Stage 0 - profile the v1.0.0 source and derive every calibration parameter.

Nothing about scale, categories, ID formats or rates is hard-coded downstream:
later stages read calibration.json (written here) plus config.yaml.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from sc_common import Ctx, banner, dump_json, id_pattern, log, week_start


def resolve_archetype(category: str, cfg: dict) -> dict:
    arch = cfg["products"]["category_archetypes"]
    if category in arch:
        return {**arch[category], "matched": "exact"}
    low = category.lower()
    for a, kws in cfg["products"]["archetype_keywords"].items():
        if any(k in low for k in kws):
            base = next((v for v in arch.values() if v["archetype"] == a), arch["default"])
            return {**base, "matched": f"keyword:{a}"}
    return {**arch["default"], "matched": "default"}


def fg_ledger(src: dict) -> pd.DataFrame:
    """End-of-day balance per product/warehouse/date (vectorised)."""
    m = src["inventory_movements"]
    daily = m.groupby(["product_id", "warehouse_id", "movement_at"], as_index=False)["quantity_change"].sum()
    daily = daily.sort_values(["product_id", "warehouse_id", "movement_at"])
    ob = src["inventory_opening_balances"][["product_id", "warehouse_id", "opening_units"]]
    daily = daily.merge(ob, on=["product_id", "warehouse_id"], how="left")
    daily["balance"] = daily.groupby(["product_id", "warehouse_id"])["quantity_change"].cumsum() + daily["opening_units"].fillna(0)
    return daily.drop(columns=["opening_units"]).reset_index(drop=True)


def run(ctx: Ctx) -> dict:
    banner("STAGE 0 - profile source dataset")
    cfg, s = ctx.cfg, ctx.src
    sup, prod, wh = s["suppliers"], s["products"], s["warehouses"]
    ob, po, pol, mv = s["inventory_opening_balances"], s["purchase_orders"], s["purchase_order_lines"], s["inventory_movements"]
    c: dict = {}

    # ---------------------------------------------------------------- basics
    c["row_counts"] = {t: int(len(df)) for t, df in s.items()}
    c["id_patterns"] = {
        "supplier_id": id_pattern(sup.supplier_id), "product_id": id_pattern(prod.product_id),
        "warehouse_id": id_pattern(wh.warehouse_id), "purchase_order_id": id_pattern(po.purchase_order_id),
        "purchase_order_line_id": id_pattern(pol.purchase_order_line_id), "movement_id": id_pattern(mv.movement_id),
        "transfer_id": id_pattern(mv.transfer_id.dropna()), "sku": {"example": prod.sku.iloc[0]},
        "opening_balance_id": {"example": ob.opening_balance_id.iloc[0], "format": "{product_id}-{warehouse_id}"},
    }
    ws, we = mv.movement_at.min(), mv.movement_at.max()
    c["window_start"], c["window_end"] = ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")
    hs = cfg["calendar"]["holdout_start"]
    if hs == "auto":
        hs = pd.Timestamp(we).to_period("Q").start_time.strftime("%Y-%m-%d")
    c["holdout_start"] = hs

    cats = prod.category.value_counts().sort_index()
    c["categories"] = {k: int(v) for k, v in cats.items()}
    c["category_archetypes"] = {k: resolve_archetype(k, cfg) for k in cats.index}
    c["countries"] = {k: int(v) for k, v in sup.country_code.value_counts().sort_index().items()}
    c["warehouses"] = wh.assign(capacity_units=wh.capacity_units.astype(int)).to_dict("records")
    c["warehouse_regions"] = wh.groupby("region").capacity_units.sum().sort_values(ascending=False).astype(int).to_dict()
    sp = prod.merge(sup, on="supplier_id")
    c["supplier_stats"] = {
        "lead_time_days": sup.lead_time_days.describe().round(2).to_dict(),
        "reliability_score": sup.reliability_score.describe().round(4).to_dict(),
        "products_per_supplier": prod.supplier_id.value_counts().describe().round(2).to_dict(),
        "categories_per_supplier": int(sp.groupby("supplier_id").category.nunique().max()),
        "supplier_category": sp.groupby("supplier_id").category.first().to_dict(),
    }

    # ---------------------------------------------------------------- POs
    po = po.copy()
    po["delay_days"] = (po.received_at - po.expected_at).dt.days
    po["po_lead_days"] = (po.expected_at - po.ordered_at).dt.days
    x = po.merge(sup, on="supplier_id")
    rec = x[x.received_at.notna()]
    pos_delays = rec.delay_days[rec.delay_days > 0]
    lt_cfg = cfg["profile"]["late_threshold_days"]
    late_thr = int(math.ceil(pos_delays.median())) if lt_cfg == "auto" else int(lt_cfg)
    c["late_threshold_days"] = late_thr
    lines = pol.merge(po[["purchase_order_id", "status", "delay_days", "supplier_id", "warehouse_id", "expected_at"]], on="purchase_order_id")
    partial_lines = lines[lines.status == "partial"]
    c["po"] = {
        "status_share": po.status.value_counts(normalize=True).round(4).to_dict(),
        "delay_quantiles": rec.delay_days.quantile([0, .1, .25, .5, .75, .9, 1]).to_dict(),
        "late_rate_any": round(float((rec.delay_days > 0).mean()), 4),
        "late_rate_threshold": round(float((rec.delay_days >= late_thr).mean()), 4),
        "early_rate": round(float((rec.delay_days < 0).mean()), 4),
        "lead_equals_supplier_lead_share": round(float((x.po_lead_days == x.lead_time_days).mean()), 4),
        "partial_line_fill_mean": round(float((partial_lines.quantity_received / partial_lines.quantity_ordered).mean()), 4),
        "lines_per_po_mean": round(float(pol.groupby("purchase_order_id").size().mean()), 3),
        "line_supplier_is_primary_share": round(float((lines.merge(prod[["product_id", "supplier_id"]], on="product_id", suffixes=("", "_p")).eval("supplier_id == supplier_id_p")).mean()), 4),
        "late_vs_reliability_spearman": round(float(rec[["reliability_score"]].assign(l=(rec.delay_days >= late_thr).astype(int)).corr(method="spearman").iloc[0, 1]), 4),
        "late_rate_by_country": rec.groupby("country_code").delay_days.apply(lambda d: round(float((d >= late_thr).mean()), 4)).to_dict(),
        "late_rate_by_expected_month": rec.groupby(rec.expected_at.dt.month).delay_days.apply(lambda d: round(float((d >= late_thr).mean()), 4)).to_dict(),
        "open_expected_range": [po[po.status == "open"].expected_at.min().strftime("%Y-%m-%d"), po[po.status == "open"].expected_at.max().strftime("%Y-%m-%d")],
        "cancelled_count": int((po.status == "cancelled").sum()),
    }

    # ---------------------------------------------------------------- sales / demand
    sales = mv[mv.movement_type == "sale"].copy()
    sales["units"] = -sales.quantity_change
    sales["week_start"] = week_start(sales.movement_at).values
    sales = sales.merge(prod[["product_id", "category", "unit_cost"]], on="product_id")
    per_prod = sales.groupby("product_id").units.sum().reindex(prod.product_id, fill_value=0).sort_values(ascending=False)
    top20 = per_prod.head(int(len(per_prod) * 0.2)).sum() / per_prod.sum()
    n_weeks = int(((we - week_start(pd.Series([ws])).iloc[0]).days // 7) + 1)
    pw = sales.groupby(["product_id", "week_start"]).units.sum()
    monthly = sales.groupby([sales.movement_at.dt.to_period("M"), "category"]).units.sum().unstack()
    days = pd.Series({p: p.days_in_month for p in monthly.index})
    per_day = monthly.div(days, axis=0)
    season = per_day.groupby(per_day.index.month).mean()
    season = (season / season.mean()).round(4)
    yearly = sales.groupby([sales.movement_at.dt.year, "category"]).units.sum().unstack()
    trend = ((yearly.iloc[-1] / yearly.iloc[0]) ** (1 / max(1, len(yearly) - 1)) - 1).round(4)
    c["sales"] = {
        "rows": int(len(sales)), "units": int(sales.units.sum()),
        "event_units_quantiles": sales.units.quantile([.1, .5, .9]).to_dict(),
        "pareto_top20_share": round(float(top20), 4),
        "weeks_in_window": n_weeks,
        "product_week_nonzero_share": round(float(len(pw) / (len(prod) * n_weeks)), 4),
        "pair_week_nonzero_share": round(float(sales.groupby(["product_id", "warehouse_id", "week_start"]).ngroups / (len(ob) * n_weeks)), 5),
        "mean_units_per_product_week": round(float(per_prod.sum() / (len(prod) * n_weeks)), 3),
        "seasonality_index_by_month": {cat: season[cat].to_dict() for cat in season.columns},
        "annual_trend_by_category": trend.to_dict(),
        "units_by_product": per_prod.to_dict(),
    }

    # ---------------------------------------------------------------- stock
    led = fg_ledger(s)
    led = led.merge(prod[["product_id", "reorder_point"]], on="product_id")
    c["stock"] = {
        "day_rows": int(len(led)),
        "rows_le_zero": int((led.balance <= 0).sum()),
        "pairs_le_zero": int(led[led.balance <= 0].groupby(["product_id", "warehouse_id"]).ngroups),
        "min_balance": int(led.balance.min()),
        "share_rows_le_rop": round(float((led.balance <= led.reorder_point).mean()), 4),
        "pairs_ever_le_rop": int(led[led.balance <= led.reorder_point].groupby(["product_id", "warehouse_id"]).ngroups),
        "opening_units_mean": round(float(ob.opening_units.mean()), 2),
    }
    tr = mv[mv.movement_type == "transfer"]
    adj = mv[mv.movement_type == "adjustment"]
    c["transfers"] = {"pairs": int(tr.transfer_id.nunique()), "units_quantiles": tr.quantity_change.abs().quantile([.1, .5, .9]).to_dict()}
    c["adjustments"] = {"rows": int(len(adj)), "negative_share": round(float((adj.quantity_change < 0).mean()), 4), "min": int(adj.quantity_change.min()), "max": int(adj.quantity_change.max())}

    # ---------------------------------------------------------------- make/buy expectation
    exp_make = sum(cnt * c["category_archetypes"][k]["make_prob"] for k, cnt in c["categories"].items())
    c["expected_make_products"] = int(round(exp_make))

    # ---------------------------------------------------------------- assumptions
    a = []
    a.append(f"Window {c['window_start']}..{c['window_end']}; holdout (not ingested) starts {c['holdout_start']}.")
    a.append(f"{len(wh)} warehouses in {wh.region.nunique()} regions; {len(prod)} products in {len(cats)} categories; {len(sup)} suppliers in {len(c['countries'])} countries.")
    a.append(f"Each supplier serves exactly {c['supplier_stats']['categories_per_supplier']} category; every FG PO line is placed with the product's primary supplier (share {c['po']['line_supplier_is_primary_share']}).")
    a.append(f"FG receipt delay is uniform-like over {int(rec.delay_days.min())}..{int(rec.delay_days.max())} days and does NOT follow reliability (spearman {c['po']['late_vs_reliability_spearman']}). "
             "Existing FG rows are left as-is; causality (reliability, lead time, country shocks) is imposed on the NEW RM side, and FG-side events are mined as statistical clusters.")
    a.append(f"'Late' for anchoring = delay >= {late_thr} days (median positive delay).")
    a.append(f"Demand is intermittent: {c['sales']['product_week_nonzero_share']:.1%} of product-weeks and {c['sales']['pair_week_nonzero_share']:.2%} of product-warehouse-weeks have sales; "
             "product_demand_weekly is therefore stored sparsely (weeks with demand or a non-zero forecast).")
    a.append(f"Pareto skew in source sales is weak (top 20% SKUs = {top20:.1%} of units); fulfilled demand must equal existing sales exactly, so the skew is inherited, not imposed.")
    a.append(f"Stock hits <= 0 on only {c['stock']['rows_le_zero']} product-warehouse-days ({c['stock']['pairs_le_zero']} pairs); most unfulfilled demand must therefore be explained by logged shock events (allocations, DC disruptions, demand spikes).")
    a.append(f"Expected make products ~{c['expected_make_products']} (category make-probabilities in config); spares-like categories are buy-only.")
    a.append("For make products the FG PO is treated as the replenishment order from the assigned plant; the PO's supplier is the tolling partner operating the line.")
    a.append("No SQLite was provided when the input is a CSV folder; the output SQLite is built from the unchanged CSV contents plus the new tables.")
    c["assumptions"] = a
    ctx.calib = c
    ctx.save_internal("fg_ledger", led)
    dump_json(c, ctx.out / "calibration.json")

    # ---------------------------------------------------------------- print
    log("row counts: " + ", ".join(f"{k}={v:,}" for k, v in c["row_counts"].items()))
    log(f"ID patterns: " + ", ".join(f"{k}={v.get('prefix')}/{v.get('width')}" for k, v in c["id_patterns"].items() if "prefix" in v))
    log(f"categories: {c['categories']}")
    log(f"archetypes: " + ", ".join(f"{k}->{v['archetype']}({v['matched']}, make_p={v['make_prob']})" for k, v in c["category_archetypes"].items()))
    log(f"countries: {c['countries']}")
    log(f"PO status share: {c['po']['status_share']}; late(any)={c['po']['late_rate_any']}, late(>={late_thr}d)={c['po']['late_rate_threshold']}, partial fill={c['po']['partial_line_fill_mean']}")
    log(f"sales: {c['sales']['rows']:,} rows / {c['sales']['units']:,} units; nonzero pair-weeks {c['sales']['pair_week_nonzero_share']:.2%}; top20 share {top20:.1%}")
    log(f"seasonality (finished_goods-like first category): {list(c['sales']['seasonality_index_by_month'].values())[0]}")
    log(f"trend by category: {c['sales']['annual_trend_by_category']}")
    log(f"stock: rows<=0 {c['stock']['rows_le_zero']}, rows<=ROP {c['stock']['share_rows_le_rop']:.1%}, pairs ever<=ROP {c['stock']['pairs_ever_le_rop']:,}")
    log(f"transfers: {c['transfers']}; adjustments: {c['adjustments']}")
    print("\nASSUMPTIONS")
    for i, t in enumerate(a, 1):
        print(f"  {i:2d}. {t}")
    return c


if __name__ == "__main__":
    import argparse
    from sc_common import build_ctx, load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--seed", type=int); ap.add_argument("--scale", default="default"); ap.add_argument("--config")
    a = ap.parse_args()
    run(build_ctx(a.input, a.output, load_config(a.config, a.scale, a.seed)))
