"""Stage 1b - raw-material operations.

production_runs (one per existing make-product FG receipt / open line), RM consumption, a causal
daily RM inventory simulation per (rm, plant) with MRP ordering, supplier delays driven by
reliability / lead time / country shocks / planted patterns, ETA revisions, QC rejects, spot buys,
planner decisions (evaluated on belief, applied, then scored on the realised trajectory), and the
CASCADE back-fill: RM PO slip -> RM below safety -> blocked run -> the existing late FG receipt.
Existing FG rows are never modified; production dates are derived from them.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import gamma

_U_GRID = np.linspace(0, 1, 20001)

from common import Ctx, banner, cat_param, cli, fmt_ids
from gen_structure import transit_days

D = pd.Timestamp
REASONS_BASE = ["supplier_capacity", "production_issue", "carrier_delay", "raw_material_shortage"]
PAD = 200  # days of array padding beyond window end


class World:
    def __init__(self, ctx: Ctx):
        self.d0 = D(ctx.cfg["sim_start"])
        self.end = D(ctx.cfg["window_end"])
        self.T = (self.end - self.d0).days + 1
        self.dates = pd.date_range(self.d0, periods=self.T + PAD)
        self.month = self.dates.month.to_numpy()
        self.weekday = self.dates.weekday.to_numpy()
        self.year = self.dates.year.to_numpy()

    def day(self, s):
        if isinstance(s, pd.Series):
            return (s - self.d0).dt.days
        return (D(s) - self.d0).days

    def date(self, i):
        return self.d0 + pd.Timedelta(days=int(i))


# ------------------------------------------------------------------ production runs & consumption
def build_runs(ctx: Ctx, W: World, src, pext, plants) -> pd.DataFrame:
    rng = ctx.rng("runs")
    P = src["products"]
    L = src["purchase_order_lines"].merge(src["purchase_orders"], on="purchase_order_id")
    mk = pext[pext.sourcing_mode == "make"][["product_id", "plant_id"]]
    L = L.merge(mk, on="product_id")
    L = L[(L.status != "cancelled") & ((L.quantity_received > 0) | (L.status == "open"))].copy()
    cat = P.set_index("product_id").category
    prod_days = {p: int(rng.integers(cat_param(ctx.cfg, "prod_days", cat[p])[0], cat_param(ctx.cfg, "prod_days", cat[p])[1] + 1))
                 for p in mk.product_id}
    wreg = src["warehouses"].set_index("warehouse_id").region
    preg = plants.set_index("plant_id").region
    pairs = L[["plant_id", "warehouse_id"]].drop_duplicates()
    tt = {(r.plant_id, r.warehouse_id): transit_days(ctx.cfg, preg[r.plant_id], wreg[r.warehouse_id]) for r in pairs.itertuples()}
    L["lead"] = L.product_id.map(prod_days) + [tt[(a, b)] for a, b in zip(L.plant_id, L.warehouse_id)]
    L["planned_start"] = L.expected_at - pd.to_timedelta(L.lead, unit="D")
    L["actual_start"] = L.received_at - pd.to_timedelta(L.lead, unit="D")
    L["delay_days"] = (L.actual_start - L.planned_start).dt.days
    runs = pd.DataFrame({
        "plant_id": L.plant_id.astype("string"), "product_id": L.product_id, "purchase_order_line_id": L.purchase_order_line_id,
        "planned_start": L.planned_start, "actual_start": L.actual_start, "planned_qty": L.quantity_ordered,
        "produced_qty": L.quantity_received.where(L.received_at.notna()).astype("Int64"),
        "delay_days": L.delay_days.astype("Int64"), "delay_reason_code": pd.NA, "rm_shortage_rm_id": pd.NA,
        "_lead": L.lead, "_status": L.status, "_warehouse_id": L.warehouse_id,
    }).sort_values(["planned_start", "purchase_order_line_id"]).reset_index(drop=True)
    runs.insert(0, "production_run_id", fmt_ids("PR", len(runs), 7))
    return runs


def bom_lines_at(bom: pd.DataFrame, runs: pd.DataFrame, date_col: str, qty_col: str) -> pd.DataFrame:
    m = runs[["production_run_id", "product_id", "plant_id", date_col, qty_col]].dropna(subset=[date_col, qty_col]) \
        .merge(bom[["bom_id", "product_id", "rm_id", "qty_per_unit", "scrap_pct", "effective_from", "effective_to"]], on="product_id")
    m = m[(m.effective_from <= m[date_col]) & (m.effective_to.isna() | (m.effective_to >= m[date_col]))].copy()
    m["qty"] = (m[qty_col].astype(float) * m.qty_per_unit * (1 + m.scrap_pct)).round(4)
    return m


# ------------------------------------------------------------------ catalog lookup
class Catalog:
    def __init__(self, cat: pd.DataFrame, W: World, suppliers: pd.DataFrame):
        c = cat.copy()
        c["f"] = W.day(c.price_valid_from)
        c["t"] = W.day(c.price_valid_to.fillna(W.end + pd.Timedelta(days=PAD)))
        self.rows = {r: g.sort_values("f").to_dict("records") for r, g in c.groupby("rm_id")}
        self.rel = suppliers.set_index("supplier_id").reliability_score.to_dict()
        self.country = suppliers.set_index("supplier_id").country_code.to_dict()

    def at(self, rm: str, t: int) -> list[dict]:
        return [r for r in self.rows[rm] if r["f"] <= t <= r["t"]]

    def primary(self, rm, t):
        rows = self.at(rm, t)
        pr = [r for r in rows if r["sourcing_rank"] == "primary"]
        return (pr or rows)[0]

    def price(self, rm, sup, t):
        rows = [r for r in self.at(rm, t) if r["supplier_id"] == sup]
        if rows:
            return rows[0]["unit_price"]
        rows = [r for r in self.rows[rm] if r["supplier_id"] == sup]
        return rows[-1]["unit_price"]


# ------------------------------------------------------------------ supply disruption plan
def plan_shocks(ctx: Ctx, W: World, cat: pd.DataFrame, S: pd.DataFrame, pat: dict) -> list[dict]:
    rng = ctx.rng("shocks")
    ro = ctx.cfg["rm_ops"]
    countries = sorted(S[S.supplier_id.isin(cat.supplier_id)].country_code.unique())
    types = ["port_congestion", "customs_backlog", "labor_strike", "severe_weather", "rail_disruption"]
    out = []
    for c in countries:
        for y in (2023, 2024, 2025):
            for _ in range(int(rng.integers(ro["country_shocks_per_year"][0], ro["country_shocks_per_year"][1] + 1))):
                s = D(f"{y}-01-01") + pd.Timedelta(days=int(rng.integers(0, 330)))
                if c == pat["feb_port_country"] and s.month in (1, 2, 3):
                    continue
                ln = int(rng.integers(*ro["country_shock_len"]))
                out.append({"country_code": c, "type": str(rng.choice(types)), "start": W.day(s), "end": W.day(s) + ln,
                            "extra": [int(x) for x in ro["country_shock_extra_delay"]], "planted": False})
    fp = ctx.cfg["patterns"]["feb_port_country"]
    for y in (2023, 2024, 2025):
        out.append({"country_code": pat["feb_port_country"], "type": "port_congestion",
                    "start": W.day(D(f"{y}-02-01")), "end": W.day(D(f"{y}-03-01")) - 1,
                    "extra": [int(x) for x in fp["extra_delay"]], "planted": True})
    out.sort(key=lambda s: s["start"])
    for i, s in enumerate(out, 1):
        s["shock_id"] = f"SHK{i:03d}"
    return out


class DelayModel:
    """Realised supplier delay. Shared uniforms keyed by (supplier, plant, order day) so that lines
    ordered together share their fate (they consolidate into one PO); line-specific effects use a line rng."""

    def __init__(self, ctx: Ctx, W: World, catalog: Catalog, plants: list, shocks: list, pat: dict):
        self.ctx, self.W, self.cat, self.pat = ctx, W, catalog, pat
        sups = sorted(catalog.rel)
        self.si = {s: i for i, s in enumerate(sups)}
        self.pi = {p: i for i, p in enumerate(plants)}
        self.U = ctx.rng("delay_shared").random((len(sups), len(plants), W.T + PAD, 6), dtype=np.float32)
        self.ro, self.pp = ctx.cfg["rm_ops"], ctx.cfg["patterns"]
        self.shocks = defaultdict(list)
        for s in shocks:
            self.shocks[s["country_code"]].append(s)
        lag = self.pp["price_spike_shortage"]["lag_days"]
        self.alloc_windows = []
        r = ctx.rng("alloc_cut")
        for d in pat["price_spike_dates"]:
            st = W.day(D(d)) + int(r.integers(lag[0], lag[1] + 1))
            self.alloc_windows.append((st, st + 45))
        self.creep_from = W.day(D(self.pp["lead_time_creep_supplier"]["from"]))
        g = np.clip(_U_GRID, 1e-4, 1 - 1e-6)
        self.gppf = gamma.ppf(g, self.ro["delay_gamma_shape"])  # unit-scale quantiles, scaled per draw

    def shock_at(self, country, t):
        for s in self.shocks.get(country, []):
            if s["start"] <= t <= s["end"]:
                return s
        return None

    def draw(self, sup, plant, rm, t, promised, lead, qty, moq, lrng):
        u = self.U[self.si[sup], self.pi[plant], min(t, self.W.T + PAD - 1)]
        rel = self.cat.rel[sup]
        pp, ro = self.pp, self.ro
        cause, d = "base", 0
        if sup == self.pat["small_po_supplier"]:
            late = lrng.random() < (pp["small_po_supplier"]["late_prob_large"] if qty > 1.5 * moq else pp["small_po_supplier"]["late_prob_small"])
            cause = "large_po" if late and qty > 1.5 * moq else "base"
        else:
            late = u[0] < (1 - rel)
        if late:
            q = self.gppf[int(float(u[1]) * 20000)]
            d = max(1, int(math.ceil(q * max(1.0, lead * ro["delay_scale_frac_of_lead"]))))
        elif u[0] > 1 - ro["early_prob"]:
            d = -(1 + int(u[1] * 3))
        m = self.W.month[min(promised, len(self.W.month) - 1)]
        if sup == self.pat["q4_slip_supplier"] and m in pp["q4_slip_supplier"]["months"]:
            lo, hi = pp["q4_slip_supplier"]["extra_delay"]
            d, cause = max(d, 0) + lo + int(u[2] * (hi - lo + 1)), "peak_season_capacity"
        sh = self.shock_at(self.cat.country[sup], promised)
        if sh is not None and u[3] < (0.85 if sh["planted"] else 0.7):
            lo, hi = sh["extra"]
            d, cause = max(d, 0) + lo + int(u[4] * (hi - lo + 1)), sh["type"]
            if sh["planted"] or cause == "base":
                pass
        if sup == self.pat["lead_time_creep_supplier"] and t >= self.creep_from:
            add = int(round(pp["lead_time_creep_supplier"]["creep"] * lead * (0.7 + 0.6 * float(u[5]))))
            d, cause = max(d, 0) + add, ("lead_time_extension" if cause == "base" else cause)
        if rm == self.pat["price_spike_rm"]:
            for a, b in self.alloc_windows:
                if a <= promised <= b:
                    lo, hi = pp["price_spike_shortage"]["extra_delay"]
                    d, cause = max(d, 0) + int(lrng.integers(lo, hi + 1)), "allocation_cut"
        return d, cause

    def revisions(self, t_order, promised, final, cause, lrng, q4):
        """ETA notices for a slipped order: list of (notice_day, old, new, reason)."""
        d = final - promised
        if d <= 0 or final <= t_order + 1:
            return []
        # the planted small-PO behaviour must be discovered from data, not read off a reason code
        reason = "supplier_capacity" if cause == "large_po" else (cause if cause != "base" else str(lrng.choice(REASONS_BASE)))
        lo, hi = self.ro["notice_days_before"]
        n1 = min(max(promised - int(lrng.integers(lo, hi + 1)), t_order + 1), final - 1)
        opt = (q4 and lrng.random() < 0.8) or lrng.random() < self.ro["second_revision_prob"]
        if opt and d >= 2:
            f = (1 - self.pp["q4_optimistic_notices"]["optimism"]) if q4 else float(lrng.uniform(0.4, 0.8))
            e1 = promised + max(1, int(round(d * f)))
            if e1 < final and final - 1 > n1:
                n2 = int(lrng.integers(n1 + 1, final))
                return [(n1, promised, e1, reason), (n2, e1, final, reason)]
        return [(n1, promised, final, reason)]


# ------------------------------------------------------------------ orders & pair simulation
@dataclass
class Order:
    lid: int
    rm: str
    plant: str
    sup: str
    qty: float
    ordered: int
    expected: int
    promised: int
    receipt: int
    recv_qty: float
    price: float
    kind: str = "mrp"
    revs: list = field(default_factory=list)
    cancelled: int | None = None
    qc_frac: float = 0.0
    cause: str = "base"
    episode: int | None = None
    fee: float = 0.0

    def belief(self, x: int) -> int:
        eta = self.promised
        for n, _o, nw, _r in self.revs:
            if n <= x:
                eta = nw
        return eta

    def open_at(self, x: int) -> bool:
        return self.cancelled is None and self.receipt > x


def project(oh, t, inbound, ap, H):
    arr = -ap[t:t + H].copy()
    for d, q in inbound:
        if t <= d < t + H:
            arr[d - t] += q
    traj = oh + np.cumsum(arr)
    return int((traj < -1e-9).sum()), float(max(0.0, -traj.min())) if len(traj) else 0.0


class PairSim:
    def __init__(self, env, pi: int, allow_decisions: bool, transfers_out=()):
        self.env, self.pi = env, pi
        self.rm, self.plant = env.pairs[pi]
        self.a, self.ap = env.A[pi], env.AP[pi]
        self.allow = allow_decisions
        self.transfers_out = list(transfers_out)
        self.rng = np.random.default_rng([env.ctx.seed, 7919, pi])

    # ---- helpers
    def new_order(self, t, sup_row, qty, kind="mrp", promised=None, lead=None):
        e = self.env
        lead = sup_row["lead_time_days"] if lead is None else lead
        expected = t + lead
        if promised is None:
            promised = expected + (int(self.rng.integers(1, 6)) if self.rng.random() < e.ro["ack_slip_prob"] and kind == "mrp" else 0)
        price = sup_row["unit_price"]
        o = Order(len(self.orders), self.rm, self.plant, sup_row["supplier_id"], float(qty), t, expected, promised,
                  promised, float(qty), price, kind)
        if kind in ("mrp", "switch", "forced", "ss_build"):
            d, cause = e.delay.draw(o.sup, self.plant, self.rm, t, promised, lead, qty, sup_row["moq"], self.rng)
            o.receipt, o.cause = promised + d, cause
            q4 = e.W.month[min(promised, len(e.W.month) - 1)] in (10, 11, 12)
            o.revs = e.delay.revisions(t, promised, o.receipt, cause, self.rng, q4)
            rel = e.catalog.rel[o.sup]
            if self.rng.random() < (1 - rel) * e.ro["partial_prob_factor"]:
                o.recv_qty = round(qty * float(self.rng.uniform(0.5, 0.95)), 4)
            qrate = e.pp["secondary_quality_supplier"]["qc_reject_rate"] if o.sup == e.pat["secondary_quality_supplier"] else e.ro["qc_reject_rate"]
            if self.rng.random() < qrate:
                o.qc_frac = float(self.rng.uniform(*e.ro["qc_reject_frac"]))
        self.orders.append(o)
        self._maybe_force_episode(o, t)
        self._register(o)
        return o

    def _register(self, o):
        self.arrivals[o.receipt].append(o)
        for n, *_ in o.revs:
            self.notices[n].append(o)

    def _unregister(self, o):
        if o in self.arrivals.get(o.receipt, []):
            self.arrivals[o.receipt].remove(o)
        for n, *_ in o.revs:
            if o in self.notices.get(n, []):
                self.notices[n].remove(o)

    def _maybe_force_episode(self, o, t):
        """Receipts that would land inside an episode's blocked window are pushed to R (cascade)."""
        for ep in self.episodes:
            if ep["scrap_day"] <= o.receipt < ep["R"] and o.ordered < ep["R"]:
                self._move(o, ep["R"] + (ep.get("orig_shift", 0) if o.kind != "switch" else 0), ep, t)

    def _move(self, o, new_receipt, ep, t_now):
        """Re-time a receipt with a consistent notice chain (notices after t_now only)."""
        keep = [r for r in o.revs if r[0] <= t_now]
        cur = keep[-1][2] if keep else o.promised
        o.receipt = new_receipt
        o.episode = ep["id"]
        reason = ep["reason"]
        if o.kind == "switch":
            reason = "carrier_delay"
        revs = list(keep)
        if new_receipt != cur:
            lo = max(t_now + 1, o.ordered + 1)
            first_n = ep["notice_day"] if o.kind != "switch" else None
            n1 = first_n if first_n is not None and first_n >= lo else lo
            n1 = min(n1, new_receipt - 1)
            if n1 >= lo and new_receipt > n1:
                q4 = self.env.W.month[min(new_receipt, len(self.env.W.month) - 1)] in (10, 11, 12)
                slip = new_receipt - cur
                if not keep and slip >= 3 and (q4 or self.rng.random() < 0.35) and new_receipt - 1 > n1 + 1:
                    f = (1 - self.env.pp["q4_optimistic_notices"]["optimism"]) if q4 else float(self.rng.uniform(0.4, 0.8))
                    e1 = cur + max(1, int(round(slip * f)))
                    n2 = int(self.rng.integers(n1 + 1, new_receipt))
                    revs += [(n1, cur, e1, reason), (n2, e1, new_receipt, reason)]
                else:
                    revs.append((n1, cur, new_receipt, reason))
        o.revs = revs
        o.recv_qty = o.qty
        o.qc_frac = 0.0

    def cat_rows(self, t):
        return [r for r in self.env.catalog.at(self.rm, t) if r["allocation_pct"] > 0 and r["qualified_flag"] == 1]

    def choose_supplier(self, t, qty):
        rows = self.cat_rows(t) or self.env.catalog.at(self.rm, t)
        tot = sum(self.alloc_done.values()) + qty
        best, gap = None, -1e18
        for r in rows:
            g = r["allocation_pct"] / 100 * tot - self.alloc_done[r["supplier_id"]]
            if g > gap:
                best, gap = r, g
        self.alloc_done[best["supplier_id"]] += qty
        return best

    def ss_at(self, t):
        m = 1.0
        for a, b, mult in self.ss_boost:
            if a <= t <= b:
                m = max(m, mult)
        return self.ss * m

    def spot(self, t, qty, why="spot_buy"):
        e = self.env
        rows = self.env.catalog.at(self.rm, t)
        r = sorted(rows, key=lambda x: {"secondary": 0, "tertiary": 1, "primary": 2}[x["sourcing_rank"]])[0]
        prem = float(self.rng.uniform(*e.ro["spot_premium"]))
        o = Order(len(self.orders), self.rm, self.plant, r["supplier_id"], round(qty, 4), t, t, t, t, round(qty, 4),
                  round(r["unit_price"] * (1 + prem), 4), "spot")
        o.fee = qty * r["unit_price"] * prem
        o.cause = why
        self.orders.append(o)
        self.short[t] = True
        self.spot_cost[t] += o.fee
        return o

    # ---- decisions
    def evaluate(self, t, trig, ep=None):
        e = self.env
        H = e.costs["decision_horizon_days"]
        opens = [o for o in self.orders if o.open_at(t)]
        inbound = [(o.belief(t), o.qty) for o in opens]
        inbound += [(d, q) for d, q in self.transfers_in if d > t]
        price = e.catalog.price(self.rm, trig.sup, t)
        daycost = self.daycost
        spot_mid = float(np.mean(e.ro["spot_premium"]))
        q4 = e.W.month[t] in (10, 11, 12)
        rel = e.catalog.rel[trig.sup]
        eta = trig.belief(t)
        opts = []

        def add(kind, inb, direct, risk, feasible=True, extra=None):
            days, sf = project(self.oh, t, inb, self.ap, H)
            if ep is not None:
                days = max(0, min(eta if kind != "expedite" else eta - extra["k"], (extra or {}).get("arrive", 10 ** 9)) - ep["t_first"]) \
                    if kind in ("accept_delay", "expedite", "switch_supplier", "reallocate_stock") else days
                sf = 0.0
            cost = direct + days * daycost + sf * price * spot_mid
            opts.append({"option": kind, "est_cost": round(cost, 2), "est_stockout_days": int(days), "risk": risk,
                         "feasible": feasible, "_extra": extra or {}, "_direct": direct})

        add("accept_delay", inbound, 0.0, "high" if eta - t > 10 else "medium")
        k = int(self.rng.integers(*e.ro["expedite_pull_days"]))
        k = min(k, eta - t - 2)
        if k >= 2:
            inb = [(d - k, q) if o is trig else (d, q) for (d, q), o in zip(inbound, opens + [None] * 99)]
            fee = trig.qty * price * float(self.rng.uniform(*e.ro["expedite_fee_rate"])) * self.exp_mult
            add("expedite", inb, fee, "high" if q4 or rel < 0.85 else "medium" if rel < 0.92 else "low", extra={"k": k, "fee": fee})
            acc = opts[0]
            risk_p = 1 - e.costs["expedite_success"]["base"]
            opts[-1]["est_cost"] = round(opts[-1]["est_cost"] + risk_p * max(0.0, acc["est_cost"] - opts[-1]["est_cost"] + fee), 2)
        alts = [r for r in e.catalog.at(self.rm, t) if r["supplier_id"] != trig.sup]
        if alts:
            r2 = sorted(alts, key=lambda r: (-r["qualified_flag"], r["lead_time_days"]))[0]
            qual_days = 0 if r2["qualified_flag"] else e.costs["qualification_days"]
            arrive = t + r2["lead_time_days"] + qual_days
            days0, sf0 = project(self.oh, t, inbound, self.ap, H)
            q2 = max(sf0 * 1.1, r2["moq"], trig.qty * 0.5)
            direct = max(0.0, r2["unit_price"] - price) * q2 + e.costs["switch_admin"] + (0 if r2["qualified_flag"] else e.costs["qualification_cost"])
            inb = [(d, q) for (d, q), o in zip(inbound, opens) if not (o is trig and d > arrive + 2)] + [(arrive, q2)]
            rel2 = e.catalog.rel[r2["supplier_id"]]
            add("switch_supplier", inb, direct, "high" if not r2["qualified_flag"] or rel2 < 0.85 else "medium",
                feasible=arrive < eta, extra={"row": r2, "q": q2, "arrive": arrive, "cancel_trig": arrive + 2 < eta})
        if self.allow_realloc and self.hub is not None:
            hub_pi, hub_plant = self.hub
            ho = e.oh0[hub_pi]
            avail = float(ho[t] - e.SS[hub_pi] - e.AP[hub_pi][t:t + 14].sum()) if ho is not None else 0.0
            days0, sf0 = project(self.oh, t, inbound, self.ap, H)
            if avail > 0 and sf0 > 0:
                q = round(min(sf0 * 1.2, avail * 0.5), 4)
                tr = transit_days(e.ctx.cfg, e.preg[hub_plant], e.preg[self.plant]) + 1
                direct = q * price * e.costs["transfer_freight_rate"] + e.costs["transfer_fixed"]
                add("reallocate_stock", inbound + [(t + tr, q)], direct, "medium" if avail > 3 * q else "high",
                    extra={"q": q, "arrive": t + tr, "src": hub_plant, "src_pi": hub_pi, "transit": tr - 1})
        grp = e.subgroup.get(self.rm)
        if grp:
            arrive = t + e.costs["substitute_qualification_days"]
            add("substitute_rm", inbound + [(arrive, self.ss)], e.costs["substitute_cost"], "medium",
                feasible=False, extra={"arrive": arrive, "group": grp})
        return opts

    def decide(self, t, trig, ep=None):
        e = self.env
        opts = self.evaluate(t, trig, ep)
        role = str(self.rng.choice(["buyer", "supply_planner", "category_manager", "plant_scheduler", "supply_chain_director"],
                                   p=[0.32, 0.30, 0.18, 0.10, 0.10]))
        bias = {"buyer": {"expedite": 0.85}, "supply_planner": {"reallocate_stock": 0.85, "accept_delay": 0.95},
                "category_manager": {"switch_supplier": 0.85}, "plant_scheduler": {"expedite": 0.9},
                "supply_chain_director": {}}[role]
        feas = [o for o in opts if o["feasible"]]
        chosen = min(feas, key=lambda o: o["est_cost"] * bias.get(o["option"], 1.0) * float(self.rng.uniform(0.95, 1.05)))
        rec = {"pair": self.pi, "rm_id": self.rm, "plant_id": self.plant, "t": t, "trigger_lid": trig.lid,
               "trigger_supplier": trig.sup, "episode": None if ep is None else ep["id"], "role": role,
               "options": [{k: v for k, v in o.items() if not k.startswith("_")} for o in opts],
               "chosen": chosen["option"], "expected_cost": chosen["est_cost"], "expected_stockout_days": chosen["est_stockout_days"],
               "belief_eta": trig.belief(t), "q4": bool(e.W.month[t] in (10, 11, 12)), "refs": {},
               "shock": (e.delay.shock_at(e.catalog.country[trig.sup], t) or {}).get("shock_id"),
               "argmin_est": min(feas, key=lambda o: o["est_cost"])["option"], "orig_receipt": trig.receipt,
               "oh_at_decision": round(self.oh, 4)}
        self.apply(t, trig, chosen, rec, ep)
        self.decisions.append(rec)
        self.last_decision = t
        return rec

    def apply(self, t, trig, ch, rec, ep):
        e = self.env
        kind, x = ch["option"], ch["_extra"]
        if kind == "expedite":
            k = x["k"]
            succ = e.costs["expedite_success"]
            p = succ["base"]
            if e.W.month[t] in (10, 11, 12):
                p = succ["q4"]
            if trig.sup == e.pat["q4_slip_supplier"] and e.W.month[t] in (11, 12):
                p = succ["q4_slip_supplier"]
            ok = self.rng.random() < p
            self._unregister(trig)
            cur = trig.belief(t)
            keep = [r for r in trig.revs if r[0] <= t]
            target = cur - k
            keep.append((t, cur, target, "expedite"))
            if ep is not None:
                final = trig.receipt  # fixed by the FG facts (R)
            else:
                pull = k if ok else int(round(k * float(self.rng.uniform(0, 0.3))))
                final = max(t + 2, trig.receipt - pull)
            if final > target and final - 1 >= t + 1:
                n = int(self.rng.integers(t + 1, final))
                keep.append((n, target, final, "expedite_missed"))
            trig.revs, trig.receipt = keep, final
            trig.fee += x["fee"]
            self.fee_cost[t] += x["fee"]
            self._register(trig)
            rec["refs"] = {"expedited_lid": trig.lid, "expedite_success": bool(final <= target)}
        elif kind == "switch_supplier":
            r2 = x["row"]
            o = self.new_order(t, r2, round(x["q"], 4), kind="switch")
            if ep is not None:
                self._unregister(o)
                self._move(o, ep["R"], ep, t)
                self._register(o)
            self.fee_cost[t] += ch["_direct"]
            refs = {"switch_lid": o.lid}
            if x.get("cancel_trig") or ep is not None:
                self._unregister(trig)
                trig.cancelled = t + 1
                trig.revs = [r for r in trig.revs if r[0] <= t]
                refs["cancelled_lid"] = trig.lid
            rec["refs"] = refs
        elif kind == "reallocate_stock":
            arrive = x["arrive"]
            if ep is not None:
                arrive = ep["R"]
                for oo in self.orders:
                    if oo.episode == ep["id"] and oo.cancelled is None and oo.kind != "switch":
                        self._unregister(oo)
                        self._move(oo, ep["R"] + int(self.rng.integers(1, 6)), ep, t)
                        self._register(oo)
            dispatch = max(t + 1, arrive - x["transit"])
            tid = len(e.transfer_requests)
            e.transfer_requests.append({"src_pi": x["src_pi"], "dst_pi": self.pi, "dispatch": dispatch, "arrive": arrive,
                                        "qty": x["q"], "rm": self.rm, "src": x["src"], "dst": self.plant, "tid": tid})
            self.transfers_in.append((arrive, x["q"]))
            self.tr_arrivals[arrive].append((x["q"], tid))
            self.fee_cost[t] += ch["_direct"]
            rec["refs"] = {"transfer_tid": tid}
        rec["direct_est"] = ch["_direct"]

    # ---- main loop
    def run(self):
        e = self.env
        W, ro = e.W, e.ro
        T = W.T
        self.orders, self.decisions = [], []
        self.arrivals, self.notices = defaultdict(list), defaultdict(list)
        self.alloc_done = defaultdict(float)
        self.transfers_in, self.tr_arrivals = [], defaultdict(list)
        self.short = np.zeros(T + PAD, bool)
        self.spot_cost = np.zeros(T + PAD)
        self.fee_cost = np.zeros(T + PAD)
        self.moves = []  # (day, type, qty, ref)
        self.ss = e.SS[self.pi]
        self.daycost = e.daycost[self.pi]
        self.ss_boost = []
        self.episodes = [dict(x) for x in e.episodes_by_pair.get(self.pi, [])]
        self.hub = e.hub.get(self.rm) if e.hub.get(self.rm, (None,))[0] != self.pi else None
        self.allow_realloc = self.allow and self.hub is not None
        self.exp_mult = e.pp["expedite_cost_plant"]["multiplier"] if self.plant == e.pat["expedite_cost_plant"] else 1.0
        self.last_decision = -10 ** 6
        protect = np.zeros(T + PAD, bool)
        hard = np.zeros(T + PAD, bool)
        for ep in self.episodes:
            protect[max(0, ep["t_first"] - 16): ep["R"] + 1] = True
            hard[ep["scrap_day"]: ep["R"]] = True
            ep.setdefault("orig_shift", 0)
        out_by_day = defaultdict(list)
        for tr in self.transfers_out:
            out_by_day[tr["dispatch"]].append(tr)
        ss_build_at = {ep["R"] + int(self.rng.integers(5, 16)): ep for ep in self.episodes
                       if self.rng.random() < e.costs["ss_build_prob"]}
        prim0 = e.catalog.primary(self.rm, 0)
        daily = self.ap[:90].mean() if self.ap[:90].sum() > 0 else self.ap.mean()
        self.oh = round(float(daily * max(ro["opening_cover_days"], prim0["lead_time_days"] + 21) + self.ss), 4)
        self.opening = self.oh
        ohs = np.zeros(T)
        scrap_due = defaultdict(list)
        ep_by_scrap = {ep["scrap_day"]: ep for ep in self.episodes}
        ep_by_force = {ep["force_day"]: ep for ep in self.episodes}
        ep_by_notice = {ep["notice_day"]: ep for ep in self.episodes}
        ep_by_R = {ep["R"]: ep for ep in self.episodes}
        pending_ss_build_po = False

        for t in range(T):
            # forced slipped order for an episode, if MRP did not create one
            ep = ep_by_force.get(t)
            if ep is not None and not any(o.episode == ep["id"] for o in self.orders):
                pr = e.catalog.primary(self.rm, t)
                lead = max(3, ep["t_first"] - int(self.rng.integers(1, 5)) - t)
                q = max(pr["moq"], float(self.a[ep["R"]:ep["R"] + 14].sum() + self.ss * 0.5))
                o = self.new_order(t, pr, round(q, 4), kind="forced", promised=t + lead, lead=lead)
                if o.episode is None:
                    self._unregister(o)
                    self._move(o, ep["R"], ep, t)
                    self._register(o)
            # receipts
            ep = ep_by_R.get(t)
            if ep is not None:
                need = float(self.a[t:t + 10].sum() + self.ss * 0.3 - self.oh)
                arr = [o for o in self.arrivals.get(t, []) if o.cancelled is None]
                have = sum(o.recv_qty for o in arr) + sum(q for q, _ in self.tr_arrivals.get(t, []))
                if arr and have < need:
                    o = max(arr, key=lambda z: z.qty)
                    o.qty = o.recv_qty = round(o.recv_qty + need - have, 4)
            for o in self.arrivals.get(t, []):
                if o.cancelled is not None:
                    continue
                self.oh += o.recv_qty
                self.moves.append((t, "receipt", o.recv_qty, o))
                if o.qc_frac > 0 and not protect[t + 1]:
                    scrap_due[t + 1].append(o)
            for q, tid in self.tr_arrivals.get(t, []):
                self.oh += q
                self.moves.append((t, "transfer", q, ("in", tid)))
            for tr in out_by_day.get(t, []):
                self.oh -= tr["qty"]
                self.moves.append((t, "transfer", -tr["qty"], ("out", tr["tid"])))
            for o in scrap_due.get(t, []):
                q = round(min(o.recv_qty * o.qc_frac, max(self.oh, 0.0)), 4)
                if q > 0:
                    self.oh -= q
                    self.moves.append((t, "scrap", -q, o))
            # episode: QC reject / top-up so stock covers on-time runs but not the blocked run
            ep = ep_by_scrap.get(t)
            if ep is not None:
                proj = self.oh - float(self.a[t:ep["t_first"]].sum())
                target = ep["on_time_sum"] + ep["eps"]
                last = next((m[3] for m in reversed(self.moves) if m[1] == "receipt"), None)
                if proj > target + 1e-6:
                    q = round(proj - target, 4)
                    self.oh -= q
                    self.moves.append((t, "scrap", -q, last))
                    ep["scrap_qty"], ep["scrap_lid"] = q, (last.lid if last is not None else None)
                elif proj < target - 1e-6:
                    o = self.spot(t, round(target - proj, 4), why="priority_topup")
                    self.short[t] = False
                    self.oh += o.recv_qty
                    self.moves.append((t, "receipt", o.recv_qty, o))
                    ep["topup_lid"] = o.lid
            # decisions on ETA notices
            if self.allow:
                ep = ep_by_notice.get(t)
                if ep is not None:
                    trig = next((o for o in self.orders if o.episode == ep["id"] and o.cancelled is None), None)
                    if trig is not None:
                        q4 = W.month[t] in (10, 11, 12)
                        if self.rng.random() < (e.costs["pessimistic_notice_prob"]["q4"] if q4 else e.costs["pessimistic_notice_prob"]["base"]):
                            # supplier announces a conservative date beyond R; the lot then lands at R (early vs notice)
                            self._unregister(trig)
                            keep = [r for r in trig.revs if r[0] < t]
                            cur = keep[-1][2] if keep else trig.promised
                            reason = next((r[3] for r in trig.revs if r[0] >= t), ep["reason"])
                            trig.revs = keep + [(t, cur, ep["R"] + int(self.rng.integers(2, 9)), reason)]
                            self._register(trig)
                        self.decide(t, trig, ep)
                elif not protect[t] and t - self.last_decision >= e.costs["decision_cooldown_days"]:
                    for o in list(self.notices.get(t, [])):
                        if o.cancelled is not None or o.kind == "spot":
                            continue
                        rv = [r for r in o.revs if r[0] == t]
                        if not rv or rv[0][3] in ("expedite", "expedite_missed") or rv[0][2] - rv[0][1] < e.costs["decision_min_slip"]:
                            continue
                        opens = [x for x in self.orders if x.open_at(t)]
                        days, _ = project(self.oh, t, [(x.belief(t), x.qty) for x in opens], self.ap, ro["lookahead_days"])
                        if days > 0:
                            self.decide(t, o)
                            break
                ep = ss_build_at.get(t)
                if ep is not None:
                    self.ss_decision(t, ep)
                    pending_ss_build_po = len(self.decisions) - 1
            # consumption
            need = self.a[t]
            if self.oh - need < -1e-9:
                if hard[t]:
                    eps_ = [x for x in self.episodes if x["t_first"] - 16 <= t <= x["R"]]
                    dd = [x["chosen"] for x in self.decisions if eps_ and x["episode"] == eps_[0]["id"]]
                    e.warnings.append((self.pi, t, eps_[0]["id"] if eps_ else None, eps_[0]["t_first"] if eps_ else None,
                                       eps_[0]["R"] if eps_ else None, dd, len(self.transfers_out), round(need - self.oh, 3)))
                o = self.spot(t, round(need - self.oh, 4))
                self.oh += o.recv_qty
                self.moves.append((t, "receipt", o.recv_qty, o))
            self.oh -= need
            if abs(self.oh) < 1e-9:
                self.oh = 0.0
            # monthly cycle count
            if W.weekday[t] == 2 and W.dates[t].day <= 7 and not protect[t] and self.oh > 0 and self.rng.random() < 0.25:
                q = round(float(self.oh * self.rng.normal(0, 0.004)), 4)
                if q != 0 and self.oh + q >= 0:
                    self.oh += q
                    self.moves.append((t, "adjustment", q, None))
            # MRP review
            if W.weekday[t] == ro["review_weekday"]:
                pr = e.catalog.primary(self.rm, t)
                H = pr["lead_time_days"] + 14
                pos = self.oh + sum(o.qty for o in self.orders if o.open_at(t)) + sum(q for d, q in self.transfers_in if d > t)
                target = float(self.ap[t + 1:t + 1 + H].sum()) + self.ss_at(t)
                if pos < target - 1e-6 and self.ap[t + 1:t + 1 + H].sum() > 0:
                    q = target - pos
                    sup = self.choose_supplier(t, q)
                    q = math.ceil(q / sup["moq"]) * sup["moq"]
                    o = self.new_order(t + 0, sup, round(q, 4), kind="ss_build" if pending_ss_build_po is not False else "mrp")
                    if pending_ss_build_po is not False:
                        self.decisions[pending_ss_build_po]["refs"]["first_po_lid"] = o.lid
                        pending_ss_build_po = False
            ohs[t] = self.oh
        self.ohs = ohs
        return self

    def ss_decision(self, t, ep):
        e = self.env
        mult, days = e.costs["ss_build_multiplier"], e.costs["ss_build_days"]
        price = e.catalog.primary(self.rm, t)["unit_price"]
        extra = self.ss * (mult - 1)
        hold = extra * price * e.ro["holding_rate_annual"] * days / 365
        opts = [{"option": "build_safety_stock", "est_cost": round(hold, 2), "est_stockout_days": 0, "risk": "low", "feasible": True},
                {"option": "accept_delay", "est_cost": round(2 * self.daycost, 2), "est_stockout_days": 2, "risk": "medium", "feasible": True}]
        self.ss_boost.append((t, t + days, mult))
        self.decisions.append({"pair": self.pi, "rm_id": self.rm, "plant_id": self.plant, "t": t, "trigger_lid": None,
                               "trigger_supplier": e.catalog.primary(self.rm, t)["supplier_id"], "episode": ep["id"],
                               "role": "supply_planner", "options": opts, "chosen": "build_safety_stock",
                               "expected_cost": round(hold, 2), "expected_stockout_days": 0, "belief_eta": None,
                               "q4": bool(e.W.month[t] in (10, 11, 12)), "refs": {"ss_until": t + days, "ss_extra": round(extra, 4)},
                               "shock": None, "argmin_est": "build_safety_stock", "direct_est": round(hold, 2), "followup": True})


