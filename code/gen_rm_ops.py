"""Stage 1b - production runs, raw-material MRP simulation and cascade back-fill.

Physics
-------
* Every existing FG PO line of a make-product that received units is the output
  of one production run (produced_qty = quantity_received). The run starts
  `planned production + transit lead` before the FG receipt.
* RM consumption = produced_qty x BOM(effective at actual_start) x (1+scrap%).
* Each (rm, plant) is replenished by a periodic-review MRP (order-up-to with
  known production schedule + safety stock). RM PO delays are sampled from a
  causal model: supplier reliability, quoted lead time, country / season shocks
  and planted patterns. Delays that would drive stock negative are absorbed
  (safety-stock buffer, supplier pull-in or emergency spot buy).
* Cascade back-fill: for ~12% of late / short make-product FG lines we
  construct RM PO slips (rm_po_revisions) so that stock falls below safety,
  then cannot cover the run at its planned start; the run starts when the
  material lands, which is exactly the late FG receipt already in the data.
  Every cascade is re-checked against the full stock trajectory.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd

from gen_structure import split_catalog
from sc_common import Ctx, banner, log, make_ids, rng_for, write_table

EPS = 1e-6
MARGIN = 0.01  # cascade conditions must hold by more than the 3-decimal rounding of stored quantities
NONRM_REASONS = {"changeover_overrun": 0.30, "equipment_breakdown": 0.20, "labor_shortage": 0.15, "qa_hold": 0.15, "schedule_change": 0.15, "utility_outage": 0.05}
SLIP_CAUSES = {"supplier_capacity": 0.30, "raw_material_shortage_at_supplier": 0.20, "transport_disruption": 0.18, "quality_hold_at_supplier": 0.14, "labor_shortage_at_supplier": 0.10, "weather_force_majeure": 0.08}


# =============================================================================
# production runs
# =============================================================================
def build_runs(ctx: Ctx, prod: pd.DataFrame, plants: pd.DataFrame) -> pd.DataFrame:
    cfg = ctx.cfg["plants"]
    s = ctx.src
    rng = ctx.rng("runs")
    lines = s["purchase_order_lines"].merge(s["purchase_orders"], on="purchase_order_id")
    lines = lines.merge(prod[["product_id", "plant_id", "sourcing_mode"]], on="product_id")
    lines = lines[(lines.sourcing_mode == "make") & (lines.status != "cancelled")].copy()
    xy = cfg["region_xy"]
    preg = plants.set_index("plant_id").region
    wreg = s["warehouses"].set_index("warehouse_id").region
    pr = lines.plant_id.map(preg)
    wr = lines.warehouse_id.map(wreg)
    dist = np.array([math.dist(xy.get(a, [0, 0]), xy.get(b, [0, 0])) for a, b in zip(pr, wr)])
    transit = cfg["transit_days_base"] + np.round(cfg["transit_days_per_unit_distance"] * dist).astype(int)
    run_days = {p: int(rng.integers(cfg["run_days"][0], cfg["run_days"][1] + 1)) for p in plants.plant_id}
    qa_days = {p: int(rng.integers(cfg["qa_release_days"][0], cfg["qa_release_days"][1] + 1)) for p in plants.plant_id}
    lead = lines.plant_id.map(run_days).values + lines.plant_id.map(qa_days).values + transit
    po_lead = (lines.expected_at - lines.ordered_at).dt.days.values
    lead = np.minimum(lead, np.maximum(1, po_lead - 1))
    lines["planned_lead"] = lead
    lines["planned_start"] = lines.expected_at - pd.to_timedelta(lead, unit="D")
    executed = lines.received_at.notna() & (lines.quantity_received > 0)
    noise = rng.choice([0, 1, 2], p=[0.7, 0.2, 0.1], size=len(lines))
    lines["transport_noise"] = np.where(executed, noise, 0)
    lines["actual_start"] = (lines.received_at - pd.to_timedelta(lines.planned_lead + lines.transport_noise, unit="D")).where(executed)
    lines["delay_days"] = (lines.actual_start - lines.planned_start).dt.days
    lines = lines.sort_values(["planned_start", "product_id", "purchase_order_line_id"]).reset_index(drop=True)
    lines["production_run_id"] = make_ids("PR", len(lines), 7)
    lines["planned_qty"] = lines.quantity_ordered.astype(int)
    lines["produced_qty"] = lines.quantity_received.astype(int)
    lines["executed"] = lines.actual_start.notna() & (lines.produced_qty > 0)
    return lines


def bom_at(bom: pd.DataFrame, runs: pd.DataFrame, date_col: str, qty_col: str) -> pd.DataFrame:
    b = bom[["product_id", "rm_id", "qty_per_unit", "scrap_pct", "effective_from", "effective_to"]]
    m = runs[["production_run_id", "product_id", "plant_id", date_col, qty_col]].merge(b, on="product_id")
    eff_to = m.effective_to.fillna(pd.Timestamp("2262-01-01"))
    m = m[(m[date_col] >= m.effective_from) & (m[date_col] <= eff_to)]
    m = m.assign(req=np.round(m[qty_col].values * m.qty_per_unit.values * (1 + m.scrap_pct.values / 100), 3))
    return m[["production_run_id", "product_id", "plant_id", "rm_id", date_col, "req"]]


# =============================================================================
# per (rm, plant) simulation
# =============================================================================
class World:
    """Shared, read-only simulation inputs."""

    def __init__(self, ctx: Ctx, S: dict, runs: pd.DataFrame, plants: pd.DataFrame):
        self.ctx, self.cfg, self.seed = ctx, ctx.cfg, ctx.seed
        self.rcfg = ctx.cfg["rm_ops"]
        self.pcfg = ctx.cfg["patterns"]
        self.plan = S["pattern_plan"]
        self.rms = S["rms"].set_index("rm_id")
        sup = ctx.src["suppliers"].set_index("supplier_id")
        self.rel = sup.reliability_score.to_dict()
        self.country = sup.country_code.to_dict()
        self.win_end = pd.Timestamp(ctx.calib["window_end"])
        first = min(runs.planned_start.min(), runs.actual_start.min())
        self.t0 = (first - pd.Timedelta(days=int(self.rcfg["ss_days_base"]) + 7)).normalize()
        self.t0 = self.t0 - pd.Timedelta(days=self.t0.dayofweek)
        self.T_end = (self.win_end - self.t0).days
        self.T = self.T_end + 1 + 240
        self.dates = pd.date_range(self.t0, periods=self.T, freq="D")
        self.plant_idx = {p: i for i, p in enumerate(plants.plant_id)}
        self.header_cache: dict = {}
        self.p04_values: list[float] = []
        self.lid = 0
        self.spike_windows = []
        if "P03" in self.plan:
            for d in self.plan["P03"]["spike_dates"]:
                a = self.day(pd.Timestamp(d))
                self.spike_windows.append((a + 21, a + 75))

    def day(self, ts) -> int:
        return int((pd.Timestamp(ts) - self.t0).days)

    def next_lid(self) -> int:
        self.lid += 1
        return self.lid

    # --------------------------------------------------------------- delay model
    def header_draw(self, sup: str, plant: str, t: int, lead: int, rm: str, value: float, small_thr: float | None) -> dict:
        p04 = self.plan.get("P04", {}).get("supplier_id") == sup
        if p04:
            # "small" = at or below the running median order value of this supplier (what a buyer would notice)
            hist = self.p04_values
            small_thr = float(np.median(hist)) if len(hist) >= 5 else value
            hist.append(value)
        key = (sup, plant, t, lead) + ((rm, value <= (small_thr or 0), len(self.p04_values)) if p04 else ())
        if key in self.header_cache:
            return self.header_cache[key]
        r = rng_for(self.seed, "hdr", *key)
        rc, pc = self.rcfg, self.pcfg
        promised = t + lead
        pdate = self.dates[min(promised, self.T - 1)]
        rel = self.rel[sup]
        p_late = (1 - rel) * rc["late_prob_multiplier"]
        if p04:
            p_late = pc["small_po_late_prob"][0] if value <= (small_thr or 0) else pc["small_po_late_prob"][1]
        u = r.random()
        if u < p_late:
            base = int(math.ceil(r.gamma(rc["late_gamma_shape"], rc["late_scale_per_lead_day"] * lead + 1)))
        elif u < p_late + rc["early_prob"]:
            base = -int(r.integers(1, 3))
        else:
            base = 0
        planted, tags = 0, []
        cause = str(r.choice(list(SLIP_CAUSES), p=np.array(list(SLIP_CAUSES.values())) / sum(SLIP_CAUSES.values())))
        if self.plan.get("P01", {}).get("supplier_id") == sup and pdate.month in (11, 12):
            planted += int(r.integers(pc["q4_slip_days"][0], pc["q4_slip_days"][1] + 1)); tags.append("P01"); cause = "supplier_capacity"
        if self.plan.get("P02", {}).get("country_code") == self.country[sup] and pdate.month == 2 and r.random() < pc["feb_congestion_prob"]:
            planted += int(r.integers(pc["feb_congestion_days"][0], pc["feb_congestion_days"][1] + 1)); tags.append("P02"); cause = "port_congestion"
        if self.plan.get("P11", {}).get("supplier_id") == sup and pdate >= pd.Timestamp(self.plan["P11"]["from"]):
            months = (pdate.year - 2025) * 12 + pdate.month
            planted += int(round(pc["leadtime_creep_days_per_month"] * months)); tags.append("P11"); cause = "supplier_capacity"
        if self.plan.get("P03", {}).get("supplier_id") == sup and any(a <= promised <= b for a, b in self.spike_windows) and r.random() < 0.9:
            planted += int(r.integers(10, 21)); tags.append("P03"); cause = "allocation_after_price_increase"
        delay = base + planted if planted == 0 or base > 0 else planted
        out = {"delay": int(delay), "tags": tags, "cause": cause, "announce": False, "revs": []}
        if delay > 0:
            ann_p = 0.85 if tags else rc["announce_prob"]
            if r.random() < ann_p:
                out["announce"] = True
                notice = int(r.integers(rc["notice_days"][0], rc["notice_days"][1] + 1))
                ra = max(t + 1, promised - notice)
                ra = min(ra, promised - 1) if promised - 1 > t else t + 1
                p09 = self.plan.get("P09", {}).get("supplier_id") == sup
                sp = pc["recovery_breach_prob"] if p09 else rc["second_slip_prob"]
                if delay >= (2 if p09 else 3) and r.random() < sp:
                    d1 = max(1, delay - int(r.integers(2, 7))) if delay >= 3 else 1
                    out["revs"].append([ra, promised, promised + d1, cause])
                    if r.random() < (0.15 if p09 else 0.5) and promised + d1 - 1 > ra:
                        out["revs"].append([promised + d1 - 1, promised + d1, promised + delay, cause])
                    out["second_slip"] = True
                else:
                    out["revs"].append([ra, promised, promised + delay, cause])
        self.header_cache[key] = out
        return out


class Combo:
    def __init__(self, w: World, rm: str, plant: str, runs_rm: pd.DataFrame, params: dict):
        self.w, self.rm, self.plant, self.p = w, rm, plant, params
        T = w.T
        self.run_ids = runs_rm.production_run_id.values
        self.run_pd = runs_rm.pd.values.astype(int)
        self.run_ad = runs_rm.ad.values.astype(int)
        self.run_preq = runs_rm.preq.values.astype(float)
        self.run_areq = runs_rm.areq.values.astype(float)
        self.cons = np.zeros(T)
        ex = self.run_ad < T
        np.add.at(self.cons, self.run_ad[ex], self.run_areq[ex])
        self.cons_cum = np.cumsum(self.cons)
        self.dbar = float(self.run_preq[self.run_pd <= w.T_end].sum()) / max(1, w.T_end)
        self.xin = np.zeros(T)
        self.xout = np.zeros(T)
        self.lines: list[dict] = []
        self.blocked: list[tuple[int, int]] = []   # (from_day, to_day_exclusive) windows where a scheduled run was blocked
        self.protect: list[tuple[int, int]] = []   # [t_revised, last landing day] of committed cascades
        self.constraints: list[tuple[int, int, float, int, float]] = []  # (P, X, req, b, ss_b) of committed cascades
        self.ss_arr = np.zeros(T)

    # --------------------------------------------------------------- helpers
    def ss_days_at(self, t: int) -> float:
        v = self.p["ss_days"]
        for d, extra in self.p.get("ss_steps", []):
            if t >= d:
                v = extra
        return v

    def inflow(self) -> np.ndarray:
        a = np.zeros(self.w.T)
        for l in self.lines:
            if l["arrival"] is not None and l["arrival"] < self.w.T:
                a[l["arrival"]] += l["qrec"] - l["reject"]
        return a

    def stock(self, inflow: np.ndarray | None = None) -> np.ndarray:
        inflow = self.inflow() if inflow is None else inflow
        return self.opening + np.cumsum(inflow + self.xin - self.xout - self.cons)

    def known_expected(self, l: dict, t: int) -> int:
        e = l["promised"]
        for ra, old, new, _ in l["revs"]:
            if ra <= t:
                e = new
        return e

    def ok(self, S: np.ndarray) -> bool:
        """Global stock sanity + every committed cascade still holds on trajectory S."""
        if S[: self.w.T_end + 1].min() < -EPS:
            return False
        for P, X, req, b, ssb in self.constraints:
            if S[P:X].max() >= req - MARGIN or S[b] >= ssb - MARGIN:
                return False
        return True

    # --------------------------------------------------------------- MRP
    def simulate(self):
        w, p, rc = self.w, self.p, self.w.rcfg
        T_end = w.T_end
        ss_days = np.array([self.ss_days_at(t) for t in range(w.T)])
        big_run = float(np.quantile(self.run_preq[self.run_preq > 0], 0.9)) if (self.run_preq > 0).any() else 0.0
        # safety stock covers both average usage (days of cover) and one large batch (lumpy schedules)
        self.ss_arr = np.maximum(ss_days * self.dbar, big_run * ss_days / max(1e-9, ss_days[0]))
        R = p["review_days"]
        max_lead = max(r["lead"] for r in p["rows"])
        H0 = max_lead + R + rc["opening_cover_extra_days"]
        m0 = self.run_pd < H0
        self.opening = round(float(self.ss_arr[0] + self.run_preq[m0].sum()), 3)
        inflow = np.zeros(w.T)
        in_cum_until = 0.0
        pending: list[dict] = []
        ordered_by: dict[str, float] = defaultdict(float)
        t = (p["review_weekday"] - w.t0.dayofweek) % 7
        last_t = 0
        while t <= T_end:
            in_cum_until += inflow[last_t:t].sum()
            last_t = t
            active = [r for r in p["rows"] if r["from"] <= t <= r["to"] and r["qualified"] and r["alloc"] > 0]
            if active:
                tot = sum(ordered_by.values())
                s_row = max(active, key=lambda r: (r["alloc"] / 100 * (tot + 1) - ordered_by[r["sup"]], r["alloc"]))
                Wn = max(r["lead"] for r in active) + R + 1
                onhand = self.opening + in_cum_until + self.xin[:t].sum() - self.xout[:t].sum() - (self.cons_cum[t - 1] if t > 0 else 0.0)
                pending = [l for l in pending if l["arrival"] is None or l["arrival"] >= t]
                # time-phased projected available balance over [t, t+Wn)
                arr_w = np.zeros(Wn)
                for l in pending:
                    e = self.known_expected(l, t) - t
                    arr_w[min(max(e, 0), Wn - 1)] += l["qty"] if e < Wn else 0.0
                mreq = (self.run_ad >= t) & (np.maximum(self.run_pd, t) < t + Wn)
                req_w = np.bincount(np.maximum(self.run_pd[mreq], t) - t, weights=self.run_preq[mreq], minlength=Wn)[:Wn]
                pab = onhand + np.cumsum(arr_w - req_w)
                ssw = self.ss_arr[t: t + Wn] if t + Wn <= w.T else np.full(Wn, self.ss_arr[-1])
                dip = np.where(pab < ssw - EPS)[0]
                if len(dip) and dip[0] < s_row["lead"]:
                    faster = [r for r in active if r["lead"] <= max(1, dip[0])]
                    if faster:
                        s_row = max(faster, key=lambda r: (r["alloc"], -r["lead"]))
                lead = s_row["lead"]
                lo, hi = min(lead, Wn - 1), min(lead + R, Wn)
                need = float((ssw[lo:hi] - pab[lo:hi]).max()) if hi > lo else 0.0
                if need > EPS:
                    q = max(s_row["moq"], need)
                    q = float(math.ceil(q)) if q >= 10 else math.ceil(q * 100) / 100
                    l = self.new_line(s_row, t, q, "regular")
                    pending.append(l)
                    ordered_by[s_row["sup"]] += q
                    if l["arrival"] is not None and l["arrival"] < w.T:
                        inflow[l["arrival"]] += l["qrec"] - l["reject"]
            t += R
        self.safety_pass()

    def new_line(self, row: dict, t: int, q: float, otype: str, arrival_override: int | None = None) -> dict:
        w = self.w
        lid = w.next_lid()
        value = q * row["price"]
        l = {"lid": lid, "rm": self.rm, "plant": self.plant, "sup": row["sup"], "ordered": t, "lead": row["lead"],
             "promised": t + row["lead"], "qty": q, "price": row["price"], "otype": otype, "revs": [], "tags": [],
             "cause": None, "cancelled": False, "cascade": None, "reject": 0.0, "pulled_in": False, "emergency": False}
        if arrival_override is None:
            hd = w.header_draw(row["sup"], self.plant, t, row["lead"], self.rm, value, self.p.get("small_thr"))
            l["arrival"] = max(t + 1, l["promised"] + hd["delay"])
            l["revs"] = [list(r) for r in hd["revs"]]
            l["tags"] = list(hd["tags"])
            l["cause"] = hd["cause"] if hd["delay"] > 0 else None
            l["second_slip"] = hd.get("second_slip", False)
        else:
            l["promised"] = arrival_override
            l["arrival"] = arrival_override
            l["second_slip"] = False
        r = rng_for(w.seed, "line", self.rm, self.plant, t, otype, round(q, 3))
        rel = w.rel[row["sup"]]
        qrec = q
        if otype == "regular" and r.random() < (1 - rel) * w.rcfg["partial_prob_multiplier"]:
            qrec = round(q * float(r.uniform(*w.rcfg["partial_fill"])), 3)
        l["qrec"] = qrec
        p07 = w.plan.get("P07")
        rej_p, rej_f = w.rcfg["quality_reject_prob"], w.rcfg["quality_reject_frac"]
        if p07 and self.rm == p07["new_rm"] and self.plant == self.p.get("p07_plant") and l["arrival"] >= w.day(p07["date"]):
            rej_p, rej_f = 0.45, [0.05, 0.15]
            l["tags"].append("P07")
        if otype == "regular" and arrival_override is None and r.random() < rej_p:
            l["reject"] = round(qrec * float(r.uniform(*rej_f)), 3)
        self.lines.append(l)
        return l

    def safety_pass(self):
        w = self.w
        for _ in range(200):
            S = self.stock()
            neg = np.where(S[: w.T_end + 1] < -EPS)[0]
            if len(neg) == 0:
                return
            d = int(neg[0])
            cands = [l for l in self.lines if not l["cancelled"] and l["arrival"] is not None and l["promised"] <= d < l["arrival"]]
            if cands:
                l = min(cands, key=lambda z: (z["promised"], z["lid"]))
                l["arrival"] = max(d, l["ordered"] + 1)
                l["pulled_in"] = True
                l["revs"] = [r for r in l["revs"] if r[0] < l["arrival"]]
                if l["revs"]:
                    l["revs"][-1][2] = l["arrival"]
                continue
            deficit = -float(S[d])
            row = self.best_row(d)
            q = float(math.ceil(deficit + self.ss_arr[d] + 1))
            nl = self.new_line(row, max(0, d - 3), q, "spot_buy", arrival_override=d)
            nl["emergency"] = True
            nl["promised"] = d
        raise RuntimeError(f"safety pass did not converge for {self.rm}/{self.plant}")

    def best_row(self, t: int, exclude: str | None = None) -> dict:
        rows = [r for r in self.p["rows"] if r["from"] <= t <= r["to"] and r["qualified"] and r["sup"] != exclude]
        if not rows:
            rows = [r for r in self.p["rows"] if r["qualified"] and r["sup"] != exclude] or self.p["rows"]
            rows = sorted(rows, key=lambda r: abs(r["from"] - t))
        return max(rows, key=lambda r: r["alloc"])


# =============================================================================
# cascades
# =============================================================================
def try_cascade(c: Combo, i: int, kind: str, response: str, r: np.random.Generator, donors: list, cid: str) -> dict | None:
    w, rc = c.w, c.w.rcfg
    P, A = int(c.run_pd[i]), int(c.run_ad[i])
    req, preq = float(c.run_areq[i]), float(c.run_preq[i])
    if A - P < 1 or A > w.T_end or P < 30:
        return None
    delta = int(r.integers(0, 2)) if A - P >= 2 else 0
    X = A - delta
    base_in = c.inflow()
    arr = np.array([l["arrival"] if (l["arrival"] is not None and not l["cancelled"]) else -1 for l in c.lines])
    net = np.array([l["qrec"] - l["reject"] for l in c.lines])
    for L in (0, 7, 14, 21, 35, 49):
        W0 = P - L
        idx = np.where((arr >= W0) & (arr < X) & (arr <= w.T_end))[0]
        if len(idx) == 0:
            continue
        # skip if any slipped line was touched by an earlier cascade or is an emergency buy
        if any(c.lines[j]["cascade"] or c.lines[j]["emergency"] for j in idx):
            return None

        inflow = base_in.copy()
        for j in idx:
            inflow[arr[j]] -= net[j]
        slip_to = X
        if response in ("expedite", "switch_supplier", "rm_transfer"):
            slip_to = X + int(r.integers(5, 16))
        # the material that ends the shortage lands on X
        land_extra = 0.0
        partial_cut = 0.0
        if response in ("accept_delay", "expedite"):
            for j in idx:
                inflow[slip_to if response == "accept_delay" else X] += net[j]
        else:
            for j in idx:
                if slip_to < w.T:
                    inflow[slip_to] += net[j]
        S = c.opening + np.cumsum(inflow + c.xin - c.xout - c.cons)
        spot = None
        transfer = None
        if response == "switch_supplier":
            other = [rr for rr in c.p["rows"] if rr["qualified"] and rr["sup"] != c.lines[idx[0]]["sup"] and rr["from"] <= P <= rr["to"]]
            if not other:
                return None
            row = max(other, key=lambda z: z["alloc"])
            q = float(math.ceil(max(req, float(net[idx].sum())) * 1.0 + 1))
            spot = (row, q)
            S = S + np.where(np.arange(w.T) >= X, q, 0.0)
        if response == "rm_transfer":
            q = float(math.ceil(req + 1))
            best = None
            p08 = w.plan.get("P08", {}).get("plant_id")
            for dc in sorted(donors, key=lambda z: z.plant != p08):
                if dc is c:
                    continue
                Sd = dc.stock()
                for d_dec in range(P - 2, P - 12, -1):
                    lo = d_dec
                    if lo < 1 or X - lo < 1:
                        continue
                    Sa = Sd - np.where(np.arange(w.T) >= lo, q, 0.0)
                    if not dc.ok(Sa):
                        continue
                    hz = slice(lo, min(lo + 22, w.T_end + 1))
                    dips = bool((Sa[hz] < dc.ss_arr[hz] - EPS).any())
                    # planted P08: the preferred donor plant is drained below safety stock; other donors must stay above it
                    if (dc.plant == p08 and dips) or (dc.plant != p08 and not dips):
                        best = (dc, lo, q)
                        break
                if best:
                    break
            if not best:
                return None
            transfer = best
            S = S + np.where(np.arange(w.T) >= X, q, 0.0)
        if kind == "partial":
            # supplier short-shipped the landing lot: the run could only make produced_qty
            before_A = S[A] + req  # stock available at A before this run's consumption
            cut = before_A - preq + max(0.5, 0.02 * preq)
            if cut > 0:
                land = float(net[idx].sum())
                if cut > 0.9 * land or response != "accept_delay":
                    return None
                partial_cut = cut
                S = S - np.where(np.arange(w.T) >= X, cut, 0.0)
        # (a) never negative inside the window horizon
        if S[: w.T_end + 1].min() < -EPS:
            if partial_cut > 0:
                # replacement lot for the short quantity arrives one primary lead time later
                row = c.best_row(X)
                land_day = X + max(3, int(row["lead"] * 0.5))
                S2 = S + np.where(np.arange(w.T) >= land_day, partial_cut, 0.0)
                if S2[: w.T_end + 1].min() < -EPS:
                    continue
                land_extra = land_day
                S = S2
            else:
                continue
        elif partial_cut > 0:
            row = c.best_row(X)
            land_extra = X + max(3, int(row["lead"] * 0.5))
            S = S + np.where(np.arange(w.T) >= land_extra, partial_cut, 0.0)
        # (b) run could not start anywhere in [P, X)
        if (S[P:X] >= req - MARGIN).any():
            continue
        if kind == "partial" and S[A] + req >= preq - EPS:
            continue
        # revision dates, then (c) stock below safety strictly after the first revision and on/before P
        revd = {}
        notice = int(r.integers(2, 11))
        first_arr = min(int(c.lines[j]["arrival"]) for j in idx)
        latest_order = max(int(c.lines[j]["ordered"]) for j in idx)
        ra = min(first_arr, P) - notice
        ra = max(ra, latest_order + 1)
        ra = min(ra, min(first_arr, P) - 1)
        if ra <= latest_order:
            continue
        for j in idx:
            revd[j] = ra
        t1 = ra
        below = np.where(S[t1 + 1: P + 1] < c.ss_arr[t1 + 1: P + 1] - MARGIN)[0]
        if len(below) == 0:
            continue
        b = t1 + 1 + int(below[0])
        if not (t1 < b <= P < A):
            continue
        d_dec = None
        if response in ("expedite", "switch_supplier", "rm_transfer"):
            lo, hi = t1 + 1, min(P, X - (3 if response == "expedite" else 2))
            if response == "rm_transfer":
                d_dec = transfer[1]
                if not (lo <= d_dec <= hi):
                    continue
            else:
                if hi < lo:
                    continue
                d_dec = int(r.integers(lo, hi + 1))
        # ---------------- commit ------------------------------------------------
        cancel_orig = False
        if response == "switch_supplier" and r.random() < 0.4:
            # cancel the slipped PO if stock still holds without it
            Sc = S - np.where(np.arange(w.T) >= slip_to, float(net[idx].sum()), 0.0)
            if c.ok(Sc):
                cancel_orig = True
                S = Sc
        if not c.ok(S):
            continue
        cause = r.choice(list(SLIP_CAUSES), p=np.array(list(SLIP_CAUSES.values())) / sum(SLIP_CAUSES.values()))
        tags = sorted({t for j in idx for t in c.lines[j]["tags"]})
        if "P03" in tags:
            cause = "allocation_after_price_increase"
        elif "P02" in tags:
            cause = "port_congestion"
        elif "P01" in tags or "P11" in tags:
            cause = "supplier_capacity"
        slipped = []
        for j in idx:
            l = c.lines[j]
            old_exp = c.known_expected(l, revd[j])
            orig_arrival = l["arrival"]
            l["revs"] = [rv for rv in l["revs"] if rv[0] < revd[j]]
            l["revs"].append([revd[j], old_exp, slip_to, str(cause)])
            if response == "expedite":
                l["revs"].append([d_dec, slip_to, X, "expedite_air_freight"])
                l["expedite"] = True
                l["arrival"] = X
            elif cancel_orig:
                l["cancelled"] = True
                l["cancel_day"] = d_dec
                l["arrival"] = None
            else:
                l["arrival"] = slip_to
            l["cascade"] = cid
            l["cause"] = str(cause)
            slipped.append({"lid": l["lid"], "orig_arrival": orig_arrival, "revised_at": revd[j]})
        new_lines = []
        if partial_cut > 0:
            main = c.lines[idx[-1]]
            main["qrec"] = round(main["qrec"] - partial_cut, 3)
            main["short_shipped"] = True
            row = c.best_row(X)
            nl = c.new_line(row, X, float(math.ceil(partial_cut * 100) / 100), "replacement", arrival_override=land_extra)
            nl["ordered"] = X
            nl["promised"] = land_extra
            nl["cascade"] = cid
            new_lines.append(nl["lid"])
        if spot is not None:
            row, q = spot
            nl = c.new_line(row, d_dec, q, "spot_buy", arrival_override=X)
            nl["ordered"] = d_dec
            nl["promised"] = X
            nl["cascade"] = cid
            nl["spot_premium"] = True
            new_lines.append(nl["lid"])
        tr = None
        if transfer is not None:
            dc, lo, q = transfer
            dc.xout[lo] += q
            c.xin[X] += q
            tr = {"donor_plant": dc.plant, "out_day": lo, "in_day": X, "qty": q, "rm": c.rm, "receiver_plant": c.plant, "cascade": cid}
        c.blocked.append((P, X))
        c.protect.append((t1, max(X, slip_to if slip_to < w.T else X, land_extra or X) + 1))
        c.constraints.append((P, X, req, b, float(c.ss_arr[b])))
        if transfer is not None:
            transfer[0].protect.append((transfer[1], transfer[1] + 1))
        return {"cascade_id": cid, "rm_id": c.rm, "plant_id": c.plant, "run_idx": i, "production_run_id": c.run_ids[i],
                "kind": kind, "response": "cancel_and_switch" if cancel_orig else response, "t_revised": t1, "t_below_ss": b,
                "P": P, "A": A, "X": X, "slip_to": slip_to, "d_dec": d_dec, "slipped": slipped, "new_lids": new_lines,
                "transfer": tr, "cause": str(cause), "tags": tags, "req": req, "preq": preq,
                "min_stock_window": float(S[P:X].min()), "ss_at_P": float(c.ss_arr[P]), "partial_cut": partial_cut}
    return None


# =============================================================================
# main
# =============================================================================
def build_params(w: World, S: dict, catalog: pd.DataFrame, combos_req: pd.DataFrame, mitig: dict) -> dict:
    rc = w.rcfg
    rms = S["rms"].set_index("rm_id")
    plan = S["pattern_plan"]
    planted_sups = {plan.get(k, {}).get("supplier_id") for k in ("P01", "P09", "P11")} - {None}
    p02c = plan.get("P02", {}).get("country_code")
    cat_by_rm = {k: g for k, g in catalog.groupby("rm_id")}
    params = {}
    for (rm, plant), g in combos_req.groupby(["rm_id", "plant_id"]):
        rows = []
        cg = cat_by_rm[rm]
        for r in cg.itertuples():
            rows.append({"sup": r.supplier_id, "alloc": float(r.allocation_pct), "lead": int(r.lead_time_days), "moq": float(r.moq),
                         "price": float(r.unit_price), "from": w.day(r.price_valid_from), "to": w.day(r.price_valid_to), "qualified": int(r.qualified_flag)})
        crit = rms.at[rm, "criticality"]
        ss = rc["ss_days_base"] + rc["ss_days_by_criticality"].get(crit, 0)
        sups = set(cg.supplier_id)
        if sups & planted_sups or any(w.country[s] == p02c for s in sups):
            ss += rc["ss_planted_extra_days"]
        prm = {"rows": rows, "ss_days": float(ss), "review_days": rc["review_days_fast"] if crit in rc["fast_review_criticality"] else rc["review_days_slow"],
               "review_weekday": w.plant_idx[plant] % 5, "ss_steps": []}
        if "P04" in plan and plan["P04"]["supplier_id"] in sups:
            daily = float(g.preq.sum()) / max(1, w.T_end)
            price = float(cg[cg.supplier_id == plan["P04"]["supplier_id"]].unit_price.median())
            prm["small_thr"] = daily * prm["review_days"] * price
        if "P07" in plan and rm == plan["P07"]["new_rm"]:
            prm["p07_plant"] = S["prod"].set_index("product_id").at[plan["P07"]["product_id"], "plant_id"]
        for step in mitig.get("ss_builds", []):
            if step["rm_id"] == rm and step["plant_id"] == plant:
                prm["ss_steps"].append((step["day"], step["new_ss_days"]))
        params[(rm, plant)] = prm
    return params


def plan_mitigations(w: World, combos: dict, S: dict, catalog: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Safety-stock builds after repeated near-misses; allocation changes on poor performers."""
    rc = w.rcfg
    r = w.ctx.rng("mitigations")
    plan = S["pattern_plan"]
    protected_rms = {plan.get("P03", {}).get("rm_id")}
    protected_sups = {plan.get(k, {}).get("supplier_id") for k in ("P01", "P03", "P04", "P09", "P11")}
    # near-miss count per combo per quarter: days below SS
    cand = []
    q_edges = pd.date_range(w.t0, w.win_end, freq="QS")
    for key, c in combos.items():
        if key[0] in protected_rms:
            continue
        Sx = c.stock()[: w.T_end + 1]
        below = Sx < c.ss_arr[: w.T_end + 1] * 0.6
        for a, b in zip(q_edges[:-1], q_edges[1:]):
            da, db = w.day(a), w.day(b)
            if da < 60 or db > w.day(pd.Timestamp(w.ctx.calib["holdout_start"])) - 30:
                continue
            nb = int(below[da:db].sum())
            if nb >= 4:
                cand.append((nb, key, db))
    cand.sort(key=lambda z: -z[0])
    ss_builds, used = [], set()
    n_ss = max(3, int(round(len(combos) * 0.03)))
    for nb, key, db in cand:
        if key in used:
            continue
        used.add(key)
        day = db + int(r.integers(3, 12))
        day = day - int(w.dates[day].dayofweek)
        old = combos[key].ss_days_at(day)
        ss_builds.append({"rm_id": key[0], "plant_id": key[1], "day": day, "date": w.dates[day], "old_ss_days": old,
                          "new_ss_days": old + int(r.integers(*rc["ss_build_extra_days"])), "trigger_days_below": nb})
        if len(ss_builds) >= n_ss:
            break
    # allocation changes: primary late share over the first 18 months (base simulation)
    perf = defaultdict(lambda: [0, 0])
    cut = w.day(w.t0 + pd.DateOffset(months=18))
    for c in combos.values():
        for l in c.lines:
            if l["ordered"] < cut and l["arrival"] is not None:
                perf[(l["rm"], l["sup"])][0] += int(l["arrival"] > l["promised"] + 2)
                perf[(l["rm"], l["sup"])][1] += 1
    alloc_changes = []
    prim = catalog[(catalog.sourcing_rank == "primary") & (catalog.change_driver == "initial")]
    scored = []
    for pr in prim.itertuples():
        if pr.rm_id in protected_rms or pr.supplier_id in protected_sups:
            continue
        late, n = perf[(pr.rm_id, pr.supplier_id)]
        if n >= 6:
            scored.append((late / n, pr.rm_id, pr.supplier_id))
    scored.sort(reverse=True)
    for lr, rm, sup in scored[: rc["allocation_changes"]]:
        others = catalog[(catalog.rm_id == rm) & (catalog.supplier_id != sup)].drop_duplicates("supplier_id")
        date = (w.t0 + pd.DateOffset(months=int(r.integers(19, 28)))).normalize()
        if others.empty:
            continue
        o = others.iloc[0]
        if o.qualified_flag == 0:
            kind = "switch_supplier"  # qualify the dormant secondary and move volume
            upd = {sup: {"allocation_pct": 60.0, "sourcing_rank": "primary"}, o.supplier_id: {"allocation_pct": 40.0, "qualified_flag": 1, "sourcing_rank": "secondary"}}
        elif r.random() < 0.5:
            kind = "switch_supplier"
            upd = {sup: {"allocation_pct": float(o.allocation_pct), "sourcing_rank": "secondary"}, o.supplier_id: {"allocation_pct": float(100 - o.allocation_pct if len(others) == 1 else o.allocation_pct + 20), "sourcing_rank": "primary"}}
        else:
            kind = "reduce_allocation"
            upd = {sup: {"allocation_pct": 50.0}, o.supplier_id: {"allocation_pct": 50.0 if len(others) == 1 else float(o.allocation_pct + 20)}}
        # keep allocations summing to 100 over qualified suppliers
        tot = 0
        for s_, u in upd.items():
            tot += u.get("allocation_pct", 0)
        rest = others[~others.supplier_id.isin(upd)].supplier_id.tolist()
        for s_ in rest:
            upd[s_] = {"allocation_pct": max(0.0, 100.0 - tot) / len(rest)} if tot < 100 else {"allocation_pct": 0.0}
        catalog = split_catalog(catalog, rm, date, upd, kind)
        alloc_changes.append({"rm_id": rm, "old_primary": sup, "new_partner": o.supplier_id, "date": date, "kind": kind,
                              "late_share_before": round(lr, 3), "update": upd, "qualified_new": int(o.qualified_flag == 0)})
    return {"ss_builds": ss_builds, "alloc_changes": alloc_changes}, catalog


