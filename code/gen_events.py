"""Stage 3 - institutional memory: disruption events, impacts, links,
negotiations, decisions, commitments, supplier scorecards, planted patterns.

Events are anchored either on existing v1.0.0 rows (anchor_type=existing_data:
statistical clusters of late / short / cancelled POs, shrink, stock-outs, sales
dips and peaks) or on the new RM-side rows (anchor_type=rm_side: cascades,
price spikes, near-misses, emergency buys, planted-pattern episodes).
Decision outcomes, commitment statuses and scorecards are COMPUTED from the
inventory / PO trajectories, never sampled.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy import stats

from sc_common import Ctx, banner, dump_json, js, log, make_ids, week_start, write_table

DAY = pd.Timedelta(days=1)


class Names:
    def __init__(self, ctx: Ctx):
        s = ctx.src
        self.sup = s["suppliers"].set_index("supplier_id").supplier_name.to_dict()
        self.country = s["suppliers"].set_index("supplier_id").country_code.to_dict()
        self.wh = s["warehouses"].set_index("warehouse_id").warehouse_name.to_dict()
        self.plant = ctx.table("plants").set_index("plant_id").plant_name.to_dict()
        rms = ctx.table("raw_materials").set_index("rm_id")
        self.rm = rms.rm_name.to_dict()
        self.rm_cat = rms.rm_category.to_dict()
        self.uom = rms.uom.to_dict()
        self.sku = s["products"].set_index("product_id").sku.to_dict()

    def S(self, x):
        return f"{self.sup[x]} ({x})"

    def P(self, x):
        return f"{self.plant[x]} ({x})"

    def R(self, x):
        return f"{self.rm[x]} ({x})"

    def W(self, x):
        return f"{self.wh[x]} ({x})"


def severity_from(score: float) -> int:
    return int(np.clip(1 + math.floor(math.log2(1 + max(score, 0) / 4)), 1, 5))


def binom_p(k: int, n: int, p: float) -> float:
    return float(stats.binom.sf(k - 1, n, p))


# =============================================================================
# event builders
# =============================================================================
class EventBook:
    def __init__(self):
        self.E: list[dict] = []
        self.by_key: dict[str, dict] = {}

    def add(self, key: str, **kw) -> dict:
        e = {"key": key, "impacts": [], "meta": {}, **kw}
        for d in ("start_date", "end_date", "detected_at"):
            if e.get(d) is not None:
                e[d] = pd.Timestamp(e[d]).normalize()
        self.E.append(e)
        self.by_key[key] = e
        return e


def build_events(ctx: Ctx, N: Names, book: EventBook, env: dict):
    cfg = ctx.cfg
    src = ctx.src
    S, R, DM = env["S"], env["R"], env["DM"]
    prod = S["prod"].set_index("product_id")
    plan = S["pattern_plan"]
    runs = env["runs"]
    rpo, rpol, rev = env["rpo"], env["rpol"], env["rev"]
    lf = R["line_facts"]
    po = src["purchase_orders"].copy()
    po["delay"] = (po.received_at - po.expected_at).dt.days
    pol = src["purchase_order_lines"]
    late_thr = ctx.calib["late_threshold_days"]
    win_end = pd.Timestamp(ctx.calib["window_end"])
    rng = ctx.rng("events")
    run_ix = runs.set_index("production_run_id")
    pol_ix = pol.set_index("purchase_order_line_id")
    po_ix = po.set_index("purchase_order_id")

    # ------------------------------------------------------------------ RM cascades
    for c in R["cascades"]:
        victims = [c["production_run_id"]] + c.get("co_victims", [])
        vr = run_ix.loc[victims]
        fg_lines = vr.purchase_order_line_id.tolist()
        fgp = pol_ix.loc[fg_lines].join(po_ix[["warehouse_id", "expected_at", "received_at", "status"]], on="purchase_order_id")
        value = float((vr.planned_qty * vr.product_id.map(prod.unit_price)).sum())
        blocked = int(c["X"] - c["P"])
        a_class = bool((vr.product_id.map(prod.abc_class) == "A").any())
        sup = c["slipped"][0]["supplier_id"]
        score = blocked * len(victims) * (1.5 if a_class else 1.0) + value / 150000
        slip_days = int(c["slip_to"] - min(s_["orig_arrival"] for s_ in c["slipped"]))
        e = book.add(f"cas:{c['cascade_id']}", event_type="rm_shortage",
                     title=f"{N.rm[c['rm_id']]} shortage at {N.plant[c['plant_id']]} after {N.sup[sup]} slip",
                     start_date=c["t_below_ss_date"], end_date=c["X_date"], detected_at=c["t_revised_date"],
                     severity=severity_from(score), root_cause_code=c["cause"],
                     root_cause_text=f"{N.S(sup)} moved the delivery of {N.R(c['rm_id'])} for {N.P(c['plant_id'])} from "
                                     f"{min(env['D'](s_['orig_arrival']) for s_ in c['slipped']):%Y-%m-%d} to {c['slip_to_date']:%Y-%m-%d} ({c['cause'].replace('_', ' ')}); "
                                     f"stock fell below safety stock on {c['t_below_ss_date']:%Y-%m-%d} and could not cover the run planned for {c['P_date']:%Y-%m-%d}.",
                     origin_entity_type="supplier", origin_entity_id=sup, anchor_type="rm_side",
                     anchor_ids=sorted({s_["rm_purchase_order_id"] for s_ in c["slipped"]}) + sorted({x for s_ in c["slipped"] for x in s_["revision_ids"]}) + victims + fg_lines)
        e["impacts"] += [("rm", c["rm_id"], "blocked_days", blocked, "days"),
                         ("plant", c["plant_id"], "runs_delayed", len(victims), "runs"),
                         ("supplier", sup, "slip_days", slip_days, "days")]
        for rid, rr in vr.iterrows():
            e["impacts"].append(("production_run", rid, "delay_days", int(rr.delay_days), "days"))
        for lid, fr in fgp.iterrows():
            e["impacts"].append(("fg_po_line", lid, "receipt_delay_days", int((fr.received_at - fr.expected_at).days), "days"))
            e["impacts"].append(("warehouse", fr.warehouse_id, "late_units", int(fr.quantity_received), "units"))
        for pid, g in vr.groupby("product_id"):
            e["impacts"].append(("product", pid, "units_delayed", int(g.produced_qty.sum()), "units"))
        e["meta"] = {"kind": "cascade", "cascade": c, "rm_id": c["rm_id"], "plant_id": c["plant_id"], "supplier_id": sup,
                     "victims": victims, "fg_lines": fg_lines, "warehouses": sorted(set(fgp.warehouse_id)), "products": sorted(set(vr.product_id)),
                     "value_blocked": value, "blocked_days": blocked, "tags": c["tags"], "family": ("rm_shortage", c["rm_id"])}
        c["event_key"] = e["key"]

    # parent supplier episodes: >=2 cascades of one supplier within 30 days of each other
    by_sup = defaultdict(list)
    for c in R["cascades"]:
        by_sup[c["slipped"][0]["supplier_id"]].append(c)
    n_parent = 0
    for sup, cs in by_sup.items():
        cs.sort(key=lambda c: c["t_revised"])
        grp = [cs[0]]
        for c in cs[1:] + [None]:
            if c is not None and c["t_revised"] - grp[-1]["t_revised"] <= 30:
                grp.append(c)
                continue
            if len({(g["rm_id"], g["plant_id"]) for g in grp}) >= 2:
                n_parent += 1
                start = min(g["t_below_ss_date"] for g in grp)
                tags = sorted({t for g in grp for t in g["tags"]})
                cause = grp[0]["cause"]
                e = book.add(f"sup:{sup}:{grp[0]['cascade_id']}", event_type="supplier_delivery_disruption",
                             title=f"{N.sup[sup]} delivery disruption hits {len(grp)} RM lots",
                             start_date=start, end_date=max(g["X_date"] for g in grp), detected_at=min(g["t_revised_date"] for g in grp),
                             severity=min(5, 1 + len(grp)), root_cause_code=cause,
                             root_cause_text=f"{N.S(sup)} slipped {len(grp)} deliveries ({', '.join(sorted({g['rm_id'] for g in grp}))}) within {max(1, (grp[-1]['t_revised'] - grp[0]['t_revised']))} days; cause reported as {cause.replace('_', ' ')}.",
                             origin_entity_type="supplier", origin_entity_id=sup, anchor_type="rm_side",
                             anchor_ids=sorted({s_["rm_purchase_order_id"] for g in grp for s_ in g["slipped"]}))
                e["impacts"].append(("supplier", sup, "lots_slipped", len(grp), "lots"))
                e["meta"] = {"kind": "supplier_parent", "children": [f"cas:{g['cascade_id']}" for g in grp], "supplier_id": sup, "tags": tags,
                             "family": ("supplier_delivery_disruption", sup)}
            if c is not None:
                grp = [c]

    # ------------------------------------------------------------------ planted-pattern RM episodes (near misses)
    lfd = lf[lf.arrival.notna()].copy()
    lfd["delay"] = (lfd.arrival - lfd.promised).dt.days

    def rm_episode(key, rows, etype, title, cause, text, origin_t, origin_id, family, pattern):
        rows = rows.sort_values("promised")
        e = book.add(key, event_type=etype, title=title, start_date=rows.promised.min(), end_date=rows.arrival.max(),
                     detected_at=max(rows.ordered.min() + DAY, rows.promised.min() - 3 * DAY), severity=int(np.clip(1 + len(rows) // 4, 1, 4)),
                     root_cause_code=cause, root_cause_text=text, origin_entity_type=origin_t, origin_entity_id=origin_id,
                     anchor_type="rm_side", anchor_ids=sorted(rows.rm_purchase_order_id.unique().tolist()))
        e["impacts"].append((origin_t, origin_id, "avg_slip_days", round(float(rows.delay.mean()), 1), "days"))
        e["impacts"].append((origin_t, origin_id, "lots_late", int((rows.delay > 0).sum()), "lots"))
        for (rm_, pl), g in rows.groupby(["rm_id", "plant_id"]):
            e["impacts"].append(("rm", rm_, f"late_lots_at_{pl}", int(len(g)), "lots"))
        e["meta"] = {"kind": "pattern_episode", "pattern": pattern, "family": family, "lines": rows.rm_purchase_order_line_id.tolist(),
                     "supplier_id": origin_id if origin_t == "supplier" else None, "rm_ids": sorted(rows.rm_id.unique()), "plants": sorted(rows.plant_id.unique()),
                     "avg_delay": float(rows.delay.mean()), "tags": [pattern]}
        return e

    if "P01" in plan:
        s1 = plan["P01"]["supplier_id"]
        x = lfd[(lfd.supplier_id == s1) & lfd.tags.str.contains("P01", na=False)]
        for yr, g in x.groupby(x.promised.dt.year):
            rm_episode(f"p01:{yr}", g, "supplier_delivery_delay", f"{N.sup[s1]} year-end slips {yr}", "supplier_capacity",
                       f"{N.S(s1)} delivered {len(g)} RM lots due in Nov-Dec {yr} late by {g.delay.mean():.1f} days on average (peak-season capacity).",
                       "supplier", s1, ("supplier_delivery_delay", s1), "P01")
    if "P02" in plan:
        cc = plan["P02"]["country_code"]
        x = lfd[lfd.tags.str.contains("P02", na=False)]
        for yr, g in x.groupby(x.promised.dt.year):
            rm_episode(f"p02:{yr}", g, "port_congestion", f"{cc} port congestion February {yr}", "port_congestion",
                       f"Ocean freight from {cc} was congested in February {yr}: {len(g)} RM lots from {g.supplier_id.nunique()} suppliers arrived {g.delay.mean():.1f} days late on average.",
                       "country", cc, ("port_congestion", cc), "P02")
    if "P11" in plan:
        s11 = plan["P11"]["supplier_id"]
        x = lfd[(lfd.supplier_id == s11) & lfd.tags.str.contains("P11", na=False)]
        for q, g in x.groupby(x.promised.dt.to_period("Q")):
            if len(g) >= 2:
                rm_episode(f"p11:{q}", g, "lead_time_creep", f"{N.sup[s11]} lead-time creep {q}", "supplier_capacity",
                           f"Actual lead times from {N.S(s11)} kept stretching in {q}: {len(g)} lots, average {g.delay.mean():.1f} days beyond the quoted lead time.",
                           "supplier", s11, ("lead_time_creep", s11), "P11")
    if "P09" in plan:
        s9 = plan["P09"]["supplier_id"]
        x = lfd[(lfd.supplier_id == s9) & lfd.second_slip]
        for h, g in x.groupby([x.promised.dt.year, (x.promised.dt.month > 6).astype(int)]):
            if len(g) >= 2:
                rm_episode(f"p09:{h[0]}H{h[1] + 1}", g, "recovery_date_missed", f"{N.sup[s9]} missed recovery dates {h[0]} H{h[1] + 1}", "supplier_capacity",
                           f"{N.S(s9)} re-promised {len(g)} late RM lots and then missed the recovery dates again.",
                           "supplier", s9, ("recovery_date_missed", s9), "P09")

    # price spikes
    cat = env["cat"]
    for sp in S["price_spikes"]:
        row = cat[(cat.rm_id == sp["rm_id"]) & (cat.supplier_id == sp["supplier_id"]) & (cat.price_valid_from == sp["date"])]
        e = book.add(f"price:{sp['rm_id']}:{sp['date']:%Y%m%d}", event_type="price_increase",
                     title=f"{N.sup[sp['supplier_id']]} raises {N.rm[sp['rm_id']]} price {sp['pct']:.0f}%",
                     start_date=sp["date"], end_date=sp["date"], detected_at=sp["date"] - 40 * DAY, severity=3 if sp.get("pattern") else 2,
                     root_cause_code="supplier_price_increase",
                     root_cause_text=f"{N.S(sp['supplier_id'])} increased {N.R(sp['rm_id'])} from {sp['old_price']:.4f} to {sp['new_price']:.4f} USD/{N.uom[sp['rm_id']]} (+{sp['pct']:.1f}%) effective {sp['date']:%Y-%m-%d}.",
                     origin_entity_type="supplier", origin_entity_id=sp["supplier_id"], anchor_type="rm_side", anchor_ids=row.catalog_id.tolist())
        e["impacts"].append(("rm", sp["rm_id"], "price_change_pct", sp["pct"], "pct"))
        e["meta"] = {"kind": "price_spike", "spike": sp, "rm_id": sp["rm_id"], "supplier_id": sp["supplier_id"], "family": ("price_increase", sp["rm_id"]),
                     "tags": ["P03"] if sp.get("pattern") else []}

    # emergency spot buys (safety-pass) grouped per rm/plant/month
    em = lf[lf.emergency]
    for (rm_, pl, mo), g in em.groupby(["rm_id", "plant_id", em.arrival.dt.to_period("M")]):
        e = book.add(f"emg:{rm_}:{pl}:{mo}", event_type="rm_emergency_buy", title=f"Emergency spot buy of {N.rm[rm_]} for {N.plant[pl]}",
                     start_date=g.ordered.min(), end_date=g.arrival.max(), detected_at=g.ordered.min(), severity=1 if len(g) == 1 else 2,
                     root_cause_code="schedule_surge", root_cause_text=f"Production pulled {N.R(rm_)} faster than planned at {N.P(pl)}; {len(g)} spot lot(s) bought at +{ctx.cfg['rm_ops']['spot_price_premium'] * 100:.0f}% to avoid a stock-out.",
                     origin_entity_type="rm", origin_entity_id=rm_, anchor_type="rm_side", anchor_ids=sorted(g.rm_purchase_order_id.unique()))
        e["impacts"].append(("rm", rm_, "spot_qty", round(float(g.qty.sum()), 2), N.uom[rm_]))
        e["meta"] = {"kind": "emergency", "rm_id": rm_, "plant_id": pl, "family": ("rm_emergency_buy", rm_), "tags": []}

    # safety-stock breach patterns (trigger of SS builds)
    snaps = env["snap"]
    for b in R["mitigations"]["ss_builds"]:
        d = pd.Timestamp(b["date"])
        q0 = d.to_period("Q") - 1
        sn = snaps[(snaps.rm_id == b["rm_id"]) & (snaps.plant_id == b["plant_id"]) & (snaps.snapshot_date.dt.to_period("Q") == q0)]
        low = sn[sn.on_hand_units < 0.6 * sn.safety_stock_units]
        if low.empty:
            low = sn.nsmallest(3, "days_of_cover")
        e = book.add(f"ssb:{b['rm_id']}:{b['plant_id']}", event_type="safety_stock_breaches",
                     title=f"Repeated safety-stock breaches: {N.rm[b['rm_id']]} at {N.plant[b['plant_id']]}",
                     start_date=sn.snapshot_date.min(), end_date=sn.snapshot_date.max(), detected_at=d - 5 * DAY, severity=2,
                     root_cause_code="buffer_undersized", root_cause_text=f"{N.R(b['rm_id'])} at {N.P(b['plant_id'])} closed {len(low)} weeks of {q0} below 60% of safety stock (lowest cover {low.days_of_cover.min():.1f} days).",
                     origin_entity_type="rm", origin_entity_id=b["rm_id"], anchor_type="rm_side",
                     anchor_ids=[f"{r.rm_id}|{r.plant_id}|{r.snapshot_date:%Y-%m-%d}" for r in low.itertuples()])
        e["impacts"].append(("rm", b["rm_id"], "weeks_below_60pct_ss", int(len(low)), "weeks"))
        e["meta"] = {"kind": "ss_trigger", "build": b, "rm_id": b["rm_id"], "plant_id": b["plant_id"], "family": ("safety_stock_breaches", b["rm_id"]), "tags": []}

    # supplier performance decline (trigger of allocation changes)
    for a in R["mitigations"]["alloc_changes"]:
        d = pd.Timestamp(a["date"])
        g = lfd[(lfd.rm_id == a["rm_id"]) & (lfd.supplier_id == a["old_primary"]) & (lfd.promised < d - 30 * DAY) & (lfd.delay > 2)]
        e = book.add(f"alc:{a['rm_id']}", event_type="supplier_performance_decline",
                     title=f"{N.sup[a['old_primary']]} on-time performance on {N.rm[a['rm_id']]} below target",
                     start_date=min(g.promised.min(), d - 90 * DAY) if len(g) else d - 180 * DAY, end_date=d - 30 * DAY, detected_at=d - 70 * DAY, severity=2,
                     root_cause_code="chronic_lateness", root_cause_text=f"{a['late_share_before'] * 100:.0f}% of {N.S(a['old_primary'])} lots of {N.R(a['rm_id'])} arrived more than 2 days late over the review period.",
                     origin_entity_type="supplier", origin_entity_id=a["old_primary"], anchor_type="rm_side",
                     anchor_ids=sorted(g.rm_purchase_order_id.unique().tolist())[:40])
        e["impacts"].append(("supplier", a["old_primary"], "late_share", round(a["late_share_before"] * 100, 1), "pct"))
        e["meta"] = {"kind": "alloc_trigger", "alloc": a, "rm_id": a["rm_id"], "supplier_id": a["old_primary"], "family": ("supplier_performance_decline", a["old_primary"]), "tags": []}

    # substitution triggers
    for bc in S["bom_changes"]:
        if bc["kind"] != "substitution":
            continue
        d = pd.Timestamp(bc["date"])
        pl = prod.at[bc["product_id"], "plant_id"]
        prior = [c for c in R["cascades"] if c["rm_id"] == bc["old_rm"] and c["plant_id"] == pl and d - 240 * DAY <= c["X_date"] < d - 10 * DAY]
        single = int(env["rms"].set_index("rm_id").at[bc["old_rm"], "is_single_source"])
        e = book.add(f"sub:{bc['product_id']}", event_type="single_source_risk" if not prior else "recurring_rm_shortage",
                     title=f"{N.rm[bc['old_rm']]} supply risk for {N.sku[bc['product_id']]}",
                     start_date=(prior[0]["t_below_ss_date"] if prior else d - 45 * DAY), end_date=d - DAY, detected_at=min(prior[-1]["X_date"] if prior else d - 45 * DAY, d - 30 * DAY),
                     severity=3 if prior else 2, root_cause_code="single_source_dependency" if single else "recurring_shortage",
                     root_cause_text=(f"{len(prior)} shortage(s) of {N.R(bc['old_rm'])} at {N.P(pl)} in the previous 8 months" if prior else f"{N.R(bc['old_rm'])} is {'single-sourced' if single else 'concentrated'}; risk review flagged {bc['product_id']}") + ".",
                     origin_entity_type="rm", origin_entity_id=bc["old_rm"], anchor_type="rm_side",
                     anchor_ids=sorted({s_["rm_purchase_order_id"] for c in prior for s_ in c["slipped"]}) or env["cat"][env["cat"].rm_id == bc["old_rm"]].catalog_id.head(3).tolist())
        e["impacts"].append(("product", bc["product_id"], "rm_shortages_prior_240d", len(prior), "events"))
        e["meta"] = {"kind": "sub_trigger", "change": bc, "rm_id": bc["old_rm"], "plant_id": pl, "prior": [f"cas:{c['cascade_id']}" for c in prior],
                     "family": ("rm_supply_risk", bc["old_rm"]), "tags": ["P07"] if bc.get("high_scrap") else []}

    # P06 plant maintenance overruns (per quarter)
    if "P06" in plan:
        p6 = plan["P06"]["plant_id"]
        mr = runs[(runs.plant_id == p6) & (runs.delay_reason_code == "maintenance_overrun")]
        for q, g in mr.groupby(mr.planned_start.dt.to_period("Q")):
            e = book.add(f"p06:{q}", event_type="plant_maintenance_overrun", title=f"{N.plant[p6]} quarter-start maintenance overrun {q}",
                         start_date=g.planned_start.min(), end_date=g.actual_start.max(), detected_at=g.planned_start.min() + DAY, severity=2,
                         root_cause_code="maintenance_overrun", root_cause_text=f"Planned quarter-start maintenance at {N.P(p6)} ran long; {len(g)} runs started {g.delay_days.mean():.1f} days late on average.",
                         origin_entity_type="plant", origin_entity_id=p6, anchor_type="rm_side", anchor_ids=g.production_run_id.tolist())
            e["impacts"].append(("plant", p6, "runs_delayed", int(len(g)), "runs"))
            e["meta"] = {"kind": "pattern_episode", "pattern": "P06", "family": ("plant_maintenance_overrun", p6), "tags": ["P06"], "plant_id": p6}

    # P07 substitute scrap
    if "P07" in plan:
        p7 = plan["P07"]
        pl7 = prod.at[p7["product_id"], "plant_id"]
        mv = env["rmv"]
        sc = mv[(mv.rm_id == p7["new_rm"]) & (mv.plant_id == pl7) & (mv.movement_type == "scrap") & (mv.movement_at >= pd.Timestamp(p7["date"]))]
        for h, g in sc.groupby(sc.movement_at.dt.to_period("Q")):
            e = book.add(f"p07:{h}", event_type="quality_rejects", title=f"Incoming rejects of substitute {N.rm[p7['new_rm']]} at {N.plant[pl7]} {h}",
                         start_date=g.movement_at.min(), end_date=g.movement_at.max(), detected_at=g.movement_at.min(), severity=2,
                         root_cause_code="substitute_material_variability", root_cause_text=f"Lots of substitute {N.R(p7['new_rm'])} (replacing {p7['old_rm']} in {p7['product_id']} since {p7['date']}) failed incoming QA; {abs(g.quantity_change.sum()):.1f} {N.uom[p7['new_rm']]} scrapped.",
                         origin_entity_type="rm", origin_entity_id=p7["new_rm"], anchor_type="rm_side", anchor_ids=g.rm_movement_id.tolist())
            e["impacts"].append(("rm", p7["new_rm"], "scrapped_qty", round(float(-g.quantity_change.sum()), 2), N.uom[p7["new_rm"]]))
            e["meta"] = {"kind": "pattern_episode", "pattern": "P07", "family": ("quality_rejects", p7["new_rm"]), "tags": ["P07"], "rm_id": p7["new_rm"], "plant_id": pl7}

    # ------------------------------------------------------------------ demand shocks
    mvs = src["inventory_movements"]
    sales = mvs[mvs.movement_type == "sale"]
    for sh in DM["shocks"]:
        st, en = pd.Timestamp(sh["start"]), pd.Timestamp(sh["end"])
        anc = sales[sales.warehouse_id.isin(sh["warehouse_ids"]) & sales.product_id.isin(sh["product_ids"]) & sales.movement_at.between(st, en)].movement_id.tolist()[:25]
        if not anc:  # e.g. a DC outage week with no shipments: anchor on that DC's movements (or sales) around the week
            near = mvs[mvs.warehouse_id.isin(sh["warehouse_ids"]) & mvs.movement_at.between(st - 7 * DAY, en + 7 * DAY)]
            anc = near.movement_id.tolist()[:25]
        if sh["type"] == "allocation_cut":
            c = next(c for c in R["cascades"] if c["cascade_id"] == sh["cascade_id"])
            e = book.add(f"shk:{sh['shock_id']}", event_type="customer_allocation", title=f"Allocation on {N.sku[sh['product_ids'][0]]} at {N.wh[sh['warehouse_ids'][0]]}",
                         start_date=st, end_date=en, detected_at=st, severity=2, root_cause_code="inbound_supply_delay",
                         root_cause_text=f"Inbound replenishment line {sh['purchase_order_line_id']} was late (upstream {N.rm[c['rm_id']]} shortage) while stock was at or below reorder point; orders were capped at {100 - sh['cut_pct']:.0f}%.",
                         origin_entity_type="product", origin_entity_id=sh["product_ids"][0], anchor_type="existing_data", anchor_ids=anc or [sh["purchase_order_line_id"]])
            e["meta"] = {"kind": "shock", "shock": sh, "cause_event": f"cas:{sh['cascade_id']}", "family": ("customer_allocation", sh["product_ids"][0]), "tags": []}
        elif sh["type"] == "dc_disruption":
            wh_ = sh["warehouse_ids"][0]
            e = book.add(f"shk:{sh['shock_id']}", event_type="dc_disruption", title=f"{N.wh[wh_]} outbound disruption ({sh['cause'].replace('_', ' ')})",
                         start_date=st, end_date=en, detected_at=st, severity=3, root_cause_code=sh["cause"],
                         root_cause_text=f"Outbound volume at {N.W(wh_)} fell to {sh['week_units']} units in the week of {st:%Y-%m-%d} vs a median of {sh['median_units']:.0f}; {sh['units_cut']} ordered units could not ship.",
                         origin_entity_type="warehouse", origin_entity_id=wh_, anchor_type="existing_data", anchor_ids=anc)
            e["meta"] = {"kind": "shock", "shock": sh, "family": ("dc_disruption", wh_), "tags": []}
        else:
            pid = sh["product_ids"][0]
            e = book.add(f"shk:{sh['shock_id']}", event_type="demand_spike", title=f"Demand spike on {N.sku[pid]} ({sh['cause'].replace('_', ' ')})",
                         start_date=st, end_date=en, detected_at=st, severity=2, root_cause_code=sh["cause"],
                         root_cause_text=f"Orders for {N.sku[pid]} ({pid}) jumped to {sh['week_units']} units in the week of {st:%Y-%m-%d} (z={sh['sales_z']}); {sh['units_cut']} units could not be served across {len(sh['warehouse_ids'])} DCs.",
                         origin_entity_type="product", origin_entity_id=pid, anchor_type="existing_data", anchor_ids=anc)
            e["meta"] = {"kind": "shock", "shock": sh, "family": ("demand_spike", pid), "tags": []}
        e["impacts"].append(("product" if sh["type"] != "dc_disruption" else "warehouse", sh["product_ids"][0] if sh["type"] != "dc_disruption" else sh["warehouse_ids"][0], "unfulfilled_units", sh["units_cut"], "units"))

    # ------------------------------------------------------------------ existing-data FG anchors
    rec = po[po.received_at.notna()].copy()
    p0 = float((rec.delay >= late_thr).mean())
    n_clusters = 60 if ctx.cfg["scale_name"] != "small" else 25
    cands = []
    for sup, g in rec.sort_values("expected_at").groupby("supplier_id"):
        ex = g.expected_at.values
        lt = (g.delay >= late_thr).values
        for i in range(len(g)):
            j = np.searchsorted(ex, ex[i] + np.timedelta64(60, "D"), side="right")
            n, k = j - i, int(lt[i:j].sum())
            if n >= 4 and k / n >= 0.8:
                cands.append((binom_p(k, n, p0), sup, i, j))
    cands.sort()
    used = defaultdict(list)
    fg_sup_clusters = []
    for p, sup, i, j in cands:
        g = rec[rec.supplier_id == sup].sort_values("expected_at").iloc[i:j]
        a, b = g.expected_at.min(), g.expected_at.max()
        if any(not (b < x or a > y) for x, y in used[sup]):
            continue
        used[sup].append((a, b))
        fg_sup_clusters.append((p, sup, g))
        if len(fg_sup_clusters) >= n_clusters:
            break
    for p, sup, g in fg_sup_clusters:
        late = g[g.delay >= late_thr]
        e = book.add(f"fgs:{sup}:{g.expected_at.min():%Y%m%d}", event_type="supplier_delivery_delay",
                     title=f"{N.sup[sup]} late on {len(late)} of {len(g)} POs", start_date=g.expected_at.min(), end_date=g.received_at.max(),
                     detected_at=late.expected_at.min() + DAY, severity=int(np.clip(1 + len(late) // 2, 1, 4)), root_cause_code=str(rng.choice(["supplier_capacity", "transport_disruption", "labor_shortage_at_supplier", "raw_material_shortage_at_supplier"])),
                     root_cause_text=f"{len(late)} of {len(g)} POs from {N.S(sup)} due {g.expected_at.min():%Y-%m-%d}..{g.expected_at.max():%Y-%m-%d} arrived {late_thr}+ days late (avg {late.delay.mean():.1f} d; binomial p={p:.3f} vs base late rate {p0:.0%}).",
                     origin_entity_type="supplier", origin_entity_id=sup, anchor_type="existing_data", anchor_ids=late.purchase_order_id.tolist())
        e["impacts"].append(("supplier", sup, "late_pos", int(len(late)), "POs"))
        e["impacts"].append(("supplier", sup, "avg_delay_days", round(float(late.delay.mean()), 1), "days"))
        for wh_, gg in late.groupby("warehouse_id"):
            e["impacts"].append(("warehouse", wh_, "late_receipts", int(len(gg)), "POs"))
        e["meta"] = {"kind": "fg_supplier_cluster", "supplier_id": sup, "pos": g.purchase_order_id.tolist(), "late_pos": late.purchase_order_id.tolist(),
                     "family": ("supplier_delivery_delay", sup), "tags": [], "p": p}

    # country x month lateness clusters
    rec["country"] = rec.supplier_id.map(N.country)
    rec["ym"] = rec.expected_at.dt.to_period("M")
    cm = rec.groupby(["country", "ym"]).apply(lambda g: pd.Series({"n": len(g), "k": int((g.delay >= late_thr).sum())})).reset_index()
    cm["p"] = [binom_p(int(k), int(n), p0) for k, n in zip(cm.k, cm.n)]
    for r in cm.sort_values("p").head(6).itertuples():
        g = rec[(rec.country == r.country) & (rec.ym == r.ym) & (rec.delay >= late_thr)]
        kind = "port_congestion" if r.country not in ("US", "CA", "MX") else "cross_border_delay"
        e = book.add(f"cty:{r.country}:{r.ym}", event_type=kind, title=f"{r.country} inbound delays {r.ym}", start_date=g.expected_at.min(), end_date=g.received_at.max(),
                     detected_at=g.expected_at.min() + 2 * DAY, severity=3, root_cause_code=kind,
                     root_cause_text=f"{r.k} of {r.n} FG POs from {r.country} suppliers due in {r.ym} arrived {late_thr}+ days late (p={r.p:.4f}).",
                     origin_entity_type="country", origin_entity_id=r.country, anchor_type="existing_data", anchor_ids=g.purchase_order_id.tolist()[:40])
        e["impacts"].append(("country", r.country, "late_pos", int(r.k), "POs"))
        e["meta"] = {"kind": "country_cluster", "family": (kind, r.country), "tags": []}

    # negative adjustment (shrink) clusters per warehouse-month
    adj = mvs[(mvs.movement_type == "adjustment") & (mvs.quantity_change < 0)].copy()
    adj["ym"] = adj.movement_at.dt.to_period("M")
    sc = adj.groupby(["warehouse_id", "ym"]).quantity_change.agg(["size", "sum"]).reset_index()
    mu = sc["size"].mean()
    sc["p"] = [float(stats.poisson.sf(k - 1, mu)) for k in sc["size"]]
    for r in sc.sort_values("p").head(6).itertuples():
        g = adj[(adj.warehouse_id == r.warehouse_id) & (adj.ym == r.ym)]
        e = book.add(f"shr:{r.warehouse_id}:{r.ym}", event_type="inventory_shrink", title=f"Cycle-count losses at {N.wh[r.warehouse_id]} {r.ym}",
                     start_date=g.movement_at.min(), end_date=g.movement_at.max(), detected_at=g.movement_at.max(), severity=2, root_cause_code="cycle_count_variance",
                     root_cause_text=f"{int(r.size)} negative adjustments ({int(-r.sum)} units) at {N.W(r.warehouse_id)} in {r.ym} vs {mu:.1f} typical.",
                     origin_entity_type="warehouse", origin_entity_id=r.warehouse_id, anchor_type="existing_data", anchor_ids=g.movement_id.tolist())
        e["impacts"].append(("warehouse", r.warehouse_id, "units_written_off", int(-r.sum), "units"))
        e["meta"] = {"kind": "shrink", "family": ("inventory_shrink", r.warehouse_id), "tags": []}

    # FG stock-outs (balance <= 0)
    led = env["led"]
    dem = env["dem"]
    for (pid, wh_), g in led[led.balance <= 0].groupby(["product_id", "warehouse_id"]):
        lost = dem[(dem.product_id == pid) & (dem.warehouse_id == wh_) & dem.week_start.isin(week_start(g.movement_at).unique()) & (dem.unfulfilled_units > 0)]
        anc = mvs[(mvs.product_id == pid) & (mvs.warehouse_id == wh_) & mvs.movement_at.isin(g.movement_at)].movement_id.tolist()
        e = book.add(f"fgo:{pid}:{wh_}", event_type="fg_stockout", title=f"{N.sku[pid]} stocked out at {N.wh[wh_]}", start_date=g.movement_at.min(), end_date=g.movement_at.max(),
                     detected_at=g.movement_at.min(), severity=3 if prod.at[pid, "abc_class"] == "A" else 2, root_cause_code="demand_exceeded_available",
                     root_cause_text=f"Book stock of {N.sku[pid]} ({pid}) at {N.W(wh_)} reached {int(g.balance.min())} units; {int(lost.unfulfilled_units.sum())} units of demand were lost.",
                     origin_entity_type="product", origin_entity_id=pid, anchor_type="existing_data", anchor_ids=anc)
        e["impacts"].append(("product", pid, "lost_units", int(lost.unfulfilled_units.sum()), "units"))
        e["meta"] = {"kind": "fg_stockout", "family": ("fg_stockout", pid), "tags": [], "warehouse_id": wh_, "product_id": pid}

    # short-shipment clusters (partial POs) per supplier
    part = po[po.status == "partial"].sort_values("expected_at")
    shorts = []
    for sup, g in part.groupby("supplier_id"):
        if len(g) >= 3:
            ex = g.expected_at.values
            for i in range(len(g)):
                j = np.searchsorted(ex, ex[i] + np.timedelta64(120, "D"), side="right")
                if j - i >= 3:
                    shorts.append((-(j - i), sup, g.iloc[i:j]))
                    break
    shorts.sort(key=lambda z: z[0])
    for _, sup, g in shorts[:12]:
        lines = pol[pol.purchase_order_id.isin(g.purchase_order_id)]
        fill = lines.quantity_received.sum() / lines.quantity_ordered.sum()
        e = book.add(f"sht:{sup}", event_type="short_shipment", title=f"{N.sup[sup]} short-shipped {len(g)} POs", start_date=g.expected_at.min(), end_date=g.received_at.max(),
                     detected_at=g.received_at.min(), severity=2, root_cause_code="supplier_capacity",
                     root_cause_text=f"{len(g)} POs from {N.S(sup)} received partially within 120 days (fill {fill:.0%}).",
                     origin_entity_type="supplier", origin_entity_id=sup, anchor_type="existing_data", anchor_ids=g.purchase_order_id.tolist())
        e["impacts"].append(("supplier", sup, "fill_rate_pct", round(float(fill * 100), 1), "pct"))
        e["meta"] = {"kind": "short_cluster", "supplier_id": sup, "pos": g.purchase_order_id.tolist(), "family": ("short_shipment", sup), "tags": []}

    # P12: recurring FG lateness of one supplier in the same month every year (mined)
    rec["m"] = rec.expected_at.dt.month
    rec["y"] = rec.expected_at.dt.year
    best = None
    for (sup, m), g in rec.groupby(["supplier_id", "m"]):
        yrs = g.groupby("y").apply(lambda h: pd.Series({"n": len(h), "r": float((h.delay >= late_thr).mean())}))
        if len(yrs) == 3 and (yrs.n >= 2).all():
            sc_ = float(yrs.r.min()) + 0.01 * float(yrs.n.sum())
            if best is None or sc_ > best[0]:
                best = (sc_, sup, m, yrs)
    if best:
        _, sup, m, yrs = best
        env["p12"] = {"supplier_id": sup, "month": int(m), "yearly_late_rate": yrs.r.round(3).to_dict()}
        for y in sorted(yrs.index):
            g = rec[(rec.supplier_id == sup) & (rec.m == m) & (rec.y == y)]
            late = g[g.delay >= late_thr]
            e = book.add(f"p12:{y}", event_type="supplier_delivery_delay", title=f"{N.sup[sup]} {pd.Timestamp(year=2000, month=m, day=1):%B} delays {y}",
                         start_date=g.expected_at.min(), end_date=g.received_at.max(), detected_at=g.expected_at.min() + DAY, severity=2, root_cause_code="seasonal_capacity",
                         root_cause_text=f"{len(late)} of {len(g)} POs from {N.S(sup)} due in {pd.Timestamp(year=2000, month=m, day=1):%B} {y} were {late_thr}+ days late.",
                         origin_entity_type="supplier", origin_entity_id=sup, anchor_type="existing_data", anchor_ids=g.purchase_order_id.tolist())
            e["impacts"].append(("supplier", sup, "late_pos", int(len(late)), "POs"))
            e["meta"] = {"kind": "pattern_episode", "pattern": "P12", "family": ("fg_seasonal_delay", sup), "tags": ["P12"], "supplier_id": sup}
    return book


# =============================================================================
# texts
# =============================================================================
DRIVER_TEXT = {
    "port_congestion": "port congestion at origin added clearance days the air quote did not include",
    "customer_allocation": "downstream customers had to be put on allocation, adding lost margin",
    "other_runs_blocked": "other runs needing the same material were also blocked",
    "slower_than_quoted": "the material landed later than quoted",
    "cost_overrun": "the actual cost ran above the estimate",
    "disruption_exceeded_buffer": "a later disruption was longer than the extra buffer",
    "supplier_missed_expedite": "the supplier missed the expedited date anyway",
    "warehouse_surcharge": "the receiving DC charged a surcharge that doubled the premium",
    "source_depleted": "the source DC ran down to or below its reorder point",
    "demand_recovered": "demand recovered and stock dipped after the cancellation",
    "stock_ran_low": "stock ran low before the next replenishment",
    "new_source_also_late": "the new source was also late",
    "substitute_quality": "the substitute failed incoming QA more often, adding scrap",
    "new_rm_shortage": "the substitute itself went short",
    "supplier_kept_slipping": "the supplier kept slipping after the agreement",
}


def entity_label(d, N):
    m = d["meta"]
    if m.get("rm_id") and m.get("plant_id"):
        return f"{N.R(m['rm_id'])} at {N.P(m['plant_id'])}"
    if m.get("supplier_id"):
        return N.S(m["supplier_id"])
    if m.get("warehouse_id"):
        return N.W(m["warehouse_id"])
    return "the affected item"


def write_texts(sel, N, by_key):
    for d in sel:
        others = [o for o in d["options"] if not d["chosen"].startswith(o["option"])]
        comp = "; ".join(f"{o['option']} ~{o['est_stockout_days']:g}d / ${o['est_cost']:,.0f} ({o['risk']} risk)" for o in others[:3])
        m = d["meta"]
        extra = ""
        if d["key"].startswith("dec:cas"):
            extra = f" Supplier ETA was {m['eta']:%Y-%m-%d}; lot value ${m['lines_value']:,.0f}."
            if m.get("cancelled"):
                extra += " Original PO cancelled once the spot lot was confirmed."
        elif m.get("fg_kind") == "expedite":
            extra = f" On hand {m['balance']:.0f} vs reorder point {m['rop']} at {N.W(m['warehouse_id'])}."
        elif m.get("fg_kind") == "transfer":
            extra = f" {N.W(m['dst'])} had {m['b_dst']:.0f} units vs ROP {m['rop']}; source {N.W(m['src'])}."
        elif m.get("fg_kind") == "cancel":
            extra = f" Stock at {N.W(m['warehouse_id'])} was {m['ratio']:.1f}x reorder point."
        d["rationale"] = f"Chose {d['chosen']}: expected {d['expected_days']:g} stockout days / ${d['expected_cost']:,.0f}. Alternatives: {comp or 'none viable'}.{extra}"
        who = entity_label(d, N)
        if d["outcome"] is None:
            d["lesson"] = None
            continue
        a = d.get("attribution")
        if a == "as_expected":
            d["lesson"] = f"{d['type'].replace('_', ' ')} for {who} worked: {d['actual_days']:g} stockout days, ${d['actual_cost']:,.0f} vs ${d['expected_cost']:,.0f} planned."
        elif a == "context_shift":
            p = by_key.get(d.get("precedent_key"))
            diff = [k for k in set(d["context"]) | set(p["context"] if p else {}) if (p and d["context"].get(k) != p["context"].get(k))]
            d["lesson"] = (f"Same move worked on {p['decided_at']:%Y-%m-%d} ({p['key'].split(':')[-1]}) but not here - conditions differed ({', '.join(sorted(diff)) or 'context'}): "
                           f"{DRIVER_TEXT.get(d['failure_driver'], 'conditions changed')}. Check {', '.join(sorted(diff)) or 'current conditions'} before reusing a precedent." if p else
                           f"Conditions differed from earlier successes: {DRIVER_TEXT.get(d['failure_driver'], 'conditions changed')}.")
        elif a == "bad_luck":
            d["lesson"] = f"Sound call ex-ante (lowest expected cost) but {DRIVER_TEXT.get(d['failure_driver'], 'an outside event')}: {d['actual_days']:g} days, ${d['actual_cost']:,.0f} vs ${d['expected_cost']:,.0f}. No process change; keep the option open."
        else:
            d["lesson"] = f"Estimate missed that {DRIVER_TEXT.get(d['failure_driver'], 'key risks were ignored')}; actual {d['actual_days']:g} days / ${d['actual_cost']:,.0f} vs {d['expected_days']:g} / ${d['expected_cost']:,.0f}. Widen the check before choosing {d['type'].replace('_', ' ')}."


# =============================================================================
# scorecard
# =============================================================================
def compute_scorecard(src: dict, rpo: pd.DataFrame, rpol: pd.DataFrame, rmv: pd.DataFrame, cm: pd.DataFrame, win_end: pd.Timestamp) -> pd.DataFrame:
    po, pol = src["purchase_orders"], src["purchase_order_lines"]
    fg = po[po.status != "cancelled"].assign(due=lambda x: x.expected_at)
    fgl = pol.merge(fg[["purchase_order_id"]], on="purchase_order_id")
    fgv = fgl.assign(ov=fgl.quantity_ordered * fgl.unit_cost, rv=fgl.quantity_received * fgl.unit_cost).groupby("purchase_order_id")[["ov", "rv"]].sum()
    fg = fg.join(fgv, on="purchase_order_id")
    rm = rpo[rpo.status != "cancelled"].assign(due=lambda x: x.promised_at)
    rl = rpol.merge(rm[["rm_purchase_order_id"]], on="rm_purchase_order_id")
    rej = -rmv[(rmv.movement_type == "scrap") & rmv.rm_purchase_order_line_id.notna()].groupby("rm_purchase_order_line_id").quantity_change.sum()
    rl = rl.assign(ov=rl.quantity_ordered * rl.unit_cost, rv=rl.quantity_received * rl.unit_cost,
                   rejv=rl.rm_purchase_order_line_id.map(rej).fillna(0) * rl.unit_cost)
    rmv_ = rl.groupby("rm_purchase_order_id")[["ov", "rv", "rejv"]].sum()
    rm = rm.join(rmv_, on="rm_purchase_order_id").rename(columns={"rm_purchase_order_id": "purchase_order_id"})
    fg["rejv"] = np.nan
    both = pd.concat([fg[["purchase_order_id", "supplier_id", "due", "received_at", "status", "ov", "rv", "rejv"]],
                      rm[["purchase_order_id", "supplier_id", "due", "received_at", "status", "ov", "rv", "rejv"]]], ignore_index=True)
    both = both[both.due <= win_end]
    both["month"] = both.due.dt.to_period("M").dt.start_time
    both["otif"] = (both.status == "received") & (both.received_at <= both.due)
    both["delay"] = (both.received_at - both.due).dt.days.clip(lower=0)
    g = both.groupby(["supplier_id", "month"])
    sc = pd.DataFrame({"po_count": g.size(), "otif_rate": g.otif.mean().round(4), "avg_delay_days": g.delay.mean().round(3),
                       "fill_rate": (g.rv.sum() / g.ov.sum()).round(4)})
    rmonly = both[both.rejv.notna()].groupby(["supplier_id", "month"])
    q = (rmonly.rejv.sum() / rmonly.rv.sum().replace(0, np.nan) * 1e6).round(1)
    sc["quality_ppm"] = q
    c = cm[cm.counterparty_type == "supplier"].assign(month=lambda x: pd.to_datetime(x.made_at).dt.to_period("M").dt.start_time)
    cg = c.groupby(["counterparty_id", "month"])
    cc = pd.DataFrame({"commitments_made": cg.size(), "commitments_kept": cg.apply(lambda z: int((z.status == "fulfilled").sum()))})
    cc.index = cc.index.set_names(["supplier_id", "month"])
    sc = sc.join(cc, how="outer").reset_index()
    sc["po_count"] = sc.po_count.fillna(0).astype(int)
    sc["commitments_made"] = sc.commitments_made.fillna(0).astype(int)
    sc["commitments_kept"] = sc.commitments_kept.fillna(0).astype(int)
    return sc.sort_values(["supplier_id", "month"]).reset_index(drop=True)


# =============================================================================
# main
# =============================================================================
def _read(ctx, name, dates):
    df = ctx.table(name).copy()
    for c in dates:
        df[c] = pd.to_datetime(df[c])
    return df


def run(ctx: Ctx):
    import gen_decisions as gd
    import patterns as pt
    banner("STAGE 3 - events, impacts, links, negotiations, decisions, commitments, scorecards, patterns")
    S, R, DM = ctx.load_internal("structure"), ctx.load_internal("rm_ops"), ctx.load_internal("demand")
    N = Names(ctx)
    t0 = pd.Timestamp(R["t0"])
    env = {"S": S, "R": R, "DM": DM, "rms": S["rms"], "D": (lambda d: t0 + pd.Timedelta(days=int(d)))}
    env["runs"] = _read(ctx, "production_runs", ["planned_start", "actual_start"])
    env["rpo"] = _read(ctx, "rm_purchase_orders", ["ordered_at", "promised_at", "expected_at", "received_at"])
    env["rpol"] = ctx.table("rm_purchase_order_lines")
    env["rev"] = _read(ctx, "rm_po_revisions", ["revised_at", "old_expected_at", "new_expected_at"])
    env["snap"] = _read(ctx, "rm_inventory_snapshots_weekly", ["snapshot_date"])
    env["rmv"] = _read(ctx, "rm_inventory_movements", ["movement_at"])
    env["cat"] = R["catalog"].copy()
    env["bom"] = _read(ctx, "bill_of_materials", ["effective_from", "effective_to"])
    env["dem"] = DM["table"].copy()  # provisional shock ids (SHK...) -> remapped below; keeps Stage 3 idempotent
    env["led"] = ctx.load_internal("fg_ledger")
    env["combo_keys"] = list(R["ss"].keys())
    for sp in S["price_spikes"]:
        sp["date"] = pd.Timestamp(sp["date"])
    win_end = pd.Timestamp(ctx.calib["window_end"])
    rng = ctx.rng("stage3")

    book = build_events(ctx, N, EventBook(), env)
    log(f"events (pre-decision): {len(book.E):,}")
    fgl = gd.FGLedger(env["led"], ctx.src["inventory_opening_balances"], ctx.src["products"])
    cands = gd.cascade_decisions(ctx, N, book, env) + gd.mitigation_decisions(ctx, N, book, env) + gd.fg_decisions(ctx, N, book, env, fgl)
    negs = gd.build_negotiations(ctx, N, book, env)
    reneg = gd.renegotiate_decisions(ctx, negs, book, env)
    reneg = reneg[: max(4, int(0.08 * ctx.cfg["scale"]["max_decisions"]))]
    env["n_reneg"] = len(reneg)
    cd_ = pd.DataFrame([{"k": d["key"].split(":")[1], "t": d["type"], "o": d["outcome"], "drv": d["failure_driver"], "best": d["exante_best"]} for d in cands])
    log("candidate outcomes by source:\n" + cd_.groupby(["k", "o"]).size().unstack(fill_value=0).to_string())
    log("failure drivers:\n" + cd_.groupby(["k", "drv"]).size().to_string())
    sel = gd.select_decisions(ctx, cands, env) + reneg
    gd.attribute(sel)
    log(f"decision candidates {len(cands):,}; selected {len(sel)}; attribution {pd.Series([d['attribution'] for d in sel]).value_counts(dropna=False).to_dict()}")

    # ---- events for FG-side decisions
    prod = S["prod"].set_index("product_id")
    for d in sel:
        if d["event_key"] is not None:
            continue
        m = d["meta"]
        k = m.get("fg_kind")
        if k == "expedite":
            r = m["line"]
            e = book.add(f"srv:{r['purchase_order_line_id']}", event_type="service_risk", title=f"{N.sku[r['product_id']]} below reorder point at {N.wh[r['warehouse_id']]}",
                         start_date=d["decided_at"] - 2 * DAY, end_date=r["received_at"], detected_at=d["decided_at"], severity=3 if prod.at[r["product_id"], "abc_class"] == "A" else 2,
                         root_cause_code="low_cover_inbound_due", root_cause_text=f"{N.sku[r['product_id']]} ({r['product_id']}) had {m['balance']:.0f} units at {N.W(r['warehouse_id'])} (ROP {m['rop']}) with {r['purchase_order_id']} from {N.S(r['supplier_id'])} due {r['expected_at']:%Y-%m-%d}.",
                         origin_entity_type="product", origin_entity_id=r["product_id"], anchor_type="existing_data", anchor_ids=d["footprint"])
            e["impacts"].append(("product", r["product_id"], "on_hand_at_detection", round(m["balance"], 1), "units"))
            e["meta"] = {"kind": "fg_service_risk", "family": ("service_risk", r["warehouse_id"]), "tags": [], "supplier_id": r["supplier_id"], "warehouse_id": r["warehouse_id"], "product_id": r["product_id"]}
        elif k == "transfer":
            e = book.add(f"imb:{m['transfer_id']}", event_type="stock_imbalance", title=f"{N.sku[m['product_id']]} short at {N.wh[m['dst']]}, surplus at {N.wh[m['src']]}",
                         start_date=d["decided_at"] - DAY, end_date=d["decided_at"] + 2 * DAY, detected_at=d["decided_at"], severity=2, root_cause_code="uneven_regional_demand",
                         root_cause_text=f"{N.W(m['dst'])} held {m['b_dst']:.0f} units of {m['product_id']} vs ROP {m['rop']}.", origin_entity_type="warehouse", origin_entity_id=m["dst"],
                         anchor_type="existing_data", anchor_ids=d["footprint"])
            e["meta"] = {"kind": "fg_imbalance", "family": ("stock_imbalance", m["src"]), "tags": [], "warehouse_id": m["dst"], "product_id": m["product_id"]}
        elif k == "cancel":
            e = book.add(f"ovs:{m['po']}", event_type="overstock_review", title=f"Overstock review cancels {m['po']} at {N.wh[m['warehouse_id']]}",
                         start_date=d["decided_at"] - 3 * DAY, end_date=d["decided_at"], detected_at=d["decided_at"] - 3 * DAY, severity=1, root_cause_code="excess_cover",
                         root_cause_text=f"Products on {m['po']} were at {m['ratio']:.1f}x reorder point at {N.W(m['warehouse_id'])}.", origin_entity_type="warehouse", origin_entity_id=m["warehouse_id"],
                         anchor_type="existing_data", anchor_ids=[m["po"]])
            e["meta"] = {"kind": "fg_overstock", "family": ("overstock_review", m["warehouse_id"]), "tags": [], "supplier_id": m["supplier_id"], "warehouse_id": m["warehouse_id"]}
        elif k == "accept":
            e = book.add(f"sh1:{m['po']}", event_type="short_shipment", title=f"{N.sup[m['supplier_id']]} short-shipped {m['po']}",
                         start_date=d["decided_at"] - DAY, end_date=d["decided_at"], detected_at=d["decided_at"] - DAY, severity=1, root_cause_code="supplier_capacity",
                         root_cause_text=f"{m['po']} arrived {m['short_units']} units short at {N.W(m['warehouse_id'])}.", origin_entity_type="supplier", origin_entity_id=m["supplier_id"],
                         anchor_type="existing_data", anchor_ids=d["footprint"])
            e["impacts"].append(("supplier", m["supplier_id"], "units_short", m["short_units"], "units"))
            e["meta"] = {"kind": "fg_short", "family": ("short_shipment", m["supplier_id"]), "tags": [], "supplier_id": m["supplier_id"], "warehouse_id": m["warehouse_id"]}
        d["event_key"] = e["key"]

    by_key = {d["key"]: d for d in sel}
    write_texts(sel, N, by_key)
    commits = gd.build_commitments(ctx, N, env, sel, negs)
    # directive lesson: cancelling a PO while a volume commitment to that supplier is open
    for d in sel:
        if d["meta"].get("cancelled"):
            sup = d["meta"]["cascade"]["slipped"][0]["supplier_id"]
            op = [c for c in commits if c["kind"] == "our_volume" and c["cp"] == sup and c["made"] <= d["decided_at"] < c["due"]]
            if op:
                d["lesson"] = (d["lesson"] or "") + f" Cancelling the original PO cut volume under an open volume commitment to {N.S(sup)} - always check open commitments before cancelling."
                d["meta"]["open_commitment_conflict"] = True

    # ---- superseded initial assessments
    cas_events = [e for e in book.E if e["meta"].get("kind") == "cascade"]
    decided = {d["event_key"] for d in sel}
    pick = [e for e in cas_events if e["key"] in decided]
    rng.shuffle(pick)
    n_sup = max(3, int(0.05 * len(pick)))
    from gen_rm_ops import SLIP_CAUSES
    for e in pick[:n_sup]:
        wrong = str(rng.choice([c for c in SLIP_CAUSES if c != e["root_cause_code"]]))
        corr = e["detected_at"] + int(rng.integers(2, 6)) * DAY
        e0 = book.add(f"ini:{e['key']}", event_type=e["event_type"], title=e["title"] + " (initial assessment)", start_date=e["detected_at"], end_date=corr,
                      detected_at=e["detected_at"], severity=max(1, e["severity"] - 1), root_cause_code=wrong,
                      root_cause_text=f"Initial read: {wrong.replace('_', ' ')} at {N.sup[e['origin_entity_id']]}; superseded on {corr:%Y-%m-%d} once the supplier confirmed {e['root_cause_code'].replace('_', ' ')}.",
                      origin_entity_type=e["origin_entity_type"], origin_entity_id=e["origin_entity_id"], anchor_type="rm_side", anchor_ids=e["anchor_ids"][:3])
        e0["meta"] = {"kind": "initial_assessment", "corrected": e["key"], "corrected_on": corr, "family": ("initial", e["key"]), "tags": []}
        e["meta"]["initial"] = e0["key"]

    # ---- links
    links = set()
    E = book.E
    for e in E:
        m = e["meta"]
        if m.get("kind") == "supplier_parent":
            for ch in m["children"]:
                links.add((e["key"], ch, "caused"))
        if m.get("kind") == "shock" and m.get("cause_event"):
            links.add((m["cause_event"], e["key"], "caused"))
        if m.get("kind") == "initial_assessment":
            links.add((e["key"], m["corrected"], "superseded_by"))
        if m.get("kind") == "sub_trigger":
            for p in m["prior"]:
                links.add((p, e["key"], "mitigated_by"))
    for sp in S["price_spikes"]:
        pk = f"price:{sp['rm_id']}:{sp['date']:%Y%m%d}"
        for c in R["cascades"]:
            if c["rm_id"] == sp["rm_id"] and sp["date"] + 14 * DAY <= c["P_date"] <= sp["date"] + 85 * DAY:
                links.add((pk, f"cas:{c['cascade_id']}", "caused"))
    for b in R["mitigations"]["ss_builds"]:
        for c in R["cascades"]:
            if (c["rm_id"], c["plant_id"]) == (b["rm_id"], b["plant_id"]) and pd.Timestamp(b["date"]) - 365 * DAY <= c["X_date"] < pd.Timestamp(b["date"]):
                links.add((f"cas:{c['cascade_id']}", f"ssb:{b['rm_id']}:{b['plant_id']}", "mitigated_by"))
    for a in R["mitigations"]["alloc_changes"]:
        for c in R["cascades"]:
            if c["rm_id"] == a["rm_id"] and c["slipped"][0]["supplier_id"] == a["old_primary"] and c["X_date"] < pd.Timestamp(a["date"]):
                links.add((f"cas:{c['cascade_id']}", f"alc:{a['rm_id']}", "mitigated_by"))
    fam = defaultdict(list)
    for e in sorted(E, key=lambda e: (e["start_date"], e["key"])):
        if e["meta"].get("kind") == "initial_assessment":
            continue
        f = e["meta"].get("family")
        if f:
            fam[f].append(e)
        pat = e["meta"].get("pattern") or ("P03" if "P03" in e["meta"].get("tags", []) and e["meta"].get("kind") == "cascade" else None)
        if pat:
            fam[("pattern", pat)].append(e)
    win = pd.Timedelta(days=ctx.cfg["memory"]["similar_window_days"])
    for f, es in fam.items():
        for i in range(1, len(es)):
            if f[0] == "pattern":
                links.add((es[i]["key"], es[0]["key"], "recurrence_of"))
            elif es[i]["start_date"] - es[i - 1]["start_date"] <= win:
                links.add((es[i]["key"], es[i - 1]["key"], "similar_to"))
    # also similar_to across plants for the same RM shortage (connects related events)
    by_rm = defaultdict(list)
    for e in E:
        if e["meta"].get("kind") == "cascade":
            by_rm[e["meta"]["rm_id"]].append(e)
    for rm_, es in by_rm.items():
        es.sort(key=lambda e: e["start_date"])
        for i in range(1, len(es)):
            if es[i]["meta"]["plant_id"] != es[i - 1]["meta"]["plant_id"] and es[i]["start_date"] - es[i - 1]["start_date"] <= win:
                links.add((es[i]["key"], es[i - 1]["key"], "similar_to"))

    # ---- renumber
    E.sort(key=lambda e: (e["start_date"], e["detected_at"], e["key"]))
    emap = {e["key"]: f"EVT{i + 1:05d}" for i, e in enumerate(E)}
    for e in E:
        e["event_id"] = emap[e["key"]]
    sel.sort(key=lambda d: (d["decided_at"], d["key"]))
    dmap = {d["key"]: f"DEC{i + 1:05d}" for i, d in enumerate(sel)}
    negs.sort(key=lambda n: (n["started_at"], n["key"]))
    nmap = {n["key"]: f"NEG{i + 1:05d}" for i, n in enumerate(negs)}
    commits.sort(key=lambda c: (c["made"], c["text"]))
    for i, c in enumerate(commits):
        c["commitment_id"] = f"CMT{i + 1:05d}"
    idmap = {**nmap}

    def fix_ids(ids):
        return [idmap.get(x, x) for x in ids]

    # ---- tables
    ev_tab = pd.DataFrame([{"event_id": e["event_id"], "event_type": e["event_type"], "title": e["title"], "start_date": e["start_date"], "end_date": e["end_date"],
                            "detected_at": e["detected_at"], "severity": int(e["severity"]), "root_cause_code": e["root_cause_code"], "root_cause_text": e["root_cause_text"],
                            "origin_entity_type": e["origin_entity_type"], "origin_entity_id": e["origin_entity_id"], "anchor_type": e["anchor_type"],
                            "anchor_ids": js(list(dict.fromkeys(e["anchor_ids"])))} for e in E])
    imp = pd.DataFrame([{"event_id": e["event_id"], "entity_type": t, "entity_id": i_, "impact_metric": m_, "impact_value": v, "unit": u} for e in E for (t, i_, m_, v, u) in e["impacts"]])
    imp = imp.drop_duplicates(["event_id", "entity_type", "entity_id", "impact_metric"])
    lk = pd.DataFrame([{"src_event_id": emap[a], "dst_event_id": emap[b], "link_type": t} for a, b, t in links if a in emap and b in emap and a != b]).drop_duplicates()
    ng = pd.DataFrame([{"negotiation_id": nmap[n["key"]], "supplier_id": n["supplier_id"], "rm_id": n["rm_id"], "started_at": n["started_at"], "concluded_at": n["concluded_at"],
                        "topic": n["topic"], "our_ask": n["our_ask"], "their_offer": n["their_offer"], "concessions_json": js(n["concessions"]), "final_terms": n["final_terms"],
                        "outcome": n["outcome"], "contract_id": n["contract_id"]} for n in negs])
    dc = pd.DataFrame([{"decision_id": dmap[d["key"]], "event_id": emap[d["event_key"]], "decided_at": d["decided_at"], "decided_by_role": d["role"], "decision_type": d["type"],
                        "options_considered_json": js(d["options"]), "chosen_option": d["chosen"], "rationale_text": d["rationale"], "expected_cost": round(d["expected_cost"], 2),
                        "expected_stockout_days": d["expected_days"], "actual_cost": None if d["actual_cost"] is None else round(d["actual_cost"], 2),
                        "actual_stockout_days": d["actual_days"], "outcome_label": d["outcome"], "outcome_assessed_at": d["assessed_at"], "lesson_text": d["lesson"],
                        "footprint_ids": js(fix_ids(d["footprint"])), "outcome_attribution": d["attribution"] if d["outcome"] else None} for d in sel])
    # decisions not yet assessable at the cutoff
    late = pd.to_datetime(dc.outcome_assessed_at) > win_end
    dc.loc[late, ["actual_cost", "actual_stockout_days", "outcome_label", "outcome_assessed_at", "lesson_text", "outcome_attribution"]] = None
    cm = pd.DataFrame([{"commitment_id": c["commitment_id"], "decision_id": dmap.get(c["decision_key"]), "negotiation_id": nmap.get(c["neg_key"]), "counterparty_type": c["ctype"],
                        "counterparty_id": c["cp"], "made_by_role": c["by"], "made_at": c["made"], "commitment_text": c["text"], "quantity": c["quantity"], "due_date": c["due"],
                        "penalty_or_credit": c["penalty"], "status": c["status"], "resolved_at": c["resolved"], "evidence_ids": js(fix_ids(c["evidence"]))} for c in commits])
    cm.loc[cm.status == "open", "resolved_at"] = None

    # demand: provisional shock ids -> event ids
    dem = env["dem"]
    shmap = {sh["shock_id"]: emap[f"shk:{sh['shock_id']}"] for sh in DM["shocks"]}
    dem["demand_shock_event_id"] = dem.demand_shock_event_id.map(shmap).where(dem.demand_shock_event_id.notna(), None)

    write_table(ctx, "disruption_events", ev_tab)
    write_table(ctx, "event_impacts", imp)
    write_table(ctx, "event_links", lk)
    write_table(ctx, "negotiations", ng)
    write_table(ctx, "decisions", dc)
    write_table(ctx, "commitments", cm)
    write_table(ctx, "product_demand_weekly", dem)
    sc = compute_scorecard(ctx.src, env["rpo"], env["rpol"], env["rmv"], cm.assign(made_at=pd.to_datetime(cm.made_at)), win_end)
    write_table(ctx, "supplier_scorecard_monthly", sc)

    # ---- planted patterns
    from schema import SCHEMA
    from sc_common import read_typed
    T = {n: read_typed(ctx, n) for n in SCHEMA if (ctx.out / "tables" / f"{n}.csv").exists() or n in ctx.tables}
    for n in ("rm_purchase_orders",):
        T[n] = env["rpo"]
    T["rm_po_revisions"] = env["rev"]
    T["rm_inventory_movements"] = env["rmv"]
    T["production_runs"] = env["runs"]
    T["product_demand_weekly"] = dem
    T["decisions"] = dc
    T["commitments"] = cm
    extra = {"P05": env["P05"], **({"P12": env["p12"]} if "p12" in env else {}), "fg_transfer_source_note": env["P08"]}
    pats = pt.measure(S["pattern_plan"], extra, T, ctx.src, ctx.calib)
    for p in pats:
        pid = p["pattern_id"]
        rel = [e["event_id"] for e in E if e["meta"].get("pattern") == pid or pid in e["meta"].get("tags", [])]
        if pid == "P05":
            rel = [emap[d["event_key"]] for d in sel if d["context"].get("warehouse_surcharge")]
        if pid == "P08":
            rel = [emap[f"cas:{c['cascade_id']}"] for c in R["cascades"] if c.get("transfer") and c["transfer"]["donor_plant"] == S["pattern_plan"]["P08"]["plant_id"]]
        p["related_event_ids"] = rel[:60]
    dump_json({"patterns": pats, "plan": S["pattern_plan"], "extra": extra}, ctx.out / "planted_patterns.json")

    ctx.save_internal("memory", {"events": E, "emap": emap, "decisions": sel, "dmap": dmap, "negs": negs, "nmap": nmap, "commits": commits,
                                 "links": list(links), "patterns": pats, "extra": extra})
    # ---- summary
    log(f"events {len(ev_tab):,} {ev_tab.event_type.value_counts().head(12).to_dict()}")
    log(f"anchor types {ev_tab.anchor_type.value_counts().to_dict()}; links {lk.link_type.value_counts().to_dict()}")
    log(f"decisions {len(dc)}: types {dc.decision_type.value_counts().to_dict()}")
    log(f"outcomes {dc.outcome_label.value_counts(dropna=False).to_dict()}; attribution {dc.outcome_attribution.value_counts(dropna=False).to_dict()}")
    log(f"commitments {len(cm)}: {cm.status.value_counts(normalize=True).round(3).to_dict()}")
    log(f"negotiations {len(ng)}: {ng.topic.value_counts().to_dict()}")
    for p in pats:
        log(f"  {p['pattern_id']} {'OK ' if p['recoverable'] else 'WEAK'} {p['name']}: {p['effect']} p={p['p_value']}")