# ------------------------------------------------------------------ cascade episode selection
def select_episodes(ctx: Ctx, W: World, runs, cons_act, cons_plan, pairs, pidx, A, SS, catalog, pat, rmx):
    rng = ctx.rng("episodes")
    cc = ctx.cfg["cascade"]
    make_lines = runs[runs._status != "open"]
    late_or_partial = int(((make_lines.delay_days > 0) | (make_lines._status == "partial")).sum())
    target = int(round(cc["target_frac"] * late_or_partial))
    r = runs[(runs.delay_days >= cc["min_fg_delay"]) & runs.actual_start.notna()]
    ps = W.day(r.planned_start).to_numpy()
    ac = W.day(r.actual_start).to_numpy()
    rinfo = pd.DataFrame({"production_run_id": r.production_run_id.to_numpy(), "p": ps, "a": ac})
    # candidate (run, rm) using the BOM active at planned start
    cp = cons_plan[cons_plan.production_run_id.isin(r.production_run_id)][["production_run_id", "rm_id", "plant_id", "qty"]]
    ca = cons_act[["production_run_id", "rm_id", "qty"]].rename(columns={"qty": "req"})
    cand = cp.merge(ca, on=["production_run_id", "rm_id"]).merge(rinfo, on="production_run_id")
    cand["pi"] = [pidx[(a, b)] for a, b in zip(cand.rm_id, cand.plant_id)]
    # preference: planted-pattern alignment, criticality, randomness
    crit = rmx.set_index("rm_id").criticality
    prim_sup = np.array([catalog.primary(rm_, int(p))["supplier_id"] for rm_, p in zip(cand.rm_id, cand.p)])
    month = W.month[cand.p.to_numpy()]
    country = np.array([catalog.country[s] for s in prim_sup])
    score = rng.random(len(cand)) + (cand.rm_id.map(crit).to_numpy() == "A") * 0.6
    score += ((prim_sup == pat["q4_slip_supplier"]) & np.isin(month, [11, 12])) * 3
    score += ((country == pat["feb_port_country"]) & (month == 2)) * 2
    score += (cand.rm_id.to_numpy() == pat["price_spike_rm"]) * 1.5
    cand["score"] = score
    cand["production_run_id"] = cand.production_run_id.astype(object)
    cand["rm_id"] = cand.rm_id.astype(object)
    cand["plant_id"] = cand.plant_id.astype(object)
    cand["ri"] = pd.factorize(cand.production_run_id)[0]
    cand = cand.sort_values("score", ascending=False)
    by_pair = {pi: {k: g.sort_values("a")[k].to_numpy() for k in ("p", "a", "ri", "req", "production_run_id")}
               for pi, g in cand.groupby("pi")}
    assigned = np.zeros(cand.ri.max() + 1, bool)
    episodes, used = [], defaultdict(list)
    n_blocked = 0
    for row in cand.itertuples():
        if n_blocked >= target:
            break
        if assigned[row.ri]:
            continue
        R = int(row.a) - int(rng.integers(cc["receipt_buffer_days"][0], cc["receipt_buffer_days"][1] + 1))
        if R <= row.p:
            R = int(row.a)
        g = by_pair[row.pi]
        m = (g["p"] < R) & (g["a"] >= R) & ~assigned[g["ri"]]
        if not m.any():
            continue
        _, first = np.unique(g["ri"][m], return_index=True)
        blk = {k: v[m][first] for k, v in g.items()}
        t_first = int(blk["p"].min())
        lead = catalog.primary(row.rm_id, t_first)["lead_time_days"]
        force_day = t_first - lead - int(rng.integers(2, 6))
        if force_day < 7:
            continue
        gap = cc["episode_gap_days"]
        if any(not (R + gap < a or t_first - gap > b) for a, b in used[row.pi]):
            continue
        on_time = float(A[row.pi][t_first:R].sum())
        if on_time > cc["max_on_time_share_of_ss"] * SS[row.pi] or SS[row.pi] <= 0:
            continue
        req_first = float(blk["req"][np.argmin(blk["p"])])
        eps = float(min(rng.uniform(0.0, 0.6) * req_first, 0.3 * SS[row.pi]))
        scrap_day = t_first - int(rng.integers(cc["qc_scrap_lead_days"][0], cc["qc_scrap_lead_days"][1] + 1))
        notice_day = scrap_day - int(rng.integers(1, 9))
        notice_day = max(notice_day, force_day + 1)
        if notice_day >= scrap_day:
            continue
        used[row.pi].append((t_first, R))
        assigned[blk["ri"]] = True
        n_blocked += len(blk["ri"])
        episodes.append({"id": len(episodes), "pi": int(row.pi), "rm_id": row.rm_id, "plant_id": row.plant_id,
                         "t_first": t_first, "R": R, "blocked_runs": list(blk["production_run_id"]),
                         "blocked_req": float(blk["req"].sum()), "on_time_sum": on_time, "eps": eps,
                         "scrap_day": scrap_day, "notice_day": notice_day, "force_day": force_day,
                         "reason": "base", "primary_supplier": catalog.primary(row.rm_id, t_first)["supplier_id"]})
    return episodes, target, late_or_partial


