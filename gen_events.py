"""Stage 3 - institutional memory ground truth.

disruption_events / event_impacts / event_links / negotiations / decisions / commitments /
supplier_scorecard_monthly + planted_patterns.json.

Events are anchored first: FG-side events are mined from the existing tables (supplier-quarter
clusters, stock <= 0 episodes, country-month delay clusters, adjustment clusters, sales spikes);
RM-side events come from the Stage 1 simulation (cascade episodes, country shocks, planted patterns).
Decision outcomes were computed on simulated trajectories in Stage 1 (RM side) or from existing
movements (FG side); commitment status is derived from what later happened in the tables.
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy import stats

from common import Ctx, banner, cli, fmt_ids, week_start
from profile import stock_trajectory

D = pd.Timestamp
DAY = pd.Timedelta(days=1)
SHOCK_TYPES = {"port_congestion", "customs_backlog", "labor_strike", "severe_weather", "rail_disruption"}
ROOT_TEXT = {
    "port_congestion": "vessel queueing and terminal congestion at the origin port",
    "customs_backlog": "customs inspection backlog at the border crossing",
    "labor_strike": "industrial action at the supplier's logistics provider",
    "severe_weather": "severe weather closing roads and the origin rail yard",
    "rail_disruption": "rail network disruption on the inbound lane",
    "peak_season_capacity": "supplier line capacity fully booked for year-end peak demand",
    "allocation_cut": "supplier placed the material on allocation after its price increase",
    "lead_time_extension": "supplier lead times creeping up after its capacity consolidation",
    "supplier_capacity": "supplier production capacity shortfall",
    "production_issue": "unplanned production issue at the supplier plant",
    "carrier_delay": "carrier missed the planned pickup",
    "raw_material_shortage": "shortage of the supplier's own upstream feedstock",
    "quality_hold_at_supplier": "supplier quality hold on the finished lot before release",
}


def dstr(x) -> str:
    return pd.Timestamp(x).strftime("%Y-%m-%d")


class Builder:
    def __init__(self, ctx: Ctx):
        self.ctx, self.cfg = ctx, ctx.cfg
        self.rng = ctx.rng("events")
        self.src = ctx.src()
        self.plan = ctx.load_json("rm_ops_plan.json")
        self.pat = ctx.load_json("pattern_plan.json")
        self.sim0 = D(self.plan["sim_start"])
        self.end = D(self.cfg["window_end"])
        r = ctx.read
        self.rm = r("raw_materials").set_index("rm_id")
        self.plants = r("plants").set_index("plant_id")
        self.cat = r("rm_supplier_catalog")
        self.cat_meta = ctx.read("catalog_meta")
        self.cat = self.cat.merge(self.cat_meta, on="catalog_line_id")
        self.contracts = r("contracts")
        self.po, self.pol, self.rev = r("rm_purchase_orders"), r("rm_purchase_order_lines"), r("rm_po_revisions")
        self.mv, self.runs = r("rm_inventory_movements"), r("production_runs")
        self.snaps = r("rm_inventory_snapshots_weekly")
        self.meta = ctx.read("rm_order_meta")
        self.meta_ix = self.meta.set_index(["pi", "lid"])
        S = self.src["suppliers"].set_index("supplier_id")
        self.sname, self.scountry, self.srel = S.supplier_name.to_dict(), S.country_code.to_dict(), S.reliability_score.to_dict()
        self.events, self.decisions, self.commitments, self.negotiations = [], [], [], []
        self.links = []
        self.po_ix = self.po.set_index("rm_po_id")
        self.line_ix = self.pol.set_index("rm_purchase_order_line_id")
        self.rev_by_po = {k: g for k, g in self.rev.groupby("rm_po_id")}
        self.pext = r("products_ext").set_index("product_id")
        self.PO = self.src["purchase_orders"].set_index("purchase_order_id")
        self.POL = self.src["purchase_order_lines"].set_index("purchase_order_line_id")

    # ------------------------------------------------------------ helpers
    def day(self, i):
        return self.sim0 + pd.Timedelta(days=int(i))

    def sup(self, s):
        return f"{self.sname.get(s, s)} ({s})"

    def rmn(self, r):
        return f"{self.rm.at[r, 'rm_name']} ({r})"

    def add_event(self, **k):
        k.setdefault("impacts", [])
        k.setdefault("meta", {})
        k["key"] = k.get("key") or f"E{len(self.events)}"
        k["detected"] = max(k["detected"], k["start"])
        k["end"] = max(k["end"], k["start"])
        self.events.append(k)
        return k

    def severity(self, x, cuts):
        return int(1 + sum(x >= c for c in cuts))

    def contract_at(self, sup, d):
        c = self.contracts[(self.contracts.supplier_id == sup) & (self.contracts.valid_from <= d) & (self.contracts.valid_to >= d)]
        return c.iloc[0] if len(c) else None

    # ------------------------------------------------------------ RM-side events
    def rm_shortage_events(self):
        eps = sorted(self.plan["episodes"], key=lambda e: e["t_first"])
        groups, open_g = [], {}
        for ep in eps:
            root = ep.get("shock_id") or (ep["reason"] if ep["reason"] in ("peak_season_capacity", "allocation_cut", "lead_time_extension") else None)
            k = (ep["rm_id"], ep["primary_supplier"], root)
            g = open_g.get(k)
            if g is not None and ep["t_first"] - g[0]["t_first"] <= 21:
                g.append(ep)
            else:
                g = [ep]
                open_g[k] = g
                groups.append(g)
        runs = self.runs.set_index("production_run_id")
        self.episode_event = {}
        for g in groups:
            rm_, sup = g[0]["rm_id"], g[0]["primary_supplier"]
            det = min(e["detected_day"] for e in g)
            t_first, R = min(e["t_first"] for e in g), max(e["R"] for e in g)
            nblk = sum(len(e["blocked_runs"]) for e in g)
            pl = sorted({e["plant_id"] for e in g})
            reason = g[0]["reason"]
            anchors = sorted({p for e in g for p in e["slipped_po_ids"]}) + sorted({l for e in g for l in e["fg_lines"]})
            imp = []
            for e in g:
                imp += [("plant", e["plant_id"], "blocked_production_runs", len(e["blocked_runs"]), "runs"),
                        ("raw_material", rm_, "stockout_days", e["R"] - e["t_first"], "days")]
                for rid in e["blocked_runs"][:6]:
                    rn = runs.loc[rid]
                    imp.append(("purchase_order_line", rn.purchase_order_line_id, "receipt_delay_days", int(rn.delay_days), "days"))
            fgw = pd.Series([self.POL.at[l, "purchase_order_id"] for e in g for l in e["fg_lines"]]).map(self.PO.warehouse_id).value_counts()
            for w, n in fgw.items():
                imp.append(("warehouse", w, "late_fg_receipts", int(n), "lines"))
            ev = self.add_event(event_type="rm_shortage",
                                title=f"{self.rm.at[rm_, 'rm_name']} shortage at {', '.join(self.plants.loc[pl, 'plant_name'])} blocks production",
                                start=self.day(det) - pd.Timedelta(days=int(self.rng.integers(0, 4))), end=self.day(R), detected=self.day(det),
                                severity=self.severity(nblk + (R - t_first) / 3, [3, 6, 10, 18]),
                                root_cause_code=reason,
                                root_cause_text=f"{ROOT_TEXT.get(reason, reason)} at {self.sup(sup)} delayed {self.rmn(rm_)}; "
                                                f"plant stock ran out before the replacement lot arrived",
                                origin_type="supplier", origin_id=sup, anchor_type="rm_side", anchor_ids=anchors, impacts=imp,
                                meta={"kind": "episode", "episodes": [e["id"] for e in g], "rm_id": rm_, "supplier_id": sup,
                                      "plants": pl, "shock_id": g[0].get("shock_id"), "reason": reason, "t_first": t_first, "R": R,
                                      "blocked": nblk})
            for e in g:
                self.episode_event[e["id"]] = ev["key"]

    def po_cause(self, cause: str):
        m = self.meta[(self.meta.cause == cause) & self.meta.rm_po_id.notna()]
        return m

    def first_rev(self, po_ids, reason=None):
        r = self.rev[self.rev.rm_po_id.isin(po_ids)]
        if reason:
            r = r[r.reason_code == reason]
        return r.revised_at.min() if len(r) else pd.NaT

    def shock_events(self):
        self.shock_event = {}
        for sh in self.plan["shocks"]:
            s0, s1 = self.day(sh["start"]), self.day(sh["end"])
            m = self.meta[(self.meta.cause == sh["type"]) & (self.meta.supplier_id.map(self.scountry) == sh["country_code"])]
            m = m[(m.promised >= sh["start"] - 3) & (m.promised <= sh["end"] + 3)]
            pos = sorted(m.rm_po_id.dropna().unique())
            if not pos:
                continue
            det = self.first_rev(pos, sh["type"])
            det = det if pd.notna(det) else s0
            sl = (m.receipt - m.promised).groupby(m.supplier_id).mean()
            imp = [("country", sh["country_code"], "affected_rm_pos", len(pos), "POs")] + \
                  [("supplier", s, "avg_slip_days", round(float(v), 1), "days") for s, v in sl.items()]
            ev = self.add_event(event_type=sh["type"], title=f"{sh['type'].replace('_', ' ').title()} - inbound from {sh['country_code']}"
                                + (" (February peak)" if sh["planted"] else ""),
                                start=min(s0, det), end=s1, detected=det, severity=self.severity(len(pos), [3, 8, 15, 30]),
                                root_cause_code=sh["type"], root_cause_text=f"{ROOT_TEXT[sh['type']]} affecting suppliers shipping from {sh['country_code']}",
                                origin_type="country", origin_id=sh["country_code"], anchor_type="rm_side", anchor_ids=pos, impacts=imp,
                                meta={"kind": "shock", "shock_id": sh["shock_id"], "country": sh["country_code"], "planted": sh["planted"],
                                      "suppliers": sorted(sl.index)})
            self.shock_event[sh["shock_id"]] = ev["key"]

    def pattern_events(self):
        pat, pp = self.pat, self.cfg["patterns"]
        self.q4_event, self.alloc_event, self.spike_event = {}, {}, {}
        # q4 supplier peak season
        s = pat["q4_slip_supplier"]
        m = self.meta[(self.meta.cause == "peak_season_capacity") & (self.meta.supplier_id == s)]
        for y in (2023, 2024, 2025):
            my = m[self.day(0).year + 0 == 0] if False else m[[self.day(p).year == y for p in m.promised]]
            pos = sorted(my.rm_po_id.dropna().unique())
            if not pos:
                continue
            det = self.first_rev(pos) if pd.notna(self.first_rev(pos)) else D(f"{y}-11-01")
            ev = self.add_event(event_type="supplier_capacity", title=f"{self.sup(s)} year-end capacity squeeze {y}",
                                start=min(D(f"{y}-11-01"), det), end=D(f"{y}-12-31"), detected=det,
                                severity=self.severity(len(pos), [2, 5, 10, 20]), root_cause_code="peak_season_capacity",
                                root_cause_text=ROOT_TEXT["peak_season_capacity"] + f"; {self.sup(s)} pushed Nov-Dec deliveries",
                                origin_type="supplier", origin_id=s, anchor_type="rm_side", anchor_ids=pos,
                                impacts=[("supplier", s, "late_pos", len(pos), "POs"),
                                         ("supplier", s, "avg_slip_days", round(float((my.receipt - my.promised).mean()), 1), "days")],
                                meta={"kind": "q4", "supplier_id": s, "year": y, "pattern": "q4_slip_supplier"})
            self.q4_event[y] = ev["key"]
        # lead time creep
        s = pat["lead_time_creep_supplier"]
        m = self.meta[(self.meta.cause == "lead_time_extension") & (self.meta.supplier_id == s)].sort_values("ordered")
        if len(m) >= 5:
            pos = sorted(m.rm_po_id.dropna().unique())
            revs = self.rev[self.rev.rm_po_id.isin(pos)].sort_values("revised_at")
            det = revs.revised_at.iloc[min(4, len(revs) - 1)]
            ev = self.add_event(event_type="supplier_performance_decline", title=f"{self.sup(s)} lead times creeping up",
                                start=D(pp["lead_time_creep_supplier"]["from"]), end=self.end, detected=det, severity=3,
                                root_cause_code="lead_time_extension", root_cause_text=ROOT_TEXT["lead_time_extension"] + f" at {self.sup(s)}",
                                origin_type="supplier", origin_id=s, anchor_type="rm_side", anchor_ids=pos[:60],
                                impacts=[("supplier", s, "late_pos", len(pos), "POs")],
                                meta={"kind": "creep", "supplier_id": s, "pattern": "lead_time_creep_supplier"})
            self.creep_event = ev["key"]
            self.creep_detect = det
        # price spikes and allocation cuts on the single-source RM
        rm_, s = pat["price_spike_rm"], pat["price_spike_supplier"]
        c = self.cat[(self.cat.rm_id == rm_)].sort_values("price_valid_from")
        spikes = c[c._why == "price_spike"]
        prev_key = None
        for i, sp in enumerate(spikes.itertuples()):
            before = c[c.price_valid_from < sp.price_valid_from].unit_price.iloc[-1]
            pct = sp.unit_price / before - 1
            rb = c[(c._why == "negotiated_rollback") & (c.price_valid_from > sp.price_valid_from)].head(1)
            ann = sp.price_valid_from - pd.Timedelta(days=int(self.rng.integers(10, 21)))
            end = rb.price_valid_from.iat[0] if len(rb) else self.end
            ev = self.add_event(event_type="price_increase", title=f"{self.sup(s)} raises price of {self.rm.at[rm_, 'rm_name']} {pct:.0%}",
                                start=ann, end=end, detected=ann, severity=3 if pct < 0.2 else 4, root_cause_code="supplier_price_increase",
                                root_cause_text=f"single-source supplier {self.sup(s)} passed through feedstock cost increases on {self.rmn(rm_)}",
                                origin_type="raw_material", origin_id=rm_, anchor_type="rm_side",
                                anchor_ids=[sp.catalog_line_id] + (list(rb.catalog_line_id) if len(rb) else []),
                                impacts=[("raw_material", rm_, "price_change_pct", round(pct * 100, 1), "pct")],
                                meta={"kind": "spike", "rm_id": rm_, "supplier_id": s, "spike_catalog": sp.catalog_line_id,
                                      "rollback_catalog": rb.catalog_line_id.iat[0] if len(rb) else None, "pct": pct,
                                      "old_price": before, "new_price": sp.unit_price,
                                      "rollback_price": rb.unit_price.iat[0] if len(rb) else None, "effective": sp.price_valid_from,
                                      "pattern": "price_spike_shortage"})
            self.spike_event[i] = ev["key"]
            if prev_key:
                self.links.append((ev["key"], prev_key, "recurrence_of"))
            prev_key = ev["key"]
            a, b = self.plan["alloc_windows"][i] if i < len(self.plan["alloc_windows"]) else (None, None)
            if a is None:
                continue
            m = self.meta[(self.meta.cause == "allocation_cut") & (self.meta.promised.between(a - 5, b + 5))]
            pos = sorted(m.rm_po_id.dropna().unique())
            if not pos:
                continue
            det = self.first_rev(pos)
            ev2 = self.add_event(event_type="single_source_shortage", title=f"{self.rm.at[rm_, 'rm_name']} placed on allocation by {self.sup(s)}",
                                 start=min(self.day(a), det), end=self.day(b), detected=det, severity=4, root_cause_code="allocation_cut",
                                 root_cause_text=ROOT_TEXT["allocation_cut"] + f"; {self.rmn(rm_)} has no qualified second source",
                                 origin_type="raw_material", origin_id=rm_, anchor_type="rm_side", anchor_ids=pos,
                                 impacts=[("raw_material", rm_, "late_pos", len(pos), "POs"),
                                          ("raw_material", rm_, "avg_slip_days", round(float((m.receipt - m.promised).mean()), 1), "days")],
                                 meta={"kind": "alloc", "rm_id": rm_, "supplier_id": s, "pattern": "price_spike_shortage"})
            self.alloc_event[i] = ev2["key"]
            self.links.append((ev["key"], ev2["key"], "caused"))
        # January resin increases
        jr = self.cat[self.cat._why == "january_resin_increase"]
        prev = None
        for y, g in jr.groupby(jr.price_valid_from.dt.year):
            if y < 2023:
                continue
            ev = self.add_event(event_type="price_increase", title=f"Resin index increase effective 1 Jan {y}",
                                start=D(f"{y - 1}-12-01"), end=D(f"{y}-01-31"), detected=D(f"{y - 1}-12-01"), severity=2,
                                root_cause_code="index_price_increase", root_cause_text="resin contract prices indexed to monomer costs reset each January",
                                origin_type="rm_category", origin_id="resin", anchor_type="rm_side", anchor_ids=sorted(g.catalog_line_id),
                                impacts=[("raw_material", r_, "price_change_pct", 0, "pct") for r_ in sorted(g.rm_id.unique())[:8]],
                                meta={"kind": "jan_resin", "year": int(y), "pattern": "january_resin_increase"})
            if prev:
                self.links.append((ev["key"], prev, "recurrence_of"))
            prev = ev["key"]
        # fill resin impact values
        for ev in self.events:
            if ev["meta"].get("kind") == "jan_resin":
                imp = []
                for et, r_, mtr, _, u in ev["impacts"]:
                    cc = self.cat[(self.cat.rm_id == r_)].sort_values("price_valid_from")
                    cur = cc[cc._why == "january_resin_increase"]
                    cur = cur[cur.price_valid_from.dt.year == ev["meta"]["year"]]
                    if len(cur):
                        pr = cc[(cc.price_valid_from < cur.price_valid_from.iat[0]) & (cc.supplier_id == cur.supplier_id.iat[0])].unit_price
                        pct = (cur.unit_price.iat[0] / pr.iat[-1] - 1) * 100 if len(pr) else 0
                        imp.append((et, r_, mtr, round(float(pct), 1), u))
                ev["impacts"] = imp
        # q4 recurrence and shock recurrence links
        ks = [self.q4_event[y] for y in sorted(self.q4_event)]
        self.links += [(b, a, "recurrence_of") for a, b in zip(ks, ks[1:])]
        feb = [e for e in self.events if e["meta"].get("kind") == "shock" and e["meta"].get("planted")]
        self.links += [(b["key"], a["key"], "recurrence_of") for a, b in zip(feb, feb[1:])]

    def quality_events(self):
        sc = self.mv[(self.mv.movement_type == "scrap") & self.mv.rm_purchase_order_line_id.notna()]
        ep_lines = {e.get("scrap_line_id") for e in self.plan["episodes"]}
        sc = sc[~sc.rm_purchase_order_line_id.isin(ep_lines)]
        sc = sc.merge(self.pol[["rm_purchase_order_line_id", "rm_po_id"]], on="rm_purchase_order_line_id")
        sc["supplier_id"] = sc.rm_po_id.map(self.po_ix.supplier_id)
        qs = self.pat["secondary_quality_supplier"]
        others = sc[sc.supplier_id != qs]
        pick = pd.concat([sc[sc.supplier_id == qs], others.sample(n=min(12, len(others)), random_state=int(self.rng.integers(1 << 30)))])
        self.quality_by_line = {}
        for s, g in pick.sort_values("movement_at").groupby("supplier_id"):
            clusters, cur = [], []
            for r in g.itertuples():
                if cur and (r.movement_at - cur[0].movement_at).days > 60:
                    clusters.append(cur)
                    cur = []
                cur.append(r)
            if cur:
                clusters.append(cur)
            for cl in clusters:
                q = sum(-x.quantity_change for x in cl)
                rms = sorted({x.rm_id for x in cl})
                ev = self.add_event(event_type="quality_rejection", title=f"Incoming QC rejects on {self.sup(s)} lots ({', '.join(rms)})",
                                    start=cl[0].movement_at, end=cl[-1].movement_at, detected=cl[0].movement_at,
                                    severity=2 if s != qs else 3, root_cause_code="incoming_qc_reject",
                                    root_cause_text=f"lots from {self.sup(s)} failed incoming inspection (out-of-spec)",
                                    origin_type="supplier", origin_id=s, anchor_type="rm_side",
                                    anchor_ids=sorted({x.rm_movement_id for x in cl} | {x.rm_po_id for x in cl}),
                                    impacts=[("supplier", s, "rejected_qty", round(q, 2), self.rm.at[rms[0], "uom"]),
                                             ("supplier", s, "rejected_lots", len(cl), "lots")],
                                    meta={"kind": "quality", "supplier_id": s, "rms": rms, "lines": [x.rm_purchase_order_line_id for x in cl],
                                          "pattern": "secondary_quality_supplier" if s == qs else None})
                for x in cl:
                    self.quality_by_line[x.rm_purchase_order_line_id] = ev["key"]
        # link recurring QC events of the pattern supplier
        qe = [e for e in self.events if e["meta"].get("kind") == "quality" and e["origin_id"] == qs]
        self.links += [(b["key"], a["key"], "recurrence_of") for a, b in zip(qe, qe[1:])]

    def sourcing_change_events(self):
        self.switch_event = {}
        c = self.cat
        for sw in self.pat["primary_switches"]:
            d = D(sw["date"])
            rows = c[(c.rm_id == sw["rm_id"]) & (c._why == "primary_supplier_changed")]
            ev = self.add_event(event_type="sourcing_change", title=f"Primary source for {self.rm.at[sw['rm_id'], 'rm_name']} moved to {self.sup(sw['new_primary'])}",
                                start=d - pd.Timedelta(days=21), end=d, detected=d - pd.Timedelta(days=21), severity=2,
                                root_cause_code="supplier_performance", root_cause_text=f"sustained delivery issues from {self.sup(sw['old_primary'])}",
                                origin_type="raw_material", origin_id=sw["rm_id"], anchor_type="rm_side", anchor_ids=sorted(rows.catalog_line_id),
                                impacts=[("supplier", sw["old_primary"], "allocation_pct_change", -float(rows[rows.supplier_id == sw['old_primary']].allocation_pct.iat[0] if len(rows) else 0), "pct")],
                                meta={"kind": "switch", **sw, "catalog": sorted(rows.catalog_line_id)})
            self.switch_event[sw["rm_id"]] = ev["key"]
            # earlier disruptions of the old primary on this RM were mitigated by the switch
            for e in self.events:
                if e is ev:
                    continue
                if e["origin_id"] == sw["old_primary"] and e["meta"].get("rm_id") == sw["rm_id"] and e["start"] < d:
                    self.links.append((e["key"], ev["key"], "mitigated_by"))

    # ------------------------------------------------------------ FG-side (existing data) events
    def fg_events(self):
        ec = self.cfg["events"]
        PO = self.src["purchase_orders"].copy()
        M = self.src["inventory_movements"]
        P = self.src["products"].set_index("product_id")
        PO["delay"] = (PO.received_at - PO.expected_at).dt.days
        PO["q"] = PO.expected_at.dt.to_period("Q")
        PO = PO[PO.expected_at <= self.end]
        g = PO.groupby(["supplier_id", "q"]).agg(n=("delay", "size"), late=("delay", lambda x: (x >= 7).mean()),
                                                avgd=("delay", "mean"), bad=("status", lambda x: x.isin(["partial", "cancelled"]).sum())).reset_index()
        g = g[g.n >= 5]
        z = lambda s: (s - s.mean()) / s.std()
        g["score"] = z(g.avgd.fillna(0)) + z(g.late) + z(g.bad)
        g = g.sort_values("score", ascending=False).drop_duplicates("supplier_id").head(ec["fg_supplier_cluster_events"])
        self.traj = stock_trajectory(self.src)
        tr = M[M.movement_type == "transfer"]
        tin = tr[tr.quantity_change > 0]
        for r in g.itertuples():
            q0, q1 = r.q.start_time, r.q.end_time.normalize()
            x = PO[(PO.supplier_id == r.supplier_id) & (PO.q == r.q)]
            badpo = x[(x.delay >= 7) | x.status.isin(["partial", "cancelled"])]
            lates = x[x.delay >= 7].sort_values("received_at")
            det = lates.received_at.iloc[min(2, len(lates) - 1)] if len(lates) else q0 + pd.Timedelta(days=45)
            ev = self.add_event(event_type="supplier_performance_decline",
                                title=f"{self.sup(r.supplier_id)} delivery performance slump {r.q}",
                                start=q0, end=q1, detected=det, severity=self.severity(r.score, [2, 3, 4.5, 6]),
                                root_cause_code="supplier_delivery_performance",
                                root_cause_text=f"{int((x.delay >= 7).sum())} of {len(x)} POs arrived 7+ days late and {int(r.bad)} were short or cancelled",
                                origin_type="supplier", origin_id=r.supplier_id, anchor_type="existing_data", anchor_ids=sorted(badpo.purchase_order_id),
                                impacts=[("supplier", r.supplier_id, "late_rate_7d", round(float(r.late) * 100, 1), "pct"),
                                         ("supplier", r.supplier_id, "avg_delay_days", round(float(r.avgd), 1), "days"),
                                         ("supplier", r.supplier_id, "short_or_cancelled_pos", int(r.bad), "POs")],
                                meta={"kind": "fg_supplier", "supplier_id": r.supplier_id, "quarter": str(r.q)})
            self.fg_supplier_decision(ev, x, PO, tin)
        self.fg_stockout_events(M, tin)
        self.fg_country_events(PO)
        self.fg_adjustment_events(M)
        self.demand_shock_events(M, tin)

    def _fg_cost(self, qty, unit_cost, rate):
        return round(float(qty * unit_cost * rate), 2)

    def fg_supplier_decision(self, ev, x, PO, tin):
        if self.rng.random() > 0.85:
            return
        s, det = ev["origin_id"], ev["detected"]
        dec_at = det + pd.Timedelta(days=int(self.rng.integers(1, 5)))
        allp = PO[PO.supplier_id == s]
        L = self.src["purchase_order_lines"]
        cands = []
        c = allp[(allp.status == "cancelled") & (allp.ordered_at <= dec_at) & (allp.expected_at > dec_at) & (allp.expected_at <= dec_at + pd.Timedelta(days=90))]
        if len(c):
            cands.append(("cancel_po", c.iloc[0]))
        prods = set(self.src["products"][self.src["products"].supplier_id == s].product_id)
        t = tin[tin.product_id.isin(prods) & (tin.movement_at > dec_at) & (tin.movement_at <= dec_at + pd.Timedelta(days=30))]
        if len(t):
            cands.append(("reallocate_stock", t.iloc[0]))
        e = allp[(allp.received_at < allp.expected_at) & (allp.ordered_at <= dec_at) & (allp.received_at > dec_at) & (allp.received_at <= dec_at + pd.Timedelta(days=45))]
        if len(e):
            cands.append(("expedite", e.iloc[0]))
        p_ = allp[(allp.status == "partial") & (allp.received_at > dec_at) & (allp.received_at <= dec_at + pd.Timedelta(days=60))]
        if len(p_):
            cands.append(("accept_delay", p_.iloc[0]))
        if not cands:
            return
        kind, row = cands[int(self.rng.integers(len(cands)))]
        uc = self.src["products"].set_index("product_id").unit_cost
        if kind == "reallocate_stock":
            fp = [row.transfer_id]
            val = row.quantity_change * uc[row.product_id]
            direct = round(val * 0.03 + 400, 2)
            label = f"transfer {row.quantity_change} units of {row.product_id} into {row.warehouse_id} (transfer {row.transfer_id})"
            prods_w = [(row.product_id, row.warehouse_id)]
        else:
            ln = L[L.purchase_order_id == row.purchase_order_id]
            val = float((ln.quantity_ordered * ln.unit_cost).sum())
            fp = [row.purchase_order_id] + ln.purchase_order_line_id.tolist()
            direct = {"cancel_po": 250.0, "expedite": round(val * 0.08, 2), "accept_delay": 0.0}[kind]
            label = {"cancel_po": f"cancel {row.purchase_order_id} and re-plan from stock",
                     "expedite": f"expedite {row.purchase_order_id} with premium freight",
                     "accept_delay": f"accept the short/late delivery on {row.purchase_order_id}"}[kind]
            prods_w = [(p, row.warehouse_id) for p in ln.product_id]
        # outcome from existing trajectory: days at/below zero for the affected product-warehouses in 35 days
        tj = self.traj
        win = tj[(tj.movement_at >= dec_at) & (tj.movement_at <= dec_at + pd.Timedelta(days=35))]
        sd = int(sum(((win.product_id == p) & (win.warehouse_id == w) & (win.on_hand <= 0)).sum() for p, w in prods_w))
        day_cost = max(200.0, val * 0.01)
        opts = self._fg_options(kind, direct, day_cost, val)
        chosen = next(o for o in opts if o["option"] == kind)
        actual = direct + sd * day_cost
        self.add_decision(ev, dec_at, str(self.rng.choice(["buyer", "supply_planner", "category_manager"])), kind, opts, label,
                          chosen["est_cost"], chosen["est_stockout_days"], actual, sd, direct, dec_at + pd.Timedelta(days=35), fp,
                          meta={"side": "fg", "supplier_id": s, "argmin": min(opts, key=lambda o: o["est_cost"])["option"], "q4": dec_at.month >= 10})

    def _fg_options(self, kind, direct, day_cost, val):
        base = {"accept_delay": (0.0, 2), "expedite": (round(val * 0.08, 2), 0), "cancel_po": (250.0, 1), "reallocate_stock": (round(val * 0.03 + 400, 2), 0)}
        opts = []
        for k, (dc_, days) in base.items():
            dc_ = direct if k == kind else dc_
            days = 0 if k == kind and kind != "accept_delay" else days
            opts.append({"option": k, "est_cost": round(dc_ + days * day_cost, 2), "est_stockout_days": days,
                         "risk": "low" if days == 0 else "medium", "feasible": True})
        return opts

    def fg_stockout_events(self, M, tin):
        tj = self.traj.sort_values(["product_id", "warehouse_id", "movement_at"])
        neg = tj[tj.on_hand <= 0]
        for (p, w), g in neg.groupby(["product_id", "warehouse_id"]):
            start = g.movement_at.min()
            after = tj[(tj.product_id == p) & (tj.warehouse_id == w) & (tj.movement_at > g.movement_at.max()) & (tj.on_hand > 0)]
            endd = after.movement_at.min() if len(after) else self.end
            mids = M[(M.product_id == p) & (M.warehouse_id == w) & M.movement_at.isin(g.movement_at) & (M.movement_type == "sale")].movement_id.tolist()
            days = int((endd - start).days)
            ev = self.add_event(event_type="fg_stockout", title=f"{p} out of stock at {w}", start=start, end=endd, detected=start,
                                severity=self.severity(days, [7, 21, 45, 90]), root_cause_code="demand_exceeded_stock",
                                root_cause_text=f"sales drew {p} below zero at {w} before the next replenishment",
                                origin_type="product", origin_id=p, anchor_type="existing_data", anchor_ids=mids,
                                impacts=[("product", p, "stockout_days", days, "days"), ("warehouse", w, "min_on_hand", int(g.on_hand.min()), "units")],
                                meta={"kind": "fg_stockout", "product_id": p, "warehouse_id": w})
            dec_at = start + pd.Timedelta(days=int(self.rng.integers(0, 2)))
            t = tin[(tin.product_id == p) & (tin.warehouse_id == w) & (tin.movement_at > dec_at) & (tin.movement_at <= dec_at + pd.Timedelta(days=21))]
            rc = M[(M.product_id == p) & (M.warehouse_id == w) & (M.movement_type == "receipt") & (M.movement_at > dec_at)].head(1)
            uc = self.src["products"].set_index("product_id").unit_cost[p]
            val = float(max(20, -g.on_hand.min() + 20) * uc)
            day_cost = max(200.0, val * 0.01)
            if len(t):
                kind, fp, direct = "reallocate_stock", [t.transfer_id.iat[0]], round(t.quantity_change.iat[0] * uc * 0.03 + 400, 2)
                label = f"transfer {t.quantity_change.iat[0]} units into {w} (transfer {t.transfer_id.iat[0]})"
                fix = t.movement_at.iat[0]
            elif len(rc):
                kind, fp, direct = "accept_delay", [rc.movement_id.iat[0], rc.purchase_order_line_id.iat[0]], 0.0
                label = f"wait for the next receipt on {rc.purchase_order_line_id.iat[0]}"
                fix = rc.movement_at.iat[0]
            else:
                continue
            sd = max(0, int((min(fix, endd) - dec_at).days))
            opts = self._fg_options(kind, direct, day_cost, val)
            for o in opts:
                if o["option"] == "accept_delay":
                    o["est_stockout_days"] = max(1, sd if kind == "accept_delay" else sd + 5)
                    o["est_cost"] = round(o["est_stockout_days"] * day_cost, 2)
            chosen = next(o for o in opts if o["option"] == kind)
            self.add_decision(ev, dec_at, "supply_planner", kind, opts, label, chosen["est_cost"], chosen["est_stockout_days"],
                              direct + sd * day_cost, sd, direct, dec_at + pd.Timedelta(days=30), fp,
                              meta={"side": "fg", "product_id": p, "warehouse_id": w, "argmin": min(opts, key=lambda o: o["est_cost"])["option"], "q4": dec_at.month >= 10})

    def fg_country_events(self, PO):
        n = self.cfg["events"]["fg_country_month_events"]
        x = PO.dropna(subset=["delay"]).copy()
        x["country"] = x.supplier_id.map(self.scountry)
        x["m"] = x.expected_at.dt.to_period("M")
        g = x.groupby(["country", "m"]).agg(n=("delay", "size"), avgd=("delay", "mean")).reset_index()
        g = g[g.n >= 15].sort_values("avgd", ascending=False).head(n)
        domestic = set(self.plants.country_code)
        for r in g.itertuples():
            sub = x[(x.country == r.country) & (x.m == r.m) & (x.delay >= 7)]
            typ = "port_congestion" if r.country not in domestic else "carrier_capacity"
            det = sub.received_at.sort_values().iloc[min(2, len(sub) - 1)]
            ev = self.add_event(event_type=typ, title=f"Inbound FG delays from {r.country} suppliers, {r.m}",
                                start=r.m.start_time, end=r.m.end_time.normalize() + pd.Timedelta(days=14), detected=det,
                                severity=2, root_cause_code=typ, root_cause_text=f"average FG receipt delay {r.avgd:.1f} days for {r.country} suppliers",
                                origin_type="country", origin_id=r.country, anchor_type="existing_data", anchor_ids=sorted(sub.purchase_order_id),
                                impacts=[("country", r.country, "avg_delay_days", round(float(r.avgd), 1), "days"),
                                         ("country", r.country, "late_pos_7d", len(sub), "POs")],
                                meta={"kind": "fg_country", "country": r.country, "month": str(r.m)})
            for e in self.events:
                if e["meta"].get("kind") == "shock" and e["origin_id"] == r.country and e["start"] <= ev["end"] and e["end"] >= ev["start"]:
                    self.links.append((ev["key"], e["key"], "superseded_by"))

    def fg_adjustment_events(self, M):
        a = M[(M.movement_type == "adjustment") & (M.quantity_change < 0)].copy()
        a["m"] = a.movement_at.dt.to_period("M")
        g = a.groupby(["warehouse_id", "m"]).quantity_change.agg(["sum", "size"]).reset_index().sort_values("sum").head(self.cfg["events"]["fg_adjustment_events"])
        for r in g.itertuples():
            sub = a[(a.warehouse_id == r.warehouse_id) & (a.m == r.m)]
            ev = self.add_event(event_type="inventory_discrepancy", title=f"Cycle-count losses at {r.warehouse_id}, {r.m}",
                                start=sub.movement_at.min(), end=sub.movement_at.max(), detected=sub.movement_at.max(), severity=1,
                                root_cause_code="count_variance", root_cause_text=f"{int(r.size)} negative adjustments totalling {int(-r.sum)} units",
                                origin_type="warehouse", origin_id=r.warehouse_id, anchor_type="existing_data", anchor_ids=sorted(sub.movement_id),
                                impacts=[("warehouse", r.warehouse_id, "adjustment_units", int(r.sum), "units")],
                                meta={"kind": "fg_adjust", "warehouse_id": r.warehouse_id, "month": str(r.m)})

    def demand_shock_events(self, M, tin):
        shk = self.ctx.read("demand_shocks")
        dm = self.ctx.read("demand_raw")
        self.shock_key_event = {}
        s = M[M.movement_type == "sale"].copy()
        s["week_start"] = week_start(s.movement_at)
        for r in shk.itertuples():
            rows = dm[dm.demand_shock_event_id == r.shock_key]
            mids = s[(s.product_id == r.product_id) & (s.week_start == r.week_start)].movement_id.tolist()
            unf = int(rows.unfulfilled_units.sum())
            ev = self.add_event(event_type="demand_spike", title=f"Demand spike on {r.product_id} week of {dstr(r.week_start)}",
                                start=r.week_start, end=r.week_start + pd.Timedelta(days=6), detected=r.week_start + pd.Timedelta(days=int(self.rng.integers(0, 3))),
                                severity=self.severity(unf, [20, 60, 120, 250]), root_cause_code="customer_order_surge",
                                root_cause_text=f"orders ran {r.z:.1f} sigma above the SKU's weekly norm; allocation capped shipments",
                                origin_type="product", origin_id=r.product_id, anchor_type="existing_data", anchor_ids=mids,
                                impacts=[("product", r.product_id, "unfulfilled_units", unf, "units"),
                                         ("product", r.product_id, "demand_z", round(float(r.z), 2), "sigma")],
                                meta={"kind": "demand", "product_id": r.product_id, "shock_key": r.shock_key})
            self.shock_key_event[r.shock_key] = ev["key"]
            ws = set(rows.warehouse_id)
            t = tin[(tin.product_id == r.product_id) & tin.warehouse_id.isin(ws) & (tin.movement_at >= ev["detected"]) & (tin.movement_at <= ev["detected"] + pd.Timedelta(days=21))]
            uc = self.src["products"].set_index("product_id").unit_cost[r.product_id]
            day_cost = max(200.0, unf * uc * 0.02)
            if len(t):
                kind, fp, direct = "reallocate_stock", [t.transfer_id.iat[0]], round(t.quantity_change.iat[0] * uc * 0.03 + 400, 2)
                label = f"rebalance stock into {t.warehouse_id.iat[0]} (transfer {t.transfer_id.iat[0]})"
            else:
                kind, fp, direct = "accept_delay", mids[:3], 0.0
                label = "ship available stock and backorder the balance"
            opts = self._fg_options(kind, direct, day_cost, unf * uc)
            chosen = next(o for o in opts if o["option"] == kind)
            self.add_decision(ev, ev["detected"], "supply_planner", kind, opts, label, chosen["est_cost"], chosen["est_stockout_days"],
                              direct + unf * uc * 0.1, 1 if unf else 0, direct, ev["detected"] + pd.Timedelta(days=21), fp,
                              meta={"side": "fg", "product_id": r.product_id, "argmin": min(opts, key=lambda o: o["est_cost"])["option"], "q4": ev["detected"].month >= 10})

    # ------------------------------------------------------------ decisions
    def add_decision(self, ev, decided_at, role, dtype, opts, label, exp_cost, exp_days, act_cost, act_days, action_cost, assessed, fp, meta=None, key=None):
        ec = self.cfg["events"]
        exp_cost, act_cost = float(exp_cost), float(act_cost)
        if act_days <= exp_days + ec["success_tol_days"] and act_cost <= ec["success_cost_ratio"] * exp_cost + 500:
            outcome = "success"
        elif act_days >= exp_days + ec["failed_days"] or act_cost >= ec["failed_cost_ratio"] * exp_cost + 1000:
            outcome = "failed"
        else:
            outcome = "partial"
        d = {"key": key or f"D{len(self.decisions)}", "event_key": ev["key"], "decided_at": pd.Timestamp(decided_at).normalize(), "role": role,
             "decision_type": dtype, "options": opts, "chosen_option": label, "expected_cost": round(exp_cost, 2),
             "expected_stockout_days": int(exp_days), "actual_cost": round(act_cost, 2), "actual_stockout_days": int(act_days),
             "action_cost": round(float(action_cost), 2), "outcome_label": outcome,
             "outcome_assessed_at": min(pd.Timestamp(assessed).normalize() + pd.Timedelta(days=int(self.rng.integers(2, 8))), self.end),
             "footprint": [f for f in fp if f is not None and f == f], "meta": meta or {}}
        ev.setdefault("decisions", []).append(d["key"])
        self.decisions.append(d)
        return d

    def rm_decisions(self):
        """Decisions made inside the Stage 1 simulation -> attach to events (episode, shock, pattern, or a new supplier_delay event)."""
        decs = self.plan["decisions"]
        budget = int(self.ctx.sc["rm_events"])
        routine = [d for d in decs if d.get("episode") is None or d["episode"] != d["episode"]]
        routine = [d for d in routine if not d.get("followup")]
        # prioritise diverse decision types and pattern contexts
        pr = []
        for d in routine:
            cause = self._cause(d)
            score = self.rng.random() + (2 if d["chosen"] != "expedite" else 0) + (1.5 if cause != "base" else 0)
            pr.append((score, d))
        pr.sort(key=lambda x: -x[0])
        chosen_routine = [d for _, d in pr[:budget]]
        for d in [x for x in decs if x.get("episode") == x.get("episode") and x.get("episode") is not None] + chosen_routine:
            ev = self._event_for(d)
            if ev is None:
                continue
            self._add_rm_decision(ev, d)

    def _cause(self, d):
        if d.get("trigger_lid") is None or d["trigger_lid"] != d["trigger_lid"]:
            return "base"
        try:
            return self.meta_ix.loc[(d["pair"], int(d["trigger_lid"])), "cause"]
        except KeyError:
            return "base"

    def _event_by_key(self, k):
        return next(e for e in self.events if e["key"] == k)

    def _event_for(self, d):
        t = self.day(d["t"])
        if d.get("episode") is not None and d["episode"] == d["episode"]:
            k = self.episode_event.get(int(d["episode"]))
            return self._event_by_key(k) if k else None
        cause = self._cause(d)
        cand = None
        if cause in SHOCK_TYPES and d.get("shock") and d["shock"] == d["shock"] and d["shock"] in self.shock_event:
            cand = self.shock_event[d["shock"]]
        elif cause == "peak_season_capacity" and t.year in self.q4_event:
            cand = self.q4_event[t.year]
        elif cause == "allocation_cut":
            for i, a_ in enumerate(self.plan["alloc_windows"]):
                if a_[0] - 60 <= d["t"] <= a_[1] + 30 and i in self.alloc_event:
                    cand = self.alloc_event[i]
        elif cause == "lead_time_extension" and getattr(self, "creep_event", None) and t >= self.creep_detect:
            cand = self.creep_event
        if cand is not None:
            ev = self._event_by_key(cand)
            if ev["detected"] <= t:
                return ev
        # new routine supplier_delay event anchored on the trigger PO and its revisions
        po_id = d["refs"].get("trigger_po_id")
        if po_id is None:
            return None
        rv = self.rev_by_po.get(po_id)
        det = rv.revised_at.min() if rv is not None and len(rv) else t
        det = min(det, t)
        slip = int((self.po_ix.at[po_id, "received_at"] - self.po_ix.at[po_id, "promised_at"]).days) if pd.notna(self.po_ix.at[po_id, "received_at"]) else None
        reason = rv.reason_code.iloc[0] if rv is not None and len(rv) else "supplier_capacity"
        s = d["trigger_supplier"]
        return self.add_event(event_type="supplier_delay", title=f"{self.sup(s)} slips {self.rm.at[d['rm_id'], 'rm_name']} delivery {po_id}",
                              start=det, end=self.po_ix.at[po_id, "received_at"] if pd.notna(self.po_ix.at[po_id, "received_at"]) else self.end,
                              detected=det, severity=2 if (slip or 0) < 10 else 3, root_cause_code=reason,
                              root_cause_text=f"{ROOT_TEXT.get(reason, reason)} at {self.sup(s)}",
                              origin_type="supplier", origin_id=s, anchor_type="rm_side", anchor_ids=[po_id] + (rv.rm_po_revision_id.tolist() if rv is not None else []),
                              impacts=[("purchase_order", po_id, "slip_days", slip if slip is not None else -1, "days"),
                                       ("plant", d["plant_id"], "projected_shortage_days", int(d["options"][0]["est_stockout_days"]), "days")],
                              meta={"kind": "routine", "supplier_id": s, "rm_id": d["rm_id"], "plant_id": d["plant_id"], "po_id": po_id,
                                    "cause": cause})

    def _add_rm_decision(self, ev, d):
        refs, t = d["refs"], self.day(d["t"])
        po_id = refs.get("trigger_po_id")
        fp = []
        if po_id:
            fp.append(po_id)
            rv = self.rev_by_po.get(po_id)
            if rv is not None:
                fp += rv[rv.revised_at <= t + DAY].rm_po_revision_id.tolist()
        for k in ("switch_po_id", "cancelled_po_id", "first_po_id", "transfer_id"):
            if refs.get(k):
                fp.append(refs[k])
        if refs.get("switch_line_id"):
            fp.append(refs["switch_line_id"])
        if d["chosen"] == "build_safety_stock" and not refs.get("first_po_id"):
            return
        fp = list(dict.fromkeys(fp))
        rm_, pl = d["rm_id"], d["plant_id"]
        ch = d["chosen"]
        label = {"expedite": f"expedite {po_id} with {self.sup(d['trigger_supplier'])}",
                 "accept_delay": f"accept the revised ETA on {po_id} and cover gaps with spot buys",
                 "switch_supplier": f"place {refs.get('switch_po_id')} with the alternate source" + (f" and cancel {refs['cancelled_po_id']}" if refs.get("cancelled_po_id") else ""),
                 "reallocate_stock": f"transfer {rm_} stock to {pl} (transfer {refs.get('transfer_id')})",
                 "build_safety_stock": f"raise {rm_} safety stock at {pl} by {refs.get('ss_extra', 0):,.0f} {self.rm.at[rm_, 'uom']} until {dstr(self.day(refs.get('ss_until', d['t'])))}"}[ch]
        opts = [dict(o) for o in d["options"]]
        dd = self.add_decision(ev, t, d["role"], ch, opts, label, d["expected_cost"], d["expected_stockout_days"], d["actual_cost"],
                               d["actual_stockout_days"], d.get("direct_est") or 0.0, self.day(d["outcome_day"]), fp,
                               meta={"side": "rm", "supplier_id": d["trigger_supplier"], "rm_id": rm_, "plant_id": pl, "q4": bool(d["q4"]),
                                     "argmin": d["argmin_est"], "belief_eta": d.get("belief_eta"), "refs": refs,
                                     "episode": d.get("episode") if d.get("episode") == d.get("episode") else None,
                                     "shock": d.get("shock") if d.get("shock") == d.get("shock") else None,
                                     "cause": self._cause(d), "followup": bool(d.get("followup") == True)})
        return dd

    def strategic_decisions(self):
        # renegotiations after price spikes (negotiations created here too)
        for i, k in self.spike_event.items():
            ev = self._event_by_key(k)
            m = ev["meta"]
            neg = self.add_negotiation(m["supplier_id"], m["rm_id"], ev["detected"] + pd.Timedelta(days=int(self.rng.integers(3, 10))),
                                       (m["effective"] + pd.Timedelta(days=int(self.rng.integers(75, 120)))) if m["rollback_price"] is None else
                                       self.cat.set_index("catalog_line_id").at[m["rollback_catalog"], "price_valid_from"] - pd.Timedelta(days=int(self.rng.integers(2, 6))),
                                       "price_increase_rollback", m)
            ask = m["old_price"]
            opts = [{"option": "renegotiate", "est_cost": round((m["new_price"] - ask) * 0.4 * self._annual_qty(m["rm_id"]) * 0.25, 2), "est_stockout_days": 0, "risk": "medium", "feasible": True},
                    {"option": "accept_delay", "est_cost": round((m["new_price"] - ask) * self._annual_qty(m["rm_id"]) * 0.25, 2), "est_stockout_days": 0, "risk": "low", "feasible": True},
                    {"option": "substitute_rm", "est_cost": float(self.cfg["costs"]["substitute_cost"]) + 15000, "est_stockout_days": 0, "risk": "high", "feasible": False}]
            act_price = m["rollback_price"] if m["rollback_price"] is not None else m["new_price"]
            act = (act_price - ask) * self._annual_qty(m["rm_id"]) * 0.25
            fp = [neg["key"], m["spike_catalog"]] + ([m["rollback_catalog"]] if m["rollback_catalog"] else [])
            d = self.add_decision(ev, ev["detected"] + pd.Timedelta(days=2), "category_manager", "renegotiate", opts,
                                  f"open price negotiation with {self.sup(m['supplier_id'])} on {m['rm_id']}", opts[0]["est_cost"], 0,
                                  act, 0, 2500.0, neg["concluded_at"] if pd.notna(neg["concluded_at"]) else self.end, fp,
                                  meta={"side": "rm", "supplier_id": m["supplier_id"], "rm_id": m["rm_id"], "argmin": "renegotiate",
                                        "q4": ev["detected"].month >= 10, "negotiation_key": neg["key"]})
            neg["decision_key"] = d["key"]
        # primary switches -> switch_supplier / reduce_allocation decisions
        for sw in self.pat["primary_switches"]:
            ev = self._event_by_key(self.switch_event[sw["rm_id"]])
            d0 = D(sw["date"])
            prior = [e for e in self.events if e["origin_id"] == sw["old_primary"] and e["start"] < d0 - pd.Timedelta(days=25)
                     and e["event_type"] != "sourcing_change"]
            dec_at = ev["detected"] + pd.Timedelta(days=int(self.rng.integers(2, 8)))
            old, new = self._otif(sw["old_primary"], sw["rm_id"], None, d0), self._otif(sw["new_primary"], sw["rm_id"], d0, None)
            kind = "switch_supplier" if self.rng.random() < 0.5 else "reduce_allocation"
            opts = [{"option": kind, "est_cost": 3500.0, "est_stockout_days": 0, "risk": "medium", "feasible": True},
                    {"option": "accept_delay", "est_cost": 9000.0, "est_stockout_days": 4, "risk": "high", "feasible": True},
                    {"option": "renegotiate", "est_cost": 5000.0, "est_stockout_days": 2, "risk": "medium", "feasible": True}]
            sdays = int(self.snaps[(self.snaps.rm_id == sw["rm_id"]) & (self.snaps.snapshot_date > d0) &
                                   (self.snaps.snapshot_date <= d0 + pd.Timedelta(days=180))].stockout_flag.sum())
            act = 3500.0 + sdays * 1500
            self.add_decision(ev, dec_at, "category_manager", kind, opts,
                              f"make {self.sup(sw['new_primary'])} primary for {sw['rm_id']} from {sw['date']}" if kind == "switch_supplier"
                              else f"cut {self.sup(sw['old_primary'])} allocation on {sw['rm_id']} and shift volume to {self.sup(sw['new_primary'])}",
                              3500.0, 0, act, sdays, 3500.0, min(d0 + pd.Timedelta(days=180), self.end), ev["meta"]["catalog"],
                              meta={"side": "rm", "supplier_id": sw["old_primary"], "rm_id": sw["rm_id"], "argmin": kind, "q4": dec_at.month >= 10,
                                    "otif_old": old, "otif_new": new, "prior_events": [e["key"] for e in prior][-3:]})
        # substitutions from versioned BOMs
        bom = self.ctx.read("bill_of_materials")
        v2 = bom[(bom.bom_version == 2) & (bom.change_reason == "substitution")]
        v1 = bom[bom.bom_version == 1]
        for pid, g in v2.groupby("product_id"):
            old_rms = set(v1[v1.product_id == pid].rm_id)
            new_rm = sorted(set(g.rm_id) - old_rms)
            gone = sorted(old_rms - set(g.rm_id))
            if not new_rm or not gone:
                continue
            eff = g.effective_from.iat[0]
            pl = self.pext.at[pid, "plant_id"]
            prior = [e for e in self.events if e["meta"].get("rm_id") == gone[0] and e["detected"] < eff - pd.Timedelta(days=20)]
            if prior:
                ev = prior[-1]
            else:
                det = eff - pd.Timedelta(days=int(self.rng.integers(35, 60)))
                ev = self.add_event(event_type="supply_risk_review", title=f"Supply risk review for {self.rm.at[gone[0], 'rm_name']} in {pid}",
                                    start=det, end=eff, detected=det, severity=1, root_cause_code="single_source_exposure" if self.rm.at[gone[0], "is_single_source"] else "cost_and_resilience",
                                    root_cause_text=f"{self.rmn(gone[0])} flagged in the quarterly risk review", origin_type="raw_material",
                                    origin_id=gone[0], anchor_type="rm_side", anchor_ids=sorted(g.bom_id), impacts=[("product", pid, "bom_lines_changed", 1, "lines")],
                                    meta={"kind": "risk_review", "rm_id": gone[0], "product_id": pid})
            dec_at = max(ev["detected"] + DAY, eff - pd.Timedelta(days=int(self.rng.integers(15, 35))))
            sdays = int(self.snaps[(self.snaps.rm_id == new_rm[0]) & (self.snaps.plant_id == pl) & (self.snaps.snapshot_date > eff) &
                                   (self.snaps.snapshot_date <= eff + pd.Timedelta(days=180))].stockout_flag.sum())
            opts = [{"option": "substitute_rm", "est_cost": float(self.cfg["costs"]["substitute_cost"]), "est_stockout_days": 0, "risk": "medium", "feasible": True},
                    {"option": "switch_supplier", "est_cost": 6000.0 + 1200, "est_stockout_days": 1, "risk": "medium", "feasible": True},
                    {"option": "accept_delay", "est_cost": 12000.0, "est_stockout_days": 5, "risk": "high", "feasible": True}]
            self.add_decision(ev, dec_at, "category_manager", "substitute_rm", opts,
                              f"qualify {self.rmn(new_rm[0])} to replace {gone[0]} in {pid} from {dstr(eff)}",
                              opts[0]["est_cost"], 0, float(self.cfg["costs"]["substitute_cost"]) + sdays * 1500, sdays,
                              float(self.cfg["costs"]["substitute_cost"]), min(eff + pd.Timedelta(days=180), self.end), sorted(g.bom_id),
                              meta={"side": "rm", "rm_id": gone[0], "new_rm": new_rm[0], "product_id": pid, "plant_id": pl, "argmin": "substitute_rm",
                                    "q4": dec_at.month >= 10, "supplier_id": None})

    def _annual_qty(self, rm_):
        m = self.meta[(self.meta.rm_id == rm_)]
        return float(m.qty.sum() / 3.1)

    def _otif(self, sup, rm_, a, b):
        po = self.po[self.po.supplier_id == sup]
        ln = self.pol[self.pol.rm_id == rm_]
        po = po[po.rm_po_id.isin(ln.rm_po_id) & po.received_at.notna()]
        if a is not None:
            po = po[po.promised_at >= a]
        if b is not None:
            po = po[po.promised_at < b]
        return round(float((po.received_at <= po.promised_at).mean()), 3) if len(po) else None

    # ------------------------------------------------------------ negotiations
    def add_negotiation(self, sup, rm_, started, concluded, topic, m=None):
        m = m or {}
        concluded = concluded if concluded <= self.end else pd.NaT
        ct = self.contract_at(sup, started)
        n = {"key": f"N{len(self.negotiations)}", "supplier_id": sup, "rm_id": rm_, "started_at": started.normalize(),
             "concluded_at": concluded.normalize() if pd.notna(concluded) else pd.NaT, "topic": topic,
             "contract_id": ct.contract_id if ct is not None else None, "meta": m}
        if topic == "price_increase_rollback":
            rb = m.get("rollback_price")
            n["our_ask"] = f"roll {rm_} back to {m['old_price']:.4f}/{self.rm.at[rm_, 'uom']} (pre-increase)"
            n["their_offer"] = f"hold {m['new_price']:.4f}; offer 60-day payment terms"
            n["concessions_json"] = json.dumps([{"party": "supplier", "concession": "partial price rollback" if rb else "none yet"},
                                                {"party": "buyer", "concession": "12-month volume commitment at contract minimum"}])
            n["final_terms"] = f"unit price {rb:.4f} from {dstr(concluded)}" if rb and pd.notna(concluded) else "pending"
            n["outcome"] = ("agreed" if rb and (m["new_price"] - rb) / (m["new_price"] - m["old_price"]) > 0.6 else "partial") if rb and pd.notna(concluded) else "open"
        self.negotiations.append(n)
        return n

    def more_negotiations(self):
        rng = self.rng
        # contract renewals
        ct = self.contracts.sort_values("valid_from")
        ren = ct[ct.duplicated("supplier_id", keep="first") & (ct.valid_from >= D("2023-02-01")) & (ct.valid_from <= self.end)]
        pats = {self.pat[k] for k in ("q4_slip_supplier", "small_po_supplier", "lead_time_creep_supplier", "secondary_quality_supplier", "price_spike_supplier")}
        pri = ren[ren.supplier_id.isin(pats)]
        rest = ren[~ren.supplier_id.isin(pats)]
        n_extra = int(self.cfg["events"]["negotiations_extra"] * (1 if self.ctx.scale == "full" else 0.5))
        pick = pd.concat([pri, rest.sample(n=min(len(rest), max(0, n_extra - len(pri))), random_state=int(rng.integers(1 << 30)))])
        for c in pick.itertuples():
            prev = ct[(ct.supplier_id == c.supplier_id) & (ct.valid_to < c.valid_from)].tail(1)
            st = c.valid_from - pd.Timedelta(days=int(rng.integers(30, 60)))
            con = c.valid_from - pd.Timedelta(days=int(rng.integers(2, 12)))
            rows = self.cat[(self.cat.contract_id == c.contract_id) & (self.cat._why == "contract_renewal")]
            before = self.cat[self.cat.contract_id.isin(prev.contract_id)]
            chg = None
            if len(rows) and len(before):
                j = rows.merge(before.sort_values("price_valid_from").drop_duplicates(["rm_id", "supplier_id"], keep="last"), on=["rm_id", "supplier_id"], suffixes=("", "_b"))
                if len(j):
                    chg = float((j.unit_price / j.unit_price_b - 1).mean())
            ask = -0.03 if chg is None or chg > 0 else chg - 0.01
            n = {"key": f"N{len(self.negotiations)}", "supplier_id": c.supplier_id, "rm_id": rows.rm_id.iat[0] if len(rows) else None,
                 "started_at": st, "concluded_at": con, "topic": "contract_renewal", "contract_id": c.contract_id,
                 "our_ask": f"price change {ask:+.1%}, min volume {c.min_volume * 0.8:,.0f}, penalty '{c.penalty_clause}'",
                 "their_offer": f"price change {((chg or 0) + 0.03):+.1%}, min volume {c.min_volume * 1.2:,.0f}",
                 "concessions_json": json.dumps([{"party": "supplier", "concession": f"accepted penalty clause '{c.penalty_clause}'"},
                                                 {"party": "buyer", "concession": f"min volume {c.min_volume:,.0f} over the term"}]),
                 "final_terms": f"{c.price_terms}; price change {(chg or 0):+.1%}; min volume {c.min_volume:,.0f}; valid {dstr(c.valid_from)} to {dstr(c.valid_to)}",
                 "outcome": "agreed" if (chg or 0) <= 0.03 else "partial", "meta": {"contract": c.contract_id, "min_volume": c.min_volume,
                                                                                  "valid_from": c.valid_from, "valid_to": c.valid_to, "chg": chg}}
            self.negotiations.append(n)
        # capacity reservations with the Q4 supplier (every October) -> commitments breached by the Nov-Dec slips
        s = self.pat["q4_slip_supplier"]
        for y in (2023, 2024, 2025):
            st = D(f"{y}-10-01") + pd.Timedelta(days=int(rng.integers(0, 10)))
            n = {"key": f"N{len(self.negotiations)}", "supplier_id": s, "rm_id": None, "started_at": st, "concluded_at": st + pd.Timedelta(days=int(rng.integers(5, 12))),
                 "topic": "capacity_reservation", "contract_id": getattr(self.contract_at(s, st), "contract_id", None),
                 "our_ask": "reserve line capacity for all Nov-Dec releases at quoted lead time",
                 "their_offer": "reserve 80% of forecast volume; balance best effort",
                 "concessions_json": json.dumps([{"party": "supplier", "concession": "priority slots for our Nov-Dec POs"},
                                                 {"party": "buyer", "concession": "firm forecast frozen 6 weeks out"}]),
                 "final_terms": "capacity reserved for Nov-Dec POs placed by 15 Oct; on-time delivery committed", "outcome": "agreed",
                 "meta": {"year": y}}
            self.negotiations.append(n)
        # late-delivery credit claims after big RM shortages with penalty clauses
        big = sorted([e for e in self.events if e["event_type"] == "rm_shortage"], key=lambda e: -e["severity"])[:10]
        for e in big:
            c = self.contract_at(e["origin_id"], e["detected"])
            if c is None or c.penalty_clause == "none":
                continue
            st = e["end"] + pd.Timedelta(days=int(rng.integers(3, 15)))
            con = st + pd.Timedelta(days=int(rng.integers(10, 30)))
            credit = round(float(e["meta"]["blocked"] * 850), 0)
            out = str(rng.choice(["agreed", "partial", "no_agreement"], p=[0.5, 0.35, 0.15]))
            n = {"key": f"N{len(self.negotiations)}", "supplier_id": e["origin_id"], "rm_id": e["meta"]["rm_id"], "started_at": st,
                 "concluded_at": con, "topic": "late_delivery_credit", "contract_id": c.contract_id,
                 "our_ask": f"credit of {credit:,.0f} under '{c.penalty_clause}'",
                 "their_offer": f"credit of {credit * 0.4:,.0f} citing force majeure" if c.force_majeure_flag else f"credit of {credit * 0.6:,.0f}",
                 "concessions_json": json.dumps([{"party": "supplier", "concession": "credit note on next invoice"}]),
                 "final_terms": {"agreed": f"credit {credit * 0.9:,.0f}", "partial": f"credit {credit * 0.5:,.0f}", "no_agreement": "no credit; escalated to QBR"}[out],
                 "outcome": out, "meta": {"event_key": e["key"], "credit": credit}}
            n["meta"]["event_key"] = e["key"]
            self.negotiations.append(n)

    # ------------------------------------------------------------ commitments
    def add_commitment(self, **k):
        k["key"] = f"C{len(self.commitments)}"
        self.commitments.append(k)
        return k

    def _po_commit_status(self, po_id, made_at, due, advance=5):
        po = self.po_ix.loc[po_id]
        rv = self.rev_by_po.get(po_id)
        later = rv[(rv.revised_at > made_at) & (rv.revised_at <= due) & (rv.reason_code != "expedite")] if rv is not None else rv
        if po.status == "cancelled":
            return "renegotiated", made_at + pd.Timedelta(days=2)
        rec = po.received_at
        if pd.notna(rec) and rec <= due:
            return "fulfilled", rec
        if later is not None and len(later):
            f = later.iloc[0]
            if (due - f.revised_at).days >= advance and f.reason_code != "expedite_missed":
                return "renegotiated", f.revised_at
        if pd.isna(rec):
            return ("open", pd.NaT) if due >= self.end or po.status == "open" else ("breached", min(due + DAY, self.end))
        return "breached", rec

    def build_commitments(self):
        for d in self.decisions:
            m, refs = d["meta"], d["meta"].get("refs", {})
            if m.get("side") != "rm":
                continue
            t = d["decided_at"]
            po_id = refs.get("trigger_po_id")
            s = m.get("supplier_id")
            if d["decision_type"] == "expedite" and po_id:
                rv = self.rev_by_po.get(po_id)
                ex = rv[(rv.reason_code == "expedite")] if rv is not None else None
                if ex is not None and len(ex):
                    due = ex.new_expected_at.iat[-1]
                    stt, res = self._po_commit_status(po_id, t, due)
                    self.add_commitment(decision_key=d["key"], negotiation_key=None, counterparty_type="supplier", counterparty_id=s,
                                        made_by_role="supplier_account_manager", made_at=t, due_date=due,
                                        text=f"{self.sup(s)} commits to deliver {po_id} by {dstr(due)} on expedited freight",
                                        quantity=float(self.pol[self.pol.rm_po_id == po_id].quantity_ordered.sum()), penalty=self._penalty(s, t),
                                        status=stt, resolved_at=res, meta={"po_id": po_id})
            if d["decision_type"] in ("accept_delay", "expedite") and po_id and self.rng.random() < 0.8:
                bel = m.get("belief_eta")
                if bel is not None and bel == bel:
                    due = self.day(bel)
                    stt, res = self._po_commit_status(po_id, t, due, advance=4)
                    self.add_commitment(decision_key=d["key"], negotiation_key=None, counterparty_type="supplier", counterparty_id=s,
                                        made_by_role="supplier_account_manager", made_at=t - pd.Timedelta(days=int(self.rng.integers(0, 2))), due_date=due,
                                        text=f"{self.sup(s)} confirms revised ship-to-arrive date {dstr(due)} for {po_id}",
                                        quantity=float(self.pol[self.pol.rm_po_id == po_id].quantity_ordered.sum()), penalty=self._penalty(s, t),
                                        status=stt, resolved_at=res, meta={"po_id": po_id})
            if d["decision_type"] == "switch_supplier" and refs.get("switch_po_id"):
                sp = refs["switch_po_id"]
                s2 = self.po_ix.at[sp, "supplier_id"]
                due = self.po_ix.at[sp, "promised_at"]
                stt, res = self._po_commit_status(sp, t, due)
                self.add_commitment(decision_key=d["key"], negotiation_key=None, counterparty_type="supplier", counterparty_id=s2,
                                    made_by_role="supplier_account_manager", made_at=t, due_date=due,
                                    text=f"{self.sup(s2)} commits to deliver replacement order {sp} by {dstr(due)}",
                                    quantity=float(self.pol[self.pol.rm_po_id == sp].quantity_ordered.sum()), penalty=self._penalty(s2, t),
                                    status=stt, resolved_at=res, meta={"po_id": sp})
            if d["decision_type"] == "reallocate_stock" and refs.get("transfer_id"):
                tr = self.mv[(self.mv.transfer_id == refs["transfer_id"])]
                src_row = tr[tr.quantity_change < 0]
                arr = tr[tr.quantity_change > 0].movement_at
                est = next((o for o in d["options"] if o["option"] == "reallocate_stock"), None)
                due = t + pd.Timedelta(days=int(max(2, 2 + (est or {}).get("est_stockout_days", 0))))
                arrival = arr.iat[0] if len(arr) else pd.NaT
                stt = "open" if pd.isna(arrival) else ("fulfilled" if arrival <= due else "breached")
                self.add_commitment(decision_key=d["key"], negotiation_key=None, counterparty_type="plant",
                                    counterparty_id=src_row.plant_id.iat[0] if len(src_row) else m["plant_id"], made_by_role="plant_scheduler", made_at=t,
                                    due_date=due, text=f"{src_row.plant_id.iat[0] if len(src_row) else 'hub plant'} commits to ship {abs(float(src_row.quantity_change.sum())):,.1f} "
                                                       f"{self.rm.at[m['rm_id'], 'uom']} of {m['rm_id']} to arrive at {m['plant_id']} by {dstr(due)}",
                                    quantity=abs(float(src_row.quantity_change.sum())), penalty="internal", status=stt,
                                    resolved_at=arrival if pd.notna(arrival) else pd.NaT, meta={"transfer_id": refs["transfer_id"]})
            if m.get("episode") is not None and d["decision_type"] != "build_safety_stock":
                ep = next(e for e in self.plan["episodes"] if e["id"] == int(m["episode"]))
                pol_id = ep["fg_lines"][0]
                po_id_fg = self.POL.at[pol_id, "purchase_order_id"]
                fg = self.PO.loc[po_id_fg]
                bel = m.get("belief_eta")
                lead = int((fg.expected_at - self.runs.set_index("purchase_order_line_id").at[pol_id, "planned_start"]).days)
                due = max(fg.expected_at, self.day(bel) + pd.Timedelta(days=lead)) if bel is not None and bel == bel else fg.expected_at
                rec = fg.received_at
                later_rev = self.rev[self.rev.rm_po_id.isin(ep["slipped_po_ids"]) & (self.rev.revised_at > t) & (self.rev.revised_at <= due - pd.Timedelta(days=5))]
                if rec <= due:
                    stt, res = "fulfilled", rec
                elif len(later_rev) and self.rng.random() < 0.6:
                    stt, res = "renegotiated", later_rev.revised_at.iat[0]
                else:
                    stt, res = "breached", rec
                self.add_commitment(decision_key=d["key"], negotiation_key=None, counterparty_type="warehouse", counterparty_id=fg.warehouse_id,
                                    made_by_role="plant_scheduler", made_at=t + DAY, due_date=due,
                                    text=f"{m['plant_id']} commits to deliver FG line {pol_id} (PO {po_id_fg}) to {fg.warehouse_id} by {dstr(due)}",
                                    quantity=float(self.POL.at[pol_id, "quantity_ordered"]), penalty="service-level KPI", status=stt, resolved_at=res,
                                    meta={"fg_line": pol_id})
            if d["decision_type"] == "build_safety_stock":
                until = self.day(refs.get("ss_until", 0))
                sn = self.snaps[(self.snaps.rm_id == m["rm_id"]) & (self.snaps.plant_id == m["plant_id"]) & (self.snaps.snapshot_date > t) & (self.snaps.snapshot_date <= until)]
                ok = (sn.on_hand_units >= 0.8 * sn.safety_stock_units).mean() if len(sn) else 1.0
                stt = "open" if until > self.end else ("fulfilled" if ok >= 0.8 else "breached")
                self.add_commitment(decision_key=d["key"], negotiation_key=None, counterparty_type="plant", counterparty_id=m["plant_id"],
                                    made_by_role="supply_planner", made_at=t, due_date=until,
                                    text=f"Planning commits to hold {m['rm_id']} at or above the raised safety stock at {m['plant_id']} until {dstr(until)}",
                                    quantity=float(refs.get("ss_extra", 0)), penalty="internal", status=stt,
                                    resolved_at=pd.NaT if stt == "open" else min(until, self.end), meta={})
        # negotiation commitments
        for n in self.negotiations:
            s = n["supplier_id"]
            if n["topic"] == "contract_renewal":
                mm = n["meta"]
                vf, vt = mm["valid_from"], mm["valid_to"]
                bought = self.pol.merge(self.po, on="rm_po_id")
                bought = bought[(bought.supplier_id == s) & (bought.ordered_at >= vf) & (bought.ordered_at <= vt)].quantity_received.sum()
                stt = "open" if vt > self.end else ("fulfilled" if bought >= mm["min_volume"] else "breached")
                self.add_commitment(decision_key=None, negotiation_key=n["key"], counterparty_type="supplier", counterparty_id=s, made_by_role="category_manager",
                                    made_at=n["concluded_at"], due_date=vt, text=f"We commit to purchase at least {mm['min_volume']:,.0f} units from {self.sup(s)} under {n['contract_id']} by {dstr(vt)}",
                                    quantity=float(mm["min_volume"]), penalty="volume shortfall invoiced at 5% of unpurchased value", status=stt,
                                    resolved_at=pd.NaT if stt == "open" else vt, meta={"buyer_side": True})
                # supplier price hold for the term
                inc = self.cat[(self.cat.supplier_id == s) & (self.cat.price_valid_from > vf) & (self.cat.price_valid_from <= min(vt, self.end)) &
                               self.cat._why.isin(["january_resin_increase", "price_spike"])]
                stt = "breached" if len(inc) else ("open" if vt > self.end else "fulfilled")
                self.add_commitment(decision_key=None, negotiation_key=n["key"], counterparty_type="supplier", counterparty_id=s, made_by_role="supplier_account_manager",
                                    made_at=n["concluded_at"], due_date=vt, text=f"{self.sup(s)} commits to hold contract prices under {n['contract_id']} until {dstr(vt)}",
                                    quantity=None, penalty=self._penalty(s, vf), status=stt,
                                    resolved_at=inc.price_valid_from.min() if len(inc) else (pd.NaT if stt == "open" else vt), meta={})
            elif n["topic"] == "capacity_reservation":
                y = n["meta"]["year"]
                po = self.po[(self.po.supplier_id == s) & (self.po.promised_at >= D(f"{y}-11-01")) & (self.po.promised_at <= D(f"{y}-12-31"))]
                late = po[(po.received_at > po.promised_at + pd.Timedelta(days=3)) | (po.received_at.isna() & (po.promised_at < self.end))]
                due = D(f"{y}-12-31")
                stt = "breached" if len(late) else ("open" if due >= self.end else "fulfilled")
                if y == 2025 and not len(late[late.received_at.notna()]):
                    stt = "open"
                self.add_commitment(decision_key=None, negotiation_key=n["key"], counterparty_type="supplier", counterparty_id=s, made_by_role="supplier_account_manager",
                                    made_at=n["concluded_at"], due_date=due, text=f"{self.sup(s)} commits to deliver all Nov-Dec {y} POs on time from reserved capacity",
                                    quantity=float(self.pol[self.pol.rm_po_id.isin(po.rm_po_id)].quantity_ordered.sum()), penalty=self._penalty(s, due),
                                    status=stt, resolved_at=(late.received_at.min() if len(late) and late.received_at.notna().any() else (pd.NaT if stt == "open" else due)),
                                    meta={"year": y})
            elif n["topic"] == "late_delivery_credit" and n["outcome"] != "no_agreement":
                due = n["concluded_at"] + pd.Timedelta(days=45)
                stt = "open" if due > self.end else ("fulfilled" if self.rng.random() < 0.75 else "breached")
                self.add_commitment(decision_key=None, negotiation_key=n["key"], counterparty_type="supplier", counterparty_id=s, made_by_role="supplier_account_manager",
                                    made_at=n["concluded_at"], due_date=due, text=f"{self.sup(s)} commits to issue the agreed credit note ({n['final_terms']}) by {dstr(due)}",
                                    quantity=None, penalty="none", status=stt, resolved_at=pd.NaT if stt == "open" else due - pd.Timedelta(days=int(self.rng.integers(1, 20))),
                                    meta={})
            elif n["topic"] == "price_increase_rollback" and pd.notna(n["concluded_at"]):
                ct = self.contracts[self.contracts.contract_id == n["contract_id"]]
                vt = ct.valid_to.iat[0] if len(ct) else n["concluded_at"] + pd.Timedelta(days=180)
                inc = self.cat[(self.cat.rm_id == n["rm_id"]) & (self.cat.price_valid_from > n["concluded_at"]) & (self.cat.price_valid_from <= min(vt, self.end)) &
                               (self.cat._why == "price_spike")]
                stt = "breached" if len(inc) else ("open" if vt > self.end else "fulfilled")
                self.add_commitment(decision_key=n.get("decision_key"), negotiation_key=n["key"], counterparty_type="supplier", counterparty_id=s,
                                    made_by_role="supplier_account_manager", made_at=n["concluded_at"], due_date=vt,
                                    text=f"{self.sup(s)} commits to hold {n['rm_id']} at the negotiated price until {dstr(vt)}", quantity=None,
                                    penalty=self._penalty(s, n["concluded_at"]), status=stt,
                                    resolved_at=inc.price_valid_from.min() if len(inc) else (pd.NaT if stt == "open" else vt), meta={})
        # order confirmations from the small-PO supplier (promise keeping depends on PO size - planted)
        s = self.pat["small_po_supplier"]
        po = self.po[(self.po.supplier_id == s) & (self.po.status != "cancelled")]
        po = po.sample(n=min(len(po), 45), random_state=int(self.rng.integers(1 << 30))).sort_values("ordered_at")
        for p in po.itertuples():
            stt, res = self._po_commit_status(p.rm_po_id, p.ordered_at + DAY, p.promised_at)
            self.add_commitment(decision_key=None, negotiation_key=None, counterparty_type="supplier", counterparty_id=s, made_by_role="supplier_account_manager",
                                made_at=p.ordered_at + DAY, due_date=p.promised_at,
                                text=f"{self.sup(s)} order confirmation: {p.rm_po_id} to arrive by {dstr(p.promised_at)}",
                                quantity=float(self.pol[self.pol.rm_po_id == p.rm_po_id].quantity_ordered.sum()), penalty=self._penalty(s, p.ordered_at),
                                status=stt, resolved_at=res, meta={"po_id": p.rm_po_id})

    def _penalty(self, s, d):
        c = self.contract_at(s, d)
        return c.penalty_clause if c is not None else "none"

    # ------------------------------------------------------------ context dependency + lessons + rationale
    def finalize_decisions(self):
        df = pd.DataFrame([{"key": d["key"], "type": d["decision_type"], "sup": d["meta"].get("supplier_id"), "rm": d["meta"].get("rm_id"),
                            "outcome": d["outcome_label"], "t": d["decided_at"]} for d in self.decisions])
        sig = {d["key"]: (bool(d["meta"].get("q4")), bool(d["meta"].get("shock")), d["meta"].get("cause") not in (None, "base"))
               for d in self.decisions}
        df["sig"] = df.key.map(sig)
        mixed = set()
        for col in ("sup", "rm"):
            for _, g in df.dropna(subset=[col]).sort_values("t").groupby([col, "type"]):
                succ = g[g.outcome == "success"]
                bad = g[g.outcome != "success"]
                for b_ in bad.itertuples():
                    prior = succ[(succ.t < b_.t) & (succ.sig != b_.sig)]
                    if len(prior):
                        mixed.add(b_.key)
                        mixed.add(prior.key.iat[-1])
        evk = {e["key"]: e for e in self.events}
        for d in self.decisions:
            m = d["meta"]
            bad_luck = d["outcome_label"] == "failed" and m.get("argmin") == d["decision_type"]
            d["context_dependent"] = int(d["key"] in mixed or bad_luck)
            factors = []
            if m.get("q4"):
                factors.append("Q4 peak season")
            if m.get("shock"):
                factors.append("active country shock")
            if m.get("cause") and m.get("cause") not in ("base", None):
                factors.append(f"root cause {m['cause']}")
            if m.get("supplier_id") == self.pat["q4_slip_supplier"]:
                factors.append("supplier with year-end capacity history")
            if m.get("plant_id") == self.pat["expedite_cost_plant"] and d["decision_type"] == "expedite":
                factors.append(f"expedite into {m['plant_id']} (premium lane)")
            if bad_luck:
                factors.append("lowest-cost estimate at decision time; realised supply worse than notified")
            d["context_factors"] = "; ".join(factors)
            d["lesson"] = self.lesson(d, evk[d["event_key"]])
            d["rationale"] = self.rationale(d)

    def rationale(self, d):
        opts = sorted(d["options"], key=lambda o: o["est_cost"])
        ch = next((o for o in opts if o["option"] == d["decision_type"]), None)
        alts = [o for o in opts if o["option"] != d["decision_type"] and o.get("feasible", True)][:2]
        s = f"Chose {d['decision_type'].replace('_', ' ')}"
        if ch:
            s += f" (est. cost {ch['est_cost']:,.0f}, {ch['est_stockout_days']} stockout days, {ch['risk']} risk)"
        if alts:
            s += " over " + " and ".join(f"{a['option'].replace('_', ' ')} ({a['est_cost']:,.0f} / {a['est_stockout_days']}d)" for a in alts)
        inf = [o["option"].replace("_", " ") for o in d["options"] if not o.get("feasible", True)]
        if inf:
            s += f"; {', '.join(inf)} could not land in time"
        if d["meta"].get("side") == "rm" and d["meta"].get("belief_eta") is not None and d["meta"]["belief_eta"] == d["meta"]["belief_eta"]:
            s += f"; plan assumed supplier ETA {dstr(self.day(d['meta']['belief_eta']))}"
        return s + "."

    def lesson(self, d, ev):
        m, o = d["meta"], d["outcome_label"]
        t = d["decision_type"].replace("_", " ")
        sup = self.sup(m["supplier_id"]) if m.get("supplier_id") else "the supplier"
        if o == "success":
            base = f"{t.capitalize()} worked: actual {d['actual_stockout_days']} stockout days vs {d['expected_stockout_days']} planned."
            if m.get("q4"):
                base += " Worked even in Q4 because the supplier's first revised ETA held."
            return base
        why = []
        if m.get("q4") and d["decision_type"] in ("expedite", "accept_delay"):
            why.append(f"Nov-Dec ETA notices from {sup} understated the slip; the first revised date was optimistic")
        if m.get("shock"):
            why.append("the alternate lane was hit by the same country disruption")
        if m.get("plant_id") == self.pat["expedite_cost_plant"] and d["decision_type"] == "expedite":
            why.append(f"premium freight into {m['plant_id']} costs about twice the network average")
        if d["decision_type"] == "switch_supplier" and m.get("side") == "rm":
            why.append("the alternate source also shipped late")
        if not why:
            why.append(f"supply arrived later than notified ({d['actual_stockout_days']} vs {d['expected_stockout_days']} stockout days)")
        verb = "failed" if o == "failed" else "only partly worked"
        return f"{t.capitalize()} {verb}: " + "; ".join(why) + ". Next time pad the supplier ETA and check alternates earlier."

    # ------------------------------------------------------------ ids + write
    def assign_and_write(self):
        ctx = self.ctx
        evs = sorted(self.events, key=lambda e: (e["start"], e["key"]))
        eid = {e["key"]: f"EVT{i + 1:05d}" for i, e in enumerate(evs)}
        decs = sorted(self.decisions, key=lambda d: (d["decided_at"], d["key"]))
        did = {d["key"]: f"DEC{i + 1:05d}" for i, d in enumerate(decs)}
        negs = sorted(self.negotiations, key=lambda n: (n["started_at"], n["key"]))
        nid = {n["key"]: f"NEG{i + 1:04d}" for i, n in enumerate(negs)}
        cms = sorted(self.commitments, key=lambda c: (pd.Timestamp(c["made_at"]), c["key"]))
        cid = {c["key"]: f"CMT{i + 1:05d}" for i, c in enumerate(cms)}
        fix = lambda xs: [nid.get(x, x) for x in xs]
        ev_df = pd.DataFrame([{"event_id": eid[e["key"]], "event_type": e["event_type"], "title": e["title"],
                               "start_date": e["start"].normalize(), "end_date": e["end"].normalize(), "detected_at": e["detected"].normalize(),
                               "severity": int(min(5, max(1, e["severity"]))), "root_cause_code": e["root_cause_code"], "root_cause_text": e["root_cause_text"],
                               "origin_entity_type": e["origin_type"], "origin_entity_id": e["origin_id"], "anchor_type": e["anchor_type"],
                               "anchor_ids": json.dumps(e["anchor_ids"])} for e in evs])
        imp = pd.DataFrame([{"event_id": eid[e["key"]], "entity_type": a, "entity_id": b, "impact_metric": c, "impact_value": float(v), "unit": u}
                            for e in evs for a, b, c, v, u in e["impacts"]])
        # similar_to: same type and same origin, not already linked
        linked = {(a, b) for a, b, _ in self.links}
        by = defaultdict(list)
        for e in evs:
            by[(e["event_type"], e["origin_id"])].append(e)
        for k, g in by.items():
            for a, b in zip(g, g[1:]):
                if (b["key"], a["key"]) not in linked and len(g) <= 12:
                    self.links.append((b["key"], a["key"], "similar_to"))
        # caused: shocks / pattern events -> episode events
        for e in evs:
            m = e["meta"]
            if m.get("kind") != "episode":
                continue
            src = None
            if m.get("shock_id") in self.shock_event:
                src = self.shock_event[m["shock_id"]]
            elif m["reason"] == "peak_season_capacity" and self.day(m["t_first"]).year in self.q4_event:
                src = self.q4_event[self.day(m["t_first"]).year]
            elif m["reason"] == "allocation_cut" and self.alloc_event:
                src = min(self.alloc_event.values(), key=lambda k: abs((self._event_by_key(k)["start"] - e["start"]).days))
            elif m["reason"] == "lead_time_extension" and getattr(self, "creep_event", None):
                src = self.creep_event
            if src:
                self.links.append((src, e["key"], "caused"))
            # routine delay events on the same RM/plant just before an episode were superseded by it
            for r in evs:
                if r["meta"].get("kind") == "routine" and r["meta"].get("rm_id") == m["rm_id"] and r["meta"].get("plant_id") in m["plants"] \
                        and 0 <= (e["detected"] - r["detected"]).days <= 30:
                    self.links.append((r["key"], e["key"], "superseded_by"))
        lk = pd.DataFrame([{"src_event_id": eid[a], "dst_event_id": eid[b], "link_type": t} for a, b, t in self.links
                           if a in eid and b in eid and a != b]).drop_duplicates()
        neg_df = pd.DataFrame([{"negotiation_id": nid[n["key"]], "supplier_id": n["supplier_id"], "rm_id": n.get("rm_id"),
                                "started_at": pd.Timestamp(n["started_at"]).normalize(),
                                "concluded_at": pd.Timestamp(n["concluded_at"]).normalize() if pd.notna(n["concluded_at"]) else pd.NaT,
                                "topic": n["topic"], "our_ask": n["our_ask"], "their_offer": n["their_offer"], "concessions_json": n["concessions_json"],
                                "final_terms": n["final_terms"], "outcome": n["outcome"], "contract_id": n.get("contract_id")} for n in negs])
        for c in ("rm_id", "contract_id"):
            neg_df[c] = neg_df[c].astype("string")
        dec_df = pd.DataFrame([{"decision_id": did[d["key"]], "event_id": eid[d["event_key"]], "decided_at": d["decided_at"],
                                "decided_by_role": d["role"], "decision_type": d["decision_type"],
                                "options_considered_json": json.dumps([{k: v for k, v in o.items() if k != "feasible"} | {"feasible": bool(o.get("feasible", True))} for o in d["options"]]),
                                "chosen_option": d["chosen_option"], "rationale_text": d["rationale"],
                                "expected_cost": d["expected_cost"], "expected_stockout_days": d["expected_stockout_days"],
                                "actual_cost": d["actual_cost"], "actual_stockout_days": d["actual_stockout_days"],
                                "outcome_label": d["outcome_label"], "outcome_assessed_at": d["outcome_assessed_at"],
                                "lesson_text": d["lesson"], "footprint_ids": json.dumps(fix(d["footprint"])),
                                "action_cost": d["action_cost"], "context_dependent": d["context_dependent"],
                                "context_factors": d["context_factors"]} for d in decs])
        cm_df = pd.DataFrame([{"commitment_id": cid[c["key"]], "decision_id": did.get(c["decision_key"]) if c["decision_key"] else None,
                               "negotiation_id": nid.get(c["negotiation_key"]) if c["negotiation_key"] else None,
                               "counterparty_type": c["counterparty_type"], "counterparty_id": c["counterparty_id"], "made_by_role": c["made_by_role"],
                               "made_at": pd.Timestamp(c["made_at"]).normalize(), "commitment_text": c["text"], "quantity": c["quantity"],
                               "due_date": pd.Timestamp(c["due_date"]).normalize(), "penalty_or_credit": c["penalty"], "status": c["status"],
                               "resolved_at": pd.Timestamp(c["resolved_at"]).normalize() if pd.notna(c["resolved_at"]) else pd.NaT} for c in cms])
        # commitments made after the window are out of scope; open ones keep resolved_at empty
        cm_df = cm_df[cm_df.made_at <= self.end].reset_index(drop=True)
        cm_df.loc[cm_df.status == "open", "resolved_at"] = pd.NaT
        early = cm_df.resolved_at.notna() & (cm_df.resolved_at < cm_df.made_at)
        cm_df.loc[early, "resolved_at"] = cm_df.loc[early, "made_at"]
        cm_df.loc[cm_df.resolved_at > self.end, ["status", "resolved_at"]] = ["open", pd.NaT]
        for c in ("decision_id", "negotiation_id"):
            cm_df[c] = cm_df[c].astype("string")
        ctx.write(ev_df, "disruption_events")
        ctx.write(imp, "event_impacts")
        ctx.write(lk, "event_links")
        ctx.write(neg_df, "negotiations")
        ctx.write(dec_df, "decisions")
        ctx.write(cm_df, "commitments")
        # remap demand shock keys to event ids
        dm = ctx.read("demand_raw").copy()
        dm["demand_shock_event_id"] = dm.demand_shock_event_id.map(lambda k: eid[self.shock_key_event[k]] if pd.notna(k) else pd.NA).astype("string")
        ctx.write(dm, "product_demand_weekly")
        # narrative support
        meta = {eid[e["key"]]: {k: v for k, v in e["meta"].items()} | {"decisions": [did[x] for x in e.get("decisions", []) if x in did]} for e in evs}
        dmeta = {did[d["key"]]: d["meta"] | {"negotiation_id": nid.get(d["meta"].get("negotiation_key"))} for d in decs}
        cmeta = {cid[c["key"]]: c["meta"] for c in cms}
        ctx.save_json({"events": meta, "decisions": dmeta, "commitments": cmeta,
                       "negotiations": {nid[n["key"]]: n.get("meta", {}) for n in negs}}, "memory_meta.json")
        return ev_df, dec_df, cm_df, neg_df, lk


# ------------------------------------------------------------------ scorecard
def compute_scorecard(ctx: Ctx) -> pd.DataFrame:
    src, r = ctx.src(), ctx.read
    end = D(ctx.cfg["window_end"])
    fg = src["purchase_orders"]
    fl = src["purchase_order_lines"].groupby("purchase_order_id")[["quantity_ordered", "quantity_received"]].sum()
    fg = fg.join(fl, on="purchase_order_id").assign(due=lambda x: x.expected_at)
    rp = r("rm_purchase_orders")
    rl = r("rm_purchase_order_lines").groupby("rm_po_id")[["quantity_ordered", "quantity_received"]].sum()
    rp = rp.join(rl, on="rm_po_id").assign(due=lambda x: x.promised_at).rename(columns={"rm_po_id": "purchase_order_id"})
    allpo = pd.concat([fg[["purchase_order_id", "supplier_id", "due", "received_at", "status", "quantity_ordered", "quantity_received"]],
                       rp[["purchase_order_id", "supplier_id", "due", "received_at", "status", "quantity_ordered", "quantity_received"]]])
    allpo = allpo[(allpo.status != "cancelled") & (allpo.due >= D(ctx.cfg["window_start"])) & (allpo.due <= end)].copy()
    allpo["month"] = allpo.due.dt.to_period("M").astype(str)
    rec = allpo[allpo.status.isin(["received", "partial"])].copy()
    rec["otif"] = ((rec.received_at <= rec.due) & (rec.status == "received")).astype(float)
    rec["delay"] = (rec.received_at - rec.due).dt.days.astype(float)
    g1 = allpo.groupby(["supplier_id", "month"]).size().rename("po_count")
    g2 = rec.groupby(["supplier_id", "month"]).agg(otif_rate=("otif", "mean"), avg_delay_days=("delay", "mean"),
                                                  q_r=("quantity_received", "sum"), q_o=("quantity_ordered", "sum"))
    sc = pd.concat([g1, g2], axis=1).reset_index()
    sc["fill_rate"] = sc.q_r / sc.q_o
    # quality: RM scrap on supplier lots / RM received qty in the month
    mv = r("rm_inventory_movements")
    s = mv[(mv.movement_type == "scrap") & mv.rm_purchase_order_line_id.notna()]
    s = s.merge(r("rm_purchase_order_lines")[["rm_purchase_order_line_id", "rm_po_id"]], on="rm_purchase_order_line_id")
    s["supplier_id"] = s.rm_po_id.map(r("rm_purchase_orders").set_index("rm_po_id").supplier_id)
    s["month"] = s.movement_at.dt.to_period("M").astype(str)
    sq = (-s.groupby(["supplier_id", "month"]).quantity_change.sum()).rename("scrap")
    rr = r("rm_inventory_movements")
    rc = rr[(rr.movement_type == "receipt") & rr.rm_purchase_order_line_id.notna()].merge(
        r("rm_purchase_order_lines")[["rm_purchase_order_line_id", "rm_po_id"]], on="rm_purchase_order_line_id")
    rc["supplier_id"] = rc.rm_po_id.map(r("rm_purchase_orders").set_index("rm_po_id").supplier_id)
    rc["month"] = rc.movement_at.dt.to_period("M").astype(str)
    rq = rc.groupby(["supplier_id", "month"]).quantity_change.sum().rename("rmrec")
    sc = sc.merge(sq, on=["supplier_id", "month"], how="left").merge(rq, on=["supplier_id", "month"], how="left")
    sc["quality_ppm"] = np.where(sc.rmrec.fillna(0) > 0, sc.scrap.fillna(0) / sc.rmrec * 1e6, 0.0).round(1)
    cm = r("commitments")
    cm = cm[cm.counterparty_type == "supplier"].assign(month=lambda x: x.made_at.dt.to_period("M").astype(str))
    cmg = cm.groupby(["counterparty_id", "month"]).agg(commitments_made=("commitment_id", "size"),
                                                       commitments_kept=("status", lambda x: int((x == "fulfilled").sum())))
    cmg.index.names = ["supplier_id", "month"]
    sc = sc.merge(cmg.reset_index(), on=["supplier_id", "month"], how="outer")
    sc = sc[sc.month.between(ctx.cfg["window_start"][:7], ctx.cfg["window_end"][:7])]
    sc["po_count"] = sc.po_count.fillna(0).astype(int)
    sc["commitments_made"] = sc.commitments_made.fillna(0).astype(int)
    sc["commitments_kept"] = sc.commitments_kept.fillna(0).astype(int)
    sc["quality_ppm"] = sc.quality_ppm.fillna(0.0)
    for c in ("otif_rate", "fill_rate"):
        sc[c] = sc[c].round(4)
    sc["avg_delay_days"] = sc.avg_delay_days.round(2)
    return sc[["supplier_id", "month", "po_count", "otif_rate", "avg_delay_days", "fill_rate", "quality_ppm",
               "commitments_made", "commitments_kept"]].sort_values(["supplier_id", "month"]).reset_index(drop=True)


# ------------------------------------------------------------------ planted patterns
def planted_patterns(ctx: Ctx) -> list[dict]:
    pat, pp = ctx.load_json("pattern_plan.json"), ctx.cfg["patterns"]
    rmx = ctx.read("raw_materials").set_index("rm_id")
    S = ctx.src()["suppliers"].set_index("supplier_id").supplier_name
    return [
        {"name": "q4_slip_supplier", "entities": {"supplier_id": pat["q4_slip_supplier"]},
         "description": f"{S[pat['q4_slip_supplier']]} ({pat['q4_slip_supplier']}) slips RM deliveries 7-14 days every November-December.",
         "tables": ["rm_purchase_orders", "rm_po_revisions"], "signal": "delay (received_at - promised_at) in Nov-Dec vs other months"},
        {"name": "feb_port_country", "entities": {"country_code": pat["feb_port_country"]},
         "description": f"Port congestion hits RM shipments from {pat['feb_port_country']} every February (+5-12 days).",
         "tables": ["rm_purchase_orders", "rm_po_revisions", "disruption_events"], "signal": "delay of POs promised in Feb vs other months, same country"},
        {"name": "price_spike_shortage", "entities": {"rm_id": pat["price_spike_rm"], "supplier_id": pat["price_spike_supplier"], "spikes": pat["price_spike_dates"]},
         "description": f"Single-source {rmx.at[pat['price_spike_rm'], 'rm_name']} ({pat['price_spike_rm']}) goes on allocation 20-45 days after each price spike.",
         "tables": ["rm_supplier_catalog", "rm_purchase_orders", "rm_po_revisions"], "signal": "late (>7d) rate of POs promised 20-65 days after a spike vs baseline"},
        {"name": "small_po_supplier", "entities": {"supplier_id": pat["small_po_supplier"]},
         "description": f"{S[pat['small_po_supplier']]} ({pat['small_po_supplier']}) keeps delivery promises on small POs but misses on large ones (>1.5x MOQ).",
         "tables": ["rm_purchase_orders", "rm_purchase_order_lines", "rm_supplier_catalog", "commitments"], "signal": "late rate large vs small POs"},
        {"name": "expedite_cost_plant", "entities": {"plant_id": pat["expedite_cost_plant"]},
         "description": f"Expedites into {pat['expedite_cost_plant']} cost ~{pp['expedite_cost_plant']['multiplier']}x the network average.",
         "tables": ["decisions", "rm_purchase_order_lines"], "signal": "expedite action_cost / expedited line value by plant"},
        {"name": "q4_optimistic_notices", "entities": {"months": [10, 11, 12]},
         "description": "Supplier ETA notices in Q4 understate the slip: first revised ETA is usually followed by a second slip.",
         "tables": ["rm_po_revisions"], "signal": "share of slipped POs with >=2 supplier revisions, Q4 vs rest"},
        {"name": "secondary_quality_supplier", "entities": {"supplier_id": pat["secondary_quality_supplier"]},
         "description": f"Lots from {S[pat['secondary_quality_supplier']]} ({pat['secondary_quality_supplier']}) fail incoming QC ~10x more often.",
         "tables": ["rm_inventory_movements", "rm_purchase_order_lines", "supplier_scorecard_monthly"], "signal": "QC reject (scrap) rate per received line"},
        {"name": "lead_time_creep_supplier", "entities": {"supplier_id": pat["lead_time_creep_supplier"], "from": pp["lead_time_creep_supplier"]["from"]},
         "description": f"{S[pat['lead_time_creep_supplier']]} ({pat['lead_time_creep_supplier']}) actual lead times grow ~25% from {pp['lead_time_creep_supplier']['from']}.",
         "tables": ["rm_purchase_orders"], "signal": "received_at - ordered_at before vs after"},
        {"name": "january_resin_increase", "entities": {"rm_category": "resin"},
         "description": "Resin RM prices step up every 1 January (index-linked).",
         "tables": ["rm_supplier_catalog", "raw_materials"], "signal": "share of resin price increases effective in January"},
        {"name": "summer_labor_plant", "entities": {"plant_id": pat["summer_labor_plant"], "months": pp["summer_labor_plant"]["months"]},
         "description": f"Production delays at {pat['summer_labor_plant']} in July-August are dominated by labor shortages.",
         "tables": ["production_runs"], "signal": "share of 'labor' among delayed runs, plant Jul-Aug vs rest"},
        {"name": "promo_underforecast_family", "entities": {"brand_family": pat["promo_underforecast_family"]},
         "description": f"Promo weeks of brand family {pat['promo_underforecast_family']} are under-forecast by ~35%.",
         "tables": ["product_demand_weekly", "products_ext"], "signal": "signed forecast error in promo weeks, family vs other families"},
    ]


def pattern_tests(ctx: Ctx, pats: list[dict]) -> dict:
    r = ctx.read
    po, pol, rev = r("rm_purchase_orders"), r("rm_purchase_order_lines"), r("rm_po_revisions")
    S = ctx.src()["suppliers"].set_index("supplier_id")
    rec = po[po.received_at.notna()].copy()
    rec["delay"] = (rec.received_at - rec.promised_at).dt.days
    rec["m"] = rec.promised_at.dt.month
    out = {}

    def res(name, effect, p, metric, ok):
        out[name] = {"effect": effect, "p_value": float(p), "metric": metric, "recovered": bool(ok and p < 0.01)}

    for p in pats:
        n, e = p["name"], p["entities"]
        if n == "q4_slip_supplier":
            x = rec[rec.supplier_id == e["supplier_id"]]
            a, b = x[x.m.isin([11, 12])].delay, x[~x.m.isin([11, 12])].delay
            res(n, round(a.mean() - b.mean(), 2), stats.mannwhitneyu(a, b, alternative="greater").pvalue, "mean delay diff (days) Nov-Dec vs rest", a.mean() - b.mean() >= 5)
        elif n == "feb_port_country":
            x = rec[rec.supplier_id.map(S.country_code) == e["country_code"]]
            a, b = x[x.m == 2].delay, x[x.m != 2].delay
            res(n, round(a.mean() - b.mean(), 2), stats.mannwhitneyu(a, b, alternative="greater").pvalue, "mean delay diff (days) Feb vs rest", a.mean() - b.mean() >= 3)
        elif n == "price_spike_shortage":
            x = rec[rec.rm_po_id.isin(pol[pol.rm_id == e["rm_id"]].rm_po_id)]
            win = np.zeros(len(x), bool)
            for sd in e["spikes"]:
                d = (x.promised_at - D(sd)).dt.days
                win |= ((d >= 20) & (d <= 65)).to_numpy()
            late = (x.delay > 7).to_numpy()
            t = [[late[win].sum(), (~late[win]).sum()], [late[~win].sum(), (~late[~win]).sum()]]
            ra, rb = late[win].mean() if win.any() else 0, late[~win].mean()
            res(n, round(ra / max(rb, 1e-3), 2), stats.fisher_exact(t, alternative="greater")[1], "late>7d rate ratio post-spike vs baseline", ra > 2 * rb)
        elif n == "small_po_supplier":
            cat = r("rm_supplier_catalog")
            moq = cat[cat.supplier_id == e["supplier_id"]].groupby("rm_id").moq.first()
            x = rec[rec.supplier_id == e["supplier_id"]].merge(pol, on="rm_po_id")
            x["large"] = x.quantity_ordered > 1.5 * x.rm_id.map(moq)
            x["late"] = x.delay > 0
            a, b = x[x.large].late, x[~x.large].late
            t = [[a.sum(), (~a).sum()], [b.sum(), (~b).sum()]]
            res(n, round(a.mean() - b.mean(), 3), stats.fisher_exact(t, alternative="greater")[1], "late-rate diff large vs small POs", a.mean() - b.mean() > 0.3)
        elif n == "expedite_cost_plant":
            dec = r("decisions")
            ex = dec[dec.decision_type == "expedite"].copy()
            ex["po"] = ex.footprint_ids.apply(lambda s: json.loads(s)[0])
            val = (pol.quantity_ordered * pol.unit_cost).groupby(pol.rm_po_id).sum()
            ex["rate"] = ex.action_cost / ex.po.map(val)
            ex["plant"] = ex.po.map(po.set_index("rm_po_id").plant_id)
            ex = ex[np.isfinite(ex.rate)]
            a, b = ex[ex.plant == e["plant_id"]].rate, ex[ex.plant != e["plant_id"]].rate
            ok = len(a) >= 3
            res(n, round(a.mean() / b.mean(), 2) if ok else None, stats.mannwhitneyu(a, b, alternative="greater").pvalue if ok else 1.0,
                "expedite cost/value ratio plant vs others", ok and a.mean() / b.mean() > 1.6)
        elif n == "q4_optimistic_notices":
            rv = rev[~rev.reason_code.isin(["expedite", "expedite_missed"])]
            cnt = rv.groupby("rm_po_id").size()
            pm = po.set_index("rm_po_id").promised_at.dt.month.reindex(cnt.index)
            q4 = pm.isin([10, 11, 12])
            multi = cnt >= 2
            t = [[(multi & q4).sum(), (~multi & q4).sum()], [(multi & ~q4).sum(), (~multi & ~q4).sum()]]
            res(n, round(multi[q4].mean() / multi[~q4].mean(), 2), stats.fisher_exact(t, alternative="greater")[1], "multi-revision share ratio Q4 vs rest", multi[q4].mean() > 1.5 * multi[~q4].mean())
        elif n == "secondary_quality_supplier":
            mv = r("rm_inventory_movements")
            ep_lines = {x.get("scrap_line_id") for x in ctx.load_json("rm_ops_plan.json")["episodes"]}
            sc = mv[(mv.movement_type == "scrap") & mv.rm_purchase_order_line_id.notna() & ~mv.rm_purchase_order_line_id.isin(ep_lines)]
            rc = mv[(mv.movement_type == "receipt") & mv.rm_purchase_order_line_id.notna()]
            lsup = pol.set_index("rm_purchase_order_line_id").rm_po_id.map(po.set_index("rm_po_id").supplier_id)
            rej = rc.rm_purchase_order_line_id.isin(set(sc.rm_purchase_order_line_id))
            is_s = rc.rm_purchase_order_line_id.map(lsup) == e["supplier_id"]
            t = [[(rej & is_s).sum(), (~rej & is_s).sum()], [(rej & ~is_s).sum(), (~rej & ~is_s).sum()]]
            ra, rb = rej[is_s].mean(), rej[~is_s].mean()
            res(n, round(ra / max(rb, 1e-4), 2), stats.fisher_exact(t, alternative="greater")[1], "QC reject rate ratio supplier vs others", ra > 4 * rb)
        elif n == "lead_time_creep_supplier":
            x = rec[(rec.supplier_id == e["supplier_id"]) & (rec.expected_at > rec.ordered_at)].copy()  # excludes same-day spot buys
            x["ltd"] = (x.received_at - x.ordered_at).dt.days
            a, b = x[x.ordered_at >= D(e["from"])].ltd, x[x.ordered_at < D(e["from"])].ltd
            res(n, round(a.mean() / b.mean() - 1, 3), stats.mannwhitneyu(a, b, alternative="greater").pvalue, "relative actual lead-time increase", a.mean() / b.mean() - 1 > 0.12)
        elif n == "january_resin_increase":
            cat = r("rm_supplier_catalog").merge(ctx.read("catalog_meta"), on="catalog_line_id")
            rmx = r("raw_materials").set_index("rm_id").rm_category
            cat = cat.sort_values(["rm_id", "supplier_id", "price_valid_from"])
            cat["prev"] = cat.groupby(["rm_id", "supplier_id"]).unit_price.shift()
            inc = cat[(cat.unit_price > cat.prev) & (cat.rm_id.map(rmx) == "resin") & (cat.price_valid_from >= D("2023-01-01"))]
            k, nn = int((inc.price_valid_from.dt.month == 1).sum()), len(inc)
            res(n, round(k / max(nn, 1), 3), stats.binomtest(k, nn, 1 / 12, alternative="greater").pvalue, "share of resin price increases effective in January", k / max(nn, 1) > 0.4)
        elif n == "summer_labor_plant":
            pr = r("production_runs")
            late = pr[pr.delay_days > 0].dropna(subset=["delay_reason_code"])
            grp = (late.plant_id == e["plant_id"]) & late.actual_start.dt.month.isin(e["months"])
            lab = late.delay_reason_code == "labor"
            t = [[(lab & grp).sum(), (~lab & grp).sum()], [(lab & ~grp).sum(), (~lab & ~grp).sum()]]
            res(n, round(lab[grp].mean() - lab[~grp].mean(), 3), stats.fisher_exact(t, alternative="greater")[1], "labor share diff (plant Jul-Aug vs rest)", lab[grp].mean() - lab[~grp].mean() > 0.3)
        elif n == "promo_underforecast_family":
            dm = r("product_demand_weekly")
            fam = r("products_ext").set_index("product_id").brand_family
            x = dm[(dm.promo_flag == 1) & (dm.actual_demand_units > 0)].copy()
            x["err"] = (x.forecast_units - x.actual_demand_units) / x.actual_demand_units
            a, b = x[x.product_id.map(fam) == e["brand_family"]].err, x[x.product_id.map(fam) != e["brand_family"]].err
            res(n, round(a.mean() - b.mean(), 3), stats.mannwhitneyu(a, b, alternative="less").pvalue, "signed promo forecast error diff (family vs others)", a.mean() - b.mean() < -0.2)
    return out


def run(ctx: Ctx):
    banner("STAGE 3 - events, decisions, commitments, negotiations, scorecards, patterns")
    b = Builder(ctx)
    b.rm_shortage_events()
    b.shock_events()
    b.pattern_events()
    b.quality_events()
    b.fg_events()
    b.sourcing_change_events()
    b.rm_decisions()
    b.strategic_decisions()
    b.more_negotiations()
    b.build_commitments()
    b.finalize_decisions()
    ev, dec, cm, neg, lk = b.assign_and_write()
    sc = compute_scorecard(ctx)
    ctx.write(sc, "supplier_scorecard_monthly")
    pats = planted_patterns(ctx)
    tests = pattern_tests(ctx, pats)
    for p in pats:
        p["recovery_test"] = tests[p["name"]]
    (ctx.output / "planted_patterns.json").write_text(json.dumps(pats, indent=2, default=str), encoding="utf-8")

    print(f"events {len(ev):,}: {ev.event_type.value_counts().to_dict()}")
    print(f"anchor_type: {ev.anchor_type.value_counts().to_dict()} | links {len(lk)}: {lk.link_type.value_counts().to_dict()}")
    print(f"decisions {len(dec):,}: {dec.decision_type.value_counts().to_dict()}")
    print(f"outcomes: {dec.outcome_label.value_counts().to_dict()} | context-dependent {dec.context_dependent.mean():.1%}")
    print(f"commitments {len(cm):,}: {cm.status.value_counts(normalize=True).round(3).to_dict()}")
    print(f"negotiations {len(neg)}: {neg.topic.value_counts().to_dict()} | scorecard rows {len(sc):,}")
    for k, v in tests.items():
        print(f"  pattern {k:<28} recovered={v['recovered']} effect={v['effect']} p={v['p_value']:.2g}")


if __name__ == "__main__":
    run(cli(__doc__))
