"""Stage 3 (part 2) - decisions with computed outcomes, negotiations and commitments."""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd

import consequences as cq

DAY = pd.Timedelta(days=1)


# =============================================================================
# FG ledger helper (piecewise-constant end-of-day balances)
# =============================================================================
class FGLedger:
    def __init__(self, led: pd.DataFrame, ob: pd.DataFrame, prod: pd.DataFrame):
        self.ob = ob.set_index(["product_id", "warehouse_id"]).opening_units.to_dict()
        self.rop = prod.set_index("product_id").reorder_point.to_dict()
        self.d = {}
        for k, g in led.groupby(["product_id", "warehouse_id"]):
            self.d[k] = (g.movement_at.values.astype("datetime64[D]"), g.balance.values.astype(float))

    def bal_at(self, pid, wh, t) -> float:
        dates, bal = self.d.get((pid, wh), (np.array([], dtype="datetime64[D]"), np.array([])))
        i = np.searchsorted(dates, np.datetime64(pd.Timestamp(t).date()), side="right") - 1
        return float(bal[i]) if i >= 0 else float(self.ob.get((pid, wh), 0))

    def days_le(self, pid, wh, t0, t1, thr) -> int:
        t0, t1 = pd.Timestamp(t0).normalize(), pd.Timestamp(t1).normalize()
        if t1 < t0:
            return 0
        days = pd.date_range(t0, t1, freq="D")
        dates, bal = self.d.get((pid, wh), (np.array([], dtype="datetime64[D]"), np.array([])))
        idx = np.searchsorted(dates, days.values.astype("datetime64[D]"), side="right") - 1
        b = np.where(idx >= 0, bal[np.maximum(idx, 0)] if len(bal) else 0, self.ob.get((pid, wh), 0))
        return int((b <= thr).sum())


# =============================================================================
# decision candidates
# =============================================================================
def _mk(**kw):
    base = {"footprint": [], "options": [], "meta": {}, "context": {}, "failure_driver": None, "commit_seeds": []}
    base.update(kw)
    return base


def _opt(name, cost, days, risk, note=""):
    return {"option": name, "est_cost": round(float(cost), 2), "est_stockout_days": round(float(days), 1), "risk": risk, **({"note": note} if note else {})}


def _season(d: pd.Timestamp) -> str:
    return "q4_peak" if d.month in (11, 12) else ("feb" if d.month == 2 else "regular")


def congested_flag(c) -> bool:
    return "P02" in c["tags"]


def cascade_decisions(ctx, N, book, env) -> list[dict]:
    cfg = ctx.cfg
    R = env["R"]
    prod = env["S"]["prod"].set_index("product_id")
    cat = env["cat"]
    rpo = env["rpo"].set_index("rm_purchase_order_id")
    runs = env["runs"].set_index("production_run_id")
    xy = cfg["plants"]["region_xy"]
    preg = ctx.table("plants").set_index("plant_id").region.to_dict()
    shocks_by_cas = {sh["cascade_id"]: sh for sh in env["DM"]["shocks"] if sh["type"] == "allocation_cut"}
    out = []
    for c in R["cascades"]:
        e = book.by_key[f"cas:{c['cascade_id']}"]
        prim = runs.loc[c["production_run_id"]]
        up = float(prod.at[prim.product_id, "unit_price"])
        daily_p = cq.block_cost_per_day(prim.planned_qty * up, cfg)
        daily_all = cq.block_cost_per_day(e["meta"]["value_blocked"], cfg)
        sup = c["slipped"][0]["supplier_id"]
        h = rpo.loc[c["slipped"][0]["rm_purchase_order_id"]]
        lead = int((h.promised_at - h.ordered_at).days)
        lines_value = float(sum(s_["qty"] * s_["price"] for s_ in c["slipped"]))
        P, X = c["P_date"], c["X_date"]
        resp = c["response"]
        d = c["d_dec_date"] if c["d_dec"] is not None else c["t_revised_date"] + DAY
        d = min(d, P)
        eta = c["slip_to_date"] if resp != "accept_delay" else X
        opts = []
        acc_days = max(0, (eta - P).days)
        opts.append(_opt("accept_delay", acc_days * daily_p, acc_days, "low", f"supplier ETA {eta:%Y-%m-%d}"))
        e_days = cq.rm_expedite_days(lead, False)
        e_arr = d + e_days * DAY
        e_d = max(0, (e_arr - P).days)
        prem_q = cq.rm_expedite_premium_pct(lead, False)
        opts.append(_opt("expedite", prem_q * lines_value + e_d * daily_p, e_d, "medium", f"air freight quote {prem_q * 100:.0f}% of lot value, ~{e_days} days door-to-door"))
        others = cat[(cat.rm_id == c["rm_id"]) & (cat.supplier_id != sup) & (cat.price_valid_from <= d) & (cat.price_valid_to >= d)]
        q_others = others[others.qualified_flag == 1]
        if len(q_others):
            o = q_others.sort_values("allocation_pct", ascending=False).iloc[0]
            sl = cq.spot_lead_days(int(o.lead_time_days))
            s_d = max(0, (d + sl * DAY - P).days)
            opts.append(_opt("switch_supplier", cfg["rm_ops"]["spot_price_premium"] * c["req"] * float(o.unit_price) + s_d * daily_p, s_d, "medium", f"spot lot from {o.supplier_id} in ~{sl} days"))
        donors = [k for k in env["combo_keys"] if k[0] == c["rm_id"] and k[1] != c["plant_id"]]
        if donors:
            dp = donors[0][1]
            td = cq.transfer_days(math.dist(xy.get(preg[dp], [0, 0]), xy.get(preg[c["plant_id"]], [0, 0])))
            t_d = max(0, (d + td * DAY - P).days)
            opts.append(_opt("reallocate_stock", cfg["rm_ops"]["transfer_cost_per_unit_value"] * c["req"] * lines_value / max(1e-9, sum(s_["qty"] for s_ in c["slipped"])) + t_d * daily_p, t_d, "medium", f"inter-plant transfer from {dp} (~{td} days)"))
        dtype = {"accept_delay": "accept_delay", "expedite": "expedite", "switch_supplier": "switch_supplier", "cancel_and_switch": "switch_supplier", "rm_transfer": "reallocate_stock"}[resp]
        # planner estimate for the chosen option: carrier / supplier quote close to the eventual landing,
        # occasionally optimistic; congestion is not in the quote. 70% of planners price all blocked runs.
        rq = np.random.default_rng([ctx.seed, int(c["cascade_id"][3:])])
        sees_all = rq.random() < 0.7
        daily_est = daily_all if sees_all else daily_p
        if dtype == "accept_delay":
            q_days = acc_days
        else:
            k = int(rq.choice([0, 0, 0, 1, 1, 2, 3, 5], p=[.25, .15, .1, .15, .1, .1, .1, .05])) + (int(rq.integers(3, 7)) if congested_flag(c) else 0)
            q_days = max(0, (X - P).days - k)
        direct_q = {"accept_delay": 0.0, "expedite": cq.rm_expedite_premium_pct(lead, False) * lines_value,
                    "switch_supplier": sum(cfg["rm_ops"]["spot_price_premium"] * nl["qty"] * nl["price"] for nl in c["new_lines"]) + (0.02 * lines_value if resp == "cancel_and_switch" else 0),
                    "reallocate_stock": (cfg["rm_ops"]["transfer_cost_per_unit_value"] * c["transfer"]["qty"] * lines_value / max(1e-9, sum(s_["qty"] for s_ in c["slipped"]))) if c.get("transfer") else 0.0}[dtype]
        chosen = _opt(dtype, direct_q + q_days * daily_est, q_days, "low" if dtype == "accept_delay" else "medium",
                      next((o.get("note", "") for o in opts if o["option"] == dtype), ""))
        opts = [o for o in opts if o["option"] != dtype] + [chosen]
        best = min(opts, key=lambda o: cq.total_score(o["est_cost"], 0, 0, o["risk"]))
        # ---- actuals from the executed rows
        days = int((X - P).days)
        congested = "P02" in c["tags"]
        direct = 0.0
        fp = sorted({x for s_ in c["slipped"] for x in s_["revision_ids"]}) + sorted({s_["rm_purchase_order_id"] for s_ in c["slipped"]})
        if dtype == "expedite":
            direct = cq.rm_expedite_premium_pct(lead, congested) * lines_value
        elif dtype == "switch_supplier":
            direct = sum(cfg["rm_ops"]["spot_price_premium"] * nl["qty"] * nl["price"] for nl in c["new_lines"])
            fp += [nl["rm_purchase_order_id"] for nl in c["new_lines"]]
            if resp == "cancel_and_switch":
                direct += 0.02 * lines_value
        elif dtype == "reallocate_stock" and c.get("transfer"):
            direct = cfg["rm_ops"]["transfer_cost_per_unit_value"] * c["transfer"]["qty"] * lines_value / max(1e-9, sum(s_["qty"] for s_ in c["slipped"]))
            fp.append(c["transfer"]["transfer_id"])
        if c.get("new_lines") and dtype != "switch_supplier":
            fp += [nl["rm_purchase_order_id"] for nl in c["new_lines"]]
        fp.append(c["production_run_id"])
        lost = 0.0
        sh = shocks_by_cas.get(c["cascade_id"])
        if sh:
            lost = sh["units_cut"] * up * float(prod.at[prim.product_id, "gross_margin_pct"]) / 100
        act_cost = direct + days * daily_all + lost
        exp_cost, exp_days = chosen["est_cost"], chosen["est_stockout_days"]
        label = cq.outcome_label(exp_cost, act_cost, exp_days, days)
        driver = None
        if label != "success":
            if dtype == "expedite" and congested:
                driver = "port_congestion"
            elif sh:
                driver = "customer_allocation"
            elif c.get("co_victims") and not sees_all:
                driver = "other_runs_blocked"
            elif days > exp_days + 0.5:
                driver = "slower_than_quoted"
            else:
                driver = "cost_overrun"
        assessed = X + 7 * DAY
        out.append(_mk(key=f"dec:cas:{c['cascade_id']}", event_key=e["key"], decided_at=d, type=dtype,
                       role={"accept_delay": "Supply Planner", "expedite": "Buyer", "switch_supplier": "Category Manager", "reallocate_stock": "Materials Manager"}[dtype],
                       options=opts, chosen=chosen["option"] + (" + cancel original PO" if resp == "cancel_and_switch" else ""),
                       expected_cost=exp_cost, expected_days=exp_days, actual_cost=round(act_cost, 2), actual_days=days,
                       outcome=label, assessed_at=assessed, footprint=fp, exante_best=(best is chosen),
                       ctx_key=(dtype, sup), context={"season": _season(P), "congested": congested, "co_victims": len(c.get("co_victims", [])), "tags": tuple(c["tags"])},
                       failure_driver=driver, meta={"cascade": c, "supplier_id": sup, "rm_id": c["rm_id"], "plant_id": c["plant_id"], "lead": lead, "eta": eta,
                                                    "lines_value": lines_value, "daily_p": daily_p, "daily_all": daily_all, "direct": direct, "lost": lost,
                                                    "cancelled": resp == "cancel_and_switch", "product_id": prim.product_id}))
    return out