def episode_reason(ep, catalog, delay: DelayModel, pat, W, rng):
    sup = ep["primary_supplier"]
    m = W.month[ep["t_first"]]
    sh = delay.shock_at(catalog.country[sup], ep["t_first"])
    if sup == pat["q4_slip_supplier"] and m in (11, 12):
        return "peak_season_capacity", None
    if sh is not None:
        return sh["type"], sh["shock_id"]
    if ep["rm_id"] == pat["price_spike_rm"] and any(a - 10 <= ep["t_first"] <= b + 10 for a, b in delay.alloc_windows):
        return "allocation_cut", None
    if sup == pat["lead_time_creep_supplier"] and ep["t_first"] >= delay.creep_from:
        return "lead_time_extension", None
    return str(rng.choice(REASONS_BASE + ["quality_hold_at_supplier"])), None


# ------------------------------------------------------------------ run
class Env:
    pass


def run(ctx: Ctx):
    banner("STAGE 1b - RM operations / production / cascade back-fill")
    src = ctx.src()
    W = World(ctx)
    pext, plants = ctx.read("products_ext"), ctx.read("plants")
    rmx, bom = ctx.read("raw_materials"), ctx.read("bill_of_materials")
    cat_df = ctx.read("rm_supplier_catalog")
    pat = ctx.load_json("pattern_plan.json")
    S = src["suppliers"]

    runs = build_runs(ctx, W, src, pext, plants)
    cons_act = bom_lines_at(bom, runs, "actual_start", "produced_qty")
    cons_plan = bom_lines_at(bom, runs, "planned_start", "planned_qty")
    pairs = sorted(set(zip(cons_plan.rm_id, cons_plan.plant_id)) | set(zip(cons_act.rm_id, cons_act.plant_id)))
    pidx = {p: i for i, p in enumerate(pairs)}
    TT = W.T + PAD
    A = np.zeros((len(pairs), TT))
    AP = np.zeros((len(pairs), TT))
    ia = np.array([pidx[(a, b)] for a, b in zip(cons_act.rm_id, cons_act.plant_id)])
    np.add.at(A, (ia, W.day(cons_act.actual_start).to_numpy()), cons_act.qty.to_numpy())
    ip = np.array([pidx[(a, b)] for a, b in zip(cons_plan.rm_id, cons_plan.plant_id)])
    np.add.at(AP, (ip, W.day(cons_plan.planned_start).to_numpy()), cons_plan.qty.to_numpy())

    catalog = Catalog(cat_df, W, S)
    crit = rmx.set_index("rm_id").criticality
    sdays = ctx.cfg["rm_ops"]["safety_days"]
    mean_daily = AP[:, :W.T].mean(axis=1)
    SS = np.array([sdays[crit[r]] * mean_daily[i] for i, (r, p) in enumerate(pairs)])
    price0 = np.array([catalog.primary(r, 0)["unit_price"] for r, p in pairs])
    daycost = mean_daily * price0 * ctx.cfg["costs"]["shortage_value_multiple"]

    shocks = plan_shocks(ctx, W, cat_df, S, pat)
    delay = DelayModel(ctx, W, catalog, plants.plant_id.tolist(), shocks, pat)
    episodes, target, lop = select_episodes(ctx, W, runs, cons_act, cons_plan, pairs, pidx, A, SS, catalog, pat, rmx)
    rr = ctx.rng("episode_reason")
    for ep in episodes:
        ep["reason"], ep["shock_id"] = episode_reason(ep, catalog, delay, pat, W, rr)

    env = Env()
    env.ctx, env.W, env.catalog, env.delay, env.pat = ctx, W, catalog, delay, pat
    env.ro, env.pp, env.costs = ctx.cfg["rm_ops"], ctx.cfg["patterns"], ctx.cfg["costs"]
    env.pairs, env.A, env.AP, env.SS, env.daycost = pairs, A, AP, SS, daycost
    env.preg = plants.set_index("plant_id").region.to_dict()
    env.subgroup = rmx.dropna(subset=["substitute_group_id"]).set_index("rm_id").substitute_group_id.to_dict()
    env.episodes_by_pair = defaultdict(list)
    for ep in episodes:
        env.episodes_by_pair[ep["pi"]].append(ep)
    # hub plant per RM = highest planned usage; it can ship to other plants but never requests reallocation
    use = pd.DataFrame({"rm": [p[0] for p in pairs], "plant": [p[1] for p in pairs], "u": AP.sum(axis=1), "pi": range(len(pairs))})
    env.hub = {r: (int(g.sort_values("u").pi.iat[-1]), g.sort_values("u").plant.iat[-1]) for r, g in use.groupby("rm") if len(g) > 1}
    env.warnings, env.transfer_requests = [], []

    # pass 0: no decisions - availability proxy for hubs
    env.oh0 = [None] * len(pairs)
    hubs = {v[0] for v in env.hub.values()}
    for pi in hubs:
        env.oh0[pi] = np.concatenate([PairSim(env, pi, allow_decisions=False).run().ohs, np.zeros(PAD)])
    # pass 1: all pairs with decisions
    results = {}
    for pi in range(len(pairs)):
        results[pi] = PairSim(env, pi, allow_decisions=True).run()
    # pass 2: hubs re-simulated with their outbound transfers
    outs = defaultdict(list)
    for tr in env.transfer_requests:
        outs[tr["src_pi"]].append(tr)
    for pi, trs in outs.items():
        results[pi] = PairSim(env, pi, allow_decisions=True, transfers_out=trs).run()
    if env.warnings:
        print(f"WARNING: {len(env.warnings)} protected-window shortfalls, e.g. {env.warnings[:3]}")

    assemble(ctx, W, env, results, runs, cons_act, episodes, shocks, plants, pat, target, lop)


