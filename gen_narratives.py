"""Stage 4a - narrative corpus -> memory_corpus.jsonl (one line = one Hindsight retain item).

Seeded compositional template grammar (opener x body frame x closer, >=15 variants per doc type).
Every rendered fact is recorded in _work/doc_facts.parquet and re-checked against the tables;
ETA/price/primary/commitment-status facts that later change are linked via supersedes_doc_ids.
Optional --llm-narratives rewrites content through any OpenAI-compatible endpoint (cached, resumable),
keeping the recorded facts verbatim.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from common import Ctx, banner, cli

D = pd.Timestamp

T = {  # doc_type -> opener / closer variants; bodies are per-situation frames below
    "po_exception_note": {
        "open": ["PO exception logged {d}.", "Exception note, {d}:", "Flagging a supplier exception today ({d}).", "Buyer desk exception, {d}.",
                 "Heads-up on an inbound PO, logged {d}.", "Logging this against the PO file ({d})."],
        "close": ["Tracker updated.", "Will chase again tomorrow and update the PO file.", "Planning has been copied.", "No action needed from the plant yet; watching it.",
                  "Filed under the supplier's exception log."]},
    "state_update": {
        "open": ["Update {d}:", "State change recorded {d}.", "Correction/update as of {d}.", "Status update ({d}).", "FYI - change effective as of {d}.",
                 "Revised information, {d}:"],
        "close": ["This replaces the earlier figure in the tracker.", "Please use this value going forward.", "Older notes on this item are superseded.",
                  "ERP and the planning sheet have both been updated.", "Shout if anything downstream still shows the old value."]},
    "decision_memo": {
        "open": ["Decision memo, {d}.", "Recording the call we made today ({d}).", "Decision log entry {d}.", "Memo to file - decision taken {d}.",
                 "For the record ({d}): options reviewed and a decision taken."],
        "close": ["We will review the outcome once the material lands.", "Post-mortem to follow.", "Owner to confirm execution by end of day.",
                  "Finance copied for the cost impact.", "Revisit if the supplier changes the date again."]},
    "commitment_confirmation": {
        "open": ["Commitment confirmed {d}.", "Confirming in writing ({d}):", "Per today's call ({d}), the following was committed.", "Commitment log {d}.",
                 "Recording a firm commitment made {d}."],
        "close": ["Logged in the commitments tracker.", "We will hold them to this date.", "Penalty terms apply as per contract.", "Follow-up set for the due date."]},
    "shift_handover": {
        "open": ["Shift handover {d}.", "Handover notes for the next planner ({d}):", "End-of-shift handover, {d}.", "Planner handover {d} - key items:",
                 "Night-shift handover ({d})."],
        "close": ["That's all from me.", "Nothing else pending.", "Call the plant scheduler if the lot shows up early.", "Ping me on chat if anything moves.",
                  "Rest of the board is green."]},
    "escalation_email": {
        "open": ["Escalation - {d}.", "Escalating this to leadership ({d}).", "URGENT: escalation raised {d}.", "Raising an escalation today ({d}).",
                 "Escalation for visibility, {d}."],
        "close": ["Need a decision by tomorrow.", "Requesting approval for premium actions if required.", "Customer service has been briefed.",
                  "Next update at the 4 pm call.", "Please advise on priority."]},
    "post_mortem": {
        "open": ["Post-mortem, {d}.", "Lessons-learned review ({d}).", "Retrospective written {d}.", "After-action review dated {d}.", "Closing review, {d}."],
        "close": ["Lesson added to the playbook.", "Filed with the supplier's history.", "Discussed at the S&OP pre-read.", "Closed."]},
    "supplier_call_summary": {
        "open": ["Supplier call summary {d}.", "Notes from today's call with the supplier ({d}).", "Call recap, {d}.", "Summary of supplier discussion {d}.",
                 "Negotiation/call notes ({d})."],
        "close": ["Next call in two weeks.", "Minutes sent to the supplier for confirmation.", "Category manager to follow up.", "Action items in the tracker."]},
    "supplier_qbr_notes": {
        "open": ["QBR notes {d}.", "Quarterly business review held {d}.", "QBR summary ({d}).", "Notes from the quarterly review ({d}).", "Supplier QBR, {d}:"],
        "close": ["Next QBR next quarter.", "Scorecard shared with the supplier.", "Improvement plan requested.", "Minutes circulated."]},
    "warehouse_ops_note": {
        "open": ["Warehouse ops note {d}.", "DC floor update ({d}).", "Ops log entry {d}.", "Warehouse note for the day ({d}).", "Receiving/ops update, {d}."],
        "close": ["Dock schedule adjusted.", "Inventory control notified.", "Nothing further.", "Customer service informed."]},
    "forecast_review_minutes": {
        "open": ["Forecast review minutes {d}.", "Demand review ({d}) - minutes.", "S&OP demand review held {d}.", "Minutes of the forecast review, {d}.",
                 "Forecast accuracy review {d}."],
        "close": ["Actions assigned to demand planning.", "Next review in four weeks.", "Consensus forecast locked.", "Minutes approved."]},
}
FILLER = ["All dates are delivery dates at the receiving site.", "Quantities are as per the PO line.", "Times are local site time.",
          "Please keep the tracker as the single source of truth.", "Copying the S&OP channel for visibility.",
          "This note is also saved in the shared drive under the supplier folder.", "Reply-all if you see a conflict with the ERP."]
ROLES = {"buyer": "Buyer", "supply_planner": "Supply Planner", "category_manager": "Category Manager", "plant_scheduler": "Plant Scheduler",
         "supply_chain_director": "Supply Chain Director", "quality_engineer": "Quality Engineer", "warehouse_ops_lead": "Warehouse Ops Lead",
         "logistics_coordinator": "Logistics Coordinator", "demand_planner": "Demand Planner", "supplier_account_manager": "Supplier Account Manager"}


class Writer:
    def __init__(self, ctx: Ctx):
        self.ctx, self.cfg = ctx, ctx.cfg
        self.rng = ctx.rng("narratives")
        r, src = ctx.read, ctx.src()
        S = src["suppliers"].set_index("supplier_id")
        self.sname = S.supplier_name.to_dict()
        self.rm = r("raw_materials").set_index("rm_id")
        self.plants = r("plants").set_index("plant_id")
        self.wh = src["warehouses"].set_index("warehouse_id")
        self.docs, self.facts, self.chains = [], [], []
        self.body_variants = defaultdict(set)
        self.hold = D(self.cfg["holdout_start"])
        self.end = D(self.cfg["window_end"])

    # ---- voice
    def sup(self, s, full=True):
        n = self.sname.get(s, s)
        if full:
            return f"{n} ({s})"
        return self.rng.choice([n, s, f"S-{s[-3:]}"])

    def rmn(self, r, full=True):
        n = self.rm.at[r, "rm_name"]
        return f"{n} ({r})" if full else self.rng.choice([r, n.split()[0], n])

    def pl(self, p, full=True):
        return f"{self.plants.at[p, 'plant_name']} ({p})" if full else self.rng.choice([p, f"the {self.plants.at[p, 'region']} plant"])

    def whn(self, w):
        return f"{self.wh.at[w, 'warehouse_name']} ({w})"

    def d(self, x):
        x = D(x)
        return self.rng.choice([x.strftime("%Y-%m-%d"), f"{x.day} {x.strftime('%b %Y')}", x.strftime("%a %d %b %Y")])

    def q(self, v, uom):
        return f"{v:,.1f} {uom}" if uom != "ea" else f"{v:,.0f} ea"

    # ---- doc assembly
    def doc(self, dtype, date, role, context, body, entities, sources, facts=(), noise=False, region=None, situation="", event_id=None):
        date = D(date).normalize()
        while date.weekday() >= 5:
            date += pd.Timedelta(days=1)
        tpl = T[dtype]
        oi, ci = int(self.rng.integers(len(tpl["open"]))), int(self.rng.integers(len(tpl["close"])))
        self.body_variants[dtype].add((situation, body[1]))
        parts = [tpl["open"][oi].format(d=self.d(date))] + body[0]
        words = lambda ps: sum(len(p.split()) for p in ps)
        fill = list(self.rng.permutation(FILLER))
        while words(parts) + len(tpl["close"][ci].split()) + 3 < 64 and fill:
            parts.append(fill.pop())
        parts += [tpl["close"][ci], f"- {ROLES.get(role, role)}"]
        while words(parts) > 250 and len(parts) > 4:
            parts.pop(-3)
        lo, hi = self.cfg["narratives"]["business_hours"]
        h, m = int(self.rng.integers(lo, hi)), int(self.rng.integers(0, 60))
        off = self.cfg["narratives"]["region_utc_offset"].get(region, self.cfg["narratives"]["region_utc_offset"]["_default"])
        ts = f"{date.strftime('%Y-%m-%d')}T{h:02d}:{m:02d}:00{off}"
        key = len(self.docs)
        self.docs.append({"_k": key, "timestamp": ts, "context": context, "content": " ".join(parts), "doc_type": dtype,
                          "entity_ids": sorted(set(str(e) for e in entities if isinstance(e, str) and e)), "source_record_ids": sorted(set(str(s) for s in sources if isinstance(s, str) and s)),
                          "split": "holdout" if date >= self.hold else "memory", "is_noise": noise, "author_role": role,
                          "event_id": event_id, "supersedes_doc_ids": []})
        for f in facts:
            self.facts.append({"_k": key, "ts": ts, **f})
        return key


# ------------------------------------------------------------------ chains
class Chains:
    def __init__(self, ctx: Ctx, w: Writer):
        self.ctx, self.w = ctx, w
        self.chains = []
        r = ctx.read
        self.ev = r("disruption_events").set_index("event_id")
        self.dec = r("decisions").set_index("decision_id")
        self.cm = r("commitments")
        self.neg = r("negotiations").set_index("negotiation_id")
        self.po = r("rm_purchase_orders").set_index("rm_po_id")
        self.pol = r("rm_purchase_order_lines")
        self.rev = r("rm_po_revisions")
        self.rev_by = {k: g.sort_values("revised_at") for k, g in self.rev.groupby("rm_po_id")}
        self.runs = r("production_runs").set_index("production_run_id")
        self.sn = r("rm_inventory_snapshots_weekly")
        self.cat = r("rm_supplier_catalog")
        self.mv = r("rm_inventory_movements")
        self.meta = ctx.load_json("memory_meta.json")
        self.plan = ctx.load_json("rm_ops_plan.json")
        self.eps = {e["id"]: e for e in self.plan["episodes"]}
        self.sim0 = D(self.plan["sim_start"])
        src = ctx.src()
        self.FGL = src["purchase_order_lines"].set_index("purchase_order_line_id")
        self.FGP = src["purchase_orders"].set_index("purchase_order_id")

    def po_docs(self, po_id, role="buyer", ev_id=None, upto=None):
        """First revision -> exception note; later revisions -> state updates (supersede). Returns (first_doc, first_date, reason)."""
        w, rv = self.w, self.rev_by.get(po_id)
        if rv is None:
            return None, None, None
        po = self.po.loc[po_id]
        ln = self.pol[self.pol.rm_po_id == po_id]
        rm_ = ln.rm_id.iat[0]
        uom = w.rm.at[rm_, "uom"]
        first = None
        for i, r in enumerate(rv.itertuples()):
            if upto is not None and r.revised_at > upto:
                break
            reason = r.reason_code.replace("_", " ")
            facts = [{"kind": "po_eta", "key": po_id, "value": str(r.new_expected_at.date()), "record_id": r.rm_po_revision_id}]
            if i == 0:
                facts.append({"kind": "po_reason", "key": po_id, "value": r.reason_code, "record_id": r.rm_po_revision_id})
                frames = [
                    f"{w.sup(po.supplier_id)} advised that {po_id} ({w.q(ln.quantity_ordered.sum(), uom)} of {w.rmn(rm_)} for {w.pl(po.plant_id)}) will not make "
                    f"its promised date of {w.d(po.promised_at)}. New ETA given: {w.d(r.new_expected_at)}. Reason stated: {reason}.",
                    f"{po_id} from {w.sup(po.supplier_id)} is slipping. Promised {w.d(po.promised_at)}, now quoted {w.d(r.new_expected_at)} ({reason}). "
                    f"Material is {w.rmn(rm_)}, {w.q(ln.quantity_ordered.sum(), uom)}, delivering to {w.pl(po.plant_id)}.",
                    f"Received a delay notice on {po_id}: {w.rmn(rm_)} for {w.pl(po.plant_id)}. {w.sup(po.supplier_id, False)} cites {reason}; "
                    f"revised ETA {w.d(r.new_expected_at)} versus the promised {w.d(po.promised_at)}.",
                    f"Supplier {w.sup(po.supplier_id)} called about {po_id}. The {w.rmn(rm_, False)} lot ({w.q(ln.quantity_ordered.sum(), uom)}) is now due "
                    f"{w.d(r.new_expected_at)}, not {w.d(po.promised_at)}. Their explanation: {reason}."]
                fi = int(w.rng.integers(len(frames)))
                k = w.doc("po_exception_note", r.revised_at, role, f"PO exception - {po_id} - {po.supplier_id} - {rm_}", ([frames[fi]], fi),
                          [po.supplier_id, rm_, po.plant_id, po_id], [po_id, r.rm_po_revision_id], facts, region=w.plants.at[po.plant_id, "region"],
                          situation="rm_slip", event_id=ev_id)
                first = (k, str(r.revised_at.date()), r.reason_code)
            else:
                frames = [
                    f"New ETA for {po_id} ({w.rmn(rm_, False)}, {w.sup(po.supplier_id, False)}): {w.d(r.new_expected_at)}. Previous date {w.d(r.old_expected_at)} "
                    f"no longer holds ({reason}).",
                    f"{w.sup(po.supplier_id)} has moved {po_id} again - from {w.d(r.old_expected_at)} to {w.d(r.new_expected_at)}. Reason: {reason}.",
                    f"{po_id} revised: expected at {w.pl(po.plant_id, False)} on {w.d(r.new_expected_at)} (was {w.d(r.old_expected_at)}). Supplier note: {reason}.",
                    f"Revised arrival for {po_id}: {w.d(r.new_expected_at)}. The {w.d(r.old_expected_at)} date given earlier is withdrawn ({reason})."]
                fi = int(w.rng.integers(len(frames)))
                w.doc("state_update", r.revised_at, "logistics_coordinator" if i % 2 else "buyer", f"Revised ETA - {po_id}", ([frames[fi]], fi),
                      [po.supplier_id, rm_, po.plant_id, po_id], [po_id, r.rm_po_revision_id], facts, region=w.plants.at[po.plant_id, "region"],
                      situation="eta_revision", event_id=ev_id)
        return first

    def decision_docs(self, did, ev_id):
        w, d = self.w, self.dec.loc[did]
        m = self.meta["decisions"][did]
        opts = json.loads(d.options_considered_json)
        ol = "; ".join(f"{o['option'].replace('_', ' ')} - est. cost {o['est_cost']:,.0f}, {o['est_stockout_days']} stockout days, {o['risk']} risk"
                       + ("" if o.get("feasible", True) else " (cannot land in time)") for o in opts)
        ents = [m.get("supplier_id"), m.get("rm_id"), m.get("plant_id")]
        frames = [f"Situation: {self.ev.at[d.event_id, 'title']}. Options: {ol}. Decision: {d.chosen_option}. {d.rationale_text}",
                  f"Options on the table for '{self.ev.at[d.event_id, 'title']}' - {ol}. We will {d.chosen_option}. Expected cost {d.expected_cost:,.0f}, "
                  f"expected stockout {d.expected_stockout_days} days.",
                  f"Decision ({d.decision_type.replace('_', ' ')}): {d.chosen_option}. Alternatives considered: {ol}. {d.rationale_text}",
                  f"We reviewed {len(opts)} options ({ol}) and agreed to {d.chosen_option}; planned impact {d.expected_stockout_days} stockout days at "
                  f"~{d.expected_cost:,.0f}."]
        fi = int(w.rng.integers(len(frames)))
        fp = json.loads(d.footprint_ids)
        memo = w.doc("decision_memo", d.decided_at, d.decided_by_role, f"Decision memo - {did} - {d.decision_type}", ([frames[fi]], fi), ents + [ev_id],
                     [did, d.event_id] + fp[:4], [{"kind": "decision", "key": did, "value": d.decision_type, "record_id": did}],
                     region=w.plants.at[m["plant_id"], "region"] if m.get("plant_id") in w.plants.index else None, situation="memo", event_id=ev_id)
        cms = self.cm[self.cm.decision_id == did]
        cdocs = []
        for c in cms.head(2).itertuples():
            cdocs.append(self.commit_docs(c, ev_id))
        pm = None
        if d.outcome_assessed_at <= w.end:
            frames = [f"Decision {did} ({d.decision_type.replace('_', ' ')}) for '{self.ev.at[d.event_id, 'title']}'. Planned: {d.expected_stockout_days} stockout days, "
                      f"cost {d.expected_cost:,.0f}. Actual: {d.actual_stockout_days} days, {d.actual_cost:,.0f}. Outcome: {d.outcome_label}. Lesson: {d.lesson_text}",
                      f"Outcome of {did}: {d.outcome_label}. We expected {d.expected_stockout_days} days short and {d.expected_cost:,.0f}; we got {d.actual_stockout_days} "
                      f"days and {d.actual_cost:,.0f}. {d.lesson_text}",
                      f"Review of '{d.chosen_option}'. Result {d.outcome_label} - actual cost {d.actual_cost:,.0f} vs {d.expected_cost:,.0f} planned, "
                      f"{d.actual_stockout_days} vs {d.expected_stockout_days} stockout days. Takeaway: {d.lesson_text}"]
            fi = int(w.rng.integers(len(frames)))
            pm = w.doc("post_mortem", d.outcome_assessed_at, "supply_chain_director" if w.rng.random() < 0.4 else "supply_planner",
                       f"Post-mortem - {did}", ([frames[fi]], fi), ents + [ev_id], [did, d.event_id],
                       [{"kind": "lesson", "key": did, "value": d.outcome_label, "record_id": did}], situation="pm", event_id=ev_id)
        return memo, cdocs, pm

    def commit_docs(self, c, ev_id):
        w = self.w
        frames = [f"{c.commitment_text}. Quantity {c.quantity:,.1f}." if c.quantity == c.quantity and c.quantity else f"{c.commitment_text}.",
                  f"Commitment ({c.counterparty_type} {c.counterparty_id}): {c.commitment_text}. Due {w.d(c.due_date)}. Terms: {c.penalty_or_credit}.",
                  f"{c.counterparty_id} committed today: {c.commitment_text}. Penalty/credit: {c.penalty_or_credit}."]
        fi = int(w.rng.integers(len(frames)))
        still_open = pd.isna(c.resolved_at) or c.resolved_at > c.made_at + pd.Timedelta(days=3)
        k = w.doc("commitment_confirmation", c.made_at, c.made_by_role if c.made_by_role != "supplier_account_manager" else "buyer",
                  f"Commitment - {c.commitment_id}", ([frames[fi]], fi), [c.counterparty_id], [c.commitment_id] + ([c.decision_id] if pd.notna(c.decision_id) else []),
                  [{"kind": "commitment_made", "key": c.commitment_id, "value": "made", "record_id": c.commitment_id}] +
                  ([{"kind": "commitment_status", "key": c.commitment_id, "value": "open", "record_id": c.commitment_id}] if still_open else []),
                  situation="commit", event_id=ev_id)
        sk = None
        if c.status != "open" and pd.notna(c.resolved_at) and c.resolved_at > c.made_at:
            word = {"fulfilled": "kept", "breached": "missed", "renegotiated": "renegotiated"}[c.status]
            frames = [f"Commitment {c.commitment_id} ('{c.commitment_text}') status: {c.status}. It was {word} as of {w.d(c.resolved_at)}.",
                      f"Closing out {c.commitment_id}: {c.counterparty_id} {word} the commitment made {w.d(c.made_at)} (due {w.d(c.due_date)}). Status {c.status}.",
                      f"{c.commitment_id} is now {c.status} - the {w.d(c.due_date)} commitment by {c.counterparty_id} was {word}."]
            fi = int(w.rng.integers(len(frames)))
            sk = w.doc("state_update", c.resolved_at, "buyer", f"Commitment status - {c.commitment_id}", ([frames[fi]], fi), [c.counterparty_id],
                       [c.commitment_id], [{"kind": "commitment_status", "key": c.commitment_id, "value": c.status, "record_id": c.commitment_id}],
                       situation="commit_status", event_id=ev_id)
        return {"doc": k, "status_doc": sk, "id": c.commitment_id, "status": c.status}

    # ---- per event kind
    def episode(self, ev_id):
        w, e, m = self.w, self.ev.loc[ev_id], self.meta["events"][ev_id]
        ep = self.eps[m["episodes"][0]]
        keys = []
        first = None
        for po_id in ep["slipped_po_ids"][:2]:
            f = self.po_docs(po_id, ev_id=ev_id)
            if f and (first is None or f[1] < first[1]):
                first = f + (po_id,)
        if ep.get("scrap_line_id"):
            ln = self.pol.set_index("rm_purchase_order_line_id").loc[ep["scrap_line_id"]]
            sc = self.mv[(self.mv.movement_type == "scrap") & (self.mv.rm_purchase_order_line_id == ep["scrap_line_id"])]
            if len(sc):
                s = sc.iloc[-1]
                uom = w.rm.at[ep["rm_id"], "uom"]
                fr = [f"Incoming QC at {w.pl(ep['plant_id'])} rejected {w.q(-s.quantity_change, uom)} of {w.rmn(ep['rm_id'])} from line {ep['scrap_line_id']} "
                      f"(PO {ln.rm_po_id}). Lot quarantined and written off.",
                      f"QC reject: {ep['scrap_line_id']} ({w.rmn(ep['rm_id'], False)}) failed inspection at {w.pl(ep['plant_id'], False)}; {w.q(-s.quantity_change, uom)} scrapped.",
                      f"Lot on {ep['scrap_line_id']} out of spec - {w.q(-s.quantity_change, uom)} of {w.rmn(ep['rm_id'])} scrapped at {w.pl(ep['plant_id'])}."]
                fi = int(w.rng.integers(len(fr)))
                w.doc("po_exception_note", s.movement_at, "quality_engineer", f"QC reject - {ep['scrap_line_id']}", ([fr[fi]], fi),
                      [ep["rm_id"], ep["plant_id"]], [s.rm_movement_id, ep["scrap_line_id"]],
                      [{"kind": "qc_reject", "key": ep["scrap_line_id"], "value": f"{-s.quantity_change:.4f}", "record_id": s.rm_movement_id}],
                      region=w.plants.at[ep["plant_id"], "region"], situation="qc", event_id=ev_id)
        t_first = self.sim0 + pd.Timedelta(days=ep["t_first"])
        R = self.sim0 + pd.Timedelta(days=ep["R"])
        runs = ep["blocked_runs"][:3]
        prods = [self.runs.at[r_, "product_id"] for r_ in runs]
        fr = [f"{w.rmn(ep['rm_id'])} at {w.pl(ep['plant_id'])} is below safety stock and cannot cover the next runs. On hold: {', '.join(runs)} "
              f"({', '.join(prods)}). Waiting on {first[3] if first else 'the inbound lot'}.",
              f"Short on {w.rmn(ep['rm_id'], False)} at {w.pl(ep['plant_id'], False)} from today. Runs {', '.join(runs)} parked until material arrives.",
              f"Heads-up: {ep['plant_id']} has run out of usable {w.rmn(ep['rm_id'])}. Production runs {', '.join(runs)} for {', '.join(prods)} are held.",
              f"Stockout of {w.rmn(ep['rm_id'])} at {w.pl(ep['plant_id'])}. {len(ep['blocked_runs'])} runs affected, first ones {', '.join(runs)}."]
        fi = int(w.rng.integers(len(fr)))
        so = w.doc("shift_handover", t_first, "supply_planner", f"Planner handover - {ep['plant_id']} - {ep['rm_id']} short", ([fr[fi]], fi),
                   [ep["rm_id"], ep["plant_id"]] + prods, runs + [ev_id],
                   [{"kind": "stockout", "key": f"{ep['rm_id']}|{ep['plant_id']}", "value": str(t_first.date()), "record_id": ev_id}],
                   region=w.plants.at[ep["plant_id"], "region"], situation="stockout", event_id=ev_id)
        if e.severity >= 3:
            fgl = ep["fg_lines"][:4]
            whs = sorted({self.FGP.at[self.FGL.at[l, "purchase_order_id"], "warehouse_id"] for l in ep["fg_lines"]})
            fr = [f"{w.rmn(ep['rm_id'])} shortage at {w.pl(ep['plant_id'])} is holding {len(ep['blocked_runs'])} production runs. FG lines at risk include "
                  f"{', '.join(fgl)} for {', '.join(whs)}. Root cause: {e.root_cause_text}.",
                  f"Severity {e.severity} issue: {len(ep['blocked_runs'])} runs blocked at {ep['plant_id']} for lack of {ep['rm_id']}; DCs affected {', '.join(whs)}. "
                  f"Cause: {e.root_cause_text}.",
                  f"We are short of {w.rmn(ep['rm_id'], False)} at {w.pl(ep['plant_id'])}. Late FG receipts expected on {', '.join(fgl)}. {e.root_cause_text.capitalize()}."]
            fi = int(w.rng.integers(len(fr)))
            w.doc("escalation_email", t_first + pd.Timedelta(days=1), "plant_scheduler", f"Escalation - {ev_id} - {ep['rm_id']} at {ep['plant_id']}",
                  ([fr[fi]], fi), [ep["rm_id"], ep["plant_id"]] + whs, [ev_id] + fgl, [], situation="rm", event_id=ev_id)
        # FG consequence at the DC
        l0 = ep["fg_lines"][0]
        po0 = self.FGL.at[l0, "purchase_order_id"]
        fg = self.FGP.loc[po0]
        late = int((fg.received_at - fg.expected_at).days)
        fr = [f"{l0} (PO {po0}) received at {w.whn(fg.warehouse_id)} {late} days late. Plant {ep['plant_id']} held the run for {ep['rm_id']}.",
              f"Late receipt: {l0} landed {w.d(fg.received_at)} vs expected {w.d(fg.expected_at)} at {fg.warehouse_id}. Source plant {w.pl(ep['plant_id'])} "
              f"was short on {w.rmn(ep['rm_id'], False)}.",
              f"Received {l0} today, {late} days behind plan (expected {w.d(fg.expected_at)}). Upstream reason per plant: {ep['rm_id']} stockout."]
        fi = int(w.rng.integers(len(fr)))
        wd = w.doc("warehouse_ops_note", fg.received_at, "warehouse_ops_lead", f"DC receiving - {fg.warehouse_id} - {l0}", ([fr[fi]], fi),
                   [fg.warehouse_id, ep["plant_id"], ep["rm_id"]], [l0, po0],
                   [{"kind": "fg_received", "key": l0, "value": str(fg.received_at.date()), "record_id": po0}],
                   region=w.wh.at[fg.warehouse_id, "region"], situation="fg_late", event_id=ev_id)
        ch = {"event_id": ev_id, "rm_id": ep["rm_id"], "plant_id": ep["plant_id"], "supplier_id": e.origin_entity_id,
              "stockout_doc": so, "stockout_date": str(t_first.date()), "wh_doc": wd, "fg_line": l0, "warehouse_id": fg.warehouse_id,
              "fg_received": str(fg.received_at.date()), "reason": e.root_cause_code}
        if first:
            ch.update({"first_notice_doc": first[0], "first_notice_date": first[1], "rm_po_id": first[3]})
        for did in m.get("decisions", [])[:2]:
            memo, cdocs, pm = self.decision_docs(did, ev_id)
            if "memo_doc" not in ch:
                d = self.dec.loc[did]
                ch.update({"memo_doc": memo, "pm_doc": pm, "decision_id": did, "decision_type": d.decision_type, "outcome": d.outcome_label})
            for c in cdocs:
                if c and "commit_doc" not in ch and c["status"] != "open":
                    ch.update({"commit_doc": c["doc"], "commit_id": c["id"], "commit_status": c["status"], "commit_status_doc": c["status_doc"]})
        return ch

    def generic(self, ev_id):
        """Shock / pattern / routine / FG / quality / price / sourcing / demand events."""
        w, e, m = self.w, self.ev.loc[ev_id], self.meta["events"][ev_id]
        kind = m.get("kind")
        anchors = json.loads(e.anchor_ids)
        ch = {"event_id": ev_id, "supplier_id": e.origin_entity_id}
        if kind in ("routine", "q4", "alloc", "creep", "shock"):
            pos = [a for a in anchors if a.startswith("RMPO")][:2 if kind != "routine" else 1]
            for po_id in pos:
                f = self.po_docs(po_id, ev_id=ev_id)
                if f and "first_notice_doc" not in ch:
                    ch.update({"first_notice_doc": f[0], "first_notice_date": f[1], "rm_po_id": po_id})
        if kind in ("shock", "q4", "alloc", "creep", "fg_supplier", "fg_country", "quality") or e.severity >= 3:
            imp = self.ctx.read("event_impacts")
            imp = imp[imp.event_id == ev_id].head(4)
            il = "; ".join(f"{r.entity_id} {r.impact_metric.replace('_', ' ')} {r.impact_value:g} {r.unit}" for r in imp.itertuples())
            fr = [f"{e.title}. {e.root_cause_text.capitalize()}. Impact so far: {il}.", f"Raising '{e.title}' (severity {e.severity}). Cause: {e.root_cause_text}. {il}.",
                  f"{e.title} - started around {w.d(e.start_date)}. {il}. Root cause per our read: {e.root_cause_text}."]
            fi = int(w.rng.integers(len(fr)))
            role = {"shock": "logistics_coordinator", "quality": "quality_engineer", "fg_supplier": "buyer"}.get(kind, "category_manager")
            dt = "supplier_call_summary" if kind in ("q4", "creep") else "escalation_email"
            w.doc(dt, e.detected_at, role, f"{e.event_type.replace('_', ' ').title()} - {ev_id}", ([fr[fi]], fi),
                  [e.origin_entity_id], [ev_id] + anchors[:3], [], situation=kind or "generic", event_id=ev_id)
        if kind == "quality":
            for ln_id in m.get("lines", [])[:2]:
                sc = self.mv[(self.mv.movement_type == "scrap") & (self.mv.rm_purchase_order_line_id == ln_id)]
                if not len(sc):
                    continue
                s = sc.iloc[0]
                uom = w.rm.at[s.rm_id, "uom"]
                fr = [f"Lot from {w.sup(e.origin_entity_id)} on line {ln_id} ({w.rmn(s.rm_id)}) rejected at incoming QC at {w.pl(s.plant_id)}: {w.q(-s.quantity_change, uom)} scrapped.",
                      f"QC reject on {ln_id}: {w.q(-s.quantity_change, uom)} of {w.rmn(s.rm_id, False)} from {w.sup(e.origin_entity_id, False)} out of spec.",
                      f"{w.pl(s.plant_id)} QC failed {ln_id} ({w.rmn(s.rm_id)}); {w.q(-s.quantity_change, uom)} written off. Supplier {w.sup(e.origin_entity_id)} notified."]
                fi = int(w.rng.integers(len(fr)))
                w.doc("po_exception_note", s.movement_at, "quality_engineer", f"QC reject - {ln_id}", ([fr[fi]], fi), [e.origin_entity_id, s.rm_id, s.plant_id],
                      [s.rm_movement_id, ln_id], [{"kind": "qc_reject", "key": ln_id, "value": f"{-s.quantity_change:.4f}", "record_id": s.rm_movement_id}],
                      situation="qc", event_id=ev_id)
        if kind in ("spike", "jan_resin"):
            rows = self.cat[self.cat.catalog_line_id.isin(anchors)]
            prev = None
            for r in rows.sort_values("price_valid_from").head(3).itertuples():
                fr = [f"Price for {w.rmn(r.rm_id)} from {w.sup(r.supplier_id)} is {r.unit_price:.4f} per {w.rm.at[r.rm_id, 'uom']} effective {w.d(r.price_valid_from)}.",
                      f"{w.sup(r.supplier_id)} price on {r.rm_id}: {r.unit_price:.4f}/{w.rm.at[r.rm_id, 'uom']} from {w.d(r.price_valid_from)} (contract {r.contract_id}).",
                      f"New unit price {r.unit_price:.4f} for {w.rmn(r.rm_id, False)} ({r.supplier_id}), valid from {w.d(r.price_valid_from)}."]
                fi = int(w.rng.integers(len(fr)))
                w.doc("state_update", min(r.price_valid_from, D(e.detected_at) if prev is None else r.price_valid_from), "category_manager", f"Price change - {r.rm_id} - {r.supplier_id}",
                      ([fr[fi]], fi), [r.rm_id, r.supplier_id], [r.catalog_line_id],
                      [{"kind": "price", "key": f"{r.rm_id}|{r.supplier_id}", "value": f"{r.unit_price:.4f}", "record_id": r.catalog_line_id}], situation="price", event_id=ev_id)
                prev = r
        if kind == "switch":
            rows = self.cat[self.cat.catalog_line_id.isin(anchors) & (self.cat.sourcing_rank == "primary")]
            if len(rows):
                r = rows.iloc[0]
                fr = [f"Primary supplier for {w.rmn(r.rm_id)} changed to {w.sup(r.supplier_id)} from {w.d(r.price_valid_from)}; {w.sup(m['old_primary'])} moves to secondary.",
                      f"Sourcing change: {r.rm_id} primary is now {r.supplier_id} (allocation {r.allocation_pct:g}%) effective {w.d(r.price_valid_from)}.",
                      f"From {w.d(r.price_valid_from)} {w.sup(r.supplier_id)} is primary on {w.rmn(r.rm_id, False)}; {m['old_primary']} is secondary."]
                fi = int(w.rng.integers(len(fr)))
                w.doc("state_update", r.price_valid_from, "category_manager", f"Primary supplier changed - {r.rm_id}", ([fr[fi]], fi), [r.rm_id, r.supplier_id, m["old_primary"]],
                      [r.catalog_line_id], [{"kind": "primary", "key": r.rm_id, "value": r.supplier_id, "record_id": r.catalog_line_id}], situation="primary", event_id=ev_id)
        if kind in ("fg_stockout", "fg_adjust"):
            fr = [f"{e.title}. {e.root_cause_text.capitalize()}.", f"Floor note: {e.title.lower()}; {e.root_cause_text}.", f"{e.title} - {e.root_cause_text}."]
            fi = int(w.rng.integers(len(fr)))
            w.doc("warehouse_ops_note", e.detected_at, "warehouse_ops_lead", f"Ops - {ev_id}", ([fr[fi]], fi), [e.origin_entity_id], [ev_id] + anchors[:3], [],
                  situation=kind, event_id=ev_id)
        if kind == "demand":
            fr = [f"{e.title}: {e.root_cause_text}. Unfulfilled demand logged against {ev_id}.", f"Spike review - {e.origin_entity_id}: {e.root_cause_text}.",
                  f"Demand planning flagged {e.origin_entity_id}; {e.root_cause_text}. Tracked as {ev_id}."]
            fi = int(w.rng.integers(len(fr)))
            w.doc("forecast_review_minutes", e.detected_at + pd.Timedelta(days=3), "demand_planner", f"Demand review - {e.origin_entity_id}", ([fr[fi]], fi),
                  [e.origin_entity_id], [ev_id] + anchors[:2], [], situation="demand", event_id=ev_id)
        for did in m.get("decisions", [])[:2]:
            memo, cdocs, pm = self.decision_docs(did, ev_id)
            if "memo_doc" not in ch:
                d = self.dec.loc[did]
                ch.update({"memo_doc": memo, "pm_doc": pm, "decision_id": did, "decision_type": d.decision_type, "outcome": d.outcome_label,
                           "rm_id": m.get("rm_id"), "plant_id": m.get("plant_id")})
        return ch

    def negotiation(self, nid):
        w, n = self.w, self.neg.loc[nid]
        fr = [f"Opened talks with {w.sup(n.supplier_id)} on {n.topic.replace('_', ' ')}. Our ask: {n.our_ask}. Their position: {n.their_offer}.",
              f"{n.topic.replace('_', ' ').capitalize()} discussion with {w.sup(n.supplier_id)}{' on ' + n.rm_id if pd.notna(n.rm_id) else ''}. We asked for {n.our_ask}; they offered {n.their_offer}.",
              f"Call with {w.sup(n.supplier_id, False)} ({n.supplier_id}) - {n.topic.replace('_', ' ')}. Ask: {n.our_ask}. Offer: {n.their_offer}."]
        fi = int(w.rng.integers(len(fr)))
        w.doc("supplier_call_summary", n.started_at, "category_manager", f"Negotiation - {nid} - {n.supplier_id}", ([fr[fi]], fi), [n.supplier_id, n.rm_id],
              [nid] + ([n.contract_id] if pd.notna(n.contract_id) else []), [], situation="negotiation")
        if pd.notna(n.concluded_at):
            fr = [f"Negotiation {nid} with {w.sup(n.supplier_id)} concluded: {n.outcome}. Final terms: {n.final_terms}.",
                  f"{nid} closed ({n.outcome}). Terms agreed with {n.supplier_id}: {n.final_terms}.",
                  f"Wrapped up {n.topic.replace('_', ' ')} with {w.sup(n.supplier_id, False)} - outcome {n.outcome}; {n.final_terms}."]
            fi = int(w.rng.integers(len(fr)))
            w.doc("state_update", n.concluded_at, "category_manager", f"Negotiation concluded - {nid}", ([fr[fi]], fi), [n.supplier_id], [nid],
                  [{"kind": "negotiation_terms", "key": nid, "value": n.final_terms, "record_id": nid}], situation="negotiation_done")
            for c in self.cm[self.cm.negotiation_id == nid].head(2).itertuples():
                self.commit_docs(c, None)

    def qbr(self, sup, q):
        w = self.w
        sc = self.ctx.read("supplier_scorecard_monthly")
        x = sc[(sc.supplier_id == sup) & (pd.PeriodIndex(sc.month, freq="M").quarter == q.quarter) & (pd.PeriodIndex(sc.month, freq="M").year == q.year)]
        if not len(x) or x.otif_rate.isna().all():
            return
        m = x.dropna(subset=["otif_rate"]).iloc[-1]
        fr = [f"{w.sup(sup)} review for {q}. {m.month}: OTIF {m.otif_rate:.1%}, avg delay {m.avg_delay_days:.1f} days, fill {m.fill_rate:.1%}, quality {m.quality_ppm:,.0f} ppm. "
              f"Commitments kept {int(x.commitments_kept.sum())} of {int(x.commitments_made.sum())} this quarter.",
              f"QBR with {w.sup(sup, False)} ({sup}), {q}. Latest month {m.month} OTIF {m.otif_rate:.1%}; average delay {m.avg_delay_days:.1f}d; fill rate {m.fill_rate:.1%}; "
              f"{m.quality_ppm:,.0f} ppm rejects.",
              f"Scorecard {q} for {w.sup(sup)}: in {m.month} they hit {m.otif_rate:.1%} OTIF with {m.avg_delay_days:.1f} days average delay and {m.quality_ppm:,.0f} ppm."]
        fi = int(w.rng.integers(len(fr)))
        w.doc("supplier_qbr_notes", q.end_time.normalize() + pd.Timedelta(days=10), "category_manager", f"QBR - {sup} - {q}", ([fr[fi]], fi), [sup], [sup],
              [{"kind": "scorecard", "key": f"{sup}|{m.month}", "value": f"{m.otif_rate:.1%}", "record_id": sup}], situation="qbr")


def noise_doc(w: Writer, date):
    kinds = [("shift_handover", "supply_planner", ["Quiet shift. No new supplier notices; all inbound lots on the board are on schedule.",
                                                    "Nothing unusual tonight. MRP run completed; exceptions list reviewed, no action.",
                                                    "Routine shift. Printer at the planning desk fixed; no open items."]),
             ("warehouse_ops_note", "warehouse_ops_lead", ["Dock door 4 back in service after maintenance.", "Forklift refresher training completed for second shift.",
                                                          "Racking inspection passed; no findings.", "Housekeeping audit done; aisle labels replaced."]),
             ("forecast_review_minutes", "demand_planner", ["Routine monthly review. No overrides proposed; consensus unchanged.",
                                                           "Attendance short due to holidays; review deferred to next cycle.",
                                                           "Walked through the dashboard refresh; no forecast changes."]),
             ("supplier_call_summary", "buyer", ["Courtesy call with a supplier account team; no changes to open orders.",
                                                 "Supplier introduced a new account manager; contact list updated.",
                                                 "Checked invoice formatting with a supplier's AR team; no operational impact."])]
    dt, role, bodies = kinds[int(w.rng.integers(len(kinds)))]
    p = w.plants.index[int(w.rng.integers(len(w.plants)))]
    fi = int(w.rng.integers(len(bodies)))
    b = bodies[fi] + f" Site: {w.pl(p)}."
    w.doc(dt, date, role, f"{dt.replace('_', ' ').title()} - routine", ([b], 10 + fi), [p], [p], noise=True, situation="noise",
          region=w.plants.at[p, "region"])


# ------------------------------------------------------------------ fact checking (also used by validate.py)
def check_facts(ctx: Ctx, f: pd.DataFrame) -> pd.Series:
    r = ctx.read
    po, rev = r("rm_purchase_orders").set_index("rm_po_id"), r("rm_po_revisions")
    revset = set(zip(rev.rm_po_id, rev.new_expected_at.dt.strftime("%Y-%m-%d")))
    rsn = set(zip(rev.rm_po_id, rev.reason_code))
    cat = r("rm_supplier_catalog").set_index("catalog_line_id")
    dec, cm = r("decisions").set_index("decision_id"), r("commitments").set_index("commitment_id")
    neg = r("negotiations").set_index("negotiation_id")
    mv = r("rm_inventory_movements").set_index("rm_movement_id")
    src = ctx.src()
    fg = src["purchase_orders"].set_index("purchase_order_id")
    ok = []
    for x in f.itertuples():
        d = x.ts[:10]
        if x.kind == "po_eta":
            v = (x.key, x.value) in revset
        elif x.kind == "po_reason":
            v = (x.key, x.value) in rsn
        elif x.kind == "price":
            v = abs(cat.at[x.record_id, "unit_price"] - float(x.value)) < 1e-3
        elif x.kind == "primary":
            v = cat.at[x.record_id, "sourcing_rank"] == "primary" and cat.at[x.record_id, "supplier_id"] == x.value
        elif x.kind == "decision":
            v = dec.at[x.record_id, "decision_type"] == x.value
        elif x.kind == "lesson":
            v = dec.at[x.record_id, "outcome_label"] == x.value and str(dec.at[x.record_id, "outcome_assessed_at"].date()) <= d
        elif x.kind == "commitment_status":
            c = cm.loc[x.key]
            v = (pd.isna(c.resolved_at) or str(c.resolved_at.date()) >= d) if x.value == "open" else (c.status == x.value and str(c.resolved_at.date()) <= d)
        elif x.kind == "commitment_made":
            v = str(cm.at[x.key, "made_at"].date()) <= d
        elif x.kind == "qc_reject":
            v = abs(-mv.at[x.record_id, "quantity_change"] - float(x.value)) < 1e-3
        elif x.kind == "fg_received":
            v = str(fg.at[x.record_id, "received_at"].date()) == x.value
        elif x.kind == "negotiation_terms":
            v = neg.at[x.key, "final_terms"] == x.value
        else:
            v = True
        ok.append(bool(v))
    return pd.Series(ok, index=f.index)


def llm_rewrite(ctx: Ctx, docs: list[dict]) -> None:
    """Optional: paraphrase content through an OpenAI-compatible endpoint, keeping every ID/number verbatim. Cached per doc hash."""
    import re
    import httpx
    c = ctx.cfg["narratives"]["llm"]
    base, key, model = os.environ.get(c["base_url_env"]), os.environ.get(c["api_key_env"]), os.environ.get(c["model_env"])
    if not (base and key and model):
        print("  --llm-narratives: LLM_BASE_URL / LLM_API_KEY / LLM_MODEL not set; keeping template text")
        return
    cache = ctx.output / c["cache_dir"]
    cache.mkdir(parents=True, exist_ok=True)
    tok = re.compile(r"[A-Z]{1,6}-?\d{2,}|\d[\d,.]*")
    for d in docs:
        h = hashlib.sha256(d["content"].encode()).hexdigest()[:24]
        p = cache / f"{h}.txt"
        if not p.exists():
            msg = ("Rewrite this internal supply-chain note in a natural organisational voice. Keep every ID, date and number exactly; "
                   "60-250 words; no new facts.\n\n" + d["content"])
            rsp = httpx.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=120,
                             json={"model": model, "messages": [{"role": "user", "content": msg}], "temperature": 0.7})
            rsp.raise_for_status()
            p.write_text(rsp.json()["choices"][0]["message"]["content"].strip(), encoding="utf-8")
        new = p.read_text(encoding="utf-8")
        if set(tok.findall(d["content"])) <= set(tok.findall(new)) and 60 <= len(new.split()) <= 250:
            d["content"] = new


def run(ctx: Ctx, llm: bool = False):
    banner("STAGE 4a - narrative corpus")
    from gen_eval import select_holdout_scenarios
    w = Writer(ctx)
    C = Chains(ctx, w)
    target = int(ctx.sc["target_docs"])
    nf = ctx.cfg["narratives"]["noise_frac"]
    signal_budget = int(target * (1 - nf))
    ev, meta = C.ev, C.meta["events"]
    scen = select_holdout_scenarios(ctx)
    must = []
    for s in scen:
        must += [s["event_id"]] + s["gold_precedents"] + [s["surface_precedent"]]
    rng = w.rng
    pri = []
    for eid, e in ev.iterrows():
        m = meta[eid]
        s = rng.random() + (100 if eid in must else 0) + (5 if m.get("pattern") else 0) + (3 if m.get("decisions") else 0) + e.severity * 0.3
        if m.get("kind") in ("fg_adjust",):
            s -= 2
        pri.append((s, eid))
    pri.sort(reverse=True)
    negs = list(C.neg.index)
    rng.shuffle(negs)
    n_neg = max(3, len(negs) if ctx.scale == "full" else len(negs) // 3)
    for nid in negs[:n_neg]:
        C.negotiation(nid)
    pats = ctx.load_json("pattern_plan.json")
    for sup in [pats["q4_slip_supplier"], pats["small_po_supplier"], pats["lead_time_creep_supplier"], pats["secondary_quality_supplier"]][: 4 if ctx.scale == "full" else 2]:
        for q in pd.period_range("2023Q1", "2025Q3", freq="Q"):
            C.qbr(sup, q)
    for _, eid in pri:
        if len(w.docs) >= signal_budget:
            break
        ch = C.episode(eid) if meta[eid].get("kind") == "episode" else C.generic(eid)
        C.chains.append(ch)
    # noise fill
    n_noise = int(round(len(w.docs) * nf / (1 - nf)))
    days = pd.bdate_range(ctx.cfg["window_start"], ctx.cfg["window_end"])
    for dte in days[rng.integers(0, len(days), n_noise)]:
        noise_doc(w, dte)

    # ids by time, supersession, chains
    docs = sorted(w.docs, key=lambda x: (x["timestamp"][:19], x["_k"]))
    kid = {d["_k"]: f"DOC{i + 1:06d}" for i, d in enumerate(docs)}
    for d in docs:
        d["doc_id"] = kid[d["_k"]]
    facts = pd.DataFrame(w.facts)
    facts["doc_id"] = facts._k.map(kid)
    by = {d["doc_id"]: d for d in docs}
    for kind in ("po_eta", "price", "primary", "commitment_status"):
        f = facts[facts.kind == kind].sort_values(["ts", "doc_id"])
        for key, g in f.groupby("key" if kind != "primary" else "key"):
            prev = []
            for x in g.itertuples():
                if prev and any(p[1] != x.value and p[2] < x.ts[:19] for p in prev):
                    by[x.doc_id]["supersedes_doc_ids"] += [p[0] for p in prev if p[1] != x.value and p[2] < x.ts[:19]]
                prev.append((x.doc_id, x.value, x.ts[:19]))
    for d in docs:
        d["supersedes_doc_ids"] = sorted(set(d["supersedes_doc_ids"]))
    if llm:
        llm_rewrite(ctx, [d for d in docs if not d["is_noise"]])
    with open(ctx.output / "memory_corpus.jsonl", "w", encoding="utf-8") as fh:
        for d in docs:
            out = {k: d[k] for k in ("doc_id", "timestamp", "context", "content", "doc_type", "entity_ids", "source_record_ids", "split", "is_noise",
                                     "supersedes_doc_ids", "author_role", "event_id")}
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")
    facts["consistent"] = check_facts(ctx, facts)
    facts.drop(columns=["_k"]).to_parquet(ctx.work / "doc_facts.parquet", index=False)
    chains = []
    for c in C.chains:
        c = {k: (kid[v] if k.endswith("_doc") and v is not None and not isinstance(v, str) else v) for k, v in c.items()}
        ids = [d["doc_id"] for d in docs if d["event_id"] == c["event_id"]]
        if ids:
            ts = [by[i]["timestamp"][:10] for i in ids]
            c.update({"doc_ids": ids, "roles": sorted({by[i]["author_role"] for i in ids}), "span_days": (D(max(ts)) - D(min(ts))).days})
            chains.append(c)
    ctx.save_json(chains, "chains.json")
    stats = {t: len(v["open"]) * len(v["close"]) * max(3, len({b for s, b in w.body_variants[t]})) for t, v in T.items()}
    ctx.save_json(stats, "template_stats.json")
    df = pd.DataFrame(docs)
    print(f"docs {len(df):,} ({df.split.value_counts().to_dict()}) noise {df.is_noise.mean():.1%} supersede {(df.supersedes_doc_ids.str.len() > 0).mean():.1%}")
    print(f"types {df.doc_type.value_counts().to_dict()}")
    print(f"facts {len(facts):,} consistent {facts.consistent.mean():.1%} | chains {len(chains)} | words {df.content.str.split().str.len().describe()[['min', 'mean', 'max']].to_dict()}")


if __name__ == "__main__":
    run(cli(__doc__), llm="--llm-narratives" in sys.argv)