def mitigation_decisions(ctx, N, book, env) -> list[dict]:
    cfg = ctx.cfg
    R, S = env["R"], env["S"]
    win_end = pd.Timestamp(ctx.calib["window_end"])
    cat = env["cat"]
    prod = S["prod"].set_index("product_id")
    cas = R["cascades"]
    ss_info = R["ss"]
    out = []
    # ---- safety-stock builds
    for b in R["mitigations"]["ss_builds"]:
        d = pd.Timestamp(b["date"])
        key = (b["rm_id"], b["plant_id"])
        dbar = ss_info[key][1]
        price = float(cat[cat.rm_id == b["rm_id"]].unit_price.median())
        extra_units = (b["new_ss_days"] - b["old_ss_days"]) * dbar
        horizon = min(365, (win_end - d).days)
        hold = cq.holding_cost(extra_units * price, horizon, cfg)
        after = [c for c in cas if (c["rm_id"], c["plant_id"]) == key and d <= c["P_date"] <= d + horizon * DAY]
        before = [c for c in cas if (c["rm_id"], c["plant_id"]) == key and d - 365 * DAY <= c["P_date"] < d]
        vals = [book.by_key[f"cas:{c['cascade_id']}"]["meta"] for c in after]
        blocked = sum(v["blocked_days"] for v in vals)
        bcost = sum(v["blocked_days"] * cq.block_cost_per_day(v["value_blocked"], cfg) for v in vals)
        est_risk_days = max(2.0, 1.5 * len(before) + 1)
        hist_days = sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] for c in before) * horizon / 365
        typical = cq.block_cost_per_day(float(np.median([book.by_key[f"cas:{c['cascade_id']}"]["meta"]["value_blocked"] for c in cas])), cfg)
        opts = [_opt("build_safety_stock", hold, 0, "low", f"+{b['new_ss_days'] - b['old_ss_days']:.0f} days of cover ({extra_units:,.1f} {N.uom[b['rm_id']]})"),
                _opt("accept_delay", est_risk_days * typical, est_risk_days, "high", "keep current buffer"),
                _opt("expedite", 0.25 * extra_units * price + est_risk_days * 0.5 * typical, est_risk_days * 0.5, "medium", "expedite case by case")]
        act = hold + bcost
        exp_days = round(0.4 * hist_days, 1)
        exp_c = hold + exp_days * typical
        label = cq.outcome_label(exp_c, act, exp_days, blocked)
        opts[0] = _opt("build_safety_stock", exp_c, exp_days, "low", opts[0]["note"])
        out.append(_mk(key=f"dec:ssb:{key[0]}:{key[1]}", event_key=f"ssb:{key[0]}:{key[1]}", decided_at=d - 3 * DAY, type="build_safety_stock", role="Materials Manager",
                       options=opts, chosen="build_safety_stock", expected_cost=round(exp_c, 2), expected_days=exp_days, actual_cost=round(act, 2), actual_days=blocked,
                       outcome=label, assessed_at=min(win_end, d + horizon * DAY), exante_best=True,
                       footprint=[f"{key[0]}|{key[1]}|{(d + pd.Timedelta(days=6 - d.dayofweek)):%Y-%m-%d}", f"{key[0]}|{key[1]}|{(d - pd.Timedelta(days=d.dayofweek + 1)):%Y-%m-%d}"],
                       ctx_key=("build_safety_stock", key[0]), context={"season": _season(d)}, failure_driver=("disruption_exceeded_buffer" if label != "success" else None),
                       meta={"build": b, "after": [c["cascade_id"] for c in after], "before": [c["cascade_id"] for c in before], "extra_units": extra_units, "rm_id": key[0], "plant_id": key[1]}))
    # ---- allocation changes
    lf = R["line_facts"]
    lfd = lf[lf.arrival.notna()].assign(delay=lambda x: (x.arrival - x.promised).dt.days)
    for a in R["mitigations"]["alloc_changes"]:
        d = pd.Timestamp(a["date"])
        rm = a["rm_id"]
        new_rows = cat[(cat.rm_id == rm) & (cat.price_valid_from == d)]
        partner = a["new_partner"]
        p_old = float(cat[(cat.rm_id == rm) & (cat.supplier_id == a["old_primary"]) & (cat.price_valid_from <= d) & (cat.price_valid_to >= d)].unit_price.iloc[0])
        p_new = float(cat[(cat.rm_id == rm) & (cat.supplier_id == partner) & (cat.price_valid_from <= d) & (cat.price_valid_to >= d)].unit_price.iloc[0])
        hz = min(180, (win_end - d).days)
        moved = lfd[(lfd.rm_id == rm) & (lfd.supplier_id == partner) & (lfd.ordered >= d) & (lfd.ordered < d + hz * DAY)]
        vol = float(moved.qty.sum())
        before_q = lfd[(lfd.rm_id == rm) & (lfd.ordered < d) & (lfd.ordered >= d - 180 * DAY)]
        est_vol = float(before_q.qty.sum()) * 0.4
        after_all = lfd[(lfd.rm_id == rm) & (lfd.ordered >= d) & (lfd.ordered < d + hz * DAY)]
        late_before = float((before_q.delay > 2).mean()) if len(before_q) else 0.0
        late_after = float((after_all.delay > 2).mean()) if len(after_all) else 0.0
        after_cas = [c for c in cas if c["rm_id"] == rm and d <= c["P_date"] <= d + hz * DAY]
        blocked = sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] for c in after_cas)
        before_cas = [c for c in cas if c["rm_id"] == rm and d - hz * DAY <= c["P_date"] < d]
        mean_block = np.mean([cq.block_cost_per_day(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["value_blocked"], cfg) for c in cas if c["rm_id"] == rm] or [0])
        exp_days = round(0.5 * sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] for c in before_cas), 1)
        exp_cost = max(0.0, (p_new - p_old)) * est_vol + (5000 if a["qualified_new"] else 0) + exp_days * mean_block
        act_cost = max(0.0, (p_new - p_old)) * vol + (5000 if a["qualified_new"] else 0) + sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] * cq.block_cost_per_day(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["value_blocked"], cfg) for c in after_cas)
        if late_after <= 0.7 * late_before:
            label = cq.outcome_label(exp_cost, act_cost, exp_days, blocked)
        elif late_after < late_before:
            label = "partial"
        else:
            label = "failed"
        dtype = a["kind"]
        opts = [_opt(dtype, exp_cost, 0, "medium", f"move volume to {partner}"),
                _opt("renegotiate", 2000, 2, "medium", f"performance plan with {a['old_primary']}"),
                _opt("build_safety_stock", 0.2 * est_vol * p_old * 0.1, 0, "low", "carry more buffer instead")]
        opts[0] = _opt(dtype, exp_cost, exp_days, "medium", opts[0]["note"])
        out.append(_mk(key=f"dec:alc:{rm}", event_key=f"alc:{rm}", decided_at=d - 14 * DAY, type=dtype, role="Category Manager",
                       options=opts, chosen=dtype, expected_cost=round(exp_cost, 2), expected_days=exp_days, actual_cost=round(act_cost, 2), actual_days=blocked,
                       outcome=label, assessed_at=min(win_end, d + hz * DAY), exante_best=False, footprint=new_rows.catalog_id.tolist(),
                       ctx_key=(dtype, rm), context={"season": _season(d)}, failure_driver=("new_source_also_late" if label != "success" else None),
                       meta={"alloc": a, "late_before": late_before, "late_after": late_after, "vol": vol, "p_old": p_old, "p_new": p_new, "rm_id": rm, "supplier_id": a["old_primary"]}))
    # ---- substitutions
    bom = env["bom"]
    rmv = env["rmv"]
    for bc in S["bom_changes"]:
        if bc["kind"] != "substitution":
            continue
        d = pd.Timestamp(bc["date"])
        pid = bc["product_id"]
        pl = prod.at[pid, "plant_id"]
        rows = bom[(bom.product_id == pid) & (bom.effective_from == d)]
        p_old = float(cat[cat.rm_id == bc["old_rm"]].unit_price.median())
        p_new = float(cat[cat.rm_id == bc["new_rm"]].unit_price.median())
        hz = min(365, (win_end - d).days)
        cons = rmv[(rmv.rm_id == bc["new_rm"]) & (rmv.plant_id == pl) & (rmv.movement_type == "consumption") & (rmv.movement_at >= d) & (rmv.movement_at < d + hz * DAY)]
        vol = float(-cons.quantity_change.sum())
        scrap = rmv[(rmv.rm_id == bc["new_rm"]) & (rmv.plant_id == pl) & (rmv.movement_type == "scrap") & (rmv.movement_at >= d) & (rmv.movement_at < d + hz * DAY)]
        scrap_cost = float(-scrap.quantity_change.sum()) * p_new
        after = [c for c in cas if c["rm_id"] == bc["new_rm"] and c["plant_id"] == pl and d <= c["P_date"] <= d + hz * DAY]
        blocked = sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] for c in after)
        hist = [c for c in cas if c["rm_id"] == bc["new_rm"] and c["plant_id"] == pl and d - hz * DAY <= c["P_date"] < d]
        exp_days = round(sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] for c in hist) * 0.5, 1)
        exp_cost = max(0.0, p_new - p_old) * vol * 0.9 + 3000
        act_cost = max(0.0, p_new - p_old) * vol + 3000 + scrap_cost * 4
        label = cq.outcome_label(exp_cost, act_cost, exp_days, blocked)
        opts = [_opt("substitute_rm", exp_cost, 0, "medium", f"{bc['new_rm']} after QA trial"),
                _opt("switch_supplier", 8000, 0, "high", f"qualify second source for {bc['old_rm']} (4-6 months)"),
                _opt("build_safety_stock", 0.2 * vol * p_old * 0.25, 1, "low", f"extra cover of {bc['old_rm']}")]
        opts[0] = _opt("substitute_rm", exp_cost, exp_days, "medium", opts[0]["note"])
        out.append(_mk(key=f"dec:sub:{pid}", event_key=f"sub:{pid}", decided_at=d - 21 * DAY, type="substitute_rm", role="Quality Lead",
                       options=opts, chosen="substitute_rm", expected_cost=round(exp_cost, 2), expected_days=exp_days, actual_cost=round(act_cost, 2), actual_days=blocked,
                       outcome=label, assessed_at=min(win_end, d + hz * DAY), exante_best=True, footprint=rows.bom_id.tolist(),
                       ctx_key=("substitute_rm", bc["new_rm"]), context={"season": _season(d), "p07": bool(bc.get("high_scrap"))},
                       failure_driver=("substitute_quality" if scrap_cost > 0 and label != "success" else ("new_rm_shortage" if label != "success" else None)),
                       meta={"change": bc, "vol": vol, "scrap_cost": scrap_cost, "rm_id": bc["old_rm"], "product_id": pid, "plant_id": pl}))
    return out


