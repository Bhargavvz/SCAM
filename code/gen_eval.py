"""Stage 4b - evaluation questions (eval_questions.jsonl) and holdout scenarios (holdout_scenarios.json).

Questions are generated from the facts behind MEMORY-split documents only, and
as_of_date is always >= the latest gold document date (no temporal leakage).
Holdout scenarios are Q4 disruptions (docs not ingested) with day-0 reports,
precedents, open commitments, simulated consequences of candidate actions and
the gold best action (>= 2 scenarios have a misleading 'most similar' precedent).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

import consequences as cq
from sc_common import Ctx, banner, dump_json, log, read_typed

DAY = pd.Timedelta(days=1)
F = lambda d: pd.Timestamp(d).strftime("%Y-%m-%d")  # noqa: E731


def run(ctx: Ctx):
    banner("STAGE 4b - evaluation questions + holdout scenarios")
    cfg = ctx.cfg
    rng = ctx.rng("eval")
    docs = ctx.load_internal("corpus")
    M = ctx.load_internal("memory")
    from gen_events import Names
    N = Names(ctx)
    hold = pd.Timestamp(ctx.calib["holdout_start"])
    mem = [d for d in docs if d["split"] == "memory" and not d["is_noise"]]
    by_story = defaultdict(list)
    for d in mem:
        by_story[d["story"]].append(d)
    facts = [(d, f) for d in mem for f in d["facts"]]
    kind = defaultdict(list)
    for d, f in facts:
        kind[f["kind"]].append((d, f))
    Q = []

    def asof(dates, lo=1, hi=21):
        a = max(pd.Timestamp(x) for x in dates) + int(rng.integers(lo, hi)) * DAY
        return F(min(a, hold - DAY))

    def add(qtype, q, a, gdocs, recs, hop, as_of=None, extra=None):
        dates = [d["date"] for d in gdocs]
        Q.append({"question": q, "type": qtype, "hop_count": hop, "as_of_date": as_of or asof(dates), "gold_answer": a,
                  "gold_doc_ids": [d["doc_id"] for d in gdocs], "gold_record_ids": list(dict.fromkeys([r for r in recs if r])), **(extra or {})})

    def story_doc(story, k):
        return next(((d, f) for d in by_story[story] for f in d["facts"] if f["kind"] == k), (None, None))

    # ---------------- fact recall
    for d, f in kind["slip"]:
        add("fact_recall", f"What reason did {N.sup[f['supplier']]} give for delaying {f['po']}, and what new date did they quote?",
            f"{f['reason'].replace('_', ' ')}; new expected date {f['new']} (was {f['old']}).", [d], [f["po"], f["revision_id"]], 1)
    for d, f in kind["neg_close"]:
        add("fact_recall", f"What terms did we agree with {N.sup[f['supplier']]} in negotiation {f['negotiation']}?", f"{f['terms']} (outcome: {f['outcome']}).", [d], [f["negotiation"]], 1)
    for d, f in kind["qbr"]:
        add("fact_recall", f"What on-time-in-full rate did {N.sup[f['supplier']]} ({f['supplier']}) post in {f['quarter']}?",
            f"{f['otif']:.0%} across {f['po_count']} POs due, average delay {f['avg_delay']:.1f} days.", [d], [f["supplier"]], 1)
    # ---------------- temporal
    for st, ds in by_story.items():
        d1, f1 = story_doc(st, "below_ss")
        d2, f2 = story_doc(st, "blocked")
        if d1 and d2:
            gap = (pd.Timestamp(f2["P"]) - pd.Timestamp(f1["date"])).days
            add("temporal", f"When did {N.rm[f1['rm']]} at {N.plant[f1['plant']]} fall below safety stock before run {f2['run']} was blocked, and how many days before the blocked start was that?",
                f"{f1['date']}; {gap} days before the planned start {f2['P']}.", [d1, d2], [f2["run"], f2["event"]], 2)
    for d, f in kind["slip"]:
        add("temporal", f"By how many days did {f['po']} slip according to the buyer note, and when was the original promise?",
            f"{(pd.Timestamp(f['new']) - pd.Timestamp(f['old'])).days} days (from {f['old']} to {f['new']}); originally promised {f['promised']}.", [d], [f["po"], f["revision_id"]], 1)
    # ---------------- causal why
    for st in by_story:
        d1, f1 = story_doc(st, "slip")
        d2, f2 = story_doc(st, "blocked")
        if d1 and d2:
            add("causal_why", f"Why could production run {f2['run']} at {N.plant[f2['plant']]} not start on {f2['P']}?",
                f"{N.rm[f2['rm']]} ({f2['rm']}) was short because {N.sup[f1['supplier']]} slipped {f1['po']} from {f1['old']} to {f1['new']} ({f1['reason'].replace('_', ' ')}).",
                [d1, d2], [f2["run"], f1["po"], f1["revision_id"], f2["event"]], 2)
    for d, f in kind["root_cause_final"]:
        di, fi = story_doc(d["story"], "root_cause_initial")
        if di:
            add("causal_why", f"What was the confirmed root cause of the delay on {f['po']}?", f"{f['cause'].replace('_', ' ')} (initially reported as {f['wrong'].replace('_', ' ')}).",
                [di, d], [f["po"], f["event"]], 2)
    # ---------------- commitment tracking
    res_docs = {f["decision"]: d for d, f in kind["outcome"]}
    for d, f in kind["commitment"]:
        made, due = pd.Timestamp(f["made"]), pd.Timestamp(f["due"])
        resolved = pd.Timestamp(f["resolved"]) if f.get("resolved") else None
        a1 = made + max(1, (min(due, resolved or due) - made).days // 2) * DAY
        if a1 < hold:
            add("commitment_tracking", f"As of {F(a1)}, what is the status of this commitment: '{f['text']}'?",
                f"Open - due {f['due']}, not yet resolved as of {F(a1)}.", [d], [f["commitment"]], 1, as_of=F(a1))
        rd = res_docs.get(f.get("decision"))
        if resolved is not None and rd is not None and rd["date"] >= resolved and rd["date"] < hold:
            add("commitment_tracking", f"Was the commitment '{f['text']}' kept?", f"{f['status']} (resolved {F(resolved)}).", [d, rd], [f["commitment"], f.get("decision")], 2,
                as_of=asof([d["date"], rd["date"]], 0, 5))
    by_cp = defaultdict(list)
    for d, f in kind["commitment"]:
        by_cp[f["counterparty"]].append((d, f))
    for cp, lst in by_cp.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda x: x[0]["date"])
        a1 = lst[-1][0]["date"] + 3 * DAY
        if a1 >= hold:
            continue
        opn = [f for d, f in lst if not f.get("resolved") or pd.Timestamp(f["resolved"]) > a1]
        add("commitment_tracking", f"As of {F(a1)}, which documented commitments by {N.sup.get(cp, N.plant.get(cp, cp))} were still open?",
            ("; ".join(f"{f['commitment']} due {f['due']}" for f in opn) or "none"), [d for d, f in lst], [f["commitment"] for d, f in lst], len(lst), as_of=F(a1))
    # ---------------- consequence evaluation
    memo = {f["decision"]: (d, f) for d, f in kind["decision"]}
    for d, f in kind["outcome"]:
        if f["decision"] in memo:
            dm, fm = memo[f["decision"]]
            add("consequence_evaluation", f"How did decision {f['decision']} ({f['type'].replace('_', ' ')}, decided {f['decided']}) turn out against its estimate?",
                f"Expected {f['expected_days']:g} stockout days / ${f['expected_cost']:,.0f}; actual {f['actual_days']:g} days / ${f['actual_cost']:,.0f} - {f['outcome']}. Lesson: {f['lesson']}",
                [dm, d], [f["decision"], f["event"]], 2)
    # ---------------- precedent / analogy
    dec_list = sorted([(d, f) for d, f in kind["decision"]], key=lambda x: x[0]["date"])
    out_by_dec = {f["decision"]: (d, f) for d, f in kind["outcome"]}
    for i, (d2, f2) in enumerate(dec_list):
        key = f2["entities"].get("rm_id") or f2["entities"].get("supplier_id") or f2["entities"].get("warehouse_id")
        if not key:
            continue
        prev = [(d1, f1) for d1, f1 in dec_list[:i] if key in f1["entities"].values() and f1["decision"] in out_by_dec and out_by_dec[f1["decision"]][0]["date"] < d2["date"]]
        if not prev:
            continue
        d1, f1 = prev[-1]
        do, fo = out_by_dec[f1["decision"]]
        add("precedent_analogy", f"On {F(d2['date'] - DAY)} we face another {f2['type'].replace('_', ' ')} situation involving {key}. What did we do last time and how did it work out?",
            f"{f1['decision']} on {f1['decided']}: chose {f1['chosen'].replace('_', ' ')}; outcome {fo['outcome']} ({fo['actual_days']:g} stockout days, ${fo['actual_cost']:,.0f}). Lesson: {fo['lesson']}",
            [d1, do], [f1["decision"], f1["event"]], 2, as_of=F(d2["date"] - DAY))
    # ---------------- contradiction / supersession
    by_id = {d["doc_id"]: d for d in docs}
    for d in mem:
        for sid in d["supersedes"]:
            old = by_id.get(sid)
            if old is None or old["split"] != "memory":
                continue
            fk = d["facts"][0] if d["facts"] else {}
            k = fk.get("kind")
            if k == "slip":
                q, a = f"What is the current expected delivery date for {fk['po']}?", f"{fk['new']} (the earlier {fk['old']} promise was superseded on {F(d['date'])})."
            elif k == "eta_update":
                q, a = f"What is the latest ETA for {fk['po']}?", f"{fk['new']} after the expedite (supersedes {fk['old']})."
            elif k == "root_cause_final":
                q, a = f"What is the root cause on record for the {fk['po']} delay?", f"{fk['cause'].replace('_', ' ')}; the initial '{fk['wrong'].replace('_', ' ')}' read was corrected."
            elif k == "qbr":
                q, a = f"What is {fk['supplier']}'s most recent quarterly OTIF on record?", f"{fk['otif']:.0%} for {fk['quarter']}."
            elif k == "neg_close":
                q, a = f"What terms currently apply from negotiation {fk['negotiation']}?", f"{fk['terms']}."
            elif k == "expedite_receipt":
                q, a = f"Did expedited {fk['po']} arrive by the committed date?", f"No - it arrived {fk['late_days']} days late despite the expedite commitment."
            elif k in ("resourced", "transfer"):
                q, a = "How is the shortage covered now?", json.dumps({kk: vv for kk, vv in fk.items() if kk != "kind"})
            else:
                continue
            add("contradiction_supersession", q, a, [old, d], d["source_record_ids"][:3], 2, as_of=asof([d["date"]], 0, 10))
    # ---------------- multi-hop (3+)
    for st in by_story:
        d1, f1 = story_doc(st, "slip")
        d2, f2 = story_doc(st, "blocked")
        whs = [(d, f) for d in by_story[st] for f in d["facts"] if f["kind"] == "fg_late_receipt"]
        if d1 and d2 and whs:
            dm, fm = story_doc(st, "decision")
            gd = [d1, d2] + [d for d, f in whs] + ([dm] if dm else [])
            ans = "; ".join(f"{f['fg_line']} at {f['warehouse']} received {f['received']} ({f['late_days']} days late)" for d, f in whs)
            if dm:
                ans += f". Response: {fm['chosen'].replace('_', ' ')} ({fm['decision']})."
            add("multi_hop", f"{N.sup[f1['supplier']]} slipped {f1['po']} on {F(d1['date'])}. Which finished-goods receipts did that ultimately delay, at which DCs, and how did we respond?",
                ans, gd, [f1["po"], f2["run"]] + [f["fg_line"] for d, f in whs] + ([fm["decision"]] if dm else []), len(gd))

    # ---------------- sample to target mix, drop leakage
    Qd = pd.DataFrame(Q)
    maxdate = {d["doc_id"]: d["date"] for d in docs}
    Qd = Qd[[all(maxdate[g] <= pd.Timestamp(a) for g in gs) for gs, a in zip(Qd.gold_doc_ids, Qd.as_of_date)]]
    target = cfg["scale"]["eval_questions"]
    out = []
    for t, share in cfg["eval"]["type_mix"].items():
        g = Qd[Qd.type == t]
        k = min(len(g), int(round(target * share)))
        out.append(g.sample(n=k, random_state=int(rng.integers(1 << 30))))
    Qo = pd.concat(out)
    if len(Qo) < target:
        rest = Qd.drop(Qo.index)
        Qo = pd.concat([Qo, rest.sample(n=min(len(rest), target - len(Qo)), random_state=1)])
    Qo = Qo.sort_values(["as_of_date", "type"]).reset_index(drop=True)
    Qo.insert(0, "question_id", [f"Q{i + 1:04d}" for i in range(len(Qo))])
    with open(ctx.out / "eval_questions.jsonl", "w") as fh:
        for r in Qo.to_dict("records"):
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"eval questions {len(Qo)} (pool {len(Qd)}): {Qo.type.value_counts().to_dict()}; hop>=3: {int((Qo.hop_count >= 3).sum())}")
    holdout_scenarios(ctx, M, N)


# =============================================================================
def holdout_scenarios(ctx: Ctx, M: dict, N):
    cfg = ctx.cfg
    rng = ctx.rng("holdout")
    hold = pd.Timestamp(ctx.calib["holdout_start"])
    S, R = ctx.load_internal("structure"), ctx.load_internal("rm_ops")
    plan = S["pattern_plan"]
    prod = S["prod"].set_index("product_id")
    cat = R["catalog"]
    snap = read_typed(ctx, "rm_inventory_snapshots_weekly").set_index(["rm_id", "plant_id", "snapshot_date"])
    cm = read_typed(ctx, "commitments")
    runs = read_typed(ctx, "production_runs").set_index("production_run_id")
    rms = S["rms"].set_index("rm_id")
    xy = cfg["plants"]["region_xy"]
    preg = ctx.table("plants").set_index("plant_id").region.to_dict()
    emap, dmap = M["emap"], M["dmap"]
    dec_by_ev = defaultdict(list)
    for d in M["decisions"]:
        dec_by_ev[d["event_key"]].append(d)
    cas_ev = [e for e in M["events"] if e["meta"].get("kind") == "cascade"]
    past = [e for e in cas_ev if e["end_date"] < hold]
    q4 = [e for e in cas_ev if e["detected_at"] >= hold]

    def sn(rm, pl, d):
        d = pd.Timestamp(d)
        sun = d - pd.Timedelta(days=(d.dayofweek + 1) % 7)
        try:
            r = snap.loc[(rm, pl, sun)]
            return float(r.on_hand_units), float(r.safety_stock_units)
        except KeyError:
            return float("nan"), float("nan")

    scen = []
    for e in q4:
        c = e["meta"]["cascade"]
        rm, pl = c["rm_id"], c["plant_id"]
        sup = c["slipped"][0]["supplier_id"]
        day0 = c["t_revised_date"]
        P = c["P_date"]
        eta = c["slip_to_date"]
        victims = e["meta"]["victims"]
        vr = runs.loc[victims]
        value = float((vr.planned_qty * vr.product_id.map(prod.unit_price)).sum())
        daily = cq.block_cost_per_day(value, cfg)
        lines_value = float(sum(s["qty"] * s["price"] for s in c["slipped"]))
        lead = int(cat[(cat.rm_id == rm) & (cat.supplier_id == sup)].lead_time_days.iloc[0])
        congested = day0.month == 2 and N.country[sup] == plan.get("P02", {}).get("country_code")
        p01 = plan.get("P01", {}).get("supplier_id") == sup and eta.month in (11, 12)
        acts = []
        d_acc = max(0, (eta - P).days)
        acts.append({"action": "accept_delay", "feasible": True, "arrival": F(eta), "stockout_days": d_acc, "cost": round(d_acc * daily, 0),
                     "risk": "high" if p01 else "low", "note": ("supplier historically slips a further 7-14 days in Nov-Dec" if p01 else "supplier date")})
        e_arr = day0 + cq.rm_expedite_days(lead, congested) * DAY
        d_e = max(0, (e_arr - P).days)
        acts.append({"action": "expedite", "feasible": True, "arrival": F(e_arr), "stockout_days": d_e, "cost": round(cq.rm_expedite_premium_pct(lead, congested) * lines_value + d_e * daily, 0),
                     "risk": "medium", "note": f"air premium {cq.rm_expedite_premium_pct(lead, congested):.0%} of lot value"})
        alt = cat[(cat.rm_id == rm) & (cat.supplier_id != sup) & (cat.qualified_flag == 1) & (cat.price_valid_from <= day0) & (cat.price_valid_to >= day0)]
        if len(alt):
            o = alt.sort_values("allocation_pct", ascending=False).iloc[0]
            s_arr = day0 + cq.spot_lead_days(int(o.lead_time_days)) * DAY
            d_s = max(0, (s_arr - P).days)
            acts.append({"action": "switch_supplier", "feasible": True, "arrival": F(s_arr), "stockout_days": d_s,
                         "cost": round(cfg["rm_ops"]["spot_price_premium"] * c["req"] * float(o.unit_price) + d_s * daily, 0), "risk": "medium", "note": f"spot lot from {o.supplier_id}"})
        else:
            acts.append({"action": "switch_supplier", "feasible": False, "note": "no other qualified source in the catalog at day 0"})
        best_tr = None
        for k in R["ss"]:
            if k[0] != rm or k[1] == pl:
                continue
            oh, ssv = sn(rm, k[1], day0)
            if not math.isnan(oh) and oh - c["req"] >= 0:
                td = cq.transfer_days(math.dist(xy.get(preg[k[1]], [0, 0]), xy.get(preg[pl], [0, 0])))
                drains = oh - c["req"] < ssv
                risk = "high" if (k[1] == plan.get("P08", {}).get("plant_id") or drains) else "medium"
                cand = {"action": "reallocate_stock", "feasible": True, "donor_plant": k[1], "arrival": F(day0 + td * DAY), "stockout_days": max(0, (day0 + td * DAY - P).days),
                        "cost": 0.0, "risk": risk, "note": f"donor on hand {oh:,.1f} vs SS {ssv:,.1f}" + ("; transfers from this plant have repeatedly left it short" if k[1] == plan.get("P08", {}).get("plant_id") else "")}
                cand["cost"] = round(cfg["rm_ops"]["transfer_cost_per_unit_value"] * lines_value + cand["stockout_days"] * daily, 0)
                if best_tr is None or (cand["risk"], cand["cost"]) < (best_tr["risk"], best_tr["cost"]):
                    best_tr = cand
        acts.append(best_tr or {"action": "reallocate_stock", "feasible": False, "note": "no other plant holds enough of this material"})
        sg = rms.at[rm, "substitute_group_id"]
        subs = [x for x in rms.index[rms.substitute_group_id == sg] if x != rm] if isinstance(sg, str) else []
        sub_ok = [x for x in subs if not math.isnan(sn(x, pl, day0)[0]) and sn(x, pl, day0)[0] >= c["req"]]
        if sub_ok:
            x = sub_ok[0]
            d_q = max(0, (day0 + 6 * DAY - P).days)
            risky = plan.get("P07", {}).get("new_rm") == x
            acts.append({"action": "substitute_rm", "feasible": True, "substitute": x, "arrival": F(day0 + 6 * DAY), "stockout_days": d_q, "cost": round(3000 + d_q * daily, 0),
                         "risk": "high" if risky else "medium", "note": "QA approval ~6 days" + ("; this substitute has a history of incoming rejects" if risky else "")})
        else:
            acts.append({"action": "substitute_rm", "feasible": False, "note": "no qualified substitute in stock at the plant"})
        opn = cm[(cm.made_at <= day0) & (cm.resolved_at.isna() | (cm.resolved_at > day0)) & cm.counterparty_id.isin([sup, pl])]
        vol_c = opn[opn.commitment_text.str.contains("We order at least", na=False)]
        acts.append({"action": "cancel_po", "feasible": True, "arrival": acts[2].get("arrival"), "stockout_days": acts[2].get("stockout_days", d_acc),
                     "cost": round((acts[2].get("cost", d_acc * daily) or 0) + 0.02 * lines_value + (0.06 * lines_value * 3 if len(vol_c) else 0), 0),
                     "risk": "high" if len(vol_c) else "medium", "note": ("cancelling would breach open volume commitment " + ", ".join(vol_c.commitment_id)) if len(vol_c) else "cancel and re-buy elsewhere"})
        feas = [a for a in acts if a["feasible"]]
        for a in feas:
            a["score"] = round(cq.total_score(a["cost"], 0, 0, a["risk"]), 0)
            a["service_impact_units"] = int(vr.planned_qty.sum()) if a["stockout_days"] > 0 else 0
        best = min(feas, key=lambda a: a["score"])
        # precedents
        prec = [p for p in past if p["meta"]["rm_id"] == rm] + [p for p in past if p["origin_entity_id"] == sup and p["meta"]["rm_id"] != rm]
        prec = sorted(prec, key=lambda p: p["end_date"], reverse=True)[:3]
        decided_prec = [p for p in sorted(past, key=lambda p: p["end_date"], reverse=True) if p["key"] in dec_by_ev and (p["meta"]["rm_id"] == rm or p["origin_entity_id"] == sup)]
        trap = None
        if decided_prec:
            p = decided_prec[0]
            pd_ = dec_by_ev[p["key"]][0]
            if pd_["outcome"] == "success" and pd_["type"] != best["action"]:
                trap = {"most_similar_event_id": emap[p["key"]], "its_decision_id": dmap[pd_["key"]], "its_action": pd_["type"], "its_outcome": pd_["outcome"],
                        "why_misleading": f"{pd_['type'].replace('_', ' ')} worked on {F(pd_['decided_at'])} but now scores worse than {best['action'].replace('_', ' ')} "
                                          f"({'supplier Q4 slip pattern' if p01 else 'different stock / sourcing position at day 0'})."}
        oh, ssv = sn(rm, pl, day0)
        report = (f"{F(day0)} - {N.sup[sup]} ({sup}) just moved {c['slipped'][0]['rm_purchase_order_id']} ({N.rm[rm]}, {rm}) for {N.plant[pl]} ({pl}) to {F(eta)}. "
                  f"We have about {oh:,.1f} {N.uom[rm]} on hand vs safety stock {ssv:,.1f}; run {c['production_run_id']} is planned {F(P)} and needs {c['req']:,.1f} {N.uom[rm]}. "
                  f"{len(victims)} run(s) and ~{int(vr.planned_qty.sum()):,} FG units for {', '.join(sorted(e['meta']['warehouses']))} depend on it. What should we do?")
        scen.append({"event_id": emap[e["key"]], "day0": F(day0), "day0_report": report, "entities": {"supplier_id": sup, "rm_id": rm, "plant_id": pl, "runs": victims},
                     "gold_precedent_event_ids": [emap[p["key"]] for p in prec], "open_commitments": opn[["commitment_id", "commitment_text", "due_date"]].assign(due_date=opn.due_date.dt.strftime("%Y-%m-%d")).to_dict("records"),
                     "candidate_actions": acts, "gold_best_action": best["action"],
                     "gold_reasoning": f"{best['action'].replace('_', ' ')} has the lowest risk-adjusted cost ({best['score']:,.0f}) with {best['stockout_days']} stockout days; "
                                       + "; ".join(f"{a['action']} {a['score']:,.0f}" for a in feas if a is not best),
                     "trap": trap, "what_actually_happened": {"decision_ids": [dmap[d["key"]] for d in dec_by_ev.get(e["key"], [])], "blocked_days": e["meta"]["blocked_days"]}})
    traps = [s for s in scen if s["trap"]]
    others = [s for s in scen if not s["trap"]]
    rng.shuffle(traps)
    rng.shuffle(others)
    n = cfg["scale"]["holdout_scenarios"]
    chosen = traps[: max(cfg["eval"]["min_traps"], min(len(traps), n // 3))]
    chosen += others[: n - len(chosen)]
    chosen = sorted(chosen, key=lambda s: s["day0"])
    for i, s in enumerate(chosen):
        s["scenario_id"] = f"HS{i + 1:02d}"
    dump_json({"note": "Q4 disruptions whose narrative docs are split=holdout (not ingested). Consequences use consequences.py over the data as of day 0.",
               "scenarios": chosen}, ctx.out / "holdout_scenarios.json")
    log(f"holdout scenarios {len(chosen)} (traps {sum(1 for s in chosen if s['trap'])}); best actions {pd.Series([s['gold_best_action'] for s in chosen]).value_counts().to_dict()}")
