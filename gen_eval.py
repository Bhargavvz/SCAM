"""Stage 4b - evaluation set.

eval_questions.jsonl  (>=200 at full scale; 8 question types; gold docs all dated <= as_of_date and in the
                       ingested memory split - no temporal leakage)
holdout_scenarios.json (12-15 Q4-2025 disruptions: day-0 planner report, gold precedents, open commitments,
                        candidate actions with counterfactually simulated consequences, gold best action; >=2 traps)

select_holdout_scenarios() is also used by Stage 4a so that scenario precedents are narrated in memory.
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
import pandas as pd

from common import Ctx, banner, cli

D = pd.Timestamp


# ------------------------------------------------------------------ counterfactual option simulator (tables only)
class Oracle:
    """Re-simulates one RM x plant from a decision date using only the published tables:
    on-hand from opening + movements, realised consumption, realised inbound receipts, catalog lead times."""

    def __init__(self, ctx: Ctx):
        self.ctx, r = ctx, ctx.read
        self.cfg = ctx.cfg
        self.ob = r("rm_inventory_opening_balances").set_index(["rm_id", "plant_id"]).opening_units
        mv = r("rm_inventory_movements")
        self.mv = {k: g.sort_values("movement_at") for k, g in mv.groupby(["rm_id", "plant_id"])}
        self.po = r("rm_purchase_orders").set_index("rm_po_id")
        self.pol = r("rm_purchase_order_lines")
        self.cat = r("rm_supplier_catalog")
        self.rm = r("raw_materials").set_index("rm_id")
        self.plants = r("plants").set_index("plant_id")
        S = ctx.src()["suppliers"].set_index("supplier_id")
        self.country, self.rel = S.country_code.to_dict(), S.reliability_score.to_dict()
        self.plan = ctx.load_json("rm_ops_plan.json")
        self.pat = ctx.load_json("pattern_plan.json")
        self.shocks = self.plan["shocks"]
        self.sim0 = D(self.plan["sim_start"])
        self.runs = r("production_runs")
        self.sn = r("rm_inventory_snapshots_weekly")

    def onhand_before(self, rm_, pl, t):
        g = self.mv[(rm_, pl)]
        return float(self.ob[(rm_, pl)] + g[g.movement_at < t].quantity_change.sum())

    def cons(self, rm_, pl, t, H):
        g = self.mv[(rm_, pl)]
        c = g[(g.movement_type == "consumption") & (g.movement_at >= t) & (g.movement_at < t + pd.Timedelta(days=H))]
        arr = np.zeros(H)
        np.add.at(arr, (c.movement_at - t).dt.days.to_numpy(), -c.quantity_change.to_numpy())
        return arr, c

    def inbound(self, rm_, pl, t, H, exclude_po=None):
        g = self.mv[(rm_, pl)]
        rc = g[(g.movement_type.isin(["receipt", "transfer"])) & (g.quantity_change > 0) & (g.movement_at >= t) &
               (g.movement_at < t + pd.Timedelta(days=H))]
        if exclude_po:
            lines = set(self.pol[self.pol.rm_po_id.isin(exclude_po)].rm_purchase_order_line_id)
            rc = rc[~rc.rm_purchase_order_line_id.isin(lines)]
        # spot buys are reactions to the realised path, not planned supply
        spot = set(self.po[(self.po.ordered_at == self.po.received_at) & (self.po.ordered_at == self.po.expected_at)].index)
        spot_lines = set(self.pol[self.pol.rm_po_id.isin(spot)].rm_purchase_order_line_id)
        rc = rc[~rc.rm_purchase_order_line_id.isin(spot_lines)]
        return [(int((d - t).days), q) for d, q in zip(rc.movement_at, rc.quantity_change)]

    def shock_extra(self, sup, d):
        c = self.country.get(sup)
        for s in self.shocks:
            if s["country_code"] == c and s["start"] <= (d - self.sim0).days <= s["end"]:
                return float(np.mean(s["extra"])) * (0.85 if s["planted"] else 0.7)
        return 0.0

    def expected_delay(self, sup, d, lead):
        pp = self.cfg["patterns"]
        x = (1 - self.rel[sup]) * 1.6 * max(1.0, lead * self.cfg["rm_ops"]["delay_scale_frac_of_lead"])
        x += self.shock_extra(sup, d)
        if sup == self.pat["q4_slip_supplier"] and d.month in (11, 12):
            x += float(np.mean(pp["q4_slip_supplier"]["extra_delay"]))
        if sup == self.pat["lead_time_creep_supplier"] and d >= D(pp["lead_time_creep_supplier"]["from"]):
            x += pp["lead_time_creep_supplier"]["creep"] * lead
        return x

    def simulate(self, rm_, pl, t, trig_po, orig_arrival, qty, H=35):
        """Returns candidate actions with simulated consequences (oracle view of the realised future)."""
        c = self.cfg["costs"]
        oh = self.onhand_before(rm_, pl, t)
        cons, crow = self.cons(rm_, pl, t, H)
        base_in = self.inbound(rm_, pl, t, H, exclude_po=[trig_po])
        cat = self.cat[(self.cat.rm_id == rm_) & (self.cat.price_valid_from <= t) &
                       (self.cat.price_valid_to.isna() | (self.cat.price_valid_to >= t))]
        trig_sup = self.po.at[trig_po, "supplier_id"]
        price = float(cat[cat.supplier_id == trig_sup].unit_price.iat[0]) if (cat.supplier_id == trig_sup).any() else float(cat.unit_price.mean())
        daily = float(self.sn[(self.sn.rm_id == rm_) & (self.sn.plant_id == pl)].pipe(lambda s: s.safety_stock_units.iat[0] if len(s) else 0)) / \
            self.cfg["rm_ops"]["safety_days"][self.rm.at[rm_, "criticality"]]
        daycost = daily * price * c["shortage_value_multiple"]
        runs = self.runs[(self.runs.plant_id == pl)].set_index("production_run_id")
        run_ids = crow.production_run_id.to_numpy()
        run_day = (crow.movement_at - t).dt.days.to_numpy()

        def outcome(extra_in, direct):
            arr = -cons.copy()
            for d, q in base_in + extra_in:
                if 0 <= d < H:
                    arr[d] += q
            traj = oh + np.cumsum(arr)
            short = traj < -1e-9
            days = int(short.sum())
            units = int(sum(runs.at[r_, "planned_qty"] for r_, d in zip(run_ids, run_day) if short[d]) if days else 0)
            return {"est_cost": round(direct + days * daycost, 2), "stockout_days": days, "fg_units_at_risk": units,
                    "direct_cost": round(direct, 2)}

        q4 = t.month in (10, 11, 12)
        succ = c["expedite_success"]["q4_slip_supplier"] if (trig_sup == self.pat["q4_slip_supplier"] and t.month in (11, 12)) else \
            (c["expedite_success"]["q4"] if q4 else c["expedite_success"]["base"])
        orig_d = int((orig_arrival - t).days)
        acts = []
        acts.append({"action": "accept_delay", "description": f"accept the supplier ETA on {trig_po}; cover gaps with spot buys",
                     **outcome([(orig_d, qty)], 0.0)})
        k = 6
        fee = qty * price * float(np.mean(self.cfg["rm_ops"]["expedite_fee_rate"])) * \
            (self.cfg["patterns"]["expedite_cost_plant"]["multiplier"] if pl == self.pat["expedite_cost_plant"] else 1.0)
        acts.append({"action": "expedite", "description": f"pay premium freight to pull {trig_po} in by ~{k} days (success odds {succ:.0%} in this context)",
                     **outcome([(max(2, orig_d - int(round(k * succ))), qty)], fee)})
        alts = cat[cat.supplier_id != trig_sup].sort_values(["qualified_flag", "lead_time_days"], ascending=[False, True])
        if len(alts):
            a = alts.iloc[0]
            qual = 0 if a.qualified_flag else c["qualification_days"]
            arr_d = int(a.lead_time_days + qual + round(self.expected_delay(a.supplier_id, t + pd.Timedelta(days=int(a.lead_time_days)), a.lead_time_days)))
            direct = max(0.0, float(a.unit_price) - price) * qty + c["switch_admin"] + (0 if a.qualified_flag else c["qualification_cost"])
            acts.append({"action": "switch_supplier", "description": f"cancel {trig_po} and re-source from {a.supplier_id} (lead {int(a.lead_time_days)}d"
                                                                     f"{', needs qualification' if not a.qualified_flag else ''})",
                         "alt_supplier_id": a.supplier_id, **outcome([(arr_d, qty)], direct)})
        # reallocation from the plant holding the most of this RM
        others = [(p_, self.onhand_before(rm_, p_, t)) for (r_, p_) in self.mv if r_ == rm_ and p_ != pl]
        if others:
            p_, st = max(others, key=lambda x: x[1])
            ss_o = self.sn[(self.sn.rm_id == rm_) & (self.sn.plant_id == p_)].safety_stock_units.median()
            avail = st - ss_o
            if avail > 0:
                from gen_structure import transit_days
                tr = transit_days(self.cfg, self.plants.at[p_, "region"], self.plants.at[pl, "region"]) + 1
                q = min(qty, avail * 0.5)
                direct = q * price * c["transfer_freight_rate"] + c["transfer_fixed"]
                acts.append({"action": "reallocate_stock", "description": f"transfer {q:,.1f} {self.rm.at[rm_, 'uom']} from {p_} ({tr}d transit)",
                             "source_plant_id": p_, **outcome([(tr, q), (orig_d, qty)], direct)})
        grp = self.rm.at[rm_, "substitute_group_id"]
        if pd.notna(grp):
            acts.append({"action": "substitute_rm", "description": f"qualify a substitute from group {grp} ({c['substitute_qualification_days']}d qualification)",
                         **outcome([(c["substitute_qualification_days"], qty), (orig_d, qty)], float(c["substitute_cost"]))})
        return acts, {"on_hand_start": round(oh, 2), "daily_usage": round(daily, 3), "shortage_day_cost": round(daycost, 2), "unit_price": price}


# ------------------------------------------------------------------ holdout scenarios
def select_holdout_scenarios(ctx: Ctx) -> list[dict]:
    p = ctx.work / "holdout_plan.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    rng = ctx.rng("holdout")
    r = ctx.read
    ev, dec = r("disruption_events"), r("decisions")
    meta = ctx.load_json("memory_meta.json")
    hold = D(ctx.cfg["holdout_start"])
    orc = Oracle(ctx)
    evx = ev.set_index("event_id")
    dmeta = meta["decisions"]
    cand = []
    for d in dec.itertuples():
        m = dmeta[d.decision_id]
        if m.get("side") != "rm" or not m.get("refs", {}).get("trigger_po_id") or d.decided_at < hold:
            continue
        if evx.at[d.event_id, "detected_at"] < hold or d.decision_type == "build_safety_stock":
            continue
        cand.append((d, m))
    # prefer diversity of event types / suppliers
    rng.shuffle(cand)
    seen_ev, out = set(), []
    pre = ev[ev.start_date < hold]
    dec_by_ev = dec.groupby("event_id")
    for d, m in sorted(cand, key=lambda x: (x[1]["supplier_id"] != ctx.load_json("pattern_plan.json")["q4_slip_supplier"], rng.random())):
        if d.event_id in seen_ev:
            continue
        e = evx.loc[d.event_id]
        refs = m["refs"]
        po_id = refs["trigger_po_id"]
        plan_dec = next((x for x in orc.plan["decisions"] if x.get("refs", {}).get("trigger_po_id") == po_id and x["t"] == (d.decided_at - orc.sim0).days), None)
        if plan_dec is None:
            continue
        orig = orc.sim0 + pd.Timedelta(days=int(plan_dec["orig_receipt"]))
        qty = float(orc.pol[orc.pol.rm_po_id == po_id].quantity_ordered.sum())
        acts, ctxinfo = orc.simulate(m["rm_id"], m["plant_id"], d.decided_at, po_id, orig, qty)
        best = min(acts, key=lambda a: a["est_cost"])
        # precedents: surface similarity vs context similarity
        sc = []
        for pe in pre.itertuples():
            pm = meta["events"].get(pe.event_id, {})
            if pe.event_id not in dec_by_ev.groups:
                continue
            surf = 2 * (pe.origin_entity_id == e.origin_entity_id) + 2 * (pe.event_type == e.event_type) + 2 * (pm.get("rm_id") == m["rm_id"]) + \
                (m["plant_id"] in (pm.get("plants") or [pm.get("plant_id")]))
            if surf < 2:
                continue
            q4p = pe.detected_at.month in (10, 11, 12)
            ctxs = surf + 3 * (q4p == (d.decided_at.month in (10, 11, 12))) + 2 * (pe.root_cause_code == e.root_cause_code)
            pdec = dec_by_ev.get_group(pe.event_id)
            sc.append((pe.event_id, surf + pe.start_date.value / 1e20, ctxs, pdec.decision_type.tolist(), pdec.outcome_label.tolist()))
        if len(sc) < 2:
            continue
        surface_top = max(sc, key=lambda x: x[1])
        gold = [x[0] for x in sorted(sc, key=lambda x: -x[2] - x[1] * 0.01)[:3]]
        trap = (surface_top[0] not in gold[:2] and "success" in surface_top[4] and
                surface_top[3][surface_top[4].index("success")] != best["action"])
        out.append({"event_id": d.event_id, "decision_id": d.decision_id, "decided_at": str(d.decided_at.date()),
                    "rm_id": m["rm_id"], "plant_id": m["plant_id"], "supplier_id": m["supplier_id"], "trigger_po_id": po_id,
                    "belief_eta": str((orc.sim0 + pd.Timedelta(days=int(plan_dec["belief_eta"]))).date()),
                    "orig_arrival": str(orig.date()), "qty": qty, "actions": acts, "context": ctxinfo, "best": best["action"],
                    "gold_precedents": gold, "surface_precedent": surface_top[0], "trap": bool(trap),
                    "actual_decision": d.decision_type, "actual_outcome": d.outcome_label})
        seen_ev.add(d.event_id)
    n = int(ctx.sc["holdout_scenarios"])
    traps = [x for x in out if x["trap"]]
    rest = [x for x in out if not x["trap"]]
    chosen = traps[:max(2, min(len(traps), n // 3))] + rest
    chosen = sorted(chosen[:n], key=lambda x: x["decided_at"])
    ctx.save_json(chosen, "holdout_plan.json")
    return chosen


# ------------------------------------------------------------------ questions
class QGen:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.rng = ctx.rng("eval")
        self.docs = [json.loads(l) for l in (ctx.output / "memory_corpus.jsonl").read_text(encoding="utf-8").splitlines()]
        self.mem = [d for d in self.docs if d["split"] == "memory" and not d["is_noise"]]
        self.by_id = {d["doc_id"]: d for d in self.docs}
        self.facts = pd.read_parquet(ctx.work / "doc_facts.parquet")
        self.facts = self.facts[self.facts.doc_id.isin({d["doc_id"] for d in self.mem})]
        self.chains = ctx.load_json("chains.json")
        r = ctx.read
        self.ev, self.dec, self.cm = r("disruption_events").set_index("event_id"), r("decisions").set_index("decision_id"), r("commitments").set_index("commitment_id")
        self.qs = []
        self.hold = D(ctx.cfg["holdout_start"])

    def date(self, doc_id):
        return self.by_id[doc_id]["timestamp"][:10]

    def add(self, q, typ, hops, gold, docs, recs):
        docs = list(dict.fromkeys(docs))
        if not docs or any(self.by_id[x]["split"] != "memory" for x in docs):
            return
        as_of = max(self.date(x) for x in docs)
        self.qs.append({"question": q, "type": typ, "hop_count": int(hops), "as_of_date": as_of, "gold_answer": gold,
                        "gold_doc_ids": docs, "gold_record_ids": list(dict.fromkeys(recs))})

    def fact_recall(self, n):
        f = self.facts[self.facts.kind.isin(["po_reason", "price", "qc_reject", "negotiation_terms", "scorecard"])]
        for r in f.sample(n=min(n, len(f)), random_state=1).itertuples():
            if r.kind == "po_reason":
                self.add(f"What reason did the supplier give when it first revised the ETA of {r.key}?", "fact recall", 1, r.value, [r.doc_id], [r.record_id])
            elif r.kind == "price":
                k = r.key.split("|")
                self.add(f"What unit price was quoted for {k[0]} from {k[1]} in the notice dated {self.date(r.doc_id)}?", "fact recall", 1, r.value, [r.doc_id], [r.record_id])
            elif r.kind == "qc_reject":
                self.add(f"How much material was rejected at incoming QC on receipt line {r.key}?", "fact recall", 1, r.value, [r.doc_id], [r.record_id])
            elif r.kind == "negotiation_terms":
                self.add(f"What final terms were agreed in negotiation {r.key}?", "fact recall", 1, r.value, [r.doc_id], [r.record_id])
            elif r.kind == "scorecard":
                k = r.key.split("|")
                self.add(f"What OTIF did {k[0]} record for {k[1]} according to the QBR notes?", "fact recall", 1, r.value, [r.doc_id], [r.record_id])

    def temporal(self, n):
        eta = self.facts[self.facts.kind == "po_eta"].sort_values("ts")
        g = [x for _, x in eta.groupby("key") if len(x) >= 2]
        self.rng.shuffle(g)
        for x in g[:n // 2]:
            a = x.iloc[0]
            self.add(f"On what date did we first receive a delay notice for {a.key}, and what ETA did it give?", "temporal", 1,
                     f"{a.ts[:10]}; ETA {a.value}", [a.doc_id], [a.record_id])
        for c in [c for c in self.chains if c.get("stockout_date") and c.get("first_notice_doc")][: n - n // 2]:
            d1, d2 = c["first_notice_doc"], c["stockout_doc"]
            gap = (D(c["stockout_date"]) - D(c["first_notice_date"])).days
            self.add(f"How many days passed between the first supplier delay notice for {c['rm_id']} at {c['plant_id']} and the plant running short "
                     f"(event {c['event_id']})?", "temporal", 2, f"{gap} days ({c['first_notice_date']} -> {c['stockout_date']})", [d1, d2], [c["event_id"]])

    def causal(self, n):
        cs = [c for c in self.chains if c.get("wh_doc") and c.get("first_notice_doc") and c.get("stockout_doc")]
        self.rng.shuffle(cs)
        for c in cs[:n]:
            self.add(f"Why did FG line {c['fg_line']} arrive late at {c['warehouse_id']} around {c['fg_received']}?", "causal-why", 3,
                     f"Its production run at {c['plant_id']} was held because {c['rm_id']} ran out; {c['supplier_id']} slipped {c['rm_po_id']} "
                     f"({c['reason']}), so the plant stocked out on {c['stockout_date']}.",
                     [c["wh_doc"], c["stockout_doc"], c["first_notice_doc"]], [c["fg_line"], c["rm_po_id"], c["event_id"]])

    def commitments(self, n):
        f = self.facts[self.facts.kind == "commitment_status"]
        made = self.facts[self.facts.kind == "commitment_made"].set_index("key")
        f = f[f.key.isin(made.index) & (f.value != "open")]
        for r in f.sample(n=min(n, len(f)), random_state=2).itertuples():
            m = made.loc[r.key]
            m = m.iloc[0] if isinstance(m, pd.DataFrame) else m
            c = self.cm.loc[r.key]
            self.add(f"Was the commitment made on {str(c.made_at.date())} by {c.counterparty_id} ('{c.commitment_text}') kept?", "commitment tracking", 2,
                     f"{r.value} (resolved {r.ts[:10]})", [m.doc_id, r.doc_id], [r.key])

    def precedent(self, n):
        pm = self.facts[self.facts.kind == "lesson"]
        for r in pm.sample(n=min(n, len(pm)), random_state=3).itertuples():
            d = self.dec.loc[r.record_id]
            e = self.ev.loc[d.event_id]
            memo = self.facts[(self.facts.kind == "decision") & (self.facts.record_id == r.record_id)]
            docs = ([memo.doc_id.iat[0]] if len(memo) else []) + [r.doc_id]
            self.add(f"The last time we faced '{e.title}', what did we decide, how did it turn out, and what was the lesson?", "precedent/analogy",
                     2, f"{d.decision_type} -> {d.outcome_label}; lesson: {d.lesson_text}", docs, [r.record_id, d.event_id])

    def consequence(self, n):
        memos = self.facts[self.facts.kind == "decision"]
        for r in memos.sample(n=min(n, len(memos)), random_state=4).itertuples():
            d = self.dec.loc[r.record_id]
            opts = json.loads(d.options_considered_json)
            if len(opts) < 2:
                continue
            desc = "; ".join(f"{o['option']}: est. cost {o['est_cost']:,.0f}, {o['est_stockout_days']} stockout days" for o in opts)
            pmx = self.facts[(self.facts.kind == "lesson") & (self.facts.record_id == r.record_id)]
            if len(pmx) and self.rng.random() < 0.5:
                self.add(f"For the decision on {d.chosen_option!r}, how did the actual cost and stockout days compare with the estimate?",
                         "consequence evaluation", 2, f"estimated {d.expected_cost:,.0f} / {d.expected_stockout_days} days; actual {d.actual_cost:,.0f} / "
                         f"{d.actual_stockout_days} days ({d.outcome_label})", [r.doc_id, pmx.doc_id.iat[0]], [r.record_id])
            else:
                self.add(f"Which options were weighed for event '{self.ev.at[d.event_id, 'title']}' and what were their estimated consequences?",
                         "consequence evaluation", 1, desc, [r.doc_id], [r.record_id])

    def supersession(self, n):
        for kind, q in (("po_eta", "What is the latest expected arrival date for {k} as of {d}?"),
                        ("primary", "Who is the primary supplier for {k} as of {d}?"),
                        ("price", "What is the current unit price for {k} as of {d}?")):
            f = self.facts[self.facts.kind == kind].sort_values("ts")
            groups = [x for _, x in f.groupby("key") if x.value.nunique() >= 2]
            self.rng.shuffle(groups)
            for x in groups[: max(1, n // 3)]:
                last = x.iloc[-1]
                k = last.key.replace("|", " from ")
                self.add(q.format(k=k, d=last.ts[:10]), "contradiction/supersession", 2,
                         f"{last.value} (superseding {x.iloc[0].value} stated on {x.iloc[0].ts[:10]})", x.doc_id.tolist(), [last.record_id])

    def multihop(self, n):
        cs = [c for c in self.chains if c.get("wh_doc") and c.get("first_notice_doc") and c.get("commit_doc")]
        self.rng.shuffle(cs)
        for c in cs[:n]:
            self.add(f"Which supplier's delay caused FG line {c['fg_line']} to arrive late, and did that supplier keep the delivery commitment it "
                     f"made during the incident?", "multi-hop", 4,
                     f"{c['supplier_id']} (slip on {c['rm_po_id']}); commitment {c['commit_id']} was {c['commit_status']}",
                     [c["wh_doc"], c["stockout_doc"], c["first_notice_doc"], c["commit_doc"]] + ([c["commit_status_doc"]] if c.get("commit_status_doc") else []),
                     [c["fg_line"], c["rm_po_id"], c["commit_id"]])
        cs2 = [c for c in self.chains if c.get("memo_doc") and c.get("pm_doc") and c.get("first_notice_doc") and c not in cs[:n]]
        for c in cs2[: max(0, n // 2)]:
            self.add(f"For the {c['rm_id']} shortage at {c['plant_id']} (event {c['event_id']}), what triggered it, what action was taken, and did the action "
                     f"meet its estimate?", "multi-hop", 3, f"trigger: {c['supplier_id']} slip on {c['rm_po_id']}; action: {c['decision_type']}; outcome: {c['outcome']}",
                     [c["first_notice_doc"], c["memo_doc"], c["pm_doc"]], [c["event_id"], c["decision_id"]])

    def build(self, total):
        per = max(10, total // 8)
        self.fact_recall(per)
        self.temporal(per)
        self.causal(per)
        self.commitments(per)
        self.precedent(per)
        self.consequence(per)
        self.supersession(per)
        self.multihop(per)
        seen, out = set(), []
        for q in self.qs:
            if q["question"] not in seen:
                seen.add(q["question"])
                out.append(q)
        out.sort(key=lambda q: (q["as_of_date"], q["type"]))
        for i, q in enumerate(out, 1):
            q["question_id"] = f"Q{i:04d}"
        return out


def day0_report(ctx: Ctx, s: dict, docs_by_id=None) -> str:
    r = ctx.read
    rmx = r("raw_materials").set_index("rm_id")
    S = ctx.src()["suppliers"].set_index("supplier_id")
    po = r("rm_purchase_orders").set_index("rm_po_id")
    rev = r("rm_po_revisions")
    rv = rev[(rev.rm_po_id == s["trigger_po_id"]) & (rev.revised_at <= D(s["decided_at"]))]
    last = rv.iloc[-1] if len(rv) else None
    c = s["context"]
    uom = rmx.at[s["rm_id"], "uom"]
    parts = [f"Planner report {s['decided_at']}: {S.at[s['supplier_id'], 'supplier_name']} ({s['supplier_id']}) has advised that {s['trigger_po_id']} "
             f"({s['qty']:,.1f} {uom} of {rmx.at[s['rm_id'], 'rm_name']}, {s['rm_id']}) for {s['plant_id']} will not arrive on "
             f"{str(po.at[s['trigger_po_id'], 'promised_at'].date())}."]
    if last is not None:
        parts.append(f"Latest supplier ETA is {str(last.new_expected_at.date())} (reason given: {last.reason_code.replace('_', ' ')}).")
    parts.append(f"{s['plant_id']} holds about {c['on_hand_start']:,.1f} {uom} against usage of roughly {c['daily_usage']:,.1f} {uom}/day. "
                 f"What should we do?")
    return " ".join(parts)


def run(ctx: Ctx):
    banner("STAGE 4b - evaluation set + holdout scenarios")
    scen = select_holdout_scenarios(ctx)
    g = QGen(ctx)
    qs = g.build(int(ctx.sc["eval_questions"]))
    with open(ctx.output / "eval_questions.jsonl", "w", encoding="utf-8") as f:
        for q in qs:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    cm = ctx.read("commitments")
    out = []
    for i, s in enumerate(scen, 1):
        t = D(s["decided_at"])
        open_c = cm[(cm.counterparty_id.isin([s["supplier_id"]])) & (cm.made_at <= t) & (cm.resolved_at.isna() | (cm.resolved_at > t))]
        best = next(a for a in s["actions"] if a["action"] == s["best"])
        alt = sorted(s["actions"], key=lambda a: a["est_cost"])
        reasoning = (f"{best['action']} has the lowest simulated total cost ({best['est_cost']:,.0f}: direct {best['direct_cost']:,.0f} + "
                     f"{best['stockout_days']} stockout days) vs " + ", ".join(f"{a['action']} {a['est_cost']:,.0f}/{a['stockout_days']}d" for a in alt if a is not best) + ".")
        if s["trap"]:
            reasoning += (f" The most similar-looking precedent ({s['surface_precedent']}) succeeded with a different action, but under different "
                          f"conditions; the context-matched precedents are {', '.join(s['gold_precedents'][:2])}.")
        if best["action"] == "switch_supplier" and len(open_c):
            reasoning += f" Before cancelling {s['trigger_po_id']}, check open commitments {', '.join(open_c.commitment_id[:3])}."
        out.append({"scenario_id": f"HS{i:02d}", "event_id": s["event_id"], "as_of_date": s["decided_at"],
                    "day0_report": day0_report(ctx, s), "rm_id": s["rm_id"], "plant_id": s["plant_id"], "supplier_id": s["supplier_id"],
                    "trigger_po_id": s["trigger_po_id"], "gold_precedent_event_ids": s["gold_precedents"],
                    "relevant_open_commitments": [{"commitment_id": c.commitment_id, "text": c.commitment_text, "due_date": str(c.due_date.date()),
                                                   "status_at_day0": "open"} for c in open_c.itertuples()][:6],
                    "candidate_actions": s["actions"], "simulation_context": s["context"], "gold_best_action": s["best"],
                    "gold_reasoning": reasoning, "trap": s["trap"], "trap_precedent_event_id": s["surface_precedent"] if s["trap"] else None,
                    "actual_decision_taken": s["actual_decision"], "actual_outcome": s["actual_outcome"]})
    (ctx.output / "holdout_scenarios.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    t = pd.Series([q["type"] for q in qs]).value_counts()
    print(f"eval questions {len(qs)}: {t.to_dict()}")
    print(f"holdout scenarios {len(out)} (traps {sum(o['trap'] for o in out)}); best actions {pd.Series([o['gold_best_action'] for o in out]).value_counts().to_dict()}")


if __name__ == "__main__":
    run(cli(__doc__))