def run(ctx: Ctx):
    banner("STAGE 1b - production runs, RM MRP simulation, cascade back-fill")
    S = ctx.load_internal("structure")
    prod, rms, catalog = S["prod"], S["rms"], S["catalog"]
    plants = ctx.table("plants")
    bom = ctx.table("bill_of_materials").copy()
    for c_ in ("effective_from", "effective_to"):
        bom[c_] = pd.to_datetime(bom[c_])
    rc = ctx.cfg["rm_ops"]

    runs = build_runs(ctx, prod, plants)
    w = World(ctx, S, runs, plants)
    runs["pd"] = (runs.planned_start - w.t0).dt.days
    runs["ad"] = ((runs.actual_start - w.t0).dt.days).fillna(10 ** 7).astype(int)
    preq = bom_at(bom, runs, "planned_start", "planned_qty").rename(columns={"req": "preq"})
    areq = bom_at(bom, runs[runs.executed], "actual_start", "produced_qty").rename(columns={"req": "areq"})
    cr = preq[["production_run_id", "rm_id", "plant_id", "preq"]].merge(areq[["production_run_id", "rm_id", "areq"]], on=["production_run_id", "rm_id"], how="outer")
    cr["plant_id"] = cr.plant_id.fillna(cr.production_run_id.map(runs.set_index("production_run_id").plant_id))
    cr = cr.fillna({"preq": 0.0, "areq": 0.0})
    cr = cr.merge(runs[["production_run_id", "pd", "ad"]], on="production_run_id")
    cr.loc[cr.areq <= 0, "ad"] = 10 ** 7
    log(f"production runs: {len(runs):,} (executed {int(runs.executed.sum()):,}); run x RM requirement rows: {len(cr):,}; RM ledger starts {w.t0.date()}")

    # ---------------- base simulation (for mitigation planning) --------------
    def simulate_all(params):
        w.header_cache.clear()
        combos = {}
        for key, g in cr.groupby(["rm_id", "plant_id"]):
            cb = Combo(w, key[0], key[1], g.sort_values("pd"), params[key])
            cb.simulate()
            combos[key] = cb
        return combos

    params = build_params(w, S, catalog, cr, {})
    base = simulate_all(params)
    log(f"base simulation: {len(base)} rm-plant combos, {sum(len(c.lines) for c in base.values()):,} RM PO lines")
    mitig, catalog = plan_mitigations(w, base, S, catalog)
    log(f"mitigations: {len(mitig['ss_builds'])} safety-stock builds, {len(mitig['alloc_changes'])} allocation changes")
    w.lid = 0
    params = build_params(w, S, catalog, cr, mitig)
    combos = simulate_all(params)

    # ---------------- cascade back-fill ------------------------------------------
    lines_fg = runs[runs.executed].copy()
    late = lines_fg.delay_days >= rc["cascade_min_prod_delay"]
    short = (lines_fg.status == "partial") & (lines_fg.delay_days >= 1)
    elig = lines_fg[late | short]
    target = int(round(rc["cascade_target_share"] * len(elig)))
    r = ctx.rng("cascade_select")
    plan = S["pattern_plan"]
    p03rm = plan.get("P03", {}).get("rm_id")
    p01s = plan.get("P01", {}).get("supplier_id")
    cr_idx = {k: g for k, g in cr.groupby("production_run_id")}
    prim_sup = catalog[catalog.sourcing_rank == "primary"].groupby("rm_id").supplier_id.first().to_dict()
    rms_i = rms.set_index("rm_id")
    usage = cr.groupby(["rm_id", "plant_id"]).size().to_dict()
    pri = np.zeros(len(elig))
    for k, (_, row) in enumerate(elig.iterrows()):
        rm_list = cr_idx[row.production_run_id].rm_id.tolist()
        P = w.day(row.planned_start)
        if p03rm in rm_list and any(a <= P <= b for a, b in w.spike_windows):
            pri[k] = 3
        elif row.planned_start.month in (11, 12) and any(prim_sup.get(x) == p01s for x in rm_list):
            pri[k] = 2
    order = np.lexsort((r.random(len(elig)), -pri))
    cascades, donors_by_rm = [], defaultdict(list)
    for key, cb in combos.items():
        donors_by_rm[key[0]].append(cb)
    resp_names = list(rc["cascade_response_mix"])
    resp_p = np.array(list(rc["cascade_response_mix"].values()))
    resp_p = resp_p / resp_p.sum()
    tried = 0
    for k in order:
        if len(cascades) >= target:
            break
        row = elig.iloc[k]
        tried += 1
        kind = "partial" if row.status == "partial" else "late"
        rm_list = cr_idx[row.production_run_id]
        rm_list = rm_list[rm_list.areq > 0]
        cand = []
        for x in rm_list.itertuples():
            if x.rm_id == p03rm and pri[k] != 3:
                continue  # P03: this RM only runs short in the weeks after its price spikes
            score = 0.0
            if x.rm_id == p03rm and pri[k] == 3:
                score += 100
            if pri[k] == 2 and prim_sup.get(x.rm_id) == p01s:
                score += 50
            score += 5 * int(rms_i.at[x.rm_id, "is_single_source"]) + 3 * (rms_i.at[x.rm_id, "criticality"] == "A")
            score -= usage.get((x.rm_id, x.plant_id), 0) / 50
            cand.append((score, x.rm_id))
        cand.sort(reverse=True)
        cid = f"CAS{len(cascades) + 1:05d}"
        rr = rng_for(ctx.seed, "cascade", row.production_run_id)
        resp = str(rr.choice(resp_names, p=resp_p)) if kind == "late" else "accept_delay"
        for _, rm_id in cand[: rc["cascade_max_attempt_rms"]]:
            cb = combos[(rm_id, row.plant_id)]
            i = int(np.where(cb.run_ids == row.production_run_id)[0][0])
            res = None
            for rsp in [resp] + (["accept_delay"] if resp != "accept_delay" else []):
                res = try_cascade(cb, i, kind, rsp, rr, donors_by_rm[rm_id], cid)
                if res:
                    break
            if res:
                res.update({"purchase_order_line_id": row.purchase_order_line_id, "fg_status": row.status})
                cascades.append(res)
                break
    log(f"cascades: {len(cascades):,} built from {tried:,} tried; eligible late/short make lines {len(elig):,}; target {target:,} ({len(cascades) / max(1, len(elig)):.1%} achieved)")

    # ---------------- co-victims and delay reasons --------------------------------
    victim = {c["production_run_id"]: c for c in cascades}
    runs["delay_reason_code"] = None
    runs["rm_shortage_rm_id"] = None
    runs["cascade_id"] = None
    co = 0
    for key, cb in combos.items():
        Sx = cb.stock()
        for (Pw, Xw) in list(cb.blocked):
            m = (cb.run_pd >= Pw - 3) & (cb.run_pd < Xw) & (cb.run_ad >= Xw) & (cb.run_ad < 10 ** 6)
            for j in np.where(m)[0]:
                rid = cb.run_ids[j]
                if rid in victim:
                    continue
                if (Sx[cb.run_pd[j]:Xw] < cb.run_areq[j] - EPS).all() and cb.run_areq[j] > 0:
                    cid = next(c["cascade_id"] for c in cascades if c["rm_id"] == key[0] and c["plant_id"] == key[1] and c["P"] == Pw and c["X"] == Xw)
                    victim[rid] = {"cascade_id": cid, "rm_id": key[0], "co_victim": True}
                    co += 1
    ri = runs.set_index("production_run_id")
    for rid, c in victim.items():
        ri.at[rid, "delay_reason_code"] = "rm_shortage"
        ri.at[rid, "rm_shortage_rm_id"] = c["rm_id"]
        ri.at[rid, "cascade_id"] = c["cascade_id"]
    runs = ri.reset_index()
    for c in cascades:
        c["co_victims"] = runs[(runs.cascade_id == c["cascade_id"]) & (runs.production_run_id != c["production_run_id"])].production_run_id.tolist()
    rr = ctx.rng("delay_reasons")
    p06 = plan.get("P06")
    need = runs.delay_reason_code.isna() & (runs.delay_days > 0) & runs.executed
    woq = ((runs.planned_start - runs.planned_start.dt.to_period("Q").dt.start_time).dt.days // 7 + 1)
    names, probs = list(NONRM_REASONS), np.array(list(NONRM_REASONS.values()))
    samp = rr.choice(names, p=probs / probs.sum(), size=len(runs))
    runs.loc[need, "delay_reason_code"] = samp[need.values]
    if p06:
        m6 = need & (runs.plant_id == p06["plant_id"]) & woq.isin(p06["weeks_of_quarter"]) & (rr.random(len(runs)) < 0.85)
        runs.loc[m6, "delay_reason_code"] = "maintenance_overrun"
    # open runs: planned but not started by the cutoff
    op = ~runs.executed
    runs.loc[op & (runs.planned_start <= w.win_end), "delay_days"] = (w.win_end - runs.loc[op & (runs.planned_start <= w.win_end), "planned_start"]).dt.days
    runs.loc[op & (runs.planned_start > w.win_end), "delay_days"] = np.nan
    open_short = 0
    for key, cb in combos.items():
        Sx = cb.stock()
        m = (cb.run_ad >= 10 ** 6) & (cb.run_pd <= w.T_end) & (cb.run_preq > 0)
        for j in np.where(m)[0]:
            if Sx[cb.run_pd[j]] < cb.run_preq[j] and (Sx[cb.run_pd[j]: w.T_end + 1] < cb.run_preq[j]).all():
                rid = cb.run_ids[j]
                ix = runs.index[runs.production_run_id == rid][0]
                if runs.at[ix, "delay_reason_code"] is None or pd.isna(runs.at[ix, "delay_reason_code"]):
                    runs.at[ix, "delay_reason_code"] = "rm_shortage"
                    runs.at[ix, "rm_shortage_rm_id"] = key[0]
                    open_short += 1
    op_wait = op & runs.delay_reason_code.isna() & (runs.delay_days > 0)
    runs.loc[op_wait, "delay_reason_code"] = "awaiting_schedule_slot"
    log(f"co-victim runs: {co}; open runs short of RM at cutoff: {open_short}")

    neg = [(k, float(cb.stock()[: w.T_end + 1].min())) for k, cb in combos.items() if cb.stock()[: w.T_end + 1].min() < -EPS]
    if neg:
        log(f"WARNING combos with negative stock after cascades: {neg[:5]}")
    # ---------------- cycle counts ------------------------------------------------
    cc_rows = []
    rcc = ctx.rng("cycle_counts")
    for key, cb in combos.items():
        n = int(rcc.poisson(rc["cycle_count_per_year"] * w.T_end / 365))
        for _ in range(n):
            d = int(rcc.integers(30, w.T_end))
            Sx = cb.stock()
            amt = round(max(0.01, float(Sx[d]) * float(rcc.uniform(*rc["cycle_count_frac"]))), 3)
            sign = -1.0 if rcc.random() < 0.55 else 1.0
            S2 = Sx + sign * np.where(np.arange(w.T) >= d, amt, 0.0)
            if not cb.ok(S2):
                continue
            if sign < 0:
                cb.xout[d] += amt
            else:
                cb.xin[d] += amt
            cc_rows.append((key, d, sign * amt))

    # ---------------- assemble tables ---------------------------------------------
    build_outputs(ctx, w, S, runs, cr, combos, cascades, cc_rows, catalog, mitig)


def build_outputs(ctx, w: World, S, runs, cr, combos, cascades, cc_rows, catalog, mitig):
    dates = w.dates
    D = lambda d: dates[int(d)] if d is not None and d < len(dates) else pd.NaT  # noqa: E731
    # ---- lines -> headers
    L = []
    for cb in combos.values():
        L.extend(cb.lines)
    for l in L:
        l["arrival_eff"] = l["arrival"] if (l["arrival"] is not None and l["arrival"] <= w.T_end and not l["cancelled"]) else None
        vis = [x for x in l["revs"] if x[0] <= w.T_end]
        l["exp_final"] = vis[-1][2] if vis else l["promised"]
        l["rev_sig"] = tuple(tuple(x) for x in vis)
        l["hkey"] = (l["sup"], l["plant"], l["ordered"], l["promised"], l["exp_final"], l["arrival_eff"], l["otype"], l["rev_sig"], l["cancelled"])
    L.sort(key=lambda l: (l["ordered"], l["sup"], l["plant"], l["lid"]))
    hk_to_id, hdr_rows = {}, []
    for l in L:
        if l["hkey"] not in hk_to_id:
            hk_to_id[l["hkey"]] = None
            hdr_rows.append(l["hkey"])
    hdr_ids = make_ids("RPO", len(hdr_rows), 6)
    for hk, hid in zip(hdr_rows, hdr_ids):
        hk_to_id[hk] = hid
    line_ids = make_ids("RPOL", len(L), 7)
    for l, lid in zip(L, line_ids):
        l["line_id"] = lid
        l["po_id"] = hk_to_id[l["hkey"]]
    lid_map = {l["lid"]: l for l in L}
    po_rows = []
    by_hdr = defaultdict(list)
    for l in L:
        by_hdr[l["po_id"]].append(l)
    for hk, hid in zip(hdr_rows, hdr_ids):
        ls = by_hdr[hid]
        l0 = ls[0]
        if l0["cancelled"]:
            status = "cancelled"
        elif l0["arrival_eff"] is None:
            status = "open"
        elif any(x["qrec"] < x["qty"] - EPS for x in ls):
            status = "partial"
        else:
            status = "received"
        po_rows.append({"rm_purchase_order_id": hid, "supplier_id": l0["sup"], "plant_id": l0["plant"], "order_type": l0["otype"],
                        "ordered_at": D(l0["ordered"]), "promised_at": D(l0["promised"]), "expected_at": D(l0["exp_final"]),
                        "received_at": D(l0["arrival_eff"]) if l0["arrival_eff"] is not None else pd.NaT, "status": status})
    rpo = pd.DataFrame(po_rows)
    rpol = pd.DataFrame([{"rm_purchase_order_line_id": l["line_id"], "rm_purchase_order_id": l["po_id"], "rm_id": l["rm"],
                          "quantity_ordered": round(l["qty"], 3),
                          "quantity_received": round(l["qrec"], 3) if l["arrival_eff"] is not None else 0.0,
                          "unit_cost": round(l["price"] * (1 + (ctx.cfg["rm_ops"]["spot_price_premium"] if l["otype"] == "spot_buy" else 0)), 4)} for l in L])
    # revisions (header level)
    rev_rows = []
    for hk, hid in zip(hdr_rows, hdr_ids):
        for ra, old, new, reason in hk[7]:
            if ra <= w.T_end:
                rev_rows.append({"rm_po_id": hid, "revised_at": D(ra), "old_expected_at": D(old), "new_expected_at": D(new), "reason_code": reason})
    rev = pd.DataFrame(rev_rows).sort_values(["revised_at", "rm_po_id"]).reset_index(drop=True)
    rev.insert(0, "revision_id", make_ids("REV", len(rev), 6))

    # ---- movements
    mv = []
    for l in L:
        if l["arrival_eff"] is not None:
            mv.append((l["rm"], l["plant"], l["arrival_eff"], "receipt", round(l["qrec"], 3), l["line_id"], None, None))
            if l["reject"] > 0:
                mv.append((l["rm"], l["plant"], l["arrival_eff"], "scrap", -round(l["reject"], 3), l["line_id"], None, None))
    cons = cr[(cr.areq > 0)].copy()
    for t in cons.itertuples():
        mv.append((t.rm_id, t.plant_id, int(t.ad), "consumption", -round(t.areq, 3), None, None, t.production_run_id))
    transfers = [c["transfer"] for c in cascades if c.get("transfer")]
    tr_ids = make_ids("RT", len(transfers), 7)
    for tr, tid in zip(transfers, tr_ids):
        tr["transfer_id"] = tid
        mv.append((tr["rm"], tr["donor_plant"], tr["out_day"], "transfer", -round(tr["qty"], 3), None, tid, None))
        mv.append((tr["rm"], tr["receiver_plant"], tr["in_day"], "transfer", round(tr["qty"], 3), None, tid, None))
    for (rm, pl), d, amt in cc_rows:
        mv.append((rm, pl, d, "adjustment", round(amt, 3), None, None, None))
    mvd = pd.DataFrame(mv, columns=["rm_id", "plant_id", "day", "movement_type", "quantity_change", "rm_purchase_order_line_id", "transfer_id", "production_run_id"])
    mvd = mvd.sort_values(["day", "rm_id", "plant_id", "movement_type"]).reset_index(drop=True)
    mvd["movement_at"] = dates[mvd.day.values]
    mvd.insert(0, "rm_movement_id", make_ids("RMM", len(mvd), 8))

    # ---- opening balances (rounded) and snapshots from the rounded movements
    ob = pd.DataFrame([{"rm_opening_balance_id": f"{k[0]}-{k[1]}", "rm_id": k[0], "plant_id": k[1], "balance_date": w.t0,
                        "opening_units": round(cb.opening, 3)} for k, cb in combos.items()])
    daily = mvd.groupby(["rm_id", "plant_id", "day"]).quantity_change.sum()
    sundays = [d for d in range(w.T_end + 1) if dates[d].dayofweek == 6]
    snap = []
    for key, cb in combos.items():
        arr = np.zeros(w.T_end + 1)
        try:
            dd = daily.loc[key]
            arr[dd.index.values.astype(int)] = dd.values
        except KeyError:
            pass
        Sx = round(cb.opening, 3) + np.cumsum(arr)
        blocked = np.zeros(w.T_end + 1, dtype=bool)
        for a, b in cb.blocked:
            blocked[a: min(b, w.T_end + 1)] = True
        pr = np.zeros(w.T + 60)
        np.add.at(pr, np.minimum(np.maximum(cb.run_pd, 0), w.T + 59), cb.run_preq)
        cpr = np.cumsum(pr)
        for s_ in sundays:
            nxt = (cpr[min(s_ + 28, len(cpr) - 1)] - cpr[s_]) / 28
            wk = slice(max(0, s_ - 6), s_ + 1)
            snap.append((key[0], key[1], dates[s_], round(float(Sx[s_]), 3), round(float(cb.ss_arr[s_]), 3),
                         round(float(Sx[s_] / nxt), 1) if nxt > EPS else np.nan,
                         int(blocked[wk].any() or (Sx[wk] <= EPS).any())))
    snaps = pd.DataFrame(snap, columns=["rm_id", "plant_id", "snapshot_date", "on_hand_units", "safety_stock_units", "days_of_cover", "stockout_flag"])

    # ---- production runs table
    pr_tab = runs.copy()
    pr_tab["delay_days"] = pr_tab.delay_days.astype("Int64")
    pr_tab["actual_start"] = pr_tab.actual_start

    # ---- catalog ids
    catalog = catalog.sort_values(["rm_id", "price_valid_from", "supplier_id"]).reset_index(drop=True)
    catalog.insert(0, "catalog_id", make_ids("CAT", len(catalog), 5))
    rms = S["rms"].copy()

    write_table(ctx, "rm_supplier_catalog", catalog)
    write_table(ctx, "rm_inventory_opening_balances", ob)
    write_table(ctx, "production_runs", pr_tab)
    write_table(ctx, "rm_purchase_orders", rpo)
    write_table(ctx, "rm_purchase_order_lines", rpol)
    write_table(ctx, "rm_po_revisions", rev)
    write_table(ctx, "rm_inventory_movements", mvd)
    write_table(ctx, "rm_inventory_snapshots_weekly", snaps)

    # ---- internal artefacts for Stage 3 ------------------------------------------
    rev_by_po = rev.groupby("rm_po_id").revision_id.apply(list).to_dict()
    for c in cascades:
        for s_ in c["slipped"]:
            l = lid_map[s_["lid"]]
            s_.update({"rm_purchase_order_line_id": l["line_id"], "rm_purchase_order_id": l["po_id"], "supplier_id": l["sup"],
                       "qty": l["qty"], "price": l["price"], "revision_ids": rev_by_po.get(l["po_id"], []), "cancelled": l["cancelled"]})
        c["new_lines"] = [{"rm_purchase_order_line_id": lid_map[x]["line_id"], "rm_purchase_order_id": lid_map[x]["po_id"], "supplier_id": lid_map[x]["sup"],
                           "qty": lid_map[x]["qty"], "price": lid_map[x]["price"], "otype": lid_map[x]["otype"]} for x in c["new_lids"]]
        for k in ("t_revised", "t_below_ss", "P", "A", "X", "slip_to", "d_dec"):
            c[k + "_date"] = D(c[k]) if c[k] is not None else None
    line_tab = pd.DataFrame([{"rm_purchase_order_line_id": l["line_id"], "rm_purchase_order_id": l["po_id"], "rm_id": l["rm"], "plant_id": l["plant"],
                              "supplier_id": l["sup"], "ordered": D(l["ordered"]), "promised": D(l["promised"]), "arrival": D(l["arrival_eff"]) if l["arrival_eff"] is not None else pd.NaT,
                              "tags": ",".join(l["tags"]), "cause": l["cause"], "cascade_id": l["cascade"], "pulled_in": l["pulled_in"], "emergency": l["emergency"],
                              "second_slip": l.get("second_slip", False), "otype": l["otype"], "qty": l["qty"], "qrec": l["qrec"], "reject": l["reject"], "price": l["price"]} for l in L])
    for m in mitig["ss_builds"]:
        m["date"] = pd.Timestamp(m["date"])
    ctx.save_internal("rm_ops", {"cascades": cascades, "mitigations": mitig, "line_facts": line_tab, "t0": w.t0, "catalog": catalog,
                                 "ss": {k: (cb.ss_days_at(w.T_end), float(cb.dbar)) for k, cb in combos.items()},
                                 "transfers": transfers, "runs_internal": runs[["production_run_id", "cascade_id", "planned_lead", "transport_noise"]]})

    # ---- summary
    n_pr = len(pr_tab)
    log(f"RM POs {len(rpo):,} / lines {len(rpol):,} (lines per PO {len(rpol) / len(rpo):.2f}); status {rpo.status.value_counts().to_dict()}")
    lt = line_tab[line_tab.arrival.notna() & (line_tab.otype == "regular")]
    lat = (lt.arrival - lt.promised).dt.days
    log(f"RM line lateness: late>0 {float((lat > 0).mean()):.1%}, mean delay {float(lat.clip(lower=0).mean()):.2f}d; pulled-in by safety pass {int(line_tab.pulled_in.sum())}, emergency buys {int(line_tab.emergency.sum())}")
    log(f"revisions {len(rev):,}; movements {len(mvd):,} {mvd.movement_type.value_counts().to_dict()}")
    log(f"snapshots {len(snaps):,}; stockout weeks {int(snaps.stockout_flag.sum()):,}; min on-hand {snaps.on_hand_units.min():.3f}")
    log(f"production runs {n_pr:,}; reasons {pr_tab.delay_reason_code.value_counts().to_dict()}")
    log(f"cascade responses: {pd.Series([c['response'] for c in cascades]).value_counts().to_dict()}; kinds {pd.Series([c['kind'] for c in cascades]).value_counts().to_dict()}")
