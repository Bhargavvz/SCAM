"""Deterministic what-if simulator for raw-material supply disruptions.

Port of dataset/code/gen_eval.py::holdout_scenarios (the generator's consequence model) over the
as-of SQLite views, using the generator's own cost functions in dataset/code/consequences.py.
Same DB + same inputs -> same numbers. The LLM never produces cost / stockout figures itself.
"""
from __future__ import annotations

import importlib.util
import os
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from agent.config import ROOT

TAXONOMY = ("expedite", "switch_supplier", "substitute_rm", "reallocate_stock", "cancel_po",
            "build_safety_stock", "renegotiate", "accept_delay", "reduce_allocation")
SIMULATED = ("accept_delay", "expedite", "switch_supplier", "reallocate_stock", "substitute_rm", "cancel_po")
RULES_PATH = Path(__file__).with_name("sim_rules.yaml")


def _load_consequences():
    spec = importlib.util.spec_from_file_location("sc_consequences", Path(os.environ.get("DATASET_DIR") or ROOT / "dataset") / "code" / "consequences.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cq = _load_consequences()


class SimulationError(ValueError):
    """The inputs cannot be turned into a scenario state (missing PO, revision, run or BOM line)."""


@dataclass(frozen=True)
class ScenarioState:
    day0: date
    rpo_id: str
    supplier_id: str
    plant_id: str
    rm_id: str
    uom: str
    eta: date
    revision_id: str
    primary_run_id: str
    run_ids: tuple[str, ...]
    planned_start: date
    req: float
    daily_block_cost: float
    service_units: int
    lot_value: float
    lot_rpo_ids: tuple[str, ...]
    lead_days: int
    congested: bool
    q4_slip_risk: bool

    def to_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, date) else list(v) if isinstance(v, tuple) else v)
                for k, v in asdict(self).items()}