def fg_decisions(ctx, N, book, env, fgl: FGLedger) -> list[dict]:
    """FG-side decisions whose footprints are EXISTING v1.0.0 rows."""
    cfg = ctx.cfg
    src = ctx.src
    rng = ctx.rng("fg_decisions")
    prod = env["S"]["prod"].set_index("product_id")
    po = src["purchase_orders"]
    pol = src["purchase_order_lines"]
    mv = src["inventory_movements"]
    win_end = pd.Timestamp(ctx.calib["window_end"])
    whs = src["warehouses"]
    w_exp = whs.sort_values("capacity_units").warehouse_id.iloc[0]
    env["P05"] = {"warehouse_id": w_exp, "multiplier": cfg["memory"]["expedite_warehouse_multiplier"]}
    out = []
    lines = pol.merge(po, on="purchase_order_id")
    lines = lines[lines.received_at.notna()]
    lines = lines[lines.product_id.map(prod.abc_class).isin(["A", "B"])]
    rcpt = mv[mv.movement_type == "receipt"].set_index("purchase_order_line_id").movement_id.to_dict()
    # ---- expedites
    samp = lines.sample(n=min(len(lines), 6000), random_state=int(rng.integers(1 << 30)))
    n_wexp_seen = 0
    for r in samp.sort_values("expected_at").itertuples():
        t = max(r.ordered_at + DAY, r.expected_at - int(rng.integers(4, 9)) * DAY)
        if t >= r.expected_at:
            continue
        rop = fgl.rop[r.product_id]
        b = fgl.bal_at(r.product_id, r.warehouse_id, t)
        if b > rop:
            continue
        value = r.quantity_ordered * r.unit_cost
        mult = env["P05"]["multiplier"] if r.warehouse_id == w_exp else 1.0
        est_mult = mult if (r.warehouse_id == w_exp and n_wexp_seen >= 3) else 1.0
        if r.warehouse_id == w_exp:
            n_wexp_seen += 1
        exp_cost = cq.fg_expedite_cost(value, est_mult, cfg)
        act_cost = cq.fg_expedite_cost(value, mult, cfg)
        late = int((r.received_at - r.expected_at).days)
        so = fgl.days_le(r.product_id, r.warehouse_id, t, r.received_at + 7 * DAY, 0)
        if late > 3 or so > 0:
            label = "failed"
        elif late > 0 or act_cost > 1.25 * exp_cost:
            label = "partial"
        else:
            label = "success"
        daily_sales = max(0.5, 13 / 30)
        wait_days = max(0.0, (r.expected_at - t).days - b / daily_sales)
        up = float(prod.at[r.product_id, "unit_price"])
        opts = [_opt("expedite", exp_cost, 0, "low", f"supplier premium {cfg['memory']['expedite_cost_rate'] * est_mult * 100:.0f}% of line value"),
                _opt("accept_delay", wait_days * daily_sales * up * 0.1, round(wait_days, 1), "medium", f"on hand {b:.0f} vs ROP {rop}"),
                _opt("reallocate_stock", cfg["memory"]["transfer_cost_rate"] * r.quantity_ordered * r.unit_cost * 0.3, 0, "medium", "pull units from another DC")]
        driver = None if label == "success" else ("supplier_missed_expedite" if late > 0 else "warehouse_surcharge")
        fp = [r.purchase_order_id] + ([rcpt[r.purchase_order_line_id]] if r.purchase_order_line_id in rcpt else [])
        out.append(_mk(key=f"dec:fgx:{r.purchase_order_line_id}", event_key=None, decided_at=t, type="expedite", role="Buyer", options=opts, chosen="expedite",
                       expected_cost=exp_cost, expected_days=0, actual_cost=act_cost, actual_days=so, outcome=label, assessed_at=min(win_end, r.received_at + 7 * DAY),
                       exante_best=True, footprint=fp, ctx_key=("expedite", r.supplier_id), context={"season": _season(t), "warehouse_surcharge": r.warehouse_id == w_exp},
                       failure_driver=driver, meta={"fg_kind": "expedite", "line": r._asdict(), "balance": b, "rop": rop, "late": late, "w_exp": w_exp, "supplier_id": r.supplier_id,
                                                    "product_id": r.product_id, "warehouse_id": r.warehouse_id}))
    # ---- reallocations (existing transfer pairs into low-stock destinations)
    tr = mv[mv.movement_type == "transfer"]
    pairs = tr.pivot_table(index="transfer_id", columns=np.sign(tr.quantity_change), values=["warehouse_id", "movement_id"], aggfunc="first")
    pairs.columns = [f"{a}_{int(b)}" for a, b in pairs.columns]
    info = tr[tr.quantity_change > 0].set_index("transfer_id")[["product_id", "movement_at", "quantity_change"]]
    pairs = pairs.join(info)
    src_stats = defaultdict(lambda: [0, 0])
    recs = []
    for tid, r in pairs.iterrows():
        pid, dst, srcw, t, q = r.product_id, r["warehouse_id_1"], r["warehouse_id_-1"], r.movement_at, int(r.quantity_change)
        rop = fgl.rop[pid]
        b_dst = fgl.bal_at(pid, dst, t - DAY)
        src_low = fgl.days_le(pid, srcw, t, min(win_end, t + 28 * DAY), rop) > 0
        src_stats[srcw][0] += int(src_low)
        src_stats[srcw][1] += 1
        if b_dst <= rop:
            recs.append((tid, pid, dst, srcw, t, q, rop, b_dst, src_low, r["movement_id_1"], r["movement_id_-1"]))
    rates = {w: a / max(1, n) for w, (a, n) in src_stats.items()}
    w_src = max(rates, key=rates.get)
    env["P08"] = {"warehouse_id": w_src, "source_depletion_rate": round(rates[w_src], 3), "other_rate": round(float(np.mean([v for k, v in rates.items() if k != w_src])), 3)}
    for tid, pid, dst, srcw, t, q, rop, b_dst, src_low, m_in, m_out in recs:
        cost = round(cfg["memory"]["transfer_cost_rate"] * q * float(prod.at[pid, "unit_cost"]), 2)
        d_so = fgl.days_le(pid, dst, t, min(win_end, t + 28 * DAY), 0)
        s_zero = fgl.days_le(pid, srcw, t, min(win_end, t + 28 * DAY), 0) > 0
        label = "failed" if (d_so > 0 or s_zero) else ("partial" if src_low else "success")
        opts = [_opt("reallocate_stock", cost, 0, "low", f"move {q} units {srcw}->{dst}"),
                _opt("expedite", cfg["memory"]["expedite_cost_rate"] * q * float(prod.at[pid, "unit_cost"]), 0, "medium", "expedite next inbound PO"),
                _opt("accept_delay", 0.1 * q * float(prod.at[pid, "unit_price"]), 1.0, "medium", "wait for replenishment")]
        out.append(_mk(key=f"dec:fgt:{tid}", event_key=None, decided_at=t - DAY, type="reallocate_stock", role="Supply Planner", options=opts, chosen="reallocate_stock",
                       expected_cost=cost, expected_days=0, actual_cost=cost, actual_days=d_so, outcome=label, assessed_at=min(win_end, t + 28 * DAY), exante_best=True,
                       footprint=[tid, m_out, m_in], ctx_key=("reallocate_stock", srcw), context={"season": _season(t), "source_is_w_src": srcw == w_src},
                       failure_driver=(None if label == "success" else "source_depleted"),
                       meta={"fg_kind": "transfer", "transfer_id": tid, "product_id": pid, "dst": dst, "src": srcw, "qty": q, "rop": rop, "b_dst": b_dst, "src_low": src_low, "warehouse_id": dst}))
    # ---- cancellations
    canc = po[po.status == "cancelled"]
    for r in canc.itertuples():
        d = r.ordered_at + max(2, int((r.expected_at - r.ordered_at).days) // 3) * DAY
        ls = pol[pol.purchase_order_id == r.purchase_order_id]
        ratio = np.mean([fgl.bal_at(p, r.warehouse_id, d) / max(1, fgl.rop[p]) for p in ls.product_id])
        dips = [fgl.days_le(p, r.warehouse_id, d, min(win_end, d + 60 * DAY), fgl.rop[p]) for p in ls.product_id]
        zeros = [fgl.days_le(p, r.warehouse_id, d, min(win_end, d + 60 * DAY), 0) for p in ls.product_id]
        value = float((ls.quantity_ordered * ls.unit_cost).sum())
        fee = round(0.01 * value, 2)
        label = "failed" if sum(zeros) > 0 else ("partial" if sum(dips) > 0 else "success")
        opts = [_opt("cancel_po", fee, 0, "low" if ratio >= 1.5 else "medium", f"stock {ratio:.1f}x ROP"),
                _opt("accept_delay", cq.holding_cost(value, 90, cfg), 0, "low", "keep PO, carry the stock"),
                _opt("reduce_allocation", fee * 0.5 + cq.holding_cost(value * 0.5, 90, cfg), 0, "low", "halve quantities")]
        out.append(_mk(key=f"dec:fgc:{r.purchase_order_id}", event_key=None, decided_at=d, type="cancel_po", role="Buyer", options=opts, chosen="cancel_po",
                       expected_cost=fee, expected_days=0, actual_cost=fee, actual_days=sum(zeros), outcome=label, assessed_at=min(win_end, d + 60 * DAY),
                       exante_best=ratio >= 1.5, footprint=[r.purchase_order_id], ctx_key=("cancel_po", r.supplier_id), context={"season": _season(d)},
                       failure_driver=(None if label == "success" else "demand_recovered"),
                       meta={"fg_kind": "cancel", "po": r.purchase_order_id, "supplier_id": r.supplier_id, "warehouse_id": r.warehouse_id, "ratio": ratio, "value": value,
                             "products": ls.product_id.tolist()}))
    # ---- accept shortfall on partial receipts
    part = po[po.status == "partial"].sample(n=min(300, int((po.status == "partial").sum())), random_state=int(rng.integers(1 << 30)))
    for r in part.itertuples():
        d = r.received_at + DAY
        ls = pol[pol.purchase_order_id == r.purchase_order_id]
        short_units = int((ls.quantity_ordered - ls.quantity_received).sum())
        zeros = sum(fgl.days_le(p, r.warehouse_id, d, min(win_end, d + 60 * DAY), 0) for p in ls.product_id)
        dips = sum(fgl.days_le(p, r.warehouse_id, d, min(win_end, d + 60 * DAY), fgl.rop[p]) for p in ls.product_id)
        margin = float(sum(q * prod.at[p, "unit_price"] * prod.at[p, "gross_margin_pct"] / 100 for p, q in zip(ls.product_id, ls.quantity_ordered - ls.quantity_received)))
        exp_cost = round(0.02 * margin, 2)
        act_cost = round(exp_cost + (0.2 * margin if zeros else 0), 2)
        label = cq.outcome_label(exp_cost, act_cost, 0, zeros)
        if label == "success" and dips > 30:
            label = "partial"
        opts = [_opt("accept_delay", exp_cost, 0, "low", f"accept shortfall of {short_units} units"),
                _opt("expedite", cfg["memory"]["expedite_cost_rate"] * float((ls.quantity_ordered - ls.quantity_received).mul(ls.unit_cost).sum()), 0, "medium", "rush the balance"),
                _opt("renegotiate", 1500, 0, "medium", "claim short-shipment credit")]
        rc = [rcpt[x] for x in ls.purchase_order_line_id if x in rcpt]
        out.append(_mk(key=f"dec:fga:{r.purchase_order_id}", event_key=None, decided_at=d, type="accept_delay", role="Supply Planner", options=opts, chosen="accept_delay",
                       expected_cost=exp_cost, expected_days=0, actual_cost=act_cost, actual_days=zeros, outcome=label, assessed_at=min(win_end, d + 60 * DAY), exante_best=True,
                       footprint=[r.purchase_order_id] + rc, ctx_key=("accept_delay", r.supplier_id), context={"season": _season(d)},
                       failure_driver=(None if label == "success" else "stock_ran_low"),
                       meta={"fg_kind": "accept", "po": r.purchase_order_id, "supplier_id": r.supplier_id, "warehouse_id": r.warehouse_id, "short_units": short_units,
                             "products": ls.product_id.tolist()}))
    return out


# =============================================================================
# attribution + selection
# =============================================================================
def attribute(decs: list[dict]) -> None:
    decs.sort(key=lambda d: d["decided_at"])
    hist = defaultdict(list)
    for d in decs:
        d["attribution"] = "as_expected" if d["outcome"] == "success" else None
        if d["outcome"] != "success":
            prev_ok = [p for p in hist[d["ctx_key"]] if p["outcome"] == "success" and p["assessed_at"] <= d["decided_at"]]
            if prev_ok and any(p["context"] != d["context"] for p in prev_ok):
                d["attribution"] = "context_shift"
                d["precedent_key"] = prev_ok[-1]["key"]
            elif d["exante_best"] and d["failure_driver"] in ("customer_allocation", "port_congestion", "slower_than_quoted", "disruption_exceeded_buffer",
                                                               "supplier_missed_expedite", "source_depleted", "demand_recovered", "stock_ran_low", "new_source_also_late", "substitute_quality"):
                d["attribution"] = "bad_luck"
            else:
                d["attribution"] = "decision_quality"
        hist[d["ctx_key"]].append(d)


def select_decisions(ctx, cands: list[dict], env) -> list[dict]:
    """Pick a realistic outcome mix per source, then make sure context-dependent
    cases (worked before / failed under new conditions, or sound-but-unlucky) are
    well represented and that each context shift keeps its successful precedent."""
    cfg = ctx.cfg
    rng = ctx.rng("select_decisions")
    maxd = cfg["scale"]["max_decisions"] - env.get("n_reneg", 0)
    mits_all = [d for d in cands if d["key"].startswith(("dec:ssb", "dec:alc", "dec:sub"))]
    cap = int(round(0.28 * maxd))
    mits_all.sort(key=lambda d: (not d["context"].get("p07", False), d["outcome"] != "success", rng.random()))
    mits = mits_all[:cap] if len(mits_all) > cap else mits_all
    pools = {p: [d for d in cands if d["key"].startswith(f"dec:{p}")] for p in ("cas", "fgx", "fgt", "fgc", "fga")}
    share = {"cas": 0.46, "fgx": 0.14, "fgt": 0.12, "fgc": 0.07, "fga": 0.06}
    mix = {"success": 0.55, "partial": 0.25, "failed": 0.20}
    budget = maxd - len(mits)
    plan = env["S"]["pattern_plan"]
    p_sups = {plan.get(k, {}).get("supplier_id") for k in ("P01", "P03", "P09", "P11", "P04")}
    chosen = {d["key"]: d for d in mits}
    for name, pool in pools.items():
        k = int(round(budget * share[name] / sum(share.values())))
        for outc, f in mix.items():
            sub = [d for d in pool if d["outcome"] == outc]
            if not sub:
                continue
            pr = np.array([(3 if d["meta"].get("supplier_id") in p_sups else 0) + (6 if d["context"].get("warehouse_surcharge") else 0)
                           + (2 if d["meta"].get("cascade", {}).get("transfer") else 0) + rng.random() for d in sub])
            for i in np.argsort(-pr)[: int(round(k * f))]:
                chosen[sub[i]["key"]] = sub[i]
    sel = list(chosen.values())
    attribute(sel)
    need = cfg["memory"]["context_dependent_min_share"]
    by_ctx = defaultdict(list)
    for d in cands:
        by_ctx[d["ctx_key"]].append(d)
    pairs = []
    for d in sorted(cands, key=lambda z: z["decided_at"]):
        if d["outcome"] == "success":
            continue
        prev = [p for p in by_ctx[d["ctx_key"]] if p["outcome"] == "success" and p["assessed_at"] <= d["decided_at"] and p["context"] != d["context"]]
        if prev:
            pairs.append((d, prev[-1]))
    rng.shuffle(pairs)
    protected = set()
    for d, p in pairs:
        cs = sum(x["attribution"] == "context_shift" for x in sel) / max(1, len(sel))
        cdep = sum(x["attribution"] in ("context_shift", "bad_luck") for x in sel) / max(1, len(sel))
        if cs >= 0.12 and cdep >= need:
            break
        if d["key"] in chosen:
            continue
        chosen[d["key"]] = d
        chosen.setdefault(p["key"], p)
        protected |= {d["key"], p["key"]}
        # hold the budget: drop an unprotected as-expected cascade decision
        while len(chosen) > maxd:
            ae = [x for x in chosen.values() if x.get("attribution") == "as_expected" and x["key"].startswith("dec:cas") and x["key"] not in protected]
            if not ae:
                break
            chosen.pop(ae[int(rng.integers(len(ae)))]["key"])
        sel = list(chosen.values())
        attribute(sel)
    return sel


# =============================================================================
# negotiations
# =============================================================================
def build_negotiations(ctx, N, book, env) -> list[dict]:
    cfg = ctx.cfg
    rng = ctx.rng("negotiations")
    S, R = env["S"], env["R"]
    cat = env["cat"]
    lf = R["line_facts"]
    lfd = lf[lf.arrival.notna()].assign(delay=lambda x: (x.arrival - x.promised).dt.days)
    win_end = pd.Timestamp(ctx.calib["window_end"])
    negs = []

    def vol12(rm, sup, d):
        g = lf[(lf.rm_id == rm) & (lf.supplier_id == sup) & (lf.ordered >= d - 365 * DAY) & (lf.ordered < d)]
        return float(g.qty.sum())

    for sp in S["price_spikes"]:
        d = pd.Timestamp(sp["date"])
        k = float(rng.uniform(4, 10))
        row = cat[(cat.rm_id == sp["rm_id"]) & (cat.supplier_id == sp["supplier_id"]) & (cat.price_valid_from == d)].iloc[0]
        vol = round(vol12(sp["rm_id"], sp["supplier_id"], d) * 0.8, -1)
        negs.append({"key": f"neg:price:{sp['rm_id']}:{d:%Y%m%d}", "supplier_id": sp["supplier_id"], "rm_id": sp["rm_id"], "started_at": d - 35 * DAY, "concluded_at": d - 4 * DAY,
                     "topic": "price_increase", "our_ask": "Hold current price to contract end; any increase capped at 5% with 90 days notice",
                     "their_offer": f"+{sp['pct'] + k:.1f}% effective {d:%Y-%m-%d} citing feedstock costs",
                     "concessions": [{"party": "supplier", "item": "reduced increase", "value": f"-{k:.1f} pts"}, {"party": "us", "item": "12-month volume commitment", "value": f"{vol:,.0f} {N.uom[sp['rm_id']]}"}],
                     "final_terms": f"+{sp['pct']:.1f}% to {sp['new_price']:.4f} USD/{N.uom[sp['rm_id']]} effective {d:%Y-%m-%d}; price held to {row.price_valid_to:%Y-%m-%d}; buyer commits {vol:,.0f} {N.uom[sp['rm_id']]} over 12 months",
                     "outcome": "partial", "contract_id": row.contract_id, "event_key": f"price:{sp['rm_id']}:{d:%Y%m%d}",
                     "commit": [("supplier", sp["supplier_id"], f"{N.S(sp['supplier_id'])} holds {N.R(sp['rm_id'])} at {sp['new_price']:.4f} USD/{N.uom[sp['rm_id']]} until {row.price_valid_to:%Y-%m-%d}", None, row.price_valid_to, "price_hold", {"rm_id": sp["rm_id"], "from": d, "price": sp["new_price"]}),
                                ("supplier", sp["supplier_id"], f"We order at least {vol:,.0f} {N.uom[sp['rm_id']]} of {N.R(sp['rm_id'])} from {N.S(sp['supplier_id'])} in the 12 months from {d:%Y-%m-%d}", vol, d + 365 * DAY, "our_volume", {"rm_id": sp["rm_id"], "from": d})],
                     "meta": {"spike": sp, "k": k}})
    # delivery-performance talks: planted suppliers + suppliers whose allocation was cut
    plan = S["pattern_plan"]
    targets = []
    for pk in ("P01", "P09", "P11"):
        if pk in plan:
            ev = sorted([e for e in book.E if e["meta"].get("pattern") == pk], key=lambda e: e["start_date"])
            if ev:
                targets.append((plan[pk]["supplier_id"], ev[0]["end_date"] + 10 * DAY, ev[0]["key"], pk))
    for a in R["mitigations"]["alloc_changes"]:
        targets.append((a["old_primary"], pd.Timestamp(a["date"]) - 60 * DAY, f"alc:{a['rm_id']}", None))
    for sup, d, ek, pk in targets:
        rms_ = lf[lf.supplier_id == sup].rm_id.unique().tolist()
        concl = d + int(rng.integers(12, 30)) * DAY
        tgt = 92 if pk != "P09" else 95
        negs.append({"key": f"neg:perf:{sup}:{d:%Y%m%d}", "supplier_id": sup, "rm_id": rms_[0] if len(rms_) == 1 else None, "started_at": d, "concluded_at": concl,
                     "topic": "delivery_performance", "our_ask": "Recovery plan; >=95% on-time lots; 2% credit per week late",
                     "their_offer": "Add a second shift from next quarter; on-time target 88%",
                     "concessions": [{"party": "supplier", "item": "on-time target", "value": f"{tgt}%"}, {"party": "us", "item": "rolling 8-week forecast shared weekly", "value": "yes"}],
                     "final_terms": f"On-time target {tgt}% of lots within 1 day of promise for 6 months from {concl:%Y-%m-%d}; late-delivery credit per contract", "outcome": "agreed",
                     "contract_id": None, "event_key": ek,
                     "commit": [("supplier", sup, f"{N.S(sup)} delivers >= {tgt}% of RM lots within 1 day of the promised date from {concl:%Y-%m-%d} to {(concl + 182 * DAY):%Y-%m-%d}", tgt, concl + 182 * DAY, "otif", {"from": concl, "target": tgt / 100})],
                     "meta": {"pattern": pk}})
    # capacity reservation with the new partner of each allocation change
    for a in R["mitigations"]["alloc_changes"]:
        d = pd.Timestamp(a["date"])
        q = round(vol12(a["rm_id"], a["old_primary"], d) / 12 * 0.4, -1)
        negs.append({"key": f"neg:cap:{a['rm_id']}", "supplier_id": a["new_partner"], "rm_id": a["rm_id"], "started_at": d - 45 * DAY, "concluded_at": d - 10 * DAY,
                     "topic": "capacity_reservation", "our_ask": f"Reserve {q:,.0f} {N.uom[a['rm_id']]}/month at current price", "their_offer": f"Reserve {q * 0.8:,.0f}/month, +3% price",
                     "concessions": [{"party": "us", "item": "allocation share", "value": f"{a['update'].get(a['new_partner'], {}).get('allocation_pct', 0):.0f}%"}],
                     "final_terms": f"Allocation moved to {a['new_partner']} from {d:%Y-%m-%d}; reservation {q:,.0f} {N.uom[a['rm_id']]}/month", "outcome": "agreed", "contract_id": None,
                     "event_key": f"alc:{a['rm_id']}",
                     "commit": [("supplier", a["new_partner"], f"{N.S(a['new_partner'])} ships up to {q:,.0f} {N.uom[a['rm_id']]} of {N.R(a['rm_id'])} per month on 1-day notice from {d:%Y-%m-%d}", q, min(d + 182 * DAY, win_end + 90 * DAY), "otif", {"from": d, "target": 0.9, "rm_id": a["rm_id"]})],
                     "meta": {"alloc": a}})
    # contract renewals (sample)
    ren = cat[cat.change_driver == "contract_renewal"].drop_duplicates(["supplier_id", "contract_id"])
    k = max(0, cfg["scale"]["max_negotiations"] - len(negs) - 3)
    for r in ren.sample(n=min(k, len(ren)), random_state=int(rng.integers(1 << 30))).itertuples():
        prev = cat[(cat.rm_id == r.rm_id) & (cat.supplier_id == r.supplier_id) & (cat.price_valid_to == r.price_valid_from - DAY)]
        if prev.empty:
            continue
        pct = (r.unit_price / prev.unit_price.iloc[0] - 1) * 100
        st = r.price_valid_from - int(rng.integers(40, 70)) * DAY
        negs.append({"key": f"neg:ren:{r.contract_id}", "supplier_id": r.supplier_id, "rm_id": r.rm_id, "started_at": st, "concluded_at": r.price_valid_from - int(rng.integers(5, 20)) * DAY,
                     "topic": "annual_renewal", "our_ask": f"Flat price, payment terms 60 days", "their_offer": f"+{max(pct, 0) + float(rng.uniform(2, 5)):.1f}%",
                     "concessions": [{"party": "supplier", "item": "price", "value": f"{pct:+.1f}%"}, {"party": "us", "item": "term", "value": "renewal signed"}],
                     "final_terms": f"{r.contract_id}: {N.R(r.rm_id)} {pct:+.1f}% to {r.unit_price:.4f} USD/{N.uom[r.rm_id]} from {r.price_valid_from:%Y-%m-%d}", "outcome": "agreed",
                     "contract_id": r.contract_id, "event_key": None, "commit": [], "meta": {"pct": pct}})
    # a few still-open negotiations at the cutoff
    late = cat[(cat.price_valid_to > win_end) & (cat.qualified_flag == 1)].drop_duplicates("supplier_id").sample(n=3, random_state=int(rng.integers(1 << 30)))
    for r in late.itertuples():
        st = win_end - int(rng.integers(10, 40)) * DAY
        negs.append({"key": f"neg:open:{r.supplier_id}", "supplier_id": r.supplier_id, "rm_id": r.rm_id, "started_at": st, "concluded_at": None, "topic": "annual_renewal",
                     "our_ask": "Price freeze for next term", "their_offer": f"+{float(rng.uniform(3, 7)):.1f}% next term", "concessions": [], "final_terms": None, "outcome": "open",
                     "contract_id": r.contract_id, "event_key": None, "commit": [], "meta": {}})
    return negs


def renegotiate_decisions(ctx, negs, book, env) -> list[dict]:
    cfg = ctx.cfg
    lf = env["R"]["line_facts"]
    out = []
    for n in negs:
        if n["topic"] not in ("price_increase", "delivery_performance") or n["event_key"] not in book.by_key:
            continue
        d = n["started_at"] + 3 * DAY
        if n["topic"] == "price_increase":
            sp = n["meta"]["spike"]
            k = n["meta"]["k"]
            nxt = lf[(lf.rm_id == sp["rm_id"]) & (lf.supplier_id == sp["supplier_id"]) & (lf.ordered >= sp["date"]) & (lf.ordered < sp["date"] + 365 * DAY)]
            vol = float(nxt.qty.sum())
            act = sp["pct"] / 100 * sp["old_price"] * vol
            exp = (sp["pct"] + k) / 100 * sp["old_price"] * vol * 0.85
            label = cq.outcome_label(exp, act, 0, 0)
            opts = [_opt("renegotiate", exp, 0, "medium", "counter with volume commitment"),
                    _opt("accept_increase", (sp["pct"] + k) / 100 * sp["old_price"] * vol, 0, "low", "accept list increase"),
                    _opt("switch_supplier", 0.05 * sp["old_price"] * vol + 8000, 3, "high", "qualify alternative source")]
            fp = [n["key"], n["contract_id"]] + env["cat"][(env["cat"].rm_id == sp["rm_id"]) & (env["cat"].price_valid_from == sp["date"])].catalog_id.tolist()
            out.append(_mk(key=f"dec:{n['key']}", event_key=n["event_key"], decided_at=d, type="renegotiate", role="Category Manager", options=opts, chosen="renegotiate",
                           expected_cost=round(exp, 2), expected_days=0, actual_cost=round(act, 2), actual_days=0, outcome=label, assessed_at=sp["date"] + 30 * DAY,
                           exante_best=True, footprint=fp, ctx_key=("renegotiate", n["supplier_id"]), context={"topic": "price"}, failure_driver=None,
                           meta={"neg_key": n["key"], "supplier_id": n["supplier_id"], "rm_id": sp["rm_id"]}))
        else:
            c0 = n["commit"][0]
            st, en = c0[6]["from"], c0[4]
            g = lf[(lf.supplier_id == n["supplier_id"]) & (lf.promised >= st) & (lf.promised <= en) & lf.arrival.notna()]
            ot = float(((g.arrival - g.promised).dt.days <= 1).mean()) if len(g) else np.nan
            exp = 2000.0
            cas = [c for c in env["R"]["cascades"] if c["slipped"][0]["supplier_id"] == n["supplier_id"] and st <= c["P_date"] <= en]
            act = 2000.0 + sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] * cq.block_cost_per_day(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["value_blocked"], cfg) for c in cas)
            days = sum(book.by_key[f"cas:{c['cascade_id']}"]["meta"]["blocked_days"] for c in cas)
            if np.isnan(ot) or en > pd.Timestamp(ctx.calib["window_end"]):
                label, assessed = None, None
            else:
                label = "success" if ot >= c0[6]["target"] and days == 0 else ("partial" if ot >= c0[6]["target"] - 0.1 else "failed")
                assessed = en
            opts = [_opt("renegotiate", 2000, 0, "medium", "performance agreement with credits"),
                    _opt("reduce_allocation", 6000, 0, "medium", "shift volume to second source"),
                    _opt("build_safety_stock", 9000, 0, "low", "carry extra cover")]
            out.append(_mk(key=f"dec:{n['key']}", event_key=n["event_key"], decided_at=d, type="renegotiate", role="Category Manager", options=opts, chosen="renegotiate",
                           expected_cost=exp, expected_days=0, actual_cost=round(act, 2) if label else None, actual_days=days if label else None, outcome=label,
                           assessed_at=assessed, exante_best=True, footprint=[n["key"]], ctx_key=("renegotiate", n["supplier_id"]), context={"topic": "delivery"},
                           failure_driver=("supplier_kept_slipping" if label in ("failed", "partial") else None),
                           meta={"neg_key": n["key"], "supplier_id": n["supplier_id"], "on_time": ot, "pattern": n["meta"].get("pattern")}))
    return out


