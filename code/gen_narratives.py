"""Stage 4a - narrative memory corpus (memory_corpus.jsonl).

Every document is rendered from structured facts (no free invention): dates,
quantities and IDs come from the tables, so no doc contradicts the data except
docs that are explicitly superseded by a later doc (`supersedes`).
Stories are chains of 3+ docs written by different roles days / weeks apart.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from narrative_grammar import CLOSE, OPEN, render, variant_counts
from sc_common import Ctx, banner, business_ts, dump_json, log, read_typed

DAY = pd.Timedelta(days=1)
F = lambda d: pd.Timestamp(d).strftime("%Y-%m-%d")  # noqa: E731
HOUR_BIAS = {"shift_handover": 0.9, "po_exception_note": 0.3, "escalation_email": 0.5, "decision_memo": 0.6, "post_mortem": 0.5, "supplier_call_summary": 0.4,
             "supplier_qbr_notes": 0.4, "commitment_confirmation": 0.55, "warehouse_ops_note": 0.2, "forecast_review_minutes": 0.35, "state_update": 0.45}


# =============================================================================
class Names2:
    def __init__(self, ctx: Ctx):
        from gen_events import Names
        self.N = Names(ctx)
        self.brand = ctx.table("products_ext").set_index("product_id").brand_family.to_dict()


class Mention:
    """First mention = name + ID; later mentions = realistic aliases."""

    def __init__(self, nm: Names2, rng):
        self.N, self.rng, self.seen, self.ids = nm.N, rng, set(), []
        self.brand = nm.brand

    def _pick(self, opts):
        return opts[int(self.rng.integers(len(opts)))]

    def sup(self, s):
        self.ids.append(s)
        if s not in self.seen:
            self.seen.add(s)
            return f"{self.N.sup[s]} ({s})"
        n = s[3:]
        return self._pick([self.N.sup[s], s, f"Sup {int(n)}", f"Supplier {int(n)}"])

    def plant(self, p):
        self.ids.append(p)
        nm = self.N.plant[p]
        if p not in self.seen:
            self.seen.add(p)
            return f"{nm} ({p})"
        return self._pick([nm.replace(" Plant", ""), p, f"the {nm.replace(' Plant', '')} site", nm])

    def rm(self, r):
        self.ids.append(r)
        nm = self.N.rm[r]
        if r not in self.seen:
            self.seen.add(r)
            return f"{nm} ({r})"
        return self._pick([nm, r, " ".join(nm.split()[:2])])

    def wh(self, w):
        self.ids.append(w)
        nm = self.N.wh[w]
        if w not in self.seen:
            self.seen.add(w)
            return f"{nm} ({w})"
        return self._pick([w, nm])

    def sku(self, p):
        self.ids.append(p)
        if p not in self.seen:
            self.seen.add(p)
            return f"{self.brand.get(p, '')} SKU {self.N.sku[p]} ({p})".strip()
        return self._pick([self.N.sku[p], p, f"the {self.brand.get(p, '')} SKU"])

    def rec(self, x):
        self.ids.append(x)
        return x


def qty(x, uom):
    x = float(x)
    return f"{x:,.0f} {uom}" if x >= 100 else f"{x:,.1f} {uom}" if x >= 1 else f"{x:.3f} {uom}"


def money(x):
    x = 0.0 if x is None or abs(x) < 0.5 else float(x)
    return f"${x:,.0f}"


# =============================================================================
class Corpus:
    def __init__(self, ctx: Ctx, nm: Names2):
        self.ctx, self.nm, self.cfg = ctx, nm, ctx.cfg
        self.docs: list[dict] = []
        self.hold = pd.Timestamp(ctx.calib["holdout_start"])
        self.win_end = pd.Timestamp(ctx.calib["window_end"])

    def rng(self, *k):
        return self.ctx.rng("doc", *k)

    def add(self, dtype, date, body: list[str], slots: dict, m: Mention, context: str, src_ids: list, facts: list | None = None,
            noise=False, supersedes=None, story=None, key=None):
        date = pd.Timestamp(date).normalize()
        if date > self.win_end:
            return None
        key = key or f"{dtype}:{len(self.docs)}"
        r = self.rng(key)
        slots = {"date": F(date), "next": F(date + int(r.integers(3, 15)) * DAY), **slots}
        opener = render(OPEN[dtype][int(r.integers(len(OPEN[dtype])))], r, slots)
        closer = render(CLOSE[dtype][int(r.integers(len(CLOSE[dtype])))], r, slots)
        parts = [opener] + [render(b, r, slots) for b in body if b] + ([closer] if closer else [])
        text = "\n".join(p for p in parts if p.strip())
        wc = len(text.split())
        if wc < self.cfg["narratives"]["min_words"]:
            filler = ["[Figures are from|Numbers per] [this morning's|the latest|today's] [MRP run|planning snapshot] ({d}).",
                      "[I have|We have] [updated|refreshed] the [exception tracker|planning board] with the above.",
                      "[Procurement and planning are both copied.|The plant team has seen this.|Customer service has been briefed.]",
                      "[Will update this thread if the dates move.|Any change will be logged against the same records.]",
                      "[Questions to|Contact:] {role}.",
                      "[Nothing else changed in the plan.|Rest of the plan is unchanged.|No knock-on changes expected.]"]
            order = r.permutation(len(filler))
            for i in order:
                if len(text.split()) >= self.cfg["narratives"]["min_words"]:
                    break
                text += "\n" + render(filler[i], r, {"role": slots.get("role", "Supply Planner"), "d": F(date)})
        words = text.split()
        if len(words) > self.cfg["narratives"]["max_words"]:
            lines = text.split("\n")
            while len(" ".join(lines).split()) > self.cfg["narratives"]["max_words"] and len(lines) > 3:
                lines.pop(-2)
            text = "\n".join(lines)
        d = {"doc_id": None, "key": key, "timestamp": business_ts(date, r, self.ctx.cfg, HOUR_BIAS.get(dtype)), "date": date, "context": context,
             "content": text, "doc_type": dtype, "entity_ids": list(dict.fromkeys(m.ids)), "source_record_ids": list(dict.fromkeys(src_ids)),
             "split": "holdout" if date >= self.hold else "memory", "is_noise": noise, "supersedes": [s for s in (supersedes or []) if s], "facts": facts or [],
             "story": story}
        self.docs.append(d)
        return key


# =============================================================================
# stories
# =============================================================================
class Stories:
    def __init__(self, ctx, corpus: Corpus, nm: Names2, M: dict, T: dict):
        self.ctx, self.C, self.nm, self.N, self.M, self.T = ctx, corpus, nm, nm.N, M, T
        self.t0 = pd.Timestamp(ctx.load_internal("rm_ops")["t0"])
        self.ev = {e["key"]: e for e in M["events"]}
        self.dec_by_event = defaultdict(list)
        for d in M["decisions"]:
            self.dec_by_event[d["event_key"]].append(d)
        self.cm_by_dec = defaultdict(list)
        for c in M["commits"]:
            if c.get("decision_key"):
                self.cm_by_dec[c["decision_key"]].append(c)
        self.cm_by_neg = defaultdict(list)
        for c in M["commits"]:
            if c.get("neg_key"):
                self.cm_by_neg[c["neg_key"]].append(c)
        rev = T["rm_po_revisions"]
        self.rev_by_po = {k: g.sort_values(["revised_at", "revision_id"]) for k, g in rev.groupby("rm_po_id")}
        self.rpo = T["rm_purchase_orders"].set_index("rm_purchase_order_id")
        self.rpol = T["rm_purchase_order_lines"]
        self.runs = T["production_runs"].set_index("production_run_id")
        src = ctx.src
        self.pol = src["purchase_order_lines"].set_index("purchase_order_line_id")
        self.po = src["purchase_orders"].set_index("purchase_order_id")
        self.stock_cache = {}
        self.snap = T["rm_inventory_snapshots_weekly"].set_index(["rm_id", "plant_id", "snapshot_date"])
        self.mv = T["rm_inventory_movements"]
        self.ob = T["rm_inventory_opening_balances"].set_index(["rm_id", "plant_id"]).opening_units

    def eid(self, key):
        return self.M["emap"][key]

    def did(self, key):
        return self.M["dmap"][key]

    def stock(self, rm, pl):
        k = (rm, pl)
        if k not in self.stock_cache:
            m = self.mv[(self.mv.rm_id == rm) & (self.mv.plant_id == pl)]
            n = (self.C.win_end - self.t0).days + 1
            arr = np.zeros(n)
            np.add.at(arr, (m.movement_at - self.t0).dt.days.values, m.quantity_change.values)
            self.stock_cache[k] = self.ob[k] + np.cumsum(arr)
        return self.stock_cache[k]

    def on_hand(self, rm, pl, d):
        return float(self.stock(rm, pl)[(pd.Timestamp(d) - self.t0).days])

    def ss(self, rm, pl, d):
        d = pd.Timestamp(d)
        sun = d + pd.Timedelta(days=6 - d.dayofweek)
        return float(self.snap.safety_stock_units.get((rm, pl, sun), np.nan))

    def contract_terms(self, sup, d):
        ct = self.T["contracts"]
        g = ct[(ct.supplier_id == sup) & (ct.valid_from <= d) & (ct.valid_to >= d)]
        return f"{g.contract_id.iloc[0]}, {g.penalty_clause.iloc[0]}" if len(g) else ""

    def why(self, d):
        ch = next((o for o in d["options"] if d["chosen"].startswith(o["option"])), d["options"][0])
        alts = [o for o in d["options"] if o is not ch]
        cheaper = [o for o in alts if o["est_cost"] < ch["est_cost"]]
        faster = [o for o in alts if o["est_stockout_days"] < ch["est_stockout_days"]]
        t = d["type"]
        if not cheaper and not faster:
            base = "it is both the cheapest and the fastest option we have"
        elif not cheaper:
            base = f"it is the lowest expected cost (vs {money(min(o['est_cost'] for o in alts))} for the next option)"
        elif faster and not [o for o in faster if o["risk"] != "high"]:
            base = "the faster alternatives carry high execution risk"
        else:
            base = f"it balances cost and risk ({ch['risk']} risk) better than {', '.join(o['option'].replace('_', ' ') for o in cheaper[:2])}"
        extra = {"expedite": "; air lands sooner than the revised supplier date", "accept_delay": "; the supplier date is firm and nothing faster beats the delay cost",
                 "switch_supplier": "; a qualified alternative source can cover the gap", "reallocate_stock": "; another site has cover to spare",
                 "build_safety_stock": "; repeated near-misses show the buffer is too thin", "substitute_rm": "; a qualified substitute removes the dependency",
                 "renegotiate": "; we have leverage through volume", "cancel_po": "; stock is well above reorder point", "reduce_allocation": "; the incumbent keeps missing dates"}.get(t, "")
        return base + extra

    def happened(self, d):
        mt = d["meta"]
        if d["key"].startswith("dec:cas"):
            c = mt["cascade"]
            return (f"material landed {F(c['X_date'])}, {int((c['X_date'] - c['P_date']).days)} days after the planned start; {c['production_run_id']} started {F(c['A_date'])}"
                    + (f"; {len(c.get('co_victims', []))} other run(s) also waited." if c.get("co_victims") else "."))
        if mt.get("fg_kind") == "expedite":
            late = mt["late"]
            return f"{mt['line']['purchase_order_id']} arrived {'on time' if late == 0 else (str(-late) + ' days early' if late < 0 else str(late) + ' days late')}; premium invoiced {money(d['actual_cost'])}."
        if mt.get("fg_kind") == "transfer":
            return f"{mt['qty']} units moved {mt['src']}->{mt['dst']}; source {'dipped to or below reorder point within 4 weeks' if mt['src_low'] else 'stayed above reorder point'}."
        if mt.get("fg_kind") == "cancel":
            return f"{mt['po']} cancelled; stock {'dipped below reorder point afterwards' if d['outcome'] != 'success' else 'stayed above reorder point for 60 days'}."
        if mt.get("fg_kind") == "accept":
            return f"shortfall of {mt['short_units']} units accepted; {'stock ran low later' if d['outcome'] != 'success' else 'no stock-out followed'}."
        if d["key"].startswith("dec:ssb"):
            return f"{len(mt['after'])} shortage(s) on this material/site after the buffer increase."
        if d["key"].startswith("dec:alc"):
            return f"late share went from {mt['late_before']:.0%} to {mt['late_after']:.0%} after the change."
        if d["key"].startswith("dec:sub"):
            ch = mt["change"]
            return (f"{self.N.rm[ch['new_rm']]} ({ch['new_rm']}) replaced {self.N.rm[ch['old_rm']]} ({ch['old_rm']}) from {F(ch['date'])}; "
                    + (f"incoming rejects cost {money(mt['scrap_cost'])}." if mt["scrap_cost"] >= 0.5 else "no incoming rejects so far."))
        if d["key"].startswith("dec:neg"):
            return "terms took effect as agreed." if d["type"] == "renegotiate" else ""
        return ""

    # ------------------------------------------------------------------ cascade story
    def cascade(self, ekey, full=True):
        e = self.ev[ekey]
        c = e["meta"]["cascade"]
        N, C = self.N, self.C
        rm, pl = c["rm_id"], c["plant_id"]
        uom = N.uom[rm]
        s0 = c["slipped"][0]
        sup = s0["supplier_id"]
        po_id = s0["rm_purchase_order_id"]
        h = self.rpo.loc[po_id]
        revs = self.rev_by_po.get(po_id)
        slip_rev = revs[revs.reason_code != "expedite_air_freight"].iloc[-1] if revs is not None and len(revs) else None
        dec = self.dec_by_event.get(ekey, [None])[0]
        story = ekey
        keys = {}
        E = self.eid(ekey)
        run = self.runs.loc[c["production_run_id"]]
        # 1. order acknowledgement (superseded by the exception note)
        m = Mention(self.nm, C.rng(ekey, "ack"))
        keys["ack"] = C.add("commitment_confirmation", h.ordered_at + DAY, [
            "{sup_} acknowledged {po} for {q} of {rm_} to {plant_}, [promised|confirmed|committed] for {prom}.",
            "[Lead time|Quoted lead time] {lead} days; [price per contract|contract price applies]."],
            {"who": m.sup(sup), "what": f"deliver {po_id} to {N.plant[pl]} by {F(h.promised_at)}", "what_verb": f"deliver {po_id} by {F(h.promised_at)}",
             "sup_": N.sup[sup], "po": m.rec(po_id), "q": qty(s0["qty"], uom), "rm_": m.rm(rm), "plant_": m.plant(pl), "prom": F(h.promised_at),
             "lead": int((h.promised_at - h.ordered_at).days), "due": F(h.promised_at), "penalty": "late-delivery credit per contract"},
            m, f"Order acknowledgement for {po_id} ({rm}) to {pl}", [po_id, s0["rm_purchase_order_line_id"]],
            [{"kind": "promise", "po": po_id, "supplier": sup, "rm": rm, "plant": pl, "promised": F(h.promised_at), "qty": s0["qty"]}], story=story, key=f"{ekey}:ack")
        if slip_rev is None:
            return keys
        # 2. exception note at the revision date
        m = Mention(self.nm, C.rng(ekey, "exc"))
        oh_t1, ss_t1 = self.on_hand(rm, pl, slip_rev.revised_at), self.ss(rm, pl, slip_rev.revised_at)
        ctr = self.contract_terms(sup, slip_rev.revised_at)
        body = ["[Reason given|Their explanation|Cause per supplier]: {reason}.",
                "{rpo_} was placed on {ordered} for {q} ([line|item] {rpol}), originally promised {prom} ({lead}-day lead time).",
                "[It feeds|This lot covers|Needed for] {plant}; next draw is {run} ({sku}) on {P}, which needs {req}.",
                "[On hand today|Current stock] at {plant_s}: {oh} vs safety stock {ss}.",
                "[Contract terms|Per contract]: {ctr}." if ctr else ""]
        keys["exc"] = C.add("po_exception_note", slip_rev.revised_at, body,
                            {"sup": m.sup(sup), "rpo": m.rec(po_id), "rpo_": po_id, "rm": m.rm(rm), "plant": m.plant(pl), "plant_s": pl, "old": F(slip_rev.old_expected_at),
                             "new": F(slip_rev.new_expected_at), "reason": slip_rev.reason_code.replace("_", " "), "q": qty(s0["qty"], uom), "rpol": s0["rm_purchase_order_line_id"],
                             "ordered": F(h.ordered_at), "prom": F(h.promised_at), "lead": int((h.promised_at - h.ordered_at).days), "run": m.rec(c["production_run_id"]),
                             "sku": m.sku(run.product_id), "P": F(c["P_date"]), "req": qty(c["req"], uom), "oh": qty(oh_t1, uom), "ss": qty(ss_t1, uom), "ctr": ctr},
                            m, f"Buyer exception note: {po_id} slip", [po_id, slip_rev.revision_id, E, s0["rm_purchase_order_line_id"]],
                            [{"kind": "slip", "po": po_id, "supplier": sup, "rm": rm, "plant": pl, "old": F(slip_rev.old_expected_at), "new": F(slip_rev.new_expected_at),
                              "reason": slip_rev.reason_code, "revision_id": slip_rev.revision_id, "event": E, "ordered": F(h.ordered_at), "promised": F(h.promised_at),
                              "on_hand": round(oh_t1, 3), "run": c["production_run_id"], "P": F(c["P_date"])}], supersedes=[keys["ack"]], story=story, key=f"{ekey}:exc")
        # initial (wrong) assessment -> later corrected
        if e["meta"].get("initial"):
            e0 = self.ev[e["meta"]["initial"]]
            m = Mention(self.nm, C.rng(ekey, "ini"))
            keys["ini"] = C.add("escalation_email", slip_rev.revised_at, [
                "[Early read|First read|Initial assessment]: {sup} [is dealing with|reports] {wrong} - {rpo} ({rm}) will be late for {plant}.",
                "[Treat as|Logging as] {wrong} until [we hear more|confirmed]."],
                {"topic": f"{po_id} late - {e0['root_cause_code'].replace('_', ' ')}", "to": "Materials Manager", "frm": "Supply Planner", "sup": m.sup(sup), "rpo": m.rec(po_id),
                 "rm": m.rm(rm), "plant": m.plant(pl), "wrong": e0["root_cause_code"].replace("_", " ")},
                m, f"Initial root-cause read on {po_id}", [po_id, self.eid(e0["key"])],
                [{"kind": "root_cause_initial", "po": po_id, "cause": e0["root_cause_code"], "event": self.eid(e0["key"])}], story=story, key=f"{ekey}:ini")
            m = Mention(self.nm, C.rng(ekey, "cor"))
            keys["cor"] = C.add("state_update", e0["meta"]["corrected_on"], [
                "{sup} confirmed the delay on {rpo} is [due to|caused by] {right}, not {wrong} as first reported.", "[Root cause|Cause] updated [in the event log|on the tracker] ({evt})."],
                {"what_changed": f"root cause for {po_id} corrected", "sup": m.sup(sup), "rpo": m.rec(po_id), "right": e["root_cause_code"].replace("_", " "),
                 "wrong": e0["root_cause_code"].replace("_", " "), "evt": E},
                m, f"Root cause correction for {po_id}", [po_id, E, self.eid(e0["key"])],
                [{"kind": "root_cause_final", "po": po_id, "cause": e["root_cause_code"], "wrong": e0["root_cause_code"], "event": E}], supersedes=[keys["ini"]], story=story, key=f"{ekey}:cor")
        # 3. handover when stock fell below safety stock
        b = c["t_below_ss_date"]
        oh, ssv = self.on_hand(rm, pl, b), self.ss(rm, pl, b)
        m = Mention(self.nm, C.rng(ekey, "ho"))
        rr0 = self.pol.loc[run.purchase_order_line_id]
        wh0 = self.po.loc[rr0.purchase_order_id].warehouse_id
        dcov = oh / max(1e-9, c["req"]) if c["req"] else 0
        keys["ho"] = C.add("shift_handover", b, [
            "{rm} [dropped below|is now under|went under] safety stock: {oh} on hand vs SS {ss}.",
            "[Next run needing it|At risk]: {run} ({sku}, {pq:,} units for {wh}) planned {P}; it needs {req}, so we [cover|have] about {cov:.0%} of it.",
            "[Inbound is|Waiting on] {rpo} from {sup}, [now due|revised to] {new} (was {old}).",
            "[Hold non-critical trials on this material.|Do not release extra trials that use it.|Keep the remaining stock for scheduled runs only.]"],
            {"plant": m.plant(pl), "plant_short": pl, "rm": m.rm(rm), "oh": qty(oh, uom), "ss": qty(ssv, uom), "run": m.rec(c["production_run_id"]), "sku": m.sku(run.product_id),
             "pq": int(run.planned_qty), "wh": m.wh(wh0), "P": F(c["P_date"]), "req": qty(c["req"], uom), "cov": min(dcov, 0.99), "rpo": m.rec(po_id), "sup": m.sup(sup),
             "new": F(slip_rev.new_expected_at), "old": F(slip_rev.old_expected_at), "role": "Plant Scheduler"},
            m, f"{pl} handover: {rm} below safety stock", [c["production_run_id"], po_id, f"{rm}|{pl}|{F(b + pd.Timedelta(days=6 - b.dayofweek))}", E],
            [{"kind": "below_ss", "rm": rm, "plant": pl, "date": F(b), "on_hand": round(oh, 3), "ss": round(ssv, 3), "run": c["production_run_id"], "event": E}], story=story, key=f"{ekey}:ho")
        # 4. escalation at planned start
        fg = []
        for rid in e["meta"]["victims"]:
            rr = self.runs.loc[rid]
            ln = self.pol.loc[rr.purchase_order_line_id]
            hh = self.po.loc[ln.purchase_order_id]
            fg.append((rid, rr.purchase_order_line_id, ln.purchase_order_id, hh.warehouse_id, hh.expected_at, hh.received_at, int(ln.quantity_ordered), rr.product_id))
        m = Mention(self.nm, C.rng(ekey, "esc"))
        lst = "; ".join(f"{m.rec(x[0])} -> {m.rec(x[1])} for {m.wh(x[3])} (due {F(x[4])}, {x[6]:,} units)" for x in fg[:4])
        keys["esc"] = C.add("escalation_email", c["P_date"], [
            "{run} could not start today at {plant}: {rm} on hand is below the {req} it needs.",
            "[Affected|Impacted] replenishment: {lst}.",
            "{sup} [ETA|delivery] is {new}; [we need to decide how to cover the gap|we need a call on how to bridge it].",
            "{extra}", "{opts}"],
            {"topic": f"{rm} stock-out at {pl}", "to": "Materials Manager", "frm": "Supply Planner", "run": m.rec(c["production_run_id"]), "plant": m.plant(pl), "rm": m.rm(rm),
             "req": qty(c["req"], uom), "lst": lst, "sup": m.sup(sup), "new": F(slip_rev.new_expected_at),
             "extra": f"{len(fg) - 1} more run(s) on the same material are also waiting." if len(fg) > 1 else "No other runs on this material in the window.",
             "opts": ("Options on the table: " + "; ".join(o["option"].replace("_", " ") + (f" ({o['note']})" if o.get("note") else "") for o in dec["options"]) + ".") if dec else ""},
            m, f"Escalation: {rm} shortage blocks {c['production_run_id']}", [E] + [x[0] for x in fg] + [x[1] for x in fg],
            [{"kind": "blocked", "rm": rm, "plant": pl, "run": c["production_run_id"], "P": F(c["P_date"]), "fg_lines": [x[1] for x in fg], "warehouses": sorted({x[3] for x in fg}),
              "supplier": sup, "po": po_id, "event": E, "victims": [x[0] for x in fg]}], story=story, key=f"{ekey}:esc")
        if full and dec:
            keys.update(self.decision_docs(dec, story, extra={"rm": rm, "plant": pl, "po": po_id, "sup": sup, "c": c, "slip_rev": slip_rev}))
        # 8. warehouse notes for late FG receipts
        for x in fg[:2] if full else fg[:1]:
            m = Mention(self.nm, C.rng(ekey, "wh", x[1]))
            late = int((x[5] - x[4]).days)
            keys[f"wh:{x[1]}"] = C.add("warehouse_ops_note", x[5], [
                "{pol} ({sku}) [received|landed|booked in] today, {late} days after the {exp} due date; {q} units.",
                "[Late because|Root cause per planning:] {plant} was short of {rm}.",
                "[Backorders released.|Allocated to open orders.|Put away and released.]"],
                {"wh": m.wh(x[3]), "pol": m.rec(x[1]), "sku": m.sku(x[7]), "late": late, "exp": F(x[4]), "q": f"{int(self.pol.loc[x[1]].quantity_received):,}", "plant": m.plant(pl), "rm": m.rm(rm)},
                m, f"{x[3]} receipt of late {x[1]}", [x[1], x[2], E],
                [{"kind": "fg_late_receipt", "fg_line": x[1], "po": x[2], "warehouse": x[3], "received": F(x[5]), "expected": F(x[4]), "late_days": late, "event": E, "rm": rm, "supplier": sup}],
                story=story, key=f"{ekey}:wh:{x[1]}")
        return keys

    # ------------------------------------------------------------------ decision docs
    def decision_docs(self, d, story, extra=None):
        C, N = self.C, self.N
        keys = {}
        D = self.did(d["key"])
        E = self.eid(d["event_key"])
        title = self.ev[d["event_key"]]["title"]
        m = Mention(self.nm, C.rng(d["key"], "memo"))
        opts = "; ".join(f"({i + 1}) {o['option'].replace('_', ' ')}: ~{o['est_stockout_days']:g} stockout days, {money(o['est_cost'])}, {o['risk']} risk" + (f" [{o['note']}]" if o.get("note") else "")
                         for i, o in enumerate(d["options"]))
        ent = []
        mt = d["meta"]
        for k_, f_ in (("supplier_id", m.sup), ("rm_id", m.rm), ("plant_id", m.plant), ("warehouse_id", m.wh), ("product_id", m.sku)):
            if mt.get(k_):
                ent.append(f_(mt[k_]))
        why = self.why(d)
        keys["memo"] = C.add("decision_memo", d["decided_at"], [
            "[Context|Situation]: {ents} - {ctxt} ({evt}).", "[Options considered|Options]: {opts}.", "[Chosen|Decision]: {chosen}, because {why}.",
            "[Expected|Plan]: {ed:g} stockout days, {ec} all-in; outcome review after {rev}."],
            {"dec": D, "role": d["role"], "title": title, "ents": ", ".join(ent) or title, "ctxt": self.ev[d["event_key"]]["event_type"].replace("_", " "), "evt": E, "opts": opts,
             "chosen": d["chosen"].replace("_", " "), "why": why, "ed": d["expected_days"], "ec": money(d["expected_cost"]),
             "rev": F(d["assessed_at"]) if d["assessed_at"] is not None else "the next review"},
            m, f"Decision memo {D}", [D, E] + d["footprint"][:6],
            [{"kind": "decision", "decision": D, "event": E, "type": d["type"], "chosen": d["chosen"], "options": [o["option"] for o in d["options"]], "expected_cost": d["expected_cost"],
              "expected_days": d["expected_days"], "decided": F(d["decided_at"]), "role": d["role"], "why": why, "entities": {k: mt.get(k) for k in ("supplier_id", "rm_id", "plant_id", "warehouse_id", "product_id") if mt.get(k)}}],
            story=story, key=f"{d['key']}:memo")
        # commitments made under the decision
        for i, cm in enumerate(self.cm_by_dec.get(d["key"], [])[:3]):
            m = Mention(self.nm, C.rng(d["key"], "cm", i))
            keys[f"cm{i}"] = C.add("commitment_confirmation", cm["made"], ["[Quantity|Qty] committed: {q}." if cm.get("quantity") else "", "[Due|Due date] {due}; [consequence if missed|if missed]: {penalty}.",
                                                                   "[Linked to|Taken under] decision {dec}; tracked as {cid}."],
                                   {"who": cm["by"], "what": cm["text"], "what_verb": cm["text"], "q": f"{cm['quantity']:,}" if cm.get("quantity") else "", "dec": D, "due": F(cm["due"]),
                                    "penalty": cm.get("penalty") or "none agreed", "cid": cm["commitment_id"]},
                                   m, f"Commitment {cm['commitment_id']}", [cm["commitment_id"], D] + cm["evidence"][:3],
                                   [{"kind": "commitment", "commitment": cm["commitment_id"], "counterparty": cm["cp"], "due": F(cm["due"]), "made": F(cm["made"]), "text": cm["text"],
                                     "status": cm["status"], "resolved": F(cm["resolved"]) if cm.get("resolved") is not None and not pd.isna(cm["resolved"]) else None, "decision": D}],
                                   story=story, key=f"{d['key']}:cm{i}")
        # state updates that supersede the exception note (expedite pull-in / spot lot / cancel)
        ex = extra or {}
        if d["key"].startswith("dec:cas") and ex:
            c = ex["c"]
            po_id = ex["po"]
            revs = self.rev_by_po.get(po_id)
            m = Mention(self.nm, C.rng(d["key"], "upd"))
            prev = f"{d['event_key']}:exc"
            if d["type"] == "expedite" and revs is not None and (revs.reason_code == "expedite_air_freight").any():
                r1 = revs[revs.reason_code == "expedite_air_freight"].iloc[-1]
                keys["upd"] = C.add("state_update", r1.revised_at, [
                    "{rpo} ({rm}) is now [booked on air freight|expedited by air], new ETA {new} (was {old}).", "[Premium approved under|Approved in] {dec}."],
                    {"what_changed": f"{po_id} expedited, ETA {F(r1.new_expected_at)}", "rpo": m.rec(po_id), "rm": m.rm(ex["rm"]), "new": F(r1.new_expected_at), "old": F(r1.old_expected_at), "dec": D},
                    m, f"Revised ETA for {po_id}", [po_id, r1.revision_id, D],
                    [{"kind": "eta_update", "po": po_id, "old": F(r1.old_expected_at), "new": F(r1.new_expected_at), "revision_id": r1.revision_id, "decision": D}],
                    supersedes=[prev], story=story, key=f"{d['key']}:upd")
            elif d["type"] == "switch_supplier" and c["new_lines"]:
                nl = c["new_lines"][0]
                hh = self.rpo.loc[nl["rm_purchase_order_id"]]
                txt = "Original {rpo} is [cancelled|cancelled with the supplier]." if mt.get("cancelled") else "Original {rpo} still expected {slip}."
                keys["upd"] = C.add("state_update", hh.ordered_at, [
                    "Spot lot {spo} of {q} {rm} placed with {sup2}, due {due2}.", txt, "[Supply plan|MRP] now counts on {spo} for {plant}."],
                    {"what_changed": f"{ex['rm']} for {ex['plant']} re-sourced to {nl['supplier_id']}", "spo": m.rec(nl["rm_purchase_order_id"]), "q": qty(nl["qty"], N.uom[ex["rm"]]), "rm": m.rm(ex["rm"]),
                     "sup2": m.sup(nl["supplier_id"]), "due2": F(hh.promised_at), "rpo": m.rec(po_id), "slip": F(c["slip_to_date"]), "plant": m.plant(ex["plant"])},
                    m, f"Re-sourcing of {ex['rm']} for {ex['plant']}", [nl["rm_purchase_order_id"], po_id, D],
                    [{"kind": "resourced", "po": po_id, "spot_po": nl["rm_purchase_order_id"], "spot_supplier": nl["supplier_id"], "spot_due": F(hh.promised_at), "cancelled": bool(mt.get("cancelled")), "decision": D}],
                    supersedes=[prev], story=story, key=f"{d['key']}:upd")
            elif d["type"] == "reallocate_stock" and c.get("transfer"):
                t = c["transfer"]
                keys["upd"] = C.add("state_update", self.t0 + t["out_day"] * DAY, [
                    "{q} of {rm} [moving|shipped] from {donor} to {plant} under {tid}, arriving {arr}.", "[Covers|Should cover] {run}; supplier lot {rpo} no longer on the critical path."],
                    {"what_changed": f"{ex['rm']} transfer {t['transfer_id']} from {t['donor_plant']}", "q": qty(t["qty"], N.uom[ex["rm"]]), "rm": m.rm(ex["rm"]), "donor": m.plant(t["donor_plant"]),
                     "plant": m.plant(ex["plant"]), "tid": m.rec(t["transfer_id"]), "arr": F(self.t0 + t["in_day"] * DAY), "run": m.rec(c["production_run_id"]), "rpo": m.rec(po_id)},
                    m, f"Inter-plant transfer {t['transfer_id']}", [t["transfer_id"], D, po_id],
                    [{"kind": "transfer", "transfer": t["transfer_id"], "donor": t["donor_plant"], "receiver": ex["plant"], "qty": t["qty"], "rm": ex["rm"], "decision": D}],
                    supersedes=[prev], story=story, key=f"{d['key']}:upd")
        # post-mortem
        if d["outcome"] is not None and d["assessed_at"] is not None:
            m = Mention(self.nm, C.rng(d["key"], "pm"))
            ents = []
            for k_, f_ in (("supplier_id", m.sup), ("rm_id", m.rm), ("plant_id", m.plant), ("warehouse_id", m.wh), ("product_id", m.sku)):
                if mt.get(k_):
                    ents.append(f_(mt[k_]))
            happened = self.happened(d)
            keys["pm"] = C.add("post_mortem", d["assessed_at"], [
                "[Scope|Covers]: {ents}.", "[What happened|Result]: {happened}",
                "[Planned|Expected]: {ed:g} stockout days / {ec}. [Actual|Outcome]: {ad:g} days / {ac} - [rated|we rate it] {lab}.", "[Lesson|Takeaway]: {lesson}"],
                {"dec": D, "title": title, "decided": F(d["decided_at"]), "ents": ", ".join(ents) or title, "happened": happened, "ed": d["expected_days"], "ec": money(d["expected_cost"]),
                 "ad": d["actual_days"], "ac": money(d["actual_cost"]), "lab": d["outcome"], "lesson": d["lesson"] or ""},
                m, f"Post-mortem {D}", [D, E] + d["footprint"][:4],
                [{"kind": "outcome", "decision": D, "event": E, "type": d["type"], "outcome": d["outcome"], "actual_cost": d["actual_cost"], "actual_days": d["actual_days"],
                  "expected_cost": d["expected_cost"], "expected_days": d["expected_days"], "attribution": d.get("attribution"), "lesson": d["lesson"], "decided": F(d["decided_at"]),
                  "happened": happened, "entities": {k: mt.get(k) for k in ("supplier_id", "rm_id", "plant_id", "warehouse_id", "product_id") if mt.get(k)}}],
                story=story, key=f"{d['key']}:pm")
        return keys

    # ------------------------------------------------------------------ other decision stories
    def fg_story(self, d):
        C = self.C
        mt = d["meta"]
        keys = {}
        story = d["key"]
        E = self.eid(d["event_key"])
        k = mt.get("fg_kind")
        if k == "expedite":
            r = mt["line"]
            m = Mention(self.nm, C.rng(d["key"], "pre"))
            keys["pre"] = C.add("warehouse_ops_note", d["decided_at"] - DAY, [
                "{sku} [down to|at] {b:.0f} units vs reorder point {rop}.", "[Inbound|Next receipt] {po} from {sup} due {exp}.", "[Asked buyer to push it.|Requesting expedite.]"],
                {"wh": m.wh(r["warehouse_id"]), "sku": m.sku(r["product_id"]), "b": mt["balance"], "rop": mt["rop"], "po": m.rec(r["purchase_order_id"]), "sup": m.sup(r["supplier_id"]), "exp": F(r["expected_at"])},
                m, f"Low stock of {r['product_id']} at {r['warehouse_id']}", [r["purchase_order_id"], E],
                [{"kind": "low_stock", "product": r["product_id"], "warehouse": r["warehouse_id"], "balance": mt["balance"], "rop": mt["rop"], "po": r["purchase_order_id"], "event": E}], story=story, key=f"{d['key']}:pre")
            keys.update(self.decision_docs(d, story))
            m = Mention(self.nm, C.rng(d["key"], "rcv"))
            late = int((r["received_at"] - r["expected_at"]).days)
            keys["rcv"] = C.add("warehouse_ops_note", r["received_at"], [
                "Expedited {po} ({sku}) [arrived|received] {when}.", "[Expedite premium on the invoice:|Invoice shows premium of] {cost}."],
                {"wh": m.wh(r["warehouse_id"]), "po": m.rec(r["purchase_order_id"]), "sku": m.sku(r["product_id"]),
                 "when": (f"{-late} days early" if late < 0 else "on the due date" if late == 0 else f"{late} days late despite the expedite"), "cost": money(d["actual_cost"])},
                m, f"Receipt of expedited {r['purchase_order_id']}", [r["purchase_order_id"], E, self.did(d["key"])],
                [{"kind": "expedite_receipt", "po": r["purchase_order_id"], "late_days": late, "cost": d["actual_cost"], "warehouse": r["warehouse_id"], "decision": self.did(d["key"])}],
                supersedes=[v for k_, v in keys.items() if k_.startswith("cm")] if late > 0 else [], story=story, key=f"{d['key']}:rcv")
        elif k == "transfer":
            m = Mention(self.nm, C.rng(d["key"], "pre"))
            keys["pre"] = C.add("warehouse_ops_note", d["decided_at"], [
                "{sku} [short|low] here: {b:.0f} units vs ROP {rop}.", "[Requested|Asked for] {q} units from {src}."],
                {"wh": m.wh(mt["dst"]), "sku": m.sku(mt["product_id"]), "b": mt["b_dst"], "rop": mt["rop"], "q": mt["qty"], "src": m.wh(mt["src"])},
                m, f"Transfer request for {mt['product_id']}", [E], [{"kind": "low_stock", "product": mt["product_id"], "warehouse": mt["dst"], "balance": mt["b_dst"], "rop": mt["rop"], "event": E}],
                story=story, key=f"{d['key']}:pre")
            keys.update(self.decision_docs(d, story))
            m = Mention(self.nm, C.rng(d["key"], "tr"))
            keys["tr"] = C.add("warehouse_ops_note", d["decided_at"] + DAY, [
                "Transfer {tid} [done|completed]: {q} units of {sku} from {src} to {dst}."],
                {"wh": m.wh(mt["src"]), "tid": m.rec(mt["transfer_id"]), "q": mt["qty"], "sku": m.sku(mt["product_id"]), "src": m.wh(mt["src"]), "dst": m.wh(mt["dst"])},
                m, f"Transfer {mt['transfer_id']}", [mt["transfer_id"], self.did(d["key"])],
                [{"kind": "fg_transfer", "transfer": mt["transfer_id"], "qty": mt["qty"], "src": mt["src"], "dst": mt["dst"], "product": mt["product_id"]}], story=story, key=f"{d['key']}:tr")
        else:
            keys.update(self.decision_docs(d, story))
        return keys

    def mitigation_story(self, d):
        C, N = self.C, self.N
        mt = d["meta"]
        story = d["key"]
        keys = {}
        E = self.eid(d["event_key"])
        e = self.ev[d["event_key"]]
        m = Mention(self.nm, C.rng(d["key"], "trg"))
        if d["key"].startswith("dec:ssb"):
            b = mt["build"]
            keys["trg"] = C.add("shift_handover", e["detected_at"], [
                "[Flagging a pattern|Recurring issue]: {txt}", "[Propose|Suggest] raising the buffer on {rm} before the next peak.", "[Most dips|The dips] coincided with late inbound lots."],
                {"plant": m.plant(b["plant_id"]), "plant_short": b["plant_id"], "rm": m.rm(b["rm_id"]), "txt": e["root_cause_text"]},
                m, f"Safety-stock breaches {b['rm_id']} at {b['plant_id']}", [E] + e["anchor_ids"][:3], [{"kind": "ss_breaches", "rm": b["rm_id"], "plant": b["plant_id"], "event": E}], story=story, key=f"{d['key']}:trg")
            keys.update(self.decision_docs(d, story))
            m = Mention(self.nm, C.rng(d["key"], "upd"))
            keys["upd"] = C.add("state_update", b["date"], ["Safety stock for {rm} at {plant} raised from {o:g} to {n:g} days of cover ({dec})."],
                                {"what_changed": f"SS for {b['rm_id']} at {b['plant_id']} raised", "rm": m.rm(b["rm_id"]), "plant": m.plant(b["plant_id"]), "o": b["old_ss_days"], "n": b["new_ss_days"], "dec": self.did(d["key"])},
                                m, f"Safety stock change {b['rm_id']} {b['plant_id']}", [self.did(d["key"])] + d["footprint"],
                                [{"kind": "ss_change", "rm": b["rm_id"], "plant": b["plant_id"], "old_days": b["old_ss_days"], "new_days": b["new_ss_days"], "effective": F(b["date"])}], story=story, key=f"{d['key']}:upd")
        elif d["key"].startswith("dec:alc"):
            a = mt["alloc"]
            keys["trg"] = C.add("supplier_qbr_notes", e["detected_at"], [
                "{rm}: {txt}", "[We flagged|Raised] that volume may move to {p2} if this continues."],
                {"sup": m.sup(a["old_primary"]), "quarter": pd.Timestamp(e["detected_at"]).to_period("Q").strftime("%YQ%q"), "rm": m.rm(a["rm_id"]), "txt": e["root_cause_text"], "p2": m.sup(a["new_partner"])},
                m, f"Performance review {a['old_primary']} on {a['rm_id']}", [E], [{"kind": "perf_decline", "supplier": a["old_primary"], "rm": a["rm_id"], "late_share": a["late_share_before"], "event": E}],
                story=story, key=f"{d['key']}:trg")
            keys.update(self.decision_docs(d, story))
            m = Mention(self.nm, C.rng(d["key"], "upd"))
            upd = a["update"]
            desc = "; ".join(f"{s} {v.get('sourcing_rank', '')} {v.get('allocation_pct', 0):.0f}%".replace("  ", " ") for s, v in upd.items())
            keys["upd"] = C.add("state_update", a["date"], ["[Sourcing split|Allocation] for {rm} changed from {d0}: {desc}.", "[Primary supplier changed.|Volume moved.]" if a["kind"] == "switch_supplier" else "[Volume rebalanced.|Split rebalanced.]"],
                                {"what_changed": f"allocation for {a['rm_id']} changed", "rm": m.rm(a["rm_id"]), "d0": F(a["date"]), "desc": desc},
                                m, f"Allocation change {a['rm_id']}", [self.did(d["key"])] + d["footprint"][:4],
                                [{"kind": "allocation_change", "rm": a["rm_id"], "old_primary": a["old_primary"], "new_partner": a["new_partner"], "effective": F(a["date"]), "kind2": a["kind"], "update": {k: {kk: vv for kk, vv in v.items()} for k, v in upd.items()}}],
                                story=story, key=f"{d['key']}:upd")
        elif d["key"].startswith("dec:sub"):
            ch = mt["change"]
            keys["trg"] = C.add("escalation_email", e["detected_at"], ["{txt}", "[Proposing|Request] QA trial of {new} as a substitute in {sku}."],
                                {"topic": f"{ch['old_rm']} supply risk", "to": "Quality Lead", "frm": "Materials Manager", "txt": e["root_cause_text"], "new": m.rm(ch["new_rm"]), "sku": m.sku(ch["product_id"])},
                                m, f"Substitution request {ch['product_id']}", [E], [{"kind": "sub_request", "old_rm": ch["old_rm"], "new_rm": ch["new_rm"], "product": ch["product_id"], "event": E}],
                                story=story, key=f"{d['key']}:trg")
            keys.update(self.decision_docs(d, story))
            m = Mention(self.nm, C.rng(d["key"], "upd"))
            keys["upd"] = C.add("state_update", ch["date"], ["BOM for {sku} [updated|re-issued]: {old} replaced by {new} from {d0}."],
                                {"what_changed": f"BOM change on {ch['product_id']}", "sku": m.sku(ch["product_id"]), "old": m.rm(ch["old_rm"]), "new": m.rm(ch["new_rm"]), "d0": F(ch["date"])},
                                m, f"BOM change {ch['product_id']}", [self.did(d["key"])] + d["footprint"][:4],
                                [{"kind": "bom_change", "product": ch["product_id"], "old_rm": ch["old_rm"], "new_rm": ch["new_rm"], "effective": F(ch["date"])}], story=story, key=f"{d['key']}:upd")
        return keys

    def negotiation_story(self, n, d=None):
        C, N = self.C, self.N
        story = n["key"]
        keys = {}
        NID = self.M["nmap"][n["key"]]
        m = Mention(self.nm, C.rng(n["key"], "open"))
        ents = f"{m.rm(n['rm_id'])} " if n.get("rm_id") else ""
        keys["open"] = C.add("supplier_call_summary", n["started_at"], [
            "Topic: {topic} {ents}([negotiation|talks] {nid}).", "[Their position|They opened with]: {offer}.", "[Our ask|We asked for]: {ask}."],
            {"sup": m.sup(n["supplier_id"]), "topic": n["topic"].replace("_", " "), "ents": ents, "nid": NID, "offer": n["their_offer"], "ask": n["our_ask"]},
            m, f"Negotiation {NID} opened", [NID] + ([n["contract_id"]] if n.get("contract_id") else []),
            [{"kind": "neg_open", "negotiation": NID, "supplier": n["supplier_id"], "topic": n["topic"], "their_offer": n["their_offer"], "our_ask": n["our_ask"], "rm": n.get("rm_id")}], story=story, key=f"{n['key']}:open")
        if d is not None:
            keys.update(self.decision_docs(d, story))
        if n["concluded_at"] is not None:
            m = Mention(self.nm, C.rng(n["key"], "close"))
            conc = "; ".join(f"{x['party']}: {x['item']} {x['value']}" for x in n["concessions"])
            dt = "state_update" if n["topic"] in ("price_increase", "annual_renewal") else "supplier_call_summary"
            keys["close"] = C.add(dt, n["concluded_at"], ["{sup} [agreed|signed|closed]: {terms}.", "[Concessions|Give and take]: {conc}.", "[Outcome|Result]: {out}."],
                                  {"what_changed": f"{n['topic'].replace('_', ' ')} terms with {n['supplier_id']}", "sup": m.sup(n["supplier_id"]), "terms": n["final_terms"], "conc": conc or "none", "out": n["outcome"]},
                                  m, f"Negotiation {NID} concluded", [NID] + ([n["contract_id"]] if n.get("contract_id") else []),
                                  [{"kind": "neg_close", "negotiation": NID, "supplier": n["supplier_id"], "terms": n["final_terms"], "outcome": n["outcome"], "topic": n["topic"], "rm": n.get("rm_id"),
                                    "price": (n["meta"]["spike"]["new_price"] if n["topic"] == "price_increase" else None)}],
                                  supersedes=[keys["open"]] if dt == "state_update" else [], story=story, key=f"{n['key']}:close")
            for i, cm in enumerate(self.cm_by_neg.get(n["key"], [])):
                m = Mention(self.nm, C.rng(n["key"], "cm", i))
                keys[f"cm{i}"] = C.add("commitment_confirmation", cm["made"], ["[Under|From] negotiation {nid}."],
                                       {"who": cm["by"], "what": cm["text"], "what_verb": cm["text"], "nid": NID, "due": F(cm["due"]), "penalty": cm.get("penalty") or "none agreed"},
                                       m, f"Commitment {cm['commitment_id']}", [cm["commitment_id"], NID],
                                       [{"kind": "commitment", "commitment": cm["commitment_id"], "counterparty": cm["cp"], "due": F(cm["due"]), "made": F(cm["made"]), "text": cm["text"],
                                         "status": cm["status"], "resolved": F(cm["resolved"]) if cm.get("resolved") is not None and not pd.isna(cm["resolved"]) else None, "negotiation": NID}],
                                       story=story, key=f"{n['key']}:cm{i}")
        return keys

    def episode_story(self, e):
        C = self.C
        E = self.eid(e["key"])
        m = Mention(self.nm, C.rng(e["key"], "ep"))
        mt = e["meta"]
        pat = mt.get("pattern")
        if e["origin_entity_type"] == "supplier":
            return C.add("supplier_call_summary", e["end_date"], ["[Raised|Discussed] {title}: {txt}", "[They blamed|Their explanation:] {cause}."],
                         {"sup": m.sup(e["origin_entity_id"]), "title": e["title"], "txt": e["root_cause_text"], "cause": e["root_cause_code"].replace("_", " ")},
                         m, e["title"], [E] + e["anchor_ids"][:5], [{"kind": "episode", "event": E, "pattern": pat, "entity": e["origin_entity_id"], "text": e["root_cause_text"]}], story=e["key"], key=f"{e['key']}:ep")
        if e["origin_entity_type"] == "plant":
            return C.add("shift_handover", e["end_date"], ["{txt}"], {"plant": m.plant(e["origin_entity_id"]), "plant_short": e["origin_entity_id"], "txt": e["root_cause_text"]},
                         m, e["title"], [E] + e["anchor_ids"][:5], [{"kind": "episode", "event": E, "pattern": pat, "entity": e["origin_entity_id"], "text": e["root_cause_text"]}], story=e["key"], key=f"{e['key']}:ep")
        return C.add("escalation_email", e["end_date"], ["{txt}", "[Please review supply plans.|Planning to review cover.]"],
                     {"topic": e["title"], "to": "S&OP Manager", "frm": "Logistics Lead" if pat == "P02" else "Quality Lead", "txt": e["root_cause_text"]},
                     m, e["title"], [E] + e["anchor_ids"][:5], [{"kind": "episode", "event": E, "pattern": pat, "entity": e["origin_entity_id"], "text": e["root_cause_text"]}], story=e["key"], key=f"{e['key']}:ep")

    def fg_event_story(self, e):
        C = self.C
        E = self.eid(e["key"])
        m = Mention(self.nm, C.rng(e["key"], "fg"))
        k = e["meta"].get("kind")
        if k == "fg_supplier_cluster" or k == "short_cluster":
            return C.add("supplier_call_summary", e["detected_at"] + 2 * DAY, ["{txt}", "[They committed to a recovery plan.|Asked for a recovery plan.]"],
                         {"sup": m.sup(e["origin_entity_id"]), "txt": e["root_cause_text"]}, m, e["title"], [E] + e["anchor_ids"][:6],
                         [{"kind": "fg_cluster", "event": E, "supplier": e["origin_entity_id"], "pos": e["anchor_ids"][:10]}], story=e["key"], key=f"{e['key']}:fg")
        if e["origin_entity_type"] == "warehouse":
            return C.add("warehouse_ops_note", e["end_date"], ["{txt}"], {"wh": m.wh(e["origin_entity_id"]), "txt": e["root_cause_text"]}, m, e["title"], [E] + e["anchor_ids"][:4],
                         [{"kind": "fg_event", "event": E, "type": e["event_type"], "warehouse": e["origin_entity_id"]}], story=e["key"], key=f"{e['key']}:fg")
        return C.add("escalation_email", e["detected_at"], ["{txt}"], {"topic": e["title"], "to": "S&OP Manager", "frm": "Supply Planner", "txt": e["root_cause_text"]}, m, e["title"],
                     [E] + e["anchor_ids"][:4], [{"kind": "fg_event", "event": E, "type": e["event_type"]}], story=e["key"], key=f"{e['key']}:fg")


# =============================================================================
# periodic + noise
# =============================================================================
def periodic_docs(ctx, C: Corpus, nm: Names2, M, T, n_qbr_sup: int):
    sc = T["supplier_scorecard_monthly"].copy()
    sc["q"] = sc.month.dt.to_period("Q")
    plan = ctx.load_internal("structure")["pattern_plan"]
    focus = [plan[k]["supplier_id"] for k in ("P01", "P04", "P09", "P11", "P03") if k in plan]
    busiest = sc.groupby("supplier_id").po_count.sum().sort_values(ascending=False).index.tolist()
    sups = list(dict.fromkeys(focus + busiest))[:n_qbr_sup]
    for s in sups:
        prev_key = None
        for q, g in sc[sc.supplier_id == s].groupby("q"):
            if g.po_count.sum() == 0:
                continue
            d = q.end_time.normalize() + pd.Timedelta(days=int(C.rng("qbr", s, str(q)).integers(8, 20)))
            m = Mention(nm, C.rng("qbr", s, str(q)))
            otif = float((g.otif_rate * g.po_count).sum() / max(1, g.po_count.sum()))
            dl = float((g.avg_delay_days.fillna(0) * g.po_count).sum() / max(1, g.po_count.sum()))
            C.add("supplier_qbr_notes", d, ["[Scorecard|Numbers] for {quarter}: {n} POs due, OTIF {otif:.0%}, average delay {dl:.1f} days, fill {fill:.1%}.",
                                            "Commitments made {cm}, kept {ck}.", "{comment}"],
                  {"sup": m.sup(s), "quarter": str(q), "n": int(g.po_count.sum()), "otif": otif, "dl": dl, "fill": float(g.fill_rate.mean()), "cm": int(g.commitments_made.sum()),
                   "ck": int(g.commitments_kept.sum()), "comment": "[Asked for a recovery plan.|OTIF below target; recovery plan requested.]" if otif < 0.5 else "[Performance acceptable.|No escalation needed.]"},
                  m, f"QBR {s} {q}", [s], supersedes=[prev_key] if prev_key else [], facts=None or [{"kind": "qbr", "supplier": s, "quarter": str(q), "po_count": int(g.po_count.sum()), "otif": round(otif, 3), "avg_delay": round(dl, 2),
                                             "commitments_made": int(g.commitments_made.sum()), "commitments_kept": int(g.commitments_kept.sum())}], story=f"qbr:{s}", key=f"qbr:{s}:{q}")
            prev_key = f"qbr:{s}:{q}"
    dem = T["product_demand_weekly"]
    cat = ctx.src["products"].set_index("product_id").category
    dem = dem.assign(cat=dem.product_id.map(cat), month=dem.week_start.dt.to_period("M"))
    ev = T["disruption_events"].set_index("event_id")
    for mo, g in dem.groupby("month"):
        a = g[g.actual_demand_units > 0]
        bias = a.groupby("cat").apply(lambda z: z.forecast_units.sum() / max(1, z.actual_demand_units.sum()) - 1)
        ape = ((a.forecast_units - a.actual_demand_units).abs() / a.actual_demand_units).mean()
        shocks = g.demand_shock_event_id.dropna().unique().tolist()
        d = mo.end_time.normalize() + pd.Timedelta(days=int(C.rng("fcst", str(mo)).integers(3, 10)))
        m = Mention(nm, C.rng("fcst", str(mo)))
        top = bias.idxmax()
        sh_txt = "; ".join(f"{s} ({ev.at[s, 'title']})" for s in shocks[:3]) or "none"
        C.add("forecast_review_minutes", d, ["MAPE for {month} at SKU-location-week level: {ape:.0%}.", "Bias by category: {bias}.",
                                             "[Largest over-forecast|Most over-forecast]: {top} ({tb:+.0%}).", "Promo weeks: {promo}. Demand events: {sh}."],
              {"month": str(mo), "ape": ape, "bias": ", ".join(f"{k} {v:+.0%}" for k, v in bias.items()), "top": top, "tb": float(bias[top]), "promo": int(g.promo_flag.sum()), "sh": sh_txt},
              m, f"Forecast review {mo}", shocks[:3], [{"kind": "forecast_review", "month": str(mo), "mape": round(float(ape), 3), "bias": {k: round(float(v), 3) for k, v in bias.items()}, "shocks": shocks}],
              story="forecast", key=f"fcst:{mo}")


def noise_docs(ctx, C: Corpus, nm: Names2, T, n: int):
    rng = ctx.rng("noise")
    rpo = T["rm_purchase_orders"]
    ontime = rpo[(rpo.status == "received") & (rpo.received_at <= rpo.promised_at)]
    lines = T["rm_purchase_order_lines"].set_index("rm_purchase_order_id")
    po = ctx.src["purchase_orders"]
    fg_ontime = po[(po.status == "received") & (po.received_at <= po.expected_at)]
    kinds = ["handover", "wh", "it", "holiday", "safety", "fcst"]
    for i in range(n):
        k = kinds[i % len(kinds)]
        r = ctx.rng("noise", i)
        m = Mention(nm, r)
        if k == "handover":
            x = ontime.iloc[int(r.integers(len(ontime)))]
            ln = lines.loc[[x.rm_purchase_order_id]].iloc[0]
            C.add("shift_handover", x.received_at, ["[Quiet shift.|Routine shift.|Nothing major.] {rpo} ({rm}) received on time from {sup} and released by QC.", "[Changeover on line 2 done.|Line 3 CIP completed.|Forklift battery swap done.]",
                                                     "[No safety incidents.|Housekeeping audit passed.|Near-miss log empty.]", "[All runs started to schedule.|Schedule attainment on target.|No material holds.]",
                                                     "[Maintenance checked the filler seals.|Compressor 2 back online.|Label printer head replaced.]"],
                  {"plant": m.plant(x.plant_id), "plant_short": x.plant_id, "rpo": m.rec(x.rm_purchase_order_id), "rm": m.rm(ln.rm_id), "sup": m.sup(x.supplier_id)},
                  m, f"Routine handover {x.plant_id}", [x.rm_purchase_order_id], noise=True, key=f"noise:{i}")
        elif k == "wh":
            x = fg_ontime.iloc[int(r.integers(len(fg_ontime)))]
            C.add("warehouse_ops_note", x.received_at, ["{po} from {sup} received [on time|as planned|without issues] and put away.", "[Dock schedule normal.|Yard clear by noon.|All doors running.]", "[Cycle counts on schedule.|No damages logged.]",
                                                         "[Pick rates at plan.|Outbound cut-off met.|Carrier pickups on time.]", "[Temp staff briefed on the new slotting.|Racking inspection booked.|Reach truck back from service.]"],
                  {"wh": m.wh(x.warehouse_id), "po": m.rec(x.purchase_order_id), "sup": m.sup(x.supplier_id)}, m, f"Routine DC note {x.warehouse_id}", [x.purchase_order_id], noise=True, key=f"noise:{i}")
        elif k == "it":
            d = pd.Timestamp(ctx.calib["window_start"]) + pd.Timedelta(days=int(r.integers(0, 1090)))
            C.add("state_update", d, ["[WMS|ERP|MRP] maintenance window [Saturday night|Sunday 02:00-06:00]; [no action needed|expect short outages].", "[Password reset reminder for planners.|New report layout goes live Monday.]",
                                      "[Save open work before the window.|Batch jobs will re-run automatically.]", "[Service desk extension 4400 for issues.|Raise tickets through the portal as usual.]", "[No impact on EDI with suppliers.|EDI queues will catch up after the window.]"],
                  {"what_changed": "[system maintenance|IT notice]"}, m, "IT notice", [], noise=True, key=f"noise:{i}")
        elif k == "holiday":
            s = ctx.src["suppliers"].sample(1, random_state=int(r.integers(1 << 30))).iloc[0]
            d = pd.Timestamp(ctx.calib["window_start"]) + pd.Timedelta(days=int(r.integers(0, 1090)))
            C.add("supplier_call_summary", d, ["[Courtesy call|Relationship check-in]; nothing open on our side.", "[They shared their holiday calendar.|New account manager introduced.|Plant tour offered for next quarter.]",
                                               "[No price or capacity changes announced.|They confirmed no change to lead times.]", "[Invoices are up to date.|No open quality claims.]", "[Next routine touch-point in a quarter.|Will speak again at the QBR.]"],
                  {"sup": m.sup(s.supplier_id)}, m, f"Routine supplier contact {s.supplier_id}", [], noise=True, key=f"noise:{i}")
        elif k == "safety":
            p = ctx.table("plants").sample(1, random_state=int(r.integers(1 << 30))).iloc[0]
            d = pd.Timestamp(ctx.calib["window_start"]) + pd.Timedelta(days=int(r.integers(0, 1090)))
            C.add("shift_handover", d, ["[Safety walk done.|Toolbox talk on manual handling.|Fire drill completed.]", "[Canteen menu changed.|Parking lot resurfacing next week.|Visitors from HQ on Thursday.]",
                                        "[Production ran to plan.|No schedule changes.|Lines ran at standard rate.]", "[No material shortages reported.|All materials in stock for the next shift.]", "[Waste bins emptied, area tidy.|5S audit score steady.]"],
                  {"plant": m.plant(p.plant_id), "plant_short": p.plant_id}, m, f"Routine plant note {p.plant_id}", [], noise=True, key=f"noise:{i}")
        else:
            d = pd.Timestamp(ctx.calib["window_start"]) + pd.Timedelta(days=int(r.integers(0, 1090)))
            C.add("forecast_review_minutes", d, ["[Pre-read|Agenda] circulated; [no exceptions raised|no changes to the consensus plan].", "[Attendance: demand, supply, finance.|Finance joined late.]",
                                                 "[Consensus numbers carried forward.|No overrides this cycle.]", "[Promo calendar unchanged.|No new launches this month.]", "[Actions from last time closed.|No open actions.]"],
                  {"month": d.to_period("M").strftime("%Y-%m")}, m, "Routine forecast meeting", [], noise=True, key=f"noise:{i}")


# =============================================================================
def run(ctx: Ctx, use_llm: bool = False):
    banner("STAGE 4a - narrative memory corpus")
    cfg = ctx.cfg
    M = ctx.load_internal("memory")
    from schema import SCHEMA
    T = {n: read_typed(ctx, n) for n in SCHEMA}
    nm = Names2(ctx)
    C = Corpus(ctx, nm)
    st = Stories(ctx, C, nm, M, T)
    target = cfg["scale"]["target_docs"]
    signal_budget = int(round(target * (1 - cfg["narratives"]["noise_share"])))
    max_st = cfg["scale"]["max_narrated_stories"]

    tier = {}
    periodic_docs(ctx, C, nm, M, T, n_qbr_sup=4 if cfg["scale_name"] == "small" else 10)
    for d in C.docs:
        tier[d["story"] if d["story"] not in ("forecast",) and not str(d["story"]).startswith("qbr:") else d["key"]] = (5, d["date"])
    negs = {n["key"]: n for n in M["negs"]}
    decs = sorted(M["decisions"], key=lambda d: d["decided_at"])
    for d in decs:
        if d["key"].startswith("dec:cas"):
            st.cascade(d["event_key"], full=True)
            tier[d["event_key"]] = (0, d["decided_at"])
        elif d["key"].startswith("dec:fg"):
            st.fg_story(d)
            tier[d["key"]] = (3, d["decided_at"])
        elif d["key"].startswith(("dec:ssb", "dec:alc", "dec:sub")):
            st.mitigation_story(d)
            tier[d["key"]] = (2, d["decided_at"])
        elif d["key"].startswith("dec:neg"):
            st.negotiation_story(negs[d["meta"]["neg_key"]], d)
            tier[d["meta"]["neg_key"]] = (1, d["decided_at"])
    done_negs = {d["meta"].get("neg_key") for d in decs}
    for k, n in negs.items():
        if k not in done_negs and n["topic"] != "annual_renewal":
            st.negotiation_story(n)
            tier[k] = (2, n["started_at"])
    for e in M["events"]:
        if e["meta"].get("kind") == "pattern_episode":
            st.episode_story(e)
            tier[e["key"]] = (5, e["start_date"])
        elif e["meta"].get("kind") in ("fg_supplier_cluster", "short_cluster", "shock", "fg_stockout", "shrink", "country_cluster"):
            st.fg_event_story(e)
            tier[e["key"]] = (6, e["start_date"])
    decided_events = {d["event_key"] for d in decs}
    linked = {b for a, b, t in M["links"] if t in ("similar_to", "recurrence_of") and a in decided_events} | {a for a, b, t in M["links"] if t in ("similar_to", "recurrence_of") and b in decided_events}
    hold = pd.Timestamp(ctx.calib["holdout_start"])
    for e in M["events"]:
        if e["meta"].get("kind") == "cascade" and e["key"] not in decided_events:
            st.cascade(e["key"], full=False)
            tier[e["key"]] = (4 if e["key"] in linked else (7 if e["start_date"] < hold else 4.5), e["start_date"])
    # select whole stories by tier until the signal budget is used; periodic docs capped at 15% of the budget
    units = defaultdict(list)
    for d in C.docs:
        u = d["story"] if d["story"] in tier else d["key"]
        units[u].append(d)
    order = sorted(units, key=lambda u: (tier.get(u, (9, None))[0], ctx.rng("order", u).random()))
    chosen, n_per = [], 0
    for u in order:
        docs_u = units[u]
        if len(chosen) + len(docs_u) > signal_budget:
            continue
        if tier.get(u, (9,))[0] == 5:
            if n_per + len(docs_u) > 0.15 * signal_budget:
                continue
            n_per += len(docs_u)
        chosen.extend(docs_u)
    C.docs = chosen
    n_noise = max(0, int(round(len(C.docs) * cfg["narratives"]["noise_share"] / (1 - cfg["narratives"]["noise_share"]))))
    noise_docs(ctx, C, nm, T, n_noise)

    # ---- ids, supersession resolution, write
    docs = [d for d in C.docs if d is not None]
    docs.sort(key=lambda d: (d["timestamp"], d["key"]))
    kmap = {}
    for i, d in enumerate(docs):
        d["doc_id"] = f"DOC{i + 1:06d}"
        kmap[d["key"]] = d["doc_id"]
    for d in docs:
        d["supersedes"] = [kmap[k] for k in d["supersedes"] if k in kmap]
    if use_llm:
        from llm_narratives import rewrite_docs
        rewrite_docs(ctx, docs)
    out = ctx.out / "memory_corpus.jsonl"
    with open(out, "w") as fh:
        for d in docs:
            fh.write(json.dumps({k: d[k] for k in ("doc_id", "timestamp", "context", "content", "doc_type", "entity_ids", "source_record_ids", "split", "is_noise", "supersedes")}, ensure_ascii=False) + "\n")
    ctx.save_internal("corpus", docs)
    df = pd.DataFrame(docs)
    wc = df.content.str.split().str.len()
    log(f"docs {len(df):,} (target {target}); noise {df.is_noise.mean():.1%}; holdout {int((df.split == 'holdout').sum())}; supersede {float((df.supersedes.str.len() > 0).mean()):.1%}")
    log(f"doc types {df.doc_type.value_counts().to_dict()}")
    log(f"words min/median/max {wc.min()}/{int(wc.median())}/{wc.max()}; stories with >=3 docs: {int((df[~df.is_noise].groupby('story').size() >= 3).sum())}")
    dump_json(variant_counts(), ctx.out / "_internal" / "template_variants.json")