def load_rules(path: Path = RULES_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def _one(con, sql, params, what):
    row = con.execute(sql, params).fetchone()
    if row is None:
        raise SimulationError(what)
    return row


def _sunday_on_or_before(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _snapshot(con, rm_id, plant_id, d: date) -> tuple[float, float]:
    row = con.execute("""SELECT on_hand_units, safety_stock_units FROM rm_inventory_snapshots_weekly
                         WHERE rm_id = ? AND plant_id = ? AND snapshot_date = ?""",
                      (rm_id, plant_id, _sunday_on_or_before(d).isoformat())).fetchone()
    return (float(row[0]), float(row[1])) if row else (math.nan, math.nan)


def build_state(con, *, rpo_id: str, run_ids, day0: str, rm_id: str | None = None, rules=None) -> ScenarioState:
    rules = rules or load_rules()
    d0 = date.fromisoformat(day0)
    run_ids = list(run_ids or [])
    if not run_ids:
        raise SimulationError("at least one production_run_id is required (the run that needs the material)")
    po = _one(con, "SELECT supplier_id, plant_id FROM rm_purchase_orders WHERE rm_purchase_order_id = ?",
              (rpo_id,), f"{rpo_id} not found (or ordered after the as-of date)")
    sup, pl = po["supplier_id"], po["plant_id"]
    rms = [r[0] for r in con.execute("""SELECT DISTINCT rm_id FROM rm_purchase_order_lines
                                        WHERE rm_purchase_order_id = ? ORDER BY rm_id""", (rpo_id,))]
    if rm_id is None:
        if len(rms) != 1:
            raise SimulationError(f"{rpo_id} has {len(rms)} materials; pass rm_id")
        rm_id = rms[0]
    elif rm_id not in rms:
        raise SimulationError(f"{rm_id} is not on {rpo_id}")
    rev = _one(con, """SELECT revision_id, revised_at, new_expected_at FROM rm_po_revisions
                       WHERE rm_po_id = ? AND revised_at <= ? ORDER BY revised_at DESC, revision_id DESC LIMIT 1""",
               (rpo_id, day0), f"{rpo_id} has no date revision on or before {day0}; nothing to simulate")
    run = _one(con, "SELECT product_id, planned_start, planned_qty FROM production_runs WHERE production_run_id = ?",
               (run_ids[0],), f"run {run_ids[0]} not found")
    ps = date.fromisoformat(run["planned_start"])
    bom = _one(con, """SELECT qty_per_unit, scrap_pct FROM bill_of_materials
                       WHERE product_id = ? AND rm_id = ? AND effective_from <= ?
                         AND (effective_to IS NULL OR effective_to = '' OR effective_to >= ?)
                       ORDER BY bom_version DESC LIMIT 1""",
               (run["product_id"], rm_id, ps.isoformat(), ps.isoformat()), f"run {run_ids[0]} does not consume {rm_id}")
    ph = ",".join("?" * len(run_ids))
    vr = con.execute(f"""SELECT r.production_run_id, r.planned_qty, x.unit_price FROM production_runs r
                         JOIN products_ext x USING (product_id) WHERE r.production_run_id IN ({ph})""",
                     tuple(run_ids)).fetchall()
    missing = set(run_ids) - {r["production_run_id"] for r in vr}
    if missing:
        raise SimulationError(f"runs not found: {sorted(missing)}")
    # the "slipped lot" = every RPO for this RM and plant whose date moved on the same day
    lot = con.execute("""SELECT o.rm_purchase_order_id, SUM(l.quantity_ordered * l.unit_cost) AS v
                         FROM rm_purchase_orders o JOIN rm_purchase_order_lines l USING (rm_purchase_order_id)
                         WHERE l.rm_id = ? AND o.plant_id = ?
                           AND o.rm_purchase_order_id IN (SELECT rm_po_id FROM rm_po_revisions WHERE revised_at = ?)
                         GROUP BY o.rm_purchase_order_id ORDER BY o.rm_purchase_order_id""",
                      (rm_id, pl, rev["revised_at"])).fetchall()
    lead = _one(con, """SELECT lead_time_days FROM rm_supplier_catalog WHERE rm_id = ? AND supplier_id = ?
                        ORDER BY catalog_id LIMIT 1""", (rm_id, sup), f"{sup} has no catalog row for {rm_id}")[0]
    country = con.execute("SELECT country_code FROM suppliers WHERE supplier_id = ?", (sup,)).fetchone()
    uom = con.execute("SELECT uom FROM raw_materials WHERE rm_id = ?", (rm_id,)).fetchone()[0]
    eta = date.fromisoformat(rev["new_expected_at"])
    value = sum(r["planned_qty"] * r["unit_price"] for r in vr)
    return ScenarioState(
        day0=d0, rpo_id=rpo_id, supplier_id=sup, plant_id=pl, rm_id=rm_id, uom=uom, eta=eta,
        revision_id=rev["revision_id"], primary_run_id=run_ids[0], run_ids=tuple(run_ids), planned_start=ps,
        req=run["planned_qty"] * bom["qty_per_unit"] * (1 + bom["scrap_pct"] / 100),
        daily_block_cost=cq.block_cost_per_day(value, {"memory": {"stockout_cost_rate_per_day": rules["stockout_cost_rate_per_day"]}}),
        service_units=int(sum(r["planned_qty"] for r in vr)),
        lot_value=float(sum(r["v"] for r in lot)), lot_rpo_ids=tuple(r[0] for r in lot), lead_days=int(lead),
        congested=d0.month == 2 and country is not None and country[0] == rules["feb_congestion_country"],
        q4_slip_risk=sup == rules["q4_slip_supplier_id"] and eta.month in (11, 12))


def _opt(action, **kw) -> dict:
    return {"action": action, "feasible": True, "supported": True, "breaches_commitments": [], **kw}


def _infeasible(action, note) -> dict:
    return {"action": action, "feasible": False, "supported": True, "note": note}


def _best_transfer(con, st: ScenarioState, rules) -> dict | None:
    xy = rules["region_xy"]
    regions = {r[0]: r[1] for r in con.execute("SELECT plant_id, region FROM plants")}
    donors = [r[0] for r in con.execute("""SELECT DISTINCT plant_id FROM rm_inventory_snapshots_weekly
                                           WHERE rm_id = ? AND plant_id <> ? ORDER BY plant_id""", (st.rm_id, st.plant_id))]
    best = None
    for k in donors:
        oh, ss = _snapshot(con, st.rm_id, k, st.day0)
        if math.isnan(oh) or oh - st.req < 0:
            continue
        td = cq.transfer_days(math.dist(xy.get(regions[k], [0, 0]), xy.get(regions[st.plant_id], [0, 0])))
        arr = st.day0 + timedelta(days=td)
        so = max(0, (arr - st.planned_start).days)
        depleting = k == rules["depleting_donor_plant_id"]
        cand = _opt("reallocate_stock", donor_plant=k, arrival=arr, stockout_days=so,
                    cost=round(rules["transfer_cost_per_unit_value"] * st.lot_value + so * st.daily_block_cost, 0),
                    risk="high" if (depleting or oh - st.req < ss) else "medium",
                    note=f"donor on hand {oh:,.1f} vs SS {ss:,.1f}"
                         + ("; transfers from this plant have repeatedly left it short" if depleting else ""),
                    evidence_record_ids=[f"{st.rm_id}|{k}|{_sunday_on_or_before(st.day0).isoformat()}"])
        # Verbatim from the generator: (risk, cost) tuples compare risk as a string, so "high" < "medium".
        # Kept for parity with holdout_scenarios.json; documented in README "Known limitations".
        if best is None or (cand["risk"], cand["cost"]) < (best["risk"], best["cost"]):
            best = cand
    return best


def _substitute(con, st: ScenarioState, rules) -> dict | None:
    sg = con.execute("SELECT substitute_group_id FROM raw_materials WHERE rm_id = ?", (st.rm_id,)).fetchone()[0]
    if not sg:
        return None
    subs = [r[0] for r in con.execute("""SELECT rm_id FROM raw_materials WHERE substitute_group_id = ? AND rm_id <> ?
                                         ORDER BY rm_id""", (sg, st.rm_id))]
    ok = [x for x in subs if _snapshot(con, x, st.plant_id, st.day0)[0] >= st.req]  # NaN compares False
    if not ok:
        return None
    x = ok[0]
    arr = st.day0 + timedelta(days=rules["substitute_qa_days"])
    dq = max(0, (arr - st.planned_start).days)
    risky = x == rules["risky_substitute_rm_id"]
    return _opt("substitute_rm", substitute=x, arrival=arr, stockout_days=dq,
                cost=round(rules["substitute_fixed_cost"] + dq * st.daily_block_cost, 0),
                risk="high" if risky else "medium",
                note="QA approval ~6 days" + ("; this substitute has a history of incoming rejects" if risky else ""),
                evidence_record_ids=[x])


def _cancel(con, st: ScenarioState, rules, switch: dict, d_acc: int) -> dict:
    d0 = st.day0.isoformat()
    rows = con.execute("""SELECT commitment_id, commitment_text FROM commitments
                          WHERE made_at <= ? AND (resolved_at IS NULL OR resolved_at > ?) AND counterparty_id IN (?, ?)
                          ORDER BY commitment_id""", (d0, d0, st.supplier_id, st.plant_id)).fetchall()
    vol = [r["commitment_id"] for r in rows if "We order at least" in (r["commitment_text"] or "")]
    base = switch["cost"] if switch["feasible"] else d_acc * st.daily_block_cost
    penalty = rules["volume_breach_rate"] * st.lot_value * rules["volume_breach_multiplier"] if vol else 0
    # the generator re-buys via the switch lane; with no alternative source the arrival is unknown (None)
    return _opt("cancel_po", arrival=switch.get("arrival"), stockout_days=switch.get("stockout_days", d_acc),
                cost=round(base + rules["cancel_fee_rate"] * st.lot_value + penalty, 0),
                risk="high" if vol else "medium",
                note=("cancelling would breach open volume commitment " + ", ".join(vol)) if vol
                     else "cancel and re-buy elsewhere",
                breaches_commitments=vol, evidence_record_ids=[st.rpo_id, *vol])


def simulate_options(con, st: ScenarioState, rules=None) -> list[dict]:
    rules = rules or load_rules()
    d0, P, daily, lv = st.day0, st.planned_start, st.daily_block_cost, st.lot_value

    def late(arr: date) -> int:
        return max(0, (arr - P).days)

    acts = []
    d_acc = late(st.eta)
    acts.append(_opt("accept_delay", arrival=st.eta, stockout_days=d_acc, cost=round(d_acc * daily, 0),
                     risk="high" if st.q4_slip_risk else "low",
                     note="supplier historically slips a further 7-14 days in Nov-Dec" if st.q4_slip_risk else "supplier date",
                     evidence_record_ids=[st.rpo_id, st.revision_id, st.primary_run_id]))
    pct = cq.rm_expedite_premium_pct(st.lead_days, st.congested)
    e_arr = d0 + timedelta(days=cq.rm_expedite_days(st.lead_days, st.congested))
    acts.append(_opt("expedite", arrival=e_arr, stockout_days=late(e_arr), cost=round(pct * lv + late(e_arr) * daily, 0),
                     risk="medium", note=f"air premium {pct:.0%} of lot value", evidence_record_ids=list(st.lot_rpo_ids)))
    alt = con.execute("""SELECT catalog_id, supplier_id, lead_time_days, unit_price FROM rm_supplier_catalog
                         WHERE rm_id = ? AND supplier_id <> ? AND qualified_flag = 1
                           AND price_valid_from <= ? AND price_valid_to >= ?
                         ORDER BY allocation_pct DESC, catalog_id LIMIT 1""",
                      (st.rm_id, st.supplier_id, d0.isoformat(), d0.isoformat())).fetchone()
    if alt:
        s_arr = d0 + timedelta(days=cq.spot_lead_days(int(alt["lead_time_days"])))
        acts.append(_opt("switch_supplier", arrival=s_arr, stockout_days=late(s_arr),
                         cost=round(rules["spot_price_premium"] * st.req * float(alt["unit_price"]) + late(s_arr) * daily, 0),
                         risk="medium", note=f"spot lot from {alt['supplier_id']}", alt_supplier_id=alt["supplier_id"],
                         evidence_record_ids=[alt["catalog_id"]]))
    else:
        acts.append(_infeasible("switch_supplier", "no other qualified source in the catalog at day 0"))
    acts.append(_best_transfer(con, st, rules) or _infeasible("reallocate_stock", "no other plant holds enough of this material"))
    acts.append(_substitute(con, st, rules) or _infeasible("substitute_rm", "no qualified substitute in stock at the plant"))
    acts.append(_cancel(con, st, rules, switch=acts[2], d_acc=d_acc))
    for a in acts:
        if a["feasible"]:
            a["score"] = round(cq.total_score(a["cost"], 0, 0, a["risk"]), 0)
            a["service_impact_units"] = st.service_units if a["stockout_days"] > 0 else 0
            a["arrival"] = a["arrival"].isoformat() if a["arrival"] else None
    for action in TAXONOMY:
        if action not in SIMULATED:
            acts.append({"action": action, "feasible": False, "supported": False,
                         "note": "not modelled by the deterministic simulator - no cost or stockout estimate available"})
    return acts


def simulate_action(con, action_type: str, *, rpo_id: str, run_ids, day0: str, rm_id: str | None = None,
                    rules=None) -> dict:
    if action_type not in TAXONOMY:
        raise SimulationError(f"unknown action_type {action_type!r}; expected one of {TAXONOMY}")
    st = build_state(con, rpo_id=rpo_id, run_ids=run_ids, day0=day0, rm_id=rm_id, rules=rules)
    return next(o for o in simulate_options(con, st, rules) if o["action"] == action_type)


def best_option(options: list[dict]) -> dict | None:
    ok = [o for o in options if o.get("feasible") and o.get("supported", True) and not o.get("breaches_commitments")]
    return min(ok, key=lambda o: (o["score"], o["action"])) if ok else None