# =============================================================================
# commitments
# =============================================================================
def eta_status(po_row, revs_po: pd.DataFrame, due: pd.Timestamp, made: pd.Timestamp, win_end):
    """Status of a supplier date promise for an RM PO, from revisions and receipt."""
    later = revs_po[(revs_po.revised_at > made) & (revs_po.revised_at < due)]
    if len(later):
        return "renegotiated", later.revised_at.min()
    if po_row.status == "cancelled":
        return "renegotiated", min(due, win_end)
    if pd.notna(po_row.received_at):
        if po_row.received_at <= due and po_row.status == "received":
            return "fulfilled", po_row.received_at
        return "breached", max(po_row.received_at, due + DAY) if po_row.received_at > due else po_row.received_at
    if due > win_end:
        return "open", None
    return "breached", due + DAY


def build_commitments(ctx, N, env, sel: list[dict], negs: list[dict]) -> list[dict]:
    cfg = ctx.cfg
    rng = ctx.rng("commitments")
    win_end = pd.Timestamp(ctx.calib["window_end"])
    rpo = env["rpo"].set_index("rm_purchase_order_id")
    rev = env["rev"]
    rev_by = {k: g.sort_values("revised_at") for k, g in rev.groupby("rm_po_id")}
    empty = rev.iloc[0:0]
    lf = env["R"]["line_facts"]
    po = ctx.src["purchase_orders"].set_index("purchase_order_id")
    out = []

    def add(**kw):
        kw.setdefault("quantity", None)
        kw.setdefault("penalty", None)
        kw.setdefault("decision_key", None)
        kw.setdefault("neg_key", None)
        out.append(kw)

    # ---- from selected decisions
    for d in sel:
        m = d["meta"]
        if d["key"].startswith("dec:cas"):
            c = m["cascade"]
            s0 = c["slipped"][0]
            h = rpo.loc[s0["rm_purchase_order_id"]]
            rv = rev_by.get(s0["rm_purchase_order_id"], empty)
            slip_rev = rv[rv.reason_code != "expedite_air_freight"]
            if len(slip_rev):
                r0 = slip_rev.iloc[-1]
                st, res = eta_status(h, rv, r0.new_expected_at, r0.revised_at, win_end)
                add(kind="eta", ctype="supplier", cp=s0["supplier_id"], by=f"{N.sup[s0['supplier_id']]} account manager", made=r0.revised_at,
                    text=f"{N.S(s0['supplier_id'])} confirmed {s0['rm_purchase_order_id']} ({N.R(c['rm_id'])}) will arrive at {N.P(c['plant_id'])} by {r0.new_expected_at:%Y-%m-%d}",
                    quantity=round(s0["qty"], 3), due=r0.new_expected_at, penalty="late-delivery credit per contract", status=st, resolved=res,
                    evidence=[s0["rm_purchase_order_id"], r0.revision_id], decision_key=d["key"])
            if d["type"] == "expedite":
                ex = rv[rv.reason_code == "expedite_air_freight"]
                if len(ex):
                    r1 = ex.iloc[-1]
                    st, res = eta_status(h, rv, r1.new_expected_at, r1.revised_at, win_end)
                    add(kind="expedite", ctype="supplier", cp=s0["supplier_id"], by="Buyer", made=r1.revised_at,
                        text=f"Air expedite of {s0['rm_purchase_order_id']} booked; {N.S(s0['supplier_id'])} and forwarder committed delivery to {N.P(c['plant_id'])} by {r1.new_expected_at:%Y-%m-%d}",
                        quantity=round(s0["qty"], 3), due=r1.new_expected_at, penalty="premium refundable if later than committed", status=st, resolved=res,
                        evidence=[s0["rm_purchase_order_id"], r1.revision_id], decision_key=d["key"])
            if d["type"] == "switch_supplier":
                for nl in c["new_lines"]:
                    hh = rpo.loc[nl["rm_purchase_order_id"]]
                    st, res = eta_status(hh, rev_by.get(nl["rm_purchase_order_id"], empty), hh.promised_at, hh.ordered_at, win_end)
                    add(kind="spot", ctype="supplier", cp=nl["supplier_id"], by=f"{N.sup[nl['supplier_id']]} sales", made=hh.ordered_at,
                        text=f"{N.S(nl['supplier_id'])} committed a spot lot of {nl['qty']:,.2f} {N.uom[c['rm_id']]} {N.R(c['rm_id'])} ({nl['rm_purchase_order_id']}) by {hh.promised_at:%Y-%m-%d}",
                        quantity=round(nl["qty"], 3), due=hh.promised_at, penalty="spot terms, no penalty", status=st, resolved=res, evidence=[nl["rm_purchase_order_id"]], decision_key=d["key"])
            if d["type"] == "reallocate_stock" and c.get("transfer"):
                t = c["transfer"]
                due = env["D"](t["out_day"])
                add(kind="transfer", ctype="plant", cp=t["donor_plant"], by="Plant Scheduler", made=due - DAY,
                    text=f"{N.P(t['donor_plant'])} committed to ship {t['qty']:,.2f} {N.uom[c['rm_id']]} of {N.R(c['rm_id'])} to {N.P(c['plant_id'])} on {due:%Y-%m-%d} ({t['transfer_id']})",
                    quantity=round(t["qty"], 3), due=due, status="fulfilled", resolved=due, evidence=[t["transfer_id"]], decision_key=d["key"])
            add(kind="restart", ctype="plant", cp=c["plant_id"], by="Plant Scheduler", made=d["decided_at"],
                text=f"{N.P(c['plant_id'])} committed to run {c['production_run_id']} the day the {N.rm[c['rm_id']]} lot clears QA (planned start was {c['P_date']:%Y-%m-%d})",
                due=c["A_date"] + DAY, status="fulfilled", resolved=c["A_date"], evidence=[c["production_run_id"]], decision_key=d["key"])
        elif m.get("fg_kind") == "expedite":
            r = m["line"]
            due = r["expected_at"]
            ok = r["received_at"] <= due
            add(kind="fg_expedite", ctype="supplier", cp=r["supplier_id"], by=f"{N.sup[r['supplier_id']]} customer service", made=d["decided_at"] + DAY,
                text=f"{N.S(r['supplier_id'])} committed to deliver {r['purchase_order_id']} line {r['purchase_order_line_id']} to {N.W(r['warehouse_id'])} by {due:%Y-%m-%d} under expedite",
                quantity=int(r["quantity_ordered"]), due=due, penalty="expedite fee waived if late", status="fulfilled" if ok else "breached", resolved=r["received_at"],
                evidence=[r["purchase_order_id"]], decision_key=d["key"])

    # ---- negotiations
    for n in negs:
        for ctype, cp, text, qty, due, kind, x in n["commit"]:
            due = pd.Timestamp(due)
            if kind == "price_hold":
                later = env["cat"][(env["cat"].rm_id == x["rm_id"]) & (env["cat"].supplier_id == cp) & (env["cat"].price_valid_from > x["from"]) & (env["cat"].price_valid_from <= due)]
                if len(later) and (later.unit_price > x["price"] + 1e-9).any():
                    st, res, ev = "breached", later.price_valid_from.min(), later.catalog_id.tolist()[:3]
                elif due > win_end:
                    st, res, ev = "open", None, []
                else:
                    st, res, ev = "fulfilled", due, []
                add(kind="price_hold", ctype="supplier", cp=cp, by=f"{N.sup[cp]} key account", made=n["concluded_at"], text=text, due=due, status=st, resolved=res, evidence=ev or [n["key"]], neg_key=n["key"])
            elif kind == "our_volume":
                g = lf[(lf.rm_id == x["rm_id"]) & (lf.supplier_id == cp) & (lf.ordered >= x["from"]) & (lf.ordered < due)]
                g = g[g.rm_purchase_order_id.map(rpo.status) != "cancelled"]
                ordered = float(g.qty.sum())
                if ordered >= qty:
                    st, res = "fulfilled", g.sort_values("ordered").ordered[g.qty.cumsum().values >= qty].iloc[0]
                elif due > win_end:
                    st, res = "open", None
                else:
                    st, res = "breached", due
                add(kind="our_volume", ctype="supplier", cp=cp, by="Category Manager", made=n["concluded_at"], text=text, quantity=qty, due=due, penalty="shortfall billed per contract",
                    status=st, resolved=res, evidence=g.rm_purchase_order_id.unique().tolist()[:5] or [n["key"]], neg_key=n["key"])
            elif kind == "otif":
                g = lf[(lf.supplier_id == cp) & (lf.promised >= x["from"]) & (lf.promised <= due) & lf.arrival.notna()]
                if "rm_id" in x:
                    g = g[g.rm_id == x["rm_id"]]
                if due > win_end:
                    st, res = "open", None
                elif len(g) == 0:
                    st, res = "fulfilled", due
                else:
                    ot = float(((g.arrival - g.promised).dt.days <= 1).mean())
                    st, res = ("fulfilled" if ot >= x["target"] else "breached"), due
                add(kind="otif", ctype="supplier", cp=cp, by=f"{N.sup[cp]} operations director", made=n["concluded_at"], text=text, quantity=qty, due=due,
                    penalty="2% credit per week late", status=st, resolved=res, evidence=g.rm_purchase_order_id.unique().tolist()[:8] or [n["key"]], neg_key=n["key"])

    # ---- pools to meet the status mix: RM order acknowledgements + revision promises + FG recovery promises
    plan = env["S"]["pattern_plan"]
    p04, p09 = plan.get("P04", {}).get("supplier_id"), plan.get("P09", {}).get("supplier_id")
    pool = []
    lines = lf.merge(rpo[["status", "received_at"]], left_on="rm_purchase_order_id", right_index=True)
    ack = lines[lines.otype == "regular"]
    focus = ack[ack.supplier_id == p04]
    focus = focus.sample(n=min(len(focus), max(24, int(0.25 * len(sel)))), random_state=int(rng.integers(1 << 30)))
    other = ack[ack.supplier_id != p04].sample(n=min(len(ack), 1500), random_state=int(rng.integers(1 << 30)))
    for r in pd.concat([focus, other]).itertuples():
        h = rpo.loc[r.rm_purchase_order_id]
        st, res = eta_status(h, rev_by.get(r.rm_purchase_order_id, empty), h.promised_at, h.ordered_at + DAY, win_end)
        if st == "fulfilled" and r.qrec < r.qty - 1e-6:
            st = "breached"
        pool.append(dict(kind="ack", ctype="supplier", cp=r.supplier_id, by=f"{N.sup[r.supplier_id]} order desk", made=h.ordered_at + DAY,
                         text=f"{N.S(r.supplier_id)} acknowledged {r.rm_purchase_order_id}: {r.qty:,.2f} {N.uom[r.rm_id]} of {N.R(r.rm_id)} to {N.P(r.plant_id)} by {h.promised_at:%Y-%m-%d}",
                         quantity=round(r.qty, 3), due=h.promised_at, penalty="late-delivery credit per contract", status=st, resolved=res, evidence=[r.rm_purchase_order_id],
                         focus=(r.supplier_id == p04), decision_key=None, neg_key=None, value=r.qty * r.price))
    rv = rev[rev.reason_code != "expedite_air_freight"]
    rv9 = rv[rv.rm_po_id.map(rpo.supplier_id) == p09]
    rv9 = rv9.sample(n=min(len(rv9), max(16, int(0.15 * len(sel)))), random_state=int(rng.integers(1 << 30)))
    rvo = rv[rv.rm_po_id.map(rpo.supplier_id) != p09].sample(n=min(len(rv), 1200), random_state=int(rng.integers(1 << 30)))
    for r in pd.concat([rv9, rvo]).itertuples():
        h = rpo.loc[r.rm_po_id]
        st, res = eta_status(h, rev_by.get(r.rm_po_id, empty), r.new_expected_at, r.revised_at, win_end)
        pool.append(dict(kind="eta", ctype="supplier", cp=h.supplier_id, by=f"{N.sup[h.supplier_id]} account manager", made=r.revised_at,
                         text=f"{N.S(h.supplier_id)} re-promised {r.rm_po_id} for {r.new_expected_at:%Y-%m-%d} (was {r.old_expected_at:%Y-%m-%d}; {r.reason_code.replace('_', ' ')})",
                         quantity=None, due=r.new_expected_at, penalty="late-delivery credit per contract", status=st, resolved=res, evidence=[r.rm_po_id, r.revision_id],
                         focus=(h.supplier_id == p09), decision_key=None, neg_key=None))
    fgp = po[po.received_at.notna() & ((po.received_at - po.expected_at).dt.days >= 3)].sample(n=800, random_state=int(rng.integers(1 << 30)))
    for pid, r in fgp.iterrows():
        k = int(rng.integers(2, 8))
        made = r.expected_at + DAY
        due = r.expected_at + k * DAY
        st = "fulfilled" if r.received_at <= due else "breached"
        pool.append(dict(kind="fg_recovery", ctype="supplier", cp=r.supplier_id, by=f"{N.sup[r.supplier_id]} customer service", made=made,
                         text=f"{N.S(r.supplier_id)} promised overdue {pid} to {N.W(r.warehouse_id)} by {due:%Y-%m-%d}", quantity=None, due=due,
                         penalty=None, status=st, resolved=r.received_at, evidence=[pid], focus=False, decision_key=None, neg_key=None))
    # open promises at the cutoff: open RM POs
    for pid_, h in rpo[rpo.status == "open"].sample(n=min(int((rpo.status == "open").sum()), max(8, int(0.05 * len(sel)))), random_state=int(rng.integers(1 << 30))).iterrows():
        pool.append(dict(kind="ack", ctype="supplier", cp=h.supplier_id, by=f"{N.sup[h.supplier_id]} order desk", made=h.ordered_at + DAY,
                         text=f"{N.S(h.supplier_id)} acknowledged {pid_} for delivery to {N.P(h.plant_id)} by {h.expected_at:%Y-%m-%d}", quantity=None, due=h.expected_at,
                         penalty="late-delivery credit per contract", status="open" if h.expected_at > win_end else "breached", resolved=None if h.expected_at > win_end else h.expected_at + DAY,
                         evidence=[pid_], focus=True, decision_key=None, neg_key=None))
    # choose from the pool: all focus rows, then fill to the target size honouring the status mix
    target_n = int(2.3 * len(sel))
    must = [p for p in pool if p["focus"]]
    rest = [p for p in pool if not p["focus"]]
    rng.shuffle(rest)
    chosen = out + must
    by_st = defaultdict(list)
    for p in rest:
        by_st[p["status"]].append(p)

    def share(s):
        return sum(x["status"] == s for x in chosen) / max(1, len(chosen))
    while len(chosen) < target_n and any(by_st.values()):
        if share("breached") < cfg["memory"]["commitment_breach_min"] + 0.02 and by_st["breached"]:
            chosen.append(by_st["breached"].pop())
        elif share("renegotiated") < cfg["memory"]["commitment_renegotiated_target"] and by_st["renegotiated"]:
            chosen.append(by_st["renegotiated"].pop())
        elif share("open") < 0.03 and by_st["open"]:
            chosen.append(by_st["open"].pop())
        elif by_st["fulfilled"]:
            chosen.append(by_st["fulfilled"].pop())
        else:
            k = next(k for k, v in by_st.items() if v)
            chosen.append(by_st[k].pop())
    return chosen