# ------------------------------------------------------------------ assemble outputs
def assemble(ctx, W, env, results, runs, cons_act, episodes, shocks, plants, pat, target, lop):
    pairs = env.pairs
    episodes = sorted([x for pi in sorted(results) for x in results[pi].episodes], key=lambda x: x["id"])
    end = W.T - 1
    # ---- orders -> POs (consolidate identical-fate lines)
    recs = []
    for pi, res in results.items():
        for o in res.orders:
            recs.append({"pi": pi, "lid": o.lid, "rm_id": o.rm, "plant_id": o.plant, "supplier_id": o.sup,
                         "qty": round(o.qty, 4), "recv": round(o.recv_qty, 4), "ordered": o.ordered, "expected": o.expected,
                         "promised": o.promised, "receipt": o.receipt, "cancelled": o.cancelled, "price": o.price,
                         "kind": o.kind, "revs": tuple((int(a), int(b), int(c), r) for a, b, c, r in o.revs if a <= end),
                         "episode": o.episode, "cause": o.cause, "fee": o.fee})
    od = pd.DataFrame(recs)
    od = od[od.ordered <= end].copy()
    od["cancelled"] = od.cancelled.astype("float")
    od["_key"] = list(zip(od.supplier_id, od.plant_id, od.ordered, od.expected, od.promised, od.receipt,
                          od.cancelled.fillna(-1), od.revs, od.kind))
    od = od.sort_values(["ordered", "supplier_id", "plant_id", "rm_id"]).reset_index(drop=True)
    keys = pd.Index(od._key.unique())
    od["po_n"] = keys.get_indexer(od._key)
    order_po = od.drop_duplicates("po_n").sort_values("po_n")
    po_ids = fmt_ids("RMPO", len(order_po), 6)
    od["rm_po_id"] = od.po_n.map(dict(zip(order_po.po_n, po_ids)))
    od["rm_purchase_order_line_id"] = fmt_ids("RMPOL", len(od), 7)
    received = (od.receipt <= end) & od.cancelled.isna()
    od["qrecv"] = np.where(received, od.recv, 0.0)
    hdr = od.groupby("rm_po_id").agg(supplier_id=("supplier_id", "first"), plant_id=("plant_id", "first"),
                                    ordered=("ordered", "first"), expected=("expected", "first"), promised=("promised", "first"),
                                    receipt=("receipt", "first"), cancelled=("cancelled", "first"),
                                    kind=("kind", "first")).reset_index()
    part = ((od.qrecv < od.qty - 1e-9) & (od.receipt <= end)).groupby(od.rm_po_id).any()
    hdr["status"] = np.where(hdr.cancelled.notna(), "cancelled",
                             np.where(hdr.receipt > end, "open", np.where(hdr.rm_po_id.map(part), "partial", "received")))
    todate = lambda s: W.d0 + pd.to_timedelta(s, unit="D")
    rm_pos = pd.DataFrame({"rm_po_id": hdr.rm_po_id, "supplier_id": hdr.supplier_id, "plant_id": hdr.plant_id,
                           "ordered_at": todate(hdr.ordered), "expected_at": todate(hdr.expected),
                           "promised_at": todate(hdr.promised),
                           "received_at": todate(hdr.receipt).where(hdr.status.isin(["received", "partial"])),
                           "status": hdr.status})
    rm_lines = pd.DataFrame({"rm_purchase_order_line_id": od.rm_purchase_order_line_id, "rm_po_id": od.rm_po_id,
                             "rm_id": od.rm_id, "quantity_ordered": od.qty, "quantity_received": od.qrecv.round(4),
                             "unit_cost": od.price.round(4)})
    # revisions (header level)
    rv = []
    for r in od.drop_duplicates("rm_po_id").itertuples():
        for n, o, nw, reason in r.revs:
            if r.cancelled == r.cancelled and n >= r.cancelled:
                continue
            rv.append({"rm_po_id": r.rm_po_id, "revised_at": W.date(n), "old_expected_at": W.date(o),
                       "new_expected_at": W.date(nw), "reason_code": reason})
    revs = pd.DataFrame(rv).sort_values(["revised_at", "rm_po_id"]).reset_index(drop=True)
    revs.insert(0, "rm_po_revision_id", fmt_ids("RMREV", len(revs), 6))
    lid_map = {(r.pi, r.lid): r.rm_purchase_order_line_id for r in od.itertuples()}
    po_map = {(r.pi, r.lid): r.rm_po_id for r in od.itertuples()}

    # ---- movements
    mv = []
    tr_ids = {}
    for pi, res in results.items():
        rm_, pl = pairs[pi]
        for day, typ, q, ref in res.moves:
            if day > end:
                continue
            row = {"rm_id": rm_, "plant_id": pl, "day": day, "movement_type": typ, "quantity_change": round(q, 4),
                   "rm_purchase_order_line_id": None, "transfer_id": None, "production_run_id": None}
            if isinstance(ref, Order):
                row["rm_purchase_order_line_id"] = lid_map.get((pi, ref.lid))
            elif isinstance(ref, tuple):
                tid = ref[1]
                tr_ids.setdefault(tid, f"RMT{len(tr_ids) + 1:07d}")
                row["transfer_id"] = tr_ids[tid]
            mv.append(row)
    mv = pd.DataFrame(mv)
    cons = pd.DataFrame({"rm_id": cons_act.rm_id, "plant_id": cons_act.plant_id, "day": W.day(cons_act.actual_start),
                         "movement_type": "consumption", "quantity_change": -cons_act.qty, "rm_purchase_order_line_id": None,
                         "transfer_id": None, "production_run_id": cons_act.production_run_id})
    mv = pd.concat([mv, cons], ignore_index=True)
    order_type = {"receipt": 0, "transfer": 1, "scrap": 2, "adjustment": 3, "consumption": 4}
    mv["_o"] = mv.movement_type.map(order_type)
    mv = mv.sort_values(["day", "rm_id", "plant_id", "_o"]).reset_index(drop=True)
    rm_mov = pd.DataFrame({"rm_movement_id": fmt_ids("RMM", len(mv), 8), "rm_id": mv.rm_id, "plant_id": mv.plant_id,
                           "movement_at": todate(mv.day), "movement_type": mv.movement_type,
                           "quantity_change": mv.quantity_change.round(4),
                           "rm_purchase_order_line_id": mv.rm_purchase_order_line_id.astype("string"),
                           "transfer_id": mv.transfer_id.astype("string"), "production_run_id": mv.production_run_id.astype("string")})

    # ---- opening balances & weekly snapshots (recomputed from movements => identity holds by construction)
    ob = pd.DataFrame({"rm_opening_balance_id": [f"{r}-{p}" for r, p in pairs], "rm_id": [r for r, _ in pairs],
                       "plant_id": [p for _, p in pairs], "balance_date": W.d0,
                       "opening_units": [round(results[i].opening, 4) for i in range(len(pairs))]})
    daily = np.zeros((len(pairs), W.T))
    key = pd.Series(range(len(pairs)), index=pd.MultiIndex.from_tuples(pairs))
    mi = key.reindex(pd.MultiIndex.from_arrays([mv.rm_id, mv.plant_id])).to_numpy()
    np.add.at(daily, (mi, mv.day.to_numpy()), mv.quantity_change.to_numpy())
    onhand = ob.opening_units.to_numpy()[:, None] + np.cumsum(daily, axis=1)
    short = np.zeros((len(pairs), W.T), bool)
    for pi, res in results.items():
        short[pi] = res.short[:W.T]
    for ep in episodes:
        short[ep["pi"], ep["t_first"]:ep["R"]] = True
    snap_days = np.where(W.weekday[:W.T] == 6)[0]
    ss_mat = np.zeros((len(pairs), W.T))
    for pi, res in results.items():
        ss_mat[pi] = res.ss
        for a, b, m in res.ss_boost:
            ss_mat[pi, a:min(b + 1, W.T)] = res.ss * m
    fwd = np.array([env.AP[:, d + 1:d + 29].mean(axis=1) for d in snap_days]).T
    fallback = env.AP[:, :W.T].mean(axis=1)[:, None]
    rate = np.where(fwd > 0, fwd, fallback)
    oh_s = onhand[:, snap_days]
    wk_short = np.array([short[:, max(0, d - 6):d + 1].any(axis=1) for d in snap_days]).T | (oh_s <= 1e-9)
    snaps = pd.DataFrame({
        "rm_id": np.repeat([p[0] for p in pairs], len(snap_days)), "plant_id": np.repeat([p[1] for p in pairs], len(snap_days)),
        "snapshot_date": np.tile(W.dates[snap_days], len(pairs)), "on_hand_units": oh_s.ravel().round(4),
        "safety_stock_units": ss_mat[:, snap_days].ravel().round(4),
        "days_of_cover": np.where(rate > 0, oh_s / np.maximum(rate, 1e-9), np.nan).ravel().round(2),
        "stockout_flag": wk_short.ravel().astype(int)})

    # ---- production runs: reasons
    rng = ctx.rng("run_reasons")
    runs = runs.copy()
    blocked = {}
    for ep in episodes:
        for r in ep["blocked_runs"]:
            blocked[r] = ep["rm_id"]
    is_b = runs.production_run_id.isin(blocked)
    runs.loc[is_b, "delay_reason_code"] = "rm_shortage"
    runs.loc[is_b, "rm_shortage_rm_id"] = runs.loc[is_b, "production_run_id"].map(blocked)
    late = (runs.delay_days > 0).fillna(False) & ~is_b
    cap = plants.set_index("plant_id").capacity_units_per_week
    wk = runs.dropna(subset=["actual_start"]).groupby(["plant_id", pd.Grouper(key="actual_start", freq="W-MON")]).produced_qty.sum()
    load = (wk / wk.index.get_level_values(0).map(cap).to_numpy()).rename("load").reset_index()
    runs["_wk"] = runs.actual_start.dt.to_period("W-MON").dt.end_time.dt.normalize()
    load["_wk"] = load.actual_start.dt.to_period("W-MON").dt.end_time.dt.normalize()
    runs = runs.merge(load[["plant_id", "_wk", "load"]], on=["plant_id", "_wk"], how="left")
    choices = rng.choice(["changeover", "quality_hold", "maintenance", "labor"], p=[0.40, 0.27, 0.27, 0.06], size=len(runs))
    reason = np.where(runs.load.fillna(0).to_numpy() > 0.85, "capacity", choices)
    summer = (runs.plant_id == pat["summer_labor_plant"]) & runs.actual_start.dt.month.isin(env.pp["summer_labor_plant"]["months"])
    reason = np.where(summer & (rng.random(len(runs)) < 0.8), "labor", reason)
    runs.loc[late, "delay_reason_code"] = reason[late.to_numpy()]
    runs = runs.drop(columns=["_wk", "load"])
    prod = runs[["production_run_id", "plant_id", "product_id", "purchase_order_line_id", "planned_start", "actual_start",
                 "planned_qty", "produced_qty", "delay_days", "delay_reason_code", "rm_shortage_rm_id"]].copy()
    prod["delay_reason_code"] = prod.delay_reason_code.astype("string")
    prod["rm_shortage_rm_id"] = prod.rm_shortage_rm_id.astype("string")

    # ---- decisions: realised outcomes on the final trajectories
    H = env.costs["decision_horizon_days"]
    dec = []
    for pi, res in results.items():
        for d in res.decisions:
            t = d["t"]
            hz = d["refs"].get("ss_until", t + H) if d["chosen"] == "build_safety_stock" else t + H
            hz = min(hz, W.T - 1)
            sd = int(short[pi, t:hz + 1].sum())
            direct = float(res.fee_cost[t:t + 1].sum()) if d["chosen"] != "build_safety_stock" else 0.0
            if d["chosen"] == "build_safety_stock":
                direct = float(d["refs"]["ss_extra"] * env.catalog.primary(pairs[pi][0], t)["unit_price"]
                               * env.ro["holding_rate_annual"] * (hz - t) / 365)
            spot = float(res.spot_cost[t:hz + 1].sum())
            actual = direct + spot + sd * env.daycost[pi]
            d2 = dict(d)
            d2.update({"actual_cost": round(actual, 2), "actual_stockout_days": sd, "outcome_day": int(hz),
                       "pair_rm": pairs[pi][0], "pair_plant": pairs[pi][1]})
            refs = d2["refs"]
            for k in ("expedited_lid", "switch_lid", "cancelled_lid", "first_po_lid"):
                if k in refs:
                    refs[k.replace("_lid", "_line_id")] = lid_map.get((pi, refs[k]))
                    refs[k.replace("_lid", "_po_id")] = po_map.get((pi, refs[k]))
            if "transfer_tid" in refs:
                refs["transfer_id"] = tr_ids.get(refs["transfer_tid"])
            if d2.get("trigger_lid") is not None:
                refs["trigger_po_id"] = po_map.get((pi, d2["trigger_lid"]))
                refs["trigger_line_id"] = lid_map.get((pi, d2["trigger_lid"]))
            dec.append(d2)

    # ---- episodes: resolve the RM PO ids in the chain
    run_pol = dict(zip(runs.production_run_id.astype(object), runs.purchase_order_line_id.astype(object)))
    for ep in episodes:
        res = results[ep["pi"]]
        slipped = [o for o in res.orders if o.episode == ep["id"]]
        ep["slipped_po_ids"] = sorted({po_map.get((ep["pi"], o.lid)) for o in slipped if po_map.get((ep["pi"], o.lid))})
        ep["scrap_line_id"] = lid_map.get((ep["pi"], ep.get("scrap_lid"))) if ep.get("scrap_lid") is not None else None
        ep["topup_line_id"] = lid_map.get((ep["pi"], ep.get("topup_lid"))) if ep.get("topup_lid") is not None else None
        ep["fg_lines"] = [run_pol[r] for r in ep["blocked_runs"]]
        ep["detected_day"] = min([r[0] for o in slipped for r in o.revs] or [ep["notice_day"]])

    ctx.write(prod, "production_runs")
    ctx.write(ob, "rm_inventory_opening_balances")
    ctx.write(rm_pos, "rm_purchase_orders")
    ctx.write(rm_lines, "rm_purchase_order_lines")
    ctx.write(revs, "rm_po_revisions")
    ctx.write(rm_mov, "rm_inventory_movements")
    ctx.write(snaps, "rm_inventory_snapshots_weekly")
    ctx.write(od.drop(columns=["_key", "revs"]).assign(cancelled=od.cancelled), "rm_order_meta", work=True)
    ctx.save_json({"episodes": episodes, "shocks": shocks, "decisions": dec, "transfers": env.transfer_requests,
                   "transfer_ids": {str(k): v for k, v in tr_ids.items()}, "alloc_windows": env.delay.alloc_windows,
                   "sim_start": str(W.d0.date()), "cascade_target_lines": target, "late_or_partial_make_lines": lop,
                   "warnings": env.warnings[:50]}, "rm_ops_plan.json")

    nb = sum(len(e["blocked_runs"]) for e in episodes)
    print(f"production runs {len(prod):,} (received {prod.actual_start.notna().sum():,}, open {prod.actual_start.isna().sum():,})")
    print(f"RM pairs {len(pairs)} | RM POs {len(rm_pos):,} lines {len(rm_lines):,} | revisions {len(revs):,} | movements {len(rm_mov):,}")
    print(f"RM PO status: {rm_pos.status.value_counts().to_dict()}")
    print(f"movement types: {rm_mov.movement_type.value_counts().to_dict()}")
    print(f"cascade episodes {len(episodes)} blocking {nb} FG lines = {nb / lop:.1%} of {lop} late/partial make lines (target {target})")
    print(f"decisions (in-sim) {len(dec)}: {pd.Series([d['chosen'] for d in dec]).value_counts().to_dict()}")
    print(f"spot buys {int((rm_pos.rm_po_id.isin(od[od.kind == 'spot'].rm_po_id)).sum())} | transfers {len(tr_ids)} | "
          f"snapshots {len(snaps):,} (stockout weeks {snaps.stockout_flag.sum():,})")


if __name__ == "__main__":
    run(cli(__doc__))
