"""Stage 0 - profile the read-only source dataset and derive calibration parameters.

Writes _work/calibration.json and _work/source_fingerprint.json. Nothing about scale,
categories or ID formats is hardcoded downstream: later stages read this file.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import SOURCE_TABLES, Ctx, banner, cli, id_pattern, table_fingerprint, week_start


def stock_trajectory(src: dict) -> pd.DataFrame:
    """Running on-hand per product/warehouse after each movement date (vectorised)."""
    m = src["inventory_movements"]
    daily = m.groupby(["product_id", "warehouse_id", "movement_at"], as_index=False).quantity_change.sum()
    daily = daily.sort_values(["product_id", "warehouse_id", "movement_at"])
    daily["cum"] = daily.groupby(["product_id", "warehouse_id"]).quantity_change.cumsum()
    ob = src["inventory_opening_balances"][["product_id", "warehouse_id", "opening_units"]]
    daily = daily.merge(ob, on=["product_id", "warehouse_id"], how="left")
    daily["on_hand"] = daily.opening_units + daily.cum
    return daily


def run(ctx: Ctx) -> dict:
    banner("STAGE 0 - profile source dataset")
    src = ctx.src()
    S, P, W = src["suppliers"], src["products"], src["warehouses"]
    PO, POL, M = src["purchase_orders"], src["purchase_order_lines"], src["inventory_movements"]
    OB = src["inventory_opening_balances"]

    fp = {t: table_fingerprint(src[t]) for t in SOURCE_TABLES}
    ctx.save_json(fp, "source_fingerprint.json")

    po = PO.merge(S, on="supplier_id")
    po["delay"] = (po.received_at - po.expected_at).dt.days
    po["lead"] = (po.expected_at - po.ordered_at).dt.days
    lines = POL.merge(PO, on="purchase_order_id")
    lines["delay"] = (lines.received_at - lines.expected_at).dt.days

    sales = M[M.movement_type == "sale"].copy()
    sales["week"] = week_start(sales.movement_at)
    weekly = sales.groupby(["product_id", "week"]).quantity_change.sum().mul(-1)
    n_weeks = int(((M.movement_at.max() - M.movement_at.min()).days + 1) / 7)
    per_prod_weekly = weekly.groupby("product_id").sum() / n_weeks
    monthly = -sales.groupby(sales.movement_at.dt.to_period("M")).quantity_change.sum()
    per_day = monthly / monthly.index.days_in_month
    seas = per_day.groupby(per_day.index.month).mean()
    seas = (seas / seas.mean()).round(4)
    t = np.arange(len(per_day))
    slope = float(np.polyfit(t, per_day.values, 1)[0] / per_day.mean())

    traj = stock_trajectory(src).merge(P[["product_id", "reorder_point"]], on="product_id")
    tr = M[M.movement_type == "transfer"]
    tr_pairs = tr[tr.quantity_change < 0].merge(tr[tr.quantity_change > 0], on="transfer_id", suffixes=("_out", "_in"))
    adj = M[M.movement_type == "adjustment"]

    rev = sales.merge(P[["product_id", "unit_cost"]], on="product_id")
    rev_p = (-rev.quantity_change * rev.unit_cost).groupby(rev.product_id).sum().sort_values(ascending=False)
    gini = float(1 - 2 * np.trapezoid(np.cumsum(np.sort(rev_p.values)) / rev_p.sum(), dx=1 / len(rev_p)))

    corr = po[["delay", "reliability_score", "lead_time_days"]].corr().loc["delay"].round(4).to_dict()
    cal = {
        "counts": {t: int(len(src[t])) for t in SOURCE_TABLES},
        "window": [str(M.movement_at.min().date()), str(M.movement_at.max().date())],
        "categories": sorted(P.category.unique().tolist()),
        "products_per_category": P.category.value_counts().to_dict(),
        "warehouses": W.warehouse_id.tolist(),
        "warehouse_regions": W.set_index("warehouse_id").region.to_dict(),
        "regions": sorted(W.region.unique().tolist()),
        "countries": S.country_code.value_counts().to_dict(),
        "id_patterns": {
            "supplier": id_pattern(S.supplier_id), "product": id_pattern(P.product_id),
            "warehouse": id_pattern(W.warehouse_id), "purchase_order": id_pattern(PO.purchase_order_id),
            "purchase_order_line": id_pattern(POL.purchase_order_line_id), "movement": id_pattern(M.movement_id),
            "transfer": id_pattern(M.transfer_id), "sku": id_pattern(P.sku),
        },
        "supplier_lead_time": S.lead_time_days.describe().round(2).to_dict(),
        "supplier_reliability": S.reliability_score.describe().round(4).to_dict(),
        "po_status_rate": PO.status.value_counts(normalize=True).round(4).to_dict(),
        "po_late_rate": float((po.delay > 0).mean()),
        "po_delay_days": po.delay.describe().round(3).to_dict(),
        "po_delay_values": sorted(po.delay.dropna().astype(int).unique().tolist()),
        "po_lead_days": po.lead.describe().round(2).to_dict(),
        "delay_correlations": corr,
        "delay_by_country": po.groupby("country_code").delay.mean().round(3).to_dict(),
        "delay_by_month": po.groupby(po.expected_at.dt.month).delay.mean().round(3).to_dict(),
        "partial_line_rate": float((lines.status == "partial").mean()),
        "partial_fill_ratio": float((lines.quantity_received / lines.quantity_ordered)[lines.status == "partial"].mean()),
        "lines_per_po": POL.groupby("purchase_order_id").size().value_counts().sort_index().to_dict(),
        "sale_rows": int(len(sales)),
        "sale_qty": (-sales.quantity_change).describe().round(2).to_dict(),
        "sale_rows_per_product_warehouse": sales.groupby(["product_id", "warehouse_id"]).size().describe().round(3).to_dict(),
        "weekly_units_per_product": per_prod_weekly.describe().round(4).to_dict(),
        "weeks_with_sales_share": float(len(weekly) / (P.shape[0] * n_weeks)),
        "sales_seasonality_month_index": {int(k): float(v) for k, v in seas.items()},
        "sales_trend_per_month_rel": round(slope, 5),
        "revenue_gini_at_cost": round(gini, 4),
        "stock_rows_at_or_below_zero": int((traj.on_hand <= 0).sum()),
        "stock_pw_at_or_below_zero": int(traj[traj.on_hand <= 0][["product_id", "warehouse_id"]].drop_duplicates().shape[0]),
        "stock_rows_at_or_below_rop_share": float((traj.on_hand <= traj.reorder_point).mean()),
        "min_on_hand": float(traj.on_hand.min()),
        "transfer_pairs": int(len(tr_pairs)),
        "transfer_qty": tr_pairs.quantity_change_in.describe().round(2).to_dict(),
        "transfer_same_region_share": float((tr_pairs.warehouse_id_out.map(W.set_index("warehouse_id").region)
                                            == tr_pairs.warehouse_id_in.map(W.set_index("warehouse_id").region)).mean()),
        "adjustment_neg_share": float((adj.quantity_change < 0).mean()),
        "adjustment_qty": adj.quantity_change.describe().round(2).to_dict(),
        "receipt_matches_po": bool((M[M.movement_type == "receipt"].merge(lines, on="purchase_order_line_id")
                                    .pipe(lambda r: (r.movement_at == r.received_at) & (r.quantity_change == r.quantity_received))).all()),
        "po_supplier_equals_product_supplier": float((POL.merge(PO, on="purchase_order_id")
                                                      .merge(P[["product_id", "supplier_id"]], on="product_id", suffixes=("", "_p"))
                                                      .pipe(lambda d: d.supplier_id == d.supplier_id_p)).mean()),
    }
    assumptions = [
        f"{cal['counts']['warehouses']} warehouses (not 6); {len(cal['categories'])} categories: {', '.join(cal['categories'])}.",
        f"FG PO delay is ~uniform over {min(cal['po_delay_values'])}..{max(cal['po_delay_values'])} days with "
        f"corr(delay, reliability)={corr['reliability_score']:.3f}; FG lateness carries no supplier/country/month signal, "
        "so causal structure and planted patterns live in the new RM-side tables; FG anchors use the most extreme clusters.",
        f"Sales are intermittent: {cal['sale_rows_per_product_warehouse']['mean']:.2f} sale rows per product-warehouse over the window; "
        "product_demand_weekly is therefore stored sparse (weeks with demand or a forecast).",
        f"Seasonality is flat (month index {min(seas):.2f}..{max(seas):.2f}, per-day normalised) and trend ~{slope:+.4f}/month; forecasts use these fitted values.",
        f"FG stock touches <=0 in only {cal['stock_rows_at_or_below_zero']} movement rows ({cal['stock_pw_at_or_below_zero']} product-warehouses): "
        "unfulfilled demand is rare and appears only there or in logged demand-shock weeks.",
        "Make-products: the existing FG PO for a make-product is treated as the replenishment order to our own plant; "
        "its supplier_id is kept as recorded (read-only) and interpreted as the commercial counterparty.",
        "RM quantities are decimal (kg, L, m2) or integer (ea); identities are checked to 1e-6 after 4-dp rounding.",
    ]
    cal["assumptions"] = assumptions
    ctx.save_json(cal, "calibration.json")

    print(f"source: {ctx.input}")
    for t in SOURCE_TABLES:
        print(f"  {t:<28} {fp[t]['rows']:>8,}  sha256={fp[t]['sha256'][:12]}")
    keys = ["categories", "po_status_rate", "po_late_rate", "delay_correlations", "partial_line_rate",
            "weekly_units_per_product", "sales_seasonality_month_index", "sales_trend_per_month_rel",
            "stock_rows_at_or_below_zero", "stock_rows_at_or_below_rop_share", "transfer_pairs",
            "adjustment_neg_share", "revenue_gini_at_cost", "id_patterns"]
    print("\ncalibration:")
    for k in keys:
        print(f"  {k}: {cal[k]}")
    print("\nassumptions:")
    for a in assumptions:
        print(f"  - {a}")
    return cal


if __name__ == "__main__":
    run(cli(__doc__))
